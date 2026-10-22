"""The device runs only what its control plane signed for it, and says what it did.

The control plane queues a command; the heartbeat collects it. These pin the
device half (``adk.node_commands`` + the beat loop): a command runs only when its
signature, device id, expiry and verb all check out and it has not run before;
a refused command runs nothing and answers nothing; a result goes back signed;
a device enrolled before the channel existed fetches its key once. No network:
the HTTP client is a recording stand-in.
"""

import asyncio
import json
import time

import pytest

from adk import enrollment, node_commands

KEY = "ab" * 32
NODE = "deck-1"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    # Never write a real Run key / user unit or spawn a beat from a test (it did once).
    from adk import node_beat
    monkeypatch.setattr(node_beat, "install_autostart",
                        lambda: {"autostart": "test-disabled"})
    # Never let an armed upgrade restart re-exec / kill the test process.
    getattr(node_commands, "_PENDING_RESTART", {}).clear()
    yield tmp_path
    getattr(node_commands, "_PENDING_RESTART", {}).clear()


def _cmd(**over):
    cmd = {"id": "cmd_1", "tenant_id": "tnt", "node_id": NODE, "verb": "lend-on",
           "args": {"via": "lan"}, "issued_by": "u1", "issued_at": int(time.time()),
           "expires_at": int(time.time()) + 600}
    cmd.update(over)
    cmd["sig"] = node_commands.sign(KEY, node_commands.canonical(cmd))
    return cmd


def _handlers(ran):
    return {v: (lambda a, v=v: ran.append((v, a)) or {"done": v}) for v in node_commands.VERBS}


def test_a_signed_command_for_this_device_runs_once_and_answers_signed():
    ran = []
    (res,) = node_commands.run_commands([_cmd()], NODE, KEY, handlers=_handlers(ran))
    assert ran == [("lend-on", {"via": "lan"})]
    assert res["ok"] is True and json.loads(res["output"]) == {"done": "lend-on"}
    assert res["sig"] == node_commands.sign(
        KEY, node_commands.canonical(res, node_commands.RESULT_FIELDS))
    assert node_commands.run_commands([_cmd()], NODE, KEY, handlers=_handlers(ran)) == []
    assert len(ran) == 1


@pytest.mark.parametrize("cmd,why", [
    (lambda: {**_cmd(), "args": {"via": "tunnel"}}, "bad signature"),
    (lambda: _cmd(node_id="other"), "addressed to another device"),
    (lambda: _cmd(expires_at=int(time.time()) - 1), "expired"),
    (lambda: {**_cmd(verb="shell", args={}), "sig": "0" * 64}, "bad signature"),
    (lambda: _cmd(args={"via": "http://x"}), "not allowed for lend-on"),
    (lambda: _cmd(args={"cmd": "lan"}), "not allowed for lend-on"),
    (lambda: {k: v for k, v in _cmd().items() if k != "sig"}, "bad signature"),
])
def test_anything_else_runs_nothing_and_answers_nothing(cmd, why):
    c = cmd()
    assert why in node_commands.verify(c, KEY, NODE, seen=[])
    ran = []
    assert node_commands.run_commands([c], NODE, KEY, handlers=_handlers(ran)) == []
    assert ran == []


def test_no_key_on_the_device_runs_nothing():
    assert node_commands.verify(_cmd(), "", NODE, seen=[]) == "no command key on this device"


def test_a_failing_handler_reports_not_raises():
    def boom(_a):
        raise RuntimeError("relay binary missing")
    (res,) = node_commands.run_commands([_cmd()], NODE, KEY, handlers={"lend-on": boom})
    assert res["ok"] is False and "relay binary missing" in res["output"]


def test_the_key_is_kept_per_device():
    assert node_commands.save_key(NODE, KEY)
    assert node_commands.load_key(NODE) == KEY
    assert node_commands.load_key("another") == ""


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    """Answers the beat with commands, the register with a key, results with 200."""

    def __init__(self, commands, key=KEY):
        self.commands, self.key, self.posts = commands, key, []

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        if url.endswith("/heartbeat"):
            return _Resp(200, {"status": "ok", "commands": self.commands})
        if url.endswith("/v1/nodes/register"):
            return _Resp(200, {"status": "registered", "command_key": self.key})
        return _Resp(200, {"status": "done"})


def _beat_once(client, monkeypatch, ran):
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    monkeypatch.setattr(node_commands, "_HANDLERS", _handlers(ran))
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url=None, node_class="deck", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True))


def test_a_device_without_a_key_fetches_it_once_then_runs_and_reports(monkeypatch):
    client, ran = _Client([_cmd(verb="update", args={})]), []
    _beat_once(client, monkeypatch, ran)
    urls = [u for u, _ in client.posts]
    assert urls == ["https://idp.test/v1/nodes/heartbeat", "https://idp.test/v1/nodes/register",
                    f"https://idp.test/v1/nodes/{NODE}/commands/cmd_1/result"]
    assert ran == [("update", {})]
    assert node_commands.load_key(NODE) == KEY
    result = client.posts[-1][1]
    assert result["id"] == "cmd_1" and result["ok"] is True and result["sig"]


def test_a_forged_beat_answer_runs_nothing(monkeypatch):
    node_commands.save_key(NODE, KEY)
    forged = {**_cmd(verb="lend-off", args={}), "sig": "0" * 64}
    client, ran = _Client([forged]), []
    _beat_once(client, monkeypatch, ran)
    assert ran == []
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/heartbeat"]


def _beat_device(client, monkeypatch, ran):
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    monkeypatch.setattr(node_commands, "_HANDLERS", _handlers(ran))
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer device-tok"}, NODE, interval=0,
        inference_url=None, node_class="spark", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True, device=True))


