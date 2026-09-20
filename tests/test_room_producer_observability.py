"""The two room producers must say WHEN they last worked, not just how often.

Why this file exists: ``published``/``rejected`` are monotonic since-boot counters, so a
health arm asserting ``published > 0`` passes forever on events delivered yesterday.
Measured 2026-09-19 on this box: ``spool.published`` was 11 across 139 spool files, the
newest spool file was ~7 h stale, and the room's newest event was NOW — the counter said
"working" about a producer that had delivered nothing all day.

🪤 The trap these tests are aimed at is a timestamp advanced on a TICK instead of on a
publish. That reads as "fresh" on an idle, wedged or misconfigured producer and rebuilds
the exact hole the timestamps were added to close, one level down — so the idle arms
below assert the field did NOT move while proving the tick really ran (``checked_at``
moves, so the assertion cannot pass by accident on a tick that never happened).

The clock is injected rather than real: on Windows ``time.time()`` has ~15.6 ms
granularity, so two publishes in the same test can land on the SAME float and a
``>`` assertion between them is flaky by construction.
"""

from __future__ import annotations

import json

import pytest
from adk.harnesses import spool as spool_mod
from adk.harnesses import transcript_bridge as bridge_mod
from adk.harnesses.rooms import RoomError

# ─────────────────────────────────────────────────────────────────────────────
# Doubles — a room that accepts or refuses, and a clock we control
# ─────────────────────────────────────────────────────────────────────────────

class _FakeRoom:
    def __init__(self, refuse: str = "") -> None:
        self.refuse = refuse
        self.events: list = []

    def publish(self, event):
        if self.refuse:
            raise RoomError(self.refuse)
        self.events.append(event)
        return event


class _FakeRegistry:
    def __init__(self, room: _FakeRoom) -> None:
        self.room = room

    def get_or_create(self, room_id: str = "main", title: str = "") -> _FakeRoom:
        return self.room


