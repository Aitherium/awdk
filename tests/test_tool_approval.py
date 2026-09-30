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


def _pending_store(sid):
    s = ApprovalStore()
    s.put_pending(sid, user_message="m", agent="test",
                  pending=[{"tool_use_id": "tc_1", "tool": "search", "args": {}}])
    return s


def test_missing_result_fails_closed(isolated_store):
    s = _pending_store("sM")
    merged = s.record_decisions("sM", [{"tool_use_id": "tc_1"}])
    assert merged == {"search": "deny"}
    assert s.decision_for("sM", "search") == "deny"


@pytest.mark.parametrize(
    "garbage", ["yes", "ALLOWED", "", None, 1, True, ["allow"], {"v": "allow"}]
)
def test_malformed_result_fails_closed(isolated_store, garbage):
    s = _pending_store("sG")
    s.record_decisions("sG", [{"tool": "search", "result": garbage}])
    assert s.decision_for("sG", "search") == "deny"


@pytest.mark.parametrize("value", ["allow", "Allow", " ALLOW "])
def test_explicit_allow_is_allowed(isolated_store, value):
    s = _pending_store("sX")
    s.record_decisions("sX", [{"tool_use_id": "tc_1", "result": value}])
    assert s.decision_for("sX", "search") == "allow"


def test_explicit_deny_is_denied(isolated_store):
    s = _pending_store("sY")
    s.record_decisions("sY", [{"tool": "search", "result": "deny"}])
    assert s.decision_for("sY", "search") == "deny"


def test_clear_empties_decisions(isolated_store):
    s = _pending_store("sC")
    s.record_decisions("sC", [{"tool": "search", "result": "allow"}])
    assert s.decision_for("sC", "search") == "allow"
    s.clear("sC")
    assert s.get("sC") is None
    assert s.decision_for("sC", "search") is None
    s.clear("sC")  # idempotent on an unknown session


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


# ── per-call keying: an allow covers the args on the card, not the tool ──────────

def _two_call_store(sid):
    s = ApprovalStore()
    s.put_pending(sid, user_message="m", agent="test", pending=[
        {"tool_use_id": "tc_a", "tool": "send", "args": {"to": "a"}},
        {"tool_use_id": "tc_b", "tool": "send", "args": {"to": "b"}},
    ])
    return s


def test_allow_by_id_covers_only_that_calls_args(isolated_store):
    s = _two_call_store("sK")
    s.record_decisions("sK", [{"tool_use_id": "tc_a", "result": "allow"}])
    assert s.decision_for("sK", "send", {"to": "a"}) == "allow"
    # the other pending call and a fresh call were not on the answer -> undecided
    assert s.decision_for("sK", "send", {"to": "b"}) is None
    assert s.decision_for("sK", "send", {"to": "c"}) is None
    # back-compat: no args -> the tool-level label
    assert s.decision_for("sK", "send") == "allow"


def test_allow_by_tool_name_covers_every_pending_call_of_that_tool(isolated_store):
    s = _two_call_store("sT")
    s.record_decisions("sT", [{"tool": "send", "result": "allow"}])
    assert s.decision_for("sT", "send", {"to": "a"}) == "allow"
    assert s.decision_for("sT", "send", {"to": "b"}) == "allow"
    assert s.decision_for("sT", "send", {"to": "zzz"}) is None


def test_mixed_decisions_are_per_call(isolated_store):
    s = _two_call_store("sMx")
    s.record_decisions("sMx", [{"tool_use_id": "tc_a", "result": "allow"},
                               {"tool_use_id": "tc_b", "result": "deny"}])
    assert s.decision_for("sMx", "send", {"to": "a"}) == "allow"
    assert s.decision_for("sMx", "send", {"to": "b"}) == "deny"


def test_tool_level_deny_is_never_narrowed(isolated_store):
    s = _two_call_store("sDn")
    s.record_decisions("sDn", [{"tool": "send", "result": "deny"}])
    assert s.decision_for("sDn", "send", {"to": "new"}) == "deny"


def test_arg_key_order_does_not_matter(isolated_store):
    s = ApprovalStore()
    s.put_pending("sO", user_message="m", agent="test",
                  pending=[{"tool_use_id": "t1", "tool": "send", "args": {"a": 1, "b": 2}}])
    s.record_decisions("sO", [{"tool_use_id": "t1", "result": "allow"}])
    assert s.decision_for("sO", "send", {"b": 2, "a": 1}) == "allow"


def test_decision_without_pending_args_stays_tool_level(isolated_store):
    """A decision recorded before any pending call (legacy clients) still answers."""
    s = ApprovalStore()
    s.record_decisions("sL", [{"tool": "search", "result": "allow"}])
    assert s.decision_for("sL", "search", {"q": "anything"}) == "allow"


def test_module_level_decision_for_passes_args(isolated_store):
    s = approval.get_approval_store()
    s.put_pending("sMod", user_message="m", agent="test",
                  pending=[{"tool_use_id": "t1", "tool": "send", "args": {"to": "a"}}])
    s.record_decisions("sMod", [{"tool_use_id": "t1", "result": "allow"}])
    assert approval.decision_for("sMod", "send", {"to": "a"}) == "allow"
    assert approval.decision_for("sMod", "send", {"to": "b"}) is None


@pytest.mark.asyncio
async def test_resume_allow_does_not_cover_a_second_call_with_other_args(
        monkeypatch, isolated_store, tmp_memory):
    """Allowing search(q=test) must not let the same turn run search(q=other) unasked."""
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "search")
    first = LLMResponse(content="", model="mock",
                        tool_calls=[ToolCall(id="tc_1", name="search", arguments={"q": "test"})])
    second = LLMResponse(content="", model="mock",
                         tool_calls=[ToolCall(id="tc_2", name="search",
                                              arguments={"q": "other"})])
    llm = MagicMock()
    llm.provider_name = "mock"
    llm.chat = AsyncMock(side_effect=[first, first, second,
                                      LLMResponse(content="done", model="mock")])
    agent = _agent(llm, tmp_memory)
    await agent.chat("Search for test", session_id="sPC")
    resumed = await agent.resume("sPC", [{"tool_use_id": "tc_1", "result": "allow"}])
    assert "search" in resumed.tool_calls_made          # the approved call ran
    assert resumed.requires_action is True             # the new-args call asks again
    assert any(p["args"] == {"q": "other"} for p in resumed.pending)
