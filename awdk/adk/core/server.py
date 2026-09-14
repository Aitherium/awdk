"""HTTP serve mode — expose an :class:`Agent` as a REST + SSE service.

Usage::

    from adk.core import Agent, auto_backend
    from adk.core.server import serve

    agent = Agent(name="x", model=auto_backend())
    serve(agent, host="0.0.0.0", port=8000)

Endpoints:
  - ``GET /health`` — liveness check
  - ``GET /info`` — agent name, tools, capabilities
  - ``POST /chat`` — body ``{"prompt": "..."}`` → ``{"output", "steps", ...}``
  - ``POST /chat/stream`` — same body, SSE stream of incremental chunks

This is a thin FastAPI wrapper; install with ``pip install 'awdk[full]'``
(or just add ``fastapi`` + ``uvicorn`` yourself).
"""

from __future__ import annotations

import os
from typing import Any

from adk.core.agent import Agent
from adk.core.logging import get_logger

_log = get_logger("server")


def build_app(agent: Agent) -> Any:
    """Build a FastAPI app exposing the given agent. Returns the app object."""
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "HTTP serve mode requires fastapi. "
            "Install with: pip install fastapi uvicorn (or 'awdk[full]')"
        ) from e

    app = FastAPI(title=f"AitherADK agent: {agent.name}")

    # CORS for the browser "is a local node running?" probe and for letting the
    # Aitherium web app talk to the node the user is running on their own machine.
    #
    # allow_origins is an explicit ALLOWLIST (never "*") — this server listens on
    # loopback, so without a list any site the user visits could reach their agent.
    # The aitherium origins must be present: the whole point is a probe from
    # https://api.aitherium.com to http://127.0.0.1:8000, which is cross-origin.
    # POST is included because /chat is the endpoint the web app calls once it has
    # detected the node; GET/OPTIONS alone made the feature dead on arrival.
    # Override with AITHER_ADK_CORS_ORIGINS (comma-separated) for a custom portal.
    _extra_origins = [
        o.strip()
        for o in os.environ.get("AITHER_ADK_CORS_ORIGINS", "").split(",")
        if o.strip()
    ]
    _allowed_origins = [
        "https://aitherium.com",
        "https://www.aitherium.com",
        "https://api.aitherium.com",
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8085",
        "http://127.0.0.1:8085",
    ] + _extra_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    # Private Network Access (PNA) support for Chrome.
    # When an HTTPS page (e.g., https://api.aitherium.com) probes a private/loopback
    # HTTP endpoint (e.g., http://127.0.0.1:8000/health), Chrome requires a preflight
    # OPTIONS response with 'Access-Control-Allow-Private-Network: true' to allow the
    # health check. This middleware adds that header ONLY for /health and /info routes,
    # and ONLY when the Origin is in the CORS allowlist.
    #
    # Scope: Narrowly applied only to health/info routes + allowlisted origins.
    # This prevents any cross-site from requesting PNA access via this server.
    @app.middleware("http")
    async def add_private_network_access(request, call_next):
        from starlette.datastructures import MutableHeaders

        origin = request.headers.get("origin", "")
        # Only add PNA header for health/info routes when Origin is in allowlist
        if request.url.path in ("/health", "/info") and origin in _allowed_origins:
            response = await call_next(request)
            # Add PNA header to allow Chrome to proceed with cross-origin private requests
            response.headers["Access-Control-Allow-Private-Network"] = "true"
            return response
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": agent.name}

    @app.get("/info")
    async def info() -> dict[str, Any]:
        return {
            "agent": agent.name,
            "instructions": agent.instructions,
            "model": getattr(agent.model, "model", None) or type(agent.model).__name__,
            "tools": [
                {"name": t.name, "description": t.description} for t in agent.tools
            ],
            "capabilities": [c.name for c in agent.capabilities],
        }

    @app.post("/chat")
    async def chat(prompt: str = Body(..., embed=True)) -> dict[str, Any]:
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        result = await agent.run(prompt)
        return {
            "output": result.output,
            "steps": result.steps,
            "finish_reason": result.finish_reason,
            "tool_calls": [
                {
                    "name": tc["name"],
                    "args": tc["args"],
                    "ok": tc["result"].ok,
                    "value": tc["result"].value if tc["result"].ok else None,
                    "error": tc["result"].error,
                }
                for tc in result.tool_calls
            ],
        }

    @app.post("/chat/stream")
    async def chat_stream(prompt: str = Body(..., embed=True)) -> Any:
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")

        async def gen():
            # The default loop is non-streaming; we run it once and emit a
            # single completion event. Backends that support streaming will
            # land in a future slice (see SPEC.md).
            yield _sse({"type": "start", "agent": agent.name})
            try:
                result = await agent.run(prompt)
            except Exception as e:  # noqa: BLE001
                yield _sse({"type": "error", "error": f"{type(e).__name__}: {e}"})
                return
            yield _sse(
                {
                    "type": "done",
                    "output": result.output,
                    "steps": result.steps,
                    "finish_reason": result.finish_reason,
                }
            )

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _sse(payload: dict[str, Any]) -> bytes:
    import json

    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def serve(
    agent: Agent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the agent's HTTP service. Blocks until interrupted."""
    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "HTTP serve mode requires uvicorn. "
            "Install with: pip install uvicorn (or 'awdk[full]')"
        ) from e
    app = build_app(agent)
    _log.info("server.start", extra={"host": host, "port": port, "agent": agent.name})
    uvicorn.run(app, host=host, port=port, log_level="info")
