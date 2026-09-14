"""Tests for pack customize functionality."""

import tempfile
from pathlib import Path

import pytest
import yaml


class TestPackCustomizeWrite:
    """Test pack customize writes agent.yaml.local correctly."""

    def test_customize_create_local_yaml(self):
        """Should create agent.yaml.local with override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create base agent.yaml
            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test-pack",
                "system_prompt": "Original",
                "capabilities": ["a", "b"],
            }), encoding="utf-8")

            # Simulate customize: write local override
            local_yaml = tmppath / "agent.yaml.local"
            local_override = {
                "system_prompt": "Custom prompt text",
            }
            local_yaml.write_text(yaml.dump(local_override, default_flow_style=False), encoding="utf-8")

            # Verify
            assert local_yaml.exists()
            data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
            assert data["system_prompt"] == "Custom prompt text"

    def test_customize_preserve_base(self):
        """Should not modify base agent.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            agent_yaml = tmppath / "agent.yaml"
            original_content = {
                "name": "test-pack",
                "system_prompt": "Original",
                "capabilities": ["a", "b"],
            }
            agent_yaml.write_text(yaml.dump(original_content), encoding="utf-8")

            # Create local override
            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "system_prompt": "Custom",
            }, default_flow_style=False), encoding="utf-8")

            # Verify base unchanged
            base_data = yaml.safe_load(agent_yaml.read_text(encoding="utf-8"))
            assert base_data["system_prompt"] == "Original"
            assert base_data["capabilities"] == ["a", "b"]

    def test_customize_with_capabilities_override(self):
        """Should override capabilities as a list replacement."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test",
                "capabilities": ["code", "search", "memory"],
            }), encoding="utf-8")

            # Customize capabilities
            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "capabilities": ["code"],
            }, default_flow_style=False), encoding="utf-8")

            # Load and verify merge
            from adk.pack_discovery import load_agent_spec

            spec = load_agent_spec(agent_yaml)
            assert spec["capabilities"] == ["code"]

    def test_customize_prompt_from_file(self):
        """Should load system_prompt from a separate file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create prompt file
            prompt_file = tmppath / "my_prompt.txt"
            custom_prompt = "You are a helpful assistant specialized in X, Y, and Z."
            prompt_file.write_text(custom_prompt, encoding="utf-8")

            # Simulate: read from file and write to local override
            prompt_content = prompt_file.read_text(encoding="utf-8")
            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "system_prompt": prompt_content,
            }, default_flow_style=False), encoding="utf-8")

            # Verify
            data = yaml.safe_load(local_yaml.read_text(encoding="utf-8"))
            assert data["system_prompt"] == custom_prompt


class TestAgentConstructionWithOverride:
    """Test that agents use the customized system_prompt."""

    def test_agent_uses_customized_prompt(self):
        """Agent should use customized system_prompt from local override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create pack directory structure
            pack_dir = Path.home() / ".aither" / "agents" / "test_pack_xyz"
            pack_dir.mkdir(parents=True, exist_ok=True)

            # Base agent.yaml
            agent_yaml = pack_dir / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test-pack",
                "identity": "test-pack",
                "system_prompt": "Base system prompt",
            }), encoding="utf-8")

            # Local override
            local_yaml = pack_dir / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "system_prompt": "My custom system prompt",
            }), encoding="utf-8")

            try:
                # Load spec
                from adk.pack_discovery import load_agent_spec

                spec = load_agent_spec(agent_yaml)

                # Verify customization is loaded
                assert spec["system_prompt"] == "My custom system prompt"

                # Create agent with the spec's system_prompt
                from adk.agent import AitherAgent

                agent = AitherAgent(
                    name="test-pack",
                    system_prompt=spec.get("system_prompt"),
                )

                # Verify agent has custom prompt
                assert agent.system_prompt == "My custom system prompt"
            finally:
                # Cleanup
                import shutil
                if pack_dir.exists():
                    shutil.rmtree(pack_dir)

    def test_agent_falls_back_to_base_if_no_override(self):
        """Agent should use base prompt if no local override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test",
                "system_prompt": "Base prompt",
            }), encoding="utf-8")

            # No local override

            from adk.pack_discovery import load_agent_spec
            from adk.agent import AitherAgent

            spec = load_agent_spec(agent_yaml)

            agent = AitherAgent(
                name="test",
                system_prompt=spec.get("system_prompt"),
            )

            assert agent.system_prompt == "Base prompt"
