"""Tests for the canonical chat-as-steering substrate (``adk.reasoning_session``)."""

from __future__ import annotations

import asyncio

import pytest

from adk.reasoning_session import (
    ReasoningSession,
    ReasoningSessionManager,
    SessionStatus,
    get_session_manager,
)


@pytest.mark.asyncio
async def test_session_runs_and_captures_result():
    sess = ReasoningSession("c1")
    assert not sess.is_active()

    async def run(s):
        return "done-value"

    sess.start(run)
    assert sess.is_active() or sess.status in (SessionStatus.DONE,)
    await sess.join(timeout=2)
    assert sess.status == SessionStatus.DONE
    assert sess.result == "done-value"
    assert not sess.is_active()


@pytest.mark.asyncio
async def test_steering_is_received_between_steps():
    """A message steered mid-flight is drained by the loop via pop_steering()."""
    sess = ReasoningSession("c2")
    seen: list[str] = []

    async def run(s):
        for _ in range(20):
            for msg in s.pop_steering():
                seen.append(msg)
            if "stop" in seen:
                return "stopped"
            await asyncio.sleep(0.01)
        return "ran-out"

    sess.start(run)
    await asyncio.sleep(0.02)
    assert sess.steer("redirect to topic X") is True
    await asyncio.sleep(0.03)
    assert sess.steer("stop") is True
    result = await sess.join(timeout=2)
    assert "redirect to topic X" in seen
    assert "stop" in seen
    assert result == "stopped"


@pytest.mark.asyncio
async def test_empty_steer_rejected():
    sess = ReasoningSession("c3")
    assert sess.steer("") is False
    assert sess.steer("   ") is False


@pytest.mark.asyncio
async def test_cancel_marks_cancelled():
    sess = ReasoningSession("c4")

    async def run(s):
        await asyncio.sleep(5)
        return "should-not-finish"

    sess.start(run)
    await asyncio.sleep(0.02)
    sess.cancel()
    await asyncio.sleep(0.02)
    assert sess.status == SessionStatus.CANCELLED
    assert not sess.is_active()


@pytest.mark.asyncio
async def test_manager_steer_only_when_active():
    mgr = ReasoningSessionManager()
    # No session yet → steer returns False (caller should start a fresh turn).
    assert mgr.steer("conv", "hi") is False

    sess = mgr.get_or_create("conv")
    started = asyncio.Event()

    async def run(s):
        started.set()
        for _ in range(50):
            if s.pop_steering():
                return "steered"
            await asyncio.sleep(0.01)
        return "idle"

    sess.start(run)
    await started.wait()
    assert mgr.active("conv") is True
    assert mgr.steer("conv", "go left") is True
    assert await sess.join(timeout=2) == "steered"
    # After completion, steer falls back to False.
    assert mgr.steer("conv", "again") is False


@pytest.mark.asyncio
async def test_manager_evicts_finished_over_cap():
    mgr = ReasoningSessionManager(max_sessions=2, ttl_s=0.0)

    async def run(s):
        return "x"

    for cid in ("a", "b", "c"):
        s = mgr.get_or_create(cid)
        s.start(run)
        await s.join(timeout=1)
    mgr.cleanup()
    # ttl_s=0 → all finished sessions are evicted on the next access.
    assert len(mgr._sessions) <= 2


@pytest.mark.asyncio
async def test_observe_streams_emitted_events():
    """An observer gets backfilled events + live events until the loop ends."""
    sess = ReasoningSession("obs1")

    async def run(s):
        for i in range(5):
            s.emit({"type": "token", "i": i})
            await asyncio.sleep(0.01)
        return "ok"

    sess.emit({"type": "start"})        # emitted BEFORE observer attaches → backfill
    sess.start(run)

    collected = []
    async for e in sess.observe(backfill=True, idle_timeout=0.1):
        collected.append(e)

    types = [e.get("type") for e in collected]
    assert "start" in types                              # backfilled
    token_ids = {e["i"] for e in collected if e.get("type") == "token"}
    assert token_ids == {0, 1, 2, 3, 4}                  # all 5 (dedup tolerates race)
    assert sess.status == SessionStatus.DONE


@pytest.mark.asyncio
async def test_externally_owned_loop_is_steerable():
    """A loop owned by an external driver (mark_running/mark_done) is steerable
    and reports active via the manager — this is the additive respond() path."""
    mgr = ReasoningSessionManager()
    sess = mgr.get_or_create("ext1")
    sess.mark_running()
    assert mgr.active("ext1") is True
    # external driver drains steering itself
    assert mgr.steer("ext1", "do X") is True
    assert sess.pop_steering() == ["do X"]
    sess.mark_done()
    assert mgr.active("ext1") is False
    assert mgr.steer("ext1", "too late") is False


def test_singleton_manager():
    assert get_session_manager() is get_session_manager()
