"""Cards-as-relay, the adk half: a raise POSTS the card as one relay message, and
every store transition PUSHES the new state so the message updates wherever it
is open.

What these pin, each able to fail:

* the wire shape is built from ``to_dict_safe`` -- a credential card's answer
  never leaves as a value;
* the post goes to ``/v1/decision-cards`` with the session bearer, and an HTTP
  error or a dead relay is REPORTED, never raised;
* the channel is OFF under pytest unless a test names the relay, so a developer
  shell with ``AWRELAY_URL`` set never posts a test card into a real channel;
* ``notify`` reports the relay as a delivery channel;
* answer / cancel / resolve / deadline each push the CLOSED state.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from adk.decisions import relay_post
from adk.decisions.store import (
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
)


def _card(**over) -> DecisionCard:
    base = dict(
        id="", title="Ship it?", summary="CI green", kind="decision", urgency="high",
        default_key="no",
        options=[DecisionOption(key="yes", label="Ship", consequence="goes live",
                                recommended=True),
                 DecisionOption(key="no", label="Hold")],
        source=DecisionSource(session_id="sess-1", agent="claude"),
    )
    base.update(over)
    return DecisionCard(**base)


class _Opener:
    def __init__(self, status=200, body="{}", exc=None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.exc:
            raise self.exc
        return self.status, self.body

    def payload(self, i=0) -> dict:
        return json.loads(self.requests[i].data.decode("utf-8"))


@pytest.fixture
def relay_env(monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_ROOM_URL", "https://relay.test/")
    monkeypatch.setenv("AITHER_DECISIONS_ROOM_TOKEN", "bearer-123")
    monkeypatch.delenv("AITHER_DECISIONS_ROOM", raising=False)


# ── the wire ────────────────────────────────────────────────────────────────


def test_card_wire_carries_the_decision():
    card = _card(id="d-abcd")
    wire = relay_post.card_wire(card)
    assert wire["type"] == "decision_card"
    assert wire["id"] == "d-abcd"
    assert wire["session"] == "sess-1"
    assert wire["recommended"] == "yes"
    assert wire["default"] == "no"
    assert [o["key"] for o in wire["options"]] == ["yes", "no"]
    assert wire["status"] == "open"


def test_card_wire_names_the_raising_machine(monkeypatch):
    # The relay answers only cards its own daemon holds; the origin is how the
    # owner tells a card raised on another machine apart.
    monkeypatch.setenv("AITHER_DECISIONS_ORIGIN", "laptop 01;rm")
    assert relay_post.card_wire(_card(id="d-abcd"))["origin"] == "laptop01rm"
    monkeypatch.delenv("AITHER_DECISIONS_ORIGIN")
    assert relay_post.card_wire(_card(id="d-abcd"))["origin"]


def test_card_wire_never_carries_a_credential_value():
    card = _card(id="d-cred", kind="credential", options=[], default_key="",
                 status="answered", answer="sk-THIS-WOULD-BE-A-SECRET",
                 answer_note="also secret")
    wire = relay_post.card_wire(card)
    assert wire["answer"] == DecisionCard.CREDENTIAL_ANSWER
    assert "SECRET" not in json.dumps(wire)


# ── switches ────────────────────────────────────────────────────────────────


def test_off_under_pytest_even_with_awrelay_url(monkeypatch):
    monkeypatch.delenv("AITHER_DECISIONS_ROOM_URL", raising=False)
    monkeypatch.setenv("AWRELAY_URL", "https://real-relay.example")
    assert relay_post.relay_url() == ""
    assert relay_post.post_card(_card(id="d-abcd")).startswith("off:")


def test_explicit_url_wins_and_the_kill_switch_wins_over_it(monkeypatch, relay_env):
    assert relay_post.relay_url() == "https://relay.test"
    monkeypatch.setenv("AITHER_DECISIONS_ROOM", "0")
    assert relay_post.relay_url() == ""


def test_no_identity_means_no_post(monkeypatch, relay_env, tmp_path):
    monkeypatch.delenv("AITHER_DECISIONS_ROOM_TOKEN", raising=False)
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    monkeypatch.setattr(relay_post, "_bearer_file", lambda: tmp_path / "missing")
    opener = _Opener()
    assert relay_post.post_card(_card(id="d-abcd"), opener=opener).startswith("off:")
    assert opener.requests == []


def test_session_bearer_file_is_the_default_identity(monkeypatch, relay_env, tmp_path):
    monkeypatch.delenv("AITHER_DECISIONS_ROOM_TOKEN", raising=False)
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    bearer = tmp_path / "session-bearer"
    bearer.write_text("file-bearer\n", encoding="utf-8")
    monkeypatch.setattr(relay_post, "_bearer_file", lambda: bearer)
    assert relay_post.relay_token() == "file-bearer"


# ── the post ────────────────────────────────────────────────────────────────


def test_post_card_hits_the_route_with_the_bearer(relay_env):
    opener = _Opener(status=200)
    assert relay_post.post_card(_card(id="d-abcd"), opener=opener) is None
    req = opener.requests[0]
    assert req.full_url == "https://relay.test/v1/decision-cards"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer bearer-123"
    assert opener.payload()["card"]["id"] == "d-abcd"


def test_post_card_reports_http_errors(relay_env):
    error = relay_post.post_card(_card(id="d-abcd"), opener=_Opener(status=403, body="no"))
    assert error and "403" in error


def test_post_card_survives_a_dead_relay(relay_env):
    error = relay_post.post_card(_card(id="d-abcd"),
                                 opener=_Opener(exc=OSError("connection refused")))
    assert error and "unreachable" in error


# ── raise: notify carries the relay channel ─────────────────────────────────


def test_notify_reports_the_relay_channel(monkeypatch, tmp_path):
    from adk.decisions import notify as notify_mod

    store = DecisionStore(tmp_path / "cards")
    card = store.create(_card())
    seen: list[str] = []
    monkeypatch.setattr(relay_post, "post_card", lambda c: seen.append(c.id))
    monkeypatch.setattr(notify_mod, "open_card_window", lambda _id: "test: no window")
    result = notify_mod.notify(card, store)
    assert seen == [card.id]
    assert "relay" in result.delivered

    monkeypatch.setattr(relay_post, "post_card", lambda c: "relay HTTP 503: down")
    result = notify_mod.notify(card, store)
    assert "relay" not in result.delivered
    assert any("relay (relay HTTP 503" in s for s in result.skipped)


# ── transitions: every close pushes the closed state ────────────────────────


@pytest.fixture
def pushed(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(relay_post, "sync_card",
                        lambda card, **_: calls.append((card.id, card.status, card.answer or "")))
    return calls


def test_answer_pushes_the_answered_state(tmp_path, pushed):
    store = DecisionStore(tmp_path / "cards")
    card = store.create(_card())
    store.answer(card.id, "yes", via="popup", deliver=False)
    assert pushed == [(card.id, "answered", "yes")]


def test_cancel_and_resolve_push_their_state(tmp_path, pushed):
    store = DecisionStore(tmp_path / "cards")
    a = store.create(_card())
    b = store.create(_card(title="Second?"))
    store.cancel(a.id)
    store.resolve(b.id)
    assert (a.id, "cancelled", "") in pushed
    assert (b.id, "answered", "kept") in pushed


def test_deadline_expiry_pushes_the_default(tmp_path, pushed):
    store = DecisionStore(tmp_path / "cards")
    card = store.create(_card(deadline=time.time() - 1))
    store.list()  # the read that applies an overdue default
    assert (card.id, "expired", "no") in pushed


def test_a_failed_answer_pushes_nothing(tmp_path, pushed):
    store = DecisionStore(tmp_path / "cards")
    card = store.create(_card())
    with pytest.raises(Exception):
        store.answer(card.id, "banana", deliver=False)
    assert pushed == []


def test_sync_card_is_off_without_a_relay(monkeypatch):
    monkeypatch.delenv("AITHER_DECISIONS_ROOM_URL", raising=False)
    assert relay_post.sync_card(_card(id="d-abcd")) is None


def test_sync_card_posts_in_the_background(relay_env):
    opener = _Opener()
    thread = relay_post.sync_card(_card(id="d-abcd", status="answered", answer="yes"),
                                  wait=True, opener=opener)
    assert thread is not None and not thread.is_alive()
    assert opener.payload()["card"]["status"] == "answered"
