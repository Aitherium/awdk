"""Shared ACP v2 wire primitives: JSON-RPC 2.0 framing, update builders, enums.

Both ACP directions in awdk speak the same JSON-RPC 2.0 wire over
newline-delimited stdio (see :mod:`adk.stdio_compat` for the Windows-safe
streams): :mod:`adk.acp` drives external ACP agents (client role) and
:mod:`adk.acp_server` exposes an adk agent to editors (server role). This
module holds the wire they share so the two directions cannot drift apart —
the v1 client and server each carried their own envelope helpers, and the v2
shapes differ from both.

Wire format follows the ACP v2 spec (https://agentclientprotocol.com/protocol/v2/).
Parsing is deliberately LENIENT: v1 field-name aliases (snake_case,
``clientCapabilities``/``clientInfo``, flat ``session/update`` payloads) are
accepted alongside the v2 camelCase nested shapes, because the reference
``agent-client-protocol`` SDK and real editors were mid-migration when this
was written. Emission is v2-native.
"""

from __future__ import annotations

import json
from typing import Any, Optional

__all__ = [
    "PROTOCOL_VERSION",
    # JSON-RPC 2.0 error codes
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "AUTH_REQUIRED",
    # Enumerations
    "AUTH_METHOD_TYPES",
    "SESSION_STATES",
    "STOP_REASONS",
    "TOOL_KINDS",
    "TOOL_STATUSES",
    "PERMISSION_OPTION_KINDS",
    "PLAN_PRIORITIES",
    "PLAN_STATUSES",
    "SESSION_UPDATE_KINDS",
    # RPC helpers
    "RpcError",
    "make_request",
    "make_response",
    "make_error",
    "make_notification",
    "encode_frame",
    "decode_frame",
    "write_frame",
    # Content blocks
    "text_block",
    "text_message",
    "extract_prompt_text",
    # session/update builders (v2-native emission)
    "session_update",
    "state_update",
    "user_message_update",
    "agent_message_chunk",
    "agent_thought_chunk",
    "tool_call_update",
    "tool_call_content_chunk",
    "plan_update",
    "available_commands_update",
    "usage_update",
    "terminal_output_chunk",
    # Parsing helpers
    "pick",
    "normalize_update",
]

#: ACP protocol major version this implementation speaks (v2).
PROTOCOL_VERSION = 2

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Agent foreground states (v2 prompt lifecycle).
SESSION_STATES = frozenset({"running", "idle", "requires_action"})

#: `auth_required` — the agent needs `authenticate` before session work. Not a
#: JSON-RPC standard code; ACP defines it in the -32000 application range.
AUTH_REQUIRED = -32000

#: The two authentication method types the ACP registry accepts. A method with
#: any other `type` is ignored by the registry's CI verifier, so an agent that
#: advertises only e.g. an env-var method is rejected while looking configured.
AUTH_METHOD_TYPES = frozenset({"agent", "terminal"})

#: Terminal stop reasons. Custom reasons must begin with "_".
STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)

#: Tool kinds. Custom kinds must begin with "_".
TOOL_KINDS = frozenset(
    {"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"}
)

#: Tool call lifecycle statuses. Custom statuses must begin with "_".
TOOL_STATUSES = frozenset({"pending", "in_progress", "completed", "failed", "cancelled"})

#: Permission option kinds offered on session/request_permission.
PERMISSION_OPTION_KINDS = frozenset(
    {"allow_once", "allow_always", "reject_once", "reject_always"}
)

#: Plan entry priority / status enums (plan_update).
PLAN_PRIORITIES = frozenset({"high", "medium", "low"})
PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})

#: Every session/update discriminator this implementation knows (validation + tests).
SESSION_UPDATE_KINDS = frozenset(
    {
        "user_message",
        "user_message_chunk",
        "agent_message",
        "agent_message_chunk",
        "agent_thought",
        "agent_thought_chunk",
        "tool_call_update",
        "tool_call_content_chunk",
        "plan_update",
        "usage_update",
        "state_update",
        "terminal_update",
        "terminal_output_chunk",
        "available_commands_update",
        "session_info_update",
    }
)


def pick(d: Any, *names: str, default: Any = None) -> Any:
    """First present non-None key among *names* (camelCase wire, snake legacy)."""
    if isinstance(d, dict):
        for n in names:
            if d.get(n) is not None:
                return d[n]
    return default


