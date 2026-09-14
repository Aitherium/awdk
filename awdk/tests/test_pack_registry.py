"""Tests for PackRegistry — agent/tool pack storage and discovery."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adk.agent_pack import AgentPackManifest, RuntimeConfig
from adk.pack_registry import (
    PackRegistry,
    PackSummary,
    PublishReceipt,
    validate_pack,
)


@pytest.fixture
def agent_manifest() -> AgentPackManifest:
    """A valid AgentPackManifest for testing."""
    return AgentPackManifest(
        id="test-agent",
        name="Test Agent",
        version="1.0.0",
        framework="nooa",
        runtime=RuntimeConfig(type="python", cmd="python -m test_agent"),
        entrypoint="main.py",
        protocol="acp",
        skills=["skill1", "skill2"],
        entitlements=[],
        min_tier="free",
    )


@pytest.fixture
def registry(tmp_path) -> PackRegistry:
    """A PackRegistry in a temporary directory."""
    return PackRegistry(tmp_path)


# ── Validation Tests ─────────────────────────────────────────────────────


class TestValidatePack:
    """Tests for validate_pack()."""

    def test_valid_manifest(self, agent_manifest):
        """A valid manifest should have no errors."""
        errors = validate_pack(agent_manifest)
        assert errors == []

    def test_missing_id(self, agent_manifest):
        """Missing id should fail."""
        agent_manifest.id = ""
        errors = validate_pack(agent_manifest)
        assert any("id" in e for e in errors)

    def test_missing_name(self, agent_manifest):
        """Missing name should fail."""
        agent_manifest.name = ""
        errors = validate_pack(agent_manifest)
        assert any("name" in e for e in errors)

    def test_missing_version(self, agent_manifest):
        """Missing version should fail."""
        agent_manifest.version = ""
        errors = validate_pack(agent_manifest)
        assert any("version" in e for e in errors)

    def test_invalid_version(self, agent_manifest):
        """Unparseable version should fail."""
        agent_manifest.version = "not-a-version"
        errors = validate_pack(agent_manifest)
        assert any("version" in e and "semver" in e for e in errors)

    def test_invalid_framework(self, agent_manifest):
        """Unknown framework should fail."""
        agent_manifest.framework = "unknown-framework"  # type: ignore
        errors = validate_pack(agent_manifest)
        assert any("framework" in e for e in errors)

    def test_invalid_protocol(self, agent_manifest):
        """Unknown protocol should fail."""
        agent_manifest.protocol = "unknown-protocol"  # type: ignore
        errors = validate_pack(agent_manifest)
        assert any("protocol" in e for e in errors)

    def test_missing_entrypoint(self, agent_manifest):
        """Missing entrypoint should fail."""
        agent_manifest.entrypoint = ""
        errors = validate_pack(agent_manifest)
        assert any("entrypoint" in e for e in errors)

    def test_invalid_entitlements(self, agent_manifest):
        """Non-list entitlements should fail."""
        agent_manifest.entitlements = "not-a-list"  # type: ignore
        errors = validate_pack(agent_manifest)
        assert any("entitlements" in e for e in errors)

    def test_invalid_min_tier(self, agent_manifest):
        """Unknown min_tier should fail."""
        agent_manifest.min_tier = "unknown-tier"
        errors = validate_pack(agent_manifest)
        assert any("min_tier" in e for e in errors)

    def test_invalid_id_characters(self, agent_manifest):
        """IDs with invalid characters should fail."""
        agent_manifest.id = "Test-Agent"  # uppercase not allowed
        errors = validate_pack(agent_manifest)
        assert any("id" in e for e in errors)


# ── Publishing Tests ─────────────────────────────────────────────────────


class TestPublish:
    """Tests for publish()."""

    def test_publish_manifest_object(self, registry, agent_manifest):
        """Publishing a manifest object should work."""
        receipt = registry.publish(agent_manifest)
        assert receipt.id == "test-agent"
        assert receipt.version == "1.0.0"
        assert len(receipt.digest) == 64  # sha256 hex
        assert receipt.remote_ok is True

    def test_publish_creates_files(self, registry, agent_manifest):
        """Publishing should create manifest.json and digest.txt."""
        registry.publish(agent_manifest)
        manifest_path = registry._manifest_path("test-agent", "1.0.0")
        digest_path = registry._digest_path("test-agent", "1.0.0")
        assert manifest_path.is_file()
        assert digest_path.is_file()

    def test_publish_yaml_file(self, registry, agent_manifest, tmp_path):
        """Publishing from a YAML file should work."""
        yaml_file = tmp_path / "agent.yaml"
        yaml_content = """
