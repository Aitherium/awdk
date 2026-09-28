"""The session directory's background snapshot: reads never wait on a rebuild.

A cold ``GET /sessions/unified`` once took most of a minute because every row
was rebuilt on the request path, including a usage scan that read 100+ MB
transcripts from byte 0. These arms pin the replacement: a refresher thread owns
the rebuild, a read returns the last snapshot, and the usage scan advances by a
bounded number of bytes per tick.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from adk.harnesses import session_directory as sd
from adk.harnesses.discovery import DiscoveredSession


def _usage_line(mid: str, tokens: int, branch: str = "main", pad: int = 0) -> str:
    return json.dumps({
        "type": "assistant",
        "gitBranch": branch,
        "message": {"id": mid, "usage": {"input_tokens": tokens}},
        "pad": "x" * pad,
    }) + "\n"


def _disc(sid: str, transcript: str = "", status: str = "idle") -> DiscoveredSession:
    return DiscoveredSession(
        id=sid, cwd="/repo", name="repo main 10:00", pid=1, entrypoint="cli",
        kind="interactive", status=status, transcript_path=transcript,
    )


@pytest.fixture(autouse=True)
def _clean_caches():
    sd._USAGE_CACHE.clear()
    sd._PROMPT_CACHE.clear()
    yield
    sd._USAGE_CACHE.clear()
    sd._PROMPT_CACHE.clear()


# ── the byte budget ─────────────────────────────────────────────────────────


def test_usage_scan_reads_at_most_the_budget_per_call(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("".join(_usage_line(f"m{i}", 10, pad=200) for i in range(50)),
                 encoding="utf-8")
    size = t.stat().st_size
    budget = 1024

    offsets, results = [], []
    for _ in range(200):
        branch, tokens, caught_up = sd._scan_usage(str(t), max_bytes=budget)
        offset = sd._USAGE_CACHE[str(t)][0]
        offsets.append(offset)
        results.append((tokens, caught_up))
        if caught_up:
            break

    steps = [b - a for a, b in zip([0] + offsets, offsets)]
    assert all(0 < step <= budget for step in steps), steps
    assert results[0][1] is False, "a 12 KB file cannot be caught up in 1 KB"
    assert results[-1] == (500, True)
    assert offsets[-1] == size
    # Same answer as a one-shot scan.
    sd._USAGE_CACHE.clear()
    assert sd._transcript_usage(str(t)) == ("main", 500)


def test_a_line_longer_than_the_budget_is_consumed_whole(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line("big", 7, pad=5000) + _usage_line("small", 3), encoding="utf-8")
    _, tokens, caught_up = sd._scan_usage(str(t), max_bytes=100)
    assert tokens == 7 and caught_up is False  # the scan made progress, did not stall
    _, tokens, caught_up = sd._scan_usage(str(t), max_bytes=100)
    assert (tokens, caught_up) == (10, True)


def test_tokens_are_null_until_the_budgeted_scan_catches_up(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("".join(_usage_line(f"m{i}", 1, pad=300) for i in range(20)),
                 encoding="utf-8")
    directory = sd.SessionDirectory(discover_fn=lambda: [_disc("tab", str(t))])
    directory.usage_budget = 1024

    seen = []
    for _ in range(50):
        row = directory.refresh(deep=True).discovered[0]
        seen.append(row.tokens_spent)
        if row.tokens_spent is not None:
            break
    assert seen[0] is None
    assert seen[-1] == 20


# ── reads never block on a rebuild ──────────────────────────────────────────


def test_slow_discovery_does_not_block_the_first_read():
    release = threading.Event()

    def slow_discover():
        release.wait(10)
        return [_disc("tab")]

    directory = sd.SessionDirectory(discover_fn=slow_discover)
    directory.start_refresher(lambda: [], interval=0.05)
    try:
        started = time.monotonic()
        rows, meta = directory.list_with_meta([])
        elapsed = time.monotonic() - started
        assert elapsed < sd.FIRST_SNAPSHOT_WAIT_SECONDS + 0.5
        assert rows == [] and meta == {"generated_at": None, "stale": True}

        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows, meta = directory.list_with_meta([])
            if rows:
                break
            time.sleep(0.02)
        assert [r.id for r in rows] == ["tab"]
        assert meta["stale"] is False and meta["generated_at"] is not None
    finally:
        release.set()
        directory.stop_refresher()


def test_a_slow_refresh_serves_the_previous_snapshot_immediately():
    slow = threading.Event()
    release = threading.Event()

    def discover():
        if slow.is_set():
            release.wait(10)
        return [_disc("tab")]

    directory = sd.SessionDirectory(discover_fn=discover)
    directory.start_refresher(lambda: [], interval=0.05)
    try:
        deadline = time.monotonic() + 5
        while not directory.list_sessions_sync([]) and time.monotonic() < deadline:
            time.sleep(0.02)
        slow.set()
        # Past the inline cache TTL too, so only a snapshot read can pass this.
        time.sleep(sd.CACHE_TTL_SECONDS + 0.2)  # the refresher is parked in discovery
        for _ in range(20):
            started = time.monotonic()
            rows = directory.list_sessions_sync([])
            assert time.monotonic() - started < 0.1
            assert [r.id for r in rows] == ["tab"]
    finally:
        release.set()
        directory.stop_refresher()


def test_a_daemon_session_newer_than_the_snapshot_is_listed_without_io():
    directory = sd.SessionDirectory(discover_fn=lambda: [])
    directory.start_refresher(lambda: [], interval=60)
    try:
        directory.list_sessions_sync([])  # first snapshot exists, next tick is far away
        info = {"id": "d1", "title": "new", "cwd": "/w", "state": "starting",
                "transcript": "/nonexistent/t.jsonl"}
        rows = directory.list_sessions_sync([info])
        assert [(r.id, r.status, r.tokens_spent) for r in rows] == [("d1", "starting", None)]
        # ...and a stopped one is shown as exited at once, not two seconds later.
        rows = directory.list_sessions_sync([{**info, "state": "exited"}])
        assert rows[0].status == "exited" and rows[0].steer_capability == "none"
    finally:
        directory.stop_refresher()


def test_without_a_refresher_the_directory_still_answers_inline(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line("m1", 5), encoding="utf-8")
    directory = sd.SessionDirectory(discover_fn=lambda: [_disc("tab", str(t))])
    rows, meta = directory.list_with_meta([])
    assert rows[0].tokens_spent == 5
    assert meta["stale"] is False


# ── Claude Code's own registry status ───────────────────────────────────────


@pytest.mark.parametrize("registry, transcript, want", [
    ("busy", "waiting-input", "working"),
    ("waiting", "working", "waiting-permission"),
    ("idle", "waiting-input", "waiting-input"),
    ("idle", "blocked?", "blocked?"),
    ("idle", "working", "idle"),
    ("", "blocked?", "blocked?"),
])
def test_registry_status_outranks_the_transcript(registry, transcript, want):
    assert sd.merge_registry_status(registry, transcript) == want


def test_discovery_reads_bridge_id_and_waiting_for(tmp_path, monkeypatch):
    from adk.harnesses import discovery

    home = tmp_path / "home"
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    project = home / ".claude" / "projects" / discovery._encode_cwd("C:\\repo")
    project.mkdir(parents=True)
    (project / "abc.jsonl").write_text("", encoding="utf-8")
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "abc", "cwd": "C:\\repo", "name": "repo main 10:00",
        "entrypoint": "cli", "kind": "interactive", "status": "waiting",
        "waitingFor": "permission prompt", "bridgeSessionId": "session_x",
    }), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(discovery, "_process_is_alive", lambda pid, claim: True)

    found = discovery.discover_live_sessions(sessions_dir=str(sessions))
    assert len(found) == 1
    assert found[0].bridge_session_id == "session_x"
    assert found[0].waiting_for == "permission prompt"

    row = sd.SessionDirectory(discover_fn=lambda: found)._build_from_discovered(found)[0]
    assert row.status == "waiting-permission"
    assert row.last_activity_summary == "waiting: permission prompt"
    assert "waiting for approval" in row.title


# ── the HTTP surface ────────────────────────────────────────────────────────


def test_unified_route_reports_generated_at_and_stale(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    token = "root-bearer-for-tests-only"
    for key, sub in (("AITHER_DECISIONS_DIR", "decisions"), ("AITHER_STEER_DIR", "steer"),
                     ("AITHER_HARNESS_ROOMS_ROOT", "rooms"), ("AITHER_HARNESS_ROOT", "sessions"),
                     ("AITHER_STEER_DISPATCH_STATUS", "dispatch.json")):
        monkeypatch.setenv(key, str(tmp_path / sub))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", token)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    monkeypatch.setattr(rooms_mod, "_registry", None)
    directory = sd.SessionDirectory(discover_fn=lambda: [_disc("tab")])
    monkeypatch.setattr(sd, "_directory", directory)
    client = TestClient(daemon.create_app(
        manager=SessionManager(root=tmp_path / "sessions"), token=token))
    client.headers = {"Authorization": f"Bearer {token}"}

    body = client.get("/sessions/unified").json()
    assert [r["id"] for r in body["sessions"]] == ["tab"]
    assert body["stale"] is False and isinstance(body["generated_at"], float)

    health = client.get("/health").json()
    assert health["directory"]["running"] is False
