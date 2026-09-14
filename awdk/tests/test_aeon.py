"""Tests for Aeon — Multi-Agent Group Chat."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.aeon import (
    AEON_PRESETS,
    AeonMessage,
    AeonResponse,
    AeonSession,
    _get_description,
    group_chat,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mock_agent_response(content="mock response", model="test-model", tokens=10):
    """Create a mock AgentResponse."""
    from adk.agent import AgentResponse
    return AgentResponse(
        content=content,
        model=model,
        tokens_used=tokens,
        latency_ms=5.0,
        session_id="test-session",
    )


def _patch_agent_chat(responses=None):
    """Patch AitherAgent.__init__ and .chat() to avoid real LLM calls."""
    if responses is None:
        responses = {}

    original_init = None

    def mock_init(self, *args, **kwargs):
        self.name = kwargs.get("name", args[0] if args else "mock")
        self._identity = MagicMock()
        self._identity.name = self.name
        self._identity.description = f"Mock {self.name}"
        self.llm = MagicMock()
        self.llm._provider_name = "vllm"
        self.config = MagicMock()
        self._session_id = "test"
        self._events = None
        self._safety = None
        self._context_mgr = None
        self._graph = None
        self._auto_neurons = None
        self._strata = None
        self.meter = MagicMock()
        self.memory = MagicMock()
        self._tools = MagicMock()
        self._system_prompt = None
        self._phonehome = False

    async def mock_chat(self, message, history=None, session_id=None, **kwargs):
        name = self.name
        if name in responses:
            content = responses[name]
        else:
            content = f"Response from {name}"
        return _mock_agent_response(content=content, model="test-model", tokens=10)

    return patch.multiple(
        "adk.agent.AitherAgent",
        __init__=mock_init,
        chat=mock_chat,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Data Model Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAeonMessage:
    def test_create_message(self):
        msg = AeonMessage(agent="atlas", content="hello", round_number=1)
        assert msg.agent == "atlas"
        assert msg.content == "hello"
        assert msg.role == "assistant"
        assert msg.round_number == 1

    def test_to_dict(self):
        msg = AeonMessage(
            agent="atlas", content="hello", role="assistant",
            timestamp=1000.0, model="test", tokens_used=5,
            latency_ms=10.0, round_number=1,
        )
        d = msg.to_dict()
        assert d["agent"] == "atlas"
        assert d["content"] == "hello"
        assert d["tokens_used"] == 5
        assert d["round_number"] == 1

    def test_default_values(self):
        msg = AeonMessage(agent="user", content="hi")
        assert msg.timestamp == 0.0
        assert msg.model == ""
        assert msg.tokens_used == 0
        assert msg.latency_ms == 0.0
        assert msg.round_number == 0


class TestAeonResponse:
    def test_empty_response(self):
        resp = AeonResponse()
        assert resp.messages == []
        assert resp.synthesis is None
        assert resp.total_tokens == 0

    def test_to_dict(self):
        msg = AeonMessage(agent="atlas", content="hello")
        synth = AeonMessage(agent="aither", content="synthesis")
        resp = AeonResponse(
            messages=[msg],
            synthesis=synth,
            round_number=1,
            total_tokens=20,
            total_latency_ms=100.0,
            session_id="aeon-test",
        )
        d = resp.to_dict()
        assert len(d["messages"]) == 1
        assert d["synthesis"]["agent"] == "aither"
        assert d["session_id"] == "aeon-test"

    def test_to_dict_no_synthesis(self):
        resp = AeonResponse(messages=[], session_id="s1")
        d = resp.to_dict()
        assert d["synthesis"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Preset Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPresets:
    def test_all_presets_exist(self):
        expected = {"balanced", "creative", "technical", "security", "minimal", "duo_code", "research"}
        assert expected == set(AEON_PRESETS.keys())

    def test_all_presets_have_aither(self):
        for name, agents in AEON_PRESETS.items():
            assert "aither" in agents, f"Preset {name} missing orchestrator 'aither'"

    def test_balanced_preset(self):
        assert AEON_PRESETS["balanced"] == ["atlas", "hydra", "aither"]

    def test_minimal_preset(self):
        assert AEON_PRESETS["minimal"] == ["aither"]

    def test_technical_preset(self):
        assert AEON_PRESETS["technical"] == ["demiurge", "hydra", "aither"]


# ─────────────────────────────────────────────────────────────────────────────
# Description Helper
# ─────────────────────────────────────────────────────────────────────────────

class TestDescriptions:
    def test_known_agent(self):
        desc = _get_description("atlas")
        assert "management" in desc.lower() or "monitoring" in desc.lower()

    def test_unknown_agent_fallback(self):
        # Should not crash, returns the name itself
        desc = _get_description("nonexistent_agent_xyz")
        assert isinstance(desc, str)
        assert len(desc) > 0


# ─────────────────────────────────────────────────────────────────────────────
# AeonSession Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAeonSession:
    def test_init_default(self):
        with _patch_agent_chat():
            session = AeonSession()
        assert session.orchestrator == "aither"
        assert session.rounds == 1
        assert session.synthesize is True
        assert session.session_id.startswith("aeon-")

    def test_init_preset(self):
        with _patch_agent_chat():
            session = AeonSession(preset="technical")
        assert "demiurge" in session.participants
        assert "hydra" in session.participants
        assert session.participants[-1] == "aither"  # orchestrator last

    def test_init_custom_participants(self):
        with _patch_agent_chat():
            session = AeonSession(participants=["athena", "demiurge"])
        # Orchestrator auto-appended
        assert "aither" in session.participants
        assert session.participants[-1] == "aither"

    def test_orchestrator_always_last(self):
        with _patch_agent_chat():
            session = AeonSession(participants=["aither", "atlas", "hydra"])
        assert session.participants[-1] == "aither"
        assert session.participants.count("aither") == 1

    def test_unknown_preset_falls_back_to_balanced(self):
        with _patch_agent_chat():
            session = AeonSession(preset="nonexistent")
        assert session.participants == ["atlas", "hydra", "aither"]

    def test_rounds_minimum_one(self):
        with _patch_agent_chat():
            session = AeonSession(rounds=0)
        assert session.rounds == 1

    def test_reset(self):
        with _patch_agent_chat():
            session = AeonSession()
        old_id = session.session_id
        session.reset()
        assert session.session_id != old_id
        assert session.session_id.startswith("aeon-")
        assert session.history == []

    def test_participants_property_returns_copy(self):
        with _patch_agent_chat():
            session = AeonSession(preset="balanced")
        p = session.participants
        p.append("extra")
        assert "extra" not in session.participants


class TestAeonChat:
    @pytest.mark.asyncio
    async def test_basic_chat(self):
        """All agents respond, synthesis is produced."""
        responses = {
            "atlas": "Atlas says hello",
            "hydra": "Hydra says hello",
            "aither": "Synthesized response",
        }
        with _patch_agent_chat(responses):
            session = AeonSession(preset="balanced")
            # Patch conversation store to avoid filesystem
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Hello everyone")

        assert len(result.messages) == 2  # atlas + hydra (non-orchestrator)
        assert result.synthesis is not None
        assert result.synthesis.agent == "aither"
        assert result.session_id.startswith("aeon-")
        assert result.round_number == 1

    @pytest.mark.asyncio
    async def test_no_synthesize(self):
        """With synthesize=False, no synthesis message."""
        with _patch_agent_chat():
            session = AeonSession(preset="balanced", synthesize=False)
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Test")

        assert result.synthesis is None
        assert len(result.messages) == 2

    @pytest.mark.asyncio
    async def test_minimal_preset(self):
        """Minimal preset — only orchestrator, no synthesis needed."""
        with _patch_agent_chat():
            session = AeonSession(preset="minimal")
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Hello")

        # Minimal has only aither, which is both the only participant and orchestrator
        assert len(result.messages) == 1
        assert result.messages[0].agent == "aither"

    @pytest.mark.asyncio
    async def test_multi_round(self):
        """Multiple rounds produce more messages."""
        with _patch_agent_chat():
            session = AeonSession(preset="duo_code", rounds=2)
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Design a cache")

        # duo_code = [demiurge, aither], 2 rounds
        # Each round: 1 non-orch message + 1 synthesis = 2 messages per round
        assert len(result.messages) == 2  # 1 per round from demiurge
        assert result.round_number == 2

    @pytest.mark.asyncio
    async def test_history_accumulates(self):
        """Multiple chat() calls accumulate history."""
        with _patch_agent_chat():
            session = AeonSession(preset="minimal")
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                await session.chat("First message")
                await session.chat("Second message")

        # Each chat adds: user msg + agent response
        assert len(session.history) == 4

    @pytest.mark.asyncio
    async def test_ollama_serial_execution(self):
        """When provider is Ollama, agents run serially."""
        call_order = []

        async def mock_chat(self, message, history=None, session_id=None, **kwargs):
            call_order.append(self.name)
            return _mock_agent_response(content=f"From {self.name}")

        def mock_init(self, *args, **kwargs):
            self.name = kwargs.get("name", args[0] if args else "mock")
            self._identity = MagicMock()
            self.llm = MagicMock()
            self.llm._provider_name = "ollama"
            self.config = MagicMock()
            self._session_id = "test"
            self._events = None
            self._safety = None
            self._context_mgr = None
            self._graph = None
            self._auto_neurons = None
            self._strata = None
            self.meter = MagicMock()
            self.memory = MagicMock()
            self._tools = MagicMock()
            self._system_prompt = None
            self._phonehome = False

        with patch.multiple("adk.agent.AitherAgent", __init__=mock_init, chat=mock_chat):
            session = AeonSession(preset="technical")
            # Set shared LLM to ollama
            session._shared_llm._provider_name = "ollama"
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Test serial")

        # demiurge and hydra should have been called (serial order)
        assert "demiurge" in call_order
        assert "hydra" in call_order

    @pytest.mark.asyncio
    async def test_agent_error_handling(self):
        """Agent errors produce error messages instead of crashing."""
        async def failing_chat(self, message, history=None, session_id=None, **kwargs):
            if self.name == "atlas":
                raise RuntimeError("LLM connection failed")
            return _mock_agent_response(content=f"From {self.name}")

        def mock_init(self, *args, **kwargs):
            self.name = kwargs.get("name", args[0] if args else "mock")
            self._identity = MagicMock()
            self.llm = MagicMock()
            self.llm._provider_name = "vllm"
            self.config = MagicMock()
            self._session_id = "test"
            self._events = None
            self._safety = None
            self._context_mgr = None
            self._graph = None
            self._auto_neurons = None
            self._strata = None
            self.meter = MagicMock()
            self.memory = MagicMock()
            self._tools = MagicMock()
            self._system_prompt = None
            self._phonehome = False

        with patch.multiple("adk.agent.AitherAgent", __init__=mock_init, chat=failing_chat):
            session = AeonSession(preset="balanced")
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Test error")

        # Should not crash — atlas produces error message, hydra works
        atlas_msg = [m for m in result.messages if m.agent == "atlas"][0]
        assert "[Error:" in atlas_msg.content
        hydra_msg = [m for m in result.messages if m.agent == "hydra"][0]
        assert "From hydra" in hydra_msg.content

    @pytest.mark.asyncio
    async def test_tokens_and_latency_aggregated(self):
        """Total tokens and latency are summed across agents."""
        with _patch_agent_chat():
            session = AeonSession(preset="duo_code")
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await session.chat("Test")

        # 1 non-orch (10 tokens) + 1 synthesis (10 tokens) = 20
        assert result.total_tokens == 20


# ─────────────────────────────────────────────────────────────────────────────
# Context Building
# ─────────────────────────────────────────────────────────────────────────────

class TestContextBuilding:
    def test_group_context(self):
        with _patch_agent_chat():
            session = AeonSession(preset="technical")
        ctx = session._build_group_context("demiurge")
        assert "[AEON GROUP CHAT]" in ctx
        assert "hydra" in ctx
        assert "aither" in ctx
        # Should not mention self
        assert "- demiurge:" not in ctx

    def test_synthesis_prompt(self):
        with _patch_agent_chat():
            session = AeonSession()
        msgs = [
            AeonMessage(agent="atlas", content="Atlas analysis"),
            AeonMessage(agent="hydra", content="Hydra review"),
        ]
        prompt = session._build_synthesis_prompt(msgs)
        assert "[SYNTHESIS]" in prompt
        assert "[atlas]: Atlas analysis" in prompt
        assert "[hydra]: Hydra review" in prompt

    def test_history_messages(self):
        with _patch_agent_chat():
            session = AeonSession(preset="balanced")
        # Add some history
        session._history.append(AeonMessage(agent="user", content="Hello", role="user"))
        session._history.append(AeonMessage(agent="atlas", content="Hi from atlas"))

        msgs = session._build_history_messages("hydra")
        assert msgs[0]["role"] == "system"  # group context
        assert "[AEON GROUP CHAT]" in msgs[0]["content"]
        assert msgs[1]["role"] == "user"
        assert msgs[2]["content"].startswith("[atlas]: ")


# ─────────────────────────────────────────────────────────────────────────────
# group_chat() convenience function
# ─────────────────────────────────────────────────────────────────────────────

class TestGroupChat:
    @pytest.mark.asyncio
    async def test_one_shot(self):
        with _patch_agent_chat():
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await group_chat("Hello", preset="minimal")
        assert isinstance(result, AeonResponse)
        assert result.session_id.startswith("aeon-")

    @pytest.mark.asyncio
    async def test_custom_participants(self):
        with _patch_agent_chat():
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await group_chat(
                    "Test", participants=["demiurge", "athena"],
                )
        # Should have responses from demiurge + athena, synthesis from aither
        agent_names = {m.agent for m in result.messages}
        assert "demiurge" in agent_names
        assert "athena" in agent_names

    @pytest.mark.asyncio
    async def test_no_synthesize(self):
        with _patch_agent_chat():
            with patch("adk.conversations.get_conversation_store", side_effect=ImportError):
                result = await group_chat("Test", preset="balanced", synthesize=False)
        assert result.synthesis is None


# ─────────────────────────────────────────────────────────────────────────────
# Conversation Persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    @pytest.mark.asyncio
    async def test_conversation_store_called(self):
        """Verify that conversation store is used for persistence."""
        mock_store = MagicMock()
        mock_conv = MagicMock()
        mock_conv.messages = []
        mock_conv.metadata = {}
        mock_store.get_or_create = AsyncMock(return_value=mock_conv)

        with _patch_agent_chat():
            with patch("adk.conversations.get_conversation_store", return_value=mock_store):
                session = AeonSession(preset="minimal")
                await session.chat("Hello")

        mock_store.get_or_create.assert_called_once()
        mock_store._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_persistence_failure_non_fatal(self):
        """Persistence failures should not crash the chat."""
        with _patch_agent_chat():
            with patch("adk.conversations.get_conversation_store", side_effect=RuntimeError("DB gone")):
                session = AeonSession(preset="minimal")
                result = await session.chat("Hello")
        # Should still return a valid response
        assert len(result.messages) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Server Endpoint Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestServerEndpoints:
    @pytest.fixture
    def client(self):
        """Create a test client with mocked agents."""
        from fastapi.testclient import TestClient
        from adk.server import create_app

        with _patch_agent_chat():
            app = create_app(identity="aither")
            return TestClient(app)

    def test_aeon_presets_endpoint(self, client):
        resp = client.get("/aeon/presets")
        assert resp.status_code == 200
        data = resp.json()
        assert "presets" in data
        assert "balanced" in data["presets"]
        assert "aither" in data["presets"]["balanced"]

    def test_aeon_chat_missing_message(self, client):
        resp = client.post("/aeon/chat", json={})
        assert resp.status_code == 400

    def test_aeon_chat_basic(self, client):
        with _patch_agent_chat(), patch("adk.conversations.get_conversation_store", side_effect=ImportError):
            resp = client.post("/aeon/chat", json={
                "message": "Hello group",
                "preset": "minimal",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "messages" in data
        assert "participants" in data

    def test_aeon_session_reuse(self, client):
        with _patch_agent_chat(), patch("adk.conversations.get_conversation_store", side_effect=ImportError):
            # First chat — creates session
            resp1 = client.post("/aeon/chat", json={
                "message": "First",
                "preset": "minimal",
            })
            sid = resp1.json()["session_id"]

            # Second chat — reuse session
            resp2 = client.post("/aeon/chat", json={
                "message": "Second",
                "session_id": sid,
            })
        assert resp2.json()["session_id"] == sid

    def test_aeon_session_detail(self, client):
        # _patch_agent_chat is required: without it the endpoint runs a REAL
        # multi-agent round — on a box with a live fleet the LLM call connects
        # and blocks past the suite timeout (hung locally; CI only passed
        # because no backend exists there and the call fails fast).
        with _patch_agent_chat(), patch("adk.conversations.get_conversation_store", side_effect=ImportError):
            resp = client.post("/aeon/chat", json={
                "message": "Hello",
                "preset": "minimal",
            })
            sid = resp.json()["session_id"]

            detail = client.get(f"/aeon/sessions/{sid}")
        assert detail.status_code == 200
        data = detail.json()
        assert data["session_id"] == sid
        assert "history" in data
        assert len(data["history"]) > 0

    def test_aeon_session_not_found(self, client):
        resp = client.get("/aeon/sessions/nonexistent")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# __init__.py exports
# ─────────────────────────────────────────────────────────────────────────────

class TestExports:
    def test_aeon_session_importable(self):
        from adk import AeonSession
        assert AeonSession is not None

    def test_aeon_response_importable(self):
        from adk import AeonResponse
        assert AeonResponse is not None

    def test_aeon_message_importable(self):
        from adk import AeonMessage
        assert AeonMessage is not None

    def test_group_chat_importable(self):
        from adk import group_chat
        assert callable(group_chat)

    def test_presets_importable(self):
        from adk import AEON_PRESETS
        assert isinstance(AEON_PRESETS, dict)
