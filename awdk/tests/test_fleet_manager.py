"""Fleet manager — cross-runtime create/list/status/remove.

Verifies the durable store, the manager's dispatch to per-runtime drivers (local /
managed / cloud-run), and — with a REAL subprocess — that the local driver's PID
tracking + terminate actually work (not just a mocked 'running').
"""

from __future__ import annotations

import sys
import time

import pytest

from adk.fleet_manager import (
    HostedDriver,
    FleetManager,
    FleetMember,
    FleetStore,
    LocalDriver,
    ManagedDriver,
)


# ── store ────────────────────────────────────────────────────────────────

def test_store_roundtrip_and_remove(tmp_path):
    store = FleetStore(path=tmp_path / "fleet.json")
    assert store.all() == []
    m = FleetMember(id="abc", name="scout", runtime="local", status="running", ref="123")
    store.upsert(m)
    got = store.get("abc")
    assert got is not None and got.name == "scout" and got.ref == "123"
    assert [x.id for x in store.all()] == ["abc"]
    assert store.remove("abc") is True
    assert store.get("abc") is None
    assert store.remove("abc") is False  # idempotent


def test_store_survives_corrupt_file(tmp_path):
    p = tmp_path / "fleet.json"
    p.write_text("{ not json", encoding="utf-8")
    store = FleetStore(path=p)
    assert store.all() == []  # tolerates a garbled file instead of crashing


# ── manager dispatch with fake drivers ──────────────────────────────────

def _mgr(tmp_path, drivers):
    return FleetManager(store=FleetStore(path=tmp_path / "fleet.json"), drivers=drivers, now=lambda: 1.0)


def test_create_managed_records_agent_id(tmp_path):
    calls = {}
    def fake_deploy(agent, opts):
        calls["agent"] = agent
        return {"ok": True, "deployed": True, "anthropic_agent_id": "agent_LIVE123", "digest": "d1"}
    mgr = _mgr(tmp_path, {"managed": ManagedDriver(deploy_fn=fake_deploy)})
    m = mgr.create("managed", "twin", pack="weather-eve-import", mcp_url="https://x/mcp")
    assert calls["agent"] == "weather-eve-import"
    assert m.runtime == "managed" and m.status == "running"
    assert m.ref == "agent_LIVE123" and m.meta.get("digest") == "d1"
    # persisted
    assert mgr.get(m.id).ref == "agent_LIVE123"


def test_create_managed_failure_marks_failed(tmp_path):
    mgr = _mgr(tmp_path, {"managed": ManagedDriver(deploy_fn=lambda a, o: {"ok": False, "error": "401"})})
    m = mgr.create("managed", "twin")
    assert m.status == "failed" and "401" in m.error


def test_create_hosted_runs_when_gateway_answers(tmp_path):
    """The old CloudRunDriver 'gracefully degraded' to pending_runtime when the
    gateway lacked its route (it always did). A hosted member is now RUNNING only
    when Genesis reports the instance ready, and FAILED — with the body — otherwise."""
    ok = {"ok": True, "instance": {"id": "inst-abc", "status": "ready",
                                   "endpoint_url": "https://hosted-acme.aitherium.com",
                                   "hostname": "hosted-acme.aitherium.com", "ready_ms": 812,
                                   "brain": "cloud", "loop": "rented", "hands": "rented"}}
    mgr = _mgr(tmp_path, {"hosted": HostedDriver(create_fn=lambda n, o: ok)})
    m = mgr.create("hosted", "hosted")
    assert m.status == "running" and m.ref == "inst-abc"
    assert m.endpoint == "https://hosted-acme.aitherium.com"
    assert m.meta["placement"]["loop"] == "rented" and m.meta["ready_ms"] == 812


def test_create_hosted_gateway_error_is_failed_not_pending(tmp_path):
    mgr = _mgr(tmp_path, {"cloud-run": HostedDriver(
        create_fn=lambda n, o: {"ok": False, "error": "404: {\"detail\":\"Not Found\"}"})})
    m = mgr.create("cloud-run", "hosted")
    assert m.status == "failed" and "404" in m.error
    assert m.status != "pending_runtime"


def test_unknown_runtime_raises(tmp_path):
    mgr = _mgr(tmp_path, {"local": LocalDriver(spawner=lambda n, o: {"pid": 1})})
    with pytest.raises(ValueError):
        mgr.create("quantum", "x")


def test_empty_name_raises(tmp_path):
    mgr = _mgr(tmp_path, {"local": LocalDriver(spawner=lambda n, o: {"pid": 1})})
    with pytest.raises(ValueError):
        mgr.create("local", "")


