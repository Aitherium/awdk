"""Test decision card triage (DECISION vs CONTEXT classification)."""

import time

from adk.decisions.store import DecisionCard, DecisionSource
from adk.decisions.triage import (
    NEEDS_YOU_CAP,
    PROTECTED_KINDS,
    is_decision_pattern_json,
    stale_view,
    triage,
)


class TestTriage:
    """Triage classification: DECISION has options/deadline, CONTEXT is info-only."""

    def test_hourly_digest_is_context(self):
        """Hourly digests without options are context."""
        card = {
            "id": "d-test",
            "title": "Hourly digest",
            "status": "open",
            "kind": "info",
            "options": [],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "context"
        assert "info-only" in reason or "status" in reason

    def test_notebook_card_is_context(self):
        """Notebook/status cards without options are context."""
        card = {
            "id": "d-test",
            "title": "Session notebook",
            "status": "open",
            "kind": "info",
            "options": [],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "context"

    def test_two_option_card_is_decision(self):
        """Card with real selectable options is a decision."""
        card = {
            "id": "d-test",
            "title": "Choose which plan",
            "status": "open",
            "kind": "decision",
            "options": [
                {"key": "plan_a", "label": "Plan A"},
                {"key": "plan_b", "label": "Plan B"},
            ],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "decision"
        assert "option" in reason

    def test_deadline_card_is_decision(self):
        """Card with a future deadline is a decision."""
        future = time.time() + 3600
        card = {
            "id": "d-test",
            "title": "Approve deployment by 5pm",
            "status": "open",
            "kind": "decision",
            "options": [],
            "deadline": future,
        }
        result, reason = triage(card)
        assert result == "decision"
        assert "deadline" in reason

    def test_expired_deadline_is_context(self):
        """Card with a past deadline is no longer a decision."""
        past = time.time() - 3600
        card = {
            "id": "d-test",
            "title": "Deployment window closed",
            "status": "open",
            "kind": "decision",
            "options": [],
            "deadline": past,
        }
        result, reason = triage(card)
        assert result == "context"

    def test_credential_card_is_decision(self):
        """Credential cards (secret input) are always decisions."""
        card = {
            "id": "d-test",
            "title": "Enter API key",
            "status": "open",
            "kind": "credential",
            "options": [],
            "secret_name": "STRIPE_API_KEY",
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "decision"
        assert "credential" in reason

    def test_blocked_card_is_decision(self):
        """Blocked cards unblock a session, even without options."""
        card = {
            "id": "d-test",
            "title": "Permission denied",
            "status": "open",
            "kind": "blocked",
            "options": [],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "decision"
        assert "blocked" in reason

    def test_status_only_options_are_context(self):
        """Options that are pure status phrases don't make it a decision."""
        card = {
            "id": "d-test",
            "title": "Status update",
            "status": "open",
            "kind": "info",
            "options": [
                {"key": "ack", "label": "Everything is running fine"},
                {"key": "waiting", "label": "The system is waiting for input"},
            ],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "context"
        assert "status-only" in reason

    def test_actionable_options_mixed_with_status(self):
        """If at least one option is actionable, it's a decision."""
        card = {
            "id": "d-test",
            "title": "Choose action",
            "status": "open",
            "kind": "decision",
            "options": [
                {"key": "wait", "label": "The build is still running"},
                {"key": "approve", "label": "Approve the deployment"},
            ],
            "deadline": None,
        }
        result, reason = triage(card)
        assert result == "decision"
        assert "option" in reason

    def test_closed_card_is_context(self):
        """Answered/expired/cancelled cards are outside triage scope."""
        for status in ["answered", "expired", "cancelled"]:
            card = {
                "id": "d-test",
                "title": "Already dealt with",
                "status": status,
                "kind": "decision",
                "options": [{"key": "yes", "label": "Yes"}],
            }
            result, reason = triage(card)
            assert result == "context"
            assert status in reason

    def test_patterns_export(self):
        """Triage patterns export for desk-side use."""
        patterns = is_decision_pattern_json()
        assert "decision_kinds" in patterns
        assert "context_phrases" in patterns
        assert "credential" in patterns["decision_kinds"]
        assert "blocked" in patterns["decision_kinds"]
        # All patterns should be strings (for desk-side regex)
        assert all(isinstance(p, str) for p in patterns["context_phrases"])


# ── stale_view: the list-first partition every surface shares ─────────────────
#
# The window, the CLI, awdesk, Fairy and Alt+Space draw the SAME two groups in
# the SAME order, so "where did that card go" has one answer. These tests pin
# the counting rule that makes the cap honest and the protection rule that keeps
# a credential ask out of the bulk-dismiss group.

DAY = 86400.0
NOW = 1_800_000_000.0


def _dc(cid: str, *, kind: str = "decision", urgency: str = "normal",
        age_days: float = 0.0, status: str = "open") -> DecisionCard:
    return DecisionCard(
        id=cid, title=cid, kind=kind, urgency=urgency, status=status,
        source=DecisionSource(session_id="sess"), created_at=NOW - age_days * DAY,
    )


class TestStaleView:
    def test_needs_you_is_capped_while_the_totals_count_the_whole_queue(self):
        cards = [_dc(f"c{i}") for i in range(7)]
        view = stale_view(cards, now=NOW)
        assert len(view["needs_you"]) == NEEDS_YOU_CAP
        assert view["needs_you_total"] == 7
        assert view["needs_you_more"] == 2
        # The cursor walks the full queue, never the visible page.
        assert [c.id for c in view["open"]] == [f"c{i}" for i in range(7)]
        assert view["total_open"] == 7

    def test_a_smaller_queue_has_no_more_row(self):
        view = stale_view([_dc("a"), _dc("b")], now=NOW)
        assert view["needs_you_more"] == 0
        assert view["older_count"] == 0

    def test_old_cards_move_to_the_older_group(self):
        cards = [_dc("fresh"), _dc("stale", age_days=9)]
        view = stale_view(cards, now=NOW)
        assert [c.id for c in view["needs_you"]] == ["fresh"]
        assert [c.id for c in view["older"]] == ["stale"]
        assert view["older_count"] == 1

    def test_protected_kinds_never_fall_into_the_older_group(self):
        cards = [
            _dc("old-cred", kind="credential", age_days=40),
            _dc("old-blocked", kind="blocked", age_days=40),
            _dc("old-blocker", kind="blocker", age_days=40),
            _dc("old-decision", age_days=40),
        ]
        view = stale_view(cards, now=NOW)
        needs = {c.id for c in view["needs_you"]}
        old = {c.id for c in view["older"]}
        assert {"old-cred", "old-blocked", "old-blocker"} <= needs
        assert {c.kind for c in view["older"]} & PROTECTED_KINDS == set()
        assert old == {"old-decision"}

    def test_a_protected_card_does_not_consume_the_cap_for_stale_checks(self):
        # A queue of only ancient credentials is ALL "needs you", none older.
        cards = [_dc(f"cred{i}", kind="credential", age_days=30) for i in range(3)]
        view = stale_view(cards, now=NOW)
        assert view["needs_you_total"] == 3
        assert view["older"] == []

    def test_both_groups_are_ranked_by_rank_key(self):
        cards = [
            _dc("normal", urgency="normal"),
            _dc("critical", urgency="critical"),
            _dc("high", urgency="high"),
            _dc("stale-normal", urgency="normal", age_days=9),
            _dc("stale-critical", urgency="critical", age_days=9),
        ]
        view = stale_view(cards, now=NOW)
        assert [c.id for c in view["needs_you"]] == ["critical", "high", "normal"]
        assert [c.id for c in view["older"]] == ["stale-critical", "stale-normal"]

    def test_credential_and_blocked_lead_even_when_fresh_normal(self):
        cards = [
            _dc("critical-fresh", urgency="critical"),
            _dc("cred", kind="credential"),
            _dc("blocked", kind="blocked"),
        ]
        view = stale_view(cards, now=NOW)
        assert [c.id for c in view["needs_you"]][:2] in (
            ["cred", "blocked"], ["blocked", "cred"],
        )

    def test_closed_cards_are_not_in_any_group(self):
        cards = [
            _dc("open"),
            _dc("answered", status="answered"),
            _dc("expired", status="expired"),
            _dc("cancelled", status="cancelled"),
        ]
        view = stale_view(cards, now=NOW)
        assert view["total_open"] == 1
        assert [c.id for c in view["open"]] == ["open"]

    def test_the_stale_line_can_be_overridden_for_a_callers_clock(self):
        cards = [_dc("two-days", age_days=2)]
        assert stale_view(cards, now=NOW, stale_after=1 * DAY)["older_count"] == 1
        assert stale_view(cards, now=NOW, stale_after=3 * DAY)["older_count"] == 0

    def test_an_explicit_zero_cap_shows_no_rows_but_keeps_the_true_total(self):
        view = stale_view([_dc("a"), _dc("b")], now=NOW, cap=0)
        assert view["needs_you"] == []
        assert view["needs_you_total"] == 2
        assert view["needs_you_more"] == 2

    def test_plain_dicts_are_accepted_so_the_desk_reads_the_same_view(self):
        raw = _dc("as-dict").to_dict()
        view = stale_view([raw], now=NOW)
        assert [c.id for c in view["needs_you"]] == ["as-dict"]