def test_a_paired_device_beats_and_reports_as_itself(monkeypatch):
    node_commands.save_key(NODE, KEY)
    client, ran = _Client([_cmd(verb="collect-diagnostics", args={})]), []
    _beat_device(client, monkeypatch, ran)
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/device/heartbeat",
                                            "https://idp.test/v1/nodes/device/results/cmd_1"]
    assert ran == [("collect-diagnostics", {})]


def test_a_device_with_no_key_never_tries_to_reregister_with_its_token(monkeypatch):
    client, ran = _Client([_cmd()]), []
    _beat_device(client, monkeypatch, ran)
    assert [u for u, _ in client.posts] == ["https://idp.test/v1/nodes/device/heartbeat"]
    assert ran == []


def test_a_removed_device_is_reported_not_reregistered(monkeypatch):
    class _Gone(_Client):
        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json))
            return _Resp(200, {"status": "unknown_node"})

    client = _Gone([])
    monkeypatch.setattr(enrollment, "_heartbeat_state", enrollment._new_heartbeat_state())
    _beat_device(client, monkeypatch, [])
    assert len(client.posts) == 1
    assert "removed" in enrollment._heartbeat_state.get("last_error", "")


def test_a_paired_record_resumes_a_device_token_heartbeat(tmp_path, monkeypatch):
    from adk import fleet_enroll
    rec = {"node_id": NODE, "bearer_token": "device-tok", "enrolled_via": "pairing-code",
           "node_class": "spark"}
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: rec)
    calls = []
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    monkeypatch.setattr(enrollment, "start_heartbeat",
                        lambda base, tok, nid, **kw: calls.append((base, tok, nid, kw)) or True)
    out = enrollment.resume_heartbeat("", default_base_url="https://idp.test")
    assert out == {"started": True, "reason": "", "node_id": NODE}
    ((base, tok, nid, kw),) = calls
    assert (base, tok, nid, kw["device"], kw["node_class"]) == (
        "https://idp.test", "device-tok", NODE, True, "spark")
    # A signed-in person still beats as themselves.
    calls.clear()
    rec["mode"] = "rich"
    enrollment.resume_heartbeat("user-tok", default_base_url="https://idp.test")
    assert calls[0][1] == "user-tok" and "device" not in calls[0][3]


def test_adk_pair_defaults_to_identitys_own_confirm_route(monkeypatch):
    """The portal proxy answered an anonymous confirm with 401; Identity is the door."""
    from types import SimpleNamespace

    from adk import devices, node_pairing

    seen = {}

    async def fake_pair(code, base, node_class="laptop", confirm_path=""):
        seen.update(code=code, base=base, node_class=node_class, path=confirm_path)
        return {"paired": True, "node_id": NODE}

    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)
    monkeypatch.setattr(devices, "enroll_base", lambda: "https://idp.test")
    monkeypatch.setattr(node_pairing, "pair_with_code", fake_pair)
    assert node_pairing.cmd_pair(SimpleNamespace(code="ABCD2345", portal="",
                                                 node_class="spark", no_autostart=True)) == 0
    assert seen == {"code": "ABCD2345", "base": "https://idp.test", "node_class": "spark",
                    "path": "/v1/nodes/pairing/confirm"}


def test_adk_pair_takes_the_code_from_the_environment_not_argv(monkeypatch):
    """The installers pass the one-time code as AITHER_PAIR_CODE: argv is world-visible
    (security review, 2026-10-04). The variable is consumed, never inherited."""
    import os
    from types import SimpleNamespace

    from adk import devices, node_pairing

    seen = {}

    async def fake_pair(code, base, node_class="laptop", confirm_path=""):
        seen["code"] = code
        return {"paired": True, "node_id": NODE}

    monkeypatch.setattr(devices, "enroll_base", lambda: "https://idp.test")
    monkeypatch.setattr(node_pairing, "pair_with_code", fake_pair)
    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)
    monkeypatch.setenv("AITHER_PAIR_CODE", "ENVC0DE9")
    assert node_pairing.cmd_pair(SimpleNamespace(code="", portal="", node_class="laptop",
                                                 no_autostart=True)) == 0
    assert seen["code"] == "ENVC0DE9"
    assert "AITHER_PAIR_CODE" not in os.environ
    assert node_pairing.cmd_pair(SimpleNamespace(code="", portal="", node_class="laptop",
                                                 no_autostart=True)) == 2  # nothing given


def test_a_paired_machine_boots_into_the_identity_heartbeat_as_itself(monkeypatch):
    """deck's finding: a paired box fell into the LEGACY hub loop with the local-root
    placeholder and went stale after one beat."""
    from adk import fleet_enroll

    rec = {"node_id": NODE, "bearer_token": "device-tok", "enrolled_via": "pairing-code",
           "mode": "rich", "enroll_base": "https://idp.test", "node_class": "deck"}
    monkeypatch.setenv("AITHER_FLEET_ENROLL", "1")
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: dict(rec))
    monkeypatch.setattr(fleet_enroll, "_load_auth_config", lambda: {})
    monkeypatch.setattr(fleet_enroll, "_backfill_node_tenant_id", lambda r: None)
    started = {}

    async def rich(base, token, node_id, **kw):
        started.update(base=base, token=token, node_id=node_id, **kw)

    async def legacy(*a, **kw):
        started["legacy"] = True

    monkeypatch.setattr(enrollment, "heartbeat_loop", rich)
    monkeypatch.setattr(fleet_enroll, "_heartbeat_loop", legacy)
    monkeypatch.setattr(fleet_enroll, "_start_heartbeat_task", _drive)
    out = asyncio.run(fleet_enroll.enroll_on_boot(start_link=False))
    assert out["enrolled"] and out["already_registered"]
    assert "legacy" not in started
    assert (started["base"], started["token"], started["device"], started["node_class"]) == (
        "https://idp.test", "device-tok", True, "deck")


