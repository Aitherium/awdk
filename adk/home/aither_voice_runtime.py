"""The Aither voice on this machine: fetch it verified, speak with it, no service needed.

ONE FILE, TWO PACKAGES. This module ships byte-identical in awdk
(``adk/home/aither_voice_runtime.py``) and in awvoice (``awvoice/aither_voice_runtime.py``)
so both speak the same voice from the same cache with the same phonemes; a test
compares the two copies byte for byte. Keep it self-contained: standard library at
import time, ``numpy`` / ``onnxruntime`` / ``espeakng-loader`` only when speaking.

THE VOICE is a Piper/VITS en-US model (MIT): ``aither-voice.onnx`` plus its
``aither-voice.onnx.json``. Both are pinned below by size AND sha256, and a file is
moved into place only after its digest matched. In-progress bytes live in
``<file>.part`` and the next run resumes them (HTTP Range).

THE CACHE is the local model directory every Aitherium installer shares:
``$AITHER_VOICE_DIR``, else ``$AITHER_BONSAI_ROOT/models``, else
``%LOCALAPPDATA%/Aitherium/bonsai/models`` on Windows and
``~/.aitherium/bonsai/models`` elsewhere. ``adk models pull aither-voice``,
``adk home voice --local aither`` and ``awvoice ... --voice local:aither`` all land
in, and read from, that one place.

PHONEMES follow piper-phonemize: espeak-ng IPA per clause, the clause terminator
decides the punctuation and the sentence break, codepoints are NFD-decomposed, and the
ids are BOS, then (phoneme, PAD) pairs, then EOS. Why not the ``piper-tts`` package:
it ships only abi3 wheels, which a free-threaded CPython cannot load;
``espeakng-loader`` is a ``py3-none`` wheel that bundles libespeak-ng and its data.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

VOICE_ID = "aither-voice"
RELEASE = "aither-voice-v1"
_GH = "https://github.com/Aitherium/aitherkvcache/releases/download/" + RELEASE + "/"


class Pin:
    """One file of the voice: where it lives and the exact bytes it must be."""

    def __init__(self, file: str, size: int, sha256: str, urls: Sequence[str]) -> None:
        self.file, self.size, self.sha256, self.urls = file, size, sha256, tuple(urls)


#: The published bytes. The release stores each file as a single ``.part0`` asset,
#: so the GitHub URL is the same bytes under that name; the digest judges every source.
MODEL = Pin("aither-voice.onnx", 63511038,
            "ff3c7973e0ccdc89ebc5913c728cb4805b4bf6ad09af25f649ff16e3316c461e",
            ("https://artifact.aitherium.com/" + RELEASE + "/aither-voice.onnx",
             "https://weights.aitherium.com/" + RELEASE + "/aither-voice.onnx",
             _GH + "aither-voice.onnx.part0"))
CONFIG = Pin("aither-voice.onnx.json", 7088,
             "754bd23cd559811254498ab78880e2ff21ab7aa67d9e2b20c24cf49295afdc29",
             ("https://artifact.aitherium.com/" + RELEASE + "/aither-voice.onnx.json",
              "https://weights.aitherium.com/" + RELEASE + "/aither-voice.onnx.json",
              _GH + "aither-voice.onnx.json.part0"))
PINS = (MODEL, CONFIG)

#: Cloudflare answers the default Python-urllib agent with 403 (error 1010).
UA = "aither-voice/1 (+https://aitherium.com)"
CHUNK = 1024 * 1024
MAX_STALLS = 4
TIMEOUT = 60.0

Say = Callable[[str], None]


class VoiceError(RuntimeError):
    """The voice could not be fetched and verified, or could not speak."""


class RuntimeMissingError(VoiceError):
    """onnxruntime / espeakng-loader / numpy are not installed (an optional extra)."""


def _quiet(_msg: str) -> None:
    return None


# --------------------------------------------------------------------------- cache


def default_voice_dir() -> Path:
    """The shared local model directory (see the module docstring)."""
    explicit = os.environ.get("AITHER_VOICE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get("AITHER_BONSAI_ROOT", "").strip()
    if root:
        return Path(root) / "models"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Aitherium" / "bonsai" / "models"
    return Path.home() / ".aitherium" / "bonsai" / "models"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def installed(voice_dir: Optional[Path] = None) -> bool:
    """Both files present at their pinned sizes (the digest was checked on arrival)."""
    d = Path(voice_dir or default_voice_dir())
    return all((d / p.file).is_file() and (d / p.file).stat().st_size == p.size for p in PINS)


def _fetch_once(url: str, part: Path, size: int, timeout: float) -> int:
    """One ranged GET appended to ``part``. -> bytes on disk afterwards."""
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": UA}
    if have:
        headers["Range"] = "bytes=%d-" % have
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if have and getattr(resp, "status", 200) != 206:
            have = 0  # the server ignored the Range: start over, never glue
        with part.open("ab" if have else "wb") as fh:
            while have < size:
                block = resp.read(min(CHUNK, size - have))
                if not block:
                    break
                fh.write(block)
                have += len(block)
            if have == size and resp.read(1):
                raise VoiceError("%s sent more than the pinned %d bytes" % (url, size))
    return part.stat().st_size


def fetch_pinned(pin: Pin, voice_dir: Path, say: Say = _quiet,
                 timeout: float = TIMEOUT) -> Path:
    """``voice_dir/pin.file``, downloaded (resuming) and sha256-verified, or raise.

    A file already present at the right size and digest is kept (idempotent). A file
    that fails its digest is deleted and fetched again. Bytes that never match are
    never moved into place.

    Raises:
        VoiceError: No source produced the pinned bytes.
    """
    voice_dir = Path(voice_dir)
    voice_dir.mkdir(parents=True, exist_ok=True)
    dest = voice_dir / pin.file
    if dest.is_file():
        if dest.stat().st_size == pin.size and sha256_file(dest) == pin.sha256:
            return dest
        say("  %s on disk is not the pinned file; fetching it again" % pin.file)
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    errors: List[str] = []
    for url in pin.urls:
        if part.exists() and part.stat().st_size > pin.size:
            part.unlink()
        stalls = 0
        say("  %s from %s" % (pin.file, url.split("/")[2]))
        while True:
            before = part.stat().st_size if part.exists() else 0
            if before == pin.size:
                break
            try:
                got = _fetch_once(url, part, pin.size, timeout)
            except urllib.error.HTTPError as exc:
                errors.append("%s: HTTP %s" % (url, exc.code))
                break
            except (urllib.error.URLError, OSError) as exc:
                got = part.stat().st_size if part.exists() else 0
                say("  connection dropped (%s); resuming" % type(exc).__name__)
            stalls = 0 if got > before else stalls + 1
            if stalls >= MAX_STALLS:
                errors.append("%s: stalled at %d of %d bytes" % (url, got, pin.size))
                break
        if not part.exists() or part.stat().st_size != pin.size:
            continue
        digest = sha256_file(part)
        if digest != pin.sha256:
            part.unlink()
            errors.append("%s: sha256 %s... is not the pinned %s..."
                          % (url, digest[:12], pin.sha256[:12]))
            continue
        os.replace(part, dest)
        return dest
    raise VoiceError("refusing %s: no source produced the pinned bytes (%s)"
                     % (pin.file, "; ".join(errors) or "no source URL"))


def ensure_voice(voice_dir: Optional[Path] = None, say: Say = _quiet,
                 timeout: float = TIMEOUT) -> Tuple[Path, Path]:
    """Fetch (or keep) both files of the voice. -> (model path, config path)."""
    d = Path(voice_dir or default_voice_dir())
    return (fetch_pinned(MODEL, d, say, timeout), fetch_pinned(CONFIG, d, say, timeout))


# --------------------------------------------------------------------------- names

#: How the voice SAYS our names: word, respelling, prefix (also matches the front of a
#: CamelCase name: AitherVeil -> Eigh-ther Veil). "Aither" is AY-ther, never EE-ther;
#: "Eigh-ther" is what espeak-ng reads as AY-ther. Longest entries apply first.
LEXICON: Tuple[Tuple[str, str, bool], ...] = (
    ("AitherOS", "Eigh-ther O S", False),
    ("Aither", "Eigh-ther", True),
    ("awdesk", "aw desk", False),
    ("awnix", "aw nix", False),
    ("awdk", "A W D K", False),
    ("adk", "A D K", False),
    ("awsh", "aw shell", False),
    ("awvoice", "aw voice", False),
)


def _rules():
    rules = []
    for word, say, prefix in sorted(LEXICON, key=lambda e: -len(e[0])):
        tail = r"(?=(?-i:[A-Z0-9])|\b)" if prefix else r"\b"
        rules.append((re.compile(r"\b" + re.escape(word) + tail, re.IGNORECASE), say))
    return rules


_RULES = _rules()


def respell(text: str) -> str:
    """``text`` as it should be SAID (a CamelCase tail after a prefix is split off)."""
    out = text or ""
    for rx, say in _RULES:
        out = rx.sub(lambda m, s=say: s + (
            " " if m.end() < len(m.string) and m.string[m.end()].isupper() else ""), out)
    return out


# --------------------------------------------------------------------------- phonemes

_ESPEAK_LOCK = threading.Lock()
_ESPEAK: Dict[str, object] = {"lib": None, "voice": None}
_CLAUSE_TYPE_SENTENCE = 0x00080000
_CLAUSE_PUNCT = {  # terminator & 0x000FFFFF -> the punctuation piper emits
    40 | 0x00000000 | 0x00080000: ".",
    20 | 0x00001000 | 0x00040000: ",",
    40 | 0x00002000 | 0x00080000: "?",
    45 | 0x00003000 | 0x00080000: "!",
    30 | 0x00000000 | 0x00040000: ":",
    30 | 0x00001000 | 0x00040000: ";",
}


def _espeak(voice: str):
    if _ESPEAK["lib"] is None:
        try:
            import espeakng_loader
        except ImportError as exc:
            raise RuntimeMissingError("espeakng-loader is not installed") from exc
        data = espeakng_loader.get_data_path()
        os.environ.setdefault("ESPEAK_DATA_PATH", str(data))
        lib = ctypes.cdll.LoadLibrary(str(espeakng_loader.get_library_path()))
        lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                                          ctypes.c_int]
        lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
        lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                              ctypes.c_int]
        if hasattr(lib, "espeak_TextToPhonemesWithTerminator"):
            f = lib.espeak_TextToPhonemesWithTerminator
            f.restype = ctypes.c_char_p
            f.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_int)]
        if lib.espeak_Initialize(0x02, 0, str(Path(data).parent).encode(), 0) <= 0:
            raise VoiceError("espeak-ng failed to initialise")
        _ESPEAK["lib"] = lib
    lib = _ESPEAK["lib"]
    if _ESPEAK["voice"] != voice:
        if lib.espeak_SetVoiceByName(voice.encode()) != 0:  # type: ignore[attr-defined]
            raise VoiceError("espeak-ng has no voice %r" % voice)
        _ESPEAK["voice"] = voice
    return lib


def phonemize(text: str, voice: str = "en-us") -> List[List[str]]:
    """Sentences of phoneme codepoints, as piper-phonemize returns them."""
    with _ESPEAK_LOCK:
        lib = _espeak(voice)
        if not hasattr(lib, "espeak_TextToPhonemesWithTerminator"):
            return _phonemize_split(lib, text)
        buf = ctypes.create_string_buffer(text.encode("utf-8"))
        ptr = ctypes.c_void_p(ctypes.addressof(buf))
        sentences: List[List[str]] = []
        cur: List[str] = []
        while ptr.value:
            term = ctypes.c_int(0)
            raw = lib.espeak_TextToPhonemesWithTerminator(ctypes.byref(ptr), 1, 0x02,
                                                          ctypes.byref(term))
            t = term.value
            cur.extend(unicodedata.normalize("NFD", (raw or b"").decode("utf-8", "ignore")))
            p = _CLAUSE_PUNCT.get(t & 0x000FFFFF)
            if p:
                cur.append(p)
            if t & _CLAUSE_TYPE_SENTENCE:
                sentences.append(cur)
                cur = []
            else:
                cur.append(" ")
        if cur:
            sentences.append(cur)
    return [s for s in sentences if any(c.strip() for c in s)]


def _clause_ipa(lib, text: str) -> str:
    buf = ctypes.create_string_buffer(text.encode("utf-8"))
    ptr = ctypes.c_void_p(ctypes.addressof(buf))
    parts = []
    while ptr.value:
        parts.append((lib.espeak_TextToPhonemes(ctypes.byref(ptr), 1, 0x02) or b"")
                     .decode("utf-8", "ignore"))
    return " ".join(p for p in parts if p)


def _phonemize_split(lib, text: str) -> List[List[str]]:
    """The bundled upstream espeak-ng has no WithTerminator call, so punctuation and
    sentence breaks are taken from the text itself."""
    sentences: List[List[str]] = []
    cur: List[str] = []
    for chunk, punct in re.findall(r"([^.!?;:,]+)([.!?;:,]*)", text):
        ipa = _clause_ipa(lib, chunk.strip())
        if not ipa:
            continue
        cur.extend(unicodedata.normalize("NFD", ipa))
        p = punct[0] if punct else ""
        if p:
            cur.append(p)
        if p in ".!?":
            sentences.append(cur)
            cur = []
        else:
            cur.append(" ")
    if cur:
        sentences.append(cur)
    return [s for s in sentences if any(c.strip() for c in s)]


def phoneme_ids(phonemes: Sequence[str], id_map: Dict[str, List[int]]) -> List[int]:
    """BOS, then (phoneme, PAD) for every phoneme the voice knows, then EOS.

    A phoneme absent from the map is dropped, as piper does.
    """
    out = list(id_map["^"])
    for ph in phonemes:
        if ph in id_map:
            out += id_map[ph] + id_map["_"]
    return out + list(id_map["$"])


# --------------------------------------------------------------------------- speech


class PiperVoice:
    """One loaded voice. :meth:`synthesize` returns 16-bit mono WAV bytes."""

    def __init__(self, model: Path, config: Optional[Path] = None) -> None:
        try:
            import numpy  # noqa: F401 -- needed by synthesize; fail at load, not mid-line
            import onnxruntime
        except ImportError as exc:
            raise RuntimeMissingError("onnxruntime and numpy are not installed") from exc
        self.config = json.loads(Path(config or "%s.json" % model).read_text(encoding="utf-8"))
        self.ids: Dict[str, List[int]] = self.config["phoneme_id_map"]
        self.rate = int(self.config["audio"]["sample_rate"])
        self.voice = (self.config.get("espeak") or {}).get("voice", "en-us")
        inf = self.config.get("inference") or {}
        self.scales = [float(inf.get("noise_scale", 0.667)), float(inf.get("length_scale", 1.0)),
                       float(inf.get("noise_w", 0.8))]
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("AITHER_PIPER_THREADS", "2"))
        self.session = onnxruntime.InferenceSession(str(model), sess_options=opts,
                                                    providers=["CPUExecutionProvider"])

    def to_ids(self, phonemes: Sequence[str]) -> List[int]:
        return phoneme_ids(phonemes, self.ids)

    def synthesize(self, text: str, speed: float = 1.0, sentence_gap: float = 0.12) -> bytes:
        import numpy as np

        text = re.sub(r"\s+", " ", respell(text)).strip()
        if not text:
            raise ValueError("nothing to say")
        chunks = []
        gap = np.zeros(int(self.rate * sentence_gap), dtype=np.float32)
        for sent in phonemize(text, self.voice):
            ids = np.array([self.to_ids(sent)], dtype=np.int64)
            scales = np.array([self.scales[0], self.scales[1] / max(0.5, min(2.0, speed)),
                               self.scales[2]], dtype=np.float32)
            audio = self.session.run(None, {
                "input": ids,
                "input_lengths": np.array([ids.shape[1]], dtype=np.int64),
                "scales": scales,
            })[0].squeeze()
            chunks += [audio.astype(np.float32), gap]
        pcm = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
        peak = float(np.max(np.abs(pcm))) or 1.0
        pcm16 = (pcm / max(peak, 0.01) * 0.95 * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(pcm16.tobytes())
        return buf.getvalue()


_LOADED: Dict[str, PiperVoice] = {}


def speak(text: str, voice_dir: Optional[Path] = None, speed: float = 1.0,
          say: Say = _quiet, fetch: bool = True) -> bytes:
    """WAV bytes of ``text`` in the Aither voice, fetching the voice first if needed.

    Args:
        text: What to say.
        voice_dir: The cache (default :func:`default_voice_dir`).
        speed: 0.5-2.0.
        say: Progress sink for a first-time download.
        fetch: False refuses to download (raises when the voice is not installed).

    Raises:
        VoiceError: The voice is not installed (and ``fetch`` is False) or failed its pin.
        RuntimeMissingError: The optional speech runtime is not installed.
        ValueError: ``text`` is empty.
    """
    if not (text or "").strip():
        raise ValueError("nothing to say")
    d = Path(voice_dir or default_voice_dir())
    # Installed = both files at their pinned sizes; the digest was checked when each
    # arrived, so a sentence does not re-hash 63 MB.
    if installed(d):
        model, config = d / MODEL.file, d / CONFIG.file
    elif fetch:
        model, config = ensure_voice(d, say)
    else:
        raise VoiceError("the Aither voice is not installed in %s" % d)
    key = str(model)
    if key not in _LOADED:
        _LOADED[key] = PiperVoice(model, config)
    return _LOADED[key].synthesize(text, speed=speed)


def wav_seconds(wav: bytes) -> float:
    """Duration of a WAV byte string, seconds."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)
