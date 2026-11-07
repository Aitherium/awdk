"""Non-stream chat: a clock question is answered from the clock, and dead tools leave.

Measured on the daemon host: "what time is it?" answered correctly 1 run in 3. The
others spent 6-10 calls on tools whose backend refused every connection
(workspace_health with a new `days` each time), and a turn of refused calls was saved
as the "what time is it" skill and replayed on the next ask.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from adk import tools as tools_mod
from adk.agent import _GAVE_UP_ANSWER
from adk.situation import asks_clock, clock_hint
from adk.skills import Skill
from adk.tools import ToolRegistry

REFUSED = json.dumps({"error": "[WinError 10061] No connection could be made because "
                               "the target machine actively refused it"})


@pytest.mark.parametrize("text", [
    "what time is it?", "whats the date", "what is the date", "what day is it today",
    "current time please", "Which day is it"])
def test_clock_questions_detected(text):
    assert asks_clock(text)


@pytest.mark.parametrize("text", [
    "time to deploy", "run the timer", "what is it", "what time to deploy?",
    "set a date for the meeting", "sometime later"])
def test_non_clock_text_ignored(text):
    assert not asks_clock(text)


def test_clock_hint_carries_local_time():
    assert clock_hint().startswith("[local time: ")


@pytest.mark.parametrize("text", [
    "I can't retrieve the current time", "I cannot determine it",
    "I don't have access to real-time data", "I couldn't find it"])
def test_give_up_answers_recognised(text):
    assert _GAVE_UP_ANSWER.match(text)


def test_real_answer_is_not_a_give_up():
    assert not _GAVE_UP_ANSWER.match("The current time is 14:22.")


@pytest.fixture
def fresh_breaker(monkeypatch):
    monkeypatch.setattr(tools_mod, "_refused", {})


def test_refused_backend_leaves_the_menu_and_short_circuits(fresh_breaker):
    calls = []

    def workspace_health(days: int = 1) -> str:
        """Workspace health."""
        calls.append(days)
        return REFUSED

    def fine() -> str:
        """Fine."""
        return "ok"

    reg = ToolRegistry()
    reg.register(workspace_health)
    reg.register(fine)

    async def run():
        for d in (1, 7):
            await reg.execute("workspace_health", {"days": d})
        return await reg.execute("workspace_health", {"days": 30})

    third = asyncio.run(run())
    assert calls == [1, 7], "the tripped tool must not reach its backend again"
    assert "unavailable" in json.loads(third)["error"]
    names = [t["function"]["name"] for t in reg.to_openai_format()]
    assert "workspace_health" not in names and "fine" in names


def test_one_refusal_does_not_trip(fresh_breaker):
    def flaky() -> str:
        """Flaky."""
        return REFUSED

    reg = ToolRegistry()
    reg.register(flaky)
    asyncio.run(reg.execute("flaky", {}))
    assert [t["function"]["name"] for t in reg.to_openai_format()] == ["flaky"]


def test_chat_filters_recalled_skills_by_available_tools():
    import inspect

    from adk import agent

    src = inspect.getsource(agent.AitherAgent.chat)
    at = src.index("self._skills.search(message")
    window = src[at:at + 600]
    assert "self._tools.get(t) is not None" in window, (
        "recalled skills must be filtered to ones whose tools this agent has")
    assert Skill(name="x", description="x").tools_used == []


def test_errored_result_marks_the_call_failed():
    import inspect

    from adk import agent

    src = inspect.getsource(agent.AitherAgent.chat)
    assert 'f"{tc.name}[failed]"' in src, (
        "a tool result starting {\"error\" must mark its call [failed] so the turn "
        "teaches no skill")


def test_clock_turn_learns_no_skill():
    import inspect

    from adk import agent

    src = inspect.getsource(agent.AitherAgent._learn_after)
    assert "not asks_clock(message)" in src, (
        "a clock question must never be saved as a multi-tool skill")


def test_bare_clock_question_offers_no_tools():
    import inspect

    from adk import agent

    src = inspect.getsource(agent.AitherAgent.chat)
    at = src.index("tools_schema = _tool_sel.schemas(")
    window = src[at:at + 900]
    assert "_user_turn is not message" in window and "tools_schema = []" in window
    assert "_ACTION_PATTERNS.search(message)" in window, "an action in the ask keeps tools"
