"""Twin self-service gaps measured 2026-11-12 on twins.aitherium.com and in the kit.

* /docs and /openapi.json on the bearer-gated public twin server listed all 180
  routes (33 /admin, /cli/execute, /secrets) to anyone.
* /health said ``tools: {mode: builtin-only, registered: 0, attach_attempts: 0,
  last_error: ""}``: no gateway credential was configured and nothing said so; the
  online attach never wrote the counters /health reads, attached or not.
* `adk agent managed status` answered ``401 Missing bearer token`` for a signed-in
  user, because the binding verbs read AITHER_API_KEY only.
* The kit had no command to deploy the project's agent as a self-hosted twin.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from adk import agent_binding_client, twin_client
from adk.server import gateway_attach_report, schema_routes_public

SCHEMA_ROUTES = ("/docs", "/redoc", "/openapi.json")


# ── schema routes on a bearer-gated server ───────────────────────────────────
def _app(api_key: str, extra_env: dict | None = None):
    env = {"AITHER_SERVER_API_KEY": api_key, "AITHER_PUBLIC_DOCS": "", **(extra_env or {})}
    with patch.dict(os.environ, env, clear=False):
        from adk.config import Config
        from adk.server import create_app

        config = Config()
        config.gateway_url = ""
        config.aither_api_key = ""
        agent = MagicMock()
        agent.name = "test"
        agent.llm = MagicMock()
        agent.llm.provider_name = "test"
        agent._identity = MagicMock()
        agent._identity.name = "test"
        agent._identity.description = "Test"
        agent._identity.skills = []
        agent._tools = MagicMock()
        agent._tools.list_tools = MagicMock(return_value=[])
        agent._safety = None
        return create_app(agent=agent, identity="test", config=config)


@pytest.mark.parametrize("path", SCHEMA_ROUTES)
def test_schema_routes_need_the_bearer_on_a_gated_server(path):
    client = TestClient(_app("secret123"))
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer secret123"}).status_code != 401


def test_health_and_landing_stay_open_on_a_gated_server():
    client = TestClient(_app("secret123"))
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ("/docs", "/redoc"))
def test_schema_routes_stay_open_without_a_server_key(path):
    # The loopback daemon: no server key, every route already open to the box's user.
    assert TestClient(_app("")).get(path).status_code == 200


def test_public_docs_is_an_explicit_opt_in():
    client = TestClient(_app("secret123", {"AITHER_PUBLIC_DOCS": "1"}))
    assert client.get("/docs").status_code == 200


def test_schema_routes_public_rule(monkeypatch):
    monkeypatch.delenv("AITHER_PUBLIC_DOCS", raising=False)
    assert schema_routes_public("") is True
    assert schema_routes_public("key") is False
    monkeypatch.setenv("AITHER_PUBLIC_DOCS", "yes")
    assert schema_routes_public("key") is True


# ── /health says why there are no platform tools ─────────────────────────────
def test_no_credential_is_reported_not_silent():
    state: dict = {}
    assert gateway_attach_report(state, key_present=False) is False
    assert "AITHER_API_KEY" in state["mcp_last_error"]
    assert state["gateway_mcp_connected"] is False
    assert state.get("mcp_attach_attempts", 0) == 0  # nothing was dialled


def test_a_working_attach_writes_the_counters_health_reads():
    state: dict = {"mcp_last_error": "old"}
    assert gateway_attach_report(state, connected=True, catalogue=1227, registered=18) is True
    assert (state["mcp_tools_registered"], state["mcp_tools_catalogue"]) == (18, 1227)
    assert state["gateway_mcp_connected"] is True
    assert state["mcp_last_error"] == ""
    assert state["mcp_attach_attempts"] == 1


@pytest.mark.parametrize("kwargs,needle", [
    ({"connected": False}, "refused"),
    ({"connected": True, "catalogue": 0, "registered": 2}, "0 tools"),
    ({"connected": True, "catalogue": 50, "registered": 0}, "registered"),
])
def test_connected_but_inert_is_not_attached(kwargs, needle):
    state: dict = {}
    assert gateway_attach_report(state, **kwargs) is False
    assert state["gateway_mcp_connected"] is False
    assert state["mcp_tools_registered"] == 0 and state["mcp_tools_catalogue"] == 0
    assert needle in state["mcp_last_error"]


def test_the_online_attach_reports_every_outcome():
    """The startup attach runs inside the app lifespan (which also starts relays and
    heartbeats, so it is not run here); assert its wiring from the source instead:
    every exit of ``_connect_gateway_mcp`` goes through ``gateway_attach_report``."""
    import ast
    import inspect

    import adk.server as server

    tree = ast.parse(inspect.getsource(server))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_connect_gateway_mcp")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and getattr(c.func, "id", "") == "gateway_attach_report"]
    kwargs = [{k.arg: getattr(k.value, "value", None) for k in c.keywords} for c in calls]
    assert {"key_present": False} in kwargs  # the no-credential exit
    assert sum(1 for k in kwargs if k.get("connected") is True) >= 2  # attached + inert
    assert sum(1 for k in kwargs if k.get("connected") is False) >= 2  # refused + raised
    # ...and nothing in it writes the verdict flag behind the report's back.
    direct = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              and "gateway_mcp_connected" in ast.dump(n.targets[0])
              and getattr(n.value, "value", None) is False]
    assert direct == []


# ── the binding verbs send the login session ─────────────────────────────────
class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def kit(monkeypatch, tmp_path):
    """A signed-in kit with NO AITHER_API_KEY: the session bearer is the credential."""
    home = tmp_path / "aither-home"
    home.mkdir()
    (home / "session-bearer").write_text("session-token-abc\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    for var in ("AITHER_API_KEY", "AITHER_MCP_KEY", "AITHER_SERVER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_API_URL", "https://api.example")
    seen: list[dict] = []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        return _Resp()

    monkeypatch.setattr(httpx, "request", fake_request)
    return {"home": home, "seen": seen, "tmp": tmp_path}


def _main(monkeypatch, argv) -> int:
    monkeypatch.setattr(sys, "argv", ["adk", *argv])
    from adk import cli

    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def test_managed_status_sends_the_login_session(kit, monkeypatch):
    assert _main(monkeypatch, ["agent", "managed", "status"]) == 0
    assert kit["seen"][0]["url"] == "https://api.example/v1/agent/managed/status"
    assert kit["seen"][0]["headers"].get("Authorization") == "Bearer session-token-abc"


def test_an_explicit_key_still_wins(kit, monkeypatch):
    monkeypatch.setenv("AITHER_API_KEY", "explicit-key")
    assert agent_binding_client.bearer()[0] == "explicit-key"
    assert "explicit-key" not in agent_binding_client.bearer()[1]


def test_signed_out_says_how_to_sign_in(kit, monkeypatch, capsys):
    (kit["home"] / "session-bearer").unlink()
    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp(401, {"error": "auth"}))
    assert _main(monkeypatch, ["agent", "managed", "status"]) == 1
    assert "adk login" in capsys.readouterr().err


# ── adk agent twin deploy ────────────────────────────────────────────────────
def _project(tmp_path, identity="twin-probe", model=""):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "agent.py").write_text("# agent\n", encoding="utf-8")
    (proj / "config.yaml").write_text(
        f"identity: {identity}\nport: 8081\n" + (f"model: {model}\n" if model else ""),
        encoding="utf-8")
    return proj


URL = "https://my-agent.example.com"


def test_dry_run_shows_the_plan_and_sends_nothing(kit, monkeypatch, capsys):
    proj = _project(kit["tmp"], model="bonsai2-27b")
    monkeypatch.setenv("AITHER_SERVER_API_KEY", "callback-secret-xyz")
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--project", str(proj),
                               "--invoke-url", URL, "--dry-run", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert kit["seen"] == []  # nothing left the machine
    plan = json.loads(out)
    assert plan["dry_run"] is True and plan["ready"] is True
    assert plan["request"]["method"] == "POST"
    assert plan["request"]["url"] == "https://api.example/v1/agent/agent-endpoints"
    assert plan["request"]["body"] == {
        "name": "twin-probe", "invoke_url": URL, "provider": "self_hosted",
        "reach": "tunnel", "agent_type": "adk-agent", "bind": True,
        "model": "bonsai2-27b", "token": "<redacted>"}
    assert plan["cost"]["this_command_usd"] == 0
    assert plan["cost"]["provider_agents_created"] == 0
    assert "callback-secret-xyz" not in out


def test_dry_run_of_an_unready_project_exits_1_and_names_what_is_missing(kit, monkeypatch,
                                                                         capsys):
    empty = kit["tmp"] / "empty"
    empty.mkdir()
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--project", str(empty), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1 and kit["seen"] == []
    assert "no agent.py" in out and "--invoke-url" in out


def test_deploy_posts_the_planned_request_with_the_session(kit, monkeypatch):
    proj = _project(kit["tmp"])
    monkeypatch.setenv("AITHER_SERVER_API_KEY", "callback-secret-xyz")
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--project", str(proj),
                               "--invoke-url", URL])
    assert code == 0 and len(kit["seen"]) == 1
    sent = kit["seen"][0]
    assert (sent["method"], sent["url"]) == ("POST", "https://api.example/v1/agent/agent-endpoints")
    assert sent["headers"]["Authorization"] == "Bearer session-token-abc"
    assert sent["json"]["provider"] == "self_hosted" and sent["json"]["bind"] is True
    assert sent["json"]["token"] == "callback-secret-xyz"  # the real bearer goes on the wire
    # Never the hosted-agent door: that one creates an agent at a model provider.
    assert "/managed/deploy" not in sent["url"]


@pytest.mark.parametrize("bad_url", ["", "http://my-agent.example.com", "https://localhost:8080",
                                     "https://192.168.1.5"])
def test_deploy_refuses_to_send_an_unready_plan(kit, monkeypatch, bad_url):
    proj = _project(kit["tmp"])
    argv = ["agent", "twin", "deploy", "--project", str(proj)]
    if bad_url:
        argv += ["--invoke-url", bad_url]
    assert _main(monkeypatch, argv) == 1
    assert kit["seen"] == []


def test_signed_out_is_not_ready(kit):
    (kit["home"] / "session-bearer").unlink()
    proj = _project(kit["tmp"])
    args = MagicMock(project=str(proj), invoke_url=URL, reach="tunnel", model="",
                     token_env="AITHER_SERVER_API_KEY")
    args.name = ""
    plan = twin_client.build_plan(args, base="https://api.example",
                                  credential_source=agent_binding_client.bearer()[1])
    assert plan["ready"] is False
    assert any(c["check"] == "sign-in" and not c["ok"] for c in plan["checks"])


def test_twin_list_reads_the_endpoints(kit, monkeypatch):
    assert _main(monkeypatch, ["agent", "twin", "list"]) == 0
    assert (kit["seen"][0]["method"], kit["seen"][0]["url"]) == (
        "GET", "https://api.example/v1/agent/agent-endpoints")


# ── twin plan (owner ruling 2026-11-12): both doors from the kit ─────────────
def test_self_host_plan_says_no_paid_plan_is_needed(kit):
    proj = _project(kit["tmp"])
    args = MagicMock(project=str(proj), invoke_url=URL, reach="tunnel", model="",
                     token_env="AITHER_SERVER_API_KEY")
    args.name = ""
    plan = twin_client.build_plan(args, base="https://api.example", credential_source="x")
    assert "no paid plan needed" in plan["cost"]["plan"]
    assert "Starter" not in json.dumps(plan)


def test_hosted_dry_run_shows_the_platform_door_and_sends_nothing(kit, monkeypatch, capsys):
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--hosted", "--dry-run", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and kit["seen"] == []
    assert plan["request"] == {"method": "POST",
                               "url": "https://api.example/v1/agent/managed/deploy",
                               "body": {"host": "platform"}}
    assert plan["cost"]["provider_agents_created"] == 0
    assert "daily turn limit" in plan["cost"]["plan"]


def test_hosted_deploy_asks_for_the_platform_host_only(kit, monkeypatch):
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--hosted"]) == 0
    sent = kit["seen"][0]
    assert sent["url"].endswith("/v1/agent/managed/deploy")
    assert sent["json"] == {"host": "platform"}   # never the Anthropic host


@pytest.mark.parametrize("argv,apply", [(["agent", "twin", "rebind"], False),
                                        (["agent", "twin", "rebind", "--apply"], True)])
def test_rebind_is_a_dry_run_unless_apply(kit, monkeypatch, argv, apply):
    assert _main(monkeypatch, argv) == 0
    sent = kit["seen"][0]
    assert sent["url"] == "https://api.example/v1/agent/managed/twins/rebind-tools"
    assert sent["json"] == {"apply": apply}


# ── the daemon enforces a turn's gateway tool whitelist ──────────────────────
def test_turn_whitelist_normalisation_can_only_narrow():
    from adk.server import turn_gateway_allow

    assert turn_gateway_allow(["Web_Search", " fetch_webpage "]) == frozenset(
        {"web_search", "fetch_webpage"})
    for nothing in (None, [], "", "web_search", {"a": 1}, [""], 5):
        assert turn_gateway_allow(nothing) is None


def test_a_turn_whitelist_denies_every_other_gateway_tool():
    import contextvars

    from adk import server

    def inside():
        server._TURN_GATEWAY_ALLOW.set(frozenset({"web_search"}))
        return (server.gateway_tool_denied("web_search"),
                server.gateway_tool_denied("WEB_SEARCH"),
                server.gateway_tool_denied("get_secret"),
                server.gateway_tool_denied("fs_write_file"))

    assert contextvars.copy_context().run(inside) == (False, False, True, True)
    # Outside that turn nothing is restricted: the whitelist belongs to the turn.
    assert server._TURN_GATEWAY_ALLOW.get() is None
    assert server.gateway_tool_denied("get_secret") is False


def test_the_whitelist_and_the_host_deny_list_both_apply(monkeypatch):
    import contextvars

    from adk import server

    monkeypatch.setenv("ADK_GATEWAY_TOOL_DENY", "web_*")

    def inside():
        server._TURN_GATEWAY_ALLOW.set(frozenset({"web_search", "notes_list"}))
        return server.gateway_tool_denied("web_search"), server.gateway_tool_denied("notes_list")

    assert contextvars.copy_context().run(inside) == (True, False)


def test_search_call_and_eager_tools_all_honour_the_turn_whitelist():
    import asyncio
    import contextvars

    from adk import server

    class _Tools:
        def __init__(self):
            self.fns = {}

        def register(self, fn, name="", **_):
            self.fns[name] = fn

    class _Client:
        def __init__(self):
            self.called = []

        async def call_tool(self, name, args):
            self.called.append(name)
            return {"success": True, "text": f"ran {name}"}

    agent = MagicMock()
    agent.tool_grants = None
    agent._tools = _Tools()
    client = _Client()
    catalogue = [{"name": "web_search", "description": "search"},
                 {"name": "get_secret", "description": "read a secret"}]
    server.register_gateway_tools_on(agent, catalogue_getter=lambda: catalogue,
                                     client_getter=lambda: client, max_tools=8)
    assert {"web_search", "get_secret", "call_tool"} <= set(agent._tools.fns)

    async def turn():
        server._TURN_GATEWAY_ALLOW.set(frozenset({"web_search"}))
        eager_ok = await agent._tools.fns["web_search"]()
        eager_no = await agent._tools.fns["get_secret"]()
        by_name = json.loads(await agent._tools.fns["call_tool"](name="get_secret"))
        return eager_ok, eager_no, by_name

    eager_ok, eager_no, by_name = contextvars.copy_context().run(lambda: asyncio.run(turn()))
    assert eager_ok == "ran web_search"
    assert "not granted" in eager_no
    assert by_name["error"] == "denied_by_grant"
    assert client.called == ["web_search"]       # the denied tool never reached the gateway


def test_stream_passes_the_whitelist_into_the_turn():
    import ast
    import inspect

    from adk import server

    src = inspect.getsource(server)
    assert 'turn_gateway_allow(body.get("allowed_gateway_tools"))' in src
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_aitheros_stream")
    sets = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "set"
            and getattr(n.func.value, "id", "") == "_TURN_GATEWAY_ALLOW"]
    assert len(sets) == 1 and getattr(sets[0].args[0], "id", "") == "gateway_allow"


# ── dedicated compute: the kit only ever quotes ──────────────────────────────
QUOTE = {"rents_anything": False,
         "quote": {"quoted": True, "usd_per_hour": 0.35, "model": "bonsai2-27b",
                   "min_vram_gb": 24, "price": "about $0.35 an hour at today's cheapest "
                   "offer; varies by model and GPU",
                   "billing": "your own GPU-provider account; you pay the provider directly"},
         "steps": ["Connect your own vast.ai account.", "Rent a GPU there."],
         "note": "Today the GPU serves a twin whose agent server you run."}


@pytest.mark.parametrize("argv", [
    ["agent", "twin", "deploy", "--dedicated", "--dry-run"],
    ["agent", "twin", "deploy", "--dedicated"],          # still read-only without --dry-run
])
def test_dedicated_only_reads_the_quote_and_never_rents(kit, monkeypatch, capsys, argv):
    seen = kit["seen"]

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        return _Resp(200, QUOTE)

    monkeypatch.setattr(httpx, "request", fake_request)
    assert _main(monkeypatch, argv) == 0
    out = capsys.readouterr().out
    assert [(c["method"], c["url"]) for c in seen] == [
        ("GET", "https://api.example/v1/agent/managed/twins/dedicated-quote")]
    assert not any("/gpu/" in c["url"] or c["method"] != "GET" for c in seen)
    assert "about $0.35 an hour" in out and "nothing was rented" in out
    assert "you pay the provider directly" in out


def test_dedicated_without_a_live_quote_prints_words_not_a_number(kit, monkeypatch, capsys):
    no_quote = {**QUOTE, "quote": {**QUOTE["quote"], "quoted": False,
                                   "price": "about the cost of the rental; varies by model and GPU",
                                   "no_live_quote_because": "no provider account connected"}}
    no_quote["quote"].pop("usd_per_hour")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: _Resp(200, no_quote))
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--dedicated", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "about the cost of the rental; varies by model and GPU" in out
    assert "no provider account connected" in out and "$" not in out


# ── the matrix and the Anthropic door from the kit ───────────────────────────
DOORS = {"rule": "Your first hosted twin is free.",
         "doors": [{"id": "shared", "title": "Aitherium's shared compute", "status": "works",
                    "what": "Hosted by Aitherium.", "cost": "First twin free.",
                    "limit": "25 turns a day per twin.",
                    "command": "adk agent twin deploy --hosted", "status_note": ""},
                   {"id": "own_hardware", "title": "Your own hardware", "status": "works",
                    "what": "Your machine.", "cost": "Free.", "limit": "No Aitherium limit.",
                    "command": "adk agent twin deploy", "status_note": "",
                    "parts": [{"part": "A shared room", "status": "not built",
                               "command": "No single command."}]}]}


def test_doors_prints_what_the_control_plane_serves_and_keeps_no_copy(kit, monkeypatch, capsys):
    seen = kit["seen"]

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        seen.append({"method": method, "url": url})
        return _Resp(200, DOORS)

    monkeypatch.setattr(httpx, "request", fake_request)
    assert _main(monkeypatch, ["agent", "twin", "doors"]) == 0
    out = capsys.readouterr().out
    assert seen == [{"method": "GET",
                     "url": "https://api.example/v1/agent/managed/twins/doors"}]
    assert "1. Aitherium's shared compute  [works]" in out
    assert "2. Your own hardware  [works]" in out
    assert "- A shared room  [not built]: No single command." in out
    # The kit has no table of its own to drift from the gate's.
    import inspect

    src = inspect.getsource(twin_client)
    assert "shared compute" not in src and "DOORS = " not in src


def test_anthropic_dry_run_names_who_pays_and_sends_nothing(kit, monkeypatch, capsys):
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--anthropic", "--dry-run", "--json"])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and kit["seen"] == []
    assert plan["request"]["body"] == {"host": "anthropic"}
    assert plan["cost"]["provider_agents_created"] == 1
    assert "billed to you by Anthropic" in plan["cost"]["model_spend"]
    assert "not counted" in plan["cost"]["plan"]
