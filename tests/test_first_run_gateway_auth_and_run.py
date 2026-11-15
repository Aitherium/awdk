"""First-run breaks measured 2026-11-12 on adk 3.8.105, walked as a new user.

1. ``adk mcp setup --mode local --bake-token`` printed ``connected (healthy)`` and
   baked a credential the local gateway answers with ``401 invalid_token``;
   ``adk mcp status`` said OK. Health was the only check.
2. ``adk tools list`` printed ``MCP: not available ('MCPTool' object has no
   attribute 'get')``.
3. ``adk run`` in a scaffolded project served an agent named ``aither`` without the
   project's tools, and an unreachable model backend ended in a transport traceback.
4. (unmasked by fixing 1) a project ``.mcp.json`` pointing at a gateway with
   ~1,900 tools put every schema in the prompt and overflowed a 32k local model.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import types
from pathlib import Path

# Same guards tests/conftest.py applies, so this module also runs with --noconftest.
if not (os.environ.get("AITHER_AIR_GAP_CONFIG") or os.environ.get("AITHER_AIR_GAP")):
    os.environ["AITHER_AIR_GAP_CONFIG"] = os.path.join(
        tempfile.mkdtemp(prefix="adk-test-airgap-"), "air_gap.yaml")
os.environ.setdefault("AITHER_TOOL_CAPTURE", "0")

import httpx  # noqa: E402
import pytest  # noqa: E402

GOOD = "good-session-bearer-0001"
STALE = "aither_stale_portal_token_0002"


# ─────────────────────────────────────────────────────────────────────────────
# A gateway that behaves like the real one: /health is open, /mcp wants a bearer
# and a session.
# ─────────────────────────────────────────────────────────────────────────────

def _gateway(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/health"):
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path.endswith("/.well-known/mcp.json"):
        return httpx.Response(200, json={"tier": "platform", "user": "tester"})
    if request.headers.get("authorization") != f"Bearer {GOOD}":
        return httpx.Response(401, json={"error": "invalid_token"})
    body = json.loads(request.content or b"{}")
    method = body.get("method")
    if method == "initialize":
        return httpx.Response(200, headers={"mcp-session-id": "sid-1"},
                              json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
    if request.headers.get("mcp-session-id") != "sid-1":
        return httpx.Response(400, json={"error": {"message": "Missing session ID"}})
    if method == "tools/list":
        sse = "event: message\ndata: " + json.dumps({
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"tools": [{"name": "time_now"}, {"name": "status"}]}}) + "\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    return httpx.Response(202)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """Route every httpx.Client in adk.mcp_setup at the fake gateway; isolate ~/.aither."""
    real_client = httpx.Client
    state = {"handler": _gateway}

    def client(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(lambda r: state["handler"](r))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    home = tmp_path / "aither-home"
    home.mkdir()
    monkeypatch.setenv("AITHER_HOME", str(home))
    for var in ("AITHER_API_KEY", "AITHER_MCP_KEY", "AITHERIUM_API_KEY", "AITHER_GATEWAY_KEY"):
        monkeypatch.delenv(var, raising=False)
    import adk.mcp_setup as mcp_setup
    monkeypatch.setattr(mcp_setup, "ensure_local_ca_trust", lambda: "already")
    state["home"] = home
    return state


def _setup_args(project: Path, bake: bool = True):
    return types.SimpleNamespace(mode="local", ide="claude-code",
                                 project_dir=str(project), bake_token=bake)


# ── 1. setup / status prove the credential ──────────────────────────────────

def test_login_session_outranks_a_stale_portal_token(gateway):
    from adk.mcp_setup import auth_candidates, resolve_auth

    (gateway["home"] / "portal.token").write_text(STALE, encoding="utf-8")
    (gateway["home"] / "session-bearer").write_text(GOOD + "\n", encoding="utf-8")
    assert [tok for tok, _ in auth_candidates()] == [GOOD, STALE]
    token, source = resolve_auth()
    assert token == GOOD and GOOD not in source


def test_verify_gateway_auth_is_a_real_tools_list(gateway):
    from adk.mcp_setup import verify_gateway_auth

    ok = verify_gateway_auth("http://localhost:8182/mcp", GOOD)
    assert ok["ok"] is True and ok["tool_count"] == 2

    bad = verify_gateway_auth("http://localhost:8182/mcp", STALE)
    assert bad["ok"] is False and bad["status_code"] == 401
    assert "401" in bad["error"] and "invalid_token" in bad["error"]
    assert STALE not in bad["error"]

    gateway["handler"] = _down
    unknown = verify_gateway_auth("http://localhost:8182/mcp", GOOD)
    assert unknown["ok"] is None and GOOD not in unknown["error"]


def test_probe_gateway_does_not_call_a_rejected_credential_healthy(gateway):
    from adk.mcp_setup import probe_gateway

    res = probe_gateway("http://localhost:8182/mcp", STALE)
    assert res["connected"] is True          # /health answered
    assert res["auth_ok"] is False           # ...and that is not the verdict
    assert probe_gateway("http://localhost:8182/mcp", GOOD)["auth_ok"] is True


def test_setup_bakes_the_credential_the_gateway_accepts(gateway, tmp_path, capsys):
    from adk import cli

    (gateway["home"] / "portal.token").write_text(STALE, encoding="utf-8")
    (gateway["home"] / "session-bearer").write_text(GOOD, encoding="utf-8")
    project = tmp_path / "proj"
    assert cli._cmd_mcp_setup(_setup_args(project)) == 0
    out = capsys.readouterr().out
    baked = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert baked["mcpServers"]["aitheros"]["headers"]["Authorization"] == f"Bearer {GOOD}"
    assert "verified" in out and "2 tools" in out
    assert "connected, authenticated" in out
    assert GOOD not in out and STALE not in out


def test_setup_fails_loudly_when_every_credential_is_rejected(gateway, tmp_path, capsys):
    from adk import cli

    (gateway["home"] / "portal.token").write_text(STALE, encoding="utf-8")
    project = tmp_path / "proj"
    assert cli._cmd_mcp_setup(_setup_args(project)) == 1
    out = capsys.readouterr().out
    assert not (project / ".mcp.json").exists(), "a rejected credential must not be baked"
    assert "rejected" in out and "401" in out and "adk login" in out
    assert "connected (healthy)" not in out
    assert STALE not in out


def test_setup_says_not_verified_when_the_gateway_is_down(gateway, tmp_path, capsys):
    from adk import cli

    (gateway["home"] / "session-bearer").write_text(GOOD, encoding="utf-8")
    gateway["handler"] = _down
    assert cli._cmd_mcp_setup(_setup_args(tmp_path / "proj")) == 0
    out = capsys.readouterr().out
    assert "NOT VERIFIED" in out and "not reachable" in out
    assert "authenticated (" not in out


def test_setup_without_bake_does_not_claim_tool_access(gateway, tmp_path, capsys):
    from adk import cli

    assert cli._cmd_mcp_setup(_setup_args(tmp_path / "proj", bake=False)) == 0
    out = capsys.readouterr().out
    assert "health only" in out and "connected (" not in out


def test_status_marks_rejected_auth_and_exits_nonzero(gateway, capsys):
    from adk import cli

    (gateway["home"] / "portal.token").write_text(STALE, encoding="utf-8")
    assert cli._cmd_mcp_status(types.SimpleNamespace()) == 1
    out = capsys.readouterr().out
    assert "[OK]" not in out and "AUTH REJECTED" in out and "adk login" in out
    assert STALE not in out

    (gateway["home"] / "session-bearer").write_text(GOOD, encoding="utf-8")
    assert cli._cmd_mcp_status(types.SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "[OK]" in out and "tools=2" in out and GOOD not in out


# ── 2. tools list ───────────────────────────────────────────────────────────

def test_tools_list_prints_mcp_tools(monkeypatch, capsys):
    from adk import cli
    import adk.mcp as mcp

    class Bridge:
        def __init__(self, *a, **k):
            pass

        async def list_tools(self):
            return [mcp.MCPTool(name="time_now", description="the current time"),
                    mcp.MCPTool(name="status", description="platform status")]

    monkeypatch.setattr(mcp, "MCPBridge", Bridge)
    monkeypatch.setenv("AITHER_API_KEY", "k")
    rc = cli.cmd_tools(types.SimpleNamespace(tools_command="list", upgrade=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not available" not in out
    assert "time_now" in out and "the current time" in out and "Total: 2 MCP tools" in out


def test_mcp_tool_row_reads_dataclasses_and_dicts():
    from adk import cli
    from adk.mcp import MCPTool

    assert cli._mcp_tool_row(MCPTool(name="a", description="d")) == ("a", "d", "")
    assert cli._mcp_tool_row({"name": "b", "description": "e", "tier": "pro"}) == ("b", "e", "pro")


def test_tools_sync_retries_a_401_with_the_login_session(gateway, monkeypatch, tmp_path, capsys):
    """The active profile's key was refused (401) while the session on the same
    machine was accepted; sync stopped at the first refusal."""
    from adk import cli
    import adk.auth as auth

    (gateway["home"] / "session-bearer").write_text(GOOD, encoding="utf-8")
    monkeypatch.setattr(auth, "resolve_credentials",
                        lambda **k: types.SimpleNamespace(access_token=STALE))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    sent = []

    def manifest(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != f"Bearer {GOOD}":
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"version": "v1", "tools": [{"name": "time_now"}]})

    real_async = httpx.AsyncClient

    def async_client(*a, **k):
        k.pop("verify", None)
        return real_async(*a, transport=httpx.MockTransport(manifest), **k)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    rc = cli.cmd_tools(types.SimpleNamespace(tools_command="sync", verbose=False))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert sent == [f"Bearer {STALE}", f"Bearer {GOOD}"]
    assert "Synced 1 tools" in out and GOOD not in out and STALE not in out
    assert (tmp_path / ".aither" / "tools-manifest.json").is_file()

    # Every credential refused: still a clear failure, exit 1.
    (gateway["home"] / "session-bearer").unlink()
    assert cli.cmd_tools(types.SimpleNamespace(tools_command="sync", verbose=False)) == 1
    assert "Authentication failed" in capsys.readouterr().out


# ── 3. adk run serves the project's agent ───────────────────────────────────

@pytest.fixture
def scaffold(tmp_path, monkeypatch):
    """A project made by `adk init`, with ~/.aither isolated and no MCP config."""
    from adk import cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AITHER_HOME", str(home / ".aither"))
    for var in ("AITHER_MCP_CONFIG", "AITHER_IDENTITY", "ADK_CRYSTAL_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    project = tmp_path / "proj"
    assert cli.cmd_init(types.SimpleNamespace(name="scaffolded", directory=str(project))) in (0, None)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return project


def _run_args(**over):
    base = dict(identity=None, port=None, host=None, backend=None, model=None,
                fleet=None, agents=None, crystal="")
    base.update(over)
    return types.SimpleNamespace(**base)


def test_load_project_agent_returns_the_scaffolded_agent_with_its_tools(scaffold):
    from adk.project import load_project_agent

    agent = load_project_agent()
    assert agent is not None and agent.name == "scaffolded"
    assert "hello" in {t.name for t in agent._tools.list_tools()}
    # An explicit, different identity is not the project's agent.
    assert load_project_agent(want="someone-else") is None
    assert load_project_agent(want="scaffolded").name == "scaffolded"


def test_load_project_agent_without_a_project_is_none(tmp_path, monkeypatch):
    from adk.project import load_project_agent

    monkeypatch.chdir(tmp_path)
    assert load_project_agent() is None


def test_load_project_agent_names_a_broken_agent_file(tmp_path, monkeypatch):
    from adk.project import ProjectAgentError, load_project_agent

    (tmp_path / "agent.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(ProjectAgentError, match="boom"):
        load_project_agent()


def test_cmd_run_serves_the_project_agent(scaffold, monkeypatch, capsys):
    from adk import cli
    import adk.server as server

    seen = {}

    def fake_main(agent=None):
        seen["agent"], seen["argv"] = agent, list(sys.argv)

    monkeypatch.setattr(server, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["adk", "run"])
    cli.cmd_run(_run_args())
    assert seen["agent"] is not None and seen["agent"].name == "scaffolded"
    assert "hello" in {t.name for t in seen["agent"]._tools.list_tools()}
    assert "--identity" not in seen["argv"]
    assert "Project agent: scaffolded" in capsys.readouterr().out

    # `-i scaffolded` used to rename an empty agent; it is the project agent too.
    cli.cmd_run(_run_args(identity="scaffolded"))
    assert seen["agent"].name == "scaffolded"

    # A different explicit identity keeps its old meaning.
    cli.cmd_run(_run_args(identity="lyra"))
    assert seen["agent"] is None and seen["argv"][-2:] == ["--identity", "lyra"]


def test_serve_identity_prefers_the_project_over_the_aither_default():
    from adk.server import resolve_serve_identity

    project_agent = types.SimpleNamespace(name="scaffolded")
    with_identity = types.SimpleNamespace(identity="from-config-yaml")
    without = types.SimpleNamespace(identity="")
    assert resolve_serve_identity(None, project_agent, without) == "scaffolded"
    assert resolve_serve_identity(None, None, with_identity) == "from-config-yaml"
    assert resolve_serve_identity("lyra", project_agent, with_identity) == "lyra"
    assert resolve_serve_identity(None, None, without) == "aither"


# ── 3b. an unreachable model backend is one line, not a traceback ───────────

def test_unreachable_backend_raises_a_one_line_error(monkeypatch):
    from adk.llm import LLMBackendUnreachable
    from adk.llm.openai_compat import LOCAL_MODEL_URL, OpenAIProvider

    monkeypatch.setenv("ADK_LLM_MAX_RETRIES", "1")
    url = "http://127.0.0.1:8080/v1/chat/completions"

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_down)) as client:
            await OpenAIProvider._post_with_retry(None, client, url, {})

    with pytest.raises(LLMBackendUnreachable) as err:
        asyncio.run(go())
    message = str(err.value)
    assert isinstance(err.value, httpx.ConnectError)   # existing handlers still match
    assert "\n" not in message
    assert "http://127.0.0.1:8080/v1" in message and LOCAL_MODEL_URL in message
    assert err.value.endpoint == url


def test_local_model_url_is_the_local_backend_preset():
    from adk import cli
    from adk.llm.openai_compat import LOCAL_MODEL_URL

    assert cli._BACKEND_PRESETS["local"]["base_url"] == LOCAL_MODEL_URL


def test_scaffolded_agent_prints_one_line_for_an_unreachable_backend(scaffold):
    import importlib.util

    from adk.llm import LLMBackendUnreachable

    spec = importlib.util.spec_from_file_location("scaffold_agent", str(scaffold / "agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def refuse(*a, **k):
        raise LLMBackendUnreachable("http://127.0.0.1:8080/v1/chat/completions")

    module.agent.chat = refuse
    with pytest.raises(SystemExit) as stop:
        asyncio.run(module.main())
    assert str(stop.value.code).startswith("error: Model backend unreachable")


def test_backend_preflight_line_names_a_dead_loopback_endpoint():
    from adk.server import _backend_preflight_line

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.listen(1)
    try:
        live = types.SimpleNamespace(llm_base_url=f"http://127.0.0.1:{port}/v1")
        assert _backend_preflight_line(live) == ""
    finally:
        probe.close()
    dead = types.SimpleNamespace(llm_base_url=f"http://127.0.0.1:{port}/v1")
    line = _backend_preflight_line(dead)
    assert "ERROR" in line and f"http://127.0.0.1:{port}/v1" in line
    assert _backend_preflight_line(types.SimpleNamespace(llm_base_url="")) == ""
    # Never dials off this machine.
    assert _backend_preflight_line(
        types.SimpleNamespace(llm_base_url="https://example.invalid/v1")) == ""


# ── 4. a huge MCP catalogue is searched, not dumped into the prompt ─────────

class _Manager:
    def __init__(self, tools):
        self._tools, self.failures, self.calls = tools, {}, []

    def connect_all_sync(self):
        return self._tools

    async def call_from_any_loop(self, server, name, arguments):
        self.calls.append((server, name, arguments))
        return f"ran {name}"


def _connected(server, n):
    from adk.mcp_client import ConnectedTool, qualified_name

    return [ConnectedTool(server=server, remote_name=f"tool_{i}",
                          name=qualified_name(server, f"tool_{i}"),
                          description=f"does thing number {i}",
                          parameters={"type": "object", "properties": {}})
            for i in range(n)]


def _register(monkeypatch, tools):
    import adk.mcp_client as mc
    from adk.tools import ToolRegistry

    manager = _Manager(tools)
    monkeypatch.setattr(mc, "load_config", lambda config=None: ({"x": object()}, "test.json"))
    monkeypatch.setattr(mc, "UserMCPManager", lambda specs, timeout=0: manager)
    monkeypatch.delenv("AITHER_MCP_EAGER_MAX", raising=False)
    agent = types.SimpleNamespace(_tools=ToolRegistry())
    count = mc.register_user_mcp_tools(agent)
    return agent, manager, count


def test_large_mcp_catalogue_registers_search_and_call_only(monkeypatch):
    big = _connected("gateway", 500)
    big[7].remote_name, big[7].description = "time_now", "the platform's current time"
    agent, manager, count = _register(monkeypatch, big + _connected("small", 3))

    names = {t.name for t in agent._tools.list_tools()}
    assert count == 5 and names == {
        "mcp__gateway__search_tools", "mcp__gateway__call_tool",
        "mcp__small__tool_0", "mcp__small__tool_1", "mcp__small__tool_2"}

    search = agent._tools.get("mcp__gateway__search_tools").fn
    found = json.loads(asyncio.run(search(query="current time", limit=3)))
    assert found["total"] == 500 and found["results"][0]["name"] == "time_now"

    call = agent._tools.get("mcp__gateway__call_tool").fn
    assert asyncio.run(call(name="time_now", arguments={"tz": "utc"})) == "ran time_now"
    assert manager.calls == [("gateway", "time_now", {"tz": "utc"})]
    missing = json.loads(asyncio.run(call(name="no_such_tool")))
    assert "no tool named" in missing["error"] and len(manager.calls) == 1


def test_small_mcp_server_still_registers_every_tool(monkeypatch):
    agent, _, count = _register(monkeypatch, _connected("small", 40))
    assert count == 40
    assert "mcp__small__search_tools" not in {t.name for t in agent._tools.list_tools()}


def test_other_hosts_are_never_handed_every_stored_credential(monkeypatch):
    """Only this machine and Aitherium hosts may be tried with each credential."""
    from adk import mcp_setup

    assert mcp_setup.may_try_every_credential("http://localhost:8182/mcp")
    assert mcp_setup.may_try_every_credential("https://mcp.aitherium.com/mcp")
    assert not mcp_setup.may_try_every_credential("https://aitherium.com.evil.example/mcp")
    assert not mcp_setup.may_try_every_credential("https://evil.example/mcp")

    sent = []

    def fake_verify(url, token, timeout=20.0):
        sent.append(token)
        return {"ok": False, "status_code": 401, "tool_count": 0, "error": "refused"}

    monkeypatch.setattr(mcp_setup, "verify_gateway_auth", fake_verify)
    mcp_setup.select_working_auth(
        "https://evil.example/mcp", candidates=[("first", "a"), ("second", "b")])
    assert sent == ["first"]
    sent.clear()
    mcp_setup.select_working_auth(
        "http://localhost:8182/mcp", candidates=[("first", "a"), ("second", "b")])
    assert sent == ["first", "second"]
