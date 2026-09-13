"""Test decision card triage (DECISION vs CONTEXT classification)."""

import time
import pytest
from adk.decisions.triage import triage, is_decision_pattern_json


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
