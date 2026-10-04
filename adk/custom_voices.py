"""Custom voices — workspace-built voices served by the tenant's Genesis.

A voice id of the form ``custom:<name>`` names a voice built for the caller's
workspace (``/voice-builds`` on Genesis). Synthesis goes to
``POST {genesis_api_base()}/voice-builds/voices/<name>/say`` with
``Authorization: Bearer $AITHER_API_KEY``; the workspace is derived by the server
from that key and is never sent. Every other voice id keeps the stock backend path
(see ``adk.voice.VoiceClient.synthesize``).

    from adk.custom_voices import list_custom_voices
    voices = await list_custom_voices()          # [] when none are built yet
    await get_voice_client().synthesize("hi", voice="custom:ava")
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from adk._tls import tls_verify

CUSTOM_PREFIX = "custom:"
VOICES_ROUTE = "/voice-builds/voices"
_TIMEOUT_SECONDS = 60.0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CustomVoiceError(RuntimeError):
    """A custom-voice call could not be made or was refused."""


def is_custom(voice: Optional[str]) -> bool:
    """True when ``voice`` is a ``custom:<name>`` id."""
    return bool(voice) and str(voice).startswith(CUSTOM_PREFIX)


def custom_name(voice: str) -> str:
    """Strip ``custom:`` and validate the name. Raises ``CustomVoiceError``."""
    name = voice[len(CUSTOM_PREFIX):] if is_custom(voice) else voice
    name = name.strip()
    if not _NAME_RE.match(name):
        raise CustomVoiceError(f"invalid custom voice name: {name!r}")
    return name


def _base() -> str:
    from adk.control_plane import genesis_api_base

    return genesis_api_base()


def _headers() -> dict[str, str]:
    key = os.getenv("AITHER_API_KEY", "").strip()
    if not key:
        raise CustomVoiceError(
            "AITHER_API_KEY is not set — run `adk enroll` or export the key to use custom voices"
        )
    return {"Authorization": f"Bearer {key}"}


def _detail(resp: httpx.Response) -> str:
    try:
        payload: Any = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is reported as text
        return resp.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("error") or payload)[:200]
    return str(payload)[:200]


async def list_custom_voices(client: Optional[httpx.AsyncClient] = None) -> list[dict]:
    """Custom voices built for the caller's workspace (``[]`` when none).

    Each entry: ``{"id": "custom:<name>", "name", "reader", "language", "built_at", "gate"}``.
    Raises ``CustomVoiceError`` on a missing key or a non-200 answer — never silent.
    """
    headers = _headers()
    url = f"{_base()}{VOICES_ROUTE}"
    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, verify=tls_verify()) as c:
            resp = await c.get(url, headers=headers)
    else:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise CustomVoiceError(f"HTTP {resp.status_code}: {_detail(resp)}")
    voices = (resp.json() or {}).get("voices") or []
    out: list[dict] = []
    for v in voices:
        name = str(v.get("name") or v.get("id") or "")
        if name.startswith(CUSTOM_PREFIX):
            name = name[len(CUSTOM_PREFIX):]
        if not name:
            continue
        out.append({
            "id": f"{CUSTOM_PREFIX}{name}",
            "name": name,
            "reader": v.get("reader"),
            "language": v.get("language"),
            "built_at": v.get("built_at"),
            "gate": v.get("gate"),
        })
    return out


async def synthesize_custom(
    text: str,
    voice: str,
    speed: float = 1.0,
    output_path: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
):
    """Synthesize ``text`` with a ``custom:<name>`` voice. Returns a ``SynthesisResult``."""
    from adk.voice import SynthesisResult, _finish_synthesis

    try:
        name = custom_name(voice)
        headers = _headers()
    except CustomVoiceError as exc:
        return SynthesisResult(success=False, error=str(exc))
    url = f"{_base()}{VOICES_ROUTE}/{quote(name, safe='')}/say"
    body = {"text": text, "speed": speed}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, verify=tls_verify()) as c:
                resp = await c.post(url, json=body, headers=headers)
        else:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        return SynthesisResult(
            success=False,
            error=f"custom voice request failed: {type(exc).__name__}: {exc}",
        )
    if resp.status_code == 404:
        return SynthesisResult(success=False, error=f"no custom voice {name!r} in this workspace")
    if resp.status_code != 200:
        return SynthesisResult(success=False, error=f"HTTP {resp.status_code}: {_detail(resp)}")
    try:
        audio = base64.b64decode((resp.json() or {}).get("audio_base64") or "")
    except Exception as exc:  # noqa: BLE001
        return SynthesisResult(success=False, error=f"bad custom voice response: {exc}")
    return _finish_synthesis(audio, output_path)


__all__ = [
    "CUSTOM_PREFIX",
    "CustomVoiceError",
    "custom_name",
    "is_custom",
    "list_custom_voices",
    "synthesize_custom",
]
