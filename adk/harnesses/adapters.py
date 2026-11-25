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

    # Gemini CLI native stream-json (measured on 0.70.0, 2026-11-23):
    # init / message{role,content,delta} / tool_use / tool_result / error /
    # result{status,stats}. Its `result` carries `status`, not Claude's `subtype`,
    # so handing it to translate_claude marked every Gemini turn as failed.
    if etype == "init":
        return [HarnessEvent(
            kind=EventKind.SESSION_READY,
            text=f"model={obj.get('model', '?')}",
            data={"model": obj.get("model", ""),
                  "harness_session_id": obj.get("session_id", "")},
        )]
    if etype == "message":
        if obj.get("role") != "assistant":
            return [notice(f"gemini/{obj.get('role', 'message')}", payload=obj)]
        content = obj.get("content")
        if isinstance(content, str):
            return [text_delta(content)] if content else []
        return translate_claude({"type": "assistant", "message": {"content": content}})
    if etype == "tool_use":
        params = obj.get("parameters", obj.get("args"))
        return [tool_call(
            str(obj.get("tool_name") or obj.get("name") or "?"),
            str(obj.get("tool_id") or obj.get("id") or ""),
            params if isinstance(params, dict) else {"value": params},
        )]
    if etype == "tool_result":
        return [tool_result(
            str(obj.get("tool_id") or obj.get("id") or ""),
            obj.get("output", obj.get("error")),
            is_error=obj.get("status") not in (None, "success"),
        )]
    if etype == "error":
        return [error(str(obj.get("message") or obj.get("error") or "gemini error"),
                      severity=obj.get("severity"))]
    if etype == "result" and "status" in obj and "subtype" not in obj:
        ok = obj.get("status") == "success"
        stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}
        events = [
            HarnessEvent(kind=EventKind.USAGE,
                         data={"usage": stats, "cost_usd": None,
                               "duration_ms": stats.get("duration_ms"),
                               "num_turns": None}),
            HarnessEvent(kind=EventKind.TURN_COMPLETED, text="",
                         data={"is_error": not ok, "subtype": obj.get("status"),
                               "harness_session_id": obj.get("session_id", "")}),
        ]
        if not ok:
            err = obj.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            events.insert(0, error(str(msg or obj.get("status") or "turn failed")))
        return events

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


# ─────────────────────────────────────────────────────────────────────────────
# OpenCode — `opencode run --format json`
# ─────────────────────────────────────────────────────────────────────────────
#
# Shape read from the opencode 1.18 `run` command (its emitter writes
# ``{"type", "timestamp", "sessionID", ...extra}`` per line): ``tool_use`` carries
# a FINISHED ToolPart (``part.tool``, ``part.callID``, ``part.state.{status,
# input, output, error}``) -- it is emitted only once the call is ``completed``
# or ``error``, so one line is both the call and its result. ``text`` /
# ``reasoning`` carry ``part.text``; ``step_start`` / ``step_finish`` frame each
# model step (``step_finish.part.tokens`` / ``cost``); ``error`` carries
# ``error.{name, data.message}``. Without ``--format json`` the same command
# prints plain text, so a non-JSON line stays assistant text (an old binary, or
# AITHER_HARNESS_OPENCODE_FORMAT=default, behaves exactly as before).
#
# Neither this adapter nor the Codex one emits TURN_COMPLETED: the session closes
# a one-shot turn itself when the process exits (session.py); a second would
# double it.


def _opencode_error_text(err: Any) -> str:
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        msg = data.get("message") or err.get("message") or err.get("name")
        return str(msg or "opencode error")
    return str(err or "opencode error")


def translate_opencode(obj: Any) -> list[HarnessEvent]:
    """Translate one ``opencode run --format json`` line (or a plain-text line)."""
    if isinstance(obj, str):
        stripped = obj.strip()
        if not stripped.startswith("{"):
            return translate_text(obj)
        try:
            obj = json.loads(stripped)
        except ValueError:
            return translate_text(obj)
    if not isinstance(obj, dict):
        return [raw(str(obj))]

    etype = obj.get("type")
    part = obj.get("part") if isinstance(obj.get("part"), dict) else {}

    if etype == "tool_use":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        call_id = str(part.get("callID") or part.get("id") or "")
        name = str(part.get("tool") or "")
        tool_input = state.get("input")
        is_error = str(state.get("status") or "") == "error"
        output = state.get("error") if is_error else state.get("output")
        return [
            tool_call(
                tool=name,
                tool_use_id=call_id,
                tool_input=tool_input if isinstance(tool_input, dict) else {},
            ),
            tool_result(
                tool_use_id=call_id,
                output=output if output is not None else "",
                is_error=is_error,
                tool=name,
            ),
        ]

    if etype == "text":
        text = str(part.get("text") or "")
        return [text_delta(text if text.endswith("\n") else text + "\n")] if text else []

    if etype == "reasoning":
        text = str(part.get("text") or "")
        return [thinking_delta(text)] if text else []

    if etype == "step_start":
        return []

    if etype == "step_finish":
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        return [
            HarnessEvent(
                kind=EventKind.USAGE,
                data={
                    "usage": tokens,
                    "cost_usd": part.get("cost"),
                    "reason": part.get("reason"),
                    "harness_session_id": obj.get("sessionID", ""),
                },
            )
        ]

    if etype == "error":
        return [error(_opencode_error_text(obj.get("error")), payload=obj)]

    return [raw(json.dumps(obj)[:2000], event_type=etype)]


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI Codex CLI — `codex exec --json`
# ─────────────────────────────────────────────────────────────────────────────
#
# Codex >= 0.44 writes thread/turn/item events: ``thread.started``,
# ``turn.started``, ``item.started|updated|completed`` (``item.type`` one of
# agent_message, reasoning, command_execution, file_change, mcp_tool_call,
# web_search, todo_list, error), ``turn.completed`` (``usage``), ``turn.failed``
# and ``error``. Codex does not emit ``item.started`` for every call type, and an
# adapter is a pure function with no memory of earlier lines, so a call and its
# result are BOTH emitted from ``item.completed`` (paired by ``item.id``): an
# orphaned result is never possible. MCP calls are named ``mcp__<server>__<tool>``
# -- the Claude Code spelling -- so the training extractor recognises gateway
# calls whichever harness made them.

