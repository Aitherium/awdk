"""Thin compatibility shim over ``awvoice.aither_voice_runtime``.

The on-device Aither voice runtime (pinned Piper/VITS download, espeak-ng
phonemizer, lexicon respelling, and ONNX synthesis) is canonically owned by the
``awvoice`` brick (``awvoice.aither_voice_runtime``). This module re-exports its
surface so ``adk.home.voice``, ``adk.home.cli``, and existing callers keep
working without maintaining a second byte-for-byte copy inside ``awdk``.
"""

from __future__ import annotations

try:
    from awvoice import aither_voice_runtime as _impl
    from awvoice.aither_voice_runtime import (
        CHUNK,
        CONFIG,
        LEXICON,
        MAX_STALLS,
        MODEL,
        PINS,
        RELEASE,
        TIMEOUT,
        UA,
        VOICE_ID,
        Pin,
        PiperVoice,
        RuntimeMissingError,
        Say,
        VoiceError,
        default_voice_dir,
        ensure_voice,
        fetch_pinned,
        installed,
        phoneme_ids,
        phonemize,
        respell,
        sha256_file,
        speak,
        wav_seconds,
    )
except ImportError as exc:
    raise ImportError(
        "adk.home.aither_voice_runtime requires the `awvoice` package. "
        "Install it with: pip install awvoice"
    ) from exc

__all__ = [
    "CHUNK",
    "CONFIG",
    "LEXICON",
    "MAX_STALLS",
    "MODEL",
    "PINS",
    "RELEASE",
    "TIMEOUT",
    "UA",
    "VOICE_ID",
    "Pin",
    "PiperVoice",
    "RuntimeMissingError",
    "Say",
    "VoiceError",
    "default_voice_dir",
    "ensure_voice",
    "fetch_pinned",
    "installed",
    "phoneme_ids",
    "phonemize",
    "respell",
    "sha256_file",
    "speak",
    "wav_seconds",
]

# Forward any private helpers (e.g. `_espeak`, `_clause_ipa`, `_fetch_once`) for
# callers/tests that reach into internal runtime primitives.
for _name in dir(_impl):
    if not _name.startswith("__") and _name not in globals():
        globals()[_name] = getattr(_impl, _name)
del _name
