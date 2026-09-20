"""Addressed events, publish listeners and ``last_event_ts`` on the room spine.

WHY THIS TEST EXISTS
--------------------
``Room._normalise`` returns a FIXED key set. Any field a producer sends that is not in
that set is dropped on the floor without a word — so a ``to`` validated on the HTTP route
and never taught to ``_normalise`` would arrive at a dispatcher as "not addressed", and
the steer path would look wired while delivering nothing. The first arm is that
regression.

Every room producer on the box crosses this file, including the two that never touch
HTTP (the spool tailer and the transcript bridge). The absent-safe arm pins their CURRENT
envelopes byte-for-byte: adding addressing must not add a single key to an event that
did not ask for it.

Listeners are how the dispatcher hears about addressed events. A listener registered at
boot must also cover rooms created later (rooms are created lazily, on first publish),
and a listener that raises must never cost the event — both are pinned here.
"""

from __future__ import annotations

import json

import pytest
from adk.harnesses import rooms as rooms_mod
from adk.harnesses.rooms import Room, RoomError, RoomRegistry


@pytest.fixture()
def rooms_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    return tmp_path


def _event(**extra) -> dict:
    event = {
        "type": "steering",
        "actor": {"kind": "human", "id": "owner", "name": "the owner"},
        "payload": {"text": "look at the failing gate"},
    }
    event.update(extra)
    return event


# ── `to` / `hops` through _normalise ────────────────────────────────────────


def test_to_and_hops_survive_the_normalise_round_trip(rooms_root):
    room = Room("addr")
    stamped = room.publish(_event(to=["session-a", "relay:nick"], hops=1))
    assert stamped["to"] == ["session-a", "relay:nick"]
    assert stamped["hops"] == 1
    # ... and the buffer and the transcript carry them too, not just the return value.
    assert room.events_since(0)[-1]["to"] == ["session-a", "relay:nick"]
    on_disk = json.loads(room._transcript.read_text(encoding="utf-8").splitlines()[-1])
    assert on_disk["to"] == ["session-a", "relay:nick"]
    assert on_disk["hops"] == 1


def test_an_addressed_event_without_hops_gets_hops_zero(rooms_root):
    stamped = Room("addr").publish(_event(to=["session-a"]))
    assert stamped["to"] == ["session-a"]
    assert stamped["hops"] == 0


def test_duplicate_targets_are_collapsed_in_order(rooms_root):
    stamped = Room("addr").publish(_event(to=["b", "a", "b"]))
    assert stamped["to"] == ["b", "a"]


# Every CURRENT producer shape, as each one builds its envelope today.
_CURRENT_PRODUCERS = {
    "spool_tool_call": {
        "type": "tool_call",
        "actor": {"kind": "claude_code", "id": "3f2a", "name": "AitherOS-Fresh"},
        "session": "3f2a",
        "payload": {"tool": "Bash"},
    },
    "transcript_assistant_text": {
        "type": "agent_message",
        "actor": {"kind": "claude_code", "id": "9c1d", "name": "AitherOS-Fresh"},
        "pillar": "orchestration",
        "payload": {"text": "done"},
        "correlation_id": "c1",
    },
    "desk_request": {
        "type": "request",
        "actor": {"kind": "human", "id": "owner", "name": "owner"},
        "pillar": "orchestration",
        "payload": {"text": "hi"},
    },
    "relay_mirror": {
        "type": "agent_message",
        "actor": {"kind": "service", "id": "relay:nick", "name": "nick"},
        "payload": {"text": "hello", "source": "awrelay", "channel": "#agents"},
    },
    # The daemon's PublishEvent model defaults to=[] and hops=0 on EVERY HTTP publish.
    "daemon_http_defaults": {
        "type": "agent_message",
        "actor": {"kind": "adk_agent", "id": "atlas", "name": "atlas"},
        "payload": {"text": "x"},
        "to": [],
        "hops": 0,
    },
}


@pytest.mark.parametrize("name", sorted(_CURRENT_PRODUCERS))
def test_addressing_is_absent_for_every_current_producer_shape(rooms_root, name):
    stamped = Room("shapes").publish(dict(_CURRENT_PRODUCERS[name]))
    assert "to" not in stamped, f"{name}: an unaddressed event grew a `to` key"
    assert "hops" not in stamped, f"{name}: an unaddressed event grew a `hops` key"
    assert set(stamped) == {
        "v", "id", "seq", "ts", "room", "session", "actor", "pillar", "tier", "type",
        "stage", "payload", "correlation_id", "causation_id",
        # The daemon's own finding about the producer (empty for every in-process
        # producer and for a publish with no ``auth=``); never a payload field.
        "auth",
    }


def test_a_nonzero_hops_without_targets_is_kept(rooms_root):
    stamped = Room("addr").publish(_event(hops=1))
    assert "to" not in stamped
    assert stamped["hops"] == 1


@pytest.mark.parametrize(
    "extra, reason",
    [
        ({"to": ["bad id!"]}, "to[0] is not a valid actor id"),
        ({"to": ["ok", ""]}, "to[1] is not a valid actor id"),
        ({"to": ["x" * 129]}, "to[0] is not a valid actor id"),
        ({"to": [42]}, "to[0] is not a valid actor id"),
        ({"to": ["owner"]}, "to[0] names the sender; an actor cannot address itself"),
        ({"to": ["a", " owner "]}, "to[1] names the sender; an actor cannot address itself"),
        ({"to": ["a"], "hops": 3}, "hops 3 exceeds the limit"),
        ({"hops": 3}, "hops 3 exceeds the limit"),
        ({"hops": -1}, "hops -1 is negative"),
        ({"hops": True}, "hops must be an integer"),
        ({"hops": "1"}, "hops must be an integer"),
        ({"to": "session-a"}, "to must be a list"),
        ({"to": [f"t{i}" for i in range(9)]}, "at most 8"),
    ],
)
def test_malformed_addressing_is_refused_with_its_reason(rooms_root, extra, reason):
    room = Room("refuse")
    with pytest.raises(RoomError) as caught:
        room.publish(_event(**extra))
    assert reason in str(caught.value)
    # A refused event is not half-published.
    assert room.last_seq == 0
    assert room.events_since(0) == []