id: test-agent
name: Test Agent
version: 1.0.0
framework: nooa
protocol: acp
entrypoint: main.py
runtime:
  type: python
  cmd: python -m test_agent
skills:
  - skill1
  - skill2
"""
        yaml_file.write_text(yaml_content)
        receipt = registry.publish(yaml_file)
        assert receipt.id == "test-agent"
        assert receipt.version == "1.0.0"

    def test_publish_validation_fails(self, registry, agent_manifest):
        """Publishing an invalid manifest should fail and not write files."""
        agent_manifest.version = "invalid"
        with pytest.raises(ValueError, match="validation failed"):
            registry.publish(agent_manifest)
        manifest_path = registry._manifest_path("test-agent", "invalid")
        assert not manifest_path.exists()

    def test_publish_duplicate_without_force(self, registry, agent_manifest):
        """Publishing the same (id, version) twice should fail."""
        registry.publish(agent_manifest)
        with pytest.raises(ValueError, match="already published"):
            registry.publish(agent_manifest)

    def test_publish_duplicate_with_force(self, registry, agent_manifest):
        """Publishing the same (id, version) with force=True should succeed."""
        receipt1 = registry.publish(agent_manifest)
        agent_manifest.name = "Updated Agent"
        receipt2 = registry.publish(agent_manifest, force=True)
        assert receipt1.digest != receipt2.digest  # digest should change

    def test_publish_digest_stability(self, registry, agent_manifest):
        """The digest should be deterministic for the same manifest."""
        receipt1 = registry.publish(agent_manifest)
        manifest_path = registry._manifest_path("test-agent", "1.0.0")
        manifest_path.unlink()

        receipt2 = registry.publish(agent_manifest, force=True)
        assert receipt1.digest == receipt2.digest

    def test_publish_digest_changes(self, registry, agent_manifest):
        """The digest should change when the manifest changes."""
        receipt1 = registry.publish(agent_manifest)
        agent_manifest.name = "Different Name"
        receipt2 = registry.publish(agent_manifest, force=True)
        assert receipt1.digest != receipt2.digest

    def test_publish_with_publisher_callback_success(
        self, registry, agent_manifest
    ):
        """A successful publisher callback should set remote_ok=True."""
        publisher = MagicMock()
        receipt = registry.publish(agent_manifest, publisher=publisher)
        assert receipt.remote_ok is True
        publisher.assert_called_once()
        call_arg = publisher.call_args[0][0]
        assert isinstance(call_arg, AgentPackManifest)
        assert call_arg.id == "test-agent"

    def test_publish_with_publisher_callback_failure(
        self, registry, agent_manifest
    ):
        """A failing publisher callback should set remote_ok=False but not fail
        the local publish."""
        def failing_publisher(manifest):
            raise RuntimeError("Remote error")

        receipt = registry.publish(agent_manifest, publisher=failing_publisher)
        # Local publish should succeed
        assert (
            registry._manifest_path("test-agent", "1.0.0").is_file()
        )
        # But remote_ok should be False
        assert receipt.remote_ok is False
        assert "Remote error" in receipt.remote_error

    def test_publish_nonexistent_yaml_file(self, registry):
        """Publishing a nonexistent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            registry.publish(Path("/nonexistent/file.yaml"))


# ── Browsing Tests ───────────────────────────────────────────────────────


