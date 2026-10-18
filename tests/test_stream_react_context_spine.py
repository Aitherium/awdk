"""The streaming turn's spine: budgeted context, compaction, guard, steering, synthesis.

Measured on the awsh path before this: a 14-step turn on an 8k-window model appended
every <think> and a head-only 3000-char observation per step and never compacted, so
the prompt outgrew the window near step 9; six context blocks each kept their own cap;
an identical call re-ran and re-appended its result; a /chat/steer hint was never read;
and a step-limited turn ended with "(reached step limit)" and no answer.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from adk import turn_memory
from adk.agent import AitherAgent
from adk.context_budget import estimate_tokens
from adk.llm import Message
from adk.llm.base import StreamChunk
from adk.steering import queue_steering_message

WINDOW = 8192
SYNTH_MARK = "Answer now from the observations above"


class _FakeCrystal:
    def __init__(self):
        self.telemetry = {"degraded": []}
        self.last_recall = {}

    async def recall_block(self, message, exclude=(), max_chars=None):
        self.last_recall = {"facts": 40, "memory": 0, "graph_hits": 0, "chars": 2000}
        return "[FACTS] Known facts.\n" + "\n".join(
            f"- crystal fact number {i} about big reads" for i in range(40))

    async def crystallize(self, text):
        return None


class _FakeNeurons:
    def __init__(self):
        self.last_fired = []
        self.skip_seen = None

    async def gather_context(self, message, *, intent_category=None, effort=None,
                             max_chars=1200, skip=()):
        self.skip_seen = tuple(skip)
        self.last_fired = ["time"]
        return "[NEURONS] clock says noon"


def _agent(monkeypatch, replies_for, seen):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)

    async def _stream(messages, *a, **k):
        # A snapshot: compaction snips message objects in place on later steps, so a
        # kept reference would under-report what this call was actually sent.
        seen.append([Message(role=m.role, content=m.content) for m in messages])
        yield StreamChunk(content=await replies_for(len(seen), messages), done=True,
                          model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.model = "fake-8k"
    llm.chat = AsyncMock(return_value=MagicMock(
        content="SUMMARY: read big files 0..n; all returned BEGIN/END blocks.",
        tool_calls=[], finish_reason="stop"))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")
    agent._context_limit_discovered = WINDOW
    agent._context_limit_observed = None
    return agent


def test_fourteen_step_turn_stays_in_the_window_and_still_answers(monkeypatch):
    sid = f"spine-{uuid.uuid4().hex}"
    seen: list = []
    runs: list = []

    async def _skills(agent, message, k=3):
        return "[LEARNED SKILLS]\n" + "\n".join(
            f"- skill {i}: read big files in chunks" for i in range(80))

    async def _graph(agent, message, limit=5):
        return "[MEMORY GRAPH]\n" + "\n".join(
            f"- node {i}: a graph memory about something else" for i in range(60))

    monkeypatch.setattr(turn_memory, "skills_block", _skills)
    monkeypatch.setattr(turn_memory, "graph_block", _graph)

    async def replies_for(n, messages):
        last = str(messages[-1].content)
        if SYNTH_MARK in last:
            return "<think>wrap up</think>FINAL: read 13 files; all had BEGIN and END."
        if n == 2:
            # A redirect the user sends while the turn runs, via /chat/steer.
            assert await queue_steering_message(sid, "hint", "PIVOT-HINT-7")
        q = "2" if n == 3 else str(n)   # step 3 repeats step 2 exactly
        return (f"<think>secret-thought-{n}</think>\n"
                f'ACTION: big_read\nINPUT: {{"q": "{q}"}}')

    agent = _agent(monkeypatch, replies_for, seen)
    agent.crystal = _FakeCrystal()
    neurons = _FakeNeurons()
    agent._auto_neurons = neurons

    @agent.tool
    def big_read(q: str) -> str:
        """read a big file"""
        runs.append(q)
        return f"BEGIN-{q}-" + ("x" * 2980) + f"-END-{q}"

    events: list = []
    resp = asyncio.run(agent.stream_react("read the big files", on_event=events.append,
                                          max_steps=14, session_id=sid))
    types = [e["type"] for e in events]

    # the assembled prompt stays at or below W at every step
    assert len(seen) == 15, "14 steps plus the forced synthesis"
    for i, msgs in enumerate(seen):
        assert estimate_tokens(msgs) <= WINDOW, f"call {i + 1} over the window"

    # a context event lists kept and evicted sources; a compact event fires
    ctx = next(e for e in events if e["type"] == "context")
    kept = [k["name"] for k in ctx["kept"]]
    assert "crystal" in kept and ctx["evicted"], ctx
    assert ctx["chars_kept"] <= ctx["budget_chars"]
    assert ctx["chars_dropped"] > 0
    assert "compact" in types
    assert "turn_start" in types and types.index("turn_start") < types.index("context")
    assert all("ok" in e for e in events if e["type"] == "tool_result")

    # the scratchpad contains no <think>
    for msgs in seen:
        for m in msgs:
            if m.role == "assistant":
                assert "<think>" not in m.content and "secret-thought" not in m.content

    # an identical second call does not run, nor append the full result again
    assert runs.count("2") == 1
    assert any("Same call as step 2" in str(m.content) for m in seen[3])
    assert sum(str(m.content).count("BEGIN-2-") for m in seen[3]) == 1

    # step exhaustion produces a non-empty FINAL
    assert "synthesis" in types
    assert resp.content == "read 13 files; all had BEGIN and END."
    assert "(reached step limit)" not in json.dumps(events)

    # a queued steering hint appears in the msgs at the next step
    assert not any("PIVOT-HINT-7" in str(m.content) for m in seen[1])
    assert any("PIVOT-HINT-7" in str(m.content) for m in seen[2])
    # the neurons stage was called through its contract
    assert neurons.skip_seen == ()
    assert "neurons" in types


@pytest.mark.parametrize("stage", ["skills_block", "graph_block"])
def test_a_provider_that_raises_degrades_and_the_turn_still_answers(monkeypatch, stage):
    async def _boom(*a, **k):
        raise RuntimeError("memory plane down")

    monkeypatch.setattr(turn_memory, stage, _boom)
    seen: list = []

    async def replies_for(n, messages):
        return "FINAL: still here"

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None
    events: list = []
    resp = asyncio.run(agent.stream_react("hello there", on_event=events.append))
    deg = [e for e in events if e["type"] == "degraded"]
    assert deg and deg[0]["stage"] in ("skills", "graph_memory")
    assert resp.content == "still here"
    assert events[-1]["type"] in ("done", "knowledge_graph")


def test_a_hanging_provider_is_bounded_in_time(monkeypatch):
    async def _hang(*a, **k):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(turn_memory, "graph_block", _hang)
    seen: list = []

    async def replies_for(n, messages):
        return "FINAL: quick"

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None
    events: list = []
    resp = asyncio.run(asyncio.wait_for(
        agent.stream_react("hello", on_event=events.append), timeout=10))
    assert resp.content == "quick"
    assert any(e["type"] == "degraded" and e["stage"] == "graph_memory" for e in events)


def test_assemble_dedupes_against_history_and_scores_overlap():
    from adk.context_assembly import Source, assemble

    hist = ["- the deploy key lives in the vault under DEPLOY_KEY"]
    srcs = [
        Source("crystal", "[FACTS]\n- the deploy key lives in the vault under DEPLOY_KEY",
               priority=60),
        Source("skills", "[SKILLS]\n- cooking pasta takes ten minutes", priority=40),
        Source("graph_memory", "[GRAPH]\n- rotating the deploy key needs the vault",
               priority=30),
    ]
    kept, evicted = assemble(srcs, budget_chars=60, query="rotate the deploy key",
                             history_texts=hist)
    assert "crystal" in evicted, "its only fact is already in the history"
    assert [s.name for s in kept] == ["graph_memory"], "overlap outranks priority"
    assert "skills" in evicted


def test_snip_treats_text_protocol_observations_as_tool_results():
    from adk.context_budget import snip_old_tool_results

    msgs = [{"role": "user", "content": "OBSERVATION: " + "a" * 5000},
            {"role": "user", "content": "q" * 5000}] + [
        {"role": "assistant", "content": "x"}] * 6
    reclaimed = snip_old_tool_results(msgs, max_chars=1000)
    assert reclaimed > 0
    assert msgs[0]["content"].startswith("OBSERVATION: ")
    assert "chars snipped" in msgs[0]["content"]
    assert msgs[1]["content"] == "q" * 5000, "a plain user message is not a tool result"


def test_a_repeat_after_a_state_changing_call_runs_again(monkeypatch):
    # read -> edit -> the same read: the second read must see the edit. Answering it
    # "from step 1" hands the model a result the edit made stale.
    state = {"v": 0}
    reads: list = []
    seen: list = []
    script = [
        'ACTION: read_counter\nINPUT: {"path": "a"}',
        'ACTION: bump_counter\nINPUT: {"path": "a"}',
        'ACTION: read_counter\nINPUT: {"path": "a"}',
        'ACTION: read_counter\nINPUT: {"path": "a"}',
        "FINAL: done",
    ]

    async def replies_for(n, messages):
        return script[min(n, len(script)) - 1]

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None

    @agent.tool
    def read_counter(path: str) -> str:
        """read the counter"""
        reads.append(state["v"])
        return f"value={state['v']}"

    @agent.tool
    def bump_counter(path: str) -> str:
        """add one to the counter"""
        state["v"] += 1
        return "bumped"

    resp = asyncio.run(agent.stream_react("bump it", on_event=lambda e: None, max_steps=6))
    assert reads == [0, 1], "the read after the edit ran; the immediate repeat did not"
    assert "value=1" in str(seen[3][-1].content)
    assert "Same call as step 3" in str(seen[4][-1].content)
    assert resp.content == "done"


def test_a_slow_recall_does_not_drop_the_pinned_constraints(monkeypatch):
    class _Typed:
        async def context_block(self, message, limit=5):
            await asyncio.sleep(30)
            return "# MEMORY\n- never"

        async def constraints_block(self):
            return "# DECISIONS / CORRECTIONS\n- NEVER-DELETE-PROD-7"

    seen: list = []

    async def replies_for(n, messages):
        return "FINAL: ok"

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None
    agent._typed = _Typed()
    events: list = []
    resp = asyncio.run(asyncio.wait_for(
        agent.stream_react("hello", on_event=events.append), timeout=10))
    assert resp.content == "ok"
    assert any("NEVER-DELETE-PROD-7" in str(m.content) for m in seen[0])
    assert any(e["type"] == "degraded" and e["stage"] == "typed_memory" for e in events)


def test_the_crystal_gets_a_share_of_the_context_budget_not_all_of_it():
    from adk import agent as agent_mod

    assert agent_mod._CRYSTAL_TURN_MAX_CHARS <= 4000
    import inspect

    src = inspect.getsource(agent_mod.AitherAgent.stream_react)
    assert "_CRYSTAL_TURN_MAX_CHARS" in src


def test_a_turn_stuck_repeating_one_call_answers_instead_of_burning_its_steps(monkeypatch):
    # Live 2026-10-14: one failing codegraph_search re-sent until the step budget ran
    # out; the owner got "I reached my step limit". Past _MAX_REPEATS the turn answers.
    seen: list = []
    calls: list = []

    async def replies_for(n, messages):
        if SYNTH_MARK in str(messages[-1].content):
            return "FINAL: the code graph is unavailable (503), so I could not look it up."
        return 'ACTION: find_symbol\nINPUT: {"q": "split_near_question"}'

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None

    @agent.tool
    def find_symbol(q: str) -> str:
        """find a symbol"""
        calls.append(q)
        return "(tool error: HTTP 503 CodeGraph not available)"

    events: list = []
    resp = asyncio.run(agent.stream_react("where is split_near_question defined?",
                                          on_event=events.append, max_steps=40))
    assert calls == ["split_near_question"], "the repeats were answered, not re-run"
    assert len(seen) <= 6, f"stopped after the repeats, not at step 40 ({len(seen)} calls)"
    assert any(e["type"] == "synthesis" for e in events)
    assert "503" in resp.content and "step limit" not in resp.content


def test_a_tool_that_keeps_failing_with_new_arguments_is_refused_then_the_turn_answers(
        monkeypatch):
    # Live 2026-10-14: ask_reasoner answered 502 every time and the 12B re-asked it with
    # reworded arguments until the step budget ran out (429 s, "I reached my step limit").
    seen: list = []
    calls: list = []

    async def replies_for(n, messages):
        if SYNTH_MARK in str(messages[-1].content):
            return "FINAL: the lookup service is down (502); I could not find it."
        return f'ACTION: lookup\nINPUT: {{"q": "variant {n}"}}'

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None

    @agent.tool
    def lookup(q: str) -> str:
        """look something up"""
        calls.append(q)
        return "(tool error: HTTPStatusError: 502 Bad Gateway)"

    events: list = []
    resp = asyncio.run(agent.stream_react("where is it defined?", on_event=events.append,
                                          max_steps=40))
    assert len(calls) == 3, f"refused after 3 failures, ran {len(calls)}"
    assert len(seen) <= 9, f"answered soon after, not at step 40 ({len(seen)} calls)"
    assert "502" in resp.content and "step limit" not in resp.content


def test_a_request_to_write_code_gets_code_not_the_previous_answer_again(monkeypatch):
    # awconnect 2026-10-14: after a news answer, "write me a python script that can
    # scrape those websites." re-ran the news web_search and returned news, no code.
    from adk.agent import asks_for_code

    assert asks_for_code("write me a python script that can scrape those websites.")
    assert asks_for_code("can you make a powershell script to restart it")
    assert not asks_for_code("what is in the news today?")
    assert not asks_for_code("write a short poem about the sea")

    seen: list = []

    async def replies_for(n, messages):
        if n == 1:
            return "FINAL: Here are some recent news stories about US politics: ..."
        return "FINAL: ```python\nimport requests\nprint(requests.get('https://x').status_code)\n```"

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None
    resp = asyncio.run(agent.stream_react(
        "write me a python script that can scrape those websites.",
        on_event=lambda e: None, max_steps=6))
    assert "[TASK] The user asked you to WRITE code" in str(seen[0][-1].content)
    assert "```" in resp.content and "news stories" not in resp.content


def test_a_question_answered_with_a_bare_greeting_is_nudged_to_answer(monkeypatch):
    # Live 2026-10-14: "where is stream_react defined?" -> "I'm here to help! How can I
    # assist you today?" twice, with recall in the prompt.
    from adk.agent import deflects_question

    q = "where is stream_react defined?"
    assert deflects_question(q, "I'm here to help! How can I assist you today?")
    assert deflects_question(q, "Hello! How can I help you?")
    assert not deflects_question("hi", "Hello! How can I help you today?")
    assert not deflects_question(q, "It is defined in awdk/adk/agent.py at line 3318.")

    seen: list = []

    async def replies_for(n, messages):
        if n == 1:
            return "FINAL: I'm here to help! How can I assist you today?"
        return "FINAL: AitherAgent.stream_react is in awdk/adk/agent.py:3318."

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None
    resp = asyncio.run(agent.stream_react(q, on_event=lambda e: None, max_steps=6))
    assert "agent.py:3318" in resp.content


def test_a_new_question_after_another_topic_is_marked_new():
    # Live 2026-10-14: "what is in the news today" right after a platform question
    # answered about the platform; follow-ups ("scrape those websites") stay follow-ups.
    from adk.agent import is_new_topic

    assert is_new_topic("what is in the news today")
    assert is_new_topic("where is register_gateway_tools_on defined?")
    assert not is_new_topic("write me a python script that can scrape those websites.")
    assert not is_new_topic("do it")
    assert not is_new_topic("yo")


def test_an_offer_to_search_after_a_failed_tool_is_still_nudged(monkeypatch):
    seen: list = []

    async def replies_for(n, messages):
        if n == 1:
            return 'ACTION: find_symbol\nINPUT: {"q": "register_gateway_tools_on"}'
        if n == 2:
            return ("FINAL: I don't have a record of it. Would you like me to search for "
                    "it using another tool?")
        return "FINAL: it is in awdk/adk/server.py at line 870."

    agent = _agent(monkeypatch, replies_for, seen)
    agent._auto_neurons = None

    @agent.tool
    def find_symbol(q: str) -> str:
        """find a symbol"""
        return "(tool error: HTTP 503 CodeGraph not available)"

    resp = asyncio.run(agent.stream_react("where is register_gateway_tools_on defined?",
                                          on_event=lambda e: None, max_steps=6))
    assert "server.py" in resp.content
