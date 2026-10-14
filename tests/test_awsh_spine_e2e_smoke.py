"""End-to-end smoke of the integrated awsh spine (WP-A..G) in one process.

Two turns in one session -- "what is in the news today" then "do it" -- through
``AitherAgent.stream_react`` with a fake streaming LLM, a fake tool, a real ``Crystal``
over an in-memory store and the real ``AutoNeuronFire`` with its web neurons stubbed
(no network). The turn must emit the context / neurons / crystal events, keep the
prompt inside the model window, carry the first turn into the second and answer.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from adk import turn_memory
from adk.agent import AitherAgent
from adk.context_budget import estimate_tokens
from adk.crystal import Crystal
from adk.llm import Message
from adk.llm.base import StreamChunk
from adk.memory import Memory
from adk.neurons import AutoNeuronFire, BaseNeuron, NeuronPool, NeuronResult

WINDOW = 8192


class _MemStore:
    """The FactStore protocol, in memory."""

    def __init__(self, rows):
        self.rows = list(rows)

    def put(self, key, value, meta):
        self.rows.append((key, value, dict(meta or {})))

    def scan(self, limit):
        return list(self.rows)[:limit]


class _StubNeuron(BaseNeuron):
    def __init__(self, name, content):
        self.name = name
        self._content = content

    async def fire(self, query, **kwargs):
        return NeuronResult(neuron=self.name, content=self._content, relevance=0.9,
                            latency_ms=1.0, source="stub")


def _neurons(agent):
    pool = NeuronPool(agent)
    for name in list(pool.neurons):
        pool.unregister(name)
    pool.register(_StubNeuron("web_search", "- Headline A: markets up\n- Headline B: rain"))
    pool.register(_StubNeuron("web", "- page: today's top story is the harvest festival"))
    pool.register(_StubNeuron("memory", "- the owner reads the news at breakfast"))
    pool.register(_StubNeuron("tools", "- web_search(query)"))
    return AutoNeuronFire(pool=pool, agent=agent)


def test_two_turn_session_emits_the_spine_events_and_answers(tmp_path, monkeypatch):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    async def _none(*a, **k):
        return ""

    monkeypatch.setattr(turn_memory, "skills_block", _none)
    monkeypatch.setattr(turn_memory, "graph_block", _none)

    seen: list = []
    turn = {"n": 0, "step": 0}

    async def _stream(messages, *a, **k):
        seen.append([Message(role=m.role, content=m.content) for m in messages])
        turn["step"] += 1
        if turn["n"] == 1:
            reply = ('<think>look it up</think>ACTION: web_search\n'
                     'INPUT: {"query": "news today"}') if turn["step"] == 1 else \
                "FINAL: Today: markets are up and it will rain."
        else:
            reply = "FINAL: Done -- saved the two headlines (markets up, rain)."
        yield StreamChunk(content=reply, done=True, model="fake-8k")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.model = "fake-8k"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[],
                                                finish_reason="stop"))
    agent = AitherAgent(name="smoke", llm=llm, builtin_tools=False,
                        system_prompt="IDENTITY",
                        memory=Memory(db_path=tmp_path / "mem.db", agent_name="smoke"))
    agent._context_limit_discovered = WINDOW
    agent._context_limit_observed = None
    agent.crystal = Crystal(scope="aitherium:smoke:test", store=_MemStore([
        ("f1", "the owner follows world news and weather", {}),
        ("f2", "news summaries should be two lines", {}),
    ]))
    agent._auto_neurons = _neurons(agent)
    searches: list = []

    @agent.tool
    def web_search(query: str) -> str:
        """search the web"""
        searches.append(query)
        return '{"results": [{"title": "Markets up"}, {"title": "Rain later"}]}'

    sid = f"smoke-{uuid.uuid4().hex}"

    async def _turn(text):
        turn["n"] += 1
        turn["step"] = 0
        events: list = []
        resp = await asyncio.wait_for(
            agent.stream_react(text, on_event=events.append, max_steps=6, session_id=sid),
            timeout=30)
        # after_turn runs in the background; let it land before the next turn.
        pending = list(getattr(agent, "_after_turn_tasks", ()) or ())
        if pending:
            await asyncio.wait(pending, timeout=10)
        return resp, events

    async def _run():
        r1 = await _turn("what is in the news today")
        n1 = len(seen)
        r2 = await _turn("do it")
        return r1, r2, n1

    (resp1, ev1), (resp2, ev2), n1 = asyncio.run(_run())

    for events in (ev1, ev2):
        types = [e["type"] for e in events]
        for t in ("turn_start", "crystal", "neurons", "context", "done"):
            assert t in types, (t, types)
        assert types.index("turn_start") < types.index("context")
        ctx = next(e for e in events if e["type"] == "context")
        assert ctx["chars_kept"] <= ctx["budget_chars"]
        assert not [e for e in events if e["type"] == "error"], events

    crystal_ev = next(e for e in ev1 if e["type"] == "crystal")
    assert crystal_ev["facts"] >= 1 and crystal_ev["chars"] > 0, crystal_ev
    neuron_ev = next(e for e in ev1 if e["type"] == "neurons")
    assert neuron_ev["fired"] and neuron_ev["chars"] > 0, neuron_ev

    # budget: every prompt the model saw fits the window
    for i, msgs in enumerate(seen):
        assert estimate_tokens(msgs) <= WINDOW, f"call {i + 1} over the window"

    # turn 1 used the tool and answered; turn 2 saw turn 1 and answered
    assert searches == ["news today"]
    assert resp1.content == "Today: markets are up and it will rain."
    assert resp2.content.startswith("Done")
    turn2_prompt = "\n".join(str(m.content) for m in seen[n1])
    assert "what is in the news today" in turn2_prompt
    assert "markets are up" in turn2_prompt
    assert getattr(agent, "_last_turn_finish", None) == "answer"


def test_a_step_limited_turn_marks_its_finish_for_after_turn(tmp_path, monkeypatch):
    """WP-E's step-limit lesson keys on agent._last_turn_finish; the loop sets it."""
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)

    async def _stream(messages, *a, **k):
        last = str(messages[-1].content)
        reply = ("FINAL: partial" if "Answer now from the observations" in last
                 else 'ACTION: probe\nINPUT: {"n": "%d"}' % len(messages))
        yield StreamChunk(content=reply, done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.model = "fake-8k"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[],
                                                finish_reason="stop"))
    agent = AitherAgent(name="smoke2", llm=llm, builtin_tools=False,
                        memory=Memory(db_path=tmp_path / "m.db", agent_name="smoke2"))
    agent._auto_neurons = None

    @agent.tool
    def probe(n: str) -> str:
        """probe"""
        return f"ok {n}"

    resp = asyncio.run(agent.stream_react("probe things", on_event=lambda e: None,
                                          max_steps=2))
    assert resp.content == "partial"
    assert agent._last_turn_finish == "step_limit"
