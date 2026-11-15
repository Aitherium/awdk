"""`adk agent twin deploy --self-host`: the zero-cost deploy row, measured 2026-11-12.

Against a framework that served an agent over HTTP in 15 s, the kit lost the row
"deploy an agent at zero cost from this machine":

* the twin dry run exited 1 "Not ready" for a scaffolded agent: it wanted a public
  https URL (the platform refuses a private one) and listed an unset
  ``AITHER_SERVER_API_KEY`` as something to fix, though nothing required it;
* nothing started the agent: the command only registered a server the user had to
  run and expose themselves;
* on a machine whose ``AITHER_API_URL`` names the MCP gateway, every twin verb
  answered a bare ``404 Not Found``.

These tests pin the plan and prerequisite logic and the registration, with the
platform API and the local service mocked.
"""

from __future__ import annotations

import json
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest

from adk import agent_daemon, twin_client
from adk import twin_selfhost as sh

MESH_IP = "100.64.0.9"
BASE = "https://api.example"


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def kit(monkeypatch, tmp_path):
    """A signed-in kit on a machine with a mesh address, a model and free ports. The
    platform and the local service are fakes that record what was asked of them."""
    home = tmp_path / "aither-home"
    home.mkdir()
    (home / "session-bearer").write_text("session-token-abc\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    for var in ("AITHER_API_KEY", "AITHER_MCP_KEY", "AITHER_SERVER_API_KEY",
                "AITHER_MESH_OVERLAY_IP", "AITHER_GENESIS_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_API_URL", BASE)
    box = SimpleNamespace(home=home, tmp=tmp_path, seen=[], started=[], killed=[],
                          alive=set(), answer=lambda method, url: _Resp(),
                          start_ok=True)

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None):
        box.seen.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        return box.answer(method, url)

    def fake_start(**kw):
        box.started.append(kw)
        if not box.start_ok:
            return {"ok": False, "pid": None, "port": kw["port"], "log": "x.log",
                    "error": "the agent did not answer /health"}
        box.alive.add(4242)
        return {"ok": True, "pid": 4242, "port": kw["port"], "log": "x.log", "error": "",
                "started_at": 1.0}

    def fake_kill(pid):
        box.killed.append(pid)
        box.alive.discard(pid)
        return True

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(sh, "start", fake_start)
    monkeypatch.setattr(sh, "overlay_ip", lambda env=None: MESH_IP)
    monkeypatch.setattr(sh, "port_owner", lambda port: None)
    monkeypatch.setattr(twin_client, "_local_backend", lambda: (True, "configured:test"))
    monkeypatch.setattr(agent_daemon, "pid_alive", lambda pid: pid in box.alive)
    monkeypatch.setattr(agent_daemon, "kill_pid", fake_kill)
    return box


def _project(tmp_path, identity="row-twin", port=8081, model=""):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (proj / "agent.py").write_text("# agent\n", encoding="utf-8")
    (proj / "config.yaml").write_text(
        f"identity: {identity}\nport: {port}\n" + (f"model: {model}\n" if model else ""),
        encoding="utf-8")
    return proj


