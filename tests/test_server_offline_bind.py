"""AITHER_OFFLINE binds loopback; /health reports the air-gap state (awdk-on-awnix).

The AFRL claim is zero open ports. ``adk.server`` defaulted to 0.0.0.0, which would
falsify it on every appliance. These tests fail if the offline default ever binds
anything but 127.0.0.1, or if /health stops exposing the ``air_gap`` block that
``awnix awdk health`` reads. No test opens a non-loopback socket.
"""

from __future__ import annotations

import os
import socket
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from adk import server as srv
from adk.agent import AitherAgent
from adk.compliance import air_gap, egress_guard

_ENV = ("AITHER_OFFLINE", "AITHER_HOST", "AITHER_DATA_DIR", "AITHER_AIR_GAP",
        "AITHER_AIR_GAP_CONFIG", "AITHER_CLOUD_MODE", "AITHER_LLM_OFFLINE_MODE",
        "AITHER_PHONEHOME_DISABLED")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AITHER_AIR_GAP_CONFIG", str(tmp_path / "air_gap.yaml"))
    prev = air_gap.set_enforcer(None)
    was = dict(egress_guard._INSTALLED)
    yield tmp_path
    if not any(was.values()):
        egress_guard.uninstall_egress_guard()
    air_gap.set_enforcer(prev)


@pytest.mark.parametrize("cli,cfg_host,env,want", [
    (None, "0.0.0.0", {"AITHER_OFFLINE": "1"}, "127.0.0.1"),
    (None, "0.0.0.0", {"AITHER_OFFLINE": "true"}, "127.0.0.1"),
    (None, "0.0.0.0", {"AITHER_OFFLINE": "1", "AITHER_HOST": "0.0.0.0"}, "127.0.0.1"),
    ("0.0.0.0", "0.0.0.0", {"AITHER_OFFLINE": "1"}, "0.0.0.0"),  # --host always wins
    (None, "0.0.0.0", {}, "0.0.0.0"),  # online behaviour unchanged
    (None, "10.1.2.3", {"AITHER_OFFLINE": "0"}, "10.1.2.3"),
])
def test_resolve_bind_host(cli, cfg_host, env, want):
    assert srv.resolve_bind_host(cli, cfg_host, env) == want


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_main_offline_binds_loopback(isolated, monkeypatch):
    """main() end to end up to uvicorn.run: the host it would bind is 127.0.0.1."""
    import uvicorn

    import adk.daemon_endpoint as de

    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setenv("AITHER_HOST", "0.0.0.0")  # an image default must not win
    seen = {}
    monkeypatch.setattr(srv, "create_app", lambda **kw: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: seen.update(host=host,
                                                                                  port=port))
    monkeypatch.setattr(de, "publish_daemon_url", lambda h, p: f"http://{h}:{p}")
    monkeypatch.setattr(de, "clear_daemon_url", lambda: None)
    port = _free_port()
    monkeypatch.setattr(sys, "argv", ["adk-serve", "--port", str(port)])
    srv.main()
    assert seen == {"host": "127.0.0.1", "port": port}


def test_main_installs_guard_before_parsing(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "_install_air_gap_guard", lambda: calls.append("guard") or True)
    monkeypatch.setattr(sys, "argv", ["adk-serve", "--bogus-flag"])
    with pytest.raises(SystemExit):
        srv.main()
    assert calls == ["guard"], "the egress guard must be installed before argv is parsed"


def _client() -> TestClient:
    agent = MagicMock(spec=AitherAgent)
    agent.name = "t"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    agent.llm.list_models = AsyncMock(return_value=[])
    # base_url on loopback: under strict the guard judges the in-process ASGI
    # request by its host too, and the default "testserver" is not loopback.
    return TestClient(srv.create_app(agent=agent), base_url="http://127.0.0.1")


def test_health_air_gap_block_disabled(isolated):
    data = _client().get("/health").json()
    ag = data["air_gap"]
    assert ag["enforced"] is False
    assert ag["mode"] == "disabled"
    assert ag["guard_installed"] is False
    assert ag["violations_total"] == 0


def test_health_air_gap_block_strict(isolated):
    (isolated / "air_gap.yaml").write_text(
        "enabled: true\nenforcement: strict\nallowed_subnets:\n  - 127.0.0.0/8\n",
        encoding="utf-8")
    assert egress_guard.install_if_enforced() is True
    enf = air_gap.get_air_gap_enforcer()
    with pytest.raises(air_gap.AirGapViolation):
        enf.enforce_destination("http://203.0.113.7/x")
    data = _client().get("/health").json()
    ag = data["air_gap"]
    assert ag == {**ag, "enforced": True, "mode": "strict", "guard_installed": True}
    assert ag["violations_total"] >= 1


def test_cli_main_calls_guard_install(isolated, monkeypatch):
    from adk import cli

    calls = []
    monkeypatch.setattr(cli, "_install_air_gap_guard", lambda: calls.append(1) or False)
    monkeypatch.setattr(sys, "argv", ["adk", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == [1]


def test_cli_guard_helper_installs_when_enforced(isolated):
    from adk import cli

    (isolated / "air_gap.yaml").write_text("enabled: true\nenforcement: strict\n",
                                           encoding="utf-8")
    assert cli._install_air_gap_guard() is True
    assert egress_guard._INSTALLED["socket"] is True
    assert os.environ.get("AITHER_AIR_GAP_CONFIG", "").endswith("air_gap.yaml")


# ---- uvloop: libuv dials past socket.socket.connect --------------------------


def _run_main_capture_loop(monkeypatch) -> dict:
    import uvicorn

    import adk.daemon_endpoint as de

    seen: dict = {}
    monkeypatch.setattr(srv, "create_app", lambda **kw: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: seen.update(kw))
    monkeypatch.setattr(de, "publish_daemon_url", lambda h, p: f"http://{h}:{p}")
    monkeypatch.setattr(de, "clear_daemon_url", lambda: None)
    monkeypatch.setattr(sys, "argv", ["adk-serve", "--port", str(_free_port())])
    srv.main()
    return seen


def test_main_forces_stdlib_loop_under_strict(isolated, monkeypatch):
    """uvicorn loop='auto' picks uvloop on Linux, which bypasses the socket guard."""
    (isolated / "air_gap.yaml").write_text("enabled: true\nenforcement: strict\n",
                                           encoding="utf-8")
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    assert _run_main_capture_loop(monkeypatch).get("loop") == "asyncio"


def test_main_keeps_auto_loop_when_air_gap_off(isolated, monkeypatch):
    assert _run_main_capture_loop(monkeypatch).get("loop") == "auto"


def test_install_drops_a_uvloop_policy(isolated, monkeypatch):
    import asyncio
    import types

    fake_mod = types.ModuleType("uvloop")

    class EventLoopPolicy(asyncio.DefaultEventLoopPolicy):
        pass

    EventLoopPolicy.__module__ = "uvloop"
    fake_mod.EventLoopPolicy = EventLoopPolicy
    prev = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(EventLoopPolicy())
    try:
        (isolated / "air_gap.yaml").write_text("enabled: true\nenforcement: strict\n",
                                               encoding="utf-8")
        assert egress_guard.install_if_enforced() is True
        assert type(asyncio.get_event_loop_policy()).__module__ != "uvloop"
    finally:
        asyncio.set_event_loop_policy(prev)
