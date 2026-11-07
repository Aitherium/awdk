"""A rolling producer updates ONE card in place: ``store.revise`` + ``POST /decisions/{id}/revise``.

Pins: the id, options and status survive a revise while title/summary/facts/urgency/
dedupe key move; a closed card is refused (409 at the daemon) so the caller raises a
new one; a dedupe key another live card holds is refused (400); unknown card is 404.
"""

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-testing-only"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    import adk.decisions.store as store_module

    monkeypatch.setattr(store_module, "_STORE", None)
    return store_module.get_store()


@pytest.fixture
def client(store):
    from adk.harnesses.daemon import create_app

    c = TestClient(create_app())
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c


def _card(store, key="rolling:aaa", title="cycle 1"):
    from adk.decisions.store import DecisionCard, DecisionOption

    return store.create(DecisionCard(
        id="", title=title, summary="first", kind="decision", default_key="ack",
        options=[DecisionOption(key="ack", label="Keep going"),
                 DecisionOption(key="pause", label="Pause it")],
        facts=["one"], dedupe_key=key))


def test_revise_keeps_id_and_options_and_moves_the_text(store):
    card = _card(store)
    out = store.revise(card.id, title="cycle 2", summary="second", facts=["a", "b"],
                       urgency="high", dedupe_key="rolling:bbb")
    again = store.get(card.id)
    assert out.id == card.id == again.id
    assert again.title == "cycle 2" and again.summary == "second"
    assert again.facts == ["a", "b"] and again.urgency == "high"
    assert again.dedupe_key == "rolling:bbb"
    assert [o.key for o in again.options] == ["ack", "pause"] and again.is_open
    assert len(store.list()) == 1  # no second card


def test_revise_none_leaves_fields_alone(store):
    card = _card(store)
    store.revise(card.id, summary="only this")
    again = store.get(card.id)
    assert again.title == "cycle 1" and again.facts == ["one"]
    assert again.dedupe_key == "rolling:aaa"


def test_revise_refuses_a_closed_card(store):
    from adk.decisions.store import CardClosedError

    card = _card(store)
    store.answer(card.id, "ack")
    with pytest.raises(CardClosedError):
        store.revise(card.id, title="late")
    assert store.get(card.id).title == "cycle 1"


def test_revise_refuses_a_key_another_live_card_holds(store):
    from adk.decisions.store import DecisionError

    _card(store, key="rolling:held", title="other")
    card = _card(store, key="rolling:mine")
    with pytest.raises(DecisionError):
        store.revise(card.id, dedupe_key="rolling:held")
    assert store.get(card.id).dedupe_key == "rolling:mine"


def test_revise_validates_like_a_raise(store):
    from adk.decisions.store import DecisionError

    card = _card(store)
    with pytest.raises(DecisionError):
        store.revise(card.id, urgency="whenever")
    with pytest.raises(DecisionError):
        store.revise(card.id, title="   ")
    assert store.get(card.id).urgency == "normal"


def test_daemon_route_revises_in_place(store, client):
    card = _card(store)
    r = client.post(f"/decisions/{card.id}/revise",
                    json={"title": "cycle 2", "facts": ["x"], "dedupe_key": "rolling:ccc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == card.id and body["title"] == "cycle 2"
    assert body["facts"] == ["x"] and body["dedupe_key"] == "rolling:ccc"
    assert body["summary"] == "first"


def test_daemon_route_answers_409_when_closed_404_when_missing_400_when_bad(store, client):
    card = _card(store)
    assert client.post("/decisions/d-zzzz/revise", json={"title": "t"}).status_code == 404
    assert client.post(f"/decisions/{card.id}/revise",
                       json={"urgency": "whenever"}).status_code == 400
    store.answer(card.id, "ack")
    assert client.post(f"/decisions/{card.id}/revise", json={"title": "t"}).status_code == 409


def test_daemon_route_requires_the_bearer(store, client):
    card = _card(store)
    r = TestClient(client.app).post(f"/decisions/{card.id}/revise", json={"title": "t"})
    assert r.status_code in (401, 403)
    assert store.get(card.id).title == "cycle 1"
