"""character_recall hook -- one spoken room event in, one persona's memory out.

WHY THESE CASES
----------------
The hook is registered on the room registry, so it runs on the publish path of EVERY event
every Claude tab emits. Most arms here are therefore refusals: telemetry is not speech, a
terminal session is not a character, and an unaddressed line is not something every persona
heard. The two positive arms (a party member's own line, an addressed line) exist so a
passing suite cannot mean "everything is skipped" -- a hook that indexes nothing and a hook
that never ran look identical from outside.

A fake store stands in for ``lib.avatars.character_recall``: the real one is in the
monorepo, and a published awdk has no ``lib/``. That is exactly the shape the hook must
tolerate, so one arm asserts the no-lib path is a NAMED no-op rather than a crash.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from adk.harnesses import character_recall as cr_mod
from adk.harnesses.character_recall import (
    CharacterRecallHook,
    PersonaResolver,
    extract_utterance,
    register,
    sanitize_actor_id,
)
from adk.harnesses.rooms import RoomRegistry


class FakeStore:
    """Records what the hook asked it to remember. ``root``/``model_tag`` so status() is
    the same shape as with the real store."""

    root = "/data/avatars/recall"
    model_tag = "fake"

    def __init__(self, fail: bool = False, embedded: bool = True) -> None:
        self.calls = []
        self.fail = fail
        self.embedded = embedded

    def remember(self, persona_id, text, role, **kw):
        if self.fail:
            raise RuntimeError("store is down for the test")
        self.calls.append({"persona_id": persona_id, "text": text, "role": role, **kw})
        return type("R", (), {"embedded": self.embedded, "error": None if self.embedded
                              else "embedder down", "error_kind": None if self.embedded
                              else "embedder-unreachable"})()

    def by_persona(self, persona_id):
        return [c for c in self.calls if c["persona_id"] == persona_id]


PARTY = {
    "version": 1,
    "exported_at": "2026-09-20T08:00:00.000Z",
    "source": "awdesk",
    "members": [
        {"persona_id": "aria", "display_name": "Aria", "character": "aria",
         "vrm": "aria/model.vrm", "animations": [], "voice": {"voice": "nova", "speed": 1.0},
         "presence": "normal", "rating": "g", "saga": None, "sprite": None,
         "origin_key": "relay:aria"},
        {"persona_id": "bram", "display_name": "Bram", "character": "bram",
         "vrm": "bram/model.vrm", "animations": [], "voice": {"voice": "onyx", "speed": 1.0},
         "presence": "quiet", "rating": "g", "saga": None, "sprite": None,
         "origin_key": "relay:bram"},
    ],
}


@pytest.fixture()
def party_file(tmp_path):
    path = tmp_path / "party.json"
    path.write_text(json.dumps(PARTY), encoding="utf-8")
    return path


def stub_load_party(path):
    """The monorepo reader's shape, in ten lines: the hook takes `load_party` injected
    precisely so a published awdk (no `lib/`) and a test need nothing from the monorepo.
    The real reader is asserted by its own pytest suite and by the party-manifest gate."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"party manifest: version: expected 1, got {data.get('version')!r}")
    member = type("M", (), {})
    members = []
    for row in data.get("members") or []:
        m = member()
        m.persona_id = row["persona_id"]
        m.origin_key = row.get("origin_key")
        members.append(m)
    return type("P", (), {"members": tuple(members)})()