_CODEX_CALL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")


def _codex_tool(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    itype = item.get("type")
    if itype == "command_execution":
        return "shell", {"command": item.get("command") or ""}
    if itype == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        return "apply_patch", {"changes": changes}
    if itype == "mcp_tool_call":
        args = item.get("arguments")
        return (
            f"mcp__{item.get('server') or 'unknown'}__{item.get('tool') or 'unknown'}",
            args if isinstance(args, dict) else {},
        )
    return "web_search", {"query": item.get("query") or ""}


def _codex_result(item: dict[str, Any]) -> tuple[Any, bool]:
    itype = item.get("type")
    failed = str(item.get("status") or "") == "failed"
    if itype == "command_execution":
        code = item.get("exit_code")
        return item.get("aggregated_output") or "", failed or (
            isinstance(code, int) and code != 0
        )
    if itype == "mcp_tool_call":
        err = item.get("error")
        if err:
            return (err.get("message") if isinstance(err, dict) else str(err)), True
        result = item.get("result")
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            return result["content"], failed
        return result if result is not None else "", failed
    if itype == "file_change":
        return str(item.get("status") or "completed"), failed
    return "", failed


def translate_codex(obj: Any) -> list[HarnessEvent]:
    """Translate one ``codex exec --json`` object."""
    if not isinstance(obj, dict):
        return [raw(str(obj))]

    etype = obj.get("type")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    itype = item.get("type")

    if etype == "thread.started":
        return [
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text="codex thread",
                data={"model": "", "harness_session_id": obj.get("thread_id", "")},
            )
        ]

    if etype == "turn.started":
        return []

    if etype == "turn.completed":
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        return [HarnessEvent(kind=EventKind.USAGE, data={"usage": usage})]

    if etype in ("turn.failed", "error"):
        err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        return [error(str(err.get("message") or obj.get("message") or etype), payload=obj)]

    if etype in ("item.started", "item.updated"):
        return []

    if etype == "item.completed":
        if itype in _CODEX_CALL_ITEMS:
            item_id = str(item.get("id") or "")
            name, tool_input = _codex_tool(item)
            output, is_error = _codex_result(item)
            return [
                tool_call(tool=name, tool_use_id=item_id, tool_input=tool_input),
                tool_result(tool_use_id=item_id, output=output, is_error=is_error, tool=name),
            ]
        if itype == "agent_message":
            text = str(item.get("text") or "")
            return [text_delta(text if text.endswith("\n") else text + "\n")] if text else []
        if itype == "reasoning":
            text = str(item.get("text") or "")
            return [thinking_delta(text)] if text else []
        if itype == "todo_list":
            return [notice("todo list", items=item.get("items") or [])]
        if itype == "error":
            return [error(str(item.get("message") or "codex item error"), payload=item)]

    return [raw(json.dumps(obj)[:2000], event_type=etype)]


def translate_antigravity(obj: Any) -> list[HarnessEvent]:
    """Translate Google Antigravity agentapi / agy output (JSON or plain text)."""
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return []
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                return [text_delta(obj)]
        else:
            return [text_delta(obj)]
    if not isinstance(obj, dict):
        return [raw(str(obj))]
    conv = obj.get("conversation") if isinstance(obj.get("conversation"), dict) else None
    if conv and conv.get("id"):
        return [
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"antigravity conversation {conv.get('id')}",
                data={"harness_session_id": str(conv["id"]), "conversation": conv},
            )
        ]
    if obj.get("conversationId") or obj.get("conversation_id"):
        cid = str(obj.get("conversationId") or obj.get("conversation_id") or "")
        return [
            HarnessEvent(
                kind=EventKind.SESSION_READY,
                text=f"antigravity conversation {cid}",
                data={"harness_session_id": cid, "payload": obj},
            )
        ]
    if obj.get("type"):
        return translate_gemini(obj)
    if obj.get("error"):
        return [error(str(obj.get("error")), payload=obj)]
    if obj.get("text"):
        return [text_delta(str(obj["text"]))]
    return [raw(json.dumps(obj)[:2000])]


#: Adapters by harness id. Registry entries reference these by name so a spec
#: stays declarative and serializable.
ADAPTERS = {
    "claude": translate_claude,
    "gemini": translate_gemini,
    "antigravity": translate_antigravity,
    "text": translate_text,
    "opencode": translate_opencode,
    "codex": translate_codex,
}
