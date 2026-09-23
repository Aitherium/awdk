"""adk fleet — managed agent_id semantics, the gateway base, and the connect-local bearer.

1. ``agent_id`` in ``POST /v1/agent/managed/deploy`` is a BINDING id. Genesis
   (``routers/agent_binding.py`` ``_resolve_agent_id``) returns an explicit id as
   given and 404s when no binding has it, so the driver must never send a pack or
   fleet name as one. A --pack is applied onto the binding via apply-pack.
2. The default gateway base was ``http://localhost:8001`` — Genesis publishes no
   host port and speaks TLS. The default is the portal's ``/api/genesis`` proxy.
3. The adk MCP server always enforces a bearer; ``connect-local`` registered the
   endpoint with none, so the round trip listed 0 tools (401).
"""

from __future__ import annotations

import httpx
import pytest

from adk import fleet_manager as fm
from adk.fleet_manager import FleetManager, FleetStore, ManagedDriver


class _Resp:
    def __init__(self, status: int = 200, data: dict | None = None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = str(self._data)

    def json(self):
        return self._data


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("AITHER_API_URL", "AITHER_GATEWAY_URL", "AITHER_PORTAL_URL",
              "AITHER_ELYSIUM_URL", "AITHER_API_KEY", "AITHER_MCP_KEY",
              "AITHER_SERVER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# ── 1. managed agent_id semantics ────────────────────────────────────────

def test_managed_pack_is_applied_not_sent_as_agent_id(tmp_path):
    calls: list = []

    def apply_fn(pack, agent_id):
        calls.append(("apply", pack, agent_id))
        return {"ok": True}

    def deploy_fn(agent_id, opts):
        calls.append(("deploy", agent_id))
        return {"ok": True, "deployed": True, "anthropic_agent_id": "agt_1",
                "binding": {"agent_id": "acme-default"}}

    removed: list = []
    drv = ManagedDriver(deploy_fn=deploy_fn, apply_fn=apply_fn,
                        remove_fn=lambda a, meta: removed.append(a) or True)
    mgr = FleetManager(store=FleetStore(path=tmp_path / "f.json"),
                       drivers={"managed": drv}, now=lambda: 1.0)
    m = mgr.create("managed", "twin", pack="weather-pack")
    assert m.status == "running", m.error
    # The pack is applied onto the primary binding; the deploy names NO binding id.
    assert calls == [("apply", "weather-pack", ""), ("deploy", "")]
    assert m.meta["agent_id"] == "acme-default"
    assert mgr.remove(m.id) is True
    assert removed == ["acme-default"]  # the resolved binding, never the pack name


def test_managed_explicit_agent_id_is_forwarded(tmp_path):
    seen = {}
    drv = ManagedDriver(deploy_fn=lambda a, o: seen.setdefault("agent", a) and
                        {"ok": True, "deployed": True},
                        apply_fn=lambda p, a: {"ok": True})
    mgr = FleetManager(store=FleetStore(path=tmp_path / "f.json"),
                       drivers={"managed": drv}, now=lambda: 1.0)
    mgr.create("managed", "twin", agent_id="bound-7")
    assert seen["agent"] == "bound-7"


def test_managed_apply_failure_marks_failed_without_deploy(tmp_path):
    deployed = []
    drv = ManagedDriver(deploy_fn=lambda a, o: deployed.append(a) or {"ok": True},
                        apply_fn=lambda p, a: {"ok": False, "error": "404: unknown pack"})
    mgr = FleetManager(store=FleetStore(path=tmp_path / "f.json"),
                       drivers={"managed": drv}, now=lambda: 1.0)
    m = mgr.create("managed", "twin", pack="nope")
    assert m.status == "failed" and "unknown pack" in m.error
    assert deployed == []


def test_http_deploy_omits_agent_id_when_none_given(clean_env):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json)
        return _Resp(200, {"ok": True, "deployed": True})

    clean_env.setenv("AITHER_API_URL", "https://api.example")
    clean_env.setattr(httpx, "post", fake_post)
    fm._http_managed_deploy("", {"model": "m"})
    assert seen["json"] == {"model": "m"}


def test_http_apply_pack_route_and_body(clean_env):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json)
        return _Resp(200, {"ok": True, "status": "applied"})

    clean_env.setenv("AITHER_API_URL", "https://api.example")
    clean_env.setattr(httpx, "post", fake_post)
    assert fm._http_apply_pack("weather-pack", "")["ok"] is True
    assert seen == {"url": "https://api.example/v1/agent/binding/apply-pack",
                    "json": {"listing_id": "weather-pack"}}


