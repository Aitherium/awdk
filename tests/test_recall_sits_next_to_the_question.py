"""Recalled facts ride in the turn's user message, and failed tool lines are not facts.

Measured live 2026-10-14: for "why were awsh turns slow yesterday?" the crystal recalled
the right fact FIRST, in a system block at the top of the prompt, and the 12B model
web-searched "awsh" and answered with AWS outages. Recall also carried junk lines
crystallized from failed calls ("call_tool: Unknown tool ...").
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from adk import turn_memory
from adk.agent import AitherAgent
from adk.llm.base import StreamChunk

FACT = "awsh turns were slow because the orchestrator lane was shared with training"


class _Crystal:
    telemetry = {"degraded": []}
    last_recall: dict = {}

    async def recall_block(self, message, exclude=(), max_chars=None):
        self.last_recall = {"facts": 1, "memory": 0, "graph_hits": 0, "chars": 120}
        return f"[FACTS] Known facts.\n- {FACT}"

    async def crystallize(self, text):
        return None


def test_crystal_facts_are_in_the_question_message_not_a_system_block(monkeypatch):
    monkeypatch.delenv("ADK_CONTEXT_LIMIT", raising=False)
    seen: list = []

    async def _stream(messages, *a, **k):
        seen.append([(m.role, str(m.content)) for m in messages])
        yield StreamChunk(content="FINAL: the orchestrator lane was shared.", done=True,
                          model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.model = "fake"
    llm.chat = AsyncMock(return_value=MagicMock(content="", tool_calls=[],
                                                finish_reason="stop"))
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")
    agent.crystal = _Crystal()
    asyncio.run(agent.stream_react("why were awsh turns slow yesterday?", on_event=lambda e: None,
                                  max_steps=2))

    first = seen[0]
    systems = [c for r, c in first if r == "system"]
    assert not any(FACT in c for c in systems), "recall must not sit at the prompt top"
    role, last = first[-1]
    assert role == "user"
    assert FACT in last and last.rstrip().endswith("why were awsh turns slow yesterday?")


def test_failed_lines_inside_an_ok_result_are_not_crystallized():
    facts = turn_memory._observation_facts([(
        "call_tool",
        "call_tool: Unknown tool 'fleet_health_summary', did you mean fleet_status\n"
        "the orchestrator lane served 41 requests in the last hour on node seven",
    )])
    assert "Unknown tool" not in facts
    assert "node seven" in facts


def test_only_facts_sharing_a_word_with_the_question_move_next_to_it():
    from adk.context_assembly import split_near_question
    block = ("[FACTS] Known facts.\n"
             "- ops_state: platform llm pool is not available on the mesh\n"
             "- web_search: tanium ships autonomous endpoint management")
    near, rest = split_near_question(block, "what is in the news today")
    assert near == "" and rest == block, "an unrelated question gets nothing glued on"
    near, rest = split_near_question(block, "anything new about tanium?")
    assert "tanium" in near and "tanium" not in rest
    assert rest.startswith("[FACTS]") and "platform" in rest
    near, rest = split_near_question(block, "is the platform llm pool up and is tanium ok")
    assert rest == "", "a header with no facts left is dropped"
