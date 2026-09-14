"""Request correlation — trace ID propagation across the ADK.

Generates a unique request_id at the server entry point and propagates
it through all downstream calls via contextvars. Every log entry,
Chronicle event, Strata record, and error report includes the trace ID.

Usage:
    from adk.trace import new_trace, get_trace_id, trace_context

    # At request entry (server.py middleware)
    request_id = new_trace()

    # Anywhere downstream
    current_id = get_trace_id()

    # Pass to Chronicle/Strata
    await chronicle.log_event("tool_call", request_id=get_trace_id(), ...)

    # Context manager for scoped traces
    async with trace_context("custom-id") as tid:
        # tid == "custom-id"
        ...
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import asynccontextmanager
from typing import Optional

# ContextVar for the current request/trace ID
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "adk_trace_id", default=""
)


def new_trace(request_id: str = "") -> str:
    """Start a new trace. Returns the trace ID.

    If request_id is provided, uses that; otherwise generates a UUID.
    Sets it in the current context so get_trace_id() works downstream.
    """
    tid = request_id or f"adk-{uuid.uuid4().hex[:12]}"
    _trace_id.set(tid)
    return tid


def get_trace_id() -> str:
    """Get the current trace/request ID. Returns empty string if none set."""
    return _trace_id.get()


def set_trace_id(trace_id: str) -> None:
    """Explicitly set the trace ID (e.g., from an incoming header)."""
    _trace_id.set(trace_id)


@asynccontextmanager
async def trace_context(request_id: str = ""):
    """Async context manager that sets a trace ID and restores the previous one on exit."""
    previous = _trace_id.get()
    tid = new_trace(request_id)
    try:
        yield tid
    finally:
        _trace_id.set(previous)


class TraceMiddleware:
    """ASGI middleware that generates/propagates request IDs.

    Reads X-Request-ID from incoming headers; if absent, generates one.
    Sets it in the response headers and in the ContextVar for downstream use.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Extract incoming request ID from headers
        headers = dict(scope.get("headers", []))
        incoming_id = (
            headers.get(b"x-request-id", b"").decode()
            or headers.get(b"x-trace-id", b"").decode()
        )
        trace_id = new_trace(incoming_id)

        # Inject trace ID into response headers
        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", trace_id.encode()))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_with_trace)