# ── 2. gateway base default ──────────────────────────────────────────────

def test_gateway_base_defaults_to_portal_genesis_proxy(clean_env):
    assert fm._gateway_base() == "https://api.aitherium.com/api/genesis"


def test_gateway_base_honours_portal_url(clean_env):
    clean_env.setenv("AITHER_PORTAL_URL", "https://portal.example/")
    assert fm._gateway_base() == "https://portal.example/api/genesis"


def test_gateway_base_explicit_api_url_wins(clean_env):
    clean_env.setenv("AITHER_PORTAL_URL", "https://portal.example")
    clean_env.setenv("AITHER_API_URL", "https://genesis.direct/")
    assert fm._gateway_base() == "https://genesis.direct"


def test_cli_control_plane_is_the_shared_answer(clean_env):
    from adk.cli import _control_plane

    clean_env.setenv("AITHER_PORTAL_URL", "https://portal.example")
    assert _control_plane() == "https://portal.example"


# ── 3. connect-local bearer round trip ───────────────────────────────────

def test_connect_local_round_trip_lists_tools(clean_env):
    """Register with connect-local, then the self-hosted loop (which the platform
    relays only ``{name, url}``) reaches the REAL adk MCP server and lists tools."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from adk.mcp_endpoint_tools import register_mcp_endpoint_tools
    from adk.mcp_server import MCPServer
    from adk.tools import ToolRegistry

    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    reg = ToolRegistry()

    def ping(text: str = "") -> str:
        """Echo."""
        return text

    reg.register(ping)
    app = fastapi.FastAPI()
    MCPServer(tool_registry=reg).mount(app)
    client = TestClient(app)

    registered = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/v1/agent/mcp-endpoints"):
            registered.update(json)
            return _Resp(200, {"ok": True, "secret_stored": True,
                               "endpoint": {"name": json["name"], "url": json["url"]}})
        # Anything else is the relayed MCP endpoint -> the real MCP app.
        r = client.post("/mcp", json=json, headers=headers)
        return _Resp(r.status_code, r.json())

    clean_env.setattr(httpx, "post", fake_post)
    res = fm.connect_local_agent("box", "http://127.0.0.1:8080/mcp")
    assert res["ok"] is True
    assert registered.get("token") == "local-mcp-key"  # bearer reaches Genesis (vault)

    # The platform's self_hosted_mcp_servers relays {name, url} only.
    agent = type("A", (), {})()
    agent.name = "t"
    agent._tools = type("R", (), {"_tools": {}})()
    n = register_mcp_endpoint_tools(agent, [{"name": "box", "url": "http://127.0.0.1:8080/mcp"}])
    assert n == 1 and "box__ping" in agent._tools._tools


def test_connect_local_explicit_token_and_no_token_warning(clean_env):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp(200, {"ok": True, "endpoint": {}})

    clean_env.setenv("AITHER_API_URL", "https://api.example")
    clean_env.setattr(httpx, "post", fake_post)
    fm.connect_local_agent("a", "https://x/mcp", token="t0k")
    assert sent[-1]["token"] == "t0k"
    res = fm.connect_local_agent("a", "https://x/mcp")
    assert "token" not in sent[-1] and "warning" in res


def test_loopback_fallback_never_applies_to_remote_urls(clean_env):
    from adk.mcp_endpoint_tools import _headers_for

    clean_env.delenv("AITHER_PORT", raising=False)
    clean_env.delenv("AITHER_DAEMON_PORT", raising=False)
    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    assert "Authorization" not in _headers_for({"url": "https://third-party.example/mcp"})
    local = "http://localhost:8080/mcp"
    assert _headers_for({"url": local})["Authorization"] == "Bearer local-mcp-key"
    assert _headers_for({"url": local, "token": "x"})["Authorization"] == "Bearer x"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:6379/mcp",       # another loopback service's port
    "http://localhost/mcp",            # no port -> 80, not the adk server
    "http://127.0.0.1:8080/admin",     # the adk port, but not the MCP mount
    "http://[::1]:5432/mcp",
])
def test_local_bearer_only_reaches_the_own_mcp_server(clean_env, url):
    """A /stream body can name any loopback URL; the local bearer goes only to
    this box's own adk MCP server (its port + /mcp), never to another port."""
    from adk.mcp_endpoint_tools import _headers_for

    clean_env.delenv("AITHER_PORT", raising=False)
    clean_env.delenv("AITHER_DAEMON_PORT", raising=False)
    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    assert "Authorization" not in _headers_for({"url": url})


