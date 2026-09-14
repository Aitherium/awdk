"""adk.desk_bridge -- the awdesk bridge client shared by `adk fleet`/`adk desk`.

Every test uses a fake transport/runner: no server, no wsl, no network.
"""

from __future__ import annotations

import json

import pytest
from adk import desk_bridge as db


def test_build_request_status_is_a_get_and_panel_maps_to_open():
    assert db.build_request("status") == ("GET", "/fleet/status", None)
    assert db.build_request("down") == ("POST", "/fleet/down", None)
    assert db.build_request("UP") == ("POST", "/fleet/up", None)
    assert db.build_request("panel") == ("POST", "/fleet/open", None)
    with pytest.raises(ValueError):
        db.build_request("nuke")


def test_command_and_history_requests():
    m, p, body = db.build_command_request("fleet status")
    assert (m, p) == ("POST", "/command")
    assert json.loads(body) == {"text": "fleet status"}
    assert db.build_history_request(7) == ("GET", "/command/history?limit=7", None)


def test_fallback_argv_maps_owner_verbs_to_the_distro_script_and_never_for_panel():
    cmd = "wsl -d Debian -u root python3 /srv/quiesce.py"
    assert db.build_fallback_argv("down", cmd)[-3:] == ["quiesce", "--all", "--json"]
    assert db.build_fallback_argv("up", cmd)[-2:] == ["resume", "--json"]
    assert db.build_fallback_argv("gaming", cmd)[-3:] == ["quiesce", "--deep", "--json"]
    assert db.build_fallback_argv("panel", cmd) == []
    assert db.build_fallback_argv("down", "") == []


def test_exit_codes():
    assert db.exit_code_for(200, {"ok": True}) == 0
    assert db.exit_code_for(202, {"id": "x"}) == 0
    assert db.exit_code_for(200, {"ok": False, "busy": "down"}) == 1
    assert db.exit_code_for(409, {"ok": False}) == 1
    assert db.exit_code_for(503, {"ok": False, "cannotJudge": True}) == 2
    assert db.exit_code_for(200, {"ok": False, "cannotJudge": True}) == 2


def _client(responses, fallback_cmd="", runner=None):
    calls = []

    def transport(method, url, body, timeout):
        calls.append((method, url, body, timeout))
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    c = db.DeskBridgeClient(url="http://127.0.0.1:1", transport=transport,
                            fallback_cmd=fallback_cmd, runner=runner, sleep=lambda s: None)
    return c, calls


def test_call_fleet_status_via_bridge_uses_get_and_long_timeouts_for_up():
    c, calls = _client([(200, json.dumps({"ok": True, "fleet": {"running": 0}}))])
    rc, doc = c.call_fleet("status")
    assert rc == 0 and doc["source"] == "bridge" and doc["fleet"]["running"] == 0
    assert calls[0][0] == "GET" and calls[0][1].endswith("/fleet/status")
    assert calls[0][3] == db.TIMEOUTS["status"]
    c, calls = _client([(200, json.dumps({"ok": True}))])
    c.call_fleet("up")
    assert calls[0][3] >= 1800


def test_call_fleet_refused_and_busy_map_to_exit_1():
    c, _ = _client([(409, json.dumps({"ok": False, "busy": "down", "error": "busy"}))])
    assert c.call_fleet("up")[0] == 1


def test_call_fleet_falls_back_to_the_distro_script_when_the_bridge_is_down():
    ran = []

    def runner(argv, timeout):
        ran.append((argv, timeout))
        return 0, "progress noise\n" + json.dumps({"ok": True, "fleet_running_after": 0})

    c, _ = _client([ConnectionError("refused")], fallback_cmd="python3 /srv/q.py", runner=runner)
    rc, doc = c.call_fleet("down")
    assert rc == 0 and doc["source"] == "fallback" and doc["fleet_running_after"] == 0
    assert ran[0][0][-3:] == ["quiesce", "--all", "--json"]


def test_bridge_down_and_no_fallback_is_cannot_judge_not_ok():
    c, _ = _client([ConnectionError("refused")])
    rc, doc = c.call_fleet("status")
    assert rc == 2 and doc["cannotJudge"] is True and "AWDESK_FLEET_FALLBACK" in doc["error"]


def test_fallback_cannot_judge_stays_exit_2():
    def runner(argv, timeout):
        return 2, json.dumps({"error": "podman ps failed", "verdict": "CANNOT_JUDGE"})

    c, _ = _client([ConnectionError("refused")], fallback_cmd="python3 /srv/q.py", runner=runner)
    rc, doc = c.call_fleet("status")
    assert rc == 2 and doc["cannotJudge"] is True


