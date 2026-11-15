"""The daemon's /desk proxy covers every route the gateway's awdesk_* tools call (2026-11-09).

The fleet cannot reach awdesk directly (it binds 127.0.0.1; from podman,
host.docker.internal refuses it on every port), so mcp_awdesk goes through
http://host.docker.internal:8362/desk with a bearer scoped to /desk. Pins: the
three new routes forward exactly what they should, fleet actions outside the
table are never forwarded, and a /desk-scoped bearer reaches /desk but not
/sessions.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-testing-only"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    monkeypatch.setenv("AITHER_HOME_OVERRIDE", str(tmp_path / "nohome"))

    import urllib.request

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(directory_mod, "_directory",
                        directory_mod.SessionDirectory(discover_fn=lambda: []))
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    monkeypatch.setattr(daemon, "get_bridge_url", lambda: "http://desk.test:1")

    sent = []

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append({"method": req.get_method(), "url": req.full_url, "timeout": timeout,
                     "body": json.loads(req.data) if req.data else None})
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"), token=TOKEN)
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    return {"client": client, "sent": sent, "daemon": daemon, "tmp": tmp_path}


def test_commands_list_and_run_forward_only_id_and_arg(env):
    c, sent = env["client"], env["sent"]
    assert c.get("/desk/commands").status_code == 200
    r = c.post("/desk/commands/run", json={"id": "fleet.status", "arg": "x", "evil": 1})
    assert r.status_code == 200
    assert sent[0]["url"] == "http://desk.test:1/commands" and sent[0]["method"] == "GET"
    assert sent[1]["url"] == "http://desk.test:1/commands/run"
    assert sent[1]["body"] == {"id": "fleet.status", "arg": "x"}
    assert c.post("/desk/commands/run", json={}).status_code == 400


def test_fleet_actions_are_validated_and_carry_their_timeout(env):
    c, sent = env["client"], env["sent"]
    assert c.post("/desk/fleet/inference-on").status_code == 200
    assert sent[-1]["url"] == "http://desk.test:1/fleet/inference-on"
    assert sent[-1]["timeout"] == 3960.0
    n = len(sent)
    assert c.post("/desk/fleet/shutdown-everything").status_code == 404
    assert len(sent) == n, "an unknown fleet action must never reach the desk"


def test_a_desk_scoped_bearer_reaches_desk_but_not_sessions(env):
    daemon = env["daemon"]
    tok = daemon.mint_scoped_token("mcpgateway", paths=("/desk",), label="mcpgateway-desk",
                                   path=env["tmp"] / "harness_tokens.json")
    c = env["client"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get("/desk/commands", headers=h).status_code == 200
    assert c.post("/desk/fleet/gpu-wake", headers=h).status_code == 200
    assert c.get("/sessions", headers=h).status_code in (401, 403)
