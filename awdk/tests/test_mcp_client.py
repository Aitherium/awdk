"""Inbound MCP: a self-hoster's own server really reaches the agent.

Every assertion here drives a REAL SUBPROCESS speaking real JSON-RPC
(`tests/fixtures/fake_mcp_server.py`). The client's entire job is to spawn a
process and talk to it, so a mocked transport would only prove our mock agrees
with our client. That failure has a precedent in this codebase — an AsyncMock
standing in for an httpx response satisfied every assertion while the code under
test fell through to a live call.

The fixture also does three things tidy fixtures do not, because real servers
do them and each one has broken a client somewhere: it prints a banner to stdout
before any protocol traffic, writes to stderr, and emits an id-less notification
in the middle of the stream.

(On the import block below: there is deliberately NO blank line before the
`adk` import. The quality gate runs ruff from the REPO ROOT, where `adk` is
not on the path and isort groups it with third-party packages; run ruff from
inside awdk/ and it is first-party and wants the blank line back. The two
views are mutually exclusive, so this satisfies the one that gates CI. The
note lives here rather than beside the imports because a comment there forms
its own group and changes the answer.)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from adk.mcp_client import (
    MCPClientError,
    ServerSpec,
    StdioMCPClient,
    UserMCPManager,
    load_config,
    parse_config,
    qualified_name,
    register_user_mcp_tools,
    render_tool_result,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def spec(*flags: str, name: str = "fake") -> ServerSpec:
    return ServerSpec(name=name, command=sys.executable,
                      args=[str(FIXTURE), *flags])


# ── config parsing ───────────────────────────────────────────────────────────

def test_parses_the_claude_code_shape():
    # Deliberately the format users already have, so nobody writes a fourth one.
    cfg = parse_config({"mcpServers": {
        "sqlite": {"command": "uvx", "args": ["mcp-server-sqlite"]},
        "weather": {"url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer k"}},
    }})
    assert cfg["sqlite"].transport == "stdio"
    assert cfg["sqlite"].args == ["mcp-server-sqlite"]
    assert cfg["weather"].transport == "http"
    assert cfg["weather"].headers["Authorization"] == "Bearer k"


def test_a_server_with_neither_command_nor_url_is_skipped_not_guessed():
    cfg = parse_config({"mcpServers": {"broken": {"description": "oops"},
                                       "good": {"command": "x"}}})
    assert "broken" not in cfg
    assert "good" in cfg


def test_disabled_is_honoured():
    cfg = parse_config({"mcpServers": {"off": {"command": "x", "disabled": True}}})
    assert cfg["off"].disabled is True


def test_a_named_config_that_does_not_exist_raises(tmp_path, monkeypatch):
    # Falling through to a different file would make "my servers aren't loading"
    # unanswerable: the user named a path and got something else.
    monkeypatch.setenv("AITHER_MCP_CONFIG", str(tmp_path / "nope.json"))
    with pytest.raises(MCPClientError) as e:
        load_config()
    assert "does not exist" in str(e.value)


def test_no_config_anywhere_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_MCP_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    specs, source = load_config(cwd=tmp_path)
    assert specs == {} and source == ""


# ── the stdio transport ──────────────────────────────────────────────────────

def test_a_real_server_hands_over_its_tools():
    async def go():
        c = StdioMCPClient(spec())
        await c.start()
        await c.initialize()
        tools = await c.list_tools()
        await c.close()
        return tools
    tools = asyncio.run(go())
    assert {t["name"] for t in tools} == {"echo", "add"}
    # The SERVER's schema, which is the only thing that can describe its args.
    echo = next(t for t in tools if t["name"] == "echo")
    assert echo["inputSchema"]["properties"]["text"]["type"] == "string"


def test_a_stdout_banner_does_not_break_the_handshake():
    # The fixture prints "fake-mcp-server starting up" before any JSON. A client
    # that treats the first stdout line as its reply dies right here.
    async def go():
        c = StdioMCPClient(spec())
        await c.start()
        info = await c.initialize()
        await c.close()
        return info
    assert asyncio.run(go())["serverInfo"]["name"] == "fake"


def test_an_idless_notification_is_skipped_not_mistaken_for_the_reply():
    # The fixture emits notifications/message immediately before the tools/list
    # result. Returning the first line read would yield the notification.
    async def go():
        c = StdioMCPClient(spec())
        await c.start()
        await c.initialize()
        tools = await c.list_tools()
        await c.close()
        return tools
    assert len(asyncio.run(go())) == 2


def test_a_tool_actually_runs():
    async def go():
        c = StdioMCPClient(spec())
        await c.start()
        await c.initialize()
        out = await c.call_tool("add", {"a": 2, "b": 3})
        await c.close()
        return out
    assert "5" in asyncio.run(go())


def test_a_server_that_dies_names_itself_and_does_not_hang():
    async def go():
        c = StdioMCPClient(spec("--die", name="deadone"))
        await c.start()
        try:
            await c.initialize()
        finally:
            await c.close()
    with pytest.raises(MCPClientError) as e:
        asyncio.run(go())
    assert "deadone" in str(e.value)


def test_a_hanging_server_times_out_rather_than_wedging_the_agent():
    async def go():
        c = StdioMCPClient(spec("--hang", name="slowpoke"), timeout=1.0)
        await c.start()
        try:
            # Through the PUBLIC api, with the client's own timeout. Reaching
            # for a private method here would have been the tell that the
            # timeout is not actually reachable the way a caller reaches it.
            await c.list_tools()
        finally:
            await c.close()
    with pytest.raises(MCPClientError) as e:
        asyncio.run(go())
    assert "slowpoke" in str(e.value)


# ── merging, naming, failure ─────────────────────────────────────────────────

def test_two_servers_with_the_same_tool_name_do_not_shadow_each_other():
    # Both fixtures expose `echo`. A bare merge would let one silently win and
    # the caller would have nothing to look at.
    async def go():
        m = UserMCPManager({"alpha": spec(name="alpha"), "beta": spec(name="beta")},
                           own_loop=False)
        tools = await m.connect_all()
        await m.close()
        return tools
    tools = asyncio.run(go())
    names = {t.name for t in tools}
    assert "mcp__alpha__echo" in names
    assert "mcp__beta__echo" in names
    assert len(names) == 4


def test_one_dead_server_does_not_take_the_others_with_it():
    async def go():
        m = UserMCPManager({"good": spec(name="good"),
                            "bad": spec("--die", name="bad")}, own_loop=False)
        tools = await m.connect_all()
        out = await m.call("bad", "echo", {"text": "hi"})
        await m.close()
        return tools, out, m.failures
    tools, out, failures = asyncio.run(go())
    assert {t.server for t in tools} == {"good"}
    # REPORTED, not merely absent.
    assert "bad" in failures
    # And a call to it NAMES it — an empty string here is indistinguishable
    # from "nothing matched", which is how a dead integration passes as working.
    assert "bad" in out and "unavailable" in out.lower()


def test_a_disabled_server_is_not_connected():
    async def go():
        s = spec(name="off")
        s.disabled = True
        m = UserMCPManager({"off": s}, own_loop=False)
        tools = await m.connect_all()
        await m.close()
        return tools, m.failures
    tools, failures = asyncio.run(go())
    # Not connected AND not a failure: switching something off is not a fault.
    assert tools == [] and failures == {}


def test_qualified_names_survive_awkward_server_names():
    assert qualified_name("my server!", "do-thing") == "mcp__my_server___do_thing"


# ── result rendering ─────────────────────────────────────────────────────────

def test_an_error_result_is_returned_as_text_not_raised():
    # A raised exception would abort the whole turn over one bad call; the agent
    # can recover from a tool that says why it failed.
    out = render_tool_result({"content": [{"type": "text", "text": "nope"}],
                              "isError": True})
    assert out.startswith("Error from tool:") and "nope" in out


def test_an_empty_result_never_renders_as_an_empty_string():
    # "returned nothing" and "did not run" must not look the same to the model.
    assert render_tool_result({}).strip() != ""
    assert render_tool_result({"content": []}).strip() != ""


def test_binary_content_is_named_not_dumped():
    # Inlining base64 would blow the context window on a single call.
    out = render_tool_result({"content": [{"type": "image", "data": "A" * 10000}]})
    assert "image" in out and len(out) < 200


# ── registration onto an agent ───────────────────────────────────────────────

class _Reg:
    def __init__(self):
        self._tools = {}

    def register(self, fn, name=None, description=None, **_):
        from adk.tools import ToolDef
        td = ToolDef(name=name or fn.__name__, description=description or "",
                     parameters={}, fn=fn, is_async=True)
        self._tools[td.name] = td
        return td


class _Agent:
    def __init__(self):
        self._tools = _Reg()


def test_registration_puts_real_callable_tools_on_the_agent(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(FIXTURE)]}}}), encoding="utf-8")
    monkeypatch.setenv("AITHER_MCP_CONFIG", str(cfg))

    agent = _Agent()
    n = register_user_mcp_tools(agent)
    assert n == 2
    assert "mcp__fake__add" in agent._tools._tools

    # The SERVER's schema reached the tool. Inferring from the handler's
    # signature would describe every tool as taking nothing, because it takes
    # **kwargs — the model would then never pass an argument.
    td = agent._tools._tools["mcp__fake__add"]
    assert td.parameters["properties"]["a"]["type"] == "number"

    # And it actually runs, through the connection the manager is holding open.
    out = asyncio.run(td.fn(a=2, b=3))
    assert "5" in out
    agent._user_mcp.close_sync()


def test_an_mcp_tool_may_not_shadow_a_builtin(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(FIXTURE)]}}}), encoding="utf-8")
    monkeypatch.setenv("AITHER_MCP_CONFIG", str(cfg))

    agent = _Agent()

    def mcp__fake__add():
        return "the builtin"
    agent._tools.register(mcp__fake__add, name="mcp__fake__add",
                          description="pre-existing")

    n = register_user_mcp_tools(agent)
    # A built-in silently replaced by a stranger's tool of the same name is a
    # capability swap nobody can see. Refuse, keep the incumbent.
    assert n == 1
    assert agent._tools._tools["mcp__fake__add"].description == "pre-existing"
    agent._user_mcp.close_sync()


def test_no_config_registers_nothing_and_says_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_MCP_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)
    agent = _Agent()
    # The ordinary case. Treating "no user servers" as a problem would make
    # every clean run noisy, and a noisy run gets ignored.
    assert register_user_mcp_tools(agent) == 0


def test_the_manager_is_held_so_connections_outlive_registration(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(FIXTURE)]}}}), encoding="utf-8")
    monkeypatch.setenv("AITHER_MCP_CONFIG", str(cfg))
    agent = _Agent()
    register_user_mcp_tools(agent)
    # Without this reference the stdio subprocesses are garbage-collected and
    # every tool fails on first use, long after registration reported success.
    assert getattr(agent, "_user_mcp", None) is not None
    assert agent._user_mcp.clients
    agent._user_mcp.close_sync()