def _main(monkeypatch, argv) -> int:
    monkeypatch.setattr(sys, "argv", ["adk", *argv])
    from adk import cli

    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def _args(proj, **kw):
    base = dict(project=str(proj), name="", invoke_url="", reach="", model="", port=0,
                no_bind=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _plan(proj, **kw):
    """A plan with every probe injected: nothing on the real machine is read."""
    probes = dict(env={}, base=BASE, credential_source="adk login session", mesh_ip=MESH_IP,
                  port_owner=lambda p: None, backend=lambda: (True, "configured:test"),
                  state=None)
    for key in list(kw):
        if key in probes:
            probes[key] = kw.pop(key)
    return twin_client.build_selfhost_plan(_args(proj, **kw), **probes)


def _failed(plan):
    return {c["check"] for c in plan["checks"] if not c["ok"] and c["required"]}


# ── reach: running here is one fact, registered with the platform another ────
def test_a_machine_on_the_mesh_registers_its_mesh_address_not_a_public_url():
    r = sh.choose_reach("", "", MESH_IP)
    assert (r["reach"], r["ok"], r["platform"]) == ("mesh", True, True)
    assert r["url_host"] == MESH_IP and r["bind_host"] == "0.0.0.0"


def test_a_machine_off_the_mesh_is_told_the_two_commands_that_fix_it():
    r = sh.choose_reach("", "", "")
    assert r["ok"] is False and r["reach"] == "mesh"
    assert r["fix"][0].startswith("adk mesh join")
    assert "--reach local" in r["fix"][1]
    # It is not started bound to every interface for a registration that cannot happen.
    assert r["bind_host"] == "127.0.0.1"


def test_local_reach_is_always_ready_and_never_registered():
    r = sh.choose_reach("local", "", "")
    assert r["ok"] is True and r["platform"] is False and r["bind_host"] == "127.0.0.1"


def test_tunnel_reach_needs_the_url_of_a_tunnel():
    assert sh.choose_reach("tunnel", "", MESH_IP)["ok"] is False
    named = sh.choose_reach("", "https://my-agent.example.com", MESH_IP)
    assert named["reach"] == "tunnel" and named["ok"] is True


@pytest.mark.parametrize("value,expected", [
    ("100.64.0.31", True), ("100.127.255.254", True), ("100.63.0.1", False),
    ("100.128.0.1", False), ("127.0.0.1", False), ("10.0.0.5", False), ("nope", False)])
def test_only_the_overlay_range_is_a_mesh_address(value, expected):
    assert sh.is_mesh_ip(value) is expected


def test_overlay_ip_ignores_a_configured_address_outside_the_mesh():
    assert sh.overlay_ip({"AITHER_MESH_OVERLAY_IP": "192.168.1.9"}) == ""
    assert sh.overlay_ip({"AITHER_MESH_OVERLAY_IP": MESH_IP}) == MESH_IP


# ── the plan and its prerequisites ───────────────────────────────────────────
def test_a_scaffolded_agent_is_ready_with_no_public_url_and_no_key(tmp_path):
    plan = _plan(_project(tmp_path))
    assert plan["ready"] is True and _failed(plan) == set()
    body = plan["request"]["body"]
    assert body["invoke_url"] == f"http://{MESH_IP}:8081" and body["reach"] == "mesh"
    assert body["provider"] == "self_hosted" and body["bind"] is True
    assert plan["reach"]["public_url_needed"] is False
    assert plan["service"]["action"] == "start"
    assert plan["service"]["command"] == "adk run --port 8081 --host 0.0.0.0"
    assert plan["cost"]["this_command_usd"] == 0
    # The bearer does not exist yet and a dry run must not mint one.
    assert body["token"] == twin_client.PENDING_TOKEN
    assert twin_client.redacted(plan)["request"]["body"]["token"] == "<redacted>"


def test_every_missing_prerequisite_is_one_line_with_its_command(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    plan = _plan(empty, credential_source="", mesh_ip="", backend=lambda: (False, ""),
                 port_owner=lambda p: "other", port=9100)
    assert plan["ready"] is False
    assert _failed(plan) == {"project", "name", "model", "port", "reach", "sign-in"}
    lines = twin_client.missing(plan)
    assert len(lines) == 6 and all("\n" not in line for line in lines)
    by = {line.split(":")[0]: line for line in lines}
    assert "fix: `adk init <name>`" in by["project"]
    assert "fix: `adk setup`" in by["model"]
    assert "fix: `adk login`" in by["sign-in"]
    assert "fix: `adk mesh join`" in by["reach"] and "--reach local" in by["reach"]
    assert "--port" in by["port"]


def test_the_callback_bearer_is_never_a_reason_to_be_unready(tmp_path):
    plan = _plan(_project(tmp_path))
    bearer = next(c for c in plan["checks"] if c["check"] == "callback bearer")
    assert bearer["required"] is False and "minted on deploy" in bearer["detail"]
    local = _plan(_project(tmp_path), reach="local")
    bearer = next(c for c in local["checks"] if c["check"] == "callback bearer")
    assert "not needed" in bearer["detail"]


def test_local_reach_needs_no_sign_in_and_plans_no_request(tmp_path):
    plan = _plan(_project(tmp_path), reach="local", credential_source="", mesh_ip="")
    assert plan["ready"] is True and plan["request"] is None
    assert plan["service"]["bind"] == "127.0.0.1"
    assert plan["reach"]["registered_with_platform"] is False


def test_signed_out_blocks_only_a_twin_that_registers(tmp_path):
    plan = _plan(_project(tmp_path), credential_source="")
    assert _failed(plan) == {"sign-in"}


def test_a_taken_default_port_steps_up_and_an_explicit_one_is_refused(tmp_path):
    proj = _project(tmp_path, port=8081)
    stepped = _plan(proj, port_owner=lambda p: "other" if p == 8081 else None)
    assert stepped["ready"] is True and stepped["service"]["port"] == 8082
    assert stepped["request"]["body"]["invoke_url"].endswith(":8082")
    refused = _plan(proj, port=8081, port_owner=lambda p: "other")
    assert _failed(refused) == {"port"}


def test_a_private_url_is_not_a_tunnel(tmp_path):
    plan = _plan(_project(tmp_path), reach="tunnel", invoke_url="https://192.168.1.5")
    assert _failed(plan) == {"reach"}
    assert "--reach mesh" in twin_client.missing(plan)[0]


def test_a_twin_that_already_runs_is_kept_not_started_twice(tmp_path):
    state = {"name": "row-twin", "pid": 77, "port": 8090, "host": "0.0.0.0"}
    plan = twin_client.build_selfhost_plan(
        _args(_project(tmp_path)), env={}, base=BASE, credential_source="s",
        mesh_ip=MESH_IP, port_owner=lambda p: "adk", backend=lambda: (True, "m"),
        state=state, alive=lambda pid: pid == 77)
    assert plan["ready"] is True
    assert plan["service"]["action"].startswith("keep") and plan["service"]["port"] == 8090
    assert plan["request"]["body"]["invoke_url"] == f"http://{MESH_IP}:8090"


# ── dry run ──────────────────────────────────────────────────────────────────
def test_dry_run_exits_0_prints_the_plan_and_touches_nothing(kit, monkeypatch, capsys):
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project",
                               str(proj), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert kit.seen == [] and kit.started == []      # nothing sent, nothing started
    assert not sh.twins_dir().exists()               # no record, no bearer minted
    assert "The plan is ready." in out and "Not ready" not in out
    assert f"mesh address {MESH_IP}" in out and "[--]" not in out


def test_dry_run_off_the_mesh_exits_1_and_names_each_fix(kit, monkeypatch, capsys):
    monkeypatch.setattr(sh, "overlay_ip", lambda env=None: "")
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project",
                               str(proj), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1 and kit.seen == [] and kit.started == []
    not_ready = out.split("Not ready:")[1].strip().splitlines()
    assert len(not_ready) == 1
    assert "adk mesh join" in not_ready[0] and "--reach local" in not_ready[0]


def test_the_old_form_points_at_self_host_and_shows_the_bearer_as_a_note(kit, monkeypatch,
                                                                         capsys):
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--project", str(proj), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1 and kit.seen == []
    assert "fix: `adk agent twin deploy --self-host`" in out
    bearer_line = next(line for line in out.splitlines() if "callback bearer" in line)
    assert bearer_line.strip().startswith("[i ]")    # a note, not something to fix
    assert out.count("[--]") == 1                    # only the URL is missing


# ── deploy: start, register, list, remove ────────────────────────────────────
def test_deploy_starts_the_agent_then_registers_it_with_the_login_session(kit, monkeypatch,
                                                                          capsys):
    proj = _project(kit.tmp, model="bonsai2-27b")
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project", str(proj)])
    out = capsys.readouterr().out
    assert code == 0
    started = kit.started[0]
    assert (started["name"], started["port"], started["host"]) == ("row-twin", 8081, "0.0.0.0")
    assert started["project"] == str(proj) and started["model"] == "bonsai2-27b"
    sent = kit.seen[0]
    assert (sent["method"], sent["url"]) == ("POST", f"{BASE}/v1/agent/agent-endpoints")
    assert sent["headers"]["Authorization"] == "Bearer session-token-abc"
    assert sent["json"]["invoke_url"] == f"http://{MESH_IP}:8081"
    assert sent["json"]["reach"] == "mesh" and sent["json"]["provider"] == "self_hosted"
    # One bearer: minted, stored owner-only, given to the child and to the platform.
    bearer = sh.stored_bearer("row-twin")
    assert len(bearer) >= 32 and sent["json"]["token"] == bearer == started["bearer"]
    assert bearer not in out and twin_client.PENDING_TOKEN not in json.dumps(sent["json"])
    state = sh.read_state("row-twin")
    assert state["registered"] is True and state["pid"] == 4242 and state["reach"] == "mesh"
    assert "token" not in json.dumps(state)
    assert "adk agent twin remove row-twin" in out


def test_no_bind_registers_the_endpoint_only(kit, monkeypatch):
    proj = _project(kit.tmp)
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--no-bind",
                               "--project", str(proj)]) == 0
    assert kit.seen[0]["json"]["bind"] is False