class TestBrowse:
    """Tests for browse()."""

    def test_browse_empty_registry(self, registry):
        """Browsing an empty registry should return an empty list."""
        summaries = registry.browse()
        assert summaries == []

    def test_browse_single_pack(self, registry, agent_manifest):
        """Browsing after publishing should return the pack."""
        registry.publish(agent_manifest)
        summaries = registry.browse()
        assert len(summaries) == 1
        assert summaries[0].id == "test-agent"
        assert summaries[0].name == "Test Agent"
        assert summaries[0].version == "1.0.0"

    def test_browse_multiple_versions_returns_latest(self, registry, agent_manifest):
        """browse() should return only the latest version per pack."""
        registry.publish(agent_manifest)
        agent_manifest.version = "2.0.0"
        registry.publish(agent_manifest)
        summaries = registry.browse()
        assert len(summaries) == 1
        assert summaries[0].version == "2.0.0"

    def test_browse_filter_by_query(self, registry, agent_manifest):
        """browse(query) should filter by pack id or name."""
        registry.publish(agent_manifest)
        summaries = registry.browse(query="test")
        assert len(summaries) == 1

        summaries = registry.browse(query="nonexistent")
        assert len(summaries) == 0

    def test_browse_filter_by_framework(self, registry, agent_manifest):
        """browse(framework) should filter by framework."""
        registry.publish(agent_manifest)
        summaries = registry.browse(framework="nooa")
        assert len(summaries) == 1

        summaries = registry.browse(framework="hermes")
        assert len(summaries) == 0

    def test_browse_excludes_yanked(self, registry, agent_manifest):
        """browse() should exclude yanked versions."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")
        summaries = registry.browse()
        assert len(summaries) == 0


# ── Versions Tests ───────────────────────────────────────────────────────


class TestVersions:
    """Tests for versions()."""

    def test_versions_nonexistent_pack(self, registry):
        """versions() for a nonexistent pack should return empty list."""
        versions = registry.versions("nonexistent")
        assert versions == []

    def test_versions_single_version(self, registry, agent_manifest):
        """versions() should return a single version."""
        registry.publish(agent_manifest)
        versions = registry.versions("test-agent")
        assert versions == ["1.0.0"]

    def test_versions_semver_sort(self, registry, agent_manifest):
        """versions() should sort by semver (descending), not string order."""
        for ver in ["0.1.0", "0.2.0", "0.10.0"]:
            agent_manifest.version = ver
            registry.publish(agent_manifest, force=True)

        versions = registry.versions("test-agent")
        assert versions == ["0.10.0", "0.2.0", "0.1.0"]

    def test_versions_excludes_yanked(self, registry, agent_manifest):
        """versions() should exclude yanked versions."""
        for ver in ["1.0.0", "2.0.0", "3.0.0"]:
            agent_manifest.version = ver
            registry.publish(agent_manifest, force=True)

        registry.yank("test-agent", "2.0.0")
        versions = registry.versions("test-agent")
        assert versions == ["3.0.0", "1.0.0"]


# ── Get Tests ────────────────────────────────────────────────────────────


class TestGet:
    """Tests for get()."""

    def test_get_specific_version(self, registry, agent_manifest):
        """get(pack_id, version) should return the manifest."""
        registry.publish(agent_manifest)
        fetched = registry.get("test-agent", "1.0.0")
        assert fetched.id == "test-agent"
        assert fetched.name == "Test Agent"
        assert fetched.version == "1.0.0"

    def test_get_latest_version(self, registry, agent_manifest):
        """get(pack_id) with no version should return the latest."""
        for ver in ["1.0.0", "2.0.0", "3.0.0"]:
            agent_manifest.version = ver
            registry.publish(agent_manifest, force=True)

        fetched = registry.get("test-agent")
        assert fetched.version == "3.0.0"

    def test_get_nonexistent_pack(self, registry):
        """get() for a nonexistent pack should raise KeyError."""
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_get_nonexistent_version(self, registry, agent_manifest):
        """get(pack_id, version) for a nonexistent version should raise KeyError."""
        registry.publish(agent_manifest)
        with pytest.raises(KeyError):
            registry.get("test-agent", "999.0.0")

    def test_get_yanked_version(self, registry, agent_manifest):
        """get() should not return yanked versions."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")
        with pytest.raises(KeyError):
            registry.get("test-agent", "1.0.0")


# ── Yank Tests ───────────────────────────────────────────────────────────


