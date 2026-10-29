"""GET /attention -- the beam every surface renders.

Pins the shape a surface relies on (items head-first, segments already one row,
open/oldest for the count, a rev that changes when the queue does) and the
liveness rule: a needs-you card of a dead Claude session is not headlined.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-attention"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_CLAUDE_SESSIONS_DIR", str(tmp_path / "sessions"))
    # The store is a process-wide singleton keyed to the FIRST decisions dir it saw;
    # without this reset every test after the first reads the previous test's cards.
    from adk.decisions import store as store_module

    original = store_module._STORE
    with store_module._STORE_LOCK:
        store_module._STORE = None
    from adk.harnesses.daemon import create_app

    c = TestClient(create_app())
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    yield c
    with store_module._STORE_LOCK:
        store_module._STORE = original


def _card(**kw):
    from adk.decisions.store import DecisionCard, DecisionOption

    base = dict(
        id="d-ts7e",
        title="Publish the Election Day brief",
        kind="decision",
        urgency="high",
        default_key="skip",
        options=[DecisionOption(key="post", label="Post"),
                 DecisionOption(key="skip", label="Skip")],
    )
    base.update(kw)
    if not base.get("options"):
        base["default_key"] = None   # a card with no options has nothing to default to
    return DecisionCard(**base)


def test_attention_names_the_newest_high_ask_first(client):
    from adk.decisions.store import get_store

    store = get_store()
    store.create(_card(id="d-a2cd", title="older high", urgency="high"))
    time.sleep(1.1)                       # created_at is a wall clock; keep the order unambiguous
    store.create(_card(id="d-b3ef", title="Election Day is Tuesday", urgency="high"))

    body = client.get("/attention").json()
    assert [i["id"] for i in body["items"]][0] == "d-b3ef"
    assert body["items"][0]["key"] == "decision:d-b3ef"
    assert body["items"][0]["hot"] is True
    assert body["open"] == 2
    assert body["segments"][0]["key"] == "decision:d-b3ef"
    assert "d-b3ef" in body["segments"][0]["plain"]
    assert body["segments"][-1]["plain"].endswith("2 open)")


def test_attention_ignores_a_needs_you_card_of_a_dead_session(client, tmp_path):
    from adk.decisions.store import DecisionSource, get_store

    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    # A live, idle session and a dead one.
    (sessions / "1.json").write_text(json.dumps(
        {"pid": 999_999, "sessionId": "sess-dead", "status": "idle"}), encoding="utf-8")
    get_store().create(_card(id="d-c4gh", title="Claude Code needs you", kind="blocked",
                             options=[], dedupe_key="cc-waiting:sess-dead:1",
                             source=DecisionSource(session_id="sess-dead")))

    body = client.get("/attention").json()
    assert body["open"] == 0
    assert body["items"] == []
    assert body["segments"] == []


def test_attention_rev_is_stable_then_changes_when_a_card_is_answered(client):
    from adk.decisions.store import get_store

    store = get_store()
    store.create(_card(id="d-d5km"))
    first = client.get("/attention").json()["rev"]
    assert client.get("/attention").json()["rev"] == first
    store.answer("d-d5km", "post", via="test")
    assert client.get("/attention").json()["rev"] != first


def test_attention_wait_returns_early_when_nothing_changes(client):
    from adk.decisions.store import get_store

    get_store().create(_card(id="d-e6np"))
    body = client.get("/attention", params={"rev": "stale-rev", "wait": 0}).json()
    assert "rev" in body and "items" in body


def test_attention_is_bounded(client):
    body = client.get("/attention", params={"width": 20, "limit": 1}).json()
    assert body["segments"] == [] or len(body["segments"][0]["plain"]) <= 40
