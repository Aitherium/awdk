"""The Aither voice in adk: catalogue entry, verified installer, phoneme framing, CLI.

Everything here runs offline against a loopback server. The one real synthesis at the
end runs only where the optional runtime AND an installed voice are both present.
"""
from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import socketserver
import threading
import wave

import pytest

from adk.home import aither_voice_runtime as rt
from adk.home import voice
from adk.models import catalogue, serve

MODEL_BLOB = bytes(i % 239 for i in range(4096))
CONFIG_BLOB = b'{"audio": {"sample_rate": 22050}, "phoneme_id_map": {}}'


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Files(http.server.BaseHTTPRequestHandler):
    """Serves ``files`` by path; honours Range like the real mirror."""

    files: dict = {}

    def log_message(self, *args):  # noqa: D102
        pass

    def _blob(self):
        return self.files.get(self.path.lstrip("/"))

    def do_HEAD(self):  # noqa: N802
        blob = self._blob()
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()

    def do_GET(self):  # noqa: N802
        blob = self._blob()
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(blob) - start))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(blob[start:])


@pytest.fixture()
def server():
    srvs = []

    def _serve(files):
        handler = type("_H", (_Files,), {"files": dict(files)})
        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        srvs.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/"

    yield _serve
    for s in srvs:
        s.shutdown()
        s.server_close()


def _fake_catalogue(base: str, model_sha: str = "", model_size: int = 0,
                    config_sha: str = "") -> dict:
    entry = {
        "id": "aither-voice", "role": "tts", "file": "aither-voice.onnx", "size_mb": 1,
        "parts": 0, "join": False, "urls": [base + "aither-voice.onnx"],
        "size_bytes": model_size or len(MODEL_BLOB), "sha256": model_sha or _sha(MODEL_BLOB),
        "licence_id": "aither-voice",
        "licence": {"record": "aither-voice", "name": "MIT", "redistribution_ok": True},
        "companions": [{"file": "aither-voice.onnx.json", "size_bytes": len(CONFIG_BLOB),
                        "sha256": config_sha or _sha(CONFIG_BLOB),
                        "urls": [base + "aither-voice.onnx.json"]}],
    }
    return {"models": {"aither-voice": entry}}


_FILES = {"aither-voice.onnx": MODEL_BLOB, "aither-voice.onnx.json": CONFIG_BLOB}


# ---------------------------------------------------------------- catalogue


def test_shipped_catalogue_carries_the_voice_as_a_pinned_tts_model():
    m = catalogue.get(catalogue.load(), "aither-voice")
    assert m["role"] == "tts" and not m["on_ladder"]
    assert catalogue.licence_verdict(m).allowed, catalogue.licence_verdict(m).reason
    # The runtime's pins (what awvoice uses) are the catalogue's, byte for byte.
    assert (m["file"], m["size_bytes"], m["sha256"]) == (rt.MODEL.file, rt.MODEL.size,
                                                          rt.MODEL.sha256)
    assert m["urls"] == list(rt.MODEL.urls)
    (comp,) = m["companions"]
    assert (comp["file"], comp["size_bytes"], comp["sha256"], comp["urls"]) == (
        rt.CONFIG.file, rt.CONFIG.size, rt.CONFIG.sha256, list(rt.CONFIG.urls))


def test_a_voice_is_never_recommended_as_a_chat_model():
    cat = catalogue.load()
    assert catalogue.recommend(cat, 10 ** 6) != "aither-voice"


def test_the_voice_cache_is_the_shared_models_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AITHER_VOICE_DIR", raising=False)
    monkeypatch.setenv("AITHER_BONSAI_ROOT", str(tmp_path))
    assert rt.default_voice_dir() == serve.models_dir() == tmp_path / "models"
    monkeypatch.setenv("AITHER_VOICE_DIR", str(tmp_path / "v"))
    assert rt.default_voice_dir() == tmp_path / "v"


# ---------------------------------------------------------------- installer


def test_install_verifies_both_files_and_is_idempotent(server, tmp_path):
    base = server(_FILES)
    out = voice.install("aither", str(tmp_path), say=lambda m: None,
                        cat=_fake_catalogue(base))
    assert out["sha256_verified"]
    assert (tmp_path / "aither-voice.onnx").read_bytes() == MODEL_BLOB
    assert (tmp_path / "aither-voice.onnx.json").read_bytes() == CONFIG_BLOB
    # Second run against a DEAD source: the verified files are kept, nothing refetched.
    dead = _fake_catalogue("http://127.0.0.1:9/")
    voice.install("aither", str(tmp_path), say=lambda m: None, cat=dead)
    assert (tmp_path / "aither-voice.onnx").read_bytes() == MODEL_BLOB


def test_install_refuses_bytes_that_fail_the_pinned_sha256(server, tmp_path):
    base = server(_FILES)
    with pytest.raises(voice.VoiceInstallError, match="sha256"):
        voice.install("aither", str(tmp_path), say=lambda m: None,
                      cat=_fake_catalogue(base, model_sha="0" * 64))
    assert not (tmp_path / "aither-voice.onnx").exists()
    assert not (tmp_path / "aither-voice.onnx.part").exists()


def test_install_refuses_a_file_of_the_wrong_size(server, tmp_path):
    base = server(_FILES)
    with pytest.raises(voice.VoiceInstallError, match="states"):
        voice.install("aither", str(tmp_path), say=lambda m: None,
                      cat=_fake_catalogue(base, model_size=len(MODEL_BLOB) + 1))
    assert not (tmp_path / "aither-voice.onnx").exists()


