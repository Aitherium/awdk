"""Gateway codegraph_search answers from the LOCAL graph first.

Measured 2026-10-14: the model called the gateway's codegraph_search, Genesis
answered 503 "CodeGraph not available", and "where is split_near_question
defined?" ran 38-429 s with no answer while the local awgraph index answered in
~3 s. These tests pin: local hit wins on both reach points (eager closure and
call_tool), a local miss/error falls through to the gateway, and grants still deny.
"""

import json

import adk.builtin_tools as bt
from adk.server import register_gateway_tools_on
from adk.tools import ToolRegistry


class FakeAgent:
    def __init__(self):
        self.name = "fake"
        self._tools = ToolRegistry()

    def tools_by_name(self):
        return {t.name: t for t in self._tools.list_tools()}


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments or {})))
        return {"success": False, "message": "HTTP 503 CodeGraph not available"}


def _local(answer, seen):
    async def graph_code_search(query: str, limit: int = 10) -> str:
        seen.append((query, limit))
        return answer
    return graph_code_search


HIT = json.dumps({"count": 1, "source": "local-awgraph", "results": [
    {"name": "split_near_question", "path": "/r/a.py", "line": 7, "signature": "def f()"}]})


def _setup(monkeypatch, answer=HIT, reachable=True):
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    monkeypatch.setattr(bt, "code_graph_reachable", lambda: reachable)
    agent = FakeAgent()
    seen = []
    agent._tools.register(_local(answer, seen), name="graph_code_search")
    mcp = FakeMCP()
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: [{"name": "codegraph_search", "description": "x"}],
        client_getter=lambda: mcp,
        max_tools=5,
    )
    return agent, mcp, seen


async def test_eager_codegraph_search_uses_local_hit(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch)
    out = await agent.tools_by_name()["codegraph_search"].fn(query="split_near_question")
    assert json.loads(out)["source"] == "local-awgraph"
    assert seen == [("split_near_question", 10)]
    assert mcp.calls == []


async def test_call_tool_codegraph_search_uses_local_hit(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch)
    out = await agent.tools_by_name()["call_tool"].fn(
        "codegraph_search", {"q": "split_near_question", "limit": 3})
    assert json.loads(out)["count"] == 1
    assert seen == [("split_near_question", 3)]
    assert mcp.calls == []


async def test_empty_local_result_falls_through_to_gateway(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch, answer=json.dumps({"count": 0, "results": []}))
    out = await agent.tools_by_name()["codegraph_search"].fn(query="nothing")
    assert seen and mcp.calls == [("codegraph_search", {"query": "nothing"})]
    assert out.startswith("Error:")


async def test_local_error_falls_through_to_gateway(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch, answer=json.dumps({"error": "boom"}))
    await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert seen == [("x", 10)]  # local was asked first
    assert [c[0] for c in mcp.calls] == ["codegraph_search"]


async def test_unreachable_local_graph_is_not_asked(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch, reachable=False)
    await agent.tools_by_name()["codegraph_search"].fn(query="x")
    assert seen == [] and len(mcp.calls) == 1
    # Reachability is read per call: once the graph is up, the local answer wins.
    monkeypatch.setattr(bt, "code_graph_reachable", lambda: True)
    out = await agent.tools_by_name()["codegraph_search"].fn(query="x")
    assert json.loads(out)["count"] == 1 and len(mcp.calls) == 1


async def test_no_local_tool_goes_to_gateway(monkeypatch):
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    monkeypatch.setattr(bt, "code_graph_reachable", lambda: True)
    agent, mcp = FakeAgent(), FakeMCP()
    register_gateway_tools_on(agent, catalogue_getter=lambda: [],
                              client_getter=lambda: mcp, max_tools=0)
    await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert len(mcp.calls) == 1
    # The local tool is looked up per call, so registering it later still counts.
    agent._tools.register(_local(HIT, []), name="graph_code_search")
    out = await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert json.loads(out)["count"] == 1 and len(mcp.calls) == 1


async def test_gateway_closure_under_the_local_name_is_not_a_local_answer(monkeypatch):
    # The gateway catalogue also carries graph_code_search; replace-by-name leaves a
    # gateway closure there, which must not be treated as the local twin.
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY", raising=False)
    monkeypatch.setattr(bt, "code_graph_reachable", lambda: True)
    agent, mcp = FakeAgent(), FakeMCP()
    agent._tools.register(_local(HIT, []), name="graph_code_search")
    register_gateway_tools_on(
        agent,
        catalogue_getter=lambda: [{"name": "graph_code_search", "description": "g"}],
        client_getter=lambda: mcp, max_tools=5)
    await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert [c[0] for c in mcp.calls] == ["codegraph_search"]
    agent._tools.register(_local(HIT, []), name="graph_code_search")
    out = await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert json.loads(out)["count"] == 1 and len(mcp.calls) == 1


async def test_denied_codegraph_search_stays_denied(monkeypatch):
    agent, mcp, seen = _setup(monkeypatch)
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "codegraph_search")
    out = json.loads(await agent.tools_by_name()["call_tool"].fn(
        "codegraph_search", {"query": "x"}))
    assert out["error"] == "denied_by_grant"
    assert seen == [] and mcp.calls == []
    monkeypatch.delenv("ADK_GATEWAY_TOOL_DENY")
    out2 = await agent.tools_by_name()["call_tool"].fn("codegraph_search", {"query": "x"})
    assert json.loads(out2)["count"] == 1 and mcp.calls == []


async def test_a_cold_local_index_is_not_waited_on(monkeypatch):
    # Review 2026-10-14: right after a restart the worker hydrates for minutes; the
    # gateway used to answer at once and must still.
    from types import SimpleNamespace

    agent, mcp, seen = _setup(monkeypatch)
    monkeypatch.setattr(bt, "_local_code_graph", lambda: SimpleNamespace(is_warm=False))
    await agent.tools_by_name()["codegraph_search"].fn(query="x")
    assert seen == [] and mcp.calls, "cold index skipped, gateway asked"


async def test_a_slow_local_answer_is_bounded(monkeypatch):
    import asyncio
    import time

    import adk.server as srv

    monkeypatch.setattr(srv, "_SHADOW_WAIT_S", 0.2)
    agent, mcp, _ = _setup(monkeypatch)

    async def graph_code_search(query: str, limit: int = 10) -> str:
        await asyncio.sleep(30)
        return HIT

    agent._tools.register(graph_code_search, name="graph_code_search")
    t0 = time.monotonic()
    await agent.tools_by_name()["codegraph_search"].fn(query="x")
    assert time.monotonic() - t0 < 5 and mcp.calls


async def test_the_tool_and_a_code_turn_give_the_worker_their_own_budget():
    # Every live query timed out at the crystal's 2.5 s; warm ones take 2.5-4.7 s.
    from adk.crystal import _graph_query

    seen = []

    class G:
        async def query(self, text, k, timeout=None):
            seen.append(timeout)
            return []

    class Old:
        async def query(self, text, k):
            seen.append("old")
            return []

    await _graph_query(G(), "q", 3, bt.TOOL_GRAPH_WAIT_S)
    await _graph_query(Old(), "q", 3, 9.0)
    assert seen == [bt.TOOL_GRAPH_WAIT_S, "old"] and bt.TOOL_GRAPH_WAIT_S > 2.5
