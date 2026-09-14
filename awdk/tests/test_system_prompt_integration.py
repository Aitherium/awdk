"""Integration test for system_prompt override flow (pack → spec → agent)."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from adk.agent import AitherAgent
from adk.config import Config
from adk.pack_discovery import load_agent_spec
from adk.llm.base import LLMResponse


class TestSystemPromptIntegration:
    """Test the complete flow from pack spec to agent construction."""

    def test_full_flow_base_spec_to_agent(self):
        """Test loading spec from yaml and creating agent with it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # 1. Create agent.yaml with base spec
            base_spec = {
                "name": "myagent",
                "system_prompt": "Base system prompt from pack",
                "capabilities": ["chat", "tools"],
            }
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(base_spec), encoding="utf-8")

            # 2. Load spec (simulating what server.py does)
            loaded_spec = load_agent_spec(agent_yaml)
            assert loaded_spec["system_prompt"] == "Base system prompt from pack"

            # 3. Create agent with loaded spec
            agent = AitherAgent(
                name="test",
                system_prompt=loaded_spec.get("system_prompt"),
            )
            assert agent.system_prompt == "Base system prompt from pack"

    def test_full_flow_with_local_override(self):
        """Test complete flow: base spec + local override → agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # 1. Create base agent.yaml
            base_spec = {
                "name": "myagent",
                "system_prompt": "Original pack prompt",
                "capabilities": ["chat", "tools", "web"],
            }
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(base_spec), encoding="utf-8")

            # 2. Create local override
            local_spec = {
                "system_prompt": "User-customized prompt",
                "capabilities": ["code"],  # List replacement
            }
            local_yaml = pack_dir / "agent.yaml.local"
            local_yaml.write_text(yaml.dump(local_spec), encoding="utf-8")

            # 3. Load merged spec (simulating /pack customize)
            merged_spec = load_agent_spec(agent_yaml)
            assert merged_spec["system_prompt"] == "User-customized prompt"
            assert merged_spec["capabilities"] == ["code"]

            # 4. Create agent with merged spec
            agent = AitherAgent(
                name="test",
                system_prompt=merged_spec.get("system_prompt"),
            )
            assert agent.system_prompt == "User-customized prompt"

    @pytest.mark.asyncio
    async def test_full_flow_with_chat(self):
        """Test agent with custom prompt can successfully chat."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # 1. Create agent spec
            spec = {
                "system_prompt": "You are a specialized assistant",
            }
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(spec), encoding="utf-8")

            # 2. Load spec
            loaded_spec = load_agent_spec(agent_yaml)

            # 3. Mock LLM
            mock_llm = MagicMock()
            mock_llm.provider_name = "mock"
            mock_llm.chat = AsyncMock(return_value=LLMResponse(
                content="Test response",
                model="mock-model",
                tokens_used=10,
                latency_ms=50.0,
            ))

            # 4. Create agent with spec
            agent = AitherAgent(
                name="test",
                system_prompt=loaded_spec.get("system_prompt"),
                llm=mock_llm,
            )

            # 5. Verify agent has correct prompt
            assert agent.system_prompt == "You are a specialized assistant"

            # 6. Agent can chat
            response = await agent.chat("Hello")
            assert response.content == "Test response"

    def test_flow_with_missing_system_prompt_uses_identity(self):
        """Test that missing system_prompt in spec falls back to identity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # 1. Create spec without system_prompt
            spec = {
                "capabilities": ["chat"],
            }
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(spec), encoding="utf-8")

            # 2. Load spec
            loaded_spec = load_agent_spec(agent_yaml)
            # system_prompt is not in spec
            assert "system_prompt" not in loaded_spec or loaded_spec.get("system_prompt") is None

            # 3. Create agent without explicit system_prompt
            # (identity will provide default)
            agent = AitherAgent(
                name="myagent",
                system_prompt=loaded_spec.get("system_prompt"),  # None
            )

            # 4. Agent still has a system prompt (from identity)
            assert len(agent.system_prompt) > 0

    def test_flow_simulating_server_get_agent(self):
        """Simulate what server.py get_agent() does."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup: create a pack in ~/.aither/agents/ mock location
            agents_dir = Path(tmpdir) / "agents"
            pack_dir = agents_dir / "myagent"
            pack_dir.mkdir(parents=True, exist_ok=True)

            # Create agent.yaml
            base_spec = {
                "system_prompt": "Custom system prompt",
                "name": "myagent",
            }
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(base_spec), encoding="utf-8")

            # Simulate server.py get_agent() logic
            identity = "myagent"
            agent_spec = {}

            if agent_yaml.exists():
                agent_spec = load_agent_spec(agent_yaml) or {}

            kwargs = {
                "name": identity,
                "identity": identity,
            }
            if agent_spec.get("system_prompt"):
                kwargs["system_prompt"] = agent_spec["system_prompt"]

            agent = AitherAgent(**kwargs)

            # Verify
            assert agent.name == "myagent"
            assert agent.system_prompt == "Custom system prompt"

    def test_flow_multiple_agents_different_prompts(self):
        """Test that multiple agents with different packs get different prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create agent1 pack
            agent1_dir = tmpdir / "agent1"
            agent1_dir.mkdir()
            spec1 = {"system_prompt": "I am agent one"}
            (agent1_dir / "agent.yaml").write_text(yaml.dump(spec1), encoding="utf-8")

            # Create agent2 pack
            agent2_dir = tmpdir / "agent2"
            agent2_dir.mkdir()
            spec2 = {"system_prompt": "I am agent two"}
            (agent2_dir / "agent.yaml").write_text(yaml.dump(spec2), encoding="utf-8")

            # Load both
            spec1_loaded = load_agent_spec(agent1_dir / "agent.yaml")
            spec2_loaded = load_agent_spec(agent2_dir / "agent.yaml")

            # Create agents
            agent1 = AitherAgent("agent1", system_prompt=spec1_loaded.get("system_prompt"))
            agent2 = AitherAgent("agent2", system_prompt=spec2_loaded.get("system_prompt"))

            # Verify each has correct prompt
            assert agent1.system_prompt == "I am agent one"
            assert agent2.system_prompt == "I am agent two"

    def test_flow_local_override_takes_precedence(self):
        """Test that local override is correctly prioritized in full flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # Base spec (shipped with pack)
            base = {
                "system_prompt": "Shipped prompt",
                "version": "1.0",
            }
            (pack_dir / "agent.yaml").write_text(yaml.dump(base), encoding="utf-8")

            # Local override (user customization)
            local = {
                "system_prompt": "My custom prompt",
            }
            (pack_dir / "agent.yaml.local").write_text(yaml.dump(local), encoding="utf-8")

            # Load merged spec
            merged = load_agent_spec(pack_dir / "agent.yaml")

            # Create agent
            agent = AitherAgent("test", system_prompt=merged.get("system_prompt"))

            # Verify user's override is used
            assert agent.system_prompt == "My custom prompt"
            # And other fields are preserved
            assert merged.get("version") == "1.0"
