"""Standalone HTTP voice server — wraps get_voice_client() for AitherShell.

POST /voice/synthesize  { text, voice, speed, format, return_base64 }
  → { success, audio_base64, format, duration_seconds }
POST /voice/transcribe  { audio_base64, language }
  → { success, text, language, duration_seconds }
GET /voice/health       → { status, backend, available_backends }

Bind 127.0.0.1 only (loopback — no auth needed; secure by network isolation).
AITHER_VOICE_HTTP_PORT (default 8085).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from adk.voice import get_voice_client

logger = logging.getLogger("adk.voice_http")

app = FastAPI(title="AitherADK Voice Server", docs_url=None, openapi_url=None)


@app.post("/voice/synthesize")
async def synthesize(body: dict[str, Any]) -> dict[str, Any]:
    """Text-to-speech. Returns audio_base64."""
    text = body.get("text", "").strip()
    if not text:
        return {
            "success": False,
            "audio_base64": "",
            "format": "wav",
            "duration_seconds": 0,
            "error": "Text must not be empty",
        }

    voice = body.get("voice", "nova")
    speed = body.get("speed", 1.0)
    return_base64 = body.get("return_base64", True)

    client = get_voice_client()
    result = await client.synthesize(text, voice=voice)

    if not result.success or not result.audio_data:
        return {
            "success": False,
            "audio_base64": "",
            "format": "wav",
            "duration_seconds": 0,
            "error": result.error or "synthesis failed",
        }

    audio_b64 = base64.b64encode(result.audio_data).decode("utf-8") if return_base64 else ""
    # Estimate duration: ~15 chars/sec default.
    duration = max(0.5, len(text) / 15.0)

    return {
        "success": True,
        "audio_base64": audio_b64,
        "format": "wav",
        "duration_seconds": duration,
    }


@app.post("/voice/transcribe")
async def transcribe(body: dict[str, Any]) -> dict[str, Any]:
    """Speech-to-text. Expects audio_base64."""
    audio_b64 = body.get("audio_base64", "")
    if not audio_b64:
        return {
            "success": False,
            "text": "",
            "language": "",
            "duration_seconds": 0,
            "error": "No audio_base64 provided",
        }

    language = body.get("language")

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        return {
            "success": False,
            "text": "",
            "language": "",
            "duration_seconds": 0,
            "error": f"Failed to decode audio_base64: {exc}",
        }

    client = get_voice_client()
    result = await client.transcribe(audio_bytes, language=language)

    if not result.success:
        return {
            "success": False,
            "text": "",
            "language": result.language or language or "",
            "duration_seconds": result.duration_seconds,
            "error": result.error or "transcription failed",
        }

    return {
        "success": True,
        "text": result.text,
        "language": result.language or language or "",
        "duration_seconds": result.duration_seconds,
    }


@app.get("/voice/health")
async def health() -> dict[str, Any]:
    """Health check — list available backends."""
    client = get_voice_client()
    backend_status = await client.status()
    return {
        "status": "ok",
        "backend": client.backend_name,
        "available_backends": ["service", "openai", "sarvam", "local", "mock"],
    }


def run(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Run the server. Importable by the CLI.

    Args:
        host: bind address (default 127.0.0.1 — localhost only, no auth needed)
        port: port number (default from AITHER_VOICE_HTTP_PORT or 8085)
    """
    if port is None:
        port = int(os.getenv("AITHER_VOICE_HTTP_PORT", "8085"))

    logger.info(f"Starting AitherADK voice server on {host}:{port}")
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
