"""Tests for pack_discovery.load_agent_spec and merging logic."""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from adk.pack_discovery import load_agent_spec, _deep_merge_dicts


class TestDeepMergeDicts:
    """Test the deep merge utility."""

    def test_merge_scalar_overlay_wins(self):
        """Overlay scalar values should replace base values."""
        base = {"name": "base", "version": "1.0", "nested": {"key": "base"}}
        overlay = {"name": "overlay", "nested": {"key": "overlay"}}
        result = _deep_merge_dicts(base, overlay)

        assert result["name"] == "overlay"
        assert result["version"] == "1.0"  # Unchanged
        assert result["nested"]["key"] == "overlay"

    def test_merge_list_overlay_replaces(self):
        """Overlay lists should replace entire base lists."""
        base = {"capabilities": ["a", "b", "c"], "other": "value"}
        overlay = {"capabilities": ["x", "y"]}
        result = _deep_merge_dicts(base, overlay)

        assert result["capabilities"] == ["x", "y"]
        assert result["other"] == "value"

    def test_merge_empty_overlay(self):
        """Empty overlay should not change base."""
        base = {"name": "test", "version": "1.0"}
        result = _deep_merge_dicts(base, {})

        assert result == base

    def test_merge_adds_new_keys(self):
        """Overlay should add new keys not in base."""
        base = {"name": "test"}
        overlay = {"version": "1.0", "description": "A test"}
        result = _deep_merge_dicts(base, overlay)

        assert result["name"] == "test"
        assert result["version"] == "1.0"
        assert result["description"] == "A test"


class TestLoadAgentSpec:
    """Test load_agent_spec with local overrides."""

    def test_load_no_spec(self):
        """Should load bundled agent.yaml if no explicit path given."""
        spec = load_agent_spec(None)
        # load_agent_spec(None) uses discover_agent_yaml which may find bundled specs
        assert isinstance(spec, dict)  # Should return a dict (may have content from bundled fallback)

    def test_load_base_only(self):
        """Should load base agent.yaml when no local override exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test-agent",
                "version": "1.0",
                "capabilities": ["read", "write"],
            }), encoding="utf-8")

            spec = load_agent_spec(agent_yaml)

            assert spec["name"] == "test-agent"
            assert spec["version"] == "1.0"
            assert spec["capabilities"] == ["read", "write"]

    def test_load_with_local_override(self):
        """Local override should replace scalar values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Base agent.yaml
            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test-agent",
                "version": "1.0",
                "system_prompt": "Original prompt",
                "capabilities": ["read", "write"],
            }), encoding="utf-8")

            # Local override
            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "system_prompt": "Custom prompt",
            }), encoding="utf-8")

            spec = load_agent_spec(agent_yaml)

            # Local override should win
            assert spec["system_prompt"] == "Custom prompt"
            # Other values unchanged
            assert spec["name"] == "test-agent"
            assert spec["version"] == "1.0"
            assert spec["capabilities"] == ["read", "write"]

    def test_load_with_local_capabilities_replace(self):
        """Local capabilities should completely replace base capabilities."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "test-agent",
                "capabilities": ["read", "write", "execute"],
            }), encoding="utf-8")

            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "capabilities": ["read"],
            }), encoding="utf-8")

            spec = load_agent_spec(agent_yaml)

            # Local capabilities should completely replace
            assert spec["capabilities"] == ["read"]

    def test_load_missing_yaml(self):
        """Should return empty dict if agent.yaml doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            agent_yaml = tmppath / "nonexistent.yaml"

            # Should return empty dict without raising
            spec = load_agent_spec(agent_yaml)
            assert spec == {}

    def test_load_complex_merge(self):
        """Test a realistic complex merge scenario."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            agent_yaml = tmppath / "agent.yaml"
            agent_yaml.write_text(yaml.dump({
                "name": "claude-code",
                "identity": "claude-code",
                "version": "1.2.3",
                "system_prompt": "You are Claude Code.",
                "capabilities": ["code", "search", "memory"],
                "enabled_domains": ["github", "gitlab"],
                "config": {"timeout": 30, "retries": 3},
            }), encoding="utf-8")

            local_yaml = tmppath / "agent.yaml.local"
            local_yaml.write_text(yaml.dump({
                "system_prompt": "You are my custom Claude Code assistant.",
                "capabilities": ["code", "search"],
                "config": {"timeout": 60},
            }), encoding="utf-8")

            spec = load_agent_spec(agent_yaml)

            # Verify merge
            assert spec["name"] == "claude-code"  # From base
            assert spec["version"] == "1.2.3"  # From base
            assert spec["system_prompt"] == "You are my custom Claude Code assistant."  # From local (override)
            assert spec["capabilities"] == ["code", "search"]  # From local (replace)
            assert spec["enabled_domains"] == ["github", "gitlab"]  # From base
            assert spec["config"]["timeout"] == 60  # From local (merge)
            assert spec["config"]["retries"] == 3  # From base
