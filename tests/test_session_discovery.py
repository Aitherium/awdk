"""Tests for session discovery and unified directory.

These tests verify:
1. Status derivation from synthetic transcript tails (all states)
2. SDK/API session exclusion
3. Merged directory produces correct origin + steer_capability
4. Mutation guards ensuring status derivation is not a constant
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from adk.harnesses.discovery import (
    EXCLUDE_ENTRYPOINT_PATTERN,
    DiscoveredSession,
    discover_live_sessions,
)
from adk.harnesses.session_directory import (
    SessionDirectory,
    _derive_status_from_transcript,
)

# ── transcript tail fixtures ────────────────────────────────────────────────


def _write_transcript(path: Path, lines: list[dict]) -> None:
    """Write JSONL lines to a transcript file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _iso_ago(seconds: float) -> str:
    """An ISO-8601 timestamp N seconds in the past, for age-sensitive fixtures.

    Status derivation now depends on how OLD an event is, so a fixture with a
    hard-coded date is a slow-acting time bomb: it tests one scenario the week
    it is written and a different one a month later.
    """
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return stamp.isoformat().replace("+00:00", "Z")


def _make_event(
    type_: str,
    subtype: str = "",
    timestamp: str = "",
    message: dict | None = None,
    **extras,
) -> dict:
    """Build a transcript event."""
    obj: dict = {"type": type_}
    if subtype:
        obj["subtype"] = subtype
    if timestamp:
        obj["timestamp"] = timestamp
    if message:
        obj["message"] = message
    obj.update(extras)
    return obj


# ── status derivation: working ──────────────────────────────────────────────


def test_status_working_assistant_generating(tmp_path):
    """Status is 'working' when assistant is generating (stop_reason=tool_use)."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            # Timestamp must be RELATIVE. Hard-coded "2026-07-12" made this
            # fixture age into a different scenario: once the pending-tool rule
            # landed, a month-old unmatched tool_use correctly read as an
            # abandoned turn, and this test began failing for a reason that had
            # nothing to do with what it asserts.
            _make_event(
                "assistant",
                timestamp=_iso_ago(2),
                message={
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Read"}],
                },
            ),
        ],
    )
    status, _, _ = _derive_status_from_transcript(str(path))
    assert status == "working", "Mutation guard: if status becomes constant, this fails"


def test_status_working_max_tokens(tmp_path):
    """Status is 'working' when assistant hits max tokens."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            _make_event(
                "assistant",
                timestamp="2026-07-12T15:14:38.922Z",
                message={"stop_reason": "max_tokens", "content": []},
            ),
        ],
    )
    status, _, _ = _derive_status_from_transcript(str(path))
    assert status == "working", "Mutation guard: if status becomes constant, this fails"


# ── status derivation: waiting-input ────────────────────────────────────────


def test_status_waiting_input_after_turn_duration(tmp_path):
    """Status is 'waiting-input' after turn_duration system event."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            _make_event("assistant", timestamp="2026-07-12T15:14:35.000Z"),
            _make_event(
                "system",
                subtype="turn_duration",
                timestamp="2026-07-12T15:14:38.922Z",
                durationMs=3922,
            ),
        ],
    )
    status, _, _ = _derive_status_from_transcript(str(path))
    assert (
        status == "waiting-input"
    ), "Mutation guard: if status becomes constant, this fails"


def test_status_waiting_input_after_user_message(tmp_path):
    """Status is 'waiting-input' when a user message was just sent."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            _make_event(
                "user",
                timestamp="2026-07-12T15:14:38.922Z",
                message={"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ),
        ],
    )
    status, _, summary = _derive_status_from_transcript(str(path))
    assert status == "waiting-input", "Mutation guard: if status becomes constant, this fails"
    assert summary == "hello"


# ── status derivation: idle ─────────────────────────────────────────────────


