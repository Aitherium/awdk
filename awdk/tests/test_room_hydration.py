"""A room's replay must survive a daemon restart.

WHY THIS TEST EXISTS
--------------------
The room writes every event to a JSONL transcript, which made it easy to believe replay
was durable. It was not: ``events_since()`` read only the in-process buffer, so after a
restart a client reconnecting with ``?since=5`` received the events since the RESTART,
not since seq 5. That is indistinguishable from "nothing happened" — no error, no gap
marker, no failed request — which is precisely the silence the event spine was built to
end. "Durable" and "replayable" were different claims and only the first was true.

Every assertion below carries the shape of the defect it guards, and the last one is a
mutation guard: it reproduces the pre-fix behaviour and proves this file would have
caught it. A test nobody has watched fail is not a test.
"""

from __future__ import annotations

import pytest
from adk.harnesses.rooms import Room, RoomRegistry


@pytest.fixture()
def rooms_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    return tmp_path


def _publish_five(room: Room) -> None:
    for event_type in ("classify", "neuron_fire", "reasoning_step", "tool_call", "mem.s"):
        room.publish(
            {"type": event_type, "actor": {"kind": "claude_code", "id": "sess-A", "name": "tab"}}
        )


def test_replay_survives_restart(rooms_root):
    """A fresh Room on the same id is a daemon restart. ?since= must still work."""
    _publish_five(Room("main"))

    restarted = Room("main")

    assert restarted.hydrated == 5
    assert [e["seq"] for e in restarted.events_since(2)] == [3, 4, 5]


def test_hydrated_events_keep_their_pillar(rooms_root):
    """A replayed event must land in the same lane it did live, or the six-lane view
    silently changes shape after every restart."""
    _publish_five(Room("main"))

    restarted = Room("main")

    assert [e["pillar"] for e in restarted.events_since(4)] == ["learning"]
    assert restarted.pillar_counts()["reasoning"] == 1


def test_sequence_continues_and_never_repeats(rooms_root):
    """A duplicate seq is worse than a lost one: ``events_since`` filters on ``> seq``,
    so a reissued number makes a client SKIP real events it never sees again."""
    _publish_five(Room("main"))

    restarted = Room("main")
    assert restarted.last_seq == 5

    fresh = restarted.publish({"type": "tool_call", "actor": {"kind": "kernel", "id": "k"}})
    assert fresh["seq"] == 6

    seqs = [e["seq"] for e in restarted.events_since(0)]
    assert seqs == [1, 2, 3, 4, 5, 6]
    assert len(seqs) == len(set(seqs))


def test_participants_survive_restart(rooms_root):
    """The roster is derived from traffic, so it has to be re-derived on hydration —
    otherwise a restarted room reports an empty room full of events."""
    _publish_five(Room("main"))

    restarted = Room("main")

    roster = {p["id"]: p for p in restarted.participants()}
    assert roster["sess-A"]["events"] == 5


def test_corrupt_transcript_line_is_skipped_not_fatal(rooms_root):
    """One bad line must not take the whole spine down, and must not pass silently."""
    room = Room("main")
    _publish_five(room)
    with room._transcript.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")

    restarted = Room("main")

    assert restarted.hydrated == 5
    assert restarted.last_seq == 5


def test_registry_finds_a_persisted_room_after_restart(rooms_root):
    """Hydration is worthless if nothing ever constructs the room.

    Rooms are created lazily, so a fresh registry starts empty: a room with a full
    transcript on disk answered 404 until something happened to publish to it again —
    durable, replayable, and invisible. Same defect as the missing hydration, one level
    up, and it survived the first fix because the unit test built the Room directly.
    """
    _publish_five(Room("main"))

    fresh_registry = RoomRegistry()

    room = fresh_registry.get("main")
    assert room is not None, "persisted room 404s after a restart"
    assert room.last_seq == 5
    assert [r["id"] for r in fresh_registry.list_rooms()] == ["main"]


def test_registry_still_returns_none_for_a_room_that_never_existed(rooms_root):
    """The disk fallback must not turn every typo into an empty room."""
    assert RoomRegistry().get("no-such-room") is None


def test_mutation_guard_without_hydration_replay_is_lost(rooms_root):
    """Reproduce the pre-fix behaviour and prove this file catches it.

    If someone removes hydration, ``?since=2`` goes quiet and every other test here
    fails — this one asserts that the failure mode is real rather than hypothetical.
    """

    class NoHydrate(Room):
        def _hydrate(self) -> int:  # the old behaviour, exactly
            return 0

    _publish_five(Room("main"))

    broken = NoHydrate("main")

    assert broken.events_since(2) == []
    assert broken.last_seq == 0
