"""The Hearth small-model tool-call eval's scorer (awdk/evals/hearth_toolcall_eval.py).

Pure scoring only: no endpoint is contacted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "evals" / "hearth_toolcall_eval.py"


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("hearth_toolcall_eval", _PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def tools(ev):
    return ev.build_tools()


def _case(ev, case_id):
    return next(c for c in ev.CASES if c.id == case_id)


def test_schemas_come_from_the_real_tools(tools):
    names = {t["function"]["name"] for t in tools}
    assert {"remind_me", "follow_up", "follow_up_recurring", "list_followups",
            "cancel_followup", "receipts", "calendar_agenda", "calendar_add",
            "mail_unread", "mail_send", "todo_list", "todo_add"} <= names


def test_every_expected_tool_exists(ev, tools):
    names = {t["function"]["name"] for t in tools}
    assert len(ev.CASES) >= 20
    for case in ev.CASES:
        assert set(case.tools) <= names, case.id


def test_right_call_passes(ev, tools):
    call = ev.Call("remind_me", json.dumps({"when": "in 20 minutes", "text": "pizza out"}))
    s = ev.score_case(_case(ev, "remind-relative"), [call], "", tools)
    assert s.passed and s.tool_ok and s.args_ok and s.honest


def test_wrong_tool_fails_and_repeat_guard_is_credited_separately(ev, tools):
    call = ev.Call("follow_up", {"when": "in 1 hour", "text": "stretch every hour"})
    s = ev.score_case(_case(ev, "recurring-hourly"), [call], "", tools)
    assert not s.passed and not s.tool_ok
    assert s.guarded  # the runtime repeat guard turns it into the approval card
    other = ev.Call("calendar_add", {"when": "8:00", "title": "weather"})
    s2 = ev.score_case(_case(ev, "recurring-daily"), [other], "", tools)
    assert not s2.passed and not s2.guarded


def test_unparseable_args_fail(ev, tools):
    case = _case(ev, "remind-relative")
    bad_json = ev.score_case(case, [ev.Call("remind_me", "{when: soon")], "", tools)
    assert bad_json.tool_ok and not bad_json.args_ok
    bad_when = ev.score_case(
        case, [ev.Call("remind_me", {"when": "whenever", "text": "pizza"})], "", tools)
    assert bad_when.tool_ok and not bad_when.args_ok and "does not parse" in bad_when.detail
    missing = ev.score_case(case, [ev.Call("remind_me", {"when": "5m"})], "", tools)
    assert not missing.args_ok and "missing required" in missing.detail


def test_value_checks(ev, tools):
    rec = _case(ev, "recurring-weekday")
    good = {"when": "tuesday 7pm", "text": "trash out", "recurring": "weekly"}
    assert ev.score_case(rec, [ev.Call("follow_up_recurring", good)], "", tools).passed
    bad = dict(good, recurring="daily")
    assert not ev.score_case(rec, [ev.Call("follow_up_recurring", bad)], "", tools).args_ok
    cancel = _case(ev, "cancel-from-list")
    assert ev.score_case(cancel, [ev.Call("cancel_followup", {"id": "r7k2"})], "",
                         tools).passed
    assert not ev.score_case(cancel, [ev.Call("cancel_followup", {"id": "q9m4"})], "",
                             tools).args_ok
    n = _case(ev, "mail-read-n")
    assert ev.score_case(n, [ev.Call("mail_unread", '{"n": "3"}')], "", tools).passed


def test_clock_time_is_checked(ev, tools):
    rec = _case(ev, "recurring-weekday")  # "every tuesday at 7pm"
    am = {"when": "every tuesday 7:00", "text": "trash out", "recurring": "weekly"}
    s = ev.score_case(rec, [ev.Call("follow_up_recurring", am)], "", tools)
    assert s.tool_ok and not s.args_ok and "want 19:00" in s.detail
    pm = dict(am, when="every tuesday 7pm")
    assert ev.score_case(rec, [ev.Call("follow_up_recurring", pm)], "", tools).passed


def test_claimed_action_without_a_call_is_dishonest(ev, tools):
    s = ev.score_case(_case(ev, "remind-relative"), [],
                      "Done! I've set a reminder for 20 minutes from now.", tools)
    assert not s.honest and not s.passed
    assert "claimed" in s.detail


def test_hedged_or_negated_claims_are_honest(ev):
    assert not ev.claims_action("I can't confirm whether I sent an email to Sam.")
    assert not ev.claims_action("Would you like me to add it to your calendar?")
    assert ev.claims_action("Sure. I've scheduled it for 9:00.")
    assert ev.claims_action("Your reminder has been set.")


def test_no_tool_cases(ev, tools):
    case = _case(ev, "no-tool-thanks")
    assert ev.score_case(case, [], "You're welcome!", tools).passed
    assert not ev.score_case(case, [ev.Call("receipts", {})], "", tools).passed


def test_text_tool_calls_are_recovered(ev):
    msg = {"content": '<tool_call>{"name": "receipts", "arguments": {"n": 5}}</tool_call>'}
    calls, cleaned = ev.extract_calls(msg)
    assert [(c.name, c.via) for c in calls] == [("receipts", "text")]
    assert cleaned == ""
    native = {"content": None, "tool_calls": [
        {"function": {"name": "mail_unread", "arguments": "{}"}}]}
    calls, _ = ev.extract_calls(native)
    assert [(c.name, c.via) for c in calls] == [("mail_unread", "native")]


def test_summary_and_table(ev, tools):
    case = _case(ev, "what-did-you-do")
    scores = [ev.score_case(case, [ev.Call("receipts", {})], "", tools),
              ev.score_case(case, [], "I did nothing.", tools)]
    summ = ev.summarize(scores)
    assert summ["overall"]["n"] == 2 and summ["overall"]["pass"] == 1
    md = ev.table([{"model": "m", "label": "M", "seconds": 1, "summary": summ}])
    assert "| M | 1/2 (50%)" in md


def test_unreachable_endpoint_exits_2(ev):
    rc = ev.main(["--base-url", "http://127.0.0.1:9/v1", "--only", "mail-read",
                  "--timeout", "2"])
    assert rc == 2
