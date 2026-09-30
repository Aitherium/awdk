"""awsh /hearth reaches the PLATFORM Hearth (the gateway's hearth_* tools).

Pins: each platform verb maps to exactly one hearth_* tool with no identity argument;
``/hearth cloud`` always goes to the gateway; with no local serve the platform verbs fall
back to it and say so; free text still needs the local serve.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from adk.home import gateway_hearth as gh
from adk.shell.plugins.builtins import hearth as hearth_plugin

STATUS = {"home": "abcd1234", "time": "09:00", "reminders": 1, "unread_mail": 0, "outbox": 0,
          "tainted_by": ["mail"], "pending_approvals": [{"code": "ab12cd34",
                                                         "summary": "send an email to sam"}]}


class Gateway:
    def __init__(self, answers: Dict[str, Dict[str, Any]]):
        self.answers = answers
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, arguments))
        return self.answers.get(name, {})


@pytest.mark.parametrize("args, tool, arguments", [
    (["status"], "hearth_status", {}),
    (["reminders"], "hearth_reminders", {}),
    (["due"], "hearth_due", {}),
    (["receipts", "3"], "hearth_receipts", {"n": 3, "verify": True}),
    (["yes", "ab12cd34"], "hearth_approve", {"code": "ab12cd34", "allow": True}),
    (["no", "ab12cd34"], "hearth_approve", {"code": "ab12cd34", "allow": False}),
    (["remind", "in", "10", "minutes", "--", "stretch"], "hearth_remind",
     {"when": "in 10 minutes", "text": "stretch"}),
])
def test_each_verb_is_one_tool_call_with_no_identity(args, tool, arguments):
    gw = Gateway({})
    gh.run_verb(args, call=gw)
    assert gw.calls == [(tool, arguments)]


def test_status_shows_the_codes_waiting_for_the_owner():
    out = gh.run_verb(["status"], call=Gateway({"hearth_status": STATUS}))
    assert "/hearth yes ab12cd34" in out and "untrusted content" in out


def test_receipts_verdict_and_errors_are_named():
    out = gh.run_verb(["receipts"], call=Gateway({"hearth_receipts": {
        "rows": [{"seq": 1, "kind": "action", "name": "hearth_remind", "approval": None}],
        "verify": {"exit": 1, "reason": "row 1 bad signature"}}}))
    assert out.startswith("receipts: BROKEN -- row 1 bad signature")
    refused = gh.run_verb(["yes", "deadbeef"], call=Gateway({"hearth_approve": {
        "error": "no pending approval with that code; nothing ran"}}))
    assert refused == "hearth: no pending approval with that code; nothing ran"


def test_bad_usage_sends_nothing():
    gw = Gateway({})
    for args in (["receipts", "x"], ["remind", "--", "x"], ["approve", "ab12cd34"], ["yes"]):
        assert gh.run_verb(args, call=gw).startswith("hearth:")
    assert gw.calls == []


@pytest.fixture
def no_local(tmp_path, monkeypatch):
    """No ``adk home serve`` running: an empty home with no local.token."""
    from adk.home import config as hc

    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "agent-home"))
    gw = Gateway({"hearth_status": STATUS})
    monkeypatch.setattr(gh, "call_gateway", gw)
    return gw


def test_cloud_prefix_goes_to_the_gateway(no_local):
    out = hearth_plugin.hearth_command(["cloud", "status"])
    assert no_local.calls == [("hearth_status", {})] and "ab12cd34" in out


def test_platform_verbs_fall_back_when_nothing_serves(no_local):
    out = hearth_plugin.hearth_command(["status"])
    assert out.startswith("(local Hearth unavailable:") and "platform Hearth:" in out
    assert no_local.calls == [("hearth_status", {})]


def test_free_text_still_needs_the_local_serve(no_local):
    out = hearth_plugin.hearth_command(["hello"])
    assert out.startswith("hearth: ") and "adk home serve" in out
    assert "/hearth cloud status" in out
    assert no_local.calls == []
