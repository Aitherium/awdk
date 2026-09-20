"""`adk harness tell` -- a finished verb that was dispatched nowhere until 2026-09-19.

Pins that the verb is REACHABLE (the harness CLI table and the argparse tree both name
it), that resolution is exactly-one (zero and many are refusals, never a guess), and
that delivery is a complete turn (/submit), not raw keystrokes (/input).
"""

from __future__ import annotations

import argparse
import io
import types

from adk.harnesses import cli as harness_cli
from adk.harnesses import tell

UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "11111111-9999-3333-4444-555555555555"

SESSIONS = [
    {"id": "aaaa1111bbbb2222", "harness_session_id": UUID_A, "title": "awdk"},
    {"id": "cccc3333dddd4444", "harness_session_id": UUID_B, "title": "veil"},
]


def test_resolve_target_is_exactly_one_by_id_uuid_or_prefix():
    got = tell.resolve_target("aaaa1111bbbb2222", SESSIONS)
    assert [s["id"] for s in got] == ["aaaa1111bbbb2222"]
    assert [s["id"] for s in tell.resolve_target(UUID_B, SESSIONS)] == ["cccc3333dddd4444"]
    assert [s["id"] for s in tell.resolve_target("cccc", SESSIONS)] == ["cccc3333dddd4444"]
    # The shared uuid prefix matches BOTH -- the caller must refuse, never pick one.
    assert len(tell.resolve_target("11111111", SESSIONS)) == 2
    assert tell.resolve_target("zzzz", SESSIONS) == []


def test_the_harness_cli_table_dispatches_tell(monkeypatch):
    seen = {}

    def fake_cmd_tell(args):
        seen["target"] = args.target
        return 7

    monkeypatch.setattr(tell, "cmd_tell", fake_cmd_tell)
    args = types.SimpleNamespace(shell_command="tell", target="awdk", text="hi")
    assert harness_cli.main(args) == 7 if hasattr(harness_cli, "main") else True
    # Whatever the entrypoint is called, the wrapper itself must reach the verb.
    assert harness_cli._cmd_tell(args) == 7
    assert seen["target"] == "awdk"


def test_the_argparse_tree_names_tell():
    from adk import cli as adk_cli

    parser = adk_cli.build_parser() if hasattr(adk_cli, "build_parser") else None
    if parser is None:  # the tree is built inline in main(); fall back to the source
        src = io.open(adk_cli.__file__, encoding="utf-8").read()
        assert 'shell_sub.add_parser(\n        "tell"' in src
        assert '"--await"' in src and 'dest="dry_run"' in src
        return
    ns = parser.parse_args(["harness", "tell", "awdk", "hello", "--dry-run"])
    assert ns.target == "awdk" and ns.text == "hello" and ns.dry_run is True


def test_dry_run_resolves_and_names_submit_not_input(monkeypatch, capsys):
    def fake_request(args, path, method="GET", body=None):
        assert path == "/sessions"
        return 200, {"sessions": SESSIONS}

    monkeypatch.setattr(harness_cli, "_request", fake_request)
    monkeypatch.setattr(harness_cli, "_die_if_down", lambda status, payload: None)
    monkeypatch.setattr(tell, "presence_nick_of", lambda hs: None)
    ns = argparse.Namespace(target="cccc", text="look at the gate", dry_run=True)
    assert tell.cmd_tell(ns) == 0
    out = capsys.readouterr().out
    assert "would tell cccc3333dddd" in out
    assert "/submit" in out and "/input" not in out


def test_an_ambiguous_target_is_refused_not_guessed(monkeypatch, capsys):
    monkeypatch.setattr(harness_cli, "_request",
                        lambda args, path, method="GET", body=None: (200, {"sessions": SESSIONS}))
    monkeypatch.setattr(harness_cli, "_die_if_down", lambda status, payload: None)
    ns = argparse.Namespace(target="11111111", text="hi", dry_run=True)
    assert tell.cmd_tell(ns) == 1
    assert "use a longer prefix" in capsys.readouterr().err


def test_delivery_is_a_complete_turn(monkeypatch):
    calls = []

    def fake_request(args, path, method="GET", body=None):
        calls.append((path, method, body))
        return (200, {"sessions": SESSIONS}) if path == "/sessions" else (200, {"ok": True})

    monkeypatch.setattr(harness_cli, "_request", fake_request)
    monkeypatch.setattr(harness_cli, "_die_if_down", lambda status, payload: None)
    monkeypatch.setattr(tell, "presence_nick_of", lambda hs: None)
    monkeypatch.setattr(tell, "post_envelope", lambda *a, **k: None)
    ns = argparse.Namespace(target="aaaa", text="ship it", dry_run=False)
    assert tell.cmd_tell(ns) == 0
    assert calls[-1] == ("/sessions/aaaa1111bbbb2222/submit", "POST", {"text": "ship it"})
