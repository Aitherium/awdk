"""A room transcript must stay small enough to append to.

WHY THIS TEST EXISTS
--------------------
Measured 2026-09-18 on the owner's box: room "main" had grown to 554 MB of JSONL —
every Claude Code tab emits tool_call rows into it — and one ``POST /events`` against
that room took OVER 25 SECONDS while the identical request against a fresh room
answered in 0.23 s. Same route, same lock, same code: the only difference was the size
of the file being appended to.

Nothing reported this. The room kept answering GETs, the daemon was healthy, and the
producers that gave up (the desk's room publisher has a 4 s client timeout) simply
logged "daemon unreachable" — the spine looked DOWN while it was merely slow. The
in-memory buffer had a cap from day one; the file behind it never did.

The last test is the mutation guard: it reproduces the pre-fix behaviour (no cap) and
proves this file would have caught it.
"""

from __future__ import annotations

import json

import pytest
from adk.harnesses import rooms as rooms_mod
from adk.harnesses.rooms import Room


@pytest.fixture()
def rooms_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    return tmp_path


def _event(text: str) -> dict:
    return {
        "type": "agent_message",
        "actor": {"kind": "adk_agent", "id": "tester", "name": "tester"},
        "payload": {"text": text},
    }


def test_the_active_transcript_never_grows_past_the_cap(rooms_root, monkeypatch):
    monkeypatch.setattr(rooms_mod, "MAX_TRANSCRIPT_BYTES", 2000)
    room = Room("cap")
    for i in range(200):
        room.publish(_event(f"line {i} " + "x" * 50))
    assert room._transcript.stat().st_size <= 2000
    # The bytes are not lost: they moved into closed segments.
    segments = sorted(p.name for p in room.dir.glob("events-*.jsonl"))
    assert segments, "rotation produced no closed segment"
    total = room._transcript.stat().st_size + sum(
        (room.dir / name).stat().st_size for name in segments
    )
    assert total > 2000


def test_rotation_keeps_every_event_readable(rooms_root, monkeypatch):
    monkeypatch.setattr(rooms_mod, "MAX_TRANSCRIPT_BYTES", 1500)
    room = Room("keep")
    for i in range(60):
        room.publish(_event(f"event {i}"))
    # Segments are ordered by the SEQ they carry, never by filename: a rotation
    # suffix of 10 sorts before 2 as a string, and seq is the room's real order.
    rows = []
    for path in [*room.dir.glob("events-*.jsonl"), room._transcript]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rows.sort(key=lambda row: row["seq"])
    assert [row["payload"]["text"] for row in rows] == [f"event {i}" for i in range(60)]
    assert [row["seq"] for row in rows] == list(range(1, 61))


def test_a_room_that_starts_oversized_rotates_on_its_first_publish(rooms_root, monkeypatch):
    # This is the live case: the 554 MB transcript already existed, so the fix only
    # helps if the size is seeded at construction rather than counted from zero.
    monkeypatch.setattr(rooms_mod, "MAX_TRANSCRIPT_BYTES", 500)
    first = Room("legacy")
    first.dir.mkdir(parents=True, exist_ok=True)
    (first.dir / "events.jsonl").write_text("x" * 5000, encoding="utf-8")

    reopened = Room("legacy")
    assert reopened._transcript_bytes == 5000
    reopened.publish(_event("after restart"))
    assert reopened._transcript.stat().st_size < 500
    assert list(reopened.dir.glob("events-*.jsonl"))


def test_two_rotations_in_the_same_second_do_not_clobber_each_other(rooms_root, monkeypatch):
    monkeypatch.setattr(rooms_mod, "MAX_TRANSCRIPT_BYTES", 300)
    room = Room("fast")
    for i in range(40):
        room.publish(_event("y" * 120))
    segments = list(room.dir.glob("events-*.jsonl"))
    assert len(segments) >= 2
    assert len({p.name for p in segments}) == len(segments)


def test_mutation_guard_no_cap_means_one_ever_growing_file(rooms_root, monkeypatch):
    # The pre-fix behaviour, asserted so this file is known to be able to fail:
    # with rotation disabled every byte lands in one file and it grows without end.
    monkeypatch.setattr(rooms_mod, "MAX_TRANSCRIPT_BYTES", 0)
    room = Room("uncapped")
    for i in range(200):
        room.publish(_event(f"line {i} " + "x" * 50))
    assert room._transcript.stat().st_size > 2000
    assert not list(room.dir.glob("events-*.jsonl"))