class _Clock:
    """Stands in for the ``time`` MODULE, because that is what the code imports."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def tick(self, seconds: float = 60.0) -> float:
        self.now += seconds
        return self.now


class _Session:
    """The three attributes the bridge reads off a discovered session."""

    def __init__(self, path, session_id: str = "sess-1", cwd: str = "/c/AitherOS-Fresh"):
        self.transcript_path = str(path)
        self.id = session_id
        self.cwd = cwd


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(spool_mod, "time", c)
    monkeypatch.setattr(bridge_mod, "time", c)
    return c


def _tailer(tmp_path, room: _FakeRoom, **kw) -> spool_mod.SpoolTailer:
    return spool_mod.SpoolTailer(
        registry=_FakeRegistry(room),
        directory=tmp_path,
        replay_existing=True,  # tests write BEFORE first sight; prod resumes at the end
        **kw,
    )


def _append(path, obj) -> None:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _hook_event(text: str = "working") -> dict:
    return {
        "v": 1,
        "room": "main",
        "type": "message",
        "actor": {"kind": "claude_code", "id": "sess-1", "name": "tab"},
        "payload": {"text": text},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spool: the timestamps move on work, and ONLY on work
# ─────────────────────────────────────────────────────────────────────────────

def test_spool_publish_stamps_when_it_happened(tmp_path, clock):
    room = _FakeRoom()
    tailer = _tailer(tmp_path, room)
    _append(tmp_path / "sess-1.jsonl", _hook_event())

    assert tailer.drain_once() == 1
    assert tailer.published == 1
    assert tailer.last_published_at == clock.now
    # Nothing was refused, so the rejection fields stay at their "never" sentinels.
    assert tailer.last_rejected_at == 0.0
    assert tailer.last_rejection == ""


def test_spool_idle_tick_does_not_advance_last_published_at(tmp_path, clock):
    room = _FakeRoom()
    tailer = _tailer(tmp_path, room)
    _append(tmp_path / "sess-1.jsonl", _hook_event())
    tailer.drain_once()
    stamped_at = tailer.last_published_at

    clock.tick(7 * 3600)  # the measured 7 h of staleness
    assert tailer.drain_once() == 0
    assert tailer.drain_once() == 0

    stats = tailer.stats()
    # The tick REALLY ran — checked_at is now 7 h later — and the publish stamp did
    # not follow it. Without both halves this arm could pass on a tick that no-oped.
    assert stats["checked_at"] == clock.now
    assert stats["last_published_at"] == stamped_at
    assert clock.now - stats["last_published_at"] == 7 * 3600


def test_spool_second_publish_moves_the_stamp_forward(tmp_path, clock):
    room = _FakeRoom()
    tailer = _tailer(tmp_path, room)
    path = tmp_path / "sess-1.jsonl"
    _append(path, _hook_event("first"))
    tailer.drain_once()
    first = tailer.last_published_at

    clock.tick(120)
    _append(path, _hook_event("second"))
    assert tailer.drain_once() == 1
    assert tailer.last_published_at == first + 120


def test_spool_rejection_carries_its_reason_verbatim(tmp_path, clock):
    reason = "unknown actor.kind 'wat'; expected one of claude_code, agent, human"
    tailer = _tailer(tmp_path, _FakeRoom(refuse=reason))
    _append(tmp_path / "sess-1.jsonl", _hook_event())

    assert tailer.drain_once() == 0
    assert tailer.rejected == 1
    # Verbatim: no "[aeon-spool]" tag, no "rejected event:" prefix. A checker reading
    # /health gets the producer's own words, which is what names the broken hook.
    assert tailer.last_rejection == reason
    assert tailer.last_rejected_at == clock.now
    assert tailer.last_published_at == 0.0


def test_spool_idle_tick_does_not_advance_last_rejected_at(tmp_path, clock):
    tailer = _tailer(tmp_path, _FakeRoom(refuse="event.type is required"))
    _append(tmp_path / "sess-1.jsonl", _hook_event())
    tailer.drain_once()
    rejected_at = tailer.last_rejected_at

    clock.tick(3600)
    tailer.drain_once()
    assert tailer.last_rejected_at == rejected_at
    assert tailer.rejected == 1


def test_spool_unparseable_lines_are_named_not_only_counted(tmp_path, clock):
    tailer = _tailer(tmp_path, _FakeRoom())
    path = tmp_path / "sess-1.jsonl"
    _append(path, "{not json at all")
    assert tailer.drain_once() == 0
    assert tailer.last_rejection == "malformed spool line"
    assert tailer.last_rejected_at == clock.now

    # A well-formed JSON line that is not an object used to be counted in TOTAL
    # silence — no stderr, no reason — which is the failure this module complains about.
    clock.tick(5)
    _append(path, "[1, 2, 3]")
    assert tailer.drain_once() == 0
    assert tailer.rejected == 2
    assert tailer.last_rejection == "spool line was not a JSON object"
    assert tailer.last_rejected_at == clock.now


def test_spool_still_resumes_at_current_end_by_default(tmp_path, clock):
    """Guard: the observability change must not turn the tailer into a replayer.

    A daemon restart that dumped 139 files of history into the live room would bury
    live traffic under archaeology — the reason the resume-at-end rule exists.
    """
    room = _FakeRoom()
    tailer = spool_mod.SpoolTailer(registry=_FakeRegistry(room), directory=tmp_path)
    _append(tmp_path / "sess-1.jsonl", _hook_event())

    assert tailer.drain_once() == 0
    assert tailer.published == 0
    assert tailer.last_published_at == 0.0
    assert room.events == []


# ─────────────────────────────────────────────────────────────────────────────
# Transcript bridge: same question, the other producer
# ─────────────────────────────────────────────────────────────────────────────

def _bridge(path, room: _FakeRoom) -> bridge_mod.TranscriptBridge:
    return bridge_mod.TranscriptBridge(
        registry=_FakeRegistry(room),
        replay_existing=True,
        discover_fn=lambda: [_Session(path)],
    )


def _assistant(*blocks) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def test_transcript_last_published_at_advances_on_an_assistant_text_block(tmp_path, clock):
    room = _FakeRoom()
    path = tmp_path / "sess-1.jsonl"
    _append(path, _assistant({"type": "text", "text": "landed the fix"}))
    bridge = _bridge(path, room)

    assert bridge.tick() == 1
    assert bridge.published == 1
    assert bridge.last_published_at == clock.now
    assert bridge.stats()["last_published_at"] == clock.now


def test_transcript_idle_tick_does_not_advance_last_published_at(tmp_path, clock):
    room = _FakeRoom()
    path = tmp_path / "sess-1.jsonl"
    _append(path, _assistant({"type": "text", "text": "landed the fix"}))
    bridge = _bridge(path, room)
    bridge.tick()
    stamped_at = bridge.last_published_at

    clock.tick(7 * 3600)
    assert bridge.tick() == 0  # the loop ran; there was simply nothing to deliver
    assert bridge.last_published_at == stamped_at
    assert clock.now - bridge.last_published_at == 7 * 3600


def test_transcript_row_that_publishes_nothing_does_not_advance_the_stamp(tmp_path, clock):
    """A tool RESULT arrives as a user entry with a list content — same trap
    ``hook_common.py`` documents — and maps to zero events. Zero events is not a
    publish, so the freshness stamp must not move."""
    room = _FakeRoom()
    path = tmp_path / "sess-1.jsonl"
    _append(path, {"type": "user", "message": {"content": [{"type": "tool_result",
                                                            "content": "ok"}]}})
    bridge = _bridge(path, room)

    assert bridge.tick() == 0
    assert bridge.published == 0
    assert bridge.last_published_at == 0.0


def test_transcript_refused_tool_call_row_does_not_advance_the_stamp(tmp_path, clock):
    room = _FakeRoom(refuse="unknown pillar 'mystery'")
    path = tmp_path / "sess-1.jsonl"
    _append(path, _assistant({"type": "tool_use", "name": "Read",
                              "input": {"file_path": "spool.py"}}))
    bridge = _bridge(path, room)

    assert bridge.tick() == 0
    assert bridge.rejected == 1
    assert bridge.last_error == "unknown pillar 'mystery'"
    assert bridge.last_published_at == 0.0


def test_transcript_accepted_tool_call_row_does_advance_the_stamp(tmp_path, clock):
    """DELIBERATE, and the one place this file differs from the plan's wording.

    ``last_published_at`` answers "is this producer delivering", not "is anyone
    chatting". A tab that spends an hour on tool calls and says nothing is the most
    ALIVE a session gets; stamping only on text blocks would report it as a dead
    producer, which is the same false verdict the monotonic counter already gives.
    Editorial filtering (what is worth voicing) belongs downstream, not here.
    """
    room = _FakeRoom()
    path = tmp_path / "sess-1.jsonl"
    _append(path, _assistant({"type": "tool_use", "name": "Read",
                              "input": {"file_path": "spool.py"}}))
    bridge = _bridge(path, room)

    assert bridge.tick() == 1
    assert bridge.last_published_at == clock.now


# ─────────────────────────────────────────────────────────────────────────────
# The wire contract: JSON-serialisable, and safe to read off an OLDER daemon
# ─────────────────────────────────────────────────────────────────────────────

_SENTINELS = {"last_published_at": 0.0, "last_rejected_at": 0.0, "last_rejection": ""}


def test_producer_stats_stay_json_serialisable(tmp_path, clock):
    spool_stats = _tailer(tmp_path, _FakeRoom()).stats()
    bridge_stats = _bridge(tmp_path / "sess-1.jsonl", _FakeRoom()).stats()

    # /health and /rooms return these straight to FastAPI: a non-primitive value here
    # is a 500 on the CHEAPEST probe there is.
    for stats in (spool_stats, bridge_stats):
        json.dumps(stats)
        for key, value in stats.items():
            assert isinstance(value, (str, int, float, bool)), key

    for key, sentinel in _SENTINELS.items():
        assert spool_stats[key] == sentinel
    assert bridge_stats["last_published_at"] == 0.0


def test_new_stats_keys_are_absent_safe_for_an_older_daemon(tmp_path, clock):
    """A consumer may be reading /health from a daemon that predates this change.

    The sentinel a reader supplies with ``.get(key, default)`` must mean the same
    thing as the value a NEW daemon sends when nothing has happened yet — otherwise
    "old daemon" and "never published" read as two different states and a checker has
    to know which daemon it is talking to.
    """
    old_daemon = {k: v for k, v in _tailer(tmp_path, _FakeRoom()).stats().items()
                  if k not in _SENTINELS}
    for key, sentinel in _SENTINELS.items():
        assert key not in old_daemon
        assert old_daemon.get(key, sentinel) == sentinel

    old_bridge = {k: v for k, v in _bridge(tmp_path / "s.jsonl", _FakeRoom()).stats().items()
                  if k != "last_published_at"}
    assert old_bridge.get("last_published_at", 0.0) == 0.0
    # And the pre-existing keys are untouched, so an old reader keeps working.
    assert {"published", "rejected", "running"} <= set(old_daemon)
    assert {"published", "rejected", "running", "last_error"} <= set(old_bridge)
