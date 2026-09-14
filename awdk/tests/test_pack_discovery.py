"""Tests for pack discovery and agent spec loading."""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from adk.pack_discovery import (
    load_agent_spec,
    _deep_merge_dicts,
)


class TestDeepMergeDicts:
    """Test dictionary merging logic."""

    def test_merge_scalars_overlay_wins(self):
        """Test that overlay values replace base values for scalars."""
        base = {"name": "base", "version": 1}
        overlay = {"name": "overlay"}
        result = _deep_merge_dicts(base, overlay)
        assert result["name"] == "overlay"
        assert result["version"] == 1

    def test_merge_nested_dicts(self):
        """Test recursive merge of nested dictionaries."""
        base = {"config": {"a": 1, "b": 2}}
        overlay = {"config": {"b": 3, "c": 4}}
        result = _deep_merge_dicts(base, overlay)
        assert result["config"]["a"] == 1
        assert result["config"]["b"] == 3
        assert result["config"]["c"] == 4

    def test_merge_lists_replace_entirely(self):
        """Test that list values are replaced entirely (not merged)."""
        base = {"capabilities": ["a", "b", "c"]}
        overlay = {"capabilities": ["x", "y"]}
        result = _deep_merge_dicts(base, overlay)
        assert result["capabilities"] == ["x", "y"]

    def test_merge_empty_overlay(self):
        """Test that empty overlay doesn't modify base."""
        base = {"name": "test", "value": 42}
        overlay = {}
        result = _deep_merge_dicts(base, overlay)
        assert result == base

    def test_merge_adds_new_keys(self):
        """Test that new keys from overlay are added."""
        base = {"name": "test"}
        overlay = {"new_key": "new_value"}
        result = _deep_merge_dicts(base, overlay)
        assert result["name"] == "test"
        assert result["new_key"] == "new_value"


class TestLoadAgentSpec:
    """Test agent spec loading and merging."""

    def test_load_agent_spec_no_file(self):
        """Test that load_agent_spec returns {} when no file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = load_agent_spec(Path(tmpdir) / "nonexistent.yaml")
            assert spec == {}

    def test_load_agent_spec_base_yaml(self):
        """Test loading a base agent.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "agent.yaml"
            spec = {
                "system_prompt": "You are a helpful assistant",
                "capabilities": ["chat", "tools"],
                "enabled_domains": ["web", "code"],
            }
            yaml_path.write_text(yaml.dump(spec), encoding="utf-8")

            result = load_agent_spec(yaml_path)
            assert result["system_prompt"] == "You are a helpful assistant"
            assert result["capabilities"] == ["chat", "tools"]
            assert result["enabled_domains"] == ["web", "code"]

    def test_load_agent_spec_local_merge_scalar(self):
        """Test that local.yaml merges: scalars override."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {
                "system_prompt": "Original prompt",
                "name": "test-agent",
                "version": "1.0",
            }
            local_spec = {
                "system_prompt": "Custom prompt",
            }

            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text(yaml.dump(local_spec), encoding="utf-8")

            result = load_agent_spec(base_path)
            assert result["system_prompt"] == "Custom prompt"
            assert result["name"] == "test-agent"
            assert result["version"] == "1.0"

    def test_load_agent_spec_local_merge_list_replace(self):
        """Test that local.yaml merges: lists REPLACE (not append)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {
                "capabilities": ["chat", "tools", "web"],
            }
            local_spec = {
                "capabilities": ["code", "files"],
            }

            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text(yaml.dump(local_spec), encoding="utf-8")

            result = load_agent_spec(base_path)
            # List should be replaced entirely
            assert result["capabilities"] == ["code", "files"]

    def test_load_agent_spec_local_merge_nested(self):
        """Test deep merge of nested dictionaries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {
                "config": {
                    "temperature": 0.7,
                    "max_tokens": 2048,
                    "model": "default-model",
                }
            }
            local_spec = {
                "config": {
                    "temperature": 0.5,
                }
            }

            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text(yaml.dump(local_spec), encoding="utf-8")

            result = load_agent_spec(base_path)
            assert result["config"]["temperature"] == 0.5
            assert result["config"]["max_tokens"] == 2048
            assert result["config"]["model"] == "default-model"

    def test_load_agent_spec_invalid_base_yaml(self):
        """Test that invalid YAML in base is handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "agent.yaml"
            yaml_path.write_text("{ invalid yaml content ]", encoding="utf-8")

            result = load_agent_spec(yaml_path)
            assert result == {}

    def test_load_agent_spec_invalid_local_yaml(self):
        """Test that invalid local.yaml is handled, base is still returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {"system_prompt": "base prompt"}
            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text("{ invalid yaml ]", encoding="utf-8")

            result = load_agent_spec(base_path)
            # Should still return base since local is invalid
            assert result["system_prompt"] == "base prompt"

    def test_load_agent_spec_complex_merge(self):
        """Test complex scenario: base + local with multiple levels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {
                "name": "test-agent",
                "system_prompt": "You are helpful",
                "capabilities": ["chat", "tools", "web"],
                "config": {
                    "temperature": 0.7,
                    "top_p": 0.9,
                },
                "metadata": {
                    "version": "1.0",
                    "author": "system",
                },
            }

            local_spec = {
                "system_prompt": "You are extremely helpful",
                "capabilities": ["code"],  # Replace entire list
                "config": {
                    "temperature": 0.5,  # Override nested scalar
                },
                "metadata": {
                    "author": "user",  # Override nested scalar
                },
            }

            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text(yaml.dump(local_spec), encoding="utf-8")

            result = load_agent_spec(base_path)

            # Scalar override
            assert result["system_prompt"] == "You are extremely helpful"
            # List replacement
            assert result["capabilities"] == ["code"]
            # Nested scalar override
            assert result["config"]["temperature"] == 0.5
            # Nested scalar not overridden
            assert result["config"]["top_p"] == 0.9
            # Nested scalar override
            assert result["metadata"]["author"] == "user"
            # Nested scalar not overridden
            assert result["metadata"]["version"] == "1.0"
            # Top-level scalar not overridden
            assert result["name"] == "test-agent"

    def test_load_agent_spec_empty_files(self):
        """Test handling of empty YAML files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "agent.yaml"
            yaml_path.write_text("", encoding="utf-8")

            result = load_agent_spec(yaml_path)
            assert result == {}

    def test_load_agent_spec_local_only_adds_keys(self):
        """Test that local.yaml can add entirely new keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir) / "agent.yaml"
            local_path = Path(tmpdir) / "agent.yaml.local"

            base_spec = {"name": "test"}
            local_spec = {"custom_field": "custom_value", "enabled_domains": ["web"]}

            base_path.write_text(yaml.dump(base_spec), encoding="utf-8")
            local_path.write_text(yaml.dump(local_spec), encoding="utf-8")

            result = load_agent_spec(base_path)
            assert result["name"] == "test"
            assert result["custom_field"] == "custom_value"
            assert result["enabled_domains"] == ["web"]

    def test_load_agent_spec_system_prompt_multiline(self):
        """Test handling of multiline system prompts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "agent.yaml"
            multiline_prompt = "You are a helpful assistant.\nYou speak clearly.\nYou are concise."
            spec = {"system_prompt": multiline_prompt}

            yaml_path.write_text(yaml.dump(spec), encoding="utf-8")
            result = load_agent_spec(yaml_path)

            assert result["system_prompt"] == multiline_prompt
