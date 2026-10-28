"""Independent read-only tool calls in one assistant turn run concurrently."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from adk.agent import AitherAgent
from adk.llm.base import LLMResponse, ToolCall
from adk.memory import Memory
from adk.tools import ToolRegistry

DELAY = 0.4


def _agent(tmp_path, calls, tools):
    llm = MagicMock()
    llm.provider_name = "mock"
    llm.chat = AsyncMock(side_effect=[
        LLMResponse(content="", model="mock", tool_calls=calls),
        LLMResponse(content="done", model="mock"),
    ])
    mem = Memory(db_path=tmp_path / "t.db", agent_name="t")
    return AitherAgent("t", llm=llm, tools=[tools], memory=mem), llm


def _registry(order, spans=None):
    spans = {} if spans is None else spans
    reg = ToolRegistry()

    async def read_file(path: str) -> str:
        order.append(("start", path))
        spans.setdefault(path, [time.perf_counter()])
        await asyncio.sleep(DELAY)
        order.append(("end", path))
        spans[path].append(time.perf_counter())
        return f"content of {path}"

    async def write_file(path: str) -> str:
        order.append(("write", path))
        await asyncio.sleep(DELAY)
        return "ok"

    reg.register(read_file, name="read_file")
    reg.register(write_file, name="write_file")
    return reg


def _tool_msgs(llm):
    msgs = llm.chat.call_args_list[1][0][0]
    return [m for m in msgs if m.role == "tool"]


def test_parallel_safe_classifier():
    reg = _registry([])
    reg.register(lambda q: q, name="search")  # sync: never parallel
    assert reg.parallel_safe("read_file")
    assert not reg.parallel_safe("write_file")
    assert not reg.parallel_safe("search")
    assert not reg.parallel_safe("missing")


@pytest.mark.asyncio
async def test_reads_run_concurrently_results_stay_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PARALLEL_TOOLS", raising=False)
    order: list = []
    calls = [ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"f{i}"})
             for i in range(4)]
    spans: dict = {}
    agent, llm = _agent(tmp_path, calls, _registry(order, spans))
    resp = await agent.chat("read four files")
    # time the tools themselves, not chat()'s memory/context overhead
    wall = max(e for _, e in spans.values()) - min(s for s, _ in spans.values())
    assert resp.content == "done"
    # all four started before any finished
    assert [k for k, _ in order[:4]] == ["start"] * 4
    assert wall < DELAY * 4 * 0.75, wall
    tool_msgs = _tool_msgs(llm)
    assert [m.tool_call_id for m in tool_msgs] == ["c0", "c1", "c2", "c3"]
    assert tool_msgs[2].content == "content of f2"


@pytest.mark.asyncio
async def test_write_is_a_barrier(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PARALLEL_TOOLS", raising=False)
    order: list = []
    calls = [
        ToolCall(id="r0", name="read_file", arguments={"path": "a"}),
        ToolCall(id="w", name="write_file", arguments={"path": "a"}),
        ToolCall(id="r1", name="read_file", arguments={"path": "b"}),
    ]
    agent, llm = _agent(tmp_path, calls, _registry(order))
    await agent.chat("read, write, read")
    assert order == [("start", "a"), ("end", "a"), ("write", "a"),
                     ("start", "b"), ("end", "b")]
    assert [m.tool_call_id for m in _tool_msgs(llm)] == ["r0", "w", "r1"]


@pytest.mark.asyncio
async def test_env_off_restores_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_PARALLEL_TOOLS", "0")
    order: list = []
    calls = [ToolCall(id=f"c{i}", name="read_file", arguments={"path": f"f{i}"})
             for i in range(2)]
    agent, _ = _agent(tmp_path, calls, _registry(order))
    await agent.chat("read two")
    assert order == [("start", "f0"), ("end", "f0"), ("start", "f1"), ("end", "f1")]


# ── stream_react: several ACTION/INPUT pairs in one reply ──

def test_extra_react_actions_parses_pairs_after_the_first():
    from adk.agent import extra_react_actions
    text = ('ACTION: read_file\nINPUT: {"path": "a"}\n'
            'ACTION: read_file\nINPUT: {"path": "b"}\n'
            'ACTION: list_dir\nINPUT: not json\n')
    assert extra_react_actions(text) == [("read_file", {"path": "b"})]


async def _stream_react_run(tmp_path, reply):
    from adk.llm.base import StreamChunk

    order: list = []
    spans: dict = {}
    calls = {"n": 0}

    async def _stream(messages, *a, **k):
        calls["n"] += 1
        text = reply if calls["n"] == 1 else "<think>k</think>FINAL: done"
        yield StreamChunk(content=text, done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=LLMResponse(content="x", model="m"))
    mem = Memory(db_path=tmp_path / "s.db", agent_name="s")
    agent = AitherAgent("s", llm=llm, memory=mem, builtin_tools=False)
    reg = _registry(order, spans)
    for td in reg.list_tools():
        agent._tools._tools[td.name] = td
    events: list = []
    resp = await agent.stream_react("read a and b", on_event=events.append)
    return resp, order, spans, events


@pytest.mark.asyncio
async def test_stream_react_runs_listed_reads_concurrently(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PARALLEL_TOOLS", raising=False)
    reply = ('<think>both</think>ACTION: read_file\nINPUT: {"path": "a"}\n'
             'ACTION: read_file\nINPUT: {"path": "b"}\n')
    resp, order, spans, events = await _stream_react_run(tmp_path, reply)
    assert resp.content == "done"
    assert [k for k, _ in order[:2]] == ["start", "start"]
    wall = max(e for _, e in spans.values()) - min(s for s, _ in spans.values())
    assert wall < DELAY * 2 * 0.75, wall
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "tool_result"]
    assert any("content of a" in r["result"] and "content of b" in r["result"]
               for r in results) or len(results) >= 2


@pytest.mark.asyncio
async def test_stream_react_write_among_actions_stays_single(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PARALLEL_TOOLS", raising=False)
    reply = ('ACTION: read_file\nINPUT: {"path": "a"}\n'
             'ACTION: write_file\nINPUT: {"path": "a"}\n')
    _, order, _, _ = await _stream_react_run(tmp_path, reply)
    assert ("write", "a") not in order  # the write is not run as an extra