@pytest.fixture()
def hook(tmp_path, party_file, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    store = FakeStore()
    h = CharacterRecallHook(store=store, party_file=party_file, worker=False,
                            load_party=stub_load_party)
    h.flush()  # bootstraps the resolver (and the party read) without a worker thread
    h.store = store  # the test's handle on what the hook remembered
    return h


def _event(**extra) -> dict:
    event = {"type": "agent_message", "actor": {"kind": "adk_agent", "id": "relay:aria",
                                               "name": "Aria"},
             "payload": {"text": "the harbour lights are lit"}}
    event.update(extra)
    return event


# ─── what counts as speech ────────────────────────────────────────────────────


def test_extract_utterance_accepts_speech_and_refuses_telemetry():
    assert extract_utterance(_event()) == "the harbour lights are lit"
    assert extract_utterance({"type": "message", "payload": {"content": "hi  there"}}) \
        == "hi there"
    assert extract_utterance({"type": "command_reply", "payload": {"reply": "done"}}) == "done"
    for bad in (
        {"type": "tool_call", "payload": {"text": "Read(x)"}},
        {"type": "thinking", "payload": {"text": "hmm"}},
        {"type": "classify", "payload": {"prompt": "do the thing"}},
        {"type": "agent_message", "payload": {"text": "   "}},
        {"type": "agent_message", "payload": {}},
        {"type": "agent_message"},
        {"payload": {"text": "no type"}},
        "not an event",
    ):
        assert extract_utterance(bad) is None


def test_text_is_clipped():
    long = {"type": "message", "payload": {"text": "x" * (cr_mod.MAX_TEXT + 500)}}
    assert len(extract_utterance(long)) == cr_mod.MAX_TEXT


# ─── whose memory it lands in ─────────────────────────────────────────────────


def test_party_members_own_line_is_indexed_as_self(hook):
    hook.handle(None, _event())
    hook.flush()
    calls = hook.store.by_persona("aria")
    assert len(calls) == 1
    assert calls[0]["role"] == "self"
    assert calls[0]["text"] == "the harbour lights are lit"
    assert calls[0]["speaker"] == "Aria"
    assert not hook.store.by_persona("bram")


def test_addressed_line_is_indexed_as_heard_for_the_target(hook):
    hook.handle(None, _event(to=["relay:bram"]))
    hook.flush()
    assert [c["role"] for c in hook.store.by_persona("aria")] == ["self"]
    assert [c["role"] for c in hook.store.by_persona("bram")] == ["heard"]


def test_an_unaddressed_line_reaches_only_its_speaker(hook):
    hook.handle(None, _event())
    hook.flush()
    assert {c["persona_id"] for c in hook.store.calls} == {"aria"}


def test_a_terminal_session_is_not_given_a_memory(hook):
    """The transcript bridge emits every Claude tab's assistant text as `message`. One
    memory directory per session id is an index of the owner's terminals, not recall."""
    hook.handle(None, {"type": "message",
                       "actor": {"kind": "claude_code", "id": "sess-abc123", "name": "tab"},
                       "payload": {"text": "I will read the file"}})
    hook.handle(None, {"type": "message",
                       "actor": {"kind": "human", "id": "owner", "name": "David"},
                       "payload": {"text": "do the thing"}})
    hook.flush()
    assert hook.store.calls == []
    assert hook.status()["skipped"] == 2


def test_a_human_addressing_a_party_member_lands_as_heard(hook):
    hook.handle(None, {"type": "message", "actor": {"kind": "human", "id": "owner",
                                                    "name": "David"},
                       "payload": {"text": "aria, light the lamps"}, "to": ["relay:aria"]})
    hook.flush()
    calls = hook.store.by_persona("aria")
    assert [c["role"] for c in calls] == ["heard"]
    assert calls[0]["speaker"] == "David"


def test_an_unknown_agent_falls_back_to_a_persona_shaped_actor_id(hook):
    hook.handle(None, _event(actor={"kind": "adk_agent", "id": "relay:#agents:lyra",
                                    "name": "lyra"}))
    hook.flush()
    assert [c["persona_id"] for c in hook.store.calls] == ["relay_agents_lyra"]


def test_one_persona_is_not_indexed_twice_for_one_event(hook):
    """`to` is deduped by the room, but a listener registered elsewhere is called with
    whatever the producer sent -- so the hook dedupes per persona itself."""
    hook.handle(None, _event(to=["relay:bram", "relay:bram"]))
    hook.flush()
    assert len(hook.store.by_persona("bram")) == 1


# ─── the publish path ─────────────────────────────────────────────────────────


def test_registered_on_a_room_registry_it_indexes_real_publishes(tmp_path, party_file,
                                                                 monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    registry = RoomRegistry()
    store = FakeStore()
    hook = register(registry, store=store, party_file=party_file, worker=False,
                    load_party=stub_load_party)
    hook.flush()
    room = registry.get_or_create("main")
    room.publish(_event())
    room.publish({"type": "tool_call", "actor": {"kind": "adk_agent", "id": "relay:aria"},
                  "payload": {"tool": "Read"}})
    hook.flush()
    assert [c["role"] for c in store.by_persona("aria")] == ["self"]
    assert store.calls[0]["room"] == "main"
    assert store.calls[0]["event_id"]
    status = hook.status()
    assert status["utterances"] == 1 and status["indexed"] == 1
    assert status["seen"] == 2


def test_a_listener_failure_never_costs_the_event(tmp_path, party_file, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    registry = RoomRegistry()
    hook = register(registry, store=FakeStore(fail=True), party_file=party_file,
                    worker=False, load_party=stub_load_party)
    hook.flush()
    room = registry.get_or_create("main")
    stored = room.publish(_event())
    hook.flush()
    assert stored["seq"] == 1                       # the event landed
    assert room.last_seq == 1
    assert hook.status()["failed"] == 1
    assert "store is down" in hook.status()["last_error"]


def test_handle_does_not_block_on_the_embedder(tmp_path, party_file, monkeypatch):
    """handle() must not call the store at all -- with no worker and no flush, nothing is
    remembered yet, but the event is queued."""
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    store = FakeStore()
    hook = CharacterRecallHook(store=store, party_file=party_file, worker=False,
                               load_party=stub_load_party)
    hook.flush()
    hook.handle(None, _event())
    assert store.calls == []
    assert hook.status()["queue_depth"] == 1
    hook.flush()
    assert len(store.calls) == 1


def test_a_full_queue_drops_the_oldest_and_says_so(tmp_path, party_file, monkeypatch):
    """Blocking a publish would wedge the room, so a saturated queue sheds the OLDEST and
    counts it -- the newest lines are the ones a character is about to need."""
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    store = FakeStore()
    hook = CharacterRecallHook(store=store, party_file=party_file, worker=False, queue_max=2,
                               load_party=stub_load_party)
    hook.flush()
    for i in range(5):
        hook.handle(None, _event(payload={"text": f"line {i}"}))
    status = hook.status()
    assert status["queue_depth"] == 2
    assert status["dropped"] == 3
    hook.flush()
    assert [c["text"] for c in store.calls] == ["line 3", "line 4"]


def test_unembedded_rows_are_counted(tmp_path, party_file, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    store = FakeStore(embedded=False)
    hook = CharacterRecallHook(store=store, party_file=party_file, worker=False,
                               load_party=stub_load_party)
    hook.flush()
    hook.handle(None, _event())
    hook.flush()
    status = hook.status()
    assert status["indexed"] == 1 and status["unembedded"] == 1
    assert status["last_error"].startswith("embedder-unreachable")


# ─── off switches and the no-lib path ─────────────────────────────────────────


def test_env_off_switch_makes_it_a_no_op(tmp_path, party_file, monkeypatch):
    monkeypatch.setenv("AITHER_CHARACTER_RECALL", "0")
    store = FakeStore()
    hook = CharacterRecallHook(store=store, party_file=party_file, worker=False,
                               load_party=stub_load_party)
    hook.handle(None, _event())
    hook.flush()
    assert store.calls == []
    assert hook.available is False
    assert hook.status()["reason"] == "AITHER_CHARACTER_RECALL is off"


def test_no_lib_is_a_named_no_op_not_a_crash(tmp_path, party_file, monkeypatch):
    monkeypatch.setattr(cr_mod, "_import_lib", lambda: (None, None, "no lib here"))
    lines = []
    hook = CharacterRecallHook(party_file=party_file, worker=False,
                               log=lines.append)
    hook.flush()
    hook.handle(None, _event())
    hook.flush()
    assert hook.available is False
    assert "not importable" in hook.status()["reason"]
    assert any("character-recall" in line for line in lines)


def test_status_before_boot_says_booting(tmp_path, party_file, monkeypatch):
    monkeypatch.setattr(cr_mod, "_import_lib", lambda: (None, None, "slow"))
    hook = CharacterRecallHook(party_file=party_file, worker=False)
    assert hook.available is None
    assert "booting" in hook.status()["reason"]
    hook.handle(None, _event())            # queued, not dropped
    assert hook.status()["queue_depth"] == 1


# ─── the persona resolver ─────────────────────────────────────────────────────


def test_resolver_maps_origin_keys_and_reloads_on_change(party_file):
    ticks = {"t": 0.0}
    resolver = PersonaResolver(party_file, stub_load_party, now=lambda: ticks["t"])
    assert resolver.resolve("relay:aria") == ("aria", True)
    assert resolver.resolve("aria") == ("aria", True)
    assert resolver.resolve("relay:nobody") == ("relay_nobody", False)

    party = json.loads(json.dumps(PARTY))
    party["members"][0]["origin_key"] = "service:awdesk"
    party_file.write_text(json.dumps(party), encoding="utf-8")
    os.utime(party_file, (1, 1))  # a coarse mtime clock must not hide the rewrite
    ticks["t"] += cr_mod.PARTY_RECHECK_SECONDS + 1
    assert resolver.resolve("service:awdesk") == ("aria", True)
    assert resolver.status()["party_members"] == 2


def test_resolver_does_not_re_read_within_the_recheck_window(party_file):
    reads = {"n": 0}

    def counting(path):
        reads["n"] += 1
        return stub_load_party(path)

    ticks = {"t": 0.0}
    resolver = PersonaResolver(party_file, counting, now=lambda: ticks["t"])
    for _ in range(10):
        resolver.resolve("relay:aria")
    assert reads["n"] == 1
    ticks["t"] += cr_mod.PARTY_RECHECK_SECONDS + 1
    resolver.resolve("relay:aria")
    assert reads["n"] == 1        # mtime unchanged: a stat, not a parse


def test_resolver_reports_a_broken_manifest(tmp_path):
    bad = tmp_path / "party.json"
    bad.write_text('{"version": 2, "members": []}', encoding="utf-8")
    resolver = PersonaResolver(bad, stub_load_party)
    assert resolver.resolve("relay:aria") == ("relay_aria", False)
    assert "version" in (resolver.status()["party_error"] or "")


def test_resolver_with_no_manifest_still_resolves(tmp_path):
    resolver = PersonaResolver(tmp_path / "absent.json", stub_load_party)
    assert resolver.resolve("relay:aria") == ("relay_aria", False)
    assert resolver.status()["party_loaded"] is False
    assert resolver.status()["party_error"] is None


def test_resolver_with_no_reader_at_all_still_resolves(party_file):
    """A pip-installed awdk has no party reader; every actor still gets a persona."""
    resolver = PersonaResolver(party_file, None)
    assert resolver.resolve("relay:aria") == ("relay_aria", False)
    assert resolver.status()["party_loaded"] is False


def test_sanitize_actor_id_is_persona_shaped():
    assert sanitize_actor_id("relay:#agents:lyra") == "relay_agents_lyra"
    assert sanitize_actor_id("service:awdesk") == "service_awdesk"
    assert sanitize_actor_id("../../etc") == "etc"
    assert sanitize_actor_id("###") is None
    assert sanitize_actor_id("") is None
    assert sanitize_actor_id(None) is None
    assert len(sanitize_actor_id("a" * 500)) == cr_mod.PERSONA_ID_MAX
