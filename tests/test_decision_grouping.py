"""Card grouping (adk.decisions.grouping): one card per QUESTION.

G1 a newer card in the same series supersedes the older open one; G2 a
cross-source duplicate collapses into the richer card; G3 an info card closes
after its TTL and never counts as a decision. Every withdrawal carries a note.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from adk.decisions import grouping
from adk.decisions.store import (
    STATUS_CANCELLED,
    STATUS_OPEN,
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
)
from adk.decisions.triage import is_decision_pattern_json, triage

NOW = 1_800_000_000.0


def _opts() -> list[DecisionOption]:
    return [DecisionOption(key="approve", label="Approve all"),
            DecisionOption(key="later", label="Later")]


def _card(cid: str, title: str, *, age: float = 0.0, kind: str = "decision",
          agent: str = "", session: str = "", facts=(), summary: str = "",
          dedupe_key: str = "") -> DecisionCard:
    return DecisionCard(
        id=cid, title=title, kind=kind, summary=summary, facts=list(facts),
        options=_opts() if kind == "decision" else [],
        default_key="later" if kind == "decision" else "",
        source=DecisionSource(agent=agent, session_id=session),
        created_at=NOW - age, dedupe_key=dedupe_key)


# ── series_key ────────────────────────────────────────────────────────────────

def test_series_key_normalises_dates_and_counts():
    a = grouping.series_key("BusinessPilot: 12 action(s) waiting for approval (2026-10-03)")
    b = grouping.series_key("BusinessPilot: 9 action(s) waiting for approval (2026-10-04)")
    assert a and a == b


def test_series_key_normalises_clock_times():
    assert grouping.series_key("Platform Ops 18:43") == grouping.series_key("Platform Ops 09:05")


@pytest.mark.parametrize("a,b", [
    ("PR 11604 fails checks", "PR 11605 fails checks"),
    ("port needs a human: abc1234", "port needs a human: def5678"),
    ("Approve #12 actions", "Approve #13 actions"),
])
def test_series_key_keeps_identities_apart(a, b):
    assert grouping.series_key(a) != grouping.series_key(b)


def test_series_key_refuses_an_all_volatile_title():
    assert grouping.series_key("2026-10-03 18:43") == ""


# ── G1 series supersede ───────────────────────────────────────────────────────

def test_g1_newest_batch_supersedes_older_ones_with_a_note():
    cards = [
        _card("d-aaaa", "BusinessPilot: 12 action(s) waiting for approval (2026-10-01)",
              age=3 * 86400),
        _card("d-bbbb", "BusinessPilot: 9 action(s) waiting for approval (2026-10-02)",
              age=2 * 86400),
        _card("d-cccc", "BusinessPilot: 14 action(s) waiting for approval (2026-10-03)",
              age=86400),
    ]
    p = grouping.plan(cards, NOW)
    assert set(p.withdraw) == {"d-aaaa", "d-bbbb"}
    assert all(rule == "G1" and "superseded by d-cccc" in note
               for rule, note in p.withdraw.values())


def test_g1_never_crosses_sessions():
    cards = [_card("d-aaaa", "2 things are waiting on you", age=60, session="s1"),
             _card("d-bbbb", "3 things are waiting on you", session="s2")]
    assert not grouping.plan(cards, NOW).withdraw


def test_g1_respects_distinct_producer_dedupe_keys():
    cards = [_card("d-aaaa", "Job failing (2026-10-01)", age=60, dedupe_key="job:a"),
             _card("d-bbbb", "Job failing (2026-10-02)", dedupe_key="job:b")]
    assert not grouping.plan(cards, NOW).withdraw


def _bp(cid: str, day: str, count: int, *, age: float = 0.0) -> DecisionCard:
    """A BusinessPilot daily card in the producer's real shape
    (lib/automation/approval_digest.py): own per-day dedupe key, 9 options."""
    c = _card(cid, f"BusinessPilot: {count} action(s) waiting for approval ({day})",
              age=age, agent="business-pilot", dedupe_key=f"bp-approvals-{day}")
    c.options = [DecisionOption(key=k, label=k) for k in
                 ("approve_all", "reject_all", "hold", "only:a", "only:b", "only:c",
                  "only:d", "only:e", "only:f")]
    c.default_key = "hold"
    return c


def test_g1_supersedes_real_businesspilot_cards_with_per_day_dedupe_keys():
    # Live 2026-10-04: d-nyy8/d-uezc/d-j5ch carried bp-approvals-2026-10-02/03/04
    # and grouping withdrew none of them.
    cards = [_bp("d-nyy8", "2026-10-02", 12, age=2 * 86400),
             _bp("d-uezc", "2026-10-03", 9, age=86400),
             _bp("d-j5ch", "2026-10-04", 14)]
    p = grouping.plan(cards, NOW)
    assert set(p.withdraw) == {"d-nyy8", "d-uezc"}
    assert all("superseded by d-j5ch" in note for _r, note in p.withdraw.values())
    twin, p2 = grouping.plan_for_new(_bp("d-new", "2026-10-05", 3), cards, NOW)
    assert twin is None and set(p2.withdraw) == {"d-nyy8", "d-uezc", "d-j5ch"}


def test_g1_dedupe_keys_that_differ_beyond_a_date_stay_apart():
    a = _bp("d-aaaa", "2026-10-03", 9, age=60)
    b = _bp("d-bbbb", "2026-10-04", 9)
    b.dedupe_key = "bp-refunds-2026-10-04"
    assert not grouping.plan([a, b], NOW).withdraw


def test_g1_leaves_a_lone_card_and_closed_cards_alone():
    lone = _card("d-aaaa", "BusinessPilot: 3 action(s) waiting (2026-10-01)", age=60)
    closed = _card("d-bbbb", "BusinessPilot: 4 action(s) waiting (2026-10-02)")
    closed.status = STATUS_CANCELLED
    assert not grouping.plan([lone, closed], NOW).withdraw


# ── G2 cross-source subject ───────────────────────────────────────────────────

def test_g2_collapses_a_support_and_error_pair_into_the_richer():
    sup = _card("d-aaaa", "support: platform [mail/lead] Cannot log in to my account",
                age=60, facts=["from jo@example.com"], summary="long summary of the thread")
    err = _card("d-bbbb", "Customer error report: [mail/lead] Cannot log in to my account",
                kind="info")
    p = grouping.plan([sup, err], NOW)
    assert list(p.withdraw) == ["d-bbbb"]
    rule, note = p.withdraw["d-bbbb"]
    assert rule == "G2" and "duplicate of d-aaaa" in note
    assert any("d-bbbb" in f for f in p.facts["d-aaaa"]), "the kept card records the other"


def test_g2_ignores_reply_prefixes_and_case():
    a = grouping.subject_of("support: platform [mail/lead] Re: Billing broke")
    b = grouping.subject_of("Customer error report: [Mail/Lead] billing BROKE")
    assert a is not None and b is not None and a[1] == b[1] and a[0] != b[0]


def test_g2_needs_two_sources_and_skips_pipeline_tags():
    same = [_card("d-aaaa", "support: platform [mail/lead] Cannot log in", age=60,
                  agent="x", session="s1"),
            _card("d-bbbb", "support: platform [mail/lead] Cannot log in", agent="y")]
    assert not any(r == "G2" for r, _ in grouping.plan(same, NOW).withdraw.values())
    assert grouping.subject_of("Triage: [Expedition] Task completed: validate") is None


def test_g2_different_subjects_stay_apart():
    cards = [_card("d-aaaa", "support: platform [mail/lead] Cannot log in", age=60),
             _card("d-bbbb", "Customer error report: [mail/lead] Refund please")]
    assert not grouping.plan(cards, NOW).withdraw


# ── G3 info TTL ───────────────────────────────────────────────────────────────

def test_g3_closes_an_info_card_past_its_ttl_with_a_note():
    old = _card("d-aaaa", "Hourly digest", kind="info", age=25 * 3600)
    young = _card("d-bbbb", "Fleet fine", kind="info", age=3600)
    decision = _card("d-cccc", "Ship it?", age=40 * 3600)
    p = grouping.plan([old, young, decision], NOW)
    assert list(p.withdraw) == ["d-aaaa"]
    assert p.withdraw["d-aaaa"][0] == "G3" and "TTL" in p.withdraw["d-aaaa"][1]


def test_g3_spares_an_info_card_with_a_future_deadline_and_can_be_disabled():
    c = _card("d-aaaa", "Notice", kind="info", age=48 * 3600)
    c.deadline = NOW + 60
    assert not grouping.plan([c], NOW).withdraw
    c.deadline = None
    assert not grouping.plan([c], NOW, info_ttl=0).withdraw


def test_info_never_counts_as_a_decision_even_with_a_deadline():
    card = _card("d-aaaa", "Digest", kind="info").to_dict()
    card["deadline"] = time.time() + 3600
    assert triage(card)[0] == "context"
    assert "info" in is_decision_pattern_json()["context_kinds"]


def test_grouping_accepts_card_dicts():
    cards = [c.to_dict() for c in (
        _card("d-aaaa", "Platform Ops 09:00", kind="info", age=600),
        _card("d-bbbb", "Platform Ops 10:00", kind="info"))]
    assert list(grouping.plan(cards, NOW).withdraw) == ["d-aaaa"]


# ── the store applies G1/G2 at raise time ─────────────────────────────────────

def _store(tmp_path: Path) -> DecisionStore:
    store = DecisionStore(tmp_path / "decisions")
    store.group_on_create = True
    return store


def _fresh(title: str, **kw) -> DecisionCard:
    c = _card("", title, **kw)
    c.created_at = time.time()
    return c


def test_create_supersedes_the_older_card_of_a_series(tmp_path):
    store = _store(tmp_path)
    old = store.create(_fresh("BusinessPilot: 12 action(s) waiting for approval (2026-10-03)"))
    new = store.create(_fresh("BusinessPilot: 14 action(s) waiting for approval (2026-10-04)"))
    assert new.id != old.id and store.get(new.id).status == STATUS_OPEN
    gone = store.get(old.id)
    assert gone.status == STATUS_CANCELLED
    assert f"superseded by {new.id}" in (gone.answer_note or "")
    assert [c.id for c in store.list()] == [new.id]


def test_create_folds_a_poorer_cross_source_duplicate_into_the_open_card(tmp_path):
    store = _store(tmp_path)
    rich = store.create(_fresh("support: platform [mail/lead] Cannot log in",
                               facts=["from jo"], summary="thread"))
    got = store.create(_fresh("Customer error report: [mail/lead] Cannot log in", kind="info"))
    assert got.id == rich.id, "the caller gets the existing richer card back"
    assert len(store.list()) == 1
    assert any("Customer error report" in f for f in store.get(rich.id).facts), \
        "the second raise is recorded, never dropped silently"


def test_create_withdraws_a_poorer_open_twin_when_the_new_card_is_richer(tmp_path):
    store = _store(tmp_path)
    poor = store.create(_fresh("Customer error report: [mail/lead] Cannot log in",
                               kind="info"))
    rich = store.create(_fresh("support: platform [mail/lead] Cannot log in",
                               facts=["from jo"]))
    assert store.get(poor.id).status == STATUS_CANCELLED
    assert f"duplicate of {rich.id}" in (store.get(poor.id).answer_note or "")
    assert any(poor.id in f for f in store.get(rich.id).facts)


def test_create_supersedes_yesterdays_businesspilot_card_despite_its_own_key(tmp_path):
    store = _store(tmp_path)
    old = _bp("", "2026-10-03", 12)
    old.created_at = time.time() - 60
    old = store.create(old)
    new = _bp("", "2026-10-04", 14)
    new.created_at = time.time()
    new = store.create(new)
    assert new.id != old.id
    gone = store.get(old.id)
    assert gone.status == STATUS_CANCELLED
    assert f"superseded by {new.id}" in (gone.answer_note or "")
    # the same day re-raised is still the producer's own dedupe hit, not a new card
    again = _bp("", "2026-10-04", 15)
    again.created_at = time.time()
    assert store.create(again).id == new.id


def test_create_grouping_can_be_turned_off(tmp_path, monkeypatch):
    store = DecisionStore(tmp_path / "decisions")
    monkeypatch.setenv("AWASK_GROUPING", "0")
    store.create(_fresh("Platform Ops 09:00 report"))
    store.create(_fresh("Platform Ops 10:00 report"))
    assert len(store.list()) == 2


def test_g1_never_closes_a_real_ask_on_an_identical_title_alone():
    cards = [_card("d-aaaa", "Pick one", age=60), _card("d-bbbb", "Pick one")]
    assert not grouping.plan(cards, NOW).withdraw
    reports = [_card("d-cccc", "Hourly digest", kind="info", age=60),
               _card("d-dddd", "Hourly digest", kind="info")]
    assert list(grouping.plan(reports, NOW).withdraw) == ["d-cccc"]


def test_g2_joins_a_pair_whose_titles_were_cut_at_different_lengths():
    # Measured 2026-10-04 on the live queue: one DMARC mail, two sources, titles
    # cut at ~100 characters -- one ends mid-UTF-8 sequence.
    err = _card("d-aaaa", "Customer error report: [mail/lead] [Preview] Report Domain: "
                "aitherium.com Submitter: enterprise.protection.outlo�",
                age=60, facts=["a", "b", "c", "d", "e"])
    sup = _card("d-bbbb", "support: platform [mail/lead] [Preview] Report Domain: "
                "aitherium.com Submitter: enterprise.protection.outloo", facts=["a", "b"])
    p = grouping.plan([err, sup], NOW)
    assert list(p.withdraw) == ["d-bbbb"] and "duplicate of d-aaaa" in p.withdraw["d-bbbb"][1]


def test_g2_short_prefixes_do_not_join():
    a = _card("d-aaaa", "support: platform [mail/lead] Invoice", age=60)
    b = _card("d-bbbb", "Customer error report: [mail/lead] Invoice 4471 overdue")
    assert not grouping.plan([a, b], NOW).withdraw


# -- G2 never merges two tickets -----------------------------------------------

_DMARC = "[mail/lead] [Preview] Report Domain: aitherium.com Submitter: enterprise.prot"
_ISSUE = "https://github.com/Aitherium/AitherOS/issues/"


def _ticketed(cid: str, prefix: str, ticket: str, issue: int, *, age: float = 0.0,
              extra: int = 0) -> DecisionCard:
    return _card(cid, f"{prefix} {_DMARC}", age=age, agent="feedback-triage",
                 facts=[f"ticket: {ticket}", f"issue: {_ISSUE}{issue}"]
                 + ["x%d" % i for i in range(extra)])


def test_g2_two_tickets_with_one_subject_both_stay_open():
    # Live 2026-10-04: d-xrgd/d-xqf3 (one ticket) plus the next day's DMARC mail
    # (another ticket) all collapsed into one card, losing the first ticket.
    cards = [_ticketed("d-xrgd", "support: platform", "tkt_f679", 11491, age=86400),
             _ticketed("d-xqf3", "Customer error report:", "tkt_f679", 11491,
                       age=86400, extra=2),
             _ticketed("d-new1", "support: platform", "tkt_a001", 11800, extra=2),
             _ticketed("d-new2", "Customer error report:", "tkt_a001", 11800)]
    p = grouping.plan(cards, NOW)
    assert set(p.withdraw) == {"d-xrgd", "d-new2"}
    assert "duplicate of d-xqf3" in p.withdraw["d-xrgd"][1]
    assert "duplicate of d-new1" in p.withdraw["d-new2"][1]


def test_g2_different_tickets_never_merge_at_create(tmp_path):
    store = _store(tmp_path)
    a = store.create(_fresh(f"support: platform {_DMARC}", agent="feedback-triage",
                            facts=["ticket: tkt_1", f"issue: {_ISSUE}1", "y"]))
    b = store.create(_fresh(f"Customer error report: {_DMARC}", agent="feedback-triage",
                            facts=["ticket: tkt_2"]))
    assert a.id != b.id
    assert {c.id for c in store.list()} == {a.id, b.id}


def test_g2_the_card_that_stands_inherits_the_withdrawn_handles():
    rich = _card("d-aaaa", f"support: platform {_DMARC}", age=60,
                 facts=["from jo", "thread", "more"])
    poor = _card("d-bbbb", f"Customer error report: {_DMARC}",
                 facts=["ticket: tkt_9", f"issue: {_ISSUE}9"])
    p = grouping.plan([rich, poor], NOW)
    assert list(p.withdraw) == ["d-bbbb"]
    assert "ticket: tkt_9" in p.facts["d-aaaa"]
    assert f"issue: {_ISSUE}9" in p.facts["d-aaaa"]


def test_create_twin_inherits_the_new_raise_handles(tmp_path):
    store = _store(tmp_path)
    rich = store.create(_fresh("support: platform [mail/lead] Cannot log in",
                               facts=["from jo", "a", "b"], summary="thread"))
    got = store.create(_fresh("Customer error report: [mail/lead] Cannot log in",
                              facts=["ticket: tkt_7"]))
    assert got.id == rich.id
    assert "ticket: tkt_7" in store.get(rich.id).facts


# -- info never counts toward "N decisions waiting" ----------------------------

def test_decisions_waiting_and_summary_ignore_info_cards():
    from adk.decisions.render import render_summary
    from adk.decisions.triage import decisions_waiting
    ask = _card("d-aaaa", "Pick a deploy window")
    ask.created_at = time.time()
    digests = [_card("d-i%d" % i, "Hourly digest %d" % i, kind="info") for i in range(3)]
    for d in digests:
        d.created_at = time.time()
        d.deadline = time.time() + 3600
    assert decisions_waiting([ask, *digests]) == [ask]
    assert decisions_waiting([ask.to_dict()]) == [ask.to_dict()]
    line = render_summary([ask, *digests])
    assert line == "Pick a deploy window · 3 for context"
    assert render_summary(digests) == "no decisions waiting · 3 for context"
    ask2 = _card("d-bbbb", "Approve the release")
    ask2.created_at = time.time()
    assert render_summary([ask, ask2, *digests]).startswith("2 decisions waiting")


def test_notify_coalesced_toast_counts_only_decisions(tmp_path, monkeypatch):
    from adk.decisions import notify
    monkeypatch.setenv("AWASK_GROUPING", "0")
    store = DecisionStore(tmp_path / "decisions")
    for i in range(3):
        c = _card("", "Info digest number %d" % i, kind="info")
        c.created_at = time.time()
        store.create(c)
    a = _card("", "Pick a deploy window")
    a.created_at = time.time()
    store.create(a)
    b = _card("", "Approve the release")
    b.created_at = time.time()
    b = store.create(b)
    toasts: list = []
    monkeypatch.setattr(notify, "_read_state", lambda: {"last_toast_at": time.time()})
    monkeypatch.setattr(notify, "_write_state", lambda st: None)
    monkeypatch.setattr(notify, "popup_enabled", lambda: False)
    monkeypatch.setattr(notify, "native_toast",
                        lambda title, body, urgency="normal": toasts.append(title))
    monkeypatch.setattr(notify, "_webhook_post", lambda card, n: None)
    notify.notify(b, store=store)
    assert toasts == ["2 decisions waiting"]
