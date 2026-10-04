"""``adk operator``: the typed words go to /api/operator under the caller's own session,
the server's answer is printed as given, and nothing runs without a session."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from adk import operator_cli as oc


@pytest.fixture
def wire(monkeypatch):
    calls = []
    replies = {}

    def send(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": json.loads(body) if body else None})
        status, payload = replies.get((method, url.rsplit("/api/operator/", 1)[-1]),
                                      (404, {"error": "not_found"}))
        return status, json.dumps(payload)

    monkeypatch.setattr(oc, "_send", send)
    monkeypatch.setenv("AITHER_OPERATOR_URL", "https://portal.example")
    monkeypatch.setattr("adk.devices.resolve_bearer", lambda: "member-session")
    return calls, replies


def test_ask_posts_only_the_typed_text_under_the_member_session(wire, capsys):
    calls, replies = wire
    replies[("POST", "request")] = (200, {"decisions": [
        {"status": "pending_approval", "message": "Waiting for the owner's OK.",
         "approval_id": "opr_1"}]})
    rc = oc.cmd_operator(SimpleNamespace(operator_command="ask",
                                         text=["pause", "lending", "on", "the", "deck"]))
    assert rc == 0
    assert calls == [{"method": "POST", "url": "https://portal.example/api/operator/request",
                      "headers": calls[0]["headers"], "body": {"text": "pause lending on the deck"}}]
    assert calls[0]["headers"]["Authorization"] == "Bearer member-session"
    assert "card opr_1" in capsys.readouterr().out


def test_no_session_calls_nothing(wire, monkeypatch, capsys):
    calls, _ = wire
    monkeypatch.setattr("adk.devices.resolve_bearer", lambda: "")
    assert oc.cmd_operator(SimpleNamespace(operator_command="approvals")) == 1
    assert calls == [] and "adk login" in capsys.readouterr().out


def test_a_refusal_is_printed_and_fails(wire, capsys):
    calls, replies = wire
    replies[("GET", "approvals")] = (403, {"detail": "Only the workspace owner or a co-guardian"})
    assert oc.cmd_operator(SimpleNamespace(operator_command="approvals")) == 1
    assert "403" in capsys.readouterr().out


def test_approve_and_deny_send_allow(wire):
    calls, replies = wire
    replies[("POST", "approvals/opr_1")] = (200, {"decision": {"status": "executed"}})
    oc.cmd_operator(SimpleNamespace(operator_command="approve", card_id="opr_1", deny=False))
    oc.cmd_operator(SimpleNamespace(operator_command="approve", card_id="opr_1", deny=True))
    assert [c["body"] for c in calls] == [{"allow": True}, {"allow": False}]


def test_parser_registers_operator():
    import argparse

    p = argparse.ArgumentParser()
    oc.add_parser(p.add_subparsers(dest="command"))
    a = p.parse_args(["operator", "ask", "pool", "diagnostics"])
    assert (a.command, a.operator_command, a.text) == ("operator", "ask", ["pool", "diagnostics"])
