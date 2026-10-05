"""register_gateway_tools_on: the platform reach lands on EVERY agent, not just one.

Measured 2026-10-05 (daemon log): the startup attach registered search_tools/call_tool
on the default identity; every CLI turn runs on the CLI's default agent, which is a
DIFFERENTLY NAMED agent built on demand — it started from built-ins only, and the model
told the owner it had no platform access, which was true of the menu it was shown.

These tests pin the per-agent contract:
* meta-tools + eager core land on the given agent (replace-by-name, idempotent);
* the closures read the LIVE client and catalogue through the getters, so a gateway
  flap that swaps the client re-points every registered agent;
* intent tags are carried, so the per-turn filter can include the meta-tools.
"""

from adk.server import (
    _mcp_intent_categories,
    _prioritise_mcp_specs,
    register_gateway_tools_on,
)
from adk.tools import ToolRegistry


class FakeAgent:
    def __init__(self):
        self.name = "fake"
        self._tools = ToolRegistry()

    def tools_by_name(self):
        return {t.name: t for t in self._tools.list_tools()}


class FakeMCP:
    """Matches the GatewayMCPClient contract call_tool() depends on."""

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments or {})))
        return {"success": True, "text": f"ok:{name}"}


def _wire(agent, client, catalogue, max_tools=1):
    return register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: catalogue,
        client_getter=lambda: client,
        max_tools=max_tools,
    )


def test_meta_tools_and_eager_core_land_on_the_given_agent():
    agent, client = FakeAgent(), FakeMCP()
    catalogue = [
        {"name": "git_status", "description": "state of the working tree"},
        {"name": "zeta_tool", "description": "unprioritised"},
    ]
    registered = _wire(agent, client, catalogue, max_tools=1)

    names = agent.tools_by_name()
    assert {"search_tools", "call_tool"} <= set(names)
    # git_ prefixes are prioritised into the eager cap; zeta_tool is not.
    assert "git_status" in names
    assert "zeta_tool" not in names
    assert registered == 3  # 2 meta + 1 eager


def test_reregistration_is_idempotent_not_additive():
    agent, client = FakeAgent(), FakeMCP()
    catalogue = [{"name": "git_status", "description": "g"}]
    _wire(agent, client, catalogue, max_tools=1)
    _wire(agent, client, catalogue, max_tools=1)

    names = [t.name for t in agent._tools.list_tools()]
    assert len(names) == len(set(names))


async def test_meta_tools_read_the_live_client_and_catalogue():
    agent = FakeAgent()
    first, second = FakeMCP(), FakeMCP()
    holder = {"client": first, "tools": [{"name": "alpha", "description": "first"}]}
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: holder["tools"],
        client_getter=lambda: holder["client"],
        max_tools=0,
    )
    tools = agent.tools_by_name()

    out = await tools["call_tool"].fn("alpha", {"x": 1})
    assert out == "ok:alpha"
    assert first.calls == [("alpha", {"x": 1})]

    # A flap swaps the client; the closure must use the new one, and the live
    # catalogue must be the one searched — never a handle captured at registration.
    holder["client"] = second
    holder["tools"] = [{"name": "beta", "description": "after the flap"}]
    await tools["call_tool"].fn("beta", {})
    assert second.calls == [("beta", {})]
    assert first.calls == [("alpha", {"x": 1})]

    found = await tools["search_tools"].fn("beta")
    assert "beta" in found


def test_intent_tags_survive_registration():
    agent, client = FakeAgent(), FakeMCP()
    catalogue = [{"name": "web_news", "description": "search the web"}]
    _wire(agent, client, catalogue, max_tools=1)

    tools = agent.tools_by_name()
    assert "question" in tools["search_tools"].intent_categories
    assert "command" in tools["call_tool"].intent_categories
    assert "research" in tools["web_news"].intent_categories
    # The description is the one line an 8B reads in the menu: it must name the
    # platform payoff, not the protocol. Measured 2026-10-05: a schema-worded
    # description left the model answering "I don't have real-time access" with
    # the tool registered and on the menu.
    desc = tools["search_tools"].description.lower()
    assert "platform" in desc and "first" in desc


def test_prioritise_keeps_preferred_prefixes_first():
    specs = [
        {"name": "zeta_tool"},
        {"name": "git_status"},
        {"name": "alpha_tool"},
        {"name": "recall"},
    ]
    ordered = [s["name"] for s in _prioritise_mcp_specs(specs)]
    assert ordered[:2] == ["git_status", "recall"]
    assert ordered[2:] == ["zeta_tool", "alpha_tool"]  # tail keeps original order


def test_mcp_intent_categories_tag_by_prefix():
    assert _mcp_intent_categories("codegraph_search") == ["code", "analysis"]
    assert _mcp_intent_categories("web_fetch") == ["research", "web_research", "question"]
    assert _mcp_intent_categories("mystery_tool") == ["analysis"]  # tagged, never bare
