"""A card must be DECIDABLE, and the rules must key on shape, not on the label.

Two defects this pins, both of which were live and neither of which raised, logged
or failed a test while it was:

1. STATUS AS A CHOICE. Options that state status ("both are running", "the only
   open items are...") give the owner nothing to pick between. The card pages a
   human to tell them a fact, which is the cost a card exists to avoid paying.

2. THE kind='info' BYPASS. `kind` only selects the popup HEADER (KIND_HEADER.get
   in popup.py) -- the buttons render either way -- while every decidability rule
   used to be guarded by `kind == "decision"`. So the label alone bought a rendered
   chooser with no default_key and no status check. It reads as a quieter card and
   is actually an unvalidated one.

So the rules key on CARRYING OPTIONS, never on kind. If the owner is shown buttons,
they are being asked to choose, whatever the card calls itself.

This file exists because the fix was written once and SILENTLY LOST -- a concurrent
session committed the file without the hunk, leaving the tree clean and the rule
absent. A test is what makes that loud instead of invisible.

Each guard below carries a MUTATION note: what must be true for the assertion to be
able to fail at all. A rule whose test cannot fail is not a rule.
"""

import pytest

from adk.decisions.store import (
    DecisionCard,
    DecisionError,
    DecisionOption,
    DecisionStore,
)


def _card(**kw):
    return DecisionCard(id="x", title="t", **kw)


def _opt(key, label):
    return DecisionOption(key=key, label=label)


# -- status is not a choice ---------------------------------------------------

STATUS_LABELS = [
    "aitheros-veil and aitheros-genesis are both running",
    "the only open items are the two background runs",
    "everything else is done",
    "the sync is an in-progress background run",
    "waiting on the run",
    "Start with: the only open item is the deploy",
]


@pytest.mark.parametrize("label", STATUS_LABELS)
def test_status_option_is_refused(label):
    """MUTATION: delete the _status_only_phrase loop in _validate and every case passes."""
    with pytest.raises(DecisionError) as err:
        DecisionStore._validate(
            _card(kind="decision", default_key="a",
                  options=[_opt("a", label), _opt("b", "cancel it")])
        )
    assert "status" in str(err.value).lower()


CHOICE_LABELS = [
    "cancel the run",
    "let it finish",
    # The agent describing its OWN next steps in passing is not a status option.
    # MUTATION: unanchor _ACTION_VERB (drop the ^) and this one starts failing.
    "restart the worker, then report what is still running",
    "stop the two background runs that are both running",
    "roll back the deploy",
    "wait for completion",
    # MUTATION: drop the word boundaries from _STATUS_ONLY and "rerunning"
    # matches "running", refusing a perfectly good option. That exact collapse
    # happened once and left the whole rule inert.
    "the job is rerunning",
]


@pytest.mark.parametrize("label", CHOICE_LABELS)
def test_real_choice_is_accepted(label):
    """A rule that floods gets switched off, so the false-positive half is pinned too."""
    DecisionStore._validate(
        _card(kind="decision", default_key="a",
              options=[_opt("a", label), _opt("b", "proceed")])
    )


def test_word_boundaries_are_real_not_literal_backspace():
    """The regexes must contain word boundaries, not the 0x08 byte.

    Writing these patterns through a shell heredoc once collapsed every ``BS b``
    into a literal backspace. The module still imported, every pattern still
    compiled, and the rule matched NOTHING -- a silent inert gate. Assert the
    byte, because the behaviour above cannot distinguish "no boundary" from
    "boundary" on most inputs.
    """
    from adk.decisions import store

    for pattern in store._STATUS_ONLY:
        assert chr(8) not in pattern, f"literal backspace in {pattern!r}"


# -- the label is not the rule ------------------------------------------------

def test_info_card_with_options_is_refused():
    """MUTATION: remove the kind == 'info' branch and this chooser stores unvalidated."""
    with pytest.raises(DecisionError) as err:
        DecisionStore._validate(
            _card(kind="info", options=[_opt("a", "restart it"), _opt("b", "leave it")])
        )
    assert "info" in str(err.value).lower()


def test_info_label_cannot_smuggle_status_options():
    """The bypass in its most useful form: relabel, and skip the status check."""
    with pytest.raises(DecisionError):
        DecisionStore._validate(
            _card(kind="info", options=[_opt("a", "both are running"), _opt("b", "proceed")])
        )


def test_info_card_without_options_is_fine():
    """info is how prose is supposed to be filed; it must stay usable."""
    DecisionStore._validate(_card(kind="info", options=[]))


def test_any_card_offering_options_needs_a_default():
    """default_key is what makes a card safe to IGNORE.

    MUTATION: restore the old ``kind == "decision"`` guard and a 'blocked' card
    stores with no default -- so leaving it unanswered has no defined outcome.
    """
    with pytest.raises(DecisionError) as err:
        DecisionStore._validate(
            _card(kind="blocked", options=[_opt("a", "retry"), _opt("b", "escalate")])
        )
    assert "default_key" in str(err.value)


def test_blocked_card_with_a_default_is_accepted():
    DecisionStore._validate(
        _card(kind="blocked", default_key="a",
              options=[_opt("a", "approve"), _opt("b", "reject")])
    )


def test_decision_card_with_no_options_is_still_prose():
    with pytest.raises(DecisionError):
        DecisionStore._validate(_card(kind="decision", options=[]))


def test_credential_card_without_options_is_deliverable():
    """A credential card has NO options by design (DC008: the value enters via
    the masked prompt, never the card), so the options gate must not suppress
    its delivery — nothing else carries the ask to the owner. Measured
    2026-08-27: three secure-input cards (LAMBDA/HETZNER/GOOGLE) sat
    undelivered while 4912 option-card deliveries went out around them; the
    fanout skipped them at `if not card.options: return False`.
    MUTATION: restore the unconditional `if not card.options: return False`
    and this test fails — a credential card with no options must still deliver.
    """
    from adk.decisions.channels import ChannelConfig, DecisionChannelBridge
    from adk.decisions.store import DecisionCard

    cfg = ChannelConfig(platform="discord", enabled=True, min_urgency="normal")
    bridge = DecisionChannelBridge()
    card = DecisionCard(
        id="d-cred1",
        title="Secure input: TEST_KEY",
        kind="credential",
        options=[],
        urgency="high",
    )
    assert bridge.should_deliver(card, cfg) is True


def test_plain_optionless_card_is_still_not_deliverable():
    """The credential exemption is narrow: an ordinary card with nothing to
    reply with stays suppressed (an unanswerable message is an interruption).
    MUTATION: drop the `card.kind != "credential"` guard and every plain
    optionless card would page the owner — this test fails.
    """
    from adk.decisions.channels import ChannelConfig, DecisionChannelBridge
    from adk.decisions.store import DecisionCard

    cfg = ChannelConfig(platform="discord", enabled=True, min_urgency="normal")
    bridge = DecisionChannelBridge()
    card = DecisionCard(
        id="d-plain1",
        title="FYI only",
        kind="info",
        options=[],
        urgency="high",
    )
    assert bridge.should_deliver(card, cfg) is False