def test_command_202_then_poll_until_reply():
    history_empty = (200, json.dumps({"items": [{"id": "c1", "text": "do x", "reply": None}]}))
    history_done = (200, json.dumps({"items": [{"id": "c1", "text": "do x", "reply": "done"}]}))
    c, calls = _client([(202, json.dumps({"id": "c1"})), history_empty, history_empty,
                        history_done])
    rc, doc = c.call_command("do x")
    assert rc == 0 and doc["pending"] is True and doc["id"] == "c1"
    rc, item = c.poll_command_reply("c1", max_wait_s=10, interval_s=2)
    assert rc == 0 and item["reply"] == "done"
    assert sum(1 for m, u, *_ in calls if "/command/history" in u) == 3


def test_poll_times_out_with_exit_1():
    empty = (200, json.dumps({"items": []}))
    c, _ = _client([empty] * 10)
    rc, doc = c.poll_command_reply("nope", max_wait_s=4, interval_s=2)
    assert rc == 1 and "no reply" in doc["error"]


def test_command_bridge_down_is_cannot_judge():
    c, _ = _client([ConnectionError("refused")])
    rc, doc = c.call_command("hello")
    assert rc == 2 and doc["cannotJudge"] is True


# ---- bearer on mutators (2026-09-08) ------------------------------------------


def test_bridge_token_env_then_file_then_empty(tmp_path):
    assert db.bridge_token(env={}, home=str(tmp_path)) == ""
    (tmp_path / ".aither").mkdir()
    (tmp_path / ".aither" / "harness_token").write_text("  from-file " + chr(10), encoding="utf-8")
    assert db.bridge_token(env={}, home=str(tmp_path)) == "from-file"
    assert db.bridge_token(env={"AITHER_HARNESS_TOKEN": " from-env "},
                           home=str(tmp_path)) == "from-env"


def test_a_401_from_the_bridge_is_cannot_judge_and_names_the_fix():
    c, _ = _client([(401, json.dumps({"ok": False, "error": "bearer required"}))])
    rc, doc = c.call_fleet("down")
    assert rc == 2 and doc["cannotJudge"] and "AITHER_HARNESS_TOKEN" in doc["error"]
    c, _ = _client([(401, json.dumps({"ok": False}))])
    rc, doc = c.call_command("ping")
    assert rc == 2 and "AITHER_HARNESS_TOKEN" in doc["error"] and doc["pending"] is False


def test_a_fleet_503_verdict_keeps_its_own_cannot_judge_reason():
    # 503 is also the bridge's CANNOT JUDGE status for a real verdict; only the
    # no-token 503 (no cannotJudge in the body) gets the auth explanation.
    c, _ = _client([(503, json.dumps({"ok": False, "cannotJudge": True,
                                     "error": "inspect rc=1"}))])
    rc, doc = c.call_fleet("status")
    assert rc == 2 and doc["error"] == "inspect rc=1"
    c, _ = _client([(503, json.dumps({"ok": False, "error": "no bridge token configured"}))])
    rc, doc = c.call_fleet("down")
    assert rc == 2 and "no bridge token" in doc["error"]


# ---- desktop surfaces (2026-09-08) --------------------------------------------


def test_build_desktop_request_status_is_a_get_and_surfaces_post():
    assert db.build_desktop_request("status") == ("GET", "/desktop/status", None)
    assert db.build_desktop_request("overlay") == ("POST", "/desktop/overlay", None)
    assert db.build_desktop_request("app") == ("POST", "/desktop/app", None)
    with pytest.raises(ValueError):
        db.build_desktop_request("taskbar")


def test_call_desktop_reports_which_surfaces_are_open_and_an_old_desk_is_cannot_judge():
    body = {"ok": True, "opened": "app", "overlay": {"open": False}, "app": {"open": True}}
    c, calls = _client([(200, json.dumps(body))])
    rc, doc = c.call_desktop("app")
    assert rc == 0 and doc["app"]["open"] is True and calls[0][0] == "POST"
    assert calls[0][1].endswith("/desktop/app") and calls[0][3] == db.TIMEOUTS["desktop"]
    c, _ = _client([(404, "")])
    rc, doc = c.call_desktop("overlay")
    assert rc == 2 and "older build" in doc["error"]
    c, _ = _client([ConnectionError("refused")])
    rc, doc = c.call_desktop()
    assert rc == 2 and "awdesk is not running" in doc["error"]
