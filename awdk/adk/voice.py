"""Voice — pluggable STT / TTS / emotion for agents.

A single, self-contained voice layer with swappable backends, so DaoOS agents
get speech-to-text and text-to-speech WITHOUT requiring the monorepo AitherVoice
service. The backend is chosen by ``AITHER_VOICE_BACKEND``:

    service   AitherVoice HTTP service on :8083 (default — preserves prior behaviour)
    openai    OpenAI Whisper (STT) + TTS                         [needs: openai]
    sarvam    Sarvam.ai STT/TTS (Indian languages)               [no extra dep]
    local     faster-whisper (STT) + Piper (TTS), fully offline  [needs: voice-local]
    mock      deterministic stub for tests / air-gapped demos
    auto      service if AITHER_VOICE_URL set, else openai if OPENAI_API_KEY,
              else local if faster-whisper installed, else service

Public API (stable — do not break):
    from adk.voice import (
        VoiceClient, get_voice_client, get_voice_backend,
        hear, say, feel,
        TranscriptionResult, SynthesisResult, EmotionResult,
    )

Usage:
    client = get_voice_client()                      # uses AITHER_VOICE_BACKEND
    result = await client.transcribe("recording.wav")
    print(result.text)

    out = await client.synthesize("Hello world", voice="nova")
    open("out.wav", "wb").write(out.audio_data)

    text  = await hear("recording.wav")              # convenience
    audio = await say("Hello world")
    mood  = await feel("recording.wav")
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("adk.voice")

_DEFAULT_SERVICE_URL = "http://localhost:8083"
_DEFAULT_VOICE = "nova"
_TIMEOUT_SECONDS = 30.0

_AVAILABLE_VOICES = [
    {"id": "alloy", "name": "Alloy", "description": "Neutral and balanced"},
    {"id": "echo", "name": "Echo", "description": "Warm and resonant"},
    {"id": "fable", "name": "Fable", "description": "Expressive storyteller"},
    {"id": "nova", "name": "Nova", "description": "Clear and friendly"},
    {"id": "onyx", "name": "Onyx", "description": "Deep and authoritative"},
    {"id": "shimmer", "name": "Shimmer", "description": "Bright and energetic"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Result types (stable shapes — consumed across the kit)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TranscriptionResult:
    """Result from speech-to-text transcription."""
    success: bool
    text: str = ""
    language: str = ""
    duration_seconds: float = 0.0
    error: str = ""


@dataclass
class SynthesisResult:
    """Result from text-to-speech synthesis."""
    success: bool
    audio_path: str = ""
    audio_data: bytes = b""
    error: str = ""


@dataclass
class EmotionResult:
    """Result from vocal emotion analysis."""
    success: bool
    emotion: str = ""
    intensity: float = 0.0
    sensation: str = ""
    error: str = ""


def _read_audio(audio: str | bytes) -> tuple[bytes, str]:
    """Normalize an audio arg to (bytes, filename). Raises FileNotFoundError."""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio), "audio.wav"
    path = Path(audio)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    return path.read_bytes(), path.name


# ─────────────────────────────────────────────────────────────────────────────
# Backend interface
# ─────────────────────────────────────────────────────────────────────────────

class VoiceBackend(ABC):
    """Abstract STT/TTS backend (mirrors the LLMProvider pattern)."""

    name: str = "base"

    @abstractmethod
    async def transcribe(
        self, audio: str | bytes, language: str | None = None
    ) -> TranscriptionResult:
        """Convert audio (file path or raw bytes) to text."""
        ...

    @abstractmethod
    async def synthesize(
        self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None
    ) -> SynthesisResult:
        """Convert text to speech audio."""
        ...

    async def analyze_emotion(self, audio: str | bytes) -> EmotionResult:
        """Optional vocal-emotion analysis. Default: unsupported."""
        return EmotionResult(success=False, error=f"emotion analysis not supported by '{self.name}' backend")

    async def status(self) -> dict:
        """Backend health/info."""
        return {"status": "ok", "backend": self.name}

    def available_voices(self) -> list[dict]:
        return list(_AVAILABLE_VOICES)


# ─────────────────────────────────────────────────────────────────────────────
# Backend: AitherVoice HTTP service (:8083) — backward-compatible default
# ─────────────────────────────────────────────────────────────────────────────

class ServiceVoiceBackend(VoiceBackend):
    """HTTP client to the AitherVoice service (STT/TTS/emotion)."""

    name = "service"

    def __init__(self, service_url: str = ""):
        self._url = (
            service_url
            or os.getenv("AITHER_VOICE_URL", "")
            or _DEFAULT_SERVICE_URL
        ).rstrip("/")

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        try:
            data_bytes, filename = _read_audio(audio)
        except FileNotFoundError as exc:
            return TranscriptionResult(success=False, error=str(exc))
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                files = {"file": (filename, data_bytes)}
                form = {"language": language} if language else {}
                resp = await client.post(f"{self._url}/api/v1/transcribe", files=files, data=form)
            if resp.status_code == 200:
                body = resp.json()
                return TranscriptionResult(
                    success=True,
                    text=body.get("text", ""),
                    language=body.get("language", ""),
                    duration_seconds=body.get("duration_seconds", 0.0),
                )
            return TranscriptionResult(success=False, error=f"Service returned {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return TranscriptionResult(success=False, error=f"Voice service unavailable at {self._url}")
        except Exception as exc:  # noqa: BLE001 - reported as result.error, never raised
            return TranscriptionResult(success=False, error=str(exc))

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        if not text.strip():
            return SynthesisResult(success=False, error="Text must not be empty")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(f"{self._url}/api/v1/synthesize", json={"text": text, "voice": voice})
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "audio" in content_type or "octet-stream" in content_type:
                    audio_data = resp.content
                else:
                    body = resp.json()
                    audio_b64 = body.get("audio", "")
                    audio_data = base64.b64decode(audio_b64) if audio_b64 else b""
                return _finish_synthesis(audio_data, output_path)
            return SynthesisResult(success=False, error=f"Service returned {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return SynthesisResult(success=False, error=f"Voice service unavailable at {self._url}")
        except Exception as exc:  # noqa: BLE001
            return SynthesisResult(success=False, error=str(exc))

    async def analyze_emotion(self, audio: str | bytes) -> EmotionResult:
        try:
            data_bytes, filename = _read_audio(audio)
        except FileNotFoundError as exc:
            return EmotionResult(success=False, error=str(exc))
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(f"{self._url}/api/v1/emotion", files={"file": (filename, data_bytes)})
            if resp.status_code == 200:
                body = resp.json()
                return EmotionResult(
                    success=True,
                    emotion=body.get("emotion", "neutral"),
                    intensity=body.get("intensity", 0.0),
                    sensation=body.get("sensation", ""),
                )
            return EmotionResult(success=False, error=f"Service returned {resp.status_code}: {resp.text[:200]}")
        except httpx.ConnectError:
            return EmotionResult(success=False, error=f"Voice service unavailable at {self._url}")
        except Exception as exc:  # noqa: BLE001
            return EmotionResult(success=False, error=str(exc))

    async def status(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/health")
            if resp.status_code == 200:
                return resp.json()
            return {"status": "error", "code": resp.status_code, "backend": self.name}
        except httpx.ConnectError:
            return {"status": "unavailable", "url": self._url, "backend": self.name}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc), "backend": self.name}


# ─────────────────────────────────────────────────────────────────────────────
# Backend: OpenAI (Whisper STT + TTS)
# ─────────────────────────────────────────────────────────────────────────────

class OpenAIVoiceBackend(VoiceBackend):
    """OpenAI Whisper transcription + TTS. Requires the ``openai`` package."""

    name = "openai"

    def __init__(self, api_key: str = "", stt_model: str = "", tts_model: str = ""):
        try:
            from openai import AsyncOpenAI  # noqa: F401 - presence check
        except ImportError as exc:  # surfaced to the factory, which falls back
            raise ImportError("openai package not installed (pip install 'awdk[voice-cloud]')") from exc
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._stt_model = stt_model or os.getenv("AITHER_VOICE_OPENAI_STT", "whisper-1")
        self._tts_model = tts_model or os.getenv("AITHER_VOICE_OPENAI_TTS", "tts-1")

    def _client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=self._api_key) if self._api_key else AsyncOpenAI()

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        try:
            data_bytes, filename = _read_audio(audio)
        except FileNotFoundError as exc:
            return TranscriptionResult(success=False, error=str(exc))
        try:
            kwargs = {"model": self._stt_model, "file": (filename, data_bytes)}
            if language:
                kwargs["language"] = language
            resp = await self._client().audio.transcriptions.create(**kwargs)
            return TranscriptionResult(success=True, text=getattr(resp, "text", "") or "", language=language or "")
        except Exception as exc:  # noqa: BLE001
            return TranscriptionResult(success=False, error=str(exc))

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        if not text.strip():
            return SynthesisResult(success=False, error="Text must not be empty")
        try:
            client = self._client()
            async with client.audio.speech.with_streaming_response.create(
                model=self._tts_model, voice=voice, input=text
            ) as response:
                audio_data = await response.read()
            return _finish_synthesis(audio_data, output_path)
        except Exception as exc:  # noqa: BLE001
            return SynthesisResult(success=False, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Backend: Sarvam.ai (STT/TTS — strong on Indian languages)
# ─────────────────────────────────────────────────────────────────────────────

class SarvamVoiceBackend(VoiceBackend):
    """Sarvam.ai STT/TTS. Key from AITHER_SARVAM_API_KEY (or DAO_SARVAM_API_KEY)."""

    name = "sarvam"
    _BASE = "https://api.sarvam.ai"

    def __init__(self, api_key: str = "", language: str = ""):
        self._key = api_key or os.getenv("AITHER_SARVAM_API_KEY", "") or os.getenv("DAO_SARVAM_API_KEY", "")
        self._lang = language or os.getenv("AITHER_SARVAM_LANGUAGE", "") or os.getenv("DAO_SARVAM_LANGUAGE", "en-IN")

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        if not self._key:
            return TranscriptionResult(success=False, error="Sarvam not configured (AITHER_SARVAM_API_KEY)")
        try:
            data_bytes, filename = _read_audio(audio)
        except FileNotFoundError as exc:
            return TranscriptionResult(success=False, error=str(exc))
        try:
            fmt = (filename.rsplit(".", 1)[-1] if "." in filename else "wav").lower()
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{self._BASE}/speech-to-text",
                    headers={"api-subscription-key": self._key},
                    files={"file": (filename, data_bytes, f"audio/{fmt}")},
                    data={"language_code": language or self._lang},
                )
            if r.status_code == 200:
                return TranscriptionResult(success=True, text=r.json().get("transcript", ""), language=language or self._lang)
            return TranscriptionResult(success=False, error=f"Sarvam returned {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            return TranscriptionResult(success=False, error=str(exc))

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        if not text.strip():
            return SynthesisResult(success=False, error="Text must not be empty")
        if not self._key:
            return SynthesisResult(success=False, error="Sarvam not configured (AITHER_SARVAM_API_KEY)")
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{self._BASE}/text-to-speech",
                    headers={"api-subscription-key": self._key},
                    json={"inputs": [text[:1000]], "target_language_code": self._lang},
                )
            if r.status_code == 200:
                audios = r.json().get("audios") or []
                audio_data = base64.b64decode(audios[0]) if audios else b""
                return _finish_synthesis(audio_data, output_path)
            return SynthesisResult(success=False, error=f"Sarvam returned {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            return SynthesisResult(success=False, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Backend: Local offline (faster-whisper STT + Piper TTS)
# ─────────────────────────────────────────────────────────────────────────────

class LocalVoiceBackend(VoiceBackend):
    """Fully-offline STT via faster-whisper; TTS via Piper. Requires voice-local."""

    name = "local"

    def __init__(self, model_size: str = "", device: str = "", piper_model: str = ""):
        try:
            import faster_whisper  # noqa: F401 - presence check
        except ImportError as exc:
            raise ImportError("faster-whisper not installed (pip install 'awdk[voice-local]')") from exc
        self._model_size = model_size or os.getenv("AITHER_WHISPER_MODEL", "base")
        self._device = device or os.getenv("AITHER_WHISPER_DEVICE", "cpu")
        self._compute = os.getenv("AITHER_WHISPER_COMPUTE", "int8")
        self._piper_model = piper_model or os.getenv("AITHER_PIPER_MODEL", "")
        self._whisper = None

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(self._model_size, device=self._device, compute_type=self._compute)
        return self._whisper

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        try:
            data_bytes, filename = _read_audio(audio)
        except FileNotFoundError as exc:
            return TranscriptionResult(success=False, error=str(exc))

        def _run() -> TranscriptionResult:
            import io
            model = self._get_whisper()
            source = io.BytesIO(data_bytes)
            segments, info = model.transcribe(source, language=language)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return TranscriptionResult(
                success=True,
                text=text,
                language=getattr(info, "language", language or ""),
                duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
            )

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            return TranscriptionResult(success=False, error=str(exc))

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        if not text.strip():
            return SynthesisResult(success=False, error="Text must not be empty")
        if not self._piper_model:
            return SynthesisResult(success=False, error="Piper TTS not configured (set AITHER_PIPER_MODEL to a .onnx voice)")

        def _run() -> bytes:
            import io
            import wave
            from piper import PiperVoice
            voice_model = PiperVoice.load(self._piper_model)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                voice_model.synthesize(text, wav)
            return buf.getvalue()

        try:
            audio_data = await asyncio.to_thread(_run)
            return _finish_synthesis(audio_data, output_path)
        except ImportError as exc:
            return SynthesisResult(success=False, error=f"piper-tts not installed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return SynthesisResult(success=False, error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Backend: Mock (deterministic, no network/models) — for tests & demos
# ─────────────────────────────────────────────────────────────────────────────

class MockVoiceBackend(VoiceBackend):
    """Deterministic stub. Transcript = filename stem; audio = UTF-8 marker bytes."""

    name = "mock"

    async def transcribe(self, audio: str | bytes, language: str | None = None) -> TranscriptionResult:
        if isinstance(audio, (bytes, bytearray)):
            return TranscriptionResult(success=True, text="mock audio", language=language or "en")
        stem = Path(audio).stem.replace("test_", "").replace("_", " ")
        return TranscriptionResult(success=True, text=stem or "mock audio", language=language or "en")

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        audio_data = f"[MOCK AUDIO: {text[:50]}...]".encode("utf-8")
        return _finish_synthesis(audio_data, output_path)

    async def analyze_emotion(self, audio: str | bytes) -> EmotionResult:
        return EmotionResult(success=True, emotion="neutral", intensity=0.5, sensation="calm")


def _finish_synthesis(audio_data: bytes, output_path: str | None) -> SynthesisResult:
    """Shared tail for synthesize(): optionally write to disk, return result."""
    saved_path = ""
    if output_path and audio_data:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(audio_data)
        saved_path = str(out)
    return SynthesisResult(success=bool(audio_data), audio_path=saved_path, audio_data=audio_data,
                           error="" if audio_data else "empty audio")


# ─────────────────────────────────────────────────────────────────────────────
# Backend factory
# ─────────────────────────────────────────────────────────────────────────────

_BACKENDS = {
    "service": ServiceVoiceBackend,
    "openai": OpenAIVoiceBackend,
    "sarvam": SarvamVoiceBackend,
    "local": LocalVoiceBackend,
    "mock": MockVoiceBackend,
}

_backend_singleton: VoiceBackend | None = None
_backend_singleton_kind: str = ""


def _resolve_auto() -> str:
    """Pick a backend when AITHER_VOICE_BACKEND=auto."""
    if os.getenv("AITHER_VOICE_URL"):
        return "service"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    try:
        import faster_whisper  # noqa: F401
        return "local"
    except ImportError:
        return "service"


def get_voice_backend(kind: str | None = None) -> VoiceBackend:
    """Return a voice backend (cached singleton for the env-selected default).

    Args:
        kind: explicit backend name; defaults to ``AITHER_VOICE_BACKEND`` ("service").
              ``"auto"`` probes the environment. Unknown / unavailable backends fall
              back to the service backend (never raises).
    """
    global _backend_singleton, _backend_singleton_kind
    desired = (kind or os.getenv("AITHER_VOICE_BACKEND", "service")).lower().strip()
    if desired == "auto":
        desired = _resolve_auto()

    if kind is None and _backend_singleton is not None and _backend_singleton_kind == desired:
        return _backend_singleton

    backend = _construct_backend(desired)

    if kind is None:
        _backend_singleton = backend
        _backend_singleton_kind = desired
    return backend


def _construct_backend(kind: str) -> VoiceBackend:
    cls = _BACKENDS.get(kind)
    if cls is None:
        logger.warning("Unknown voice backend %r; falling back to 'service'", kind)
        return ServiceVoiceBackend()
    try:
        return cls()
    except ImportError as exc:
        logger.warning("Voice backend %r unavailable (%s); falling back to 'service'", kind, exc)
        return ServiceVoiceBackend()


def reset_voice_backend() -> None:
    """Drop the cached backend singleton (test isolation when env changes)."""
    global _backend_singleton, _backend_singleton_kind
    _backend_singleton = None
    _backend_singleton_kind = ""


# ─────────────────────────────────────────────────────────────────────────────
# VoiceClient — stable facade over a backend
# ─────────────────────────────────────────────────────────────────────────────

class VoiceClient:
    """Async client for STT/TTS/emotion. Delegates to a pluggable backend.

    Args:
        service_url: if set, pins the ``service`` backend to this URL (back-compat).
        backend: explicit backend name; otherwise ``AITHER_VOICE_BACKEND``.
    """

    def __init__(self, service_url: str = "", backend: str = ""):
        if service_url:
            self._backend: VoiceBackend = ServiceVoiceBackend(service_url)
        else:
            self._backend = get_voice_backend(backend or None)
        # Back-compat attribute some callers introspect for logging.
        self._url = getattr(self._backend, "_url", "")
        self._service_url = self._url

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def transcribe(self, audio_path: str | bytes, language: str | None = None) -> TranscriptionResult:
        return await self._backend.transcribe(audio_path, language)

    async def synthesize(self, text: str, voice: str = _DEFAULT_VOICE, output_path: str | None = None) -> SynthesisResult:
        return await self._backend.synthesize(text, voice=voice, output_path=output_path)

    async def analyze_emotion(self, audio_path: str | bytes) -> EmotionResult:
        return await self._backend.analyze_emotion(audio_path)

    async def status(self) -> dict:
        return await self._backend.status()

    def available_voices(self) -> list[dict]:
        return self._backend.available_voices()


# ─────────────────────────────────────────────────────────────────────────────
# Convenience functions
# ─────────────────────────────────────────────────────────────────────────────

async def hear(path: str | bytes) -> str:
    """Transcribe audio to text. Returns empty string on failure."""
    result = await get_voice_client().transcribe(path)
    return result.text if result.success else ""


async def say(text: str, voice: str = _DEFAULT_VOICE) -> bytes:
    """Synthesize text to audio bytes. Returns empty bytes on failure."""
    result = await get_voice_client().synthesize(text, voice=voice)
    return result.audio_data if result.success else b""


async def feel(path: str | bytes) -> str:
    """Detect emotion from audio. Returns emotion string or empty on failure."""
    result = await get_voice_client().analyze_emotion(path)
    return result.emotion if result.success else ""


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: VoiceClient | None = None


def get_voice_client(service_url: str | None = None) -> VoiceClient:
    """Get or create the module-level VoiceClient singleton."""
    global _instance
    if _instance is None or service_url:
        _instance = VoiceClient(service_url=service_url or "")
    return _instance