def test_list_sorted_and_remove(tmp_path):
    mgr = _mgr(tmp_path, {
        "local": LocalDriver(spawner=lambda n, o: {"pid": 999999}),
        "cloud-run": HostedDriver(
            create_fn=lambda n, o: {"ok": True, "instance": {"id": "cr", "status": "ready"}},
            remove_fn=lambda r: True,
        ),
    })
    a = mgr.create("local", "a")
    b = mgr.create("cloud-run", "b")
    ids = [m.id for m in mgr.list_members()]
    assert set(ids) == {a.id, b.id}
    assert mgr.remove(a.id) is True
    assert {m.id for m in mgr.list_members()} == {b.id}
    assert mgr.remove("nope") is False


# ── local driver against a REAL process ──────────────────────────────────

def test_local_driver_real_process_lifecycle(tmp_path):
    """Spawn a real background process, confirm status=running, remove kills it,
    status=stopped — proving the actual PID logic, not a mock."""
    procs = []
    def real_spawner(name, opts):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return {"pid": p.pid, "endpoint": "http://127.0.0.1:8080"}

    mgr = _mgr(tmp_path, {"local": LocalDriver(spawner=real_spawner)})
    m = mgr.create("local", "scout")
    try:
        assert m.status == "running" and int(m.ref) > 0
        assert mgr.refresh(m.id).status == "running"  # the real PID is alive

        assert mgr.remove(m.id) is True
        # Confirm it's gone using the module's cross-platform liveness (raw
        # os.kill(pid,0) is a Windows footgun — it terminates, not probes).
        from adk.fleet_manager import _pid_alive
        deadline = time.time() + 5
        while time.time() < deadline and _pid_alive(int(m.ref)):
            time.sleep(0.2)
        assert _pid_alive(int(m.ref)) is False, "process should be dead after remove()"
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass


def test_local_status_stopped_for_dead_pid(tmp_path):
    mgr = _mgr(tmp_path, {"local": LocalDriver(spawner=lambda n, o: {"pid": 2147480000})})
    m = mgr.create("local", "ghost")  # a PID that (almost certainly) isn't running
    assert mgr.refresh(m.id).status == "stopped"


# ── cloud-run emit with intent posting ───────────────────────────────────

def test_hosted_gateway_down_is_failed_not_pending(monkeypatch):
    """A gateway that cannot be reached is a FAILED create with the transport error
    in `error` — never the old 'pending_runtime' that nothing ever fulfilled."""
    from adk.fleet_manager import _http_instance_create

    monkeypatch.setenv("AITHER_API_URL", "http://127.0.0.1:9")  # nothing listens
    res = _http_instance_create("offline-agent", {})
    assert res.get("ok") is False and res.get("error")
    m = FleetMember(id="x", name="offline-agent", runtime="hosted")
    out = HostedDriver().create(m, {})
    assert out["status"] == "failed" and "pending" not in out["status"]


# ── bidirectional: connect-local ─────────────────────────────────────────

def test_connect_local_agent_posts_mcp_endpoint():
    """connect_local_agent POSTs the agent's MCP URL to the gateway endpoint."""
    calls = []

    def fake_poster(name, mcp_url):
        calls.append({"name": name, "mcp_url": mcp_url})
        return {
            "ok": True,
            "endpoint": {"name": name, "url": mcp_url, "auth_vault_key": "vault::local/auth"},
        }

    from adk.fleet_manager import connect_local_agent

    result = connect_local_agent("my-agent", "http://localhost:8080/mcp", poster=fake_poster)
    assert result.get("ok") is True
    assert calls[0]["name"] == "my-agent"
    assert calls[0]["mcp_url"] == "http://localhost:8080/mcp"
    assert result.get("endpoint", {}).get("name") == "my-agent"


def test_connect_local_agent_empty_url_fails():
    """connect_local_agent rejects an empty MCP URL."""

    def fake_poster(name, mcp_url):
        return {"ok": False, "error": "mcp_url is required"}

    from adk.fleet_manager import connect_local_agent

    result = connect_local_agent("my-agent", "", poster=fake_poster)
    assert result.get("ok") is False
    assert "mcp_url" in result.get("error", "").lower()


def test_connect_local_agent_degrades_on_network_error():
    """When the gateway is down, connect_local_agent returns an error dict,
    never crashes."""

    def failing_poster(name, mcp_url):
        raise ConnectionError("gateway unreachable")

    from adk.fleet_manager import connect_local_agent

    result = connect_local_agent("my-agent", "http://localhost:8080/mcp", poster=failing_poster)
    assert result.get("ok") is False
    assert "gateway" in result.get("error", "").lower() or "error" in result
