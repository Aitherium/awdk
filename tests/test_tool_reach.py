"""Tool reach on the awsh turn: local code graph, skills, delegation, instruments.

Each test pins one measured defect:

- graph_code_search / code_concept_search called Genesis at localhost:8001, which
  publishes no host port, so every code question came back as a connection error
  while the repo's awgraph index sat on disk;
- no tool could search or load a skill;
- an instrument whose package was missing still registered and was still advertised;
- swarm_code was sync and, inside the agent's loop, returned a "working" placeholder
  for a task nobody awaited;
- a gateway delegation (forge_subagent, ask_agent) died at the 30 s client default;
- three reach tools sat outside the core menu, so the model saw no schema for them.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import adk.builtin_tools as bt
import adk.reach as reach
from adk.client import _gateway_mcp as gw
from adk.pack_activation import _make_load_tool
from adk.reach import REACH_MAX_CHARS, reach_lines
from adk.server import _prioritise_mcp_specs
from adk.tool_selection import CORE_TOOL_NAMES
from adk.tools import ToolRegistry

NEW_CORE = {"repl_run", "skill_search", "load_pack_skill"}


class _FakeGraph:
    """Stands in for adk.crystal.AwgraphIndex: async query(text, k) -> hits."""

    def __init__(self, root: str):
        self.root = root
        self.queries: list[tuple[str, int]] = []

    async def query(self, text: str, k: int) -> list[dict]:
        self.queries.append((text, k))
        return [{"name": "stream_react", "path": os.path.join("adk", "agent.py"),
                 "line": 3122, "signature": "async def stream_react(self, message)"}]


def _agent() -> SimpleNamespace:
    return SimpleNamespace(name="reach-test", _tools=ToolRegistry())


@pytest.fixture
def local_graph(tmp_path, monkeypatch):
    root = str(tmp_path / "repo")
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.setenv("ADK_CRYSTAL_GRAPH_ROOT", root)
    fake = _FakeGraph(os.path.abspath(root))
    monkeypatch.setitem(bt._LOCAL_GRAPHS, os.path.abspath(root), fake)
    return fake


# ── code graph ────────────────────────────────────────────────────────────


def test_graph_code_search_uses_local_graph_without_genesis(local_graph):
    out = json.loads(asyncio.run(bt.graph_code_search("stream_react", limit=3)))
    assert "error" not in out, out
    assert out["count"] == 1 and out["source"] == "local-awgraph"
    hit = out["results"][0]
    assert hit["name"] == "stream_react" and hit["line"] == 3122
    # Absolute: the daemon's cwd is not the index root.
    assert os.path.isabs(hit["path"])
    assert hit["path"].startswith(local_graph.root)
    assert local_graph.queries == [("stream_react", 3)]

    cards = json.loads(asyncio.run(bt.code_concept_search("stream_react")))
    assert cards["count"] == 1 and "error" not in cards


def test_code_graph_tools_register_only_with_a_backend(local_graph, monkeypatch):
    agent = _agent()
    bt.register_builtin_tools(agent, categories=["graph"])
    assert agent._tools.get("graph_code_search") is not None
    assert agent._tools.get("code_concept_search") is not None

    monkeypatch.delenv("ADK_CRYSTAL_GRAPH_ROOT", raising=False)
    bare = _agent()
    bt.register_builtin_tools(bare, categories=["graph"])
    assert bare._tools.get("graph_code_search") is None
    assert bare._tools.get("code_concept_search") is None
    assert bare._tools.get("graph_search") is not None  # the rest is unchanged


# ── skills ────────────────────────────────────────────────────────────────


@pytest.fixture
def skill_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    sk = tmp_path / "skills"
    (sk / "deploy-fleet").mkdir(parents=True)
    (sk / "deploy-fleet" / "SKILL.md").write_text(
        "---\nname: deploy-fleet\ndescription: Roll a service image onto the fleet\n---\n"
        "# Deploy\n" + ("Step line.\n" * 600), encoding="utf-8")
    (sk / "rotate-key.json").write_text(json.dumps({
        "name": "rotate key", "description": "Rotate an API key without an outage",
        "steps": ["mint the new key", "roll consumers", "revoke the old key"],
        "tools_used": ["shell_exec"], "tags": ["secrets"],
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_SKILL_DIRS", str(sk))
    return sk


def test_skill_search_finds_json_and_skill_md(skill_dirs):
    a = json.loads(asyncio.run(bt.skill_search("rotate the api key")))
    assert [r["name"] for r in a["results"]][:1] == ["rotate key"]
    assert a["results"][0]["kind"] == "json"
    b = json.loads(asyncio.run(bt.skill_search("deploy a service to the fleet")))
    assert [r["name"] for r in b["results"]][:1] == ["deploy-fleet"]
    assert b["results"][0]["kind"] == "md"


def test_skill_load_returns_capped_body(skill_dirs):
    body = asyncio.run(bt.skill_load("deploy-fleet"))
    assert body.startswith("---\nname: deploy-fleet")
    assert len(body) <= 3000 + 200 and "[truncated:" in body
    steps = asyncio.run(bt.skill_load("rotate-key"))  # slug form finds the json skill
    assert "1. mint the new key" in steps and "shell_exec" in steps
    missing = json.loads(asyncio.run(bt.skill_load("nope")))
    assert "error" in missing


# ── instruments ───────────────────────────────────────────────────────────


def test_missing_package_drops_instrument_from_registry_and_reach(monkeypatch):
    monkeypatch.setattr(bt, "_package_importable", lambda name: name != "awrepl")
    agent = _agent()
    bt.register_builtin_tools(agent, categories=["instruments"])
    assert agent._tools.get("repl_run") is None
    assert agent._tools.get("repl_reset") is None
    assert agent._tools.get("recurse_file") is not None
    lines = reach_lines(agent)
    assert "repl_run" not in lines
    assert "recurse_file(path, query)" in lines

    monkeypatch.setattr(bt, "_package_importable", lambda name: True)
    full = _agent()
    bt.register_builtin_tools(full, categories=["instruments"])
    assert full._tools.get("repl_run") is not None
    assert "repl_run(code)" in reach_lines(full)


def test_reach_lines_name_only_registered_tools_within_budget(local_graph, monkeypatch):
    monkeypatch.setattr(bt, "_package_importable", lambda name: True)
    agent = _agent()
    bt.register_builtin_tools(
        agent, categories=["graph", "skills", "a2a", "instruments", "browse"])

    async def call_tool(name: str, arguments: dict | None = None) -> str:
        return ""

    agent._tools.register(call_tool, name="call_tool")
    text = reach_lines(agent)
    for name in ("ask_agent", "code_concept_search", "skill_search", "a2a_send",
                 "repl_run"):
        assert name in text, (name, text)
    assert len(text) <= reach.REACH_TARGET_CHARS <= REACH_MAX_CHARS
    assert reach_lines(_agent()) == ""  # nothing registered -> nothing advertised
    assert reach_lines(agent, SimpleNamespace(agentic=False)) == ""


# ── delegation ────────────────────────────────────────────────────────────


def test_swarm_code_is_a_coroutine():
    assert inspect.iscoroutinefunction(bt.swarm_code)


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"result": {"content": [{"type": "text", "text": "done"}]}}


def test_long_gateway_tools_get_a_600s_timeout(monkeypatch):
    seen: list[object] = []

    class _Client:
        def __init__(self, timeout=None, headers=None, **_kw):
            seen.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            return _Resp()

    monkeypatch.setattr(gw.httpx, "AsyncClient", _Client)
    client = gw.GatewayMCPClient(gateway_url="http://gateway.test", api_key="k")

    async def _ok() -> bool:
        return True

    monkeypatch.setattr(client, "_ensure_session", _ok)
    asyncio.run(client.call_tool("forge_subagent", {"task": "x"}))
    asyncio.run(client.call_tool("ask_agent", {"agent": "a", "question": "q"}))
    asyncio.run(client.call_tool("fs_read_file", {"path": "x"}))
    assert seen[0] >= 600 and seen[1] >= 600
    assert seen[2] == 30.0  # ordinary tools keep the client default


def test_delegation_tools_lead_the_eager_order():
    specs = [{"name": n} for n in (
        "zz_misc", "fs_read_file", "relay_a2a_delegate", "codegraph_search",
        "forge_subagent", "ask_agent")]
    order = [s["name"] for s in _prioritise_mcp_specs(specs)]
    assert order[:3] == ["ask_agent", "forge_subagent", "relay_a2a_delegate"]
    assert order[3:] == ["fs_read_file", "codegraph_search", "zz_misc"]


# ── schema tax ────────────────────────────────────────────────────────────


def _tax(reg: ToolRegistry, names) -> int:
    tds = [td for td in reg.list_tools() if td.name in names]
    return len(json.dumps(reg.to_openai_format(tds))) // 4


def test_core_menu_schema_tax_increase_is_at_most_200_tokens():
    assert NEW_CORE <= CORE_TOOL_NAMES
    reg = ToolRegistry()
    for fn in (bt.file_read, bt.file_list, bt.file_write, bt.file_edit, bt.shell_exec,
               bt.web_search, bt.web_fetch, bt.repl_run, bt.skill_search):
        reg.register(fn)
    reg.register(_make_load_tool(SimpleNamespace()), name="load_pack_skill")
    before = _tax(reg, CORE_TOOL_NAMES - NEW_CORE)
    after = _tax(reg, CORE_TOOL_NAMES)
    print(f"core schema tax: before={before} after={after} tokens (chars/4)")
    assert after - before <= 200, (before, after)


def test_first_local_graph_query_hydrates_off_the_event_loop(tmp_path, monkeypatch):
    """awgraph's _open_graph is async in name only (sync pickle load + hydrate, tens
    of seconds on a real index); the first query must not freeze the daemon's loop."""
    import time

    from adk.crystal import AwgraphIndex

    class _SlowOpen(AwgraphIndex):
        async def _open(self) -> bool:
            time.sleep(0.6)  # blocking-ok: models the sync hydrate inside _open_graph
            self._ok = True
            return True

        async def query(self, text: str, k: int) -> list[dict]:
            await self._open() if self._ok is None else None
            return [{"name": "x", "path": "a.py", "line": 1, "signature": ""}]

    root = os.path.abspath(str(tmp_path / "repo"))
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.setenv("ADK_CRYSTAL_GRAPH_ROOT", root)
    monkeypatch.setitem(bt._LOCAL_GRAPHS, root, _SlowOpen(root))

    async def _run() -> tuple[int, dict]:
        ticks = 0
        done = asyncio.Event()

        async def _ticker() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.02)

        t = asyncio.ensure_future(_ticker())
        await asyncio.sleep(0)
        out = json.loads(await bt.graph_code_search("x"))
        done.set()
        await t
        return ticks, out

    ticks, out = asyncio.run(_run())
    assert out["count"] == 1, out
    assert ticks >= 10, f"event loop starved during hydration: {ticks} ticks in 0.6 s"