def test_a_paired_machine_signed_in_later_still_holds_the_link(monkeypatch):
    """spark, 2026-10-18: paired before `mode` was saved, then `adk login` as the owner.
    It fell into the legacy loop: no reverse link, `reach: none`, rc 'Holding the link'."""
    from adk import fleet_enroll

    rec = {"node_id": NODE, "bearer_token": "device-tok", "enrolled_via": "pairing-code",
           "enroll_base": "https://idp.test", "node_class": "spark"}  # no "mode"
    monkeypatch.setenv("AITHER_FLEET_ENROLL", "1")
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: dict(rec))
    monkeypatch.setattr(fleet_enroll, "_load_auth_config", lambda: {"access_token": "owner-tok"})
    monkeypatch.setattr(fleet_enroll, "_backfill_node_tenant_id", lambda r: None)
    links, started = [], {}

    def fake_link(node_id, token, **kw):
        links.append((node_id, token))
        return None

    async def rich(base, token, node_id, **kw):
        started.update(base=base, token=token, device=kw.get("device"))

    async def legacy(*a, **kw):
        started["legacy"] = True

    monkeypatch.setattr(fleet_enroll, "_start_node_link", fake_link)
    monkeypatch.setattr(enrollment, "heartbeat_loop", rich)
    monkeypatch.setattr(fleet_enroll, "_heartbeat_loop", legacy)
    monkeypatch.setattr(fleet_enroll, "_start_heartbeat_task", _drive)
    asyncio.run(fleet_enroll.enroll_on_boot(start_link=True))
    assert "legacy" not in started
    assert links == [(NODE, "owner-tok")]          # the owner's sign-in holds the link
    assert (started["token"], started["device"]) == ("owner-tok", False)


def _drive(coro):
    """Run a coroutine that never suspends to completion (the fake loop above)."""
    try:
        coro.send(None)
    except StopIteration:
        return True
    raise AssertionError("the heartbeat stand-in was expected to finish in one step")


def test_rc_accepts_a_paired_machine_as_signed_in(monkeypatch):
    from adk import fleet_enroll, rc

    monkeypatch.setattr(fleet_enroll, "_load_auth_config", lambda: {})
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {})
    assert rc._signed_in() is False
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {
        "node_id": NODE, "enrolled_via": "pairing-code", "bearer_token": "device-tok"})
    assert rc._signed_in() is True


def test_pairing_marks_the_record_as_an_identity_registration(monkeypatch):
    from adk import fleet_enroll, node_pairing

    saved = {}
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {})
    monkeypatch.setattr(fleet_enroll, "_save_node_auth", lambda d: saved.update(d))
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "node_class": kw.get("node_class", "laptop"),
        "inference_url": ""})
    monkeypatch.setattr(enrollment, "_persist_device_cert", lambda d: {"success": True})
    monkeypatch.setattr(enrollment, "_save_workspace", lambda w: None)

    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            assert url == "https://idp.test/v1/nodes/pairing/confirm"
            return _Resp(200, {"node_id": json["node_id"], "tenant_id": "platform",
                               "bearer_token": "device-tok", "command_key": KEY})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _C)
    out = asyncio.run(node_pairing.pair_with_code(
        "ABCD2345", "https://idp.test", node_class="deck",
        confirm_path=node_pairing.IDENTITY_CONFIRM_PATH))
    assert out["paired"]
    assert (saved["mode"], saved["enroll_base"], saved["enrolled_via"], saved["node_class"]) == (
        "rich", "https://idp.test", "pairing-code", "deck")
    assert node_commands.load_key(saved["node_id"]) == KEY


def test_pair_installs_the_heartbeat_autostart_unless_told_not_to(monkeypatch):
    from types import SimpleNamespace

    from adk import devices, node_beat, node_pairing

    async def fake_pair(code, base, node_class="laptop", confirm_path=""):
        return {"paired": True, "node_id": NODE, "tenant_id": "platform"}

    installed = []
    monkeypatch.delenv("AITHER_PORTAL_URL", raising=False)
    monkeypatch.setattr(devices, "enroll_base", lambda: "https://idp.test")
    monkeypatch.setattr(node_pairing, "pair_with_code", fake_pair)
    monkeypatch.setattr(node_beat, "install_autostart",
                        lambda: installed.append(1) or {"autostart": "systemd-user"})
    args = SimpleNamespace(code="ABCD2345", portal="", node_class="spark", no_autostart=False)
    assert node_pairing.cmd_pair(args) == 0 and installed == [1]
    args.no_autostart = True
    assert node_pairing.cmd_pair(args) == 0 and installed == [1]


def test_node_beat_resumes_as_the_device_when_nobody_is_signed_in(monkeypatch):
    from adk import fleet_enroll, node_beat

    seen = {}
    monkeypatch.setattr(fleet_enroll, "_load_auth_config", lambda: {})
    monkeypatch.setattr(enrollment, "resume_heartbeat",
                        lambda tok, default_base_url, **kw: seen.update(
                            tok=tok, base=default_base_url,
                            fresh=kw["token_provider"]() if kw.get("token_provider") else None)
                        or {"started": False, "reason": "not registered", "node_id": ""})
    monkeypatch.delenv("AITHER_IDP_URL", raising=False)
    monkeypatch.delenv("AITHER_IDP_BASE_URL", raising=False)
    assert asyncio.run(node_beat._main()) == 1
    # The token is re-read every beat (a refreshed sign-in reaches the loop).
    assert seen == {"tok": "", "base": "https://idp.aitherium.com", "fresh": ""}


