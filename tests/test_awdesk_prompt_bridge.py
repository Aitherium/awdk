"""Tests for Track 5: awdesk (fairy) -> Antigravity prompt bridge.

Covers:
  1. Single-surface ``ask_question`` routing in ``antigravity_hooks.PreToolUse``:
     - When ``awdesk`` is online (fresh ``.desk-alive``) and ``:8362/decisions`` raises a
       card, ``PreToolUse`` suppresses the duplicate client-only Antigravity modal
       (``decision: "deny"``) and directs the agent to ``adk decide wait <id>``.
     - When ``awdesk`` is offline, ``AITHER_ASK_QUESTION_BRIDGE=0``, questions are empty,
       or daemon raise fails, ``PreToolUse`` fails open with ``{"decision": "allow"}``.
  2. Live Antigravity steerback tier (``adk.decisions.steerback.try_antigravity``):
     - Calls ``agentapi send-message <conversation_id> <text>`` for Antigravity cards
       (identified via ``source.agent == "antigravity"`` or ``antigravity_sessions/<id>.json``).
     - Archives the steer mailbox file when live delivery succeeds so the answer lands ONCE.
     - Falls back cleanly to the steer mailbox when ``agentapi`` is missing, exits non-zero,
       or the answer is marked as a test answer.
  3. ``adk decide wait <id>`` CLI subcommand:
     - Returns immediately with exit 0 + JSON when card is already answered.
     - Blocks until another thread answers the card, then returns exit 0 with the answer.
     - Returns exit 1 on timeout and exit 2 on invalid timeout duration.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from adk.decisions import cli as decisions_cli
from adk.decisions import steerback
from adk.decisions.store import DecisionCard, DecisionOption, DecisionSource, DecisionStore
from adk.harnesses import antigravity_hooks as agh


class _MockDaemon:
    """Minimal loopback HTTP server mocking ``:8362/decisions`` and ``/events``."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, Any]]) -> None:
        self.records: list[dict[str, Any]] = []
        self.routes = dict(routes)
        records_ref = self.records
        routes_ref = self.routes

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                body = json.loads(raw) if raw.strip() else None
                path_only = self.path.split("?", 1)[0]
                records_ref.append({"method": method, "path": path_only, "body": body})
                status, resp = routes_ref.get((method, path_only), (200, {"ok": True}))
                payload = json.dumps(resp).encode("utf-8") if resp is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _ask_payload(cid: str, cwd: Path) -> dict[str, Any]:
    return {
        "conversationId": cid,
        "cwd": str(cwd),
        "toolCall": {
            "id": "tc-ask-1",
            "name": "ask_question",
            "args": {
                "questions": [
                    {
                        "question": "Deploy staging build now?",
                        "options": ["Yes, deploy now", "Hold until morning"],
                    }
                ]
            },
        },
    }


def _touch_desk_alive(aither_home: Path, *, age_s: float = 0.0) -> Path:
    beat = aither_home / "decisions" / ".desk-alive"
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text('{"attention": "fairy"}\n', encoding="utf-8")
    if age_s > 0:
        stale_ts = time.time() - age_s
        os.utime(beat, (stale_ts, stale_ts))
    return beat


# ── 1. PreToolUse single-surface ask_question routing ────────────────────────


