"""Guaranteed delivery of high-urgency decision cards, from any raiser.

Measured 2026-11-18: d-43nk (high, raised by the awdk daemon agent with no session
or steer channel) and d-v9hh never reached the owner. The card was durable and
every surface existed, but a raise only attempted notify() and nobody read the
verdict. These tests pin the three halves of the fix:

* a high/critical card with no real push surface (desk INBOX only, or nothing)
  is forced to the relay -- the same path session-raised cards use;
* every attempt is recorded in ``delivery-receipts.jsonl``;
* the daemon sweep re-pushes an open, unanswered, undelivered high card after a
  short window, then at the retry cadence.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adk.decisions import agent_tools, delivery
from adk.decisions.notify import NotifyResult
from adk.decisions.store import DecisionCard, DecisionOption, DecisionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AWASK_GROUPING", "0")
    import adk.decisions.store as store_mod

    monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    return DecisionStore(tmp_path / "decisions")


def _card(store: DecisionStore, urgency: str = "high") -> DecisionCard:
    return store.create(DecisionCard(
        id="", title="Confirm: reject the re-filed script", urgency=urgency,
        default_key="yes",
        options=[DecisionOption(key="yes", label="Yes"), DecisionOption(key="no", label="No")],
    ))


def _notify(monkeypatch, delivered=(), skipped=("card window (disabled or no display)",)):
    def fake(card, store=None):
        return NotifyResult(delivered=list(delivered), skipped=list(skipped), errors=[])

    monkeypatch.setattr("adk.decisions.notify.notify", fake)


def test_high_card_filed_only_in_the_desk_inbox_forces_the_relay(store, monkeypatch):
    card = _card(store)
    _notify(monkeypatch, delivered=["awdesk"])            # filed, not pushed
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)

    result = delivery.deliver_on_raise(card, store)

    assert result.ok and "relay (forced)" in result.delivered
    assert delivery.delivered(card.id)
    row = delivery._rows_for(card.id)[-1]
    assert row["surface"] == delivery.SURFACE_RAISE and row["ok"] is True
    assert row["channels"] == ["awdesk", "relay (forced)"]


def test_normal_card_is_never_forced_but_its_success_is_recorded(store, monkeypatch):
    card = _card(store, urgency="normal")
    _notify(monkeypatch, delivered=["awdesk"])
    monkeypatch.setattr(delivery, "_post_relay",
                        lambda c: pytest.fail("normal cards are not forced to the relay"))

    result = delivery.deliver_on_raise(card, store)

    assert result.ok                       # any channel counts for a normal card
    rows = delivery._rows_for(card.id)
    assert len(rows) == 1 and rows[0]["ok"] is True and rows[0]["channels"] == ["awdesk"]


def test_undelivered_high_card_is_recorded_then_re_pushed_after_the_window(store, monkeypatch):
    card = _card(store)
    _notify(monkeypatch, delivered=[])
    monkeypatch.setattr(delivery, "_post_relay", lambda c: "off: no relay configured")

    first = delivery.deliver_on_raise(card, store)
    assert not first.ok and not delivery.delivered(card.id)
    assert delivery._rows_for(card.id)[-1]["ok"] is False

    start = card.created_at
    assert not delivery.due_for_escalation(card, now=start + delivery.ESCALATE_AFTER_SECONDS / 2)
    assert delivery.due_for_escalation(card, now=start + delivery.ESCALATE_AFTER_SECONDS + 1)

    # still undelivered: the sweep records the failed re-push and stays quiet until retry
    out = delivery.sweep(store, now=start + delivery.ESCALATE_AFTER_SECONDS + 1)
    assert [r["card"] for r in out] == [card.id] and out[0]["delivered"] is False
    assert delivery._rows_for(card.id)[-1]["surface"] == delivery.SURFACE_ESCALATE
    assert delivery.sweep(store, now=start + delivery.ESCALATE_AFTER_SECONDS + 2) == []

    # the relay comes back: the next retry window delivers it
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)
    later = start + delivery.ESCALATE_AFTER_SECONDS + delivery.ESCALATE_RETRY_SECONDS + 1
    out = delivery.sweep(store, now=later)
    assert out and out[0]["delivered"] and delivery.delivered(card.id)


def test_sweep_ignores_answered_low_and_fresh_cards(store, monkeypatch):
    answered = _card(store)
    store.answer(answered.id, "yes", deliver=False)
    low = _card(store, urgency="low")
    delivery.record_attempt(low.id, ok=False, channels=[], surface=delivery.SURFACE_RAISE)
    fresh = _card(store)
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)
    _notify(monkeypatch, delivered=[])

    out = delivery.sweep(store, now=fresh.created_at + 1)

    assert out == []
    assert not delivery.delivered(fresh.id)


def test_daemon_raise_of_a_high_card_uses_the_guarantee(store, monkeypatch):
    from adk.harnesses import daemon

    root = "root-bearer-for-tests"
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", root)
    _notify(monkeypatch, delivered=[])
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)
    client = TestClient(daemon.create_app(token=root))
    client.headers = {"Authorization": f"Bearer {root}"}

    resp = client.post("/decisions", json={
        "title": "Confirm: reject proposal 20261118-141159-897f67",
        "urgency": "high",
        "default": "yes",
        "options": [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}],
        "agent": "adk-agent",
    })

    assert resp.status_code == 200, resp.text
    card_id = resp.json()["id"]
    assert delivery.delivered(card_id)
    assert delivery._rows_for(card_id)[-1]["surface"] == delivery.SURFACE_RAISE


def test_library_raise_card_uses_the_guarantee(store, monkeypatch):
    monkeypatch.setattr(agent_tools, "get_store", lambda: store)
    _notify(monkeypatch, delivered=["awdesk"])
    monkeypatch.setattr(delivery, "_post_relay", lambda c: None)

    card = agent_tools.raise_card("Ship the high-urgency fix?", kind="info",
                                  urgency="high")

    assert delivery.delivered(card.id)
