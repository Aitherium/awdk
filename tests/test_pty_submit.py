"""submit() -- the Enter key -- and the tier-1 receipt that stops lying (2026-09-19).

Measured that day: the steer dispatcher's managed-pty tier called ``send(text)`` and
reported ``landed_now=True`` / "the agent has it now". On a pty that is a line sitting
unsubmitted in the input box. These arms pin: a pty ``submit`` types one line and
presses Enter, ``send`` stays raw, the base class's ``submit`` is ``send``, and tier 1
needs BOTH the daemon's own owner-plan ``auth`` stamp AND (a HUMAN actor OR the target's
opt-in) -- a peer agent into a tab that did not opt in is queued with a receipt that
says so (owner ruling 2026-09-19, option b).
"""

from __future__ import annotations

import pytest
from adk.harnesses.pty_session import PtyHarnessSession
from adk.harnesses.registry import SPECS
from adk.harnesses.rooms import RoomRegistry
from adk.harnesses.session import HarnessSession, SessionConfig
from adk.harnesses.steer_dispatch import TIER1_ACTOR_KINDS, register


class _FakePty:
    def __init__(self) -> None:
        self.written: list[str] = []

    def isalive(self) -> bool:
        return True

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)


def _pty_session(tmp_path) -> tuple[PtyHarnessSession, _FakePty]:
    s = PtyHarnessSession(SPECS["terminal"], SessionConfig(harness="terminal"), root=tmp_path)
    fake = _FakePty()
    s._pty = fake  # the seam start() would have filled; no process is spawned here
    return s, fake


def test_pty_submit_types_one_line_and_presses_enter(tmp_path):
    s, pty = _pty_session(tmp_path)
    assert s.submit("hello there") is True
    # Two writes, not one: a single burst ending in CR is read as a paste by Claude
    # Code's input box and never submits (measured 2026-09-19 with an 83-char prompt).
    assert pty.written == ["hello there", "\r"]


def test_pty_submit_collapses_newline_runs_to_one_line(tmp_path):
    s, pty = _pty_session(tmp_path)
    assert s.submit("a\nb\r\n\n  c  \n") is True
    assert pty.written == ["a b c", "\r"]


def test_pty_submit_refuses_whitespace_rather_than_sending_a_bare_enter(tmp_path):
    s, pty = _pty_session(tmp_path)
    assert s.submit("   \n\n") is False
    assert pty.written == []


def test_pty_send_stays_raw(tmp_path):
    s, pty = _pty_session(tmp_path)
    assert s.send("\x03") is True
    assert s.send("y") is True
    assert pty.written == ["\x03", "y"]


def test_base_submit_is_send(tmp_path):
    s = HarnessSession(SPECS["claude"], SessionConfig(harness="claude"), root=tmp_path)
    calls: list[str] = []
    s.send = lambda text: calls.append(text) or True  # type: ignore[method-assign]
    assert s.submit("one turn") is True
    assert calls == ["one turn"]


# ── the dispatcher: who reaches the keyboard ──────────────────────────────────────


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "status.json"))
    return tmp_path


def _event(actor: dict, **extra) -> dict:
    event = {"type": "steering", "actor": actor, "payload": {"text": "look at the gate"}}
    event.update(extra)
    return event


def _receipts(room) -> list[dict]:
    return [e for e in room.events_since(0) if e["type"] == "steering_receipt"]


def test_tier1_is_for_humans_only():
    assert TIER1_ACTOR_KINDS == frozenset({"human"})


def test_a_human_steer_reaches_the_managed_pty_now(env):
    registry = RoomRegistry()
    sent: list[tuple[str, str]] = []
    register(
        registry,
        list_unified_sessions=lambda: [{"id": "session-a", "title": "t"}],
        send_managed_input=lambda sid, text: sent.append((sid, text)) or True,
    )
    room = registry.get_or_create("pty-human")
    # ``auth=`` is the daemon's finding about the bearer, not a payload field; a human
    # claim with no stamp is an unvouched producer and takes the mailbox instead.
    room.publish(_event({"kind": "human", "id": "owner", "name": "the owner"},
                        to=["session-a"], id="e-h"), auth=("owner", "owner"))
    assert sent == [("session-a", "look at the gate")]
    receipt = _receipts(room)[0]["payload"]
    assert receipt["channel"] == "pty" and receipt["landed_now"] is True


def test_a_peer_agent_is_never_typed_into_a_live_session(env):
    registry = RoomRegistry()
    sent: list[tuple[str, str]] = []
    register(
        registry,
        list_unified_sessions=lambda: [{"id": "session-a", "title": "t"}],
        send_managed_input=lambda sid, text: sent.append((sid, text)) or True,
    )
    room = registry.get_or_create("pty-peer")
    # Even vouched-for as owner-plan by the daemon: the target did not opt in
    # (no ``tier1_opt_in`` resolver wired = never), so a peer kind stays queued.
    room.publish(_event({"kind": "adk_agent", "id": "atlas", "name": "Atlas"},
                        to=["session-a"], id="e-p"), auth=("owner", "owner"))
    assert sent == [], "a peer's words must not reach the keyboard"
    receipt = _receipts(room)[0]["payload"]
    assert receipt["channel"] == "mailbox"
    assert receipt["landed_now"] is False and receipt["queued"] is True
    assert "opted in at spawn" in receipt["detail"]
    box = env / "steer" / "session-a"
    assert len(list(box.glob("*.md"))) == 1
