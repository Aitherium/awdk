"""``adk home voice``: install the Aither voice and speak with it, on this machine.

    adk home voice --local aither           download + verify the voice (idempotent)
    adk home voice --say "Hello there."     speak it into a wav (installs first if needed)
    adk home voice --say "Hi" -o hi.wav     ... to a file of your choosing
    adk home voice                          where the voice lives and whether it is ready

The installer reads the ``aither-voice`` entry of the packaged model catalogue (the same
file ``adk models`` reads): its URLs, its size and its sha256, plus the ``.onnx.json``
companion pinned the same way. Bytes are resumable (``<file>.part``) and nothing is moved
into place before its digest matched. The licence gate that guards ``adk models pull``
applies here too.

Speech runs locally with onnxruntime + espeak-ng, the optional extra
``pip install "awdk[aither-voice]"``; no service, no account, no network after the
first download.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import aither_voice_runtime as rt

VOICES = {"aither": rt.VOICE_ID}
EXTRA_HINT = 'pip install "awdk[aither-voice]"'

Say = Callable[[str], None]


class VoiceInstallError(RuntimeError):
    """The voice could not be installed (refused, unreachable, or failed its pin)."""


def voice_dir(override: str = "") -> Path:
    """Where the voice lives: ``override``, else the shared local model directory."""
    return Path(override).expanduser() if override else rt.default_voice_dir()


def install(name: str = "aither", directory: str = "", say: Say = print,
            cat: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fetch the named voice into the local model directory, verified. Idempotent.

    Args:
        name: Voice name (``aither``).
        directory: Install directory (default :func:`voice_dir`).
        say: Progress sink.
        cat: A catalogue document (tests); default the packaged one.

    Returns:
        ``{"voice", "dir", "model", "config", "sha256_verified"}``.

    Raises:
        VoiceInstallError: Unknown voice, licence refusal, or no verified bytes.
    """
    from adk.models import catalogue, download

    if name not in VOICES:
        raise VoiceInstallError(f"no voice '{name}' (have: {', '.join(sorted(VOICES))})")
    try:
        entry = catalogue.get(cat if cat is not None else catalogue.load(), VOICES[name])
    except catalogue.CatalogueError as exc:
        raise VoiceInstallError(str(exc)) from exc
    if entry.get("role") != "tts" or not entry.get("sha256") or not entry.get("companions"):
        raise VoiceInstallError(f"catalogue entry '{entry.get('id')}' is not a pinned voice")
    verdict = catalogue.licence_verdict(entry)
    if not verdict.allowed:
        raise VoiceInstallError(f"refused: {verdict.reason}")
    d = voice_dir(directory)
    say(f"Installing the {name} voice -> {d}")
    try:
        model, hashed = download.fetch_model(entry, d, say=say)
    except download.DownloadError as exc:
        raise VoiceInstallError(str(exc)) from exc
    return {"voice": name, "dir": str(d), "model": str(model),
            "config": str(d / entry["companions"][0]["file"]),
            "sha256_verified": bool(hashed), "licence": verdict.reason}


def status(directory: str = "") -> Dict[str, Any]:
    d = voice_dir(directory)
    try:
        import espeakng_loader  # noqa: F401
        import onnxruntime  # noqa: F401
        runtime = True
    except ImportError:
        runtime = False
    return {"voice": "aither", "dir": str(d), "installed": rt.installed(d),
            "runtime": runtime, "runtime_hint": "" if runtime else EXTRA_HINT}


def say_to_file(text: str, out: Path, directory: str = "", speed: float = 1.0,
                say: Say = print) -> float:
    """Synthesize ``text`` into the wav ``out``; installs the voice first if needed.

    Returns:
        The wav's duration in seconds.

    Raises:
        VoiceInstallError: The voice could not be installed.
        rt.RuntimeMissingError: The optional speech runtime is not installed.
        ValueError: Empty text.
    """
    if not (text or "").strip():
        raise ValueError("nothing to say")
    d = voice_dir(directory)
    if not rt.installed(d):
        install("aither", str(d), say=say)
    wav = rt.speak(text, voice_dir=d, speed=speed, fetch=False)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wav)
    return rt.wav_seconds(wav)
