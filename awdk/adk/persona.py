"""Persona avatar integration — control a desktop VRM avatar via HTTP bridge.

This module provides tools for self-hosted ADK agents to drive a Persona avatar
running on the local machine at http://127.0.0.1:47831.

Persona is loopback-only — containerized agents cannot reach it without a host relay
(tunneling support is an owner-gated roadmap item). A HOST-run adk agent can use
these tools directly.

Tools register automatically if ADK_PERSONA=1 (default). Set ADK_PERSONA=0 to disable.

Endpoints hit:
  POST /events  — send state/animation/audio-level events
  GET  /health  — check if persona is alive
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("adk.persona")

# Disable persona tools entirely with ADK_PERSONA=0
_PERSONA_ENABLED = os.getenv("ADK_PERSONA", "1").lower() not in ("0", "false")

# Persona avatar bridge endpoint (loopback-only)
_PERSONA_ENDPOINT = "http://127.0.0.1:47831"

# Connection timeout for persona requests (fire-and-forget, so short is OK)
_PERSONA_TIMEOUT = 1.0


def _is_persona_available() -> bool:
    """Quick liveness check: is persona responding?"""
    if not _PERSONA_ENABLED:
        return False
    try:
        import httpx
        with httpx.Client(timeout=_PERSONA_TIMEOUT) as c:
            resp = c.get(f"{_PERSONA_ENDPOINT}/health")
            return resp.status_code == 200
    except Exception:
        return False


def _persona_request(event_type: str, payload: dict) -> bool:
    """Send an event to persona. Returns True if sent, False on error (silent)."""
    if not _PERSONA_ENABLED:
        return False
    try:
        import httpx
        body = {"type": event_type, **payload}
        with httpx.Client(timeout=_PERSONA_TIMEOUT) as c:
            resp = c.post(
                f"{_PERSONA_ENDPOINT}/events",
                json=body,
                timeout=_PERSONA_TIMEOUT
            )
            return resp.status_code in (200, 202, 204)
    except Exception as e:
        logger.debug(f"Persona request failed ({event_type}): {e}")
        return False


def persona_status() -> str:
    """Get the status of the Persona avatar (liveness check).

    Returns a JSON dict with {available, ok} and optional lastState from the avatar.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"available": False, "ok": False, "reason": "ADK_PERSONA=0"})
    try:
        import httpx
        with httpx.Client(timeout=_PERSONA_TIMEOUT) as c:
            resp = c.get(f"{_PERSONA_ENDPOINT}/health")
            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                return json.dumps({"available": True, "ok": True, **data})
            return json.dumps({"available": False, "ok": False, "status": resp.status_code})
    except Exception as e:
        logger.debug(f"Persona status check failed: {e}")
        return json.dumps({"available": False, "ok": False, "error": str(e)})


def persona_animate(animation: str) -> str:
    """Trigger an animation on the avatar.

    animation: One of IDLE, GREETING, TALK, HAPPY, FINGER_GUN, DANCE, or FILE:<name>.vrma
              (FILE prefix references a .vrma file in D:\\persona\\characters\\<char>\\animations\\)

    Returns JSON {sent: true/false}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    animation = str(animation or "IDLE").strip()
    sent = _persona_request("animation", {"animation": animation})
    return json.dumps({"sent": sent, "animation": animation})


def persona_set_character(name: str) -> str:
    """Switch to a different avatar character.

    name: Character directory name under D:\\persona\\characters\\
          (e.g., 'default', 'angel', 'devil')

    Returns JSON {sent: true/false, character: name}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    name = str(name or "default").strip()
    sent = _persona_request("character", {"character": name})
    return json.dumps({"sent": sent, "character": name})


def persona_speak_state(activity: str) -> str:
    """Update the avatar's speech activity state.

    activity: One of 'speaking', 'listening', or 'idle'.
              (Affects lip-sync and animation timing.)

    Returns JSON {sent: true/false, activity: activity}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    activity = str(activity or "idle").strip().lower()
    if activity not in ("speaking", "listening", "idle"):
        return json.dumps(
            {"sent": False, "activity": activity, "error": "must be speaking|listening|idle"}
        )
    sent = _persona_request("state", {"activity": activity})
    return json.dumps({"sent": sent, "activity": activity})


def persona_audio_level(level: float) -> str:
    """Update the avatar's audio input level (for visualization).

    level: A float 0.0–1.0 representing current mic input RMS.

    Returns JSON {sent: true/false, level: level}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    try:
        level = float(level or 0.0)
        level = max(0.0, min(1.0, level))  # clamp to [0, 1]
    except (TypeError, ValueError):
        return json.dumps({"sent": False, "level": level, "error": "level must be 0.0–1.0"})
    sent = _persona_request("audio-level", {"level": level})
    return json.dumps({"sent": sent, "level": level})


def persona_mute_microphone(muted: bool = True) -> str:
    """Mute or unmute the avatar's microphone indicator.

    muted: True to mute, False to unmute (visual only, does not affect actual audio input).

    Returns JSON {sent: true/false, muted: muted}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    muted = bool(muted)
    sent = _persona_request("state", {"microphoneMuted": muted})
    return json.dumps({"sent": sent, "muted": muted})


def persona_mute_output(muted: bool = True) -> str:
    """Mute or unmute the avatar's output speaker indicator.

    muted: True to mute, False to unmute (visual only, does not affect actual audio output).

    Returns JSON {sent: true/false, muted: muted}.
    """
    if not _PERSONA_ENABLED:
        return json.dumps({"sent": False, "reason": "ADK_PERSONA=0"})
    muted = bool(muted)
    sent = _persona_request("state", {"outputMuted": muted})
    return json.dumps({"sent": sent, "muted": muted})


# Tool list: exported for registration in builtin_tools.py
PERSONA_TOOLS = [
    persona_status,
    persona_animate,
    persona_set_character,
    persona_speak_state,
    persona_audio_level,
    persona_mute_microphone,
    persona_mute_output,
]
