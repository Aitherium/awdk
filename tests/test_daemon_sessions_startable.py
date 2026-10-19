"""``GET /sessions/startable``: what this machine can start, for the Agents app's Start.

``detect()`` reports a relay harness (aither, group) as installed unconditionally, so a
remote picker offered Aither on a box with no Genesis route and the first turn died
"Connection refused" (spark, 2026-10-19). Pinned here:
  - a relay harness is offered only when its relay answers; an uninstalled CLI never is
  - entitlement-gated harnesses follow the caller
  - the route is reachable with the LINK's scoped token (it is under /sessions), and is
    not swallowed by ``/sessions/{session_id}``
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"
LINK_TOKEN = "link-bearer-for-tests-only"

RELAY, CLI = "http-stream", "stream-json"
ROWS = [
    {"id": "claude", "label": "Claude Code", "transport": CLI, "installed": False},
    {"id": "aither", "label": "Aither (sovereign agent)", "transport": RELAY, "installed": True},
    {"id": "group", "label": "Group", "transport": RELAY, "installed": True},
    {"id": "awdk", "label": "awdk (local agent loop)", "transport": RELAY, "installed": True},
    {"id": "paid", "label": "Paid", "transport": CLI, "installed": True,
     "requires_entitlement": "harness:paid"},
]


def test_relay_harnesses_need_their_relay_and_uninstalled_clis_never_show():
    from adk.harnesses.daemon import startable_harnesses

    allow_all = lambda r: True  # noqa: E731
    def ids(may_drive, up):
        rows = startable_harnesses(ROWS, may_drive=may_drive, relay_ok=lambda hid: hid in up)
        return [h["id"] for h in rows]

    assert ids(allow_all, set()) == ["paid"]                  # no relay answers (spark)
    assert ids(allow_all, {"awdk"}) == ["awdk", "paid"]       # its own loop is up
    assert ids(allow_all, {"aither", "group", "awdk"}) == ["aither", "group", "awdk", "paid"]
    no_paid = lambda r: not r.get("requires_entitlement")  # noqa: E731
    assert ids(no_paid, {"awdk"}) == ["awdk"]
    first = startable_harnesses(ROWS, may_drive=allow_all, relay_ok=lambda hid: hid == "awdk")[0]
    assert first == {"id": "awdk", "label": "awdk (local agent loop)"}


def test_each_relay_harness_is_judged_by_its_own_target():
    from adk.harnesses import daemon
    from adk.harnesses.agents import ADK_URL, GENESIS_URL

    assert daemon._relay_base("awdk") == ADK_URL
    assert daemon._relay_base("aither") == daemon._relay_base("group") == GENESIS_URL


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    registry_path = tmp_path / "harness_tokens.json"
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry_path))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    registry_path.write_text(json.dumps({
        hashlib.sha256(LINK_TOKEN.encode()).hexdigest(): {
            "principal": "node-link", "plan": "link", "paths": list(daemon.SCOPED_LINK_PATHS),
        },
    }), encoding="utf-8")
    rows = staticmethod(lambda with_version=False: [dict(r) for r in ROWS])
    monkeypatch.setattr(SessionManager, "harnesses", rows)
    monkeypatch.setattr(daemon, "_relay_reachable", lambda base, now=None: base == "http://loop")
    monkeypatch.setattr(
        daemon, "_relay_base", lambda hid: "http://loop" if hid == "awdk" else "http://g")
    app = daemon.create_app(manager=SessionManager(root=tmp_path / "sessions"), token=TOKEN)
    return TestClient(app)


def test_the_route_answers_the_link_token_and_is_not_a_session_id(client):
    r = client.get("/sessions/startable", headers={"Authorization": f"Bearer {LINK_TOKEN}"})
    assert r.status_code == 200, r.text
    # the link principal holds no entitlement: the gated harness is not offered to it
    assert [h["id"] for h in r.json()["harnesses"]] == ["awdk"]
    root = client.get("/sessions/startable", headers={"Authorization": f"Bearer {TOKEN}"})
    assert [h["id"] for h in root.json()["harnesses"]] == ["awdk", "paid"]
    assert client.get("/sessions/startable").status_code == 401


def test_the_relay_probe_is_cached_and_a_refusal_is_false(monkeypatch):
    import adk.harnesses.daemon as daemon

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("refused")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(daemon, "_relay_probes", {})
    assert daemon._relay_reachable("http://x", now=1000.0) is False
    assert daemon._relay_reachable("http://x", now=1010.0) is False
    assert len(calls) == 1
    assert daemon._relay_reachable("http://y", now=1010.0) is False   # its own probe
    assert len(calls) == 2
