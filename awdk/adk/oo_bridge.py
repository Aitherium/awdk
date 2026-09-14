"""Bridge the OO agent model onto the production adk plane.

:class:`adk.core.oo.OOAgent` runs on the *core* primitives (``ModelBackend``,
``adk.core.tool.Tool``); production agents run on the deployed plane
(``adk.llm.LLMRouter`` for models, ``adk.tools.ToolRegistry`` for tools).
This module is the seam that lets an OO agent be written once and served
with full production capability:

    from adk import OOAgent
    from adk.oo_bridge import RouterBackend, tools_from_registry

    class Atlas(OOAgent):
        \"\"\"You are Atlas, the service-discovery agent.\"\"\"

        async def survey(self, service: str) -> Report:
            \"\"\"Survey the service and report its state.\"\"\"
            ...

    agent = Atlas(
        model=RouterBackend(router),
        tools=tools_from_registry(registry),
    )

Clearance is honored fail-closed: a registry tool that declares
``required_clearance > 0`` is NOT bridged unless the caller explicitly
grants a clearance level that covers it.
"""

from __future__ import annotations

from typing import Any

from adk.core.logging import get_logger
from adk.core.model import Message as CoreMessage
from adk.core.model import ModelResponse
from adk.core.tool import Tool

_log = get_logger("oo_bridge")


class RouterBackend:
    """Core :class:`ModelBackend` over the production :class:`LLMRouter`.

    Lets an OOAgent run on the same routed model plane as every deployed
    agent (the router fronts the platform scheduler / configured provider).

    Args:
        router: An ``adk.llm.LLMRouter`` (or any provider with an async
            ``chat(messages, ...)`` returning an ``LLMResponse``).
        model: Optional model name forwarded to the router per call.
    """

    name = "router"

    def __init__(self, router: Any, *, model: str | None = None) -> None:
        self._router = router
        self.model = model or getattr(router, "default_model", "") or "router"

    async def generate(
        self,
        messages: list[CoreMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from adk.llm.base import Message as LLMMessage

        conv = [LLMMessage(role=m.role, content=m.content) for m in messages]
        resp = await self._router.chat(
            conv,
            model=self.model if self.model != "router" else None,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
        )
        return ModelResponse(
            text=resp.content,
            model=resp.model or self.model,
            finish_reason=resp.finish_reason or "stop",
        )

    async def stream(self, messages: list[CoreMessage], **opts: Any):
        # Real incremental delivery via the router's chat_stream (StreamChunk
        # objects); a router without one falls back to a single-chunk stream.
        chat_stream = getattr(self._router, "chat_stream", None)
        if chat_stream is None:
            resp = await self.generate(messages, **opts)
            yield resp.text
            return
        from adk.llm.base import Message as LLMMessage

        conv = [LLMMessage(role=m.role, content=m.content) for m in messages]
        async for chunk in chat_stream(
            conv, model=self.model if self.model != "router" else None
        ):
            text = getattr(chunk, "content", None)
            if text:
                yield text


class RegistryTool(Tool):
    """A production :class:`ToolDef` exposed as a core :class:`Tool`."""

    def __init__(self, tool_def: Any) -> None:
        super().__init__(name=tool_def.name, description=tool_def.description)
        self._def = tool_def

    def call(self, **kwargs: Any) -> Any:
        # Tool.__call__ awaits awaitables, so async ToolDef fns work unchanged.
        return self._def.fn(**kwargs)

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._def.parameters or {"type": "object", "properties": {}},
        }


def tools_from_registry(registry: Any, *, clearance: int = 0) -> list[Tool]:
    """Bridge a :class:`ToolRegistry` into core Tools an OOAgent can use.

    Fail-closed on clearance: a tool whose ``required_clearance`` exceeds
    the granted ``clearance`` is skipped (and logged), never silently
    exposed. The default grants level 0 — public tools only.
    """
    bridged: list[Tool] = []
    skipped: list[str] = []
    for tool_def in registry.list_tools():
        if getattr(tool_def, "required_clearance", 0) > clearance:
            skipped.append(tool_def.name)
            continue
        bridged.append(RegistryTool(tool_def))
    if skipped:
        _log.info(
            "oo_bridge.tools.clearance_skipped",
            extra={"count": len(skipped), "names": skipped[:20]},
        )
    return bridged


__all__ = [
    "RegistryTool",
    "RouterBackend",
    "tools_from_registry",
]
