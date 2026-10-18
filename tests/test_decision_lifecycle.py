"""Card store lifecycle: one rank, credential dedupe by secret family, cancels with a
reason, re-raisable keys, stale scratch files swept.

Each test pins a failure measured on the owner's store (2026-10-14): a credential
ask at #28 of 41, three open cards for one Proton Bridge login, 13 CI cards
cancelled with no reason, a cancelled key swallowing a real future failure, and 16
orphaned ``*.tmp`` files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from adk.decisions import grouping
from adk.decisions import store as store_mod
from adk.decisions.store import (
    STATUS_CANCELLED,
    CancelNoteRequiredError,
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
    credential_dedupe_key,
    rank,
)

FIXTURE = Path(__file__).parent / "fixtures" / "card_rank_cases.json"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.delenv("AITHER_DECISIONS_STALE_DAYS", raising=False)
    monkeypatch.setenv("AWASK_GROUPING", "0")
    # No live steering tier in a test: the mailbox is the delivery we assert.
    monkeypatch.setattr("adk.decisions.steerback.deliver",
                        lambda card, text: (False, "test"), raising=False)
    # Waiter delivery must never reach a real daemon from a test: sessions read
    # as live (so the answer lands in their mailbox), and a resume is a failure.
    monkeypatch.setattr("adk.decisions.steerback.session_live", lambda waiter: True)

    def _no_resume(waiter, text):
        raise AssertionError(f"a test tried to resume a session: {waiter}")

    monkeypatch.setattr("adk.decisions.steerback.RESUME_HOOK", _no_resume)
    monkeypatch.setattr("adk.decisions.steerback.POST_HOOK", lambda st, card, line: None)
    return DecisionStore(tmp_path / "cards")


def _cred(secret: str, session: str, *, recipe: str = "proton_bridge",
          urgency: str = "normal") -> DecisionCard:
    return DecisionCard(
        id="", title=f"Proton Bridge login ({secret})", kind="credential",
        urgency=urgency, secret_name=secret, credential_format="password",
        credential_scope="platform", credential_description="inbound mail stopped",
        credential_recipe=recipe or None,
        source=DecisionSource(session_id=session, agent="test"))


def _decision(title: str, *, key: str = "", session: str = "s-raiser") -> DecisionCard:
    return DecisionCard(
        id="", title=title, dedupe_key=key,
        options=[DecisionOption(key="yes", label="Do it"),
                 DecisionOption(key="no", label="Leave it")],
        default_key="no", source=DecisionSource(session_id=session, agent="test"))


# ── (c) credential dedupe by secret family ────────────────────────────────────


def test_two_credential_raises_of_one_family_are_one_card_with_two_waiters(store):
    a = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    b = store.create(_cred("proton_bridge_pw_x", "s-two"))
    assert b.id == a.id
    open_cards = [c for c in store.list() if c.kind == "credential"]
    assert len(open_cards) == 1
    card = open_cards[0]
    assert card.dedupe_key == "cred:proton_bridge:platform"
    waiting = [card.source.session_id] + [w.session_id for w in card.waiters]
    assert waiting == ["s-one", "s-two"]
    # The same session raising again does not wait twice.
    store.create(_cred("AITHER_IMAP_PASS", "s-two"))
    assert len(store.get(a.id).waiters) == 1


def test_both_waiters_resume_when_the_credential_is_answered(store, tmp_path):
    a = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    store.create(_cred("proton_bridge_pw_x", "s-two"))
    store.answer(a.id, DecisionCard.CREDENTIAL_ANSWER, via="test",
                 receipt={"version": 1, "signed": False, "payload": {}})
    steer = tmp_path / "steer"
    for session in ("s-one", "s-two"):
        box = steer / session
        files = list(box.glob("*answer*")) if box.is_dir() else []
        assert files, f"{session} got no answer in its mailbox"
        assert a.id in files[0].read_text(encoding="utf-8")


def test_credential_family_falls_back_to_the_vault_key_and_keeps_scope(store):
    card = _cred("GOOGLE_CLIENT_ID", "s-one", recipe="")
    assert credential_dedupe_key(card) == "cred:GOOGLE_CLIENT_ID:platform"
    card.credential_scope = "workspace"
    assert credential_dedupe_key(card) == "cred:GOOGLE_CLIENT_ID:workspace"
    # Characters outside the key alphabet never make the card unraisable.
    odd = _cred("proton pw/david", "s-one", recipe="")
    assert store.create(odd).dedupe_key == "cred:proton-pw-david:platform"
    # The grouping planner names the same family the store dedupes on.
    assert grouping.credential_family(card.to_dict()) == credential_dedupe_key(card)


def test_an_answered_credential_reraised_is_a_new_ask(store):
    a = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    store.answer(a.id, DecisionCard.CREDENTIAL_ANSWER, via="test", deliver=False,
                 receipt={"version": 1, "signed": False, "payload": {}})
    again = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    assert again.id != a.id and again.is_open


def test_a_blocked_session_credential_ask_defaults_to_high(store):
    waiting = store.create(_cred("DISCORD_CLIENT_SECRET", "s-one", recipe=""), blocking=True)
    assert waiting.urgency == "high"
    calm = store.create(_cred("GOOGLE_CLIENT_ID", "s-one", recipe=""))
    assert calm.urgency == "normal"
    # A blocked second asker lifts the urgency of the card it joins.
    joined = store.create(_cred("GOOGLE_CLIENT_ID", "s-two", recipe=""), blocking=True)
    assert joined.id == calm.id and joined.urgency == "high"


def test_grouping_collapses_existing_credential_twins_onto_the_oldest():
    now = time.time()
    old = _cred("AITHER_IMAP_PASS", "s-one").to_dict()
    old.update(id="d-aaaa", created_at=now - 300)
    new = _cred("proton_bridge_pw_x", "s-two").to_dict()
    new.update(id="d-bbbb", created_at=now - 60)
    other = _cred("GOOGLE_CLIENT_ID", "s-three", recipe="").to_dict()
    other.update(id="d-cccc", created_at=now - 30)
    plan = grouping.plan([old, new, other], now)
    assert list(plan.withdraw) == ["d-bbbb"]
    assert plan.withdraw["d-bbbb"][0] == "G4"
    assert [w["session_id"] for w in plan.waiters["d-aaaa"]] == ["s-two"]
    twin, _ = grouping.plan_for_new(new, [old, other], now)
    assert twin is not None and twin["id"] == "d-aaaa"


def test_add_waiters_skips_known_and_empty_sessions(store):
    a = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    store.add_waiters(a.id, [{"session_id": "s-one"}, {"session_id": ""},
                             {"session_id": "s-two"}, DecisionSource(session_id="s-two")])
    assert [w.session_id for w in store.get(a.id).waiters] == ["s-two"]


# ── (d) re-raisable keys, cancels with a reason, reconcile ────────────────────


def test_a_cancelled_dedupe_key_can_be_raised_again(store):
    first = store.create(_decision("CI auto-fix could not run", key="ci-autofix-fail:12801"))
    store.cancel(first.id, note="fixed in #12983")
    again = store.create(_decision("CI auto-fix could not run", key="ci-autofix-fail:12801"))
    assert again.id != first.id and again.is_open
    # An ANSWERED key still holds: answering must not start a storm.
    store.answer(again.id, "no", via="test", deliver=False)
    third = store.create(_decision("CI auto-fix could not run", key="ci-autofix-fail:12801"))
    assert third.id == again.id


def test_an_owner_dismissal_still_holds_its_key(store):
    # The owner closing a card from the desk is a verdict, like an answer: a
    # producer re-raising it on its next pass would be the storm keys prevent.
    first = store.create(_decision("Wake failed", key="wake-failed:job-x"))
    store.cancel(first.id, note="dismissed from the desk", via="owner")
    again = store.create(_decision("Wake failed", key="wake-failed:job-x"))
    assert again.id == first.id


def test_an_expired_dedupe_key_is_released(store):
    card = _decision("Rotate the key?", key="rotate:x")
    card.deadline = time.time() - 1
    first = store.create(card)
    store.list()  # the read that expires it
    assert store.get(first.id).status == "expired"
    again = store.create(_decision("Rotate the key?", key="rotate:x"))
    assert again.id != first.id


def test_a_non_raiser_cancel_with_no_note_raises_value_error(store):
    card = store.create(_decision("Ship it?", session="s-raiser"))
    with pytest.raises(ValueError):
        store.cancel(card.id, by="s-someone-else")
    with pytest.raises(ValueError):
        store.cancel(card.id)  # a caller that does not say who it is
    with pytest.raises(CancelNoteRequiredError):
        store.cancel(card.id, note="   ")
    assert store.get(card.id).is_open
    # The raiser may withdraw its own card silently; anyone may with a reason.
    store.cancel(card.id, by="s-raiser")
    assert store.get(card.id).status == STATUS_CANCELLED
    other = store.create(_decision("Ship that?", session="s-raiser"))
    assert store.cancel(other.id, note="superseded by #1", by="s-x").answer_note == (
        "superseded by #1")


def test_reconcile_cancels_open_and_renotes_silent_cancels(store):
    silent = store.create(_decision("CI fail A", key="ci-autofix-fail:1"))
    store.cancel(silent.id, by="s-raiser")
    live = store.create(_decision("CI fail B", key="ci-autofix-fail:2"))
    unrelated = store.create(_decision("Other", key="other:1"))
    out = store.reconcile("ci-autofix-fail:", "fixed in #12983", by="owner-session")
    assert out == {"cancelled": [live.id], "renoted": [silent.id]}
    assert store.get(live.id).status == STATUS_CANCELLED
    assert store.get(live.id).answer_note == "fixed in #12983"
    assert store.get(silent.id).answer_note == "fixed in #12983"
    assert any(n.via == "reconcile" and "owner-session" in n.text
               for n in store.get(silent.id).notes)
    assert store.get(unrelated.id).is_open
    # Idempotent: a second run touches nothing.
    assert store.reconcile("ci-autofix-fail:", "fixed in #12983",
                           by="owner-session") == {"cancelled": [], "renoted": []}
    with pytest.raises(ValueError):
        store.reconcile("ci-autofix-fail:", "")


def test_reconcile_cli_verb(store, capsys, monkeypatch):
    from adk.decisions import cli

    card = store.create(_decision("CI fail C", key="ci-autofix-fail:3"))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(store.path))
    rc = cli.main(["reconcile", "--dedupe-prefix", "ci-autofix-fail:",
                   "--reason", "fixed in #12983", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["cancelled"] == [card.id]


# ── (b) one rank ──────────────────────────────────────────────────────────────


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_rank_constants_match_the_fixture():
    data = _fixture()
    assert list(store_mod.RANK_FIRST_KINDS) == data["rank_first_kinds"]
    assert store_mod.URGENCY_RANK == data["urgency_rank"]
    assert store_mod.STALE_AFTER_SECONDS == data["stale_after_seconds"]


@pytest.mark.parametrize("case", _fixture()["cases"], ids=lambda c: c["name"])
def test_rank_matches_card_rank_cases(case, monkeypatch):
    monkeypatch.delenv("AITHER_DECISIONS_STALE_DAYS", raising=False)
    cards = [DecisionCard.from_dict({"title": "t", **raw}) for raw in case["cards"]]
    assert [c.id for c in rank(cards, now=case["now"])] == case["expected"]
    # Input order never matters.
    assert [c.id for c in rank(list(reversed(cards)), now=case["now"])] == case["expected"]


def test_list_order_queue_uses_rank(store):
    report = store.create(DecisionCard(id="", title="hourly digest", kind="info",
                                       urgency="high"))
    ask = store.create(_cred("AITHER_IMAP_PASS", "s-one"))
    assert [c.id for c in store.list()] == [report.id, ask.id]
    assert [c.id for c in store.list(order="queue")] == [ask.id, report.id]
    with pytest.raises(store_mod.DecisionError):
        store.list(order="random")


# ── (a) info TTL ported from the package copy ─────────────────────────────────


def test_an_old_info_card_closes_on_its_own(store):
    card = DecisionCard(id="", title="digest", kind="info")
    card.created_at = time.time() - store_mod.INFO_TTL_SECONDS - 5
    made = store.create(card)
    assert store.list() == []
    closed = store.get(made.id)
    assert closed.status == "expired" and closed.answered_via == "info-ttl"


# ── (e) stale scratch files ───────────────────────────────────────────────────


def test_sweep_removes_stale_tmp_files(store):
    old = store.path / "d-abcd.1234.tmp"
    young = store.path / "d-efgh.1234.tmp"
    old.write_text("{}", encoding="utf-8")
    young.write_text("{}", encoding="utf-8")
    past = time.time() - store_mod.STALE_TMP_SECONDS - 60
    os.utime(old, (past, past))
    store.sweep()
    assert not old.exists()
    assert young.exists()


def test_cli_second_credential_ask_joins_and_opens_no_second_prompt(store, capsys,
                                                                    monkeypatch):
    from adk.decisions import cli

    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(store.path))
    monkeypatch.setattr(cli, "notify", lambda card, st: type(
        "R", (), {"describe": staticmethod(lambda: "test")})())
    ask = ["ask", "Proton Bridge password", "--credential", "--secret-name",
           "AITHER_IMAP_PASS", "--credential-description", "inbound mail", "--no-prompt",
           "--json", "--quiet"]
    monkeypatch.setenv("AITHER_SESSION_ID", "s-one")
    assert cli.main(ask) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("AITHER_SESSION_ID", "s-two")
    assert cli.main(ask) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["id"] == first["id"]
    assert second["prompt"].startswith("joined the open ask")
    assert [w.session_id for w in store.get(first["id"]).waiters] == ["s-two"]
