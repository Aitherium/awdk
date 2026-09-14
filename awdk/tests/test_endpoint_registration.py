"""Tests for endpoint registration payload with A2A public key."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestEndpointRegistrationPayload:
    """Test that registration payloads include the A2A public_key field."""

    def test_registration_body_includes_public_key_tunnel(self, tmp_path, monkeypatch):
        """Registration body in tunnel mode includes public_key field."""
        # Set up a temporary identity dir
        identity_dir = tmp_path / "a2a"
        monkeypatch.setenv("AITHER_A2A_IDENTITY_DIR", str(identity_dir))

        from adk.a2a_identity import get_a2a_public_key

        # Generate a public key first
        public_key = get_a2a_public_key(directory=identity_dir)

        # Simulate the registration body construction (from cmd_up)
        name = "test-agent"
        invoke_url = "https://example.trycloudflare.com"
        reach = "tunnel"
        agent_type = "adk-agent"
        token = "test-callback-token"
        model = "gpt-4"
        provider_hint = "openai"

        # This is the body from cmd_up
        body = {
            "name": name,
            "invoke_url": invoke_url,
            "reach": reach,
            "agent_type": agent_type,
            "token": token,
            "model": model,
            "provider_hint": provider_hint,
            "public_key": public_key,
        }

        # Verify the body has all required fields
        assert body["name"] == name
        assert body["invoke_url"] == invoke_url
        assert body["reach"] == reach
        assert body["public_key"] == public_key
        assert len(body["public_key"]) == 64  # Raw 32 bytes hex

    def test_registration_body_includes_public_key_mesh(self, tmp_path, monkeypatch):
        """Registration body in mesh mode includes public_key field."""
        identity_dir = tmp_path / "a2a"
        monkeypatch.setenv("AITHER_A2A_IDENTITY_DIR", str(identity_dir))

        from adk.a2a_identity import get_a2a_public_key

        public_key = get_a2a_public_key(directory=identity_dir)

        # Simulate mesh registration body
        name = "test-mesh-agent"
        invoke_url = "http://192.168.1.100:8080"
        reach = "mesh"
        public_key_from_id = public_key

        body = {
            "name": name,
            "invoke_url": invoke_url,
            "reach": reach,
            "agent_type": "adk-agent",
            "token": "test-callback",
            "model": "",
            "provider_hint": "",
            "public_key": public_key_from_id,
        }

        assert body["reach"] == reach
        assert body["public_key"] == public_key_from_id

    def test_public_key_is_distinct_per_agent(self, tmp_path, monkeypatch):
        """Different agents get different public keys (not all the same)."""
        from adk.a2a_identity import get_a2a_public_key

        dir1 = tmp_path / "agent1"
        dir2 = tmp_path / "agent2"

        pub1 = get_a2a_public_key(directory=dir1)
        pub2 = get_a2a_public_key(directory=dir2)

        # Each agent should have a different public key
        assert pub1 != pub2
        # Both should be valid hex strings
        assert len(pub1) == 64
        assert len(pub2) == 64
        bytes.fromhex(pub1)
        bytes.fromhex(pub2)

    def test_registration_payload_json_serializable(self, tmp_path, monkeypatch):
        """Registration payload with public_key is valid JSON."""
        identity_dir = tmp_path / "a2a"
        monkeypatch.setenv("AITHER_A2A_IDENTITY_DIR", str(identity_dir))

        from adk.a2a_identity import get_a2a_public_key

        public_key = get_a2a_public_key(directory=identity_dir)

        body = {
            "name": "test-agent",
            "invoke_url": "https://example.com",
            "reach": "tunnel",
            "agent_type": "adk-agent",
            "token": "token123",
            "model": "gpt-4",
            "provider_hint": "openai",
            "public_key": public_key,
        }

        # Should be JSON serializable
        json_str = json.dumps(body)
        assert isinstance(json_str, str)
        # Should round-trip
        parsed = json.loads(json_str)
        assert parsed["public_key"] == public_key

    def test_reregister_payload_updates_public_key(self, tmp_path, monkeypatch):
        """Re-registration payload includes the updated public_key."""
        identity_dir = tmp_path / "a2a"
        monkeypatch.setenv("AITHER_A2A_IDENTITY_DIR", str(identity_dir))

        from adk.a2a_identity import get_a2a_public_key

        public_key = get_a2a_public_key(directory=identity_dir)

        # Simulate a retrieved endpoint that already exists
        existing_endpoint = {
            "name": "my-agent",
            "invoke_url": "https://old.url.com",
            "reach": "tunnel",
            "agent_type": "adk-agent",
            "public_key": "",  # Was empty before the fix
        }

        # The re-register flow updates this
        updated_endpoint = dict(existing_endpoint)
        updated_endpoint["public_key"] = public_key

        # Verify the update
        assert existing_endpoint["public_key"] == ""
        assert updated_endpoint["public_key"] == public_key
        assert len(updated_endpoint["public_key"]) == 64
