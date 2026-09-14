"""Voice I/O tools for agents: transcribe audio (hear) and synthesize speech (say).

These tools enable agents to process audio in their ReAct loop, useful for
voice channels, IVR integration, and multi-modal interactions. Can use local
offline backends (faster-whisper + Piper) or cloud APIs (OpenAI, Deepgram,
ElevenLabs).

Tools:
  - hear(path, language): Transcribe audio file to text (STT)
  - say(text, voice_id): Synthesize text to audio bytes (TTS)
  - analyze_voice_emotion(path): Detect emotion from audio (optional)

For testing without network/models, set AITHER_VOICE_MODE=mock.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from adk.voice import VoiceClient

logger = logging.getLogger("adk.builtin_tools_voice")

# Module state: lazy voice client init
_voice_client: Optional[object] = None
_voice_mode: str = os.getenv("AITHER_VOICE_MODE", "auto").lower()  # "auto" | "mock" | "local" | "cloud"


class MockVoiceClient:
    """Stub voice client for offline testing (no network, no ML models)."""

    async def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        """Mock transcription: returns filename stem as transcript."""
        stem = Path(audio_path).stem.replace("test_", "").replace("_", " ")
        logger.debug("MockVoiceClient.transcribe(%s) -> '%s'", audio_path, stem)
        return stem or "mock audio"

    async def synthesize(self, text: str, voice: str = "nova", output_path: Optional[str] = None) -> bytes:
        """Mock synthesis: returns UTF-8 encoded text as fake audio bytes."""
        fake_audio = f"[MOCK AUDIO: {text[:50]}...]".encode("utf-8")
        if output_path:
            Path(output_path).write_bytes(fake_audio)
        logger.debug("MockVoiceClient.synthesize('%s') -> %d bytes", text[:30], len(fake_audio))
        return fake_audio

    async def analyze_emotion(self, audio_path: str) -> dict:
        """Mock emotion analysis: returns neutral."""
        logger.debug("MockVoiceClient.analyze_emotion(%s) -> neutral", audio_path)
        return {"emotion": "neutral", "confidence": 0.5, "intensity": 0.5}


def _get_voice_client() -> object:
    """Lazy-initialize and return the voice client (real or mock)."""
    global _voice_client
    if _voice_client is not None:
        return _voice_client

    if _voice_mode == "mock":
        _voice_client = MockVoiceClient()
        logger.info("Voice client initialized in MOCK mode (offline)")
    else:
        # Try real VoiceClient from adk/voice.py (requires awdk[voice-*] installed)
        try:
            from adk.voice import VoiceClient
            _voice_client = VoiceClient()
            logger.info("Voice client initialized in REAL mode (service on %s)",
                       getattr(_voice_client, '_service_url', 'http://localhost:8083'))
        except ImportError:
            logger.warning("VoiceClient not available; falling back to MOCK mode")
            _voice_client = MockVoiceClient()

    return _voice_client


def _audio_bytes(result) -> bytes:
    """Normalize a synthesize() return (SynthesisResult OR raw bytes) to bytes.

    The real VoiceClient returns a SynthesisResult dataclass; MockVoiceClient
    returns raw bytes. This bridges both so the tools don't care which is active.
    """
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    data = getattr(result, "audio_data", None)
    return bytes(data) if data else b""


def _emotion_to_dict(result) -> dict:
    """Normalize an analyze_emotion() return (EmotionResult OR dict) to a dict."""
    if isinstance(result, dict):
        return result
    out = {
        "emotion": getattr(result, "emotion", "") or "unknown",
        "intensity": getattr(result, "intensity", 0.0),
        # Real EmotionResult has no 'confidence'; intensity is the closest proxy.
        "confidence": getattr(result, "intensity", 0.0),
        "sensation": getattr(result, "sensation", ""),
    }
    err = getattr(result, "error", "")
    if err:
        out["error"] = err
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tool Functions (registered via builtin_tools.TOOL_CATEGORIES['voice'])
# ─────────────────────────────────────────────────────────────────────────────

async def hear(path: str, language: str = "") -> str:
    """Transcribe audio file to text using local or cloud STT.

    Args:
        path: Path to audio file (WAV, MP3, OGG, FLAC, etc.)
        language: ISO 639-1 code (e.g. 'en', 'es', 'fr'). Omit for auto-detect.

    Returns:
        Transcribed text from the audio.

    Example:
        text = await hear('/path/to/message.wav')
        # agent can now process text in ReAct loop
    """
    client = _get_voice_client()
    try:
        text = await client.transcribe(path, language=language or None)
        logger.debug("hear('%s') -> %s", Path(path).name, text[:100])
        return text
    except Exception as e:
        logger.error("hear() failed: %s", e)
        return json.dumps({"error": f"transcription failed: {str(e)}"})


async def say(text: str, voice_id: str = "nova") -> bytes:
    """Synthesize text to speech audio using local or cloud TTS.

    Programmatic helper that returns raw audio BYTES — convenient for code that
    pipes audio to a channel. NOTE: this is NOT the LLM-facing tool; tools must
    return a string (see ``say_to_file``), or the bytes get JSON-stringified into
    the model's context. Registered tool = ``say_to_file``.

    Args:
        text: Text to speak.
        voice_id: Voice name ('nova' default, or 'shimmer', 'echo', etc. on OpenAI).

    Returns:
        Audio bytes (WAV or MP3 format, depending on backend).

    Example:
        audio_bytes = await say("Hello, welcome to our support team!")
        # agent can send audio_bytes over phone/chat channel
    """
    client = _get_voice_client()
    try:
        result = await client.synthesize(text, voice=voice_id)
        audio = _audio_bytes(result)
        logger.debug("say('%s...') -> %d bytes", text[:30], len(audio))
        return audio
    except Exception as e:
        logger.error("say() failed: %s", e)
        return json.dumps({"error": f"synthesis failed: {str(e)}"}).encode("utf-8")


async def say_to_file(text: str, output_path: str = "", voice_id: str = "nova") -> str:
    """Synthesize speech to an audio FILE and return its path (LLM-facing tool).

    Use this instead of `say` inside an agent's ReAct loop: it returns a short
    JSON string (path + byte count) rather than raw audio bytes, so the model's
    context never fills with binary.

    Args:
        text: Text to speak.
        output_path: Where to write the audio. Omit for an auto temp .wav file.
        voice_id: Voice name ('nova' default, or 'shimmer', 'echo', etc.).

    Returns:
        JSON string: {"audio_path": "...", "bytes": N, "voice": "..."} or {"error": ...}.
    """
    if not text.strip():
        return json.dumps({"error": "text must not be empty"})
    client = _get_voice_client()
    try:
        if not output_path:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(prefix="adk_say_", suffix=".wav", delete=False)
            tmp.close()
            output_path = tmp.name
        result = await client.synthesize(text, voice=voice_id, output_path=output_path)
        audio = _audio_bytes(result)
        path = getattr(result, "audio_path", "") or output_path
        # If the backend returned bytes but didn't write a file, persist them here.
        if audio and not (path and Path(path).exists()):
            Path(output_path).write_bytes(audio)
            path = output_path
        logger.debug("say_to_file('%s...') -> %s (%d bytes)", text[:30], path, len(audio))
        return json.dumps({"audio_path": path, "bytes": len(audio), "voice": voice_id})
    except Exception as e:
        logger.error("say_to_file() failed: %s", e)
        return json.dumps({"error": f"synthesis failed: {str(e)}"})


async def analyze_voice_emotion(path: str) -> dict:
    """Detect emotion from audio (for empathetic response tuning).

    Args:
        path: Path to audio file.

    Returns:
        Dict with emotion, confidence, intensity, etc.
    """
    client = _get_voice_client()
    try:
        result = await client.analyze_emotion(path)
        emo = _emotion_to_dict(result)
        logger.debug("analyze_voice_emotion('%s') -> %s", Path(path).name, emo.get("emotion"))
        return emo
    except Exception as e:
        logger.error("analyze_voice_emotion() failed: %s", e)
        return {"emotion": "unknown", "confidence": 0.0, "error": str(e)}
