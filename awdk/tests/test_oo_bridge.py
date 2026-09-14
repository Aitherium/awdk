"""Tests for adk.oo_bridge — OOAgent on the production plane."""

import json

import pytest
from adk.core.oo import OOAgent
from adk.llm.base import LLMResponse
from adk.oo_bridge import RegistryTool, RouterBackend, tools_from_registry
from adk.tools import ToolRegistry
from pydantic import BaseModel


class FakeRouter:
    """Production-shaped router: async chat() returning an LLMResponse."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, **kw):
        self.calls.append({"messages": list(messages), "model": model})
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return LLMResponse(content=self.responses[idx], model="routed-model")


class Verdict(BaseModel):
    ok: bool
    reason: str


@pytest.mark.asyncio
async def test_ooagent_runs_on_router_backend():
    router = FakeRouter([json.dumps({"ok": True, "reason": "fine"})])

    class Auditor(OOAgent):
        """You audit things."""

        async def audit(self, target: str) -> Verdict:
            """Audit the target."""
            ...

    agent = Auditor(model=RouterBackend(router))
    verdict = await agent.audit("the fleet")
    assert isinstance(verdict, Verdict)
    assert verdict.ok is True
    # The production router really served the call.
    assert len(router.calls) == 1
    assert any("the fleet" in str(m.content) for m in router.calls[0]["messages"])


@pytest.mark.asyncio
async def test_registry_tools_bridge_and_execute():
    registry = ToolRegistry()

    def lookup(item: str) -> str:
        """Look up an item."""
        return f"found:{item}"

    async def fetch(url: str) -> str:
        """Fetch a URL."""
        return f"fetched:{url}"

    registry.register(lookup)
    registry.register(fetch)

    tools = tools_from_registry(registry)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"lookup", "fetch"}
    assert isinstance(by_name["lookup"], RegistryTool)

    # Sync and async registry fns both execute through the core Tool contract.
    r1 = await by_name["lookup"](item="x")
    assert r1.ok and r1.value == "found:x"
    r2 = await by_name["fetch"](url="http://e")
    assert r2.ok and r2.value == "fetched:http://e"


def test_clearance_gated_tools_are_not_bridged_by_default():
    registry = ToolRegistry()

    def public_tool() -> str:
        """Public."""
        return "p"

    def admin_tool() -> str:
        """Admin-only."""
        return "a"

    registry.register(public_tool)
    registry.register(admin_tool, required_clearance=3)

    names = {t.name for t in tools_from_registry(registry)}
    assert "public_tool" in names
    assert "admin_tool" not in names  # fail-closed

    # Explicit grant covers it.
    names = {t.name for t in tools_from_registry(registry, clearance=3)}
    assert "admin_tool" in names


@pytest.mark.asyncio
async def test_router_backend_stream_falls_back_without_chat_stream():
    router = FakeRouter(["chunk-text"])  # FakeRouter has no chat_stream
    backend = RouterBackend(router)
    chunks = [c async for c in backend.stream([])]
    assert chunks == ["chunk-text"]


@pytest.mark.asyncio
async def test_router_backend_streams_incrementally():
    from adk.llm.base import StreamChunk

    class StreamingRouter(FakeRouter):
        async def chat_stream(self, messages, model=None, **kw):
            for piece in ("hel", "lo ", "world"):
                yield StreamChunk(content=piece, done=False, model="routed-model")
            yield StreamChunk(content="", done=True, model="routed-model")

    backend = RouterBackend(StreamingRouter([]))
    chunks = [c async for c in backend.stream([])]
    # Incremental: multiple chunks, empty done-marker not emitted.
    assert chunks == ["hel", "lo ", "world"]