def test_a_refused_beat_gets_a_fresh_client(monkeypatch):
    """After an identity redeploy a long-lived client answered 404 for 12 minutes while a
    fresh one got 200 (measured 2026-10-03); a refused beat now swaps the client."""
    class _Stuck(_Client):
        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json))
            return _Resp(404, {"detail": "Not Found"})

    stuck, fresh = _Stuck([]), _Client([])
    renewed = []

    async def renew():
        renewed.append(1)
        return fresh

    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    asyncio.run(enrollment._heartbeat_beats(
        stuck, "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url=None, node_class="spark", max_beats=2, reach_provider=None,
        harness_provider=None, beat_immediately=True, device=True, renew=renew))
    assert renewed == [1]
    assert len(stuck.posts) == 1 and len(fresh.posts) == 1


# ── upgrade: one exact release, a fixed pip argv, a restart after the answer ─


@pytest.mark.parametrize("version", [
    "1.2.3; rm -rf /", ">=1", "1.2", "1.2.3.4", "https://evil/awdk.whl", "../awdk",
    "1.2.3 --index-url https://evil", "1.2.3\n", "\uff11.2.3", "", "--pre",
])
def test_upgrade_refuses_anything_but_an_exact_release(version):
    why = node_commands.verify(_cmd(verb="upgrade", args={"version": version}), KEY, NODE,
                               seen=[])
    assert "is not a valid version" in why, why
    assert "takes exactly version" in node_commands.verify(
        _cmd(verb="upgrade", args={"version": "1.2.3", "index": "x"}), KEY, NODE, seen=[])
    assert node_commands._upgrade({"version": version})["ok"] is False


def _fake_run(calls, *, pip_rc=0, installed="3.8.59", units=("aither-node-beat.service",)):
    class _P:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def run(argv, **kw):
        calls.append((list(argv), kw))
        if argv[1:3] == ["-m", "pip"]:
            return _P(pip_rc, err="" if pip_rc == 0 else "No matching distribution")
        if argv[1] == "-c":
            return _P(0, out=installed + "\n")
        return _P(0)
    return run


def test_upgrade_runs_exactly_the_fixed_pip_argv_and_restarts_its_unit(monkeypatch):
    import shutil
    import subprocess
    import sys

    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(node_commands, "_own_user_unit", lambda: "aither-node-beat.service")
    assert node_commands.verify(_cmd(verb="upgrade", args={"version": "3.8.59"}), KEY, NODE,
                                seen=[]) == ""
    out = node_commands._upgrade({"version": "3.8.59"})
    pip_argv, pip_kw = calls[0]
    assert pip_argv == [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "-q", "awdk==3.8.59"]
    assert pip_kw.get("shell") is not True and pip_kw["timeout"] == 600
    assert calls[1][0][:2] == [sys.executable, "-c"] and "importlib.metadata" in calls[1][0][2]
    assert calls[2][0] == ["/usr/bin/systemd-run", "--user", "--on-active=30",
                           "--timer-property=AccuracySec=1s", "/usr/bin/systemctl", "--user",
                           "restart", "aither-node-beat.service"]
    assert out["ok"] is True and out["to"] == "3.8.59" and out["rc"] == 0
    assert out["restart"]["mechanism"] == "systemd-run" and out["restart"]["scheduled"] is True


def _no_pip(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "pip" else real(name, *a, **k))


def test_a_venv_without_pip_upgrades_through_uv(monkeypatch):
    """The DGX Spark's heartbeat venv was made by uv and has no pip (2026-10-04)."""
    import shutil
    import subprocess
    import sys

    _no_pip(monkeypatch)
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(node_commands, "_own_user_unit", lambda: "aither-node-beat.service")
    out = node_commands._upgrade({"version": "3.8.59"})
    assert calls[0][0] == ["/usr/bin/uv", "pip", "install", "--python", sys.executable, "-q",
                           "awdk==3.8.59"]
    assert calls[0][1].get("shell") is not True
    assert out["ok"] is True and out["restart"]["scheduled"] is True


def test_no_pip_and_no_uv_runs_nothing(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    _no_pip(monkeypatch)
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = node_commands._upgrade({"version": "3.8.59"})
    assert out["ok"] is False and "no installer" in out["error"] and calls == []


def test_a_failed_or_mismatched_install_restarts_nothing(monkeypatch):
    import subprocess

    for kw in ({"pip_rc": 1}, {"installed": "3.8.58"}):
        calls = []
        monkeypatch.setattr(subprocess, "run", _fake_run(calls, **kw))
        monkeypatch.setattr(node_commands, "_own_user_unit",
                            lambda: "aither-node-beat.service")
        out = node_commands._upgrade({"version": "3.8.59"})
        assert out["ok"] is False and "restart" not in out, out
        assert not any("systemd-run" in c[0][0] for c in calls)


def test_outside_a_user_unit_the_upgrade_arms_a_self_restart(monkeypatch):
    """Windows laptop 2026-10-22: 3.8.81 -> 3.8.84 installed, restart "none", and the
    beat ran 3.8.81 for good. Off systemd the upgrade now arms a restart (nothing runs
    yet: the result has not been posted)."""
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(calls))
    monkeypatch.setattr(node_commands, "_own_user_unit", lambda: "")
    for kind, mech in (("windows", "detached-relaunch"), ("posix", "execv")):
        monkeypatch.setattr(node_commands, "_host_kind", lambda kind=kind: kind, raising=False)
        out = node_commands._upgrade({"version": "3.8.59"})
        assert out["ok"] is True and out["restart"]["mechanism"] == mech, out
        assert out["restart"]["scheduled"] is True
        assert node_commands._PENDING_RESTART["mechanism"] == mech
    assert len(calls) == 4  # pip + version probe, twice; nothing restarted inline


