"""The queue order: a card that answers itself yields to one that needs the owner.

Measured 2026-11-10: a 43 h "paid launch refused" card that picks its own default
at the deadline, and streak cards that pick "Skip" / "Keep it scheduled", floated
over 99+ waiting cards and hid a permission grant a live session was stopped on.
The term sits right after the credential/blocked tier, so those still lead.
"""
from __future__ import annotations

import time

from adk.decisions.store import (
    DecisionCard,
    DecisionOption,
    DecisionStore,
    is_noop_suggestion,
    is_self_answering,
    queue_order,
)

DAY = 86400.0


def test_a_self_answering_card_ranks_below_a_card_needing_the_owner(tmp_path):
    store = DecisionStore(tmp_path)
    governor = store.create(DecisionCard(
        id="",
        title="Paid launch refused",
        urgency="critical",
        options=[
            DecisionOption(key="refuse", label="Keep refusing"),
            DecisionOption(key="launch", label="Launch"),
        ],
        default_key="refuse",
        deadline=time.time() + 3600,
    ))
    share = store.create(DecisionCard(
        id="",
        title="Share: a blog post",
        urgency="high",
        options=[
            DecisionOption(key="share", label="Share"),
            DecisionOption(key="skip", label="Skip"),
        ],
        default_key="skip",
    ))
    grant = store.create(DecisionCard(
        id="",
        title="Permission grant",
        options=[
            DecisionOption(key="allow", label="Allow"),
            DecisionOption(key="deny", label="Deny"),
        ],
        default_key="allow",
    ))
    ordered = queue_order([governor, share, grant])
    assert [c.title for c in ordered] == [
        "Permission grant",
        "Paid launch refused",
        "Share: a blog post",
    ]
    assert is_self_answering(governor) and is_noop_suggestion(share)
    assert not is_self_answering(grant)


def test_a_blocked_card_leads_even_when_its_suggestion_is_a_noop():
    stopped = DecisionCard(
        id="d-stop", title="Session stopped", kind="blocked",
        options=[
            DecisionOption(key="go", label="Go"),
            DecisionOption(key="skip", label="Skip"),
        ],
        default_key="skip",
    )
    ask = DecisionCard(
        id="d-ask", title="A real ask", created_at=time.time() - DAY,
        options=[
            DecisionOption(key="yes", label="Approve"),
            DecisionOption(key="no", label="Deny"),
        ],
        default_key="yes",
    )
    assert [c.id for c in queue_order([ask, stopped])] == ["d-stop", "d-ask"]
