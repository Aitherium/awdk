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


# ── brain pack → OOAgent shell (NOOA follow-on #1, issue #5328) ──────────────

_PACK_YAML = """
identity: hera
system_prompt: |
  You are Hera, head of growth. Draft first, publish on approval.
tools:
  - send_email
  - forms_create
  - not_registered_anywhere
"""


def _growth_registry():
    registry = ToolRegistry()

    def send_email(to: str) -> str:
        """Send an email."""
        return f"sent:{to}"

    def forms_create(title: str) -> str:
        """Create a form."""
        return f"form:{title}"

    def delete_everything() -> str:
        """Not in the pack whitelist."""
        return "boom"

    for fn in (send_email, forms_create, delete_everything):
        registry.register(fn)
    return registry


def test_brain_pack_generates_ooagent_class(tmp_path):
    from adk.oo_bridge import oo_class_from_brain_pack

    (tmp_path / "brain_pack.yaml").write_text(_PACK_YAML, encoding="utf-8")
    cls = oo_class_from_brain_pack(tmp_path)  # a directory resolves brain_pack.yaml
    assert issubclass(cls, OOAgent)
    assert cls.__name__ == "HeraAgent"
    assert cls.brain_pack_identity == "hera"
    assert cls.tool_whitelist == ("send_email", "forms_create", "not_registered_anywhere")
    assert "head of growth" in cls.__doc__


@pytest.mark.asyncio
async def test_brain_pack_agent_bridges_only_whitelisted_tools(tmp_path):
    from adk.oo_bridge import oo_agent_from_brain_pack

    pack = tmp_path / "brain_pack.yaml"
    pack.write_text(_PACK_YAML, encoding="utf-8")
    router = FakeRouter(["done"])
    agent = oo_agent_from_brain_pack(
        pack, model=RouterBackend(router), registry=_growth_registry(),
    )
    names = {t.name for t in agent.tools}
    assert names == {"send_email", "forms_create"}  # default-deny: no delete_everything
    assert agent.missing_tools == ["not_registered_anywhere"]  # reported, not hidden
    assert agent.name == "hera"
    assert "head of growth" in agent.instructions
    by_name = {t.name: t for t in agent.tools}
    result = await by_name["send_email"](to="a@b")
    assert result.ok and result.value == "sent:a@b"


def test_brain_pack_without_tools_key_is_default_deny():
    from adk.oo_bridge import oo_agent_from_brain_pack

    agent = oo_agent_from_brain_pack(
        {"identity": "athena", "system_prompt": "You guard."},
        model=RouterBackend(FakeRouter(["x"])),
        registry=_growth_registry(),
    )
    assert agent.tools == []


@pytest.mark.asyncio
async def test_brain_pack_subclass_adds_typed_agentic_method():
    from adk.oo_bridge import oo_class_from_brain_pack

    auditor_base = oo_class_from_brain_pack({"identity": "auditor", "system_prompt": "You audit."})

    class Auditor(auditor_base):
        async def audit(self, target: str) -> Verdict:
            """Audit the target."""
            ...

    router = FakeRouter([json.dumps({"ok": False, "reason": "drift"})])
    verdict = await Auditor(model=RouterBackend(router)).audit("fleet")
    assert isinstance(verdict, Verdict) and verdict.reason == "drift"


def test_brain_pack_rejects_non_list_tools():
    from adk.oo_bridge import oo_class_from_brain_pack

    with pytest.raises(ValueError):
        oo_class_from_brain_pack({"identity": "x", "tools": "send_email"})


def test_tools_from_registry_allow_filters_and_none_keeps_old_behaviour():
    registry = _growth_registry()
    assert {t.name for t in tools_from_registry(registry)} == {
        "send_email", "forms_create", "delete_everything",
    }
    assert [t.name for t in tools_from_registry(registry, allow=[])] == []
    only = tools_from_registry(registry, allow=["forms_create"])
    assert [t.name for t in only] == ["forms_create"]
