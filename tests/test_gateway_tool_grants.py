"""Gateway tool grants: ADK_GATEWAY_TOOL_DENY enforced at all three reach points.

A grant that only filters discovery is not a grant — call_tool reaches anything in
the catalogue by name. These tests pin the deny list on the eager set, on search
results, and on call_tool itself.
"""

import json

from adk.server import _filter_search_json, gateway_tool_denied, register_gateway_tools_on
from adk.tools import ToolRegistry


class FakeAgent:
    def __init__(self):
        self.name = "fake"
        self._tools = ToolRegistry()

    def tools_by_name(self):
        return {t.name: t for t in self._tools.list_tools()}


class FakeMCP:
    async def call_tool(self, name, arguments):
        return {"success": True, "text": f"ok:{name}"}


def test_deny_matches_exact_and_prefix(monkeypatch):
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "danger_tool, secret_*")
    assert gateway_tool_denied("danger_tool")
    assert gateway_tool_denied("DANGER_TOOL")  # case-insensitive
    assert gateway_tool_denied("secret_handshake")
    assert not gateway_tool_denied("safe_tool")
    # An empty name reaches nothing, denied or not.
    assert gateway_tool_denied("")


def test_deny_is_empty_by_default(monkeypatch):
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    assert not gateway_tool_denied("anything_at_all")


def test_registration_excludes_denied_from_the_eager_set(monkeypatch):
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "git_status")
    catalogue = [
        {"name": "git_status", "description": "would win the priority ordering"},
        {"name": "web_x", "description": "kept"},
    ]
    agent = FakeAgent()
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: catalogue,
        client_getter=lambda: FakeMCP(),
        max_tools=5,
    )
    names = {t.name for t in agent._tools.list_tools()}
    assert "git_status" not in names
    assert "web_x" in names


async def test_call_tool_refuses_a_denied_name(monkeypatch):
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "danger_tool")
    agent = FakeAgent()
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: [],
        client_getter=lambda: FakeMCP(),
        max_tools=0,
    )
    tools = agent.tools_by_name()
    out = json.loads(await tools["call_tool"].fn("danger_tool", {}))
    assert out["error"] == "denied_by_grant"
    # The refusal carries a message and a next step (review finding).
    assert "grant" in out["message"] and out["fix"]
    # A granted name still goes through to the gateway client.
    out2 = await tools["call_tool"].fn("safe_tool", {})
    assert "denied" not in str(out2)


def test_agent_grants_union_with_the_host_list(monkeypatch):
    # Per-agent plane: agent.tool_grants = {"gateway_deny": [...]} (from its
    # agent.yaml) narrows ONE agent without touching the host list.
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    agent = FakeAgent()
    agent.tool_grants = {"gateway_deny": ["agent_secret*", "exact_name"]}
    assert gateway_tool_denied("agent_secret_x", agent)
    assert gateway_tool_denied("exact_name", agent)
    assert not gateway_tool_denied("open_y", agent)
    # Union: the host list still applies to a granted agent.
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "host_tool")
    assert gateway_tool_denied("host_tool", agent)


def test_agent_grants_exclude_from_eager_and_search(monkeypatch):
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    agent = FakeAgent()
    agent.tool_grants = {"gateway_deny": ["git_status"]}
    catalogue = [{"name": "git_status", "description": "x"}, {"name": "web_x", "description": "y"}]
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: catalogue,
        client_getter=lambda: FakeMCP(),
        max_tools=5,
    )
    names = {t.name for t in agent._tools.list_tools()}
    assert "git_status" not in names and "web_x" in names


async def test_search_results_are_filtered(monkeypatch):
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "secret_*")
    import adk.tools_meta as tm

    async def fake_ranked(query, all_tools, limit=8, mcp_client=None):
        return json.dumps({
            "results": [{"name": "secret_x"}, {"name": "open_y"}],
            "count": 2,
            "query": query,
        })

    monkeypatch.setattr(tm, "search_tools_ranked", fake_ranked)
    agent = FakeAgent()
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: [],
        client_getter=lambda: FakeMCP(),
        max_tools=0,
    )
    out = json.loads(await agent.tools_by_name()["search_tools"].fn("q"))
    assert [r["name"] for r in out["results"]] == ["open_y"]
    assert out["count"] == 1


def test_non_json_search_output_passes_through():
    assert _filter_search_json("not json at all") == "not json at all"


async def test_mcp_bridge_registration_respects_the_deny_list(monkeypatch):
    # The FOURTH reach point: ServiceBridge/MCPBridge.register_tools flat-registered
    # the whole catalogue with no deny check (found by adversarial review), so a
    # denied name landed on the agent anyway.
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "secret_*")
    from adk.mcp import MCPBridge

    class FakeBridge(MCPBridge):
        def __init__(self):
            pass

        async def list_tools(self):
            class T:
                def __init__(self, name):
                    self.name = name
                    self.description = "d"
            return [T("secret_x"), T("open_y")]

        async def call_tool(self, name, args):
            return "ok"

    agent = FakeAgent()
    n = await FakeBridge().register_tools(agent)
    names = {t.name for t in agent._tools.list_tools()}
    assert "secret_x" not in names
    assert "open_y" in names
    assert n == 1