# ── review fixes ──────────────────────────────────────────────────────────


def test_delegate_line_omits_tools_a_deny_grant_blocks(monkeypatch):
    """call_tool refuses a denied name, so the reach line must not advertise it."""
    import adk.server  # noqa: F401 -- reach reads gateway_tool_denied from it

    agent = _agent()

    async def call_tool(name: str, arguments: dict | None = None) -> str:
        return ""

    agent._tools.register(call_tool, name="call_tool")
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "forge_subagent")
    text = reach_lines(agent)
    assert "ask_agent" in text and "forge_subagent" not in text, text
    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "ask_agent,forge_subagent")
    assert "delegate" not in reach_lines(agent)


def test_failed_local_graph_open_unregisters_the_code_tools(tmp_path, monkeypatch):
    """An index whose open failed (no index on disk) must not keep both tools
    registered on every later agent build, each call an error."""
    from adk.crystal import AwgraphIndex

    root = os.path.abspath(str(tmp_path / "repo"))
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.setenv("ADK_CRYSTAL_GRAPH_ROOT", root)
    dead = AwgraphIndex(root)
    dead._ok = False
    dead.reason = "no-index"
    monkeypatch.setitem(bt._LOCAL_GRAPHS, root, dead)
    assert bt.code_graph_reachable() is False
    agent = _agent()
    bt.register_builtin_tools(agent, categories=["graph"])
    assert agent._tools.get("graph_code_search") is None


def test_skill_scan_cap_bounds_json_files_too(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    sk = tmp_path / "skills"
    sk.mkdir()
    for i in range(12):
        (sk / f"s{i:02d}.json").write_text(json.dumps({"name": f"s{i}"}), encoding="utf-8")
    monkeypatch.setenv("AITHER_SKILL_DIRS", str(sk))
    monkeypatch.setattr(bt, "_SKILL_SCAN_CAP", 5)
    assert len(bt._skill_index()) <= 5


def test_a2a_send_reports_why_the_peer_failed_and_refuses_itself(monkeypatch):
    import adk.a2a_client as ac

    async def _failed(agent_name, text, **kw):
        return {"task": {"id": "t1", "status": {"state": "failed",
                                                "message": "No agent configured"}}}

    monkeypatch.setattr(ac, "send_message", _failed)
    out = json.loads(asyncio.run(bt.a2a_send("peer", "hi")))
    assert out["state"] == "failed" and "No agent configured" in out.get("detail", ""), out

    monkeypatch.setattr(bt, "_a2a_sender_name", lambda: "awsh-agent")
    me = json.loads(asyncio.run(bt.a2a_send("AWSH-Agent", "hi")))
    assert "error" in me and "itself" in me["error"], me
