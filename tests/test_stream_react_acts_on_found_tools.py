"""awsh transcript 2026-10-11: discovery stopped one call short of the work.

"what is in the news today" -> search_tools("news") -> the model described the
newsletter tools and stopped; "do it" -> search_tools("do it") because the daemon's
stream path handed the model no history.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from adk.agent import AitherAgent, discovery_nudge
from adk.llm.base import StreamChunk


def _agent(replies, seen):
    it = iter(replies)

    async def _stream(messages, *a, **k):
        seen.append([m.content for m in messages])
        yield StreamChunk(content=next(it), done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.chat = AsyncMock(return_value=MagicMock(content="x", tool_calls=[], finish_reason="stop"))
    return AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")


def test_a_search_tools_menu_answered_with_prose_is_sent_back_to_act():
    seen: list = []
    agent = _agent([
        'ACTION: search_tools\nINPUT: {"query": "news"}',
        "FINAL: The tools related to news are available. I can search the web.",
        'ACTION: web_search\nINPUT: {"query": "news today"}',
        "FINAL: Top story: X.",
    ], seen)
    calls = {"web": 0}

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return json.dumps({"results": [{"name": "newsletter_create_campaign",
                                        "parameters": {"required": ["list_cn"]}}]})

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        calls["web"] += 1
        return "X happened"

    resp = asyncio.run(agent.stream_react("what is in the news today",
                                          on_event=lambda e: None, max_steps=6))
    assert calls["web"] == 1, "the turn went on to do the work"
    assert resp.content == "Top story: X."
    assert any("web_search" in str(m) and "FIND a tool" in str(m) for m in seen[2])


def test_web_reach_line_names_web_search_for_news():
    seen: list = []
    agent = _agent(["FINAL: ok"], seen)

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return "{}"

    @agent.tool
    def web_search(query: str) -> str:
        """web"""
        return ""

    asyncio.run(agent.stream_react("hi", on_event=lambda e: None))
    assert "call web_search directly" in seen[0][0]


def test_a_question_about_tools_may_end_on_the_menu():
    seen: list = []
    agent = _agent([
        'ACTION: search_tools\nINPUT: {"query": "mail"}',
        "FINAL: mail_list and mail_read.",
    ], seen)

    @agent.tool
    def search_tools(query: str) -> str:
        """find"""
        return json.dumps({"results": [{"name": "mail_list"}]})

    resp = asyncio.run(agent.stream_react("which tools handle mail?",
                                          on_event=lambda e: None))
    assert resp.content == "mail_list and mail_read."
    assert len(seen) == 2


def test_the_stream_reads_its_own_session_history_when_none_is_passed():
    seen: list = []
    agent = _agent(["FINAL: first", "FINAL: second"], seen)
    _sid = f"s-hist-{uuid.uuid4().hex}"
    asyncio.run(agent.stream_react("what is in the news today", on_event=lambda e: None,
                                   session_id=_sid))
    asyncio.run(agent.stream_react("do it", on_event=lambda e: None, session_id=_sid))
    assert "what is in the news today" in seen[1], "the follow-up carries the prior turn"


def test_nudge_hands_back_a_copyable_call():
    obs = json.dumps({"results": [{"name": "web_search",
                                   "parameters": {"required": ["query"]}}]})
    text = discovery_nudge("search_tools", obs)
    assert 'INPUT: {"name": "web_search", "arguments": {"query": "<value>"}}' in text
    assert "call_tool" not in discovery_nudge("load_tools", "ok")


def test_daemon_stream_budget_follows_effort_and_fails_open():
    from adk import server

    class _Mem:
        async def get_history(self, sid, limit=50):
            return []

    def _agent_with(effort=None, boom=False):
        a = MagicMock()
        a.memory = _Mem()

        async def _classify(message, history=None):
            if boom:
                raise RuntimeError("router down")
            return MagicMock(effort=effort, intent="research", agentic=True)
        a.classify_intent = _classify
        return a

    events: list = []
    assert asyncio.run(server._effort_step_budget(_agent_with(8), "m", "s", events.append)) == 14
    assert events[0]["type"] == "effort" and events[0]["max_steps"] == 14
    assert asyncio.run(server._effort_step_budget(_agent_with(3), "m", "s", events.append)) == 6
    assert asyncio.run(server._effort_step_budget(_agent_with(boom=True), "m", "s",
                                                  events.append)) == 6
