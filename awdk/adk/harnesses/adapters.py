"""Per-harness translation into the normalized event vocabulary.

Each adapter is a pure function: native output object -> list[HarnessEvent].
Purity matters — adapters are the most likely thing to break when a harness
bumps its output schema, and a pure function is testable against a captured
transcript without spawning anything.

Every adapter ends with a RAW passthrough for anything it cannot classify.
That is deliberate: an adapter that silently drops unknown shapes turns "the
harness changed its protocol" into "the model stopped responding", which is the
single most expensive misdiagnosis in this class of system.
"""

from __future__ import annotations

import json
from typing import Any

from adk.harnesses.events import (
    EventKind,
    HarnessEvent,
    error,
    notice,
    raw,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
)


def _content_blocks(message: Any) -> list[dict[str, Any]]:
    """Normalize a message's ``content`` to a list of block dicts."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code — `--output-format stream-json --input-format stream-json`
# ─────────────────────────────────────────────────────────────────────────────

def translate_claude(obj: Any) -> list[HarnessEvent]:
    """Translate one Claude Code stream-json object."""
    if not isinstance(obj, dict):
        return [raw(str(obj))]

    etype = obj.get("type")

    if etype == "system":
        subtype = obj.get("subtype")
        if subtype == "init":
            ev = HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"model={obj.get('model', '?')}",
                data={
                    "model": obj.get("model", ""),
                    "harness_session_id": obj.get("session_id", ""),
                    "cwd": obj.get("cwd", ""),
                    "tools": obj.get("tools", []),
                    "mcp_servers": obj.get("mcp_servers", []),
                    "permission_mode": obj.get("permissionMode", ""),
                    "slash_commands": obj.get("slash_commands", []),
                },
            )
            return [ev]
        return [notice(f"system/{subtype}", **{"payload": obj})]

    if etype == "assistant":
        events: list[HarnessEvent] = []
        for block in _content_blocks(obj.get("message")):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text:
                    events.append(text_delta(text))
            elif btype == "thinking":
                text = block.get("thinking") or block.get("text") or ""
                if text:
                    events.append(thinking_delta(text))
            elif btype == "tool_use":
                events.append(
                    tool_call(
                        tool=str(block.get("name") or ""),
                        tool_use_id=str(block.get("id") or ""),
                        tool_input=(
                            block.get("input")
                            if isinstance(block.get("input"), dict)
                            else {}
                        ),
                    )
                )
            else:
                events.append(raw(json.dumps(block)[:2000], block_type=btype))
        return events or [raw(json.dumps(obj)[:2000], reason="empty assistant message")]

    if etype == "user":
        events = []
        for block in _content_blocks(obj.get("message")):
            if block.get("type") == "tool_result":
                events.append(
                    tool_result(
                        tool_use_id=str(block.get("tool_use_id") or ""),
                        output=block.get("content"),
                        is_error=bool(block.get("is_error")),
                    )
                )
        return events

    if etype == "result":
        is_error = bool(obj.get("is_error")) or obj.get("subtype") != "success"
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        events = [
            HarnessEvent(
                kind=EventKind.USAGE,
                data={
                    "usage": usage,
                    "cost_usd": obj.get("total_cost_usd"),
                    "duration_ms": obj.get("duration_ms"),
                    "num_turns": obj.get("num_turns"),
                },
            ),
            HarnessEvent(
                kind=EventKind.TURN_COMPLETED,
                text=str(obj.get("result") or ""),
                data={
                    "is_error": is_error,
                    "subtype": obj.get("subtype"),
                    "harness_session_id": obj.get("session_id", ""),
                },
            ),
        ]
        if is_error:
            events.insert(
                0,
                error(
                    str(obj.get("result") or obj.get("subtype") or "turn failed"),
                    subtype=obj.get("subtype"),
                ),
            )
        return events

    if etype == "rate_limit_event":
        return [notice("rate limit", payload=obj)]

    if etype == "stream_event":
        # Emitted only with --include-partial-messages; token-level deltas.
        event = obj.get("event") if isinstance(obj.get("event"), dict) else {}
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [text_delta(str(delta["text"]))]
        if delta.get("type") == "thinking_delta" and delta.get("thinking"):
            return [thinking_delta(str(delta["thinking"]))]
        return []

    return [raw(json.dumps(obj)[:2000], event_type=etype)]


# ─────────────────────────────────────────────────────────────────────────────
# Gemini CLI — `-o stream-json`
# ─────────────────────────────────────────────────────────────────────────────

def translate_gemini(obj: Any) -> list[HarnessEvent]:
    """Translate one Gemini CLI stream-json object.

    Gemini's schema is Claude-shaped but not identical and is explicitly
    unstable. Anything the Claude translator cannot place is preserved as RAW
    rather than guessed at.
    """
    if not isinstance(obj, dict):
        return [raw(str(obj))]

    etype = obj.get("type")
    if etype in ("assistant", "user", "system", "result"):
        return translate_claude(obj)

    # Gemini also emits bare content/text objects in some builds.
    if "text" in obj and isinstance(obj["text"], str):
        return [text_delta(obj["text"])]
    if "response" in obj and isinstance(obj["response"], str):
        return [text_delta(obj["response"])]

    return [raw(json.dumps(obj)[:2000], event_type=etype)]


# ─────────────────────────────────────────────────────────────────────────────
# Plain-text harnesses (and any process whose stdout is just output)
# ─────────────────────────────────────────────────────────────────────────────

def translate_text(line: Any) -> list[HarnessEvent]:
    """Treat every line as assistant text. Used by oneshot/exec harnesses."""
    if isinstance(line, str):
        return [text_delta(line if line.endswith("\n") else line + "\n")]
    return [raw(str(line))]


#: Adapters by harness id. Registry entries reference these by name so a spec
#: stays declarative and serializable.
ADAPTERS = {
    "claude": translate_claude,
    "gemini": translate_gemini,
    "text": translate_text,
}
