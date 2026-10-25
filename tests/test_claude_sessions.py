"""Tests for adk.shell.claude_sessions — Claude Code session manager engine.

All tests run against synthetic journals in tmp_path; nothing touches the real
~/.claude/projects store and nothing spawns processes.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adk.shell import claude_sessions as cs

UUID_A = "11111111-2222-3333-4444-555555555555"
UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UUID_C = "99999999-8888-7777-6666-555555555555"


def _write_journal(root: Path, project: str, session_id: str, entries: list[dict]) -> Path:
    proj = root / project
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{session_id}.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return f


def _basic_entries(cwd: str, title: str, prompt: str, when: str, branch: str = "main"):
    return [
        {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": when,
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "cwd": cwd, "timestamp": when,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": f"reply about {title}"}]}},
        {"type": "ai-title", "aiTitle": title},
        {"type": "last-prompt", "lastPrompt": prompt},
    ]


@pytest.fixture
def projects(tmp_path):
    now = datetime.now(timezone.utc)
    cwd_a = str(tmp_path / "workA")
    cwd_b = str(tmp_path / "workB")
    Path(cwd_a).mkdir()
    Path(cwd_b).mkdir()
    root = tmp_path / "projects"
    _write_journal(root, "projA", UUID_A,
                   _basic_entries(cwd_a, "Fix vllm empty replies", "why is vllm empty",
                                  now.isoformat()))
    _write_journal(root, "projA", UUID_B,
                   _basic_entries(cwd_a, "Mailbox feature", "add shared mailbox",
                                  (now - timedelta(hours=5)).isoformat(), branch="feat/mail"))
    _write_journal(root, "projB", UUID_C,
                   _basic_entries(cwd_b, "Compose dedup", "dedupe compose files",
                                  (now - timedelta(days=3)).isoformat()))
    # Noise that must be ignored: non-UUID journal + nested subagent journal.
    _write_journal(root, "projB", "agent-deadbeef", _basic_entries(cwd_b, "noise", "x", now.isoformat()))
    nested = root / "projB" / UUID_C / "subagents"
    nested.mkdir(parents=True)
    (nested / f"{UUID_A}.jsonl").write_text("{}\n", encoding="utf-8")
    return root, cwd_a, cwd_b


def test_parse_session_meta(projects):
    root, cwd_a, _ = projects
    meta = cs.parse_session_meta(root / "projA" / f"{UUID_A}.jsonl")
    assert meta.id == UUID_A
    assert meta.title == "Fix vllm empty replies"
    assert meta.last_prompt == "why is vllm empty"
    assert meta.cwd == cwd_a
    assert meta.branch == "main"
    assert meta.when is not None


def test_scan_orders_and_skips_noise(projects):
    root, cwd_a, cwd_b = projects
    found = cs.scan_sessions(projects_root=root, detect_live=False)
    ids = [s.id for s in found]
    assert ids == [UUID_A, UUID_B, UUID_C]  # newest first, noise excluded


def test_scan_filters(projects):
    root, cwd_a, cwd_b = projects
    # text filter matches branch
    found = cs.scan_sessions(projects_root=root, text_filter="feat/mail", detect_live=False)
    assert [s.id for s in found] == [UUID_B]
    # lookback excludes the 3-day-old session
    found = cs.scan_sessions(projects_root=root, lookback_hours=24, detect_live=False)
    assert UUID_C not in [s.id for s in found]
    # per-dir collapses cwd_a to its newest session
    found = cs.scan_sessions(projects_root=root, per_dir=True, detect_live=False)
    assert [s.id for s in found] == [UUID_A, UUID_C]
    # missing cwd excluded unless include_missing
    import shutil
    shutil.rmtree(cwd_b)
    found = cs.scan_sessions(projects_root=root, detect_live=False)
    assert UUID_C not in [s.id for s in found]
    found = cs.scan_sessions(projects_root=root, include_missing=True, detect_live=False)
    assert UUID_C in [s.id for s in found]


def test_tail_read_large_journal(tmp_path):
    """Metadata written near the end must survive a bounded tail read."""
    root = tmp_path / "projects"
    cwd = str(tmp_path)
    filler = [{"type": "assistant", "cwd": cwd,
               "message": {"role": "assistant",
                           "content": [{"type": "text", "text": "x" * 2000}]}}] * 300
    entries = filler + _basic_entries(cwd, "Big journal", "the end",
                                      datetime.now(timezone.utc).isoformat())
    f = _write_journal(root, "projX", UUID_A, entries)
    assert f.stat().st_size > cs._TAIL_BYTES
    meta = cs.parse_session_meta(f)
    assert meta.title == "Big journal"
    assert meta.last_prompt == "the end"


def test_launch_cwd_wins_over_drifted_tail_cwd(tmp_path):
    """A session that cd'd elsewhere mid-conversation must still resume from
    the directory it was LAUNCHED in (that's where Claude's project lives)."""
    root = tmp_path / "projects"
    launch_dir = str(tmp_path / "launch")
    drift_dir = str(tmp_path / "drift")
    Path(launch_dir).mkdir()
    Path(drift_dir).mkdir()
    now = datetime.now(timezone.utc).isoformat()
    entries = (
        _basic_entries(launch_dir, "early", "hello", now)
        + _basic_entries(drift_dir, "Drifted session", "cd'd away", now)
    )
    f = _write_journal(root, "projX", UUID_A, entries)
    meta = cs.parse_session_meta(f)
    assert meta.cwd == launch_dir
    assert meta.title == "Drifted session"  # tail still wins for title/prompt


def test_expand_selection():
    assert cs.expand_selection("1,3,5-7", 10) == [1, 3, 5, 6, 7]
    assert cs.expand_selection("all", 3) == [1, 2, 3]
    assert cs.expand_selection("a", 2) == [1, 2]
    assert cs.expand_selection("7-5", 10) == [5, 6, 7]      # reversed range
    assert cs.expand_selection("0,99,2", 3) == [2]          # out of bounds dropped
    assert cs.expand_selection("2,2,2", 3) == [2]           # deduped
    assert cs.expand_selection("", 3) == []
    assert cs.expand_selection("garbage", 3) == []


def test_search_finds_text_and_snippets(projects):
    root, _, _ = projects
    hits = cs.search_sessions("shared mailbox", projects_root=root, days=0)
    assert len(hits) == 1
    assert hits[0].session.id == UUID_B
    assert hits[0].matches >= 1
    assert any("shared mailbox" in s for s in hits[0].snippets)


def test_search_ignores_tool_noise(tmp_path):
    """Matches inside tool_result blocks (not human text) must not count."""
    root = tmp_path / "projects"
    entries = [
        {"type": "user", "cwd": str(tmp_path),
         "timestamp": datetime.now(timezone.utc).isoformat(),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "secretword in tool output"}]}},
    ]
    _write_journal(root, "projX", UUID_A, entries)
    assert cs.search_sessions("secretword", projects_root=root, days=0) == []


def test_search_respects_days_bound(projects):
    root, _, _ = projects
    import os
    import time
    old = time.time() - 90 * 86400
    target = root / "projB" / f"{UUID_C}.jsonl"
    os.utime(target, (old, old))
    hits = cs.search_sessions("compose", projects_root=root, days=30)
    assert all(h.session.id != UUID_C for h in hits)


def test_detect_crash_heuristic():
    prev = {"claude_processes": 5, "sessions": [{}] * 5}
    assert cs.detect_crash(prev, now_count=0, terminal_alive=False)
    # terminal still up → not a crash (tabs closed on purpose)
    assert not cs.detect_crash(prev, now_count=0, terminal_alive=True)
    # some sessions still alive → not a mass-death
    assert not cs.detect_crash(prev, now_count=2, terminal_alive=False)
    # below threshold → intentional close of a couple of tabs
    assert not cs.detect_crash({"claude_processes": 2}, 0, False, min_sessions=3)
    assert not cs.detect_crash(None, 0, False)
    assert not cs.detect_crash({}, 0, False)


def test_snapshot_roundtrip(tmp_path, monkeypatch, projects):
    root, cwd_a, _ = projects
    monkeypatch.setattr(cs, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cs, "LIVE_SNAPSHOT", tmp_path / "state" / "live.json")
    monkeypatch.setattr(cs, "CRASH_SNAPSHOT", tmp_path / "state" / "crash.json")
    # Force one session live regardless of real processes on this machine.
    monkeypatch.setattr(cs, "claude_process_cwds", lambda: {cs._norm_path(cwd_a): 1})
    monkeypatch.setattr(cs, "claude_process_count", lambda: 1)
    snap = cs.snapshot_live(projects_root=root)
    assert len(snap["sessions"]) == 1
    assert snap["sessions"][0]["id"] == UUID_A  # newest journal in cwd_a wins
    cs.record_crash(snap)
    pending = cs.pending_crash()
    assert pending and pending["crashed_at"]
    restored = cs.snapshot_sessions(pending)
    assert [s.id for s in restored] == [UUID_A]
    assert restored[0].cwd == cwd_a
    cs.clear_crash()
    assert cs.pending_crash() is None


def test_mark_live_flags_top_n_per_cwd(projects, monkeypatch):
    root, cwd_a, _ = projects
    monkeypatch.setattr(cs, "claude_process_cwds", lambda: {cs._norm_path(cwd_a): 1})
    found = cs.scan_sessions(projects_root=root)
    live = {s.id for s in found if s.live}
    assert live == {UUID_A}  # only the newest session in cwd_a


def test_session_transcript_skips_noise(tmp_path):
    root = tmp_path / "projects"
    now = datetime.now(timezone.utc).isoformat()
    entries = [
        {"type": "user", "cwd": str(tmp_path), "timestamp": now,
         "message": {"role": "user", "content": "real question"}},
        {"type": "user", "cwd": str(tmp_path),
         "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
        {"type": "user", "cwd": str(tmp_path),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "tool noise"}]}},
        {"type": "assistant", "cwd": str(tmp_path),
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "real answer"}]}},
    ]
    f = _write_journal(root, "projX", UUID_A, entries)
    turns = cs.session_transcript(f)
    assert turns == [("user", "real question"), ("assistant", "real answer")]


def test_iter_journal_turns_incremental(tmp_path):
    root = tmp_path / "projects"
    now = datetime.now(timezone.utc).isoformat()
    f = _write_journal(root, "projX", UUID_A,
                       _basic_entries(str(tmp_path), "t", "first prompt", now))
    turns = list(cs.iter_journal_turns(f))
    assert [t[1] for t in turns] == ["first prompt", "reply about t"]
    watermark = turns[-1][3]
    # Append a new turn; only it should surface past the watermark.
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "cwd": str(tmp_path), "timestamp": now,
                             "message": {"role": "user", "content": "second prompt"}}) + "\n")
    new = list(cs.iter_journal_turns(f, from_byte=watermark))
    assert [t[1] for t in new] == ["second prompt"]


def test_launch_dry_run(projects):
    root, cwd_a, _ = projects
    found = cs.scan_sessions(projects_root=root, detect_live=False)
    lines = cs.launch_sessions(found[:1], dry_run=True)
    assert len(lines) == 1
    assert f"claude --resume {UUID_A}" in lines[0]
    assert cwd_a in lines[0]