def test_status_idle_after_away_summary(tmp_path):
    """Status is 'idle' after away_summary system event."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            _make_event(
                "system",
                subtype="away_summary",
                timestamp="2026-07-12T15:14:38.922Z",
                content="Working on feature X",
            ),
        ],
    )
    status, _, summary = _derive_status_from_transcript(str(path))
    assert status == "idle", "Mutation guard: if status becomes constant, this fails"
    assert summary == "Working on feature X"


def test_status_idle_no_activity(tmp_path):
    """Status is 'idle' when transcript is empty or has no meaningful events."""
    path = tmp_path / "t.jsonl"
    # Write a transcript with only system metadata (no user/assistant messages)
    _write_transcript(
        path,
        [
            _make_event("mode", mode="normal"),
            _make_event("permission-mode", permissionMode="auto"),
        ],
    )
    status, _, _ = _derive_status_from_transcript(str(path))
    assert status == "idle", "Mutation guard: if status becomes constant, this fails"


# ── status derivation: missing/invalid transcript ────────────────────────────


def test_status_unknown_transcript_missing(tmp_path):
    """Status is 'unknown' when transcript file doesn't exist."""
    path = tmp_path / "nonexistent.jsonl"
    status, _, _ = _derive_status_from_transcript(str(path))
    assert status == "unknown"


def test_status_handles_corrupted_json(tmp_path):
    """Status derivation gracefully handles corrupted JSON in transcript."""
    path = tmp_path / "t.jsonl"
    with open(path, "w") as f:
        f.write('{"type":"user","timestamp":"2026-07-12T15:14:38.922Z"}\n')
        f.write("not valid json\n")
        f.write('{"type":"system","subtype":"away_summary","content":"test"}\n')
    status, _, summary = _derive_status_from_transcript(str(path))
    # Should parse the valid away_summary line
    assert status == "idle"
    assert summary == "test"


# ── activity summary extraction ─────────────────────────────────────────────


def test_last_activity_summary_from_user_message(tmp_path):
    """last_activity_summary is extracted from user message."""
    path = tmp_path / "t.jsonl"
    long_prompt = "a" * 200
    _write_transcript(
        path,
        [
            _make_event(
                "user",
                message={"role": "user", "content": [{"type": "text", "text": long_prompt}]},
            ),
        ],
    )
    _, _, summary = _derive_status_from_transcript(str(path))
    # Should be truncated to ~80 chars
    assert len(summary) <= 100
    assert summary.startswith("a" * 80)


def test_last_activity_summary_from_away_summary(tmp_path):
    """last_activity_summary is extracted from away_summary event."""
    path = tmp_path / "t.jsonl"
    _write_transcript(
        path,
        [
            _make_event(
                "system",
                subtype="away_summary",
                content="Debugging the login flow",
            ),
        ],
    )
    _, _, summary = _derive_status_from_transcript(str(path))
    assert summary == "Debugging the login flow"


# ── timestamp parsing ──────────────────────────────────────────────────────


def test_last_activity_at_parsed_from_timestamp(tmp_path):
    """last_activity_at is parsed from ISO-8601 timestamp."""
    path = tmp_path / "t.jsonl"
    ts = "2026-07-12T15:14:38.922Z"
    _write_transcript(
        path,
        [_make_event("system", subtype="away_summary", timestamp=ts)],
    )
    _, activity_at, _ = _derive_status_from_transcript(str(path))
    # Parse the same timestamp
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    expected = dt.timestamp()
    assert abs(activity_at - expected) < 1.0


# ── discovery: sdk/api exclusion ────────────────────────────────────────────


