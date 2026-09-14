"""Human-in-the-loop tool approval — pause before a gated tool, resume on decision."""

import os
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock

import adk.approval as approval
from adk.agent import AitherAgent
from adk.approval import ApprovalStore, needs_approval
from adk.llm.base import LLMResponse, ToolCall
from adk.memory import Memory
from adk.tools import ToolRegistry


@pytest.fixture()
def tmp_memory(tmp_path):
    return Memory(db_path=tmp_path / "test.db", agent_name="test")


@pytest.fixture()
def isolated_store(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("AITHER_ADK_STATE_DIR", d)
    monkeypatch.setattr(approval, "_STORE", None)  # force re-init at the new path
    return d


def _tool_llm():
    """A mock LLM that proposes the gated 'search' tool, then answers."""
    tool_call_resp = LLMResponse(
        content="", model="mock",
        tool_calls=[ToolCall(id="tc_1", name="search", arguments={"q": "test"})],
    )
    final_resp = LLMResponse(content="Found results!", model="mock")
    llm = MagicMock()
    llm.provider_name = "mock"
    # 1st chat: proposes tool → pauses. resume chat: proposes tool → decision applied →
    # (if allowed) final answer. Provide enough turns for both passes.
    llm.chat = AsyncMock(side_effect=[tool_call_resp, tool_call_resp, final_resp])
    return llm


def _agent(llm, tmp_memory):
    tools = ToolRegistry()
    tools.register(lambda q: f"Results for {q}", name="search", description="Search")
    return AitherAgent("test", llm=llm, tools=[tools], memory=tmp_memory)


def test_policy_reads_env(monkeypatch):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "search, file_write")
    assert needs_approval("test", "search") is True
    assert needs_approval("test", "file_write") is True
    assert needs_approval("test", "list_dir") is False
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "*")
    assert needs_approval("test", "anything") is True
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    assert needs_approval("test", "search") is False


def test_store_roundtrip_and_decisions(isolated_store):
    s = ApprovalStore()
    s.put_pending("sid1", user_message="do it", agent="test",
                  pending=[{"tool_use_id": "tc_1", "tool": "search", "args": {}}])
    assert s.get("sid1")["user_message"] == "do it"
    # decision by tool_use_id resolves to the tool name
    s.record_decisions("sid1", [{"tool_use_id": "tc_1", "result": "allow"}])
    assert s.decision_for("sid1", "search") == "allow"
    s.clear("sid1")
    assert s.get("sid1") is None


@pytest.mark.asyncio
async def test_chat_pauses_for_gated_tool(monkeypatch, isolated_store, tmp_memory):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "search")
    agent = _agent(_tool_llm(), tmp_memory)
    resp = await agent.chat("Search for test", session_id="sP")
    assert resp.requires_action is True
    assert resp.finish_reason == "requires_action"
    assert any(p["tool"] == "search" for p in resp.pending)
    # The pause is persisted for a later (possibly days-later) resume.
    assert approval.get_approval_store().get("sP") is not None


@pytest.mark.asyncio
async def test_resume_allow_executes_and_completes(monkeypatch, isolated_store, tmp_memory):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "search")
    agent = _agent(_tool_llm(), tmp_memory)
    await agent.chat("Search for test", session_id="sA")
    resumed = await agent.resume("sA", [{"tool_use_id": "tc_1", "result": "allow"}])
    assert resumed.requires_action is False
    assert resumed.content == "Found results!"
    assert "search" in resumed.tool_calls_made
    # The paused entry is cleared once the turn completes.
    assert approval.get_approval_store().get("sA") is None


@pytest.mark.asyncio
async def test_resume_deny_skips_tool(monkeypatch, isolated_store, tmp_memory):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "search")
    agent = _agent(_tool_llm(), tmp_memory)
    await agent.chat("Search for test", session_id="sD")
    resumed = await agent.resume("sD", [{"tool_use_id": "tc_1", "result": "deny"}])
    assert resumed.requires_action is False
    assert "search[denied]" in resumed.tool_calls_made


@pytest.mark.asyncio
async def test_no_policy_never_pauses(monkeypatch, isolated_store, tmp_memory):
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")  # gating off
    agent = _agent(_tool_llm(), tmp_memory)
    resp = await agent.chat("Search for test", session_id="sN")
    assert resp.requires_action is False
    assert resp.content == "Found results!"
