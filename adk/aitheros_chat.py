"""One chat turn with an AitherOS platform service (Atlas).

`adk chat <agent>` already speaks two protocols: "adk" (an adk agent server,
POST /chat/stream, SSE) and "openai" (a raw inference server, POST
/v1/chat/completions). A platform service serves neither. Its chat route is

    POST {invoke_url}/chat
    {"message": "<text>", "auto_execute": false}

and it answers one JSON object, not a stream. Atlas, 2026-11-10:

    {"session_id": "session_f2daafcd", "response": "Online and standing by.",
     "reasoning_available": true, "actions_proposed": null,
     "auto_executing": false, "message_count": 2, "timestamp": "..."}

An agent registered with `"chat_protocol": "aitheros"` in ~/.aither/agents.json is
chatted with through this module.

`auto_execute` is always sent as false. The caller is a one-shot CLI turn or a
queue runner with nobody watching; a service that delegates work on its own from
that turn has been handed an approval nobody gave.

TLS goes through `adk._tls.tls_verify()` like every other adk client: platform
services present certificates from the AitherNet internal CA, which that helper
trusts when the bundle is installed. Verification is never turned off here.
"""

from __future__ import annotations

from typing import Any

import httpx

from adk._tls import tls_verify

#: The reply field that carries the text. Read from the service's own reply
#: (its OpenAPI declares no response schema), not guessed.
REPLY_FIELD = "response"


class AitherOSChatError(RuntimeError):
    """The service did not produce a usable reply."""


def build_request(message: str) -> dict[str, Any]:
    """The request body. `auto_execute` is false, always."""
    return {"message": message, "auto_execute": False}


def parse_reply(payload: Any) -> str:
    """The reply text, or AitherOSChatError.

    An empty or missing reply is an error, not an empty success: the caller
    reports the turn as done, and "done" with nothing said is a false report.
    """
    if not isinstance(payload, dict):
        raise AitherOSChatError(
            f"expected a JSON object from /chat, got {type(payload).__name__}")
    text = payload.get(REPLY_FIELD)
    if not isinstance(text, str) or not text.strip():
        raise AitherOSChatError(
            f"/chat reply carries no '{REPLY_FIELD}' text (keys: {sorted(payload)})")
    return text


async def chat(invoke_url: str, message: str, *, read_timeout: float = 600.0) -> str:
    """Send one message, return the reply text. Raises AitherOSChatError."""
    url = invoke_url.rstrip("/") + "/chat"
    timeout = httpx.Timeout(connect=30.0, read=read_timeout, write=30.0, pool=30.0)
    try:
        async with httpx.AsyncClient(verify=tls_verify(), timeout=timeout) as client:
            resp = await client.post(url, json=build_request(message))
    except httpx.HTTPError as exc:
        raise AitherOSChatError(f"could not reach {url}: {exc!r}") from exc
    if resp.status_code != 200:
        raise AitherOSChatError(f"{resp.status_code} from {url}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AitherOSChatError(f"non-JSON reply from {url}: {resp.text[:300]}") from exc
    return parse_reply(payload)