def test_install_refuses_a_companion_that_fails_its_pin(server, tmp_path):
    base = server(_FILES)
    with pytest.raises(voice.VoiceInstallError):
        voice.install("aither", str(tmp_path), say=lambda m: None,
                      cat=_fake_catalogue(base, config_sha="1" * 64))
    assert not (tmp_path / "aither-voice.onnx.json").exists()


def test_install_refuses_an_unknown_voice(tmp_path):
    with pytest.raises(voice.VoiceInstallError, match="no voice"):
        voice.install("nobody", str(tmp_path), say=lambda m: None)


def test_install_honours_the_licence_gate(server, tmp_path):
    cat = _fake_catalogue(server(_FILES))
    cat["models"]["aither-voice"]["licence"] = None
    with pytest.raises(voice.VoiceInstallError, match="refused"):
        voice.install("aither", str(tmp_path), say=lambda m: None, cat=cat)


# ---------------------------------------------------------------- runtime downloader


def test_runtime_fetch_pinned_verifies_and_refuses(server, tmp_path):
    base = server(_FILES)
    good = rt.Pin("aither-voice.onnx", len(MODEL_BLOB), _sha(MODEL_BLOB),
                  [base + "missing.onnx", base + "aither-voice.onnx"])
    assert rt.fetch_pinned(good, tmp_path).read_bytes() == MODEL_BLOB
    assert rt.fetch_pinned(good, tmp_path).read_bytes() == MODEL_BLOB  # idempotent
    bad = rt.Pin("other.onnx", len(MODEL_BLOB), "0" * 64, [base + "aither-voice.onnx"])
    with pytest.raises(rt.VoiceError, match="sha256"):
        rt.fetch_pinned(bad, tmp_path)
    assert not (tmp_path / "other.onnx").exists()
    short = rt.Pin("short.onnx", len(MODEL_BLOB) + 10, _sha(MODEL_BLOB),
                   [base + "aither-voice.onnx"])
    with pytest.raises(rt.VoiceError):
        rt.fetch_pinned(short, tmp_path)
    assert not (tmp_path / "short.onnx").exists()


def test_a_tampered_file_on_disk_is_replaced(server, tmp_path):
    base = server(_FILES)
    pin = rt.Pin("aither-voice.onnx", len(MODEL_BLOB), _sha(MODEL_BLOB),
                 [base + "aither-voice.onnx"])
    (tmp_path / "aither-voice.onnx").write_bytes(b"\0" * len(MODEL_BLOB))
    assert rt.fetch_pinned(pin, tmp_path).read_bytes() == MODEL_BLOB


# ---------------------------------------------------------------- phonemes


ID_MAP = {"^": [1], "$": [2], "_": [0], "h": [20], "ə": [59], "l": [24], "ˈ": [120],
          "o": [27], "ʊ": [100], " ": [3], ".": [10]}


def test_phoneme_ids_are_bos_then_phoneme_pad_pairs_then_eos():
    ipa = list("həlˈoʊ.") + ["§"]  # § is not in the map: dropped, as piper does
    assert rt.phoneme_ids(ipa, ID_MAP) == [1, 20, 0, 59, 0, 24, 0, 120, 0, 27, 0, 100, 0,
                                           10, 0, 2]
    assert rt.phoneme_ids([], ID_MAP) == [1, 2]


@pytest.mark.parametrize("text,said", [
    ("Aither", "Eigh-ther"),
    ("Hello from AitherOS.", "Hello from Eigh-ther O S."),
    ("AitherVeil", "Eigh-ther Veil"),
    ("Aitherium", "Aitherium"),
    ("Aitherial", "Aitherial"),
    ("run adk now", "run A D K now"),
])
def test_names_are_respelled_for_speech(text, said):
    assert rt.respell(text) == said


# ---------------------------------------------------------------- CLI


def _tiny_wav(seconds: float = 0.5, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))
    return buf.getvalue()


def test_cli_voice_say_writes_a_wav(monkeypatch, tmp_path, capsys):
    from adk.home import cli

    monkeypatch.setattr(rt, "installed", lambda d=None: True)
    monkeypatch.setattr(rt, "speak", lambda text, **kw: _tiny_wav())
    out = tmp_path / "hi.wav"
    rc = cli.main(["voice", "--say", "Welcome to Aitherium.", "-o", str(out),
                   "--dir", str(tmp_path)])
    assert rc == 0 and out.is_file()
    assert "0.50 s" in capsys.readouterr().out


def test_cli_voice_names_the_extra_when_the_runtime_is_missing(monkeypatch, tmp_path):
    from adk.home import cli

    def _missing(text, **kw):
        raise rt.RuntimeMissingError("onnxruntime and numpy are not installed")

    monkeypatch.setattr(rt, "installed", lambda d=None: True)
    monkeypatch.setattr(rt, "speak", _missing)
    assert cli.main(["voice", "--say", "hi", "-o", str(tmp_path / "x.wav")]) == cli.EXIT_SETUP


def test_adk_models_use_refuses_a_voice(capsys):
    from adk.models import cli as mcli

    assert mcli.main(["use", "aither-voice", "--no-start"]) == 1
    assert "adk home voice" in capsys.readouterr().err


# ---------------------------------------------------------------- real synthesis


_REAL = all(importlib.util.find_spec(m) for m in ("onnxruntime", "espeakng_loader", "numpy"))


@pytest.mark.skipif(not (_REAL and rt.installed()),
                    reason="needs onnxruntime + espeakng-loader and an installed voice")
def test_real_synthesis_when_the_runtime_and_voice_are_present(tmp_path):
    d = rt.default_voice_dir()
    out = tmp_path / "welcome.wav"
    secs = voice.say_to_file("Welcome to Aitherium.", out, str(d), say=lambda m: None)
    assert 0.5 < secs < 6.0
    assert out.stat().st_size > 10000