def test_discover_excludes_sdk_entrypoint(tmp_path):
    """Discovered sessions with entrypoint matching ^sdk are excluded."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # State file with sdk entrypoint
    state = {
        "sessionId": "sdk-session-123",
        "pid": 99999,
        "procStart": int(time.time() * 1e7),  # Far future
        "cwd": "C:\\Project",
        "name": "SDK Session",
        "kind": "interactive",
        "entrypoint": "sdk-cli",  # Should be excluded
        "status": "ready",
    }
    with open(sessions_dir / "99999.json", "w") as f:
        json.dump(state, f)

    # With the default exclusion pattern, this should be filtered out
    discovered = discover_live_sessions(sessions_dir=str(sessions_dir))
    assert len(discovered) == 0


def test_discover_excludes_api_entrypoint(tmp_path):
    """Discovered sessions with entrypoint matching ^api are excluded."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    state = {
        "sessionId": "api-session-456",
        "pid": 99998,
        "procStart": int(time.time() * 1e7),
        "cwd": "C:\\Project",
        "name": "API Session",
        "kind": "interactive",
        "entrypoint": "api-runner",
        "status": "ready",
    }
    with open(sessions_dir / "99998.json", "w") as f:
        json.dump(state, f)

    discovered = discover_live_sessions(sessions_dir=str(sessions_dir))
    assert len(discovered) == 0


