"""`adk enroll` registers with Identity reliably, or says loudly that it did not.

Measured 2026-10-14: a customer ran the setup door (whose last step is `adk enroll`)
and Identity's device registry held NOTHING for his tenant. Every path below is a
way that run could have registered nothing while looking fine, and each test fails
on the code before this change:

* a 5xx from Identity (its Postgres was down that day) was one attempt, then fell
  through to the legacy hub, which "enrolled" the PC into no device list -- and
  every later `adk enroll` answered "Already enrolled";
* `adk enroll --force` never re-registered: enroll_on_boot still short-circuited;
* the heartbeat died with the `adk enroll` process, so the machine read offline ten
  minutes later; nothing kept it beating;
* the device's own 365-day token was thrown away, so a lapsed sign-in meant 401
  beats forever.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from adk import enrollment, fleet_enroll

# ── rich_enroll: retry 5xx / transport, never a 4xx ─────────────────────────


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = json.dumps(body or {"detail": "x"})
    return r


def _client(*answers):
    """An httpx.AsyncClient stand-in whose post() yields ``answers`` in order
    (an exception instance is raised)."""
    posts = []
    seq = list(answers)

    async def post(url, json=None, headers=None):
        posts.append(url)
        a = seq.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    c = AsyncMock()
    c.__aenter__.return_value = c
    c.__aexit__.return_value = None
    c.post.side_effect = post
    return c, posts


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(enrollment, "REGISTER_RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **k: {
        "node_id": node_id, "inference_url": "", "inference_kind": "none",
        "node_class": "laptop"})
    monkeypatch.setattr(enrollment, "_save_workspace", lambda ws: None)
    monkeypatch.setattr(enrollment, "_persist_device_cert", lambda d: {"success": False})

    async def _no_storage(*a, **k):
        return {"registered": False}

    monkeypatch.setattr(enrollment, "_contribute_storage", _no_storage)


def test_a_503_is_retried_until_identity_answers(no_delay):
    c, posts = _client(_resp(503), _resp(503), _resp(200, {"node_id": "n1", "tenant_id": "t"}))
    with patch("httpx.AsyncClient", return_value=c):
        out = asyncio.run(enrollment.rich_enroll("https://id.test", "tok", "n1",
                                                 enable_heartbeat=False))
    assert out["enrolled"] is True
    assert len(posts) == 3


def test_a_503_on_every_attempt_is_reported_as_identity_unavailable(no_delay):
    c, posts = _client(*[_resp(503) for _ in range(4)])
    with patch("httpx.AsyncClient", return_value=c):
        out = asyncio.run(enrollment.rich_enroll("https://id.test", "tok", "n1",
                                                 enable_heartbeat=False))
    assert out["enrolled"] is False
    assert out["http_status"] == 503
    assert out["identity_unavailable"] is True
    assert len(posts) == 4  # first try + three retries


def test_a_refusal_is_final_on_the_first_answer(no_delay):
    c, posts = _client(_resp(402, {"detail": "subscription_required"}))
    with patch("httpx.AsyncClient", return_value=c):
        out = asyncio.run(enrollment.rich_enroll("https://id.test", "tok", "n1",
                                                 enable_heartbeat=False))
    assert out["http_status"] == 402
    assert not out.get("identity_unavailable")
    assert len(posts) == 1


def test_a_dropped_connection_is_retried(no_delay):
    c, posts = _client(httpx.ConnectError("refused"),
                       _resp(200, {"node_id": "n1", "tenant_id": "t"}))
    with patch("httpx.AsyncClient", return_value=c):
        out = asyncio.run(enrollment.rich_enroll("https://id.test", "tok", "n1",
                                                 enable_heartbeat=False))
    assert out["enrolled"] is True and len(posts) == 2


# ── enroll_on_boot ──────────────────────────────────────────────────────────


@pytest.fixture
def aither_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(fleet_enroll, "_AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(fleet_enroll, "_CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(fleet_enroll, "_AGENTS_FILE", tmp_path / "agents.json")
    monkeypatch.setattr(fleet_enroll, "_heartbeat_task", None)
    monkeypatch.setattr(fleet_enroll, "_start_node_link", lambda *a, **k: None)
    monkeypatch.setenv("AITHER_FLEET_ENROLL", "1")
    monkeypatch.setenv("AITHER_ENROLL_BASE", "https://identity.test")
    (tmp_path / "auth.json").write_text(json.dumps(
        {"access_token": "person-token", "user": {"tenant_slug": "acme"}}), encoding="utf-8")
    return tmp_path


def _must_not_federate(*_a, **_k):
    raise AssertionError("the legacy hub ran after Identity answered 5xx")


def test_identity_unavailable_is_not_papered_over_by_the_legacy_hub(aither_dir):
    async def fake_rich(base, token, node_id, **kw):
        return {"enrolled": False, "error": "HTTP 503", "http_status": 503,
                "body": '{"detail":"Directory unavailable"}', "url": base,
                "identity_unavailable": True}

    with patch.object(enrollment, "rich_enroll", fake_rich), \
            patch.object(fleet_enroll, "_register_node_with_federation", _must_not_federate), \
            patch.object(fleet_enroll, "_register_node_with_genesis", _must_not_federate):
        out = asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False))
    assert out["enrolled"] is False
    assert out["identity_unavailable"] is True
    assert "nothing was registered" in out["error"]
    assert not (aither_dir / "node_auth.json").exists()


def _ok_rich(seen):
    async def fake_rich(base, token, node_id, **kw):
        seen.append(node_id)
        return {"enrolled": True, "node_id": node_id, "tenant_id": "tnt_acme",
                "bearer_token": "device-cap-token", "registration": {
                    "inference_url": "", "inference_kind": "none", "node_class": "laptop"}}
    return fake_rich


def _quiet():
    return (patch.object(fleet_enroll, "_upsert_agents_to_portal", AsyncMock(return_value=False)),
            patch.object(fleet_enroll, "_sync_entitled_packs_best_effort",
                         AsyncMock(return_value=(0, 0))),
            patch.object(fleet_enroll, "_enable_session_sync_default", lambda: None),
            patch.object(fleet_enroll, "_self_mint_gateway_key", AsyncMock(return_value="")))


def test_force_registers_again_with_the_stored_node_id(aither_dir):
    (aither_dir / "node_auth.json").write_text(json.dumps(
        {"node_id": "adk-stored", "mode": "rich"}), encoding="utf-8")
    seen: list = []
    a, b, c, d = _quiet()
    with patch.object(enrollment, "rich_enroll", _ok_rich(seen)), a, b, c, d:
        out = asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False, force=True))
    assert seen == ["adk-stored"]
    assert out["enrolled"] is True and not out.get("already_registered")


def test_the_device_token_is_kept_for_the_heartbeat(aither_dir):
    a, b, c, d = _quiet()
    with patch.object(enrollment, "rich_enroll", _ok_rich([])), a, b, c, d:
        asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False))
    saved = json.loads((aither_dir / "node_auth.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "rich"
    assert saved["bearer_token"] == "device-cap-token"
    assert saved["tenant_id"] == "tnt_acme"


def test_a_legacy_fallback_carries_the_identity_error(aither_dir):
    async def fake_rich(base, token, node_id, **kw):
        return {"enrolled": False, "error": "connection refused"}

    async def fed(hub_url, api_key, node_id=None):
        return {"node_id": node_id or "fed-1", "api_key": api_key, "hub_url": hub_url}

    a, b, c, d = _quiet()
    with patch.object(enrollment, "rich_enroll", fake_rich), \
            patch.object(fleet_enroll, "_register_node_with_federation", fed), a, b, c, d:
        out = asyncio.run(fleet_enroll.enroll_on_boot(enable_heartbeat=False))
    assert out["enrolled"] is True
    assert out["mode"] == "legacy"
    assert out["identity_error"] == "connection refused"


# ── resume_heartbeat: the machine keeps beating as itself ───────────────────


def test_a_signed_in_enrolment_beats_as_the_device_once_nobody_is_signed_in(monkeypatch):
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {
        "node_id": "adk-1", "mode": "rich", "bearer_token": "device-cap",
        "enroll_base": "https://id.test"})
    started = []
    monkeypatch.setattr(enrollment, "start_heartbeat",
                        lambda base, tok, node_id, **kw: started.append((tok, kw)) or True)
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    out = enrollment.resume_heartbeat("", default_base_url="https://id.default")
    assert out["started"] is True
    tok, kw = started[0]
    assert tok == "device-cap" and kw["device"] is True


def test_a_signed_in_beat_carries_the_device_fallback(monkeypatch):
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {
        "node_id": "adk-1", "mode": "rich", "bearer_token": "device-cap"})
    started = []
    monkeypatch.setattr(enrollment, "start_heartbeat",
                        lambda base, tok, node_id, **kw: started.append((tok, kw)) or True)
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    enrollment.resume_heartbeat("person", default_base_url="https://id.default")
    tok, kw = started[0]
    assert tok == "person" and kw["device_fallback_token"] == "device-cap"


# ── cmd_enroll ──────────────────────────────────────────────────────────────


def _args(**over):
    base = dict(portal=None, genesis=None, no_heartbeat=False, no_link=True, force=False,
                inference_url="auto", node_class=None, invite=None, yes=False, label=None,
                no_autostart=False)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def cli_env(aither_dir, monkeypatch):
    from adk import cli, node_beat

    calls = {"enroll": [], "autostart": 0}

    def fake_boot(**kw):
        calls["enroll"].append(kw)
        return calls.get("result") or {"enrolled": True, "node_id": "adk-1", "mode": "rich"}

    async def _boot(**kw):
        return fake_boot(**kw)

    monkeypatch.setattr(fleet_enroll, "enroll_on_boot", _boot)

    def _auto():
        calls["autostart"] += 1
        return {"autostart": "windows-run-key"}

    monkeypatch.setattr(node_beat, "install_autostart", _auto)
    import adk.device_identity as di
    monkeypatch.setattr(di, "configure_settings_sync", lambda *a: (0, "ok"), raising=False)
    return cli, calls


def test_enroll_keeps_the_machine_beating_after_it_exits(cli_env, capsys):
    cli, calls = cli_env
    assert cli.cmd_enroll(_args()) == 0
    assert calls["autostart"] == 1
    assert "kept running at login" in capsys.readouterr().out


def test_no_autostart_installs_nothing(cli_env):
    cli, calls = cli_env
    assert cli.cmd_enroll(_args(no_autostart=True)) == 0
    assert calls["autostart"] == 0


def test_force_reaches_enroll_on_boot(cli_env):
    cli, calls = cli_env
    (fleet_enroll._NODE_AUTH_FILE).write_text(json.dumps(
        {"node_id": "adk-1", "mode": "rich"}), encoding="utf-8")
    assert cli.cmd_enroll(_args(force=True)) == 0
    assert calls["enroll"][0]["force"] is True


def test_a_legacy_only_record_is_registered_with_identity(cli_env, capsys):
    cli, calls = cli_env
    (fleet_enroll._NODE_AUTH_FILE).write_text(json.dumps(
        {"node_id": "adk-old", "hub_url": "https://hub"}), encoding="utf-8")
    assert cli.cmd_enroll(_args()) == 0
    assert calls["enroll"] and calls["enroll"][0]["force"] is True
    assert "legacy hub only" in capsys.readouterr().out


def test_a_legacy_only_result_fails_loudly(cli_env, capsys):
    cli, calls = cli_env
    calls["result"] = {"enrolled": True, "node_id": "fed-1", "mode": "legacy",
                       "identity_error": "connection refused"}
    assert cli.cmd_enroll(_args()) == cli.EXIT_NOT_REGISTERED
    out = capsys.readouterr().out
    assert "Not registered with Identity" in out and "connection refused" in out
    assert calls["autostart"] == 0


def test_a_402_from_identity_exits_not_registered_so_setup_goes_on(cli_env, capsys):
    """402 (no device plan) is NOT "not signed in": exit 4, which the setup page
    (AitherZero 3266) reports loudly and continues past to the app launcher, whose
    watcher lists the PC in the app (adk/app_device.py)."""
    cli, calls = cli_env
    calls["result"] = {"enrolled": False, "http_status": 402,
                       "url": "https://identity.test/v1/nodes/register",
                       "body": '{"detail":"subscription_required"}'}
    assert cli.cmd_enroll(_args()) == cli.EXIT_NOT_REGISTERED == 4
    assert "HTTP 402" in capsys.readouterr().out


def test_a_device_flow_login_without_a_tenant_slug_is_signed_in(cli_env, aither_dir):
    """`adk login` saves an access_token; a /me answer with no tenant_slug made
    `adk enroll` say "Not signed in" and register nothing."""
    cli, calls = cli_env
    (aither_dir / "auth.json").write_text(json.dumps(
        {"access_token": "person-token", "user": {"id": "user-1"}}), encoding="utf-8")
    assert cli.cmd_enroll(_args()) == 0
    assert calls["enroll"], "enroll_on_boot never ran"


# ── node_beat: one beater per machine ───────────────────────────────────────


def test_a_second_node_beat_does_not_start(tmp_path):
    from adk import node_beat

    lock = tmp_path / "node_beat.lock"
    held = node_beat.acquire_singleton(lock)
    assert held is not None
    try:
        assert node_beat.acquire_singleton(lock) is None
        assert node_beat.is_running(lock) is True
    finally:
        node_beat.release_singleton(held)
    assert node_beat.is_running(lock) is False


def test_rerunning_enroll_on_windows_starts_no_extra_beater(monkeypatch, tmp_path):
    from pathlib import Path

    from adk import node_beat

    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # the task XML lands here

    popens = []
    # A stand-in os for node_beat only: patching the real os.name makes every Path()
    # built meanwhile (pytest's own cache write included) a WindowsPath, which cannot
    # be instantiated on Linux under 3.10 and crashed the ADK payload run (2026-10-14).
    import os as _real_os
    import types as _types
    fake_os = _types.ModuleType("os")
    fake_os.__dict__.update(_real_os.__dict__)
    fake_os.name = "nt"
    monkeypatch.setattr(node_beat, "os", fake_os)
    runs = []
    task_run_rc = {"rc": 0}

    def fake_run(argv, *a, **k):
        runs.append(list(argv))
        rc = task_run_rc["rc"] if list(argv[:2]) == ["schtasks", "/run"] else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="")
    monkeypatch.setattr(node_beat.subprocess, "run", fake_run)
    monkeypatch.setattr(node_beat.subprocess, "Popen", lambda *a, **k: popens.append(a))
    monkeypatch.setattr(node_beat, "is_running", lambda path=None: True)
    out = node_beat.install_autostart()
    assert out["autostart"] == "windows-task" and out.get("running") == "already"
    assert popens == [] and not any(r[:2] == ["schtasks", "/run"] for r in runs)
    monkeypatch.setattr(node_beat, "is_running", lambda path=None: False)
    node_beat.install_autostart()  # started through its own task, not a second Popen
    assert popens == [] and ["schtasks", "/run", "/tn", node_beat.TASK_NAME] in runs
    task_run_rc["rc"] = 1
    node_beat.install_autostart()  # the task would not start: one detached beater
    assert len(popens) == 1
