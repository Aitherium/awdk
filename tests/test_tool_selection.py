"""An unclassified turn ships a small core + load_tools, never every schema.

Measured 2026-09-27 (in-process, the :9001 daemon's identity, fake LLM, chars/4 —
the estimate MicroScheduler applies): "reply ok" cost ~9.7k prompt tokens — 55 tool
schemas ~5.2k, a recalled-memory block ~3.6k, the system prompt ~0.9k — against a
gemma4-12b slot of 8,192. After: ~2.2k (8 schemas ~0.86k incl. load_tools, memory
capped at 2k chars).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from adk.agent import AitherAgent, _filter_tools_by_intent
from adk.llm.base import LLMResponse, ToolCall
from adk.tool_selection import (
    CORE_TOOL_NAMES,
    LOAD_TOOLS_NAME,
    TurnToolSelection,
    categorize,
    is_unclassified,
)
from adk.tools import ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    def file_read(path: str) -> str:
        """Read a file."""
        return "x"

    def web_search(query: str) -> str:
        """Search the web."""
        return "x"

    def git_status() -> str:
        """Git status."""
        return "clean"

    def git_log(n: int = 5) -> str:
        """Git log."""
        return "log"

    def self_session_summary() -> str:
        """What I did."""
        return "s"

    def gateway_thing(q: str) -> str:
        """An eager gateway tool."""
        return "g"

    for fn in (file_read, web_search, git_status, git_log, self_session_summary,
               gateway_thing):
        reg.register(fn, intent_categories=["code"] if fn.__name__.startswith("git") else None)
    return reg


def _names(schemas) -> list[str]:
    return sorted(s["function"]["name"] for s in (schemas or []))


@pytest.mark.parametrize("intent", [None, "", "DEFAULT", "default "])
def test_unclassified_turn_ships_core_plus_load_tools(intent, monkeypatch):
    monkeypatch.delenv("ADK_TOOL_SELECTION", raising=False)
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), intent, _filter_tools_by_intent)
    assert sel.active and is_unclassified(intent)
    assert _names(sel.schemas(reg.to_openai_format)) == [
        "file_read", LOAD_TOOLS_NAME, "web_search"]


def test_load_tools_description_names_every_other_category():
    """Nothing is invisible: the meta-tool lists what it can load (the 2026-08-22
    lesson — a filtered-out capability reads as one that does not exist)."""
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), None, _filter_tools_by_intent)
    desc = sel.meta_schema()["function"]["description"]
    groups = categorize(reg.list_tools())
    assert set(groups) == {"git", "self", "platform"}
    for cat, tds in groups.items():
        assert f"{cat} ({len(tds)})" in desc


def test_load_tools_expands_the_offer():
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), None, _filter_tools_by_intent)
    out = sel.load({"category": "git"})
    assert "git_status" in out and "git_log" in out
    names = _names(sel.schemas(reg.to_openai_format))
    assert {"git_status", "git_log"} <= set(names)
    assert "git" not in sel.categories  # loaded once, no longer offered to load
    assert "Unknown category" in sel.load({"category": "nope"})
    sel.load({"category": "all"})
    assert _names(sel.schemas(reg.to_openai_format)) == sorted(
        t.name for t in reg.list_tools())  # nothing left to load -> no meta-tool


def test_classified_turn_keeps_the_intent_filter():
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), "CODE", _filter_tools_by_intent)
    assert not sel.active
    assert _names(sel.schemas(reg.to_openai_format)) == _names(
        reg.to_openai_format(_filter_tools_by_intent(reg.list_tools(), "CODE")))


def test_opt_out_restores_every_schema(monkeypatch):
    monkeypatch.setenv("ADK_TOOL_SELECTION", "all")
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), None, _filter_tools_by_intent)
    assert not sel.active
    assert len(sel.schemas(reg.to_openai_format)) == len(reg.list_tools())


def test_core_set_is_small():
    """The core must stay a core: every name added costs every turn.

    Pin raised 10 -> 11 on 2026-10-05 for remember_fact + recall_facts: outside the
    core, "call remember_fact with the fact: ..." wrote a FILE named after the fact
    (measured live) — the invisible-capability defect this module already records
    for web_search. Two tiny schemas (~15% more core tokens) against that miss.
    """
    assert len(CORE_TOOL_NAMES) <= 11


def _agent_with(llm, reg) -> AitherAgent:
    agent = AitherAgent("toolsel-test", llm=llm, tools=[reg], builtin_tools=False)
    agent._graph = None
    agent._typed = None
    agent._skills = None
    agent._auto_neurons = None
    return agent


def test_chat_sends_core_then_loads_on_demand(tmp_path, monkeypatch):
    """End to end through AitherAgent.chat: turn 1 offers the core, the model calls
    load_tools, turn 2 offers the loaded category and the model can call it."""
    monkeypatch.delenv("ADK_TOOL_SELECTION", raising=False)
    seen: list[list[str]] = []
    replies = [
        LLMResponse(content="", model="m", tool_calls=[
            ToolCall(id="c1", name=LOAD_TOOLS_NAME, arguments={"category": "git"})]),
        LLMResponse(content="", model="m", tool_calls=[
            ToolCall(id="c2", name="git_status", arguments={})]),
        LLMResponse(content="clean tree", model="m"),
    ]

    async def _chat(messages, tools=None, **_kw):
        seen.append(_names(tools))
        return replies[len(seen) - 1]

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=_chat)
    llm.provider_name = "mock"
    agent = _agent_with(llm, _registry())
    resp = asyncio.run(agent.chat("summarise where we are", session_id="s-toolsel"))
    assert resp.content == "clean tree"
    assert seen[0] == ["file_read", LOAD_TOOLS_NAME, "web_search"]
    assert "git_status" in seen[1] and "git_log" in seen[1]
    assert "git_status" in resp.tool_calls_made


def test_trivial_turn_prompt_is_small(monkeypatch):
    """The measured target: a trivial unclassified turn's TOOL payload stays well under
    the 8k slot even with the daemon's full builtin set registered."""
    monkeypatch.delenv("ADK_TOOL_SELECTION", raising=False)
    captured = {}

    async def _chat(messages, tools=None, **_kw):
        captured["tools"] = tools or []
        return LLMResponse(content="ok", model="m")

    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=_chat)
    llm.provider_name = "mock"
    agent = AitherAgent("adk-daemon", llm=llm)  # the daemon identity's builtins
    agent._graph = None
    agent._typed = None
    agent._skills = None
    agent._auto_neurons = None
    registered = len(agent._tools.list_tools())
    asyncio.run(agent.chat("reply ok", session_id="s-trivial"))
    tool_tokens = len(json.dumps(captured["tools"])) // 4
    assert registered > 20, "fixture no longer registers the daemon's builtin set"
    assert len(captured["tools"]) <= len(CORE_TOOL_NAMES) + 1
    assert tool_tokens < 1500, f"trivial turn ships ~{tool_tokens} tokens of tool schemas"



def test_agent_choice_all_ships_every_schema_even_unclassified(monkeypatch):
    """Agent Home's serve agent passes mode="all": its real tools are not in the
    core, and a small local model never calls load_tools to find them."""
    monkeypatch.delenv("ADK_TOOL_SELECTION", raising=False)
    reg = _registry()
    sel = TurnToolSelection(reg.list_tools(), None, _filter_tools_by_intent, mode="all")
    assert not sel.active
    assert len(sel.schemas(reg.to_openai_format)) == len(reg.list_tools())
    assert LOAD_TOOLS_NAME not in _names(sel.schemas(reg.to_openai_format))
    # and the default is untouched
    assert TurnToolSelection(reg.list_tools(), None, _filter_tools_by_intent).active
