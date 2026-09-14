"""Tests for agent construction with system prompt overrides."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from adk.agent import AitherAgent
from adk.config import Config
from adk.llm.base import LLMResponse


class TestAgentSystemPromptOverride:
    """Test system_prompt parameter override in agent construction."""

    def test_system_prompt_override_from_kwarg(self):
        """Test that system_prompt kwarg overrides default."""
        custom_prompt = "You are a custom assistant"
        agent = AitherAgent("test", system_prompt=custom_prompt)
        assert agent.system_prompt == custom_prompt

    def test_system_prompt_from_identity_default(self):
        """Test that system_prompt comes from identity if not provided."""
        agent = AitherAgent(identity="aither")
        # Should contain identity-based content
        assert len(agent.system_prompt) > 0
        assert "aither" in agent.system_prompt.lower() or "assistant" in agent.system_prompt.lower()

    def test_system_prompt_kwarg_overrides_identity(self):
        """Test that explicit system_prompt kwarg takes precedence over identity."""
        custom_prompt = "You are a completely different assistant"
        agent = AitherAgent(identity="aither", system_prompt=custom_prompt)
        assert agent.system_prompt == custom_prompt

    def test_system_prompt_none_uses_identity(self):
        """Test that system_prompt=None still uses identity default."""
        agent = AitherAgent(identity="aither", system_prompt=None)
        # Should still have a system prompt from identity
        assert len(agent.system_prompt) > 0

    def test_system_prompt_empty_string_uses_identity(self):
        """Test that empty string falls back to identity default."""
        agent = AitherAgent("test", system_prompt="")
        # Empty string should fall back to identity-based prompt
        # since empty prompts aren't useful
        assert len(agent.system_prompt) > 0

    def test_system_prompt_with_config(self):
        """Test that system_prompt works with custom config."""
        cfg = Config()
        cfg.llm_backend = "mock"
        custom_prompt = "Mock assistant"
        agent = AitherAgent("test", config=cfg, system_prompt=custom_prompt)
        assert agent.system_prompt == custom_prompt
        assert agent.config.llm_backend == "mock"

    def test_system_prompt_multiline(self):
        """Test multiline system prompts."""
        prompt = """You are a helpful assistant.
You follow the user's instructions carefully.
You provide clear and concise responses.
You ask for clarification when needed."""
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt
        assert prompt.count("\n") == 3

    def test_system_prompt_with_special_characters(self):
        """Test system prompts with special characters."""
        prompt = 'You are "helpful" & friendly! {Code: 42}'
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt

    def test_system_prompt_long_text(self):
        """Test handling of long system prompts."""
        prompt = "You are a helpful assistant. " * 500  # Very long prompt
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt
        assert len(agent.system_prompt) > 5000


class TestAgentConstructionFromPackSpec:
    """Test agent construction with system_prompt from pack spec."""

    def test_agent_from_pack_spec_with_system_prompt(self):
        """Test constructing agent from pack spec with system_prompt."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir) / "test_pack"
            pack_dir.mkdir(parents=True)

            spec = {
                "system_prompt": "Pack-specified system prompt",
                "name": "pack-agent",
                "capabilities": ["chat"],
            }

            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump(spec), encoding="utf-8")

            # Simulate loading spec and creating agent
            from adk.pack_discovery import load_agent_spec

            loaded_spec = load_agent_spec(agent_yaml)
            prompt = loaded_spec.get("system_prompt")

            # Create agent with the loaded prompt
            agent = AitherAgent(
                name="test",
                system_prompt=prompt,
            )

            assert agent.system_prompt == "Pack-specified system prompt"

    def test_agent_from_pack_spec_with_local_override(self):
        """Test agent construction with local override applied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir)

            # Create base spec
            base_spec = {
                "system_prompt": "Original pack prompt",
                "name": "myagent",
                "capabilities": ["chat", "tools"],
            }

            base_yaml = pack_dir / "agent.yaml"
            base_yaml.write_text(yaml.dump(base_spec), encoding="utf-8")

            # Create local override
            local_spec = {
                "system_prompt": "User-customized prompt",
            }

            local_yaml = pack_dir / "agent.yaml.local"
            local_yaml.write_text(yaml.dump(local_spec), encoding="utf-8")

            # Load merged spec
            from adk.pack_discovery import load_agent_spec

            merged_spec = load_agent_spec(base_yaml)

            # Create agent with merged prompt
            agent = AitherAgent(
                name="customized",
                system_prompt=merged_spec.get("system_prompt"),
            )

            # Should use the local override
            assert agent.system_prompt == "User-customized prompt"

    def test_agent_construction_preserves_other_fields(self):
        """Test that system_prompt doesn't interfere with other agent fields."""
        custom_prompt = "Custom prompt"
        agent = AitherAgent(
            name="myagent",
            identity="aither",
            system_prompt=custom_prompt,
        )

        assert agent.name == "myagent"
        assert agent.system_prompt == custom_prompt
        assert agent._identity is not None

    @pytest.mark.asyncio
    async def test_agent_with_prompt_can_chat(self):
        """Test that agent with custom prompt can still chat."""
        mock_llm = MagicMock()
        mock_llm.provider_name = "mock"
        mock_llm.chat = AsyncMock(return_value=LLMResponse(
            content="Test response",
            model="mock-model",
            tokens_used=10,
            latency_ms=50.0,
        ))

        custom_prompt = "You are a test assistant"
        agent = AitherAgent(
            "test",
            system_prompt=custom_prompt,
            llm=mock_llm,
        )

        # Verify the agent has the custom prompt
        assert agent.system_prompt == custom_prompt

        # Chat should work normally
        response = await agent.chat("Hello")
        assert response.content == "Test response"
        mock_llm.chat.assert_called_once()


class TestAgentConstructionEdgeCases:
    """Test edge cases in agent construction with system_prompt."""

    def test_system_prompt_with_name_collision(self):
        """Test that system_prompt doesn't collide with name parameter."""
        prompt = "You are helpful"
        agent = AitherAgent(
            name="collision-test",
            system_prompt=prompt,
        )
        assert agent.name == "collision-test"
        assert agent.system_prompt == prompt

    def test_system_prompt_unicode(self):
        """Test unicode in system prompts."""
        prompt = "You are helpful. 你好世界 🌍"
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt
        assert "世界" in agent.system_prompt
        assert "🌍" in agent.system_prompt

    def test_system_prompt_with_newlines_tabs(self):
        """Test system prompts with various whitespace."""
        prompt = "You are helpful.\n\tBe clear.\n\tBe concise."
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt
        assert "\t" in agent.system_prompt
        assert "\n" in agent.system_prompt

    def test_multiple_agents_different_prompts(self):
        """Test creating multiple agents with different prompts."""
        agent1 = AitherAgent("agent1", system_prompt="I am agent 1")
        agent2 = AitherAgent("agent2", system_prompt="I am agent 2")

        assert agent1.system_prompt == "I am agent 1"
        assert agent2.system_prompt == "I am agent 2"
        assert agent1.name == "agent1"
        assert agent2.name == "agent2"

    def test_system_prompt_with_code_block(self):
        """Test system prompt containing code blocks."""
        prompt = """You are a code assistant.

Example:
```python
def hello():
    return "world"
```

Always format code with triple backticks."""
        agent = AitherAgent("test", system_prompt=prompt)
        assert agent.system_prompt == prompt
        assert "```python" in agent.system_prompt