class _Exited(Exception):
    pass


def test_windows_upgrade_relaunches_hidden_after_the_result_is_posted(monkeypatch):
    """Non-systemd Windows: the beat posts the upgrade result FIRST, then a hidden
    detached helper is spawned to relaunch this command line, then the old process
    exits. Never a console window; never execv on Windows."""
    import json as _json
    import os
    import subprocess
    import sys

    events = []
    node_commands.save_key(NODE, KEY)
    monkeypatch.setattr(subprocess, "run", _fake_run([]))
    monkeypatch.setattr(node_commands, "_own_user_unit", lambda: "")
    monkeypatch.setattr(node_commands, "_host_kind", lambda: "windows", raising=False)

    class _Popen:
        def __init__(self, argv, **kw):
            events.append(("spawn", argv, kw))
            self.pid = 4242

    def _exit(code):
        events.append(("exit", code))
        raise _Exited()

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    monkeypatch.setattr(os, "_exit", _exit)
    monkeypatch.setattr(os, "execv", lambda *a: events.append(("execv", a)))

    class _RecClient(_Client):
        async def post(self, url, json=None, headers=None):
            events.append(("post", url))
            return await super().post(url, json=json, headers=headers)

    client = _RecClient([_cmd(verb="upgrade", args={"version": "3.8.59"})])
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url=None, node_class="deck", max_beats=1, reach_provider=None,
        harness_provider=None, beat_immediately=True))

    result_url = f"https://idp.test/v1/nodes/{NODE}/commands/cmd_1/result"
    kinds = [e[0] for e in events]
    assert ("post", result_url) in events, events
    assert "spawn" in kinds and "exit" in kinds, events
    assert events.index(("post", result_url)) < kinds.index("spawn") < kinds.index("exit")
    assert "execv" not in kinds
    _, argv, kw = events[kinds.index("spawn")]
    assert argv[:3] == [sys.executable, "-I", "-c"]
    spec = _json.loads(argv[4])
    assert spec["argv"][0] == sys.executable and spec["pid"] == os.getpid()
    no_window, detached = 0x08000000, 0x00000008
    for flags in (kw["creationflags"], spec["flags"]):
        assert flags & no_window and flags & detached
    posted = [j for u, j in client.posts if u == result_url][-1]
    out = _json.loads(posted["output"])
    assert posted["ok"] is True and out["restart"]["mechanism"] == "detached-relaunch"


def test_posix_without_a_unit_re_execs_after_the_result_is_posted(monkeypatch):
    import os

    events = []
    monkeypatch.setattr(os, "execv", lambda path, argv: events.append(("execv", path, argv)))
    node_commands._PENDING_RESTART.update({"mechanism": "execv", "argv": ["/py", "-m", "x"]})
    assert node_commands.perform_pending_restart() == {}
    assert events == [("execv", "/py", ["/py", "-m", "x"])]
    assert node_commands.perform_pending_restart() == {}  # disarmed after one use
    assert len(events) == 1


def test_macos_launchd_job_is_kickstarted(monkeypatch):
    import os
    import subprocess

    monkeypatch.setattr(node_commands, "_host_kind", lambda: "darwin", raising=False)
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.aitherium.agent")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    plan = node_commands._plan_restart()
    assert plan["mechanism"] == "launchctl-kickstart" and plan["label"] == "com.aitherium.agent"
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        type("P", (), {"returncode": 0})())
    out = node_commands.perform_pending_restart()
    assert calls == [["launchctl", "kickstart", "-k", "gui/501/com.aitherium.agent"]]
    assert out["mechanism"] == "launchctl-kickstart"
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")  # a Terminal session is not a job
    assert node_commands._plan_restart()["mechanism"] == "execv"


def test_no_upgrade_means_no_restart(monkeypatch):
    import os

    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("restarted without an upgrade"))
    assert node_commands.perform_pending_restart() == {}


def test_an_authentic_unknown_verb_is_answered_failed_not_left_delivered():
    """mesh-lend-on sat "delivered" 30+ min on a 3.8.81 laptop (2026-10-22)."""
    from adk import __version__

    ran = []
    cmd = _cmd(verb="verb-from-the-future", args={"x": "1"})
    results = node_commands.run_commands([cmd], NODE, KEY, handlers=_handlers(ran))
    assert len(results) == 1, "an authentic unknown verb must be answered"
    (res,) = results
    assert ran == []
    assert res["ok"] is False and res["id"] == "cmd_1"
    out = json.loads(res["output"])
    assert out["error"] == f"unknown verb on this awdk {__version__}; upgrade first"
    assert res["sig"] == node_commands.sign(
        KEY, node_commands.canonical(res, node_commands.RESULT_FIELDS))
    # answered once; a replay is refused silently like any replay
    assert node_commands.run_commands([cmd], NODE, KEY, handlers=_handlers(ran)) == []


def test_an_unknown_verb_from_a_beat_gets_its_failure_posted(monkeypatch):
    node_commands.save_key(NODE, KEY)
    client, ran = _Client([_cmd(verb="mesh-lend-sideways", args={})]), []
    _beat_once(client, monkeypatch, ran)
    url, body = client.posts[-1]
    assert url == f"https://idp.test/v1/nodes/{NODE}/commands/cmd_1/result"
    assert body["ok"] is False and "upgrade first" in body["output"] and ran == []