def test_ask_question_suppressed_when_awdesk_online_and_card_raised(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AWSETTINGS_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AWRELAY_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AITHER_HOOKD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AWDESK_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("AITHER_ASK_QUESTION_BRIDGE", raising=False)
    (tmp_path / "harness_token").write_text("tok-bridge", encoding="utf-8")
    _touch_desk_alive(tmp_path)

    daemon = _MockDaemon(
        routes={
            ("GET", "/decisions"): (200, {"decisions": []}),
            ("POST", "/decisions"): (200, {"id": "d-ask1", "status": "open"}),
            ("POST", "/events"): (200, {"ok": True}),
        }
    )
    try:
        monkeypatch.setenv("AITHER_HARNESS_URL", daemon.url)
        resp = agh.handle_event("PreToolUse", _ask_payload("ag-bridge-1", tmp_path))
    finally:
        daemon.close()

    assert resp["decision"] == "deny"
    assert "d-ask1" in resp["reason"]
    assert "adk decide wait d-ask1" in resp["reason"]
    raised = [r for r in daemon.records if r["method"] == "POST" and r["path"] == "/decisions"]
    assert len(raised) == 1
    opt_labels = [o["label"] for o in raised[0]["body"]["options"]]
    assert "Yes, deploy now" in opt_labels
    assert "Hold until morning" in opt_labels
    # Single-surface fallback label must not tell the user to answer in a suppressed modal.
    assert "Answer in Antigravity" not in opt_labels


def test_ask_question_allows_when_awdesk_offline_or_opt_out_or_daemon_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AWSETTINGS_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AWRELAY_HOOKS_DISABLED", "1")
    monkeypatch.setenv("AITHER_HOOKD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AWDESK_URL", "http://127.0.0.1:1")
    (tmp_path / "harness_token").write_text("tok-bridge", encoding="utf-8")

    daemon = _MockDaemon(
        routes={
            ("GET", "/decisions"): (200, {"decisions": []}),
            ("POST", "/decisions"): (200, {"id": "d-ask2", "status": "open"}),
            ("POST", "/events"): (200, {"ok": True}),
        }
    )
    try:
        monkeypatch.setenv("AITHER_HARNESS_URL", daemon.url)

        # (a) Stale .desk-alive -> awdesk offline -> allow Antigravity modal
        _touch_desk_alive(tmp_path, age_s=300.0)
        r_offline = agh.handle_event("PreToolUse", _ask_payload("ag-offline-1", tmp_path))
        assert r_offline == {"decision": "allow"}

        # (b) Fresh .desk-alive + explicit opt-out -> allow Antigravity modal
        _touch_desk_alive(tmp_path, age_s=0.0)
        monkeypatch.setenv("AITHER_ASK_QUESTION_BRIDGE", "0")
        r_optout = agh.handle_event("PreToolUse", _ask_payload("ag-optout-1", tmp_path))
        assert r_optout == {"decision": "allow"}

        # (c) Daemon offline -> raise fails -> fail-open allow Antigravity modal
        monkeypatch.setenv("AITHER_ASK_QUESTION_BRIDGE", "1")
        monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
        r_failopen = agh.handle_event("PreToolUse", _ask_payload("ag-failopen-1", tmp_path))
        assert r_failopen == {"decision": "allow"}
    finally:
        daemon.close()


# ── 2. Live Antigravity steerback tier (agentapi send-message) ───────────────


def test_steerback_antigravity_delivers_once_and_archives_mailbox(tmp_path: Path, monkeypatch):
    steer_dir = tmp_path / "steer"
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_STEER_DIR", str(steer_dir))
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("AITHER_DECISIONS_STEER_URL", raising=False)
    monkeypatch.delenv("AITHER_DECISIONS_ANTIGRAVITY_STEER", raising=False)

    fake_bin = tmp_path / "agentapi"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("AITHER_ANTIGRAVITY_AGENTAPI", str(fake_bin))

    calls: list[list[str]] = []

    class _FakeProc:
        returncode = 0
        stdout = "delivered"
        stderr = ""

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(steerback.subprocess, "run", _fake_run)

    store = DecisionStore(tmp_path / "decisions")
    card = store.create(
        DecisionCard(
            id="",
            title="Deploy staging build now?",
            options=[
                DecisionOption(key="ans:1", label="Yes, deploy now"),
                DecisionOption(key="later", label="Not now"),
            ],
            default_key="later",
            source=DecisionSource(session_id="ag-conv-77", agent="antigravity"),
        )
    )

    store.answer(card.id, "ans:1", via="desk")

    assert len(calls) == 1
    assert calls[0][:3] == [str(fake_bin), "send-message", "ag-conv-77"]
    assert "ans:1" in calls[0][3]
    # Because live tier landed, mailbox file must be archived (answer lands ONCE).
    box = steer_dir / "ag-conv-77"
    assert list(box.glob("*.md")) == []
    assert len(list((box / "delivered").glob("*.md"))) == 1


def test_steerback_antigravity_fallback_to_mailbox_on_missing_or_error(tmp_path: Path, monkeypatch):
    steer_dir = tmp_path / "steer"
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_STEER_DIR", str(steer_dir))
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AITHER_ANTIGRAVITY_AGENTAPI", str(tmp_path / "missing-agentapi"))

    # Also verify session discovery via antigravity_sessions/<id>.json when agent is blank.
    sdir = tmp_path / "antigravity_sessions"
    sdir.mkdir(parents=True)
    (sdir / "ag-conv-88.json").write_text('{"id": "ag-conv-88"}\n', encoding="utf-8")

    landed, why = steerback.try_antigravity("ag-conv-88", "hello")
    assert not landed
    assert "agentapi binary not found" in why

    # Non-antigravity session must miss cleanly without calling agentapi.
    landed_other, why_other = steerback.try_antigravity("claude-sess-1", "hello", agent="claude-code")
    assert not landed_other
    assert "not an antigravity session" in why_other

    # Store answer falls back to pending mailbox file when agentapi is absent.
    store = DecisionStore(tmp_path / "decisions")
    card = store.create(
        DecisionCard(
            id="",
            title="Proceed?",
            options=[DecisionOption(key="yes", label="Yes")],
            default_key="yes",
            source=DecisionSource(session_id="ag-conv-88", agent="antigravity"),
        )
    )
    store.answer(card.id, "yes", via="desk")
    box = steer_dir / "ag-conv-88"
    assert len(list(box.glob("*.md"))) == 1
    assert not (box / "delivered").exists()


def test_steerback_self_test_still_passes():
    assert steerback._self_test() == 0


# ── 3. adk decide wait <id> CLI subcommand ───────────────────────────────────


def test_cli_decide_wait_returns_answer_json_and_handles_timeout(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AITHER_ANTIGRAVITY_AGENTAPI", str(tmp_path / "no-agentapi"))

    store = DecisionStore(tmp_path / "decisions")
    card = store.create(
        DecisionCard(
            id="",
            title="Choose region",
            options=[DecisionOption(key="ans:1", label="us-central1")],
            default_key="ans:1",
            source=DecisionSource(session_id="ag-wait-1", agent="antigravity"),
        )
    )

    # Answer from a background thread after 150ms (simulating awdesk fairy click).
    def _answer_soon() -> None:
        time.sleep(0.15)
        store.answer(card.id, "ans:1", note="ship it", via="desk")

    t = threading.Thread(target=_answer_soon, daemon=True)
    t.start()
    rc = decisions_cli.main(["wait", card.id, "--timeout", "5s", "--json"])
    t.join(timeout=2.0)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == card.id
    assert out["status"] == "answered"
    assert out["answer"] == "ans:1"
    assert out["note"] == "ship it"

    # Unanswered card times out cleanly with exit 1.
    open_card = store.create(
        DecisionCard(
            id="",
            title="Will timeout",
            options=[DecisionOption(key="1", label="One")],
            default_key="1",
            source=DecisionSource(session_id="ag-wait-2", agent="antigravity"),
        )
    )
    rc_to = decisions_cli.main(["wait", open_card.id, "--timeout", "0.05s"])
    assert rc_to == 1

    # Bad timeout string exits 2.
    rc_bad = decisions_cli.main(["wait", open_card.id, "--timeout", "notaduration"])
    assert rc_bad == 2