class RpcError(Exception):
    """A JSON-RPC 2.0 error that should reach the peer verbatim."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ─── JSON-RPC 2.0 envelope builders ─────────────────────────────────────────


def make_request(msg_id: Any, method: str, params: Optional[dict[str, Any]] = None) -> dict:
    req: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        req["params"] = params
    return req


def make_response(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def make_notification(method: str, params: dict[str, Any]) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


# ─── Framing ────────────────────────────────────────────────────────────────


def encode_frame(obj: Any) -> bytes:
    """Serialize *obj* as a newline-delimited JSON-RPC frame.

    ACP stdio framing: one JSON object per line, no embedded newlines.
    """
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def decode_frame(line: bytes) -> Any:
    """Parse one newline-delimited frame. Raises ValueError on bad JSON."""
    return json.loads(line.decode("utf-8", errors="replace"))


async def write_frame(writer: Any, obj: Any, *, lock: Optional[Any] = None) -> None:
    """Write one frame to *writer*, honoring an optional write lock.

    *writer* only needs the minimal shape servers/clients already use:
    ``write(bytes)`` and optionally ``drain()`` (``ThreadStdoutWriter`` has no
    drain and flushes on write instead — see :mod:`adk.stdio_compat`).
    """
    data = encode_frame(obj)

    async def _do() -> None:
        writer.write(data)
        drain = getattr(writer, "drain", None)
        if drain is not None:
            await drain()

    if lock is not None:
        async with lock:
            await _do()
    else:
        await _do()


# ─── Content blocks ─────────────────────────────────────────────────────────


def text_block(text: str) -> dict:
    """A ``text`` content block (the same shape MCP uses)."""
    return {"type": "text", "text": text}


def text_message(text: str) -> list[dict]:
    """A single-user-message prompt array holding *text*."""
    return [text_block(text)]


def extract_prompt_text(blocks: Any) -> str:
    """Flatten ACP prompt content blocks into plain text (text + resource text)."""
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for b in blocks or []:
        if isinstance(b, dict):
            btype = b.get("type")
            if btype == "text":
                parts.append(b.get("text", ""))
            elif btype == "resource":
                res = b.get("resource") or {}
                if isinstance(res, dict) and res.get("text"):
                    parts.append(str(res["text"]))
        elif isinstance(b, str):
            parts.append(b)
    return "".join(parts)


# ─── session/update builders (v2-native) ────────────────────────────────────
#
# Each builder returns the RAW ``update`` payload (the object nested under
# ``params.update`` on the wire). ``session_update()`` is the ONLY function
# that wraps a raw update into a full ``session/update`` notification — call
# sites must not wrap twice (a wrapped-then-wrapped update is a silent
# corruption the conformance suite catches).


def session_update(session_id: str, update: dict[str, Any]) -> dict:
    """A ``session/update`` notification in ACP v2's nested shape."""
    return make_notification("session/update", {"sessionId": session_id, "update": update})


def state_update(state: str, *, stop_reason: Optional[str] = None) -> dict:
    """A ``state_update``: running / requires_action / idle (+ stopReason)."""
    update: dict[str, Any] = {"sessionUpdate": "state_update", "state": state}
    if stop_reason is not None:
        update["stopReason"] = stop_reason
    return update


def user_message_update(message_id: str, blocks: list[dict]) -> dict:
    """Confirm where a prompt was inserted (the agent-owned messageId)."""
    return {"sessionUpdate": "user_message", "messageId": message_id, "content": blocks}


def agent_message_chunk(text: str, *, message_id: Optional[str] = None) -> dict:
    update: dict[str, Any] = {
        "sessionUpdate": "agent_message_chunk",
        "content": text_block(text),
    }
    if message_id:
        update["messageId"] = message_id
    return update


def agent_thought_chunk(text: str) -> dict:
    return {"sessionUpdate": "agent_thought_chunk", "content": text_block(text)}


def tool_call_update(
    tool_call_id: str,
    *,
    title: Optional[str] = None,
    kind: str = "other",
    status: str = "pending",
    raw_input: Any = None,
    raw_output: Any = None,
    content: Optional[list[dict]] = None,
) -> dict:
    """A ``tool_call_update`` upsert (omitted fields stay unchanged on the client)."""
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
        "kind": kind,
        "status": status,
    }
    if title is not None:
        update["title"] = title
    if raw_input is not None:
        update["rawInput"] = raw_input
    if raw_output is not None:
        update["rawOutput"] = raw_output
    if content is not None:
        update["content"] = content
    return update


def tool_call_content_chunk(tool_call_id: str, block: dict) -> dict:
    """Append one content item to a tool call's streamed output."""
    return {
        "sessionUpdate": "tool_call_content_chunk",
        "toolCallId": tool_call_id,
        "content": {"type": "content", "content": block},
    }


def plan_update(plan_id: str, entries: list[dict]) -> dict:
    """A ``plan_update``; the client replaces its stored plan completely."""
    return {"sessionUpdate": "plan_update",
            "plan": {"type": "items", "planId": plan_id, "entries": entries}}


def available_commands_update(commands: list[dict]) -> dict:
    """Advertise the slash commands available in a session (after session/new)."""
    return {"sessionUpdate": "available_commands_update", "availableCommands": commands}


def usage_update(input_tokens: int, output_tokens: int) -> dict:
    """Report token usage.

    v2 carries ``used``/``size``; the v1-era reference SDK and editors read
    ``input_tokens``/``output_tokens``. Emit both so neither is starved.
    """
    return {
        "sessionUpdate": "usage_update",
        "used": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input": input_tokens,
            "output": output_tokens,
        },
        "size": input_tokens + output_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def terminal_output_chunk(terminal_id: str, data_b64: str) -> dict:
    """Append base64-encoded display-only terminal bytes."""
    return {
        "sessionUpdate": "terminal_output_chunk",
        "terminalId": terminal_id,
        "data": data_b64,
    }


# ─── Parsing helpers ────────────────────────────────────────────────────────


def normalize_update(payload: Any) -> dict:
    """Extract the update object from a ``session/update`` params payload.

    v2 nests the payload under ``update``; the v1 reference SDK and legacy
    mocks send the fields flat. Both must parse.
    """
    if isinstance(payload, dict):
        nested = payload.get("update")
        if isinstance(nested, dict):
            return nested
        return payload
    return {}