def test_own_user_unit_reads_the_cgroup(monkeypatch, tmp_path):
    from pathlib import Path

    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                  "aither-node-beat.service\n", encoding="utf-8")
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: real(
        cg if str(self).replace("\\", "/") == "/proc/self/cgroup" else self, *a, **k))
    assert node_commands._own_user_unit() == "aither-node-beat.service"
    cg.write_text("0::/system.slice/aither-genesis.service\n", encoding="utf-8")
    assert node_commands._own_user_unit() == ""


# ── advertise-inference: the owner names the URL the heartbeat advertises ───


@pytest.mark.parametrize("url", [
    "ftp://10.0.0.5:8114", "http://10.0.0.5", "http://user:pw@10.0.0.5:8114",
    "http://10.0.0.5:8114/v1?x=1", "http://10.0.0.5:8114\n", "http://10.0.0.5:99999",
    "http://10.0.0.5:8114/other", "http://" + "a" * 260 + ":1", "file:///etc/passwd",
])
def test_advertise_inference_refuses_a_bad_url(url):
    why = node_commands.verify(_cmd(verb="advertise-inference", args={"url": url}), KEY, NODE,
                               seen=[])
    assert "is not a valid url" in why, why


def test_advertise_inference_persists_and_the_probe_uses_it(monkeypatch, _home):
    monkeypatch.delenv("AITHER_NODE_INFERENCE_URL", raising=False)
    probed = []
    monkeypatch.setattr(enrollment, "_openai_models",
                        lambda base: probed.append(base) or (True, ["m"]))
    monkeypatch.setattr(enrollment, "_fingerprint", lambda base: "llama-server")
    url = "http://10.0.0.5:8114/v1"
    assert node_commands.verify(_cmd(verb="advertise-inference", args={"url": url}), KEY,
                                NODE, seen=[]) == ""
    out = node_commands._advertise_inference({"url": url})
    assert out["ok"] is True and out["url"] == "http://10.0.0.5:8114"
    assert json.loads((_home / "node-inference.json").read_text())["url"] == out["url"]
    assert enrollment.probe_inference().inference_url == "http://10.0.0.5:8114"
    # the env still wins over the file
    monkeypatch.setenv("AITHER_NODE_INFERENCE_URL", "http://127.0.0.1:9999")
    assert enrollment.probe_inference().inference_url == "http://127.0.0.1:9999"
    monkeypatch.delenv("AITHER_NODE_INFERENCE_URL")
    # 'auto' clears it: back to the ladder
    assert node_commands._advertise_inference({"url": "auto"})["cleared"] is True
    assert not (_home / "node-inference.json").exists()
    assert enrollment.advertised_inference_url() == ""
    assert enrollment.probe_inference(candidates=[]).inference_url == ""


def test_the_heartbeat_picks_up_a_changed_url_on_its_next_beat(monkeypatch, _home):
    monkeypatch.delenv("AITHER_NODE_INFERENCE_URL", raising=False)
    seen = []

    def reg(node_id, **kw):
        seen.append(kw["inference_url"])
        if len(seen) == 1:  # the owner's command lands between beats
            enrollment.save_advertised_inference_url("http://10.0.0.5:8114")
        return {"node_id": node_id, "inference_ready": False, "available_models": [],
                "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"}

    monkeypatch.setattr(enrollment, "build_registration", reg)
    asyncio.run(enrollment._heartbeat_beats(
        _Client([]), "https://idp.test", {"Authorization": "Bearer t"}, NODE, interval=0,
        inference_url="http://127.0.0.1:8114", node_class="spark", max_beats=2,
        reach_provider=None, harness_provider=None, beat_immediately=True, device=True))
    assert seen == ["http://127.0.0.1:8114", "http://10.0.0.5:8114"]


# ── upgrade-component: closed package list, fixed argv, restarts nothing ────


@pytest.mark.parametrize("args", [
    {"component": "awdk", "version": "1.2.3"}, {"component": "awnode", "version": ">=1"},
    {"component": "awnode"}, {"component": "awnode", "version": "1.2.3", "x": "y"},
    {"component": "awnode; rm -rf /", "version": "1.2.3"},
])
def test_upgrade_component_refuses_off_list_args(args):
    why = node_commands.verify(_cmd(verb="upgrade-component", args=args), KEY, NODE, seen=[])
    assert why, args
    assert node_commands._upgrade_component(args)["ok"] is False


def test_upgrade_component_runs_the_fixed_argv_for_awnode_only(monkeypatch):
    import subprocess
    import sys

    calls = []

    class _P:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def run(argv, **kw):
        calls.append(list(argv))
        return _P(0, "0.9.1\n") if argv[1] == "-c" else _P(0)

    monkeypatch.setattr(subprocess, "run", run)
    assert node_commands.verify(
        _cmd(verb="upgrade-component", args={"component": "awnode", "version": "0.9.1"}),
        KEY, NODE, seen=[]) == ""
    out = node_commands._upgrade_component({"component": "awnode", "version": "0.9.1"})
    assert out["ok"] is True and out["installed"] == "0.9.1"
    pip = [c for c in calls if c[1:3] == ["-m", "pip"]]
    assert pip == [[sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "-q", "awnode==0.9.1"]]
    assert not any("systemctl" in " ".join(c) or "systemd-run" in " ".join(c) for c in calls)


# ── a stale person sign-in must not strand a code-paired device ─────────────