def test_a_refused_registration_stops_what_it_started(kit, monkeypatch, capsys):
    kit.answer = lambda method, url: _Resp(400, {"detail": {"error": "bad_overlay_ip"}})
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project", str(proj)])
    err = capsys.readouterr().err
    assert code == 1 and "not registered" in err and "bad_overlay_ip" in err
    assert kit.killed == [4242]                      # nothing left running
    assert sh.read_state("row-twin") is None and sh.stored_bearer("row-twin") == ""


def test_an_agent_that_does_not_come_up_is_not_registered(kit, monkeypatch, capsys):
    kit.start_ok = False
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project", str(proj)])
    assert code == 1 and kit.seen == []
    assert "not started" in capsys.readouterr().err and sh.read_state("row-twin") is None


def test_local_reach_runs_without_the_platform_and_is_still_listed(kit, monkeypatch, capsys):
    (kit.home / "session-bearer").unlink()           # signed out
    monkeypatch.setattr(sh, "overlay_ip", lambda env=None: "")
    proj = _project(kit.tmp)
    code = _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--reach", "local",
                               "--project", str(proj)])
    assert code == 0 and kit.seen == []              # nothing left the machine
    assert kit.started[0]["host"] == "127.0.0.1" and kit.started[0]["bearer"] == ""
    assert sh.read_state("row-twin")["registered"] is False
    capsys.readouterr()
    assert _main(monkeypatch, ["agent", "twin", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert kit.seen == [] and "adk login" in listed["platform"]
    assert listed["local"][0]["name"] == "row-twin" and listed["local"][0]["running"] is True
    assert listed["local"][0]["registered"] is False
    # Removing it stops the service and asks the platform nothing.
    capsys.readouterr()
    assert _main(monkeypatch, ["agent", "twin", "remove", "row-twin"]) == 0
    assert kit.seen == [] and kit.killed == [4242] and sh.read_state("row-twin") is None


def test_list_shows_the_platform_rows_and_this_machines_record(kit, monkeypatch, capsys):
    proj = _project(kit.tmp)
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project",
                               str(proj)]) == 0
    capsys.readouterr()
    kit.answer = lambda method, url: _Resp(200, {
        "endpoints": [{"name": "row-twin", "reach": "mesh"}], "total": 1})
    assert _main(monkeypatch, ["agent", "twin", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["endpoints"][0]["name"] == "row-twin"
    assert listed["local"][0]["registered"] is True and listed["local"][0]["port"] == 8081


def test_remove_stops_the_service_and_deletes_the_registration(kit, monkeypatch, capsys):
    proj = _project(kit.tmp)
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project",
                               str(proj)]) == 0
    capsys.readouterr()
    assert _main(monkeypatch, ["agent", "twin", "remove", "row-twin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["registration"] == "removed"
    assert out["service"] == {"known": True, "was_running": True, "stopped": True}
    assert (kit.seen[-1]["method"], kit.seen[-1]["url"]) == (
        "DELETE", f"{BASE}/v1/agent/agent-endpoints/row-twin")
    assert kit.killed == [4242]
    assert sh.read_state("row-twin") is None and sh.stored_bearer("row-twin") == ""


def test_remove_keeps_the_record_when_the_platform_refuses(kit, monkeypatch, capsys):
    proj = _project(kit.tmp)
    assert _main(monkeypatch, ["agent", "twin", "deploy", "--self-host", "--project",
                               str(proj)]) == 0
    kit.answer = lambda method, url: _Resp(401, {"error": "auth"})
    assert _main(monkeypatch, ["agent", "twin", "remove", "row-twin"]) == 1
    assert sh.read_state("row-twin") is not None     # still known, so it can be retried


def test_remove_of_an_unknown_twin_exits_1(kit, monkeypatch, capsys):
    kit.answer = lambda method, url: _Resp(404, {"detail": "not found"})
    assert _main(monkeypatch, ["agent", "twin", "remove", "ghost"]) == 1
    assert "no twin named 'ghost'" in capsys.readouterr().err


# ── the base that serves /v1/agent ───────────────────────────────────────────
def test_an_api_url_that_names_the_mcp_gateway_is_not_a_genesis_base(monkeypatch):
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://api.aitherium.com")
    monkeypatch.setenv("AITHER_API_URL", "https://mcp.aitherium.com")
    assert twin_client.agent_base() == "https://api.aitherium.com/api/genesis"
    monkeypatch.setenv("AITHER_API_URL", "https://gw.corp.example")
    monkeypatch.setenv("AITHER_MCP_URL", "https://gw.corp.example/mcp")
    assert twin_client.agent_base() == "https://api.aitherium.com/api/genesis"


def test_any_other_override_is_used_as_given(monkeypatch):
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    monkeypatch.setenv("AITHER_MCP_URL", "https://mcp.aitherium.com/mcp")
    monkeypatch.setenv("AITHER_API_URL", "https://genesis.corp.example")
    assert twin_client.agent_base() == "https://genesis.corp.example"


def test_twin_list_goes_to_the_portal_when_the_api_url_is_the_mcp_gateway(kit, monkeypatch):
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://api.aitherium.com")
    monkeypatch.setenv("AITHER_API_URL", "https://mcp.aitherium.com")
    assert _main(monkeypatch, ["agent", "twin", "list"]) == 0
    assert kit.seen[0]["url"] == "https://api.aitherium.com/api/genesis/v1/agent/agent-endpoints"


# ── the pieces under the plan ────────────────────────────────────────────────
def test_the_callers_own_server_key_wins_and_a_minted_one_is_reused(kit):
    assert sh.ensure_bearer("row-twin", {"AITHER_SERVER_API_KEY": "mine"}) == (
        "mine", "$AITHER_SERVER_API_KEY")
    assert not sh.token_path("row-twin").exists()    # nothing minted when one was given
    first, where = sh.ensure_bearer("row-twin", {})
    assert where == str(sh.token_path("row-twin")) and len(first) >= 32
    assert sh.ensure_bearer("row-twin", {})[0] == first


def test_a_port_held_by_something_that_speaks_no_http_is_not_free(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(agent_daemon, "port_owner", lambda port: None)   # silent on /health
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        if sys.platform == "win32":
            held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert sh.port_owner(port) == "other"
    assert sh.port_owner(port) is None


def test_the_service_runs_adk_run_in_the_project_directory(monkeypatch, tmp_path):
    calls = {}

    class FakeDaemon:
        LOG_DIR = tmp_path

        @staticmethod
        def _ensure_dirs():
            pass

        @staticmethod
        def spawn_detached(argv, log, env=None, cwd=None):
            calls.update(argv=argv, env=env, cwd=cwd, log=log)
            return 31337

        @staticmethod
        def wait_for_health(port, timeout, pid, not_before):
            calls["health"] = (port, pid)
            return True

    res = sh.start(name="row-twin", project=str(tmp_path), port=8123, host="0.0.0.0",
                   model="m1", bearer="b-secret", daemon=FakeDaemon, env={"PATH": "x"})
    assert res["ok"] is True and res["pid"] == 31337
    assert calls["argv"][1:] == ["-m", "adk", "run", "--port", "8123", "--host", "0.0.0.0",
                                 "--model", "m1"]
    assert calls["cwd"] == str(tmp_path)             # agent.py is found there
    assert calls["env"]["AITHER_SERVER_API_KEY"] == "b-secret"
    assert calls["health"] == (8123, 31337)          # only OUR child counts as healthy


def test_a_child_that_never_answers_is_stopped(tmp_path):
    killed = []

    class DeadDaemon:
        LOG_DIR = tmp_path
        _ensure_dirs = staticmethod(lambda: None)
        spawn_detached = staticmethod(lambda argv, log, env=None, cwd=None: 555)
        wait_for_health = staticmethod(lambda port, timeout, pid, not_before: False)
        kill_pid = staticmethod(killed.append)

    res = sh.start(name="t", project=str(tmp_path), port=8124, host="127.0.0.1",
                   daemon=DeadDaemon, env={})
    assert res["ok"] is False and killed == [555] and "did not answer" in res["error"]


def test_spawn_detached_passes_the_working_directory(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        pid = 9

        def __init__(self, argv, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(agent_daemon, "LOG_DIR", tmp_path)
    monkeypatch.setattr(agent_daemon.subprocess, "Popen", FakePopen)
    assert agent_daemon.spawn_detached(["x"], tmp_path / "a.log", cwd=str(tmp_path)) == 9
    assert seen["cwd"] == str(tmp_path)
    seen.clear()
    agent_daemon.spawn_detached(["x"], tmp_path / "a.log")
    assert "cwd" not in seen                          # unchanged for every existing caller
