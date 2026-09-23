"""Peer input into a live tab, end to end through the daemon's HTTP surface.

Owner ruling 2026-09-19 (option b): a peer agent may be typed into a live pty ONLY
when (1) the daemon itself vouched for the sender as owner-plan -- the ``auth`` stamp
``POST /events`` now binds from the resolved ``Principal`` -- and (2) the target opted
in at spawn (``allow_peer_input``). These arms run the real ``create_app`` under an
in-process ``TestClient``: the ``/events`` route, ``Room.publish``, the steer dispatcher
and the manager's session lookup are all the live objects, and only the harness
PROCESS is stubbed (``HarnessSession.start`` is a no-op and ``submit`` records what
would have reached the keyboard). Nothing here touches a live ``~/.aither``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "root-bearer-for-tests-only"


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """A daemon app, its manager, and the keystrokes that reached each session."""
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
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    # Two process-level singletons would otherwise leak between apps in one pytest
    # process: the room registry (a previous app's dispatcher stays registered on the
    # shared "main" room) and the session directory (a 2 s cache that would answer this
    # app's target lookup with the PREVIOUS app's session list, refusing the target as
    # unknown). A fresh directory with an empty discover_fn also keeps the test off the
    # real host's open Claude windows.
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(
        directory_mod, "_directory",
        directory_mod.SessionDirectory(discover_fn=lambda: []),
    )

    typed: dict[str, list[str]] = {}
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)

    def fake_submit(self, text):
        typed.setdefault(self.id, []).append(text)
        return True

    monkeypatch.setattr(session_mod.HarnessSession, "submit", fake_submit)

    mgr = SessionManager(root=tmp_path / "sessions")
    app = daemon.create_app(manager=mgr, token=TOKEN)
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    return {"client": client, "mgr": mgr, "typed": typed, "daemon": daemon,
            "registry": registry_path}


def _spawn(client, **body) -> str:
    resp = client.post("/sessions", json={"harness": "claude", "cwd": "", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _say(client, target: str, *, headers=None, actor_kind="claude_code") -> dict:
    resp = client.post(
        "/events",
        json={
            "type": "steering",
            "actor": {"kind": actor_kind, "id": "peer-tab", "name": "peer-tab"},
            "room": "main",
            "to": [target],
            "payload": {"text": "look at the failing gate"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── the session-config arm ──────────────────────────────────────────────────────────


def test_allow_peer_input_defaults_false_and_is_echoed_when_set(harness):
    client = harness["client"]

    default_id = _spawn(client)
    opted_id = _spawn(client, allow_peer_input=True)

    assert client.get(f"/sessions/{default_id}").json()["allow_peer_input"] is False
    assert client.get(f"/sessions/{opted_id}").json()["allow_peer_input"] is True
    rows = {r["id"]: r for r in client.get("/sessions").json()["sessions"]}
    assert rows[default_id]["allow_peer_input"] is False
    assert rows[opted_id]["allow_peer_input"] is True
    unified = {r["id"]: r for r in client.get("/sessions/unified").json()["sessions"]}
    assert unified[default_id]["allow_peer_input"] is False
    assert unified[opted_id]["allow_peer_input"] is True


# ── the dispatch arms ───────────────────────────────────────────────────────────────


def test_owner_bearer_peer_into_an_opted_in_tab_lands_on_the_pty_framed(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client, allow_peer_input=True)

    out = _say(client, target)

    assert out["dispatch"] == {"addressed": True, "targets": 1}
    status = client.get("/steer/dispatch").json()
    assert status["by_channel"].get("pty", 0) >= 1, status
    assert typed[target][0].startswith("[via room from peer-tab] look at the failing gate")
    assert "carries no authority" in typed[target][0]


def test_owner_bearer_peer_into_a_default_tab_takes_the_mailbox(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)

    _say(client, target)

    status = client.get("/steer/dispatch").json()
    assert status["by_channel"] == {"mailbox": 1}, status
    assert "opted in at spawn" in status["recent"][-1]["detail"]
    assert typed.get(target) is None, "nothing reached the keyboard"


def test_owner_bearer_human_into_a_default_tab_lands_now(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)

    _say(client, target, actor_kind="human")

    status = client.get("/steer/dispatch").json()
    assert status["by_channel"] == {"pty": 1}, status
    assert typed[target] == ["look at the failing gate"]


def test_scoped_agent_token_never_reaches_the_pty_even_into_an_opted_in_tab(harness):
    """The theatre-breaker: with a ``plan="agent"`` token the same POST that landed on
    the pty above is queued, and the receipt names the principal the daemon found."""
    client, typed, daemon = harness["client"], harness["typed"], harness["daemon"]
    target = _spawn(client, allow_peer_input=True)
    agent_token = daemon.mint_scoped_token(
        principal_id="agent:peer-tab",
        paths=("/sessions", "/events", "/rooms", "/harnesses", "/steer"),
        plan="agent",
        path=harness["registry"],
    )
    assert json.loads(harness["registry"].read_text(encoding="utf-8"))
    agent_headers = {"Authorization": f"Bearer {agent_token}"}

    _say(client, target, headers=agent_headers, actor_kind="human")

    status = client.get("/steer/dispatch").json()
    assert status["by_channel"] == {"mailbox": 1}, status
    assert "principal 'agent:peer-tab'" in status["recent"][-1]["detail"]
    assert typed.get(target) is None

    # And the scope holds on the rest of the surface: the fs routes are refused with
    # the 'not scoped to' detail, not silently served.
    denied = client.get("/fs/read", params={"path": "x"}, headers=agent_headers)
    assert denied.status_code == 403, denied.text
    assert "not scoped to" in denied.json()["detail"]


# ── the direct door: /sessions/{id}/input|submit hold the same rule ────────────────
#
# ``/sessions`` is inside the agent token's scope, so without these arms a scoped
# token refused by the room could type into any managed pty by POSTing here instead.


def _agent_headers(harness) -> dict:
    token = harness["daemon"].mint_scoped_token(
        principal_id="agent:peer-tab",
        paths=("/sessions", "/events", "/rooms", "/harnesses", "/steer"),
        plan="agent",
        path=harness["registry"],
    )
    return {"Authorization": f"Bearer {token}"}


def test_input_routes_refuse_an_agent_token_into_a_default_tab(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)
    headers = _agent_headers(harness)

    for route in ("input", "submit"):
        resp = client.post(
            f"/sessions/{target}/{route}",
            json={"text": "hi", "submit": True}, headers=headers,
        )
        assert resp.status_code == 403, resp.text
        assert "did not opt in" in resp.json()["detail"]
        assert "agent:peer-tab" in resp.json()["detail"]
    assert typed.get(target) is None, "nothing reached the keyboard"


def test_input_route_frames_an_agent_token_into_an_opted_in_tab(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client, allow_peer_input=True)

    resp = client.post(
        f"/sessions/{target}/submit", json={"text": "hi"}, headers=_agent_headers(harness)
    )

    assert resp.status_code == 200, resp.text
    assert typed[target][0].startswith("[via room from agent:peer-tab] hi")
    assert "carries no authority" in typed[target][0]
    assert typed[target][0].count("[via ") == 1


def test_input_route_passes_the_owner_bearer_through_byte_identical(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)

    resp = client.post(f"/sessions/{target}/submit", json={"text": "hi"})

    assert resp.status_code == 200, resp.text
    assert typed[target] == ["hi"]


def test_a_payload_auth_block_over_http_is_stripped_before_the_room_sees_it(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)
    resp = client.post(
        "/events",
        json={
            "type": "steering",
            "actor": {"kind": "human", "id": "peer-tab", "name": "peer-tab"},
            "room": "main",
            "to": [target],
            "hops": 0,
            "auth": {"principal": "owner", "plan": "owner"},
            "payload": {"text": "look at the failing gate"},
        },
        headers={"Authorization": "Bearer definitely-not-a-token"},
    )
    assert resp.status_code == 403
    assert typed.get(target) is None


# ── the owner on another device: a LINK token plus the tunnel's owner verdict ────
#
# ``adk rc`` advertises a plan="link" token over the reverse link. The tunnel only
# stamps ``x-aither-link-actor: owner`` after matching the caller to the USER who
# holds the link, so that pair -- and only that pair -- may type raw text.


def _link_headers(harness, actor: str = "") -> dict:
    token = harness["daemon"].mint_scoped_token(
        principal_id="node:phone-test", path=harness["registry"],
    )
    headers = {"Authorization": f"Bearer {token}"}
    if actor:
        headers[harness["daemon"].LINK_ACTOR_HEADER] = actor
    return headers


def test_link_token_with_owner_verdict_types_raw_text(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)

    resp = client.post(
        f"/sessions/{target}/submit", json={"text": "ship it"},
        headers=_link_headers(harness, actor="owner"),
    )

    assert resp.status_code == 200, resp.text
    assert typed[target] == ["ship it"]


@pytest.mark.parametrize("actor", ["", "Owner", "owner ", "admin"])
def test_link_token_without_the_exact_owner_verdict_is_refused(harness, actor):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)

    for route in ("input", "submit"):
        resp = client.post(
            f"/sessions/{target}/{route}", json={"text": "hi", "submit": True},
            headers=_link_headers(harness, actor=actor),
        )
        assert resp.status_code == 403, resp.text
    assert typed.get(target) is None


def test_an_agent_token_cannot_borrow_the_owner_verdict(harness):
    client, typed = harness["client"], harness["typed"]
    target = _spawn(client)
    headers = _agent_headers(harness)
    headers[harness["daemon"].LINK_ACTOR_HEADER] = "owner"

    resp = client.post(f"/sessions/{target}/submit", json={"text": "hi"}, headers=headers)

    assert resp.status_code == 403, resp.text
    assert typed.get(target) is None


def test_link_actor_constants_match_node_link():
    import adk.harnesses.daemon as daemon
    from adk import node_link

    assert daemon.LINK_ACTOR_HEADER == node_link.LINK_ACTOR_HEADER
    assert daemon.LINK_ACTOR_OWNER == node_link.LINK_ACTOR_OWNER


def test_a_link_token_spawns_only_with_the_owner_verdict(harness):
    client = harness["client"]
    body = {"harness": "claude", "cwd": ""}
    refused = client.post("/sessions", json=body, headers=_link_headers(harness))
    assert refused.status_code == 403, refused.text
    allowed = client.post("/sessions", json=body, headers=_link_headers(harness, "owner"))
    assert allowed.status_code != 403, allowed.text