def test_local_bearer_follows_the_configured_server_port(clean_env):
    from adk.mcp_endpoint_tools import _headers_for

    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    clean_env.setenv("AITHER_PORT", "8123")
    clean_env.delenv("AITHER_DAEMON_PORT", raising=False)
    assert _headers_for({"url": "http://127.0.0.1:8123/mcp/"})["Authorization"] ==         "Bearer local-mcp-key"
    assert "Authorization" not in _headers_for({"url": "http://127.0.0.1:8080/mcp"})


# ── 4. remove on a member created before meta carried agent_id ───────────

def test_remove_legacy_member_never_targets_the_primary_binding(tmp_path):
    """A member stored before this change has no meta.agent_id. Sending no id
    would de-migrate the tenant's PRIMARY binding; keep the old target."""
    from adk.fleet_manager import FleetMember

    removed: list = []
    drv = ManagedDriver(deploy_fn=lambda a, o: {"ok": True},
                        remove_fn=lambda a, meta: removed.append(a) or True,
                        apply_fn=lambda p, a: {"ok": True})
    legacy = FleetMember(id="m1", name="twin", runtime="managed", source="weather-pack",
                         meta={"digest": "d"})
    assert drv.remove(legacy) is True
    assert removed == ["weather-pack"]
    no_source = FleetMember(id="m2", name="twin", runtime="managed", meta={})
    drv.remove(no_source)
    assert removed[-1] == "twin"
    # A member created now records the resolved binding (empty = primary).
    current = FleetMember(id="m3", name="twin", runtime="managed", source="weather-pack",
                          meta={"agent_id": ""})
    drv.remove(current)
    assert removed[-1] == ""


# ── 5. loopback is an IP literal or "localhost", never a string prefix ─────

@pytest.mark.parametrize("url", [
    "http://127.evil.example:8080/mcp",   # public DNS name that merely STARTS with 127.
    "http://127.0.0.1.evil.example:8080/mcp",
    "https://localhost.evil.example:8080/mcp",
    "http://[::ffff:8.8.8.8]:8080/mcp",
])
def test_loopback_lookalike_hostnames_never_get_the_local_bearer(clean_env, url):
    from adk.mcp_endpoint_tools import _headers_for, _is_loopback_url

    clean_env.delenv("AITHER_PORT", raising=False)
    clean_env.delenv("AITHER_DAEMON_PORT", raising=False)
    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    assert _is_loopback_url(url) is False
    assert "Authorization" not in _headers_for({"url": url})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/mcp", "http://127.8.9.10:8080/mcp", "http://[::1]:9001/mcp",
    "http://LOCALHOST:8080/mcp",
])
def test_real_loopback_literals_still_get_the_local_bearer(clean_env, url):
    from adk.mcp_endpoint_tools import _headers_for

    clean_env.delenv("AITHER_PORT", raising=False)
    clean_env.delenv("AITHER_DAEMON_PORT", raising=False)
    clean_env.setenv("AITHER_MCP_KEY", "local-mcp-key")
    assert _headers_for({"url": url})["Authorization"] == "Bearer local-mcp-key"


# ── 6. a FAILED member was never deployed: removal touches nothing remote ──

def _removal_mgr(tmp_path, deploy_fn, apply_fn=lambda p, a: {"ok": True}):
    removed: list = []
    drv = ManagedDriver(deploy_fn=deploy_fn, apply_fn=apply_fn,
                        remove_fn=lambda a, meta: removed.append(a) or True)
    mgr = FleetManager(store=FleetStore(path=tmp_path / "f.json"),
                       drivers={"managed": drv}, now=lambda: 1.0)
    return mgr, removed


def test_remove_failed_deploy_member_touches_no_binding(tmp_path):
    # --agent-id names a WORKING binding; the deploy fails; removing the failed
    # record must not de-migrate that binding (nor the primary).
    mgr, removed = _removal_mgr(tmp_path, lambda a, o: {"ok": False, "error": "500"})
    m = mgr.create("managed", "twin", agent_id="bound-7")
    assert m.status == "failed"
    assert mgr.remove(m.id) is True
    assert removed == []


def test_remove_apply_failed_member_touches_no_binding(tmp_path):
    mgr, removed = _removal_mgr(tmp_path, lambda a, o: {"ok": True, "deployed": True},
                                apply_fn=lambda p, a: {"ok": False, "error": "404"})
    m = mgr.create("managed", "twin", pack="nope")
    assert m.status == "failed"
    assert mgr.remove(m.id) is True
    assert removed == []