def test_discover_custom_exclusion_pattern(tmp_path):
    """Exclusion pattern can be customized."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    state = {
        "sessionId": "test-session",
        "pid": 99997,
        "procStart": int(time.time() * 1e7),
        "cwd": "C:\\Project",
        "name": "Test",
        "kind": "interactive",
        "entrypoint": "test",
        "status": "ready",
    }
    with open(sessions_dir / "99997.json", "w") as f:
        json.dump(state, f)

    # Custom pattern that excludes "test"
    discovered = discover_live_sessions(sessions_dir=str(sessions_dir), exclude_entrypoint="^test")
    assert len(discovered) == 0


# ── unified directory: merge ────────────────────────────────────────────────


def test_unified_directory_merge_daemon_and_discovered(tmp_path):
    """Unified directory merges daemon sessions with discovered sessions."""
    directory = SessionDirectory()

    # Daemon session
    daemon_sessions = [
        {
            "id": "daemon-001",
            "title": "Daemon Session",
            "cwd": "/project1",
            "harness": "claude",
            "harness_label": "Claude Code",
            "state": "idle",
            "transcript": "",
        }
    ]

    # Discovered session (simulated)
    # (Can't easily create real discovered sessions without live process)
    # The merge would include both if they existed

    unified = directory.list_sessions_sync(daemon_sessions)
    assert len(unified) >= 1
    daemon_unified = [s for s in unified if s.origin == "daemon"]
    assert len(daemon_unified) == 1
    assert daemon_unified[0].steer_capability == "full"


def test_unified_directory_deduplicates_by_id(tmp_path):
    """Unified directory deduplicates sessions by id (daemon takes precedence)."""
    directory = SessionDirectory()

    daemon_sessions = [
        {
            "id": "session-001",
            "title": "In Daemon",
            "cwd": "/project",
            "harness": "claude",
            "harness_label": "Claude Code",
            "state": "idle",
            "transcript": "",
        }
    ]

    unified = directory.list_sessions_sync(daemon_sessions)
    # Count sessions with id "session-001" — should be exactly 1
    matching = [s for s in unified if s.id == "session-001"]
    assert len(matching) == 1
    assert matching[0].origin == "daemon"


def test_unified_directory_caches_results(tmp_path):
    """Unified directory caches results for short TTL.

    Discovery is injected as a no-op so this asserts the CACHE, not the host.
    With the real probe it saw whatever Claude windows happened to be open.
    """
    directory = SessionDirectory(discover_fn=lambda: [])
    daemon_sessions = [
        {
            "id": "test-001",
            "title": "Test",
            "cwd": "/tmp",
            "harness": "claude",
            "harness_label": "Claude Code",
            "state": "idle",
            "transcript": "",
        }
    ]

    # First call
    result1 = directory.list_sessions_sync(daemon_sessions)
    # Second call (should use cache, so count should be same)
    result2 = directory.list_sessions_sync(daemon_sessions)
    assert result1 is result2, "Cache should return the same list object"


# ── discovered session origin and capability ────────────────────────────────


def test_daemon_session_has_full_steering(tmp_path):
    """Daemon sessions have 'full' steering capability.

    Discovery injected empty: this test is about the daemon branch, and on a
    box with live Claude tabs the real probe made `len(unified) == 1` mean
    `== 1 + however many windows the owner had open` (measured: 20).
    """
    directory = SessionDirectory(discover_fn=lambda: [])
    daemon_sessions = [
        {
            "id": "daemon-123",
            "title": "Test",
            "cwd": "/tmp",
            "harness": "claude",
            "harness_label": "Claude Code",
            "state": "ready",
            "transcript": "",
        }
    ]
    unified = directory.list_sessions_sync(daemon_sessions)
    assert len(unified) == 1
    assert unified[0].origin == "daemon"
    assert unified[0].steer_capability == "full"


def test_discovered_session_has_turn_boundary_steering(tmp_path):
    """Discovered tab sessions have 'turn-boundary' steering capability.

    Mutation guard: if steer_capability becomes a constant, this test fails.
    """
    directory = SessionDirectory()

    # Manually call the private builder with a discovered session
    discovered = [
        DiscoveredSession(
            id="discovered-999",
            cwd="/project",
            name="Found Session",
            pid=9999,
            entrypoint="cli",
            kind="interactive",
            status="idle",
            transcript_path="",
        )
    ]
    unified = directory._build_from_discovered(discovered)
    assert len(unified) == 1
    assert unified[0].origin == "discovered"
    assert (
        unified[0].steer_capability == "turn-boundary"
    ), "Mutation guard: if this becomes a constant, test fails"


# ── regex exclusion pattern validation ──────────────────────────────────────


def test_exclude_entrypoint_pattern_matches_sdk():
    """The default EXCLUDE_ENTRYPOINT_PATTERN matches sdk*."""
    import re

    pattern = re.compile(EXCLUDE_ENTRYPOINT_PATTERN)
    assert pattern.match("sdk-cli")
    assert pattern.match("sdk")
    assert not pattern.match("cli")


def test_exclude_entrypoint_pattern_matches_api():
    """The default EXCLUDE_ENTRYPOINT_PATTERN matches api*."""
    import re

    pattern = re.compile(EXCLUDE_ENTRYPOINT_PATTERN)
    assert pattern.match("api-runner")
    assert pattern.match("api")
    assert not pattern.match("cli")


def test_discovery_never_walks_the_projects_tree(tmp_path, monkeypatch):
    """Mutation guard: transcript lookup must not use a recursive `**` glob.

    Measured 2026-08-09 on a box with 19 live sessions: resolving each
    transcript with `glob("**/<encoded>/<id>.jsonl")` walked the ENTIRE
    ~/.claude/projects tree — 50,000 directory scans, ~9s per pass. That does
    not merely make discovery slow, it defeats the cockpit: SessionDirectory's
    cache TTL is 2s, so the cache could never hit, every 2s UI poll re-walked
    the tree, and the view sat permanently ~9s stale. After switching to a
    direct path check the same 19 sessions resolve in 0.014s.

    A pattern with no wildcard before the filename has exactly one possible
    answer, so this asserts the shape rather than a timing threshold — a
    duration assertion would be flaky on a loaded box and would get deleted.
    """
    import pathlib

    real_glob = pathlib.Path.glob
    offenders: list[str] = []

    def guarded_glob(self, pattern, *a, **kw):
        if "**" in str(pattern):
            offenders.append(str(pattern))
        return real_glob(self, pattern, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "glob", guarded_glob)
    discover_live_sessions()
    assert not offenders, f"recursive glob reintroduced in discovery: {offenders}"


def _tool_line(ts, tool_id, kind="tool_use", name="Bash"):
    """One transcript line carrying a tool_use or its matching tool_result."""
    if kind == "tool_use":
        block = {"type": "tool_use", "id": tool_id, "name": name}
    else:
        block = {"type": "tool_result", "tool_use_id": tool_id}
    return json.dumps({
        "type": "assistant", "timestamp": ts, "message": {"content": [block]},
    })


def test_pending_tool_pairs_by_id_not_position():
    """Out-of-order completion must not report a phantom pending tool.

    A turn can issue several tool calls at once and they finish in any order.
    Index-matching would call this pending; id-matching correctly sees none.
    """
    from adk.harnesses.session_directory import _pending_tool_use

    lines = [
        _tool_line("2026-08-09T12:00:00.000Z", "t1"),
        _tool_line("2026-08-09T12:00:01.000Z", "t2"),
        _tool_line("2026-08-09T12:00:02.000Z", "t2", "tool_result"),
        _tool_line("2026-08-09T12:00:03.000Z", "t1", "tool_result"),
    ]
    name, started = _pending_tool_use(lines)
    assert name is None, f"reported {name} pending when both tools completed"
    assert started == 0.0


def test_pending_tool_detected_when_unmatched():
    """A tool_use with no tool_result is found, with its name and start time."""
    from adk.harnesses.session_directory import _pending_tool_use

    lines = [
        _tool_line("2026-08-09T12:00:00.000Z", "done"),
        _tool_line("2026-08-09T12:00:01.000Z", "done", "tool_result"),
        _tool_line("2026-08-09T12:00:02.000Z", "stuck", name="Edit"),
    ]
    name, started = _pending_tool_use(lines)
    assert name == "Edit", f"expected Edit pending, got {name}"
    assert started > 0


def test_blocked_status_only_after_threshold(tmp_path):
    """Mutation guard: a FRESH pending tool is 'working', an OLD one is 'blocked?'.

    If the threshold is removed and every pending tool reports blocked, the
    cockpit cries wolf on every normal turn and the operator stops reading it.
    If 'blocked?' is never returned, a session waiting on a permission prompt is
    indistinguishable from one doing work — the case the cockpit exists for.
    """
    import time as _t
    from datetime import datetime, timezone

    from adk.harnesses.session_directory import (
        PENDING_TOOL_BLOCKED_SECONDS,
        _derive_status_from_transcript,
    )

    def _write(age_seconds):
        ts = datetime.fromtimestamp(
            _t.time() - age_seconds, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        f = tmp_path / f"t{int(age_seconds)}.jsonl"
        f.write_text(_tool_line(ts, "pending", name="Bash") + "\n", encoding="utf-8")
        return str(f)

    fresh, _, fresh_summary = _derive_status_from_transcript(_write(5))
    assert fresh == "working", f"fresh pending tool should be working, got {fresh}"
    assert "Bash" in fresh_summary

    old_status, _, old_summary = _derive_status_from_transcript(
        _write(PENDING_TOOL_BLOCKED_SECONDS + 120)
    )
    assert old_status == "blocked?", f"stale pending tool should be blocked?, got {old_status}"
    assert "Bash" in old_summary and "approval" in old_summary


def test_waiting_input_summary_is_never_empty(tmp_path):
    """Every waiting-input row carries context — a string prompt, or a fallback.

    Measured live: ~a third of rows rendered an empty summary because `content`
    is a plain STRING for a typed prompt and only the LIST form was handled. An
    empty cell reads as "nothing to report" when the truth is "this session is
    waiting for you", so the operator has to open the tab anyway.
    """
    from adk.harnesses.session_directory import _derive_status_from_transcript

    cases = {
        "string": "please fix the failing test",
        "blocks": None,
        "toolresult": None,
    }
    # 1) content as a bare string
    p1 = tmp_path / "s.jsonl"
    p1.write_text(json.dumps({
        "type": "user", "timestamp": _iso_ago(5),
        "message": {"content": cases["string"]},
    }) + "\n", encoding="utf-8")
    status, _, summary = _derive_status_from_transcript(str(p1))
    assert status == "waiting-input"
    assert "fix the failing test" in summary, f"string prompt lost: {summary!r}"

    # 2) content as blocks with no text block at all
    p2 = tmp_path / "b.jsonl"
    p2.write_text(json.dumps({
        "type": "user", "timestamp": _iso_ago(5),
        "message": {"content": [{"type": "image"}]},
    }) + "\n", encoding="utf-8")
    status2, _, summary2 = _derive_status_from_transcript(str(p2))
    assert status2 == "waiting-input"
    assert summary2.strip(), "empty summary returned — the row would carry no context"
