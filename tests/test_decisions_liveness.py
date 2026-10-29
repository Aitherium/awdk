"""adk.decisions.liveness -- which Claude Code sessions are alive AND waiting.

Pins the rule that removed eight zombie "needs you" cards (2026-10-28): a session
counts only while its process is running and it is not busy. "Not measured" is
None, and callers count the card then: an unknown must never hide an ask.
"""

from __future__ import annotations

import json
import os

from adk.decisions import liveness


def _write(dirpath, name: str, **fields) -> None:
    (dirpath / name).write_text(json.dumps(fields), encoding="utf-8")


def test_a_live_idle_session_counts_and_a_busy_one_does_not(tmp_path):
    dirpath = tmp_path / "sessions"
    dirpath.mkdir()
    _write(dirpath, "1.json", pid=os.getpid(), sessionId="sess-live", status="idle")
    _write(dirpath, "2.json", pid=os.getpid(), sessionId="sess-busy", status="busy")
    assert liveness.live_waiting_session_ids(dirpath) == {"sess-live"}


def test_a_dead_pid_does_not_count(tmp_path):
    dirpath = tmp_path / "sessions"
    dirpath.mkdir()
    _write(dirpath, "1.json", pid=999_999, sessionId="sess-dead", status="idle")
    assert liveness.live_waiting_session_ids(dirpath) == set()


def test_unreadable_files_and_junk_are_skipped_not_fatal(tmp_path):
    dirpath = tmp_path / "sessions"
    dirpath.mkdir()
    (dirpath / "broken.json").write_text("{not json", encoding="utf-8")
    _write(dirpath, "nopid.json", sessionId="sess-nopid", status="idle")
    _write(dirpath, "ok.json", pid=os.getpid(), sessionId="sess-ok", status="idle")
    (dirpath / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert liveness.live_waiting_session_ids(dirpath) == {"sess-ok"}


def test_an_unreadable_directory_is_not_measured(tmp_path):
    # None, not set(): "not measured" must make callers COUNT the card.
    assert liveness.live_waiting_session_ids(tmp_path / "missing") is None


def test_keep_waiting_uses_the_shared_beam_rule(tmp_path):
    zombie = {"id": "d-a2cd", "kind": "blocked", "options": [],
              "dedupe_key": "cc-waiting:sess-dead:1", "source": {"session_id": "sess-dead"}}
    live = {"id": "d-b3ef", "kind": "blocked", "options": [],
            "dedupe_key": "cc-waiting:sess-live:1", "source": {"session_id": "sess-live"}}
    real = {"id": "d-c4gh", "kind": "decision", "options": [{"key": "post"}]}
    kept = liveness.keep_waiting([zombie, live, real], {"sess-live"})
    assert [c["id"] for c in kept] == ["d-b3ef", "d-c4gh"]
    assert len(liveness.keep_waiting([zombie], None)) == 1     # unknown counts it