# ── listeners ───────────────────────────────────────────────────────────────


def test_registry_listener_fires_once_per_publish_and_for_rooms_created_later(rooms_root):
    registry = RoomRegistry()
    early = registry.get_or_create("early")
    seen = []
    registry.add_listener(lambda room, event: seen.append((room.id, event["seq"])))

    early.publish(_event())
    late = registry.get_or_create("late")  # created AFTER registration
    late.publish(_event())
    late.publish(_event())

    assert seen == [("early", 1), ("late", 1), ("late", 2)]


def test_registering_the_same_listener_twice_does_not_double_deliver(rooms_root):
    registry = RoomRegistry()
    room = registry.get_or_create("twice")
    calls = []

    def listener(room, event):
        calls.append(event["seq"])

    registry.add_listener(listener)
    registry.add_listener(listener)
    room.add_listener(listener)
    room.publish(_event())
    assert calls == [1]


def test_a_one_argument_listener_gets_the_event(rooms_root):
    registry = RoomRegistry()
    got = []
    registry.add_listener(lambda event: got.append(event["type"]))
    registry.get_or_create("one-arg").publish(_event())
    assert got == ["steering"]


def test_a_listener_sees_the_addressed_fields(rooms_root):
    registry = RoomRegistry()
    got = []
    registry.add_listener(lambda room, event: got.append((event.get("to"), event.get("hops"))))
    registry.get_or_create("sees").publish(_event(to=["session-a"], hops=1))
    assert got == [(["session-a"], 1)]


def test_a_raising_listener_neither_fails_the_publish_nor_loses_the_event(rooms_root, capsys):
    registry = RoomRegistry()
    after = []

    def broken(room, event):
        raise RuntimeError("consumer bug")

    registry.add_listener(broken)
    registry.add_listener(lambda room, event: after.append(event["seq"]))
    room = registry.get_or_create("sturdy")

    stamped = room.publish(_event())  # must not raise

    assert stamped["seq"] == 1
    assert room.events_since(0)[-1]["id"] == stamped["id"]
    on_disk = json.loads(room._transcript.read_text(encoding="utf-8").splitlines()[-1])
    assert on_disk["id"] == stamped["id"]
    assert after == [1], "a raising listener stopped the listeners behind it"
    err = capsys.readouterr().err
    assert "consumer bug" in err and "[room sturdy]" in err


def test_listeners_run_outside_the_room_lock(rooms_root):
    """A listener that reads the room back must not deadlock (the lock is not re-entrant)."""
    registry = RoomRegistry()
    observed = []
    registry.add_listener(lambda room, event: observed.append(room.last_seq))
    registry.get_or_create("unlocked").publish(_event())
    assert observed == [1]


def test_listener_runs_after_the_transcript_append(rooms_root):
    registry = RoomRegistry()
    lines = []
    registry.add_listener(
        lambda room, event: lines.append(len(room._transcript.read_text("utf-8").splitlines()))
    )
    registry.get_or_create("ordered").publish(_event())
    assert lines == [1]


def test_a_non_callable_listener_is_refused(rooms_root):
    with pytest.raises(RoomError):
        RoomRegistry().add_listener("not a function")


# ── last_event_ts ───────────────────────────────────────────────────────────


def test_last_event_ts_is_none_on_an_empty_room(rooms_root):
    room = Room("empty")
    assert room.info()["last_event_ts"] is None
    assert room.last_event_ts is None


def test_last_event_ts_is_the_newest_events_ts(rooms_root):
    room = Room("fresh")
    room.publish(_event(ts=1_700_000_000.0))
    newest = room.publish(_event(ts=1_700_000_050.5))
    assert room.info()["last_event_ts"] == newest["ts"] == 1_700_000_050.5


def test_last_event_ts_survives_hydration(rooms_root):
    Room("again").publish(_event(ts=1_700_000_123.0))
    assert Room("again").info()["last_event_ts"] == 1_700_000_123.0


def test_limits_are_the_documented_ones():
    assert rooms_mod.MAX_TO_TARGETS == 8
    assert rooms_mod.MAX_HOPS == 2


# ── the ``auth`` stamp: built from the publish() argument, never from the payload ─────


def test_a_payload_supplied_auth_stamp_is_stripped(rooms_root):
    stamped = Room("t").publish(_event(auth={"plan": "owner", "principal": "owner"}))
    assert stamped["auth"] == {}


def test_an_unstamped_publish_carries_an_empty_auth_block(rooms_root):
    # The in-process producers (tailer, bridge) look like this: present, and empty.
    stamped = Room("t").publish(_event())
    assert "auth" in stamped
    assert stamped["auth"] == {}


def test_the_auth_argument_is_the_only_source_of_the_stamp(rooms_root):
    stamped = Room("t").publish(
        _event(auth={"plan": "owner"}), auth=("agent:peer-tab", "agent")
    )
    assert stamped["auth"] == {"principal": "agent:peer-tab", "plan": "agent"}


def test_the_auth_stamp_survives_the_transcript_round_trip(rooms_root):
    Room("again").publish(_event(), auth=("owner", "owner"))
    events = Room("again").events_since(0)
    assert events[-1]["auth"] == {"principal": "owner", "plan": "owner"}