class TestYank:
    """Tests for yank()."""

    def test_yank_creates_marker(self, registry, agent_manifest):
        """yank() should create a .yanked marker file."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")
        yanked_path = registry._yanked_marker_path("test-agent", "1.0.0")
        assert yanked_path.is_file()

    def test_yank_hides_from_browse(self, registry, agent_manifest):
        """Yanked versions should be hidden from browse()."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")
        summaries = registry.browse()
        assert len(summaries) == 0

    def test_yank_hides_from_versions(self, registry, agent_manifest):
        """Yanked versions should be hidden from versions()."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")
        versions = registry.versions("test-agent")
        assert len(versions) == 0

    def test_yank_nonexistent_pack(self, registry):
        """yank() for a nonexistent pack should raise KeyError."""
        with pytest.raises(KeyError):
            registry.yank("nonexistent", "1.0.0")


# ── Digest Verification Tests ────────────────────────────────────────────


class TestVerifyDigest:
    """Tests for verify_digest()."""

    def test_verify_digest_valid(self, registry, agent_manifest):
        """verify_digest() should return True for a valid digest."""
        registry.publish(agent_manifest)
        valid = registry.verify_digest("test-agent", "1.0.0")
        assert valid is True

    def test_verify_digest_corrupted(self, registry, agent_manifest):
        """verify_digest() should return False if the manifest is corrupted."""
        registry.publish(agent_manifest)
        manifest_path = registry._manifest_path("test-agent", "1.0.0")
        manifest_path.write_text('{"corrupted": "data"}')
        valid = registry.verify_digest("test-agent", "1.0.0")
        assert valid is False

    def test_verify_digest_nonexistent_manifest(self, registry):
        """verify_digest() should raise KeyError if manifest is missing."""
        with pytest.raises(KeyError):
            registry.verify_digest("nonexistent", "1.0.0")

    def test_verify_digest_nonexistent_digest_file(self, registry, agent_manifest):
        """verify_digest() should raise KeyError if digest.txt is missing."""
        registry.publish(agent_manifest)
        digest_path = registry._digest_path("test-agent", "1.0.0")
        digest_path.unlink()
        with pytest.raises(KeyError):
            registry.verify_digest("test-agent", "1.0.0")


# ── Integration Tests ────────────────────────────────────────────────────


class TestIntegration:
    """Integration tests for the full publish → browse → get workflow."""

    def test_publish_browse_get_workflow(self, registry, agent_manifest):
        """Full workflow: publish, browse, and get."""
        # Publish
        receipt = registry.publish(agent_manifest)
        assert receipt.id == "test-agent"

        # Browse
        summaries = registry.browse()
        assert len(summaries) == 1
        assert summaries[0].id == "test-agent"
        assert summaries[0].version == "1.0.0"

        # Get
        fetched = registry.get("test-agent", "1.0.0")
        assert fetched.name == "Test Agent"
        assert fetched.framework == "nooa"

    def test_multiple_packs(self, registry, agent_manifest):
        """Registry should handle multiple packs."""
        registry.publish(agent_manifest)

        agent_manifest.id = "another-agent"
        agent_manifest.name = "Another Agent"
        registry.publish(agent_manifest)

        summaries = registry.browse()
        assert len(summaries) == 2
        ids = {s.id for s in summaries}
        assert ids == {"test-agent", "another-agent"}

    def test_version_progression(self, registry, agent_manifest):
        """Registry should handle version progression correctly."""
        for ver in ["0.1.0", "0.2.0", "1.0.0", "2.0.0"]:
            agent_manifest.version = ver
            registry.publish(agent_manifest, force=True)

        # browse() should return only latest
        summaries = registry.browse()
        assert len(summaries) == 1
        assert summaries[0].version == "2.0.0"

        # versions() should return all in semver order
        versions = registry.versions("test-agent")
        assert versions == ["2.0.0", "1.0.0", "0.2.0", "0.1.0"]

    def test_yank_then_republish(self, registry, agent_manifest):
        """Yanking and then republishing should work."""
        registry.publish(agent_manifest)
        registry.yank("test-agent", "1.0.0")

        # Should be hidden
        summaries = registry.browse()
        assert len(summaries) == 0

        # Republish with force
        agent_manifest.name = "Republished Agent"
        registry.publish(agent_manifest, force=True)

        # Should be visible again
        summaries = registry.browse()
        assert len(summaries) == 1
        assert summaries[0].name == "Republished Agent"

    def test_receipt_includes_timestamp(self, registry, agent_manifest):
        """PublishReceipt should include published_at timestamp."""
        receipt = registry.publish(agent_manifest)
        assert receipt.published_at
        assert "T" in receipt.published_at  # ISO 8601 format
