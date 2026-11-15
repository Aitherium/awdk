"""A per-server `"tools": [...]` allowlist mounts only the tools it names.

Adopted from Flue's MCP `tools` allowlist. The case it exists for is a server too
large for a small model's context: the platform gateway lists about 1,900 tools.
Without an allowlist that server is reached through a search tool and a call
tool. With one, the named tools mount directly and the model can call the right
one on the first turn.

Like tests/test_mcp_client.py, every connection here is a REAL SUBPROCESS
(`tests/fixtures/fake_mcp_server.py`, which serves `echo` and `add`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest
from adk.mcp_client import (
    EAGER_MAX_ENV,
    MCPClientError,
    ServerSpec,
    UserMCPManager,
    apply_allowlist,
    parse_config,
    register_user_mcp_tools,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def spec(tools: list[str] | None = None, name: str = "fake") -> ServerSpec:
    return ServerSpec(name=name, command=sys.executable, args=[str(FIXTURE)],
                      tools=tools or [])


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


def _config(tmp_path, monkeypatch, entry: dict) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"fake": {
        "command": sys.executable, "args": [str(FIXTURE)], **entry}}}), encoding="utf-8")
    monkeypatch.setenv("AITHER_MCP_CONFIG", str(cfg))


def test_the_config_key_is_parsed_and_deduplicated_in_order():
    specs = parse_config({"mcpServers": {"gw": {
        "url": "http://localhost:8182/mcp", "tools": ["time_now", "catalog_browse", "time_now"]}}})
    assert specs["gw"].tools == ["time_now", "catalog_browse"]
    # absent = no allowlist = everything, exactly as before
    assert parse_config({"mcpServers": {"gw": {"url": "http://x/mcp"}}})["gw"].tools == []


@pytest.mark.parametrize("bad", ["time_now", {"time_now": True}, ["ok", 3], ["ok", ""]])
def test_a_malformed_allowlist_is_ignored_loudly_not_turned_into_no_tools(bad, caplog):
    with caplog.at_level(logging.WARNING, logger="adk.mcp_client"):
        specs = parse_config({"mcpServers": {"gw": {"url": "http://x/mcp", "tools": bad}}})
    assert specs["gw"].tools == []
    assert "allowlist" in caplog.text and "'gw'" in caplog.text


def test_only_the_named_tools_are_mounted():
    async def go():
        m = UserMCPManager({"fake": spec(["add"])}, own_loop=False)
        tools = await m.connect_all()
        out = await m.call("fake", "add", {"a": 2, "b": 3})
        await m.close()
        return tools, out, m.failures
    tools, out, failures = asyncio.run(go())
    assert [t.name for t in tools] == ["mcp__fake__add"]
    assert "5" in out and failures == {}


def test_the_allowlist_sets_the_order():
    listed = [{"name": "echo"}, {"name": "add"}]
    assert [t["name"] for t in apply_allowlist(spec(["add", "echo"]), listed)] == ["add", "echo"]
    assert apply_allowlist(spec(), listed) is listed          # no allowlist = untouched


def test_a_name_the_server_does_not_expose_fails_the_server_loudly():
    """A typo must not quietly narrow the tool set to less than was asked for."""
    with pytest.raises(MCPClientError, match="ad_d"):
        apply_allowlist(spec(["add", "ad_d"]), [{"name": "echo"}, {"name": "add"}])

    async def go():
        m = UserMCPManager({"typo": spec(["add", "ad_d"], name="typo"),
                            "good": spec(["echo"], name="good")}, own_loop=False)
        tools = await m.connect_all()
        await m.close()
        return tools, m.failures
    tools, failures = asyncio.run(go())
    # the whole server is reported, not half-mounted; the other server is unaffected
    assert [t.name for t in tools] == ["mcp__good__echo"]
    assert "ad_d" in failures["typo"]


def test_an_allowlist_mounts_by_name_where_a_large_server_would_get_search_and_call(
        tmp_path, monkeypatch):
    """The point of the feature. With the eager limit at 1 the two-tool fixture is
    'too large': it registers a search tool and a call tool. Name one tool and it
    mounts directly instead."""
    monkeypatch.setenv(EAGER_MAX_ENV, "1")

    _config(tmp_path, monkeypatch, {})
    agent = _Agent()
    register_user_mcp_tools(agent)
    names = set(agent._tools._tools)
    agent._user_mcp.close_sync()
    assert "mcp__fake__search_tools" in names and "mcp__fake__add" not in names

    _config(tmp_path, monkeypatch, {"tools": ["add"]})
    agent = _Agent()
    assert register_user_mcp_tools(agent) == 1
    names = set(agent._tools._tools)
    assert names == {"mcp__fake__add"}
    out = asyncio.run(agent._tools._tools["mcp__fake__add"].fn(a=2, b=3))
    agent._user_mcp.close_sync()
    assert "5" in out