def test_a_401_sign_in_beat_falls_back_to_the_paired_device_token(monkeypatch):
    """Measured 2026-10-07 on the optiplex: a July auth.json (an expired person
    sign-in) shadowed the pairing-code device token, and every beat answered 401
    forever. With the device token as a fallback the second beat goes out as the
    device, on the device route, and is accepted."""

    class _Stale:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, headers["Authorization"]))
            if url.endswith("/v1/nodes/heartbeat"):
                return _Resp(401, {"detail": "expired"})
            return _Resp(200, {"status": "ok"})

    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **kw: {
        "node_id": node_id, "inference_ready": False, "available_models": [],
        "gpu_vram_mb": 0, "inference_url": "", "inference_kind": "none"})
    client = _Stale()
    asyncio.run(enrollment._heartbeat_beats(
        client, "https://idp.test", {"Authorization": "Bearer stale-user"}, NODE, interval=0,
        inference_url=None, node_class="sovereign", max_beats=2, reach_provider=None,
        harness_provider=None, beat_immediately=True, token_provider=lambda: "stale-user",
        device_fallback_token="device-tok"))
    assert client.posts == [
        ("https://idp.test/v1/nodes/heartbeat", "Bearer stale-user"),
        ("https://idp.test/v1/nodes/device/heartbeat", "Bearer device-tok"),
    ]


def test_resume_passes_the_device_token_as_fallback_for_an_identity_record(monkeypatch):
    """Since 2026-10-14 a signed-in `adk enroll` keeps its device token too, so a
    lapsed sign-in falls back to beating as the device instead of 401 forever. A
    record with no device token gets no fallback."""
    from adk import fleet_enroll
    rec = {"node_id": NODE, "bearer_token": "device-tok", "enrolled_via": "pairing-code",
           "mode": "rich", "node_class": "sovereign"}
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: rec)
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    calls = []
    monkeypatch.setattr(enrollment, "start_heartbeat",
                        lambda base, tok, nid, **kw: calls.append((tok, kw)) or True)
    enrollment.resume_heartbeat("user-tok", default_base_url="https://idp.test")
    assert calls[-1][0] == "user-tok" and calls[-1][1]["device_fallback_token"] == "device-tok"
    rec["enrolled_via"] = "device-flow"
    enrollment.resume_heartbeat("user-tok", default_base_url="https://idp.test")
    assert calls[-1][1]["device_fallback_token"] == "device-tok"
    del rec["bearer_token"]
    enrollment.resume_heartbeat("user-tok", default_base_url="https://idp.test")
    assert "device_fallback_token" not in calls[-1][1]


# ── advertise-inference probe_url: where this host reaches the advertised server ──


def test_advertise_inference_accepts_and_persists_a_probe_url(monkeypatch, _home):
    monkeypatch.delenv("AITHER_NODE_INFERENCE_URL", raising=False)
    monkeypatch.delenv("AITHER_NODE_INFERENCE_PROBE_URL", raising=False)
    probed = []
    monkeypatch.setattr(enrollment, "_openai_models",
                        lambda base: probed.append(base) or (True, ["m"]))
    monkeypatch.setattr(enrollment, "_fingerprint", lambda base: "llama-server")
    args = {"url": "http://192.168.1.122:8091", "probe_url": "http://127.0.0.1:8091/v1"}
    assert node_commands.verify(_cmd(verb="advertise-inference", args=args), KEY, NODE,
                                seen=[]) == ""
    out = node_commands._advertise_inference(args)
    assert out["ok"] is True and out["probe_url"] == "http://127.0.0.1:8091"
    stored = json.loads((_home / "node-inference.json").read_text())
    assert stored == {"url": "http://192.168.1.122:8091",
                      "probe_url": "http://127.0.0.1:8091", "set_at": stored["set_at"]}
    probe = enrollment.probe_inference()
    assert probe.inference_url == "http://192.168.1.122:8091" and probe.ready is True
    assert probed == ["http://127.0.0.1:8091"]


@pytest.mark.parametrize("args", [
    {"url": "http://10.0.0.5:8114", "probe_url": "ftp://127.0.0.1:8091"},
    {"url": "http://10.0.0.5:8114", "probe_url": "http://127.0.0.1"},
    {"url": "http://10.0.0.5:8114", "probe_url": 8091},
    {"url": "http://10.0.0.5:8114", "probe_url": "http://127.0.0.1:8091", "x": "y"},
    {"probe_url": "http://127.0.0.1:8091"},
])
def test_advertise_inference_refuses_a_bad_probe_url(args):
    why = node_commands.verify(_cmd(verb="advertise-inference", args=args), KEY, NODE,
                               seen=[])
    assert why, args


# ── mesh-lend-on / mesh-lend-off: adk mesh lend, by signed command ──────────


def test_mesh_lend_on_with_a_whole_gb_verifies():
    assert node_commands.verify(_cmd(verb="mesh-lend-on", args={"gb": "24"}), KEY, NODE,
                                seen=[]) == ""
    for gb in ("1", "9", "10", "99", "100", "199", "249", "256"):
        assert node_commands.verify(_cmd(verb="mesh-lend-on", args={"gb": gb}), KEY, NODE,
                                    seen=[]) == "", gb
    assert node_commands.verify(_cmd(verb="mesh-lend-off", args={}), KEY, NODE, seen=[]) == ""


