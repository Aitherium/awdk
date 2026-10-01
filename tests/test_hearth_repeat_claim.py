"""Hearth honesty: "it repeats" needs follow_up_recurring to have run, not just any action.

Measured live 2026-10-01 on the hosted Hearth: after a ONE-TIME reminder the model said
"scheduled to repeat daily", and the check passed it because an action had run.
"""

from types import SimpleNamespace

import pytest
from adk.home import hearth


def honest(calls, text):
    rows = []
    core = SimpleNamespace(_turn_calls=list(calls),
                           _receipt=lambda *a: rows.append(a))
    return hearth.HearthCore._honest(core, text), rows


ONE_TIME = [("remind_me", True, "")]
REPEATING = [("follow_up_recurring", True, "")]


@pytest.mark.parametrize("draft", [
    "I've set a reminder for 9am, scheduled to repeat daily.",
    "Your reminder is set and will repeat every day.",
    "Done: I've set a daily reminder to take your vitamins.",
    "Reminder set for 9am. It repeats weekly.",
])
def test_repeat_claim_after_a_one_time_reminder_is_replaced(draft):
    reply, rows = honest(ONE_TIME, draft)
    assert reply.startswith("I set that once. It does not repeat"), draft
    assert rows and rows[0][1] == "repeat_claim_without_action"


def test_repeat_claim_is_kept_when_the_repeating_tool_ran():
    draft = "Done: your vitamins reminder now repeats daily."
    assert honest(REPEATING, draft) == (draft, [])


@pytest.mark.parametrize("draft", [
    "Reminder is set for 9am.",
    "I've set that for 9am. Want me to make it repeat daily?",
    "I've set it for 9am. I can make it repeat every day if you like.",
    "I've added the daily standup to your list.",
])
def test_one_time_replies_and_offers_are_kept(draft):
    assert honest(ONE_TIME, draft) == (draft, [])


def test_a_claim_with_no_action_is_still_replaced():
    reply, rows = honest([], "I've scheduled it to repeat daily.")
    assert reply.startswith("I did not do that") and rows[0][1] == "claim_without_action"
