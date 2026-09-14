"""Regression guards for ACP streamed-update aggregation.

Two defects found by adversarial review of the first ACP build:
  1. a fixed ``asyncio.sleep(0.1)`` after the prompt response silently DROPPED
     tool-call completions that arrived later than 100ms;
  2. ``_parse_prompt_result`` tracked a single "active" tool call, so interleaved
     tool calls overwrote each other and an uncompleted call vanished entirely.
Both are fixed by a bounded+loud drain and id-keyed aggregation.
"""
import asyncio

from adk.acp import ACPClient, ACPToolCall


def _client() -> ACPClient:
    # No transport needed: these tests exercise pure aggregation/drain logic.
    return ACPClient(command="python", args=["-c", "pass"])


def _start(tid: str, name: str = "read_file") -> dict:
    return {
        "session_update": "tool_call_start",
        "tool_call_id": tid,
        "tool_name": name,
        "function_args": {"path": "/tmp/x"},
    }


def _complete(tid: str, result: str = "ok") -> dict:
    return {"session_update": "tool_call_complete", "tool_call_id": tid, "result": result}


def _idle() -> dict:
    """A terminal ``state_update: idle`` — a v2 turn is only complete at idle."""
    return {"session_update": "state_update", "state": "idle", "stop_reason": "end_turn"}


# --- id-keyed aggregation ----------------------------------------------------

def test_interleaved_tool_calls_both_survive():
    """start A, start B, complete A, complete B — the old single-slot parser lost A."""
    updates = [_start("a"), _start("b", "write_file"), _complete("a", "ra"), _complete("b", "rb")]
    res = _client()._parse_prompt_result({}, updates)
    by_id = {c.tool_call_id: c for c in res.tool_calls}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"].result == "ra"
    assert by_id["b"].result == "rb"


def test_incomplete_tool_call_is_reported_not_dropped():
    """A started-but-never-completed call must appear with result=None, not vanish."""
    updates = [_start("a"), _complete("a"), _start("orphan", "shell")]
    res = _client()._parse_prompt_result({}, updates)
    by_id = {c.tool_call_id: c for c in res.tool_calls}
    assert set(by_id) == {"a", "orphan"}
    assert by_id["orphan"].result is None


def test_completion_without_start_is_recorded():
    res = _client()._parse_prompt_result({}, [_complete("ghost", "r")])
    assert [c.tool_call_id for c in res.tool_calls] == ["ghost"]
    assert res.tool_calls[0].result == "r"


def test_text_and_usage_still_aggregate():
    updates = [
        {"session_update": "agent_message_chunk", "content": {"type": "text", "text": "he"}},
        {"session_update": "agent_message_chunk", "content": {"type": "text", "text": "llo"}},
        {"session_update": "usage_update", "input_tokens": 7, "output_tokens": 3},
    ]
    res = _client()._parse_prompt_result({"stop_reason": "end_turn"}, updates)
    assert res.text == "hello"
    assert res.usage.input_tokens == 7 and res.usage.output_tokens == 3
    assert res.stop_reason == "end_turn"
    assert res.tool_calls == []


# --- bounded drain -----------------------------------------------------------

def test_outstanding_tool_calls_detection():
    c = _client()
    assert c._outstanding_tool_calls([_start("a")]) == {"a"}
    assert c._outstanding_tool_calls([_start("a"), _complete("a")]) == set()


def test_drain_waits_for_late_completion():
    """The old fixed 0.1s sleep dropped this; the drain must wait for it."""
    c = _client()
    updates: list[dict] = [_start("late")]

    async def scenario():
        async def complete_later():
            await asyncio.sleep(0.25)  # later than the old 0.1s window
            updates.append(_complete("late", "arrived"))
            updates.append(_idle())

        task = asyncio.create_task(complete_later())
        await c._drain_updates(updates, timeout=2.0)
        await task
        return c._parse_prompt_result({}, updates)

    res = asyncio.run(scenario())
    assert [x.tool_call_id for x in res.tool_calls] == ["late"]
    assert res.tool_calls[0].result == "arrived"


def test_drain_waits_for_idle_to_collect_text():
    """Text arrives as chunks BEFORE idle, all AFTER the immediate prompt
    response — the drain must NOT return early on 'no outstanding tool calls',
    or a text-only turn comes back empty (measured live: PONG was dropped)."""
    c = _client()
    updates: list[dict] = []

    async def scenario():
        async def emit_text_later():
            await asyncio.sleep(0.25)  # later than any settle window
            updates.append(
                {"session_update": "agent_message_chunk",
                 "content": {"type": "text", "text": "PONG"}}
            )
            updates.append(_idle())

        task = asyncio.create_task(emit_text_later())
        await c._drain_updates(updates, timeout=5.0)
        await task
        return c._parse_prompt_result({}, updates)

    res = asyncio.run(scenario())
    assert res.text == "PONG"


def test_drain_is_bounded_and_warns(caplog):
    """If a completion never arrives, drain must return by the deadline AND log."""
    c = _client()
    updates = [_start("never")]

    async def scenario():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await c._drain_updates(updates, timeout=0.3)
        return loop.time() - t0

    with caplog.at_level("WARNING"):
        elapsed = asyncio.run(scenario())
    assert elapsed < 2.0, "drain must be bounded by its timeout"
    assert any("drain_timeout" in r.message or "drain_timeout" in r.getMessage()
               for r in caplog.records), "late/missing completion must be logged, not silent"


def test_drain_returns_fast_when_turn_complete():
    """A completed v2 turn (all tool calls done AND idle seen) returns fast."""
    c = _client()

    async def scenario():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await c._drain_updates(
            [_start("a"), _complete("a"), _idle()], timeout=5.0
        )
        return loop.time() - t0

    assert asyncio.run(scenario()) < 1.0


def test_acp_tool_call_model_allows_none_result():
    assert ACPToolCall(tool_call_id="x", tool_name="t", arguments={}).result is None