@pytest.mark.parametrize("args", [
    {"gb": "0"}, {"gb": "300"}, {"gb": "257"}, {"gb": "24; rm -rf /"}, {"gb": "024"},
    {"gb": "-1"}, {"gb": "+24"}, {"gb": "24.5"}, {"gb": " 24"}, {"gb": "24\n"},
    {"gb": "２４"}, {"gb": ""}, {"gb": 24}, {"gb": None}, {},
    {"gb": "24", "pool": "x"}, {"gb": "24", "registry_url": "https://evil"},
    {"gb": "24", "port": "22"},
])
def test_mesh_lend_on_refuses_anything_but_one_whole_gb(args):
    ran = []
    cmd = _cmd(verb="mesh-lend-on", args=args)
    assert node_commands.verify(cmd, KEY, NODE, seen=[]) != "", args
    assert node_commands.run_commands([cmd], NODE, KEY, handlers=_handlers(ran)) == []
    assert ran == []


@pytest.mark.parametrize("args", [{"gb": "24"}, {"via": "lan"}, {"x": ""}])
def test_mesh_lend_off_takes_no_argument(args):
    assert node_commands.verify(_cmd(verb="mesh-lend-off", args=args), KEY, NODE,
                                seen=[]) != ""


def test_mesh_lend_on_calls_mesh_lend_with_the_hosts_own_defaults(monkeypatch):
    from adk import mesh_lend
    seen = []

    def lend(ns, say=print):
        seen.append(vars(ns).copy())
        say("lending 24576 MB on 100.64.0.51:50052")
        return 0
    monkeypatch.setattr(mesh_lend, "cmd_lend", lend)
    monkeypatch.setattr(mesh_lend, "read_state", lambda: None)
    (res,) = node_commands.run_commands(
        [_cmd(verb="mesh-lend-on", args={"gb": "24"})], NODE, KEY)
    assert res["ok"] is True
    out = json.loads(res["output"])
    assert out["gb"] == 24 and "lending 24576 MB" in out["output"]
    assert out["status"]["lending"] is False
    (ns,) = seen
    assert ns["gb"] == 24 and isinstance(ns["gb"], int)
    assert (ns["port"], ns["pool"], ns["registry_url"], ns["coordinator"]) == (
        mesh_lend.DEFAULT_PORT, "", "", "")
    assert ns["status"] is False and ns["stop"] is False


def test_mesh_lend_on_handler_rechecks_gb_and_a_failed_lend_is_not_ok(monkeypatch):
    from adk import mesh_lend
    monkeypatch.setattr(mesh_lend, "cmd_lend", lambda ns, say=None: (_ for _ in ()).throw(
        AssertionError("must not run")))
    assert node_commands._mesh_lend_on({"gb": "24; rm"})["ok"] is False
    monkeypatch.setattr(mesh_lend, "cmd_lend", lambda ns, say=None: 2)
    monkeypatch.setattr(mesh_lend, "read_state", lambda: None)
    out = node_commands._mesh_lend_on({"gb": "24"})
    assert out["ok"] is False and out["rc"] == 2


def test_mesh_lend_off_stops_and_diagnostics_show_the_lend(monkeypatch):
    from adk import mesh_lend
    stopped = []
    monkeypatch.setattr(mesh_lend, "cmd_stop", lambda ns, say=None: stopped.append(ns.stop) or 0)
    monkeypatch.setattr(mesh_lend, "read_state", lambda: None)
    (res,) = node_commands.run_commands(
        [_cmd(verb="mesh-lend-off", args={})], NODE, KEY)
    assert res["ok"] is True and stopped == [True]
    diag = node_commands._diagnostics({})
    assert diag["mesh_lend"]["lending"] is False and diag["mesh_lend"]["rc"] == 0


def test_mesh_lend_handlers_never_swap_the_process_streams(monkeypatch):
    """What another thread prints while a lend runs must not land in the signed result:
    the handler collects mesh_lend's own lines through its ``say`` sink and leaves
    sys.stdout / sys.stderr alone."""
    import sys

    from adk import mesh_lend
    streams = (sys.stdout, sys.stderr)

    def lend(ns, say=None):
        assert (sys.stdout, sys.stderr) == streams, "the handler swapped a process stream"
        print("another thread's secret line")
        say("lending 24576 MB", False)
        say("not announced", True)
        return 0

    def stop(ns, say=None):
        assert (sys.stdout, sys.stderr) == streams, "the handler swapped a process stream"
        say("stopped", False)
        return 0
    monkeypatch.setattr(mesh_lend, "cmd_lend", lend)
    monkeypatch.setattr(mesh_lend, "cmd_stop", stop)
    monkeypatch.setattr(mesh_lend, "read_state", lambda: None)
    on = node_commands._mesh_lend_on({"gb": "24"})
    assert on["ok"] is True, on
    assert "lending 24576 MB" in on["output"] and "error: not announced" in on["output"]
    assert "secret" not in on["output"]
    off = node_commands._mesh_lend_off({})
    assert off["ok"] is True and off["output"] == "stopped"


def test_mesh_lend_reports_through_say_not_print(monkeypatch, capsys):
    """adk.mesh_lend's command functions report every line through ``say`` when given
    one -- nothing reaches the process's stdout/stderr."""
    from adk import mesh_lend
    monkeypatch.setattr(mesh_lend, "read_state", lambda: None)
    lines = []
    assert mesh_lend.cmd_status(node_commands._mesh_lend_args(status=True, json=True),
                                say=lambda t, e=False: lines.append(t)) == 0
    assert json.loads(lines[0]) == {"lending": False}
    lines.clear()
    rc = mesh_lend.cmd_lend(node_commands._mesh_lend_args(gb=24),
                            overlay={"joined": False, "code": "not_running"},
                            say=lambda t, e=False: lines.append((t, e)))
    assert rc == 2 and lines and lines[0][1] is True and "not on the mesh" in lines[0][0]
    assert capsys.readouterr() == ("", "")
    assert mesh_lend.status_dict() == {"lending": False}
