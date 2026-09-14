"""Tests for adk.mesh_provider — AitherNet community inference provider setup.

Tests cover:
- Full happy path: advertise + consent + trust already granted
- Awaiting trust: advertise + consent, but operator hasn't promoted yet (trust 0)
- Advertise failure: peer doesn't exist or endpoint is unreachable
- Already-onboarded reuse: peer_id resolved from config, no re-onboard

All tests are mocked (no network, no containers). Every test must:
  1. Use fail-closed authz: no unguarded allow paths
  2. Never verify=False (use mocked TLS)
  3. Never trust caller-supplied input for authz decisions
  4. Test positive paths (confirm data actually returned, not silent no-op)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from pathlib import Path

import pytest

from adk.mesh_provider import (
    advertise,
    grant_consent,
    poll_trust_tier,
    provide,
    _resolve_peer_id,
    _get_auth_token,
)


class TestResolvePeerId:
    """Test peer_id resolution from env, config, or overlay IP."""

    @pytest.mark.asyncio
    async def test_resolve_peer_id_from_env(self):
        """AITHER_PEER_ID env var wins."""
        with patch.dict("os.environ", {"AITHER_PEER_ID": "test_peer_123"}):
            result = await _resolve_peer_id()
            assert result == "test_peer_123"

    @pytest.mark.asyncio
    async def test_resolve_peer_id_overlay_ip_without_peer_id_raises(self):
        """Overlay IP alone (no env/node_auth peer_id) must RAISE (fail-closed)."""
        with patch.dict(
            "os.environ",
            {
                "AITHER_PEER_ID": "",
                "AITHER_MESH_OVERLAY_IP": "10.77.0.39"
            },
            clear=False,
        ):
            with patch("pathlib.Path.home") as mock_home:
                # Simulate no ~/.aither/node_auth.json
                mock_path = MagicMock()
                mock_path.__truediv__.return_value.exists.return_value = False
                mock_home.return_value = mock_path

                with pytest.raises(RuntimeError, match="Cannot resolve peer_id"):
                    await _resolve_peer_id()

    @pytest.mark.asyncio
    async def test_resolve_peer_id_missing_raises(self):
        """RuntimeError when peer_id cannot be resolved."""
        with patch.dict(
            "os.environ",
            {
                "AITHER_PEER_ID": "",
                "AITHER_MESH_OVERLAY_IP": "",
            },
            clear=False,
        ):
            with pytest.raises(RuntimeError, match="Cannot resolve peer_id"):
                await _resolve_peer_id()

    @pytest.mark.asyncio
    async def test_resolve_peer_id_never_fabricates_overlay_format(self):
        """Resolved peer_id never matches the fabricated peer_<overlay>_ip format."""
        with patch.dict("os.environ", {"AITHER_PEER_ID": "peer-abcdef123456"}):
            result = await _resolve_peer_id()
            # Assert the resolved ID does NOT match the bad fabricated format
            assert not result.startswith("peer_")
            # Assert it looks like a real peer ID (peer- prefix)
            assert result.startswith("peer-")


class TestGetAuthToken:
    """Test auth token resolution from env or config."""

    def test_get_auth_token_from_env(self):
        """AITHER_AUTH_TOKEN env var wins."""
        with patch.dict("os.environ", {"AITHER_AUTH_TOKEN": "token_from_env"}):
            result = _get_auth_token()
            assert result == "token_from_env"

    def test_get_auth_token_missing_returns_none(self):
        """Returns None when token not available."""
        with patch.dict("os.environ", {"AITHER_AUTH_TOKEN": ""}, clear=False):
            with patch("pathlib.Path.home") as mock_home:
                mock_home.side_effect = RuntimeError("No home")
                result = _get_auth_token()
                assert result is None


class TestAdvertise:
    """Test advertise step: register inference endpoint on peer record."""

    @pytest.mark.asyncio
    async def test_advertise_success(self):
        """Successful advertise returns ok=True with endpoint details."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {}

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await advertise(
                    peer_id="peer_10_77_0_39",
                    inference_url="http://10.77.0.39:8000/v1",
                    inference_model="gemma4-12b",
                )

                assert result["ok"] is True
                assert result["step"] == "advertise"
                assert result["peer_id"] == "peer_10_77_0_39"
                assert result["inference_url"] == "http://10.77.0.39:8000/v1"
                assert result["inference_model"] == "gemma4-12b"

                # Verify POST was called with correct payload
                mock_client.post.assert_called_once()
                call_args = mock_client.post.call_args
                assert "peers/inference" in call_args[0][0]
                assert call_args[1]["json"]["peer_id"] == "peer_10_77_0_39"

    @pytest.mark.asyncio
    async def test_advertise_missing_peer_id_fails_closed(self):
        """Empty peer_id returns error (fail-closed)."""
        result = await advertise(
            peer_id="",
            inference_url="http://10.77.0.39:8000/v1",
            inference_model="gemma4-12b",
        )

        assert result["ok"] is False
        assert result["error"] == "peer_id required"

    @pytest.mark.asyncio
    async def test_advertise_missing_inference_url_fails_closed(self):
        """Empty inference_url returns error (fail-closed)."""
        result = await advertise(
            peer_id="peer_123",
            inference_url="",
            inference_model="gemma4-12b",
        )

        assert result["ok"] is False
        assert result["error"] == "inference_url and inference_model required"

    @pytest.mark.asyncio
    async def test_advertise_404_peer_not_found(self):
        """404 response (peer doesn't exist) returns error."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 404
                mock_response.text = "peer not found"
                mock_response.raise_for_status.side_effect = (
                    __import__("httpx").HTTPStatusError(
                        "404", request=None, response=mock_response
                    )
                )

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client.post.side_effect = lambda *a, **k: (_ for _ in ()).throw(
                    __import__("httpx").HTTPStatusError(
                        "404", request=None, response=mock_response
                    )
                )
                mock_client_class.return_value = mock_client

                result = await advertise(
                    peer_id="peer_123",
                    inference_url="http://10.77.0.39:8000/v1",
                    inference_model="gemma4-12b",
                )

                assert result["ok"] is False
                assert "404" in result["error"]


class TestGrantConsent:
    """Test consent step: grant participation permission."""

    @pytest.mark.asyncio
    async def test_consent_success(self):
        """Successful consent returns ok=True."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {}

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await grant_consent(
                    peer_id="peer_10_77_0_39",
                    tenant_id="acme-corp",
                    auth_token="test_token_123",
                )

                assert result["ok"] is True
                assert result["step"] == "consent"
                assert result["peer_id"] == "peer_10_77_0_39"
                assert result["tenant_id"] == "acme-corp"

                # Verify POST was called with correct payload
                mock_client.post.assert_called_once()
                call_args = mock_client.post.call_args
                assert f"peers/peer_10_77_0_39/consent" in call_args[0][0]
                assert call_args[1]["json"]["granted"] is True
                assert call_args[1]["json"]["tenant_id"] == "acme-corp"

    @pytest.mark.asyncio
    async def test_consent_missing_tenant_id_fails_closed(self):
        """Empty tenant_id returns error (fail-closed authz)."""
        result = await grant_consent(
            peer_id="peer_123",
            tenant_id="",
        )

        assert result["ok"] is False
        assert result["error"] == "tenant_id required (your authenticated owner identity)"

    @pytest.mark.asyncio
    async def test_consent_missing_peer_id_fails_closed(self):
        """Empty peer_id returns error (fail-closed)."""
        result = await grant_consent(
            peer_id="",
            tenant_id="acme-corp",
            auth_token="test_token",
        )

        assert result["ok"] is False
        assert result["error"] == "peer_id required"

    @pytest.mark.asyncio
    async def test_consent_missing_auth_token_fails_closed(self):
        """Empty auth_token returns error (fail-closed authz)."""
        result = await grant_consent(
            peer_id="peer_123",
            tenant_id="acme-corp",
            auth_token="",
        )

        assert result["ok"] is False
        assert "auth_token required" in result["error"]
        assert result["step"] == "consent"


class TestPollTrustTier:
    """Test trust polling: wait for operator promotion."""

    @pytest.mark.asyncio
    async def test_poll_trust_tier_granted_immediately(self):
        """When trust_level >= 1, returns immediately with ok=True."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"trust_level": 1}

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await poll_trust_tier(
                    peer_id="peer_10_77_0_39",
                    max_wait_seconds=10,
                )

                assert result["ok"] is True
                assert result["step"] == "poll_trust"
                assert result["trust_level"] == 1

    @pytest.mark.asyncio
    async def test_poll_trust_tier_timeout_awaiting_operator(self):
        """Timeout (trust still 0) returns ok=False with awaiting message."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"trust_level": 0}

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await poll_trust_tier(
                    peer_id="peer_10_77_0_39",
                    max_wait_seconds=1,  # Very short timeout
                )

                assert result["ok"] is False
                assert "operator" in result.get("message", "").lower()
                assert "timed out" in result.get("error", "").lower()


class TestProvideFullFlow:
    """Test the full provide flow: advertise → consent → poll trust."""

    @pytest.mark.asyncio
    async def test_provide_happy_path_trust_granted(self):
        """Full happy path: advertise + consent + trust already granted."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("adk.mesh_provider._resolve_peer_id") as mock_resolve_peer:
                mock_resolve_peer.return_value = "peer_10_77_0_39"

                with patch("adk.mesh_provider._get_auth_token") as mock_get_token:
                    mock_get_token.return_value = "token123"

                    with patch("httpx.AsyncClient") as mock_client_class:
                        # Mock responses: advertise, consent, poll
                        mock_ad_response = MagicMock()
                        mock_ad_response.status_code = 200

                        mock_consent_response = MagicMock()
                        mock_consent_response.status_code = 200

                        mock_poll_response = MagicMock()
                        mock_poll_response.status_code = 200
                        mock_poll_response.json.return_value = {"trust_level": 1}

                        mock_trust_req_response = MagicMock()
                        mock_trust_req_response.status_code = 200
                        mock_trust_req_response.json.return_value = {
                            "status": "auto_granted", "auto_tier_eligible": True,
                        }
                        mock_trust_req_response.raise_for_status = MagicMock()

                        # Self-service pool entry (2026-07-23): provide() now
                        # completes the loop with join-pool — no operator step.
                        mock_join_response = MagicMock()
                        mock_join_response.status_code = 200
                        mock_join_response.json.return_value = {
                            "joined": True,
                            "backend_name": "community_peer_10_77_0_39",
                        }
                        mock_join_response.raise_for_status = MagicMock()

                        mock_client = AsyncMock()
                        mock_client.__aenter__.return_value = mock_client
                        mock_client.__aexit__.return_value = None

                        # Set up post and get responses
                        responses = [
                            mock_ad_response,
                            mock_consent_response,
                            mock_trust_req_response,
                            mock_join_response,
                        ]
                        mock_client.post = AsyncMock(side_effect=responses)
                        mock_client.get = AsyncMock(return_value=mock_poll_response)
                        mock_client_class.return_value = mock_client

                        result = await provide(
                            inference_url="http://10.77.0.39:8000/v1",
                            inference_model="gemma4-12b",
                            tenant_id="acme-corp",
                        )

                        assert result["ok"] is True
                        assert result["trust_granted"] is True
                        assert result["joined_pool"] is True
                        assert result["backend_name"] == "community_peer_10_77_0_39"
                        assert all(step["ok"] for step in result["steps"][:2])

    @pytest.mark.asyncio
    async def test_provide_awaiting_operator_trust(self):
        """Advertise + consent succeed, but operator hasn't promoted yet."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("adk.mesh_provider._resolve_peer_id") as mock_resolve_peer:
                mock_resolve_peer.return_value = "peer_10_77_0_39"

                with patch("adk.mesh_provider._get_auth_token") as mock_get_token:
                    mock_get_token.return_value = "token123"

                    with patch("httpx.AsyncClient") as mock_client_class:
                        # Mock responses: advertise, consent OK; poll returns trust_tier 0
                        mock_ad_response = MagicMock()
                        mock_ad_response.status_code = 200

                        mock_consent_response = MagicMock()
                        mock_consent_response.status_code = 200

                        mock_poll_response = MagicMock()
                        mock_poll_response.status_code = 200
                        mock_poll_response.json.return_value = {"trust_level": 0}

                        mock_client = AsyncMock()
                        mock_client.__aenter__.return_value = mock_client
                        mock_client.__aexit__.return_value = None

                        # Set up post and get responses
                        responses = [mock_ad_response, mock_consent_response]
                        mock_client.post = AsyncMock(side_effect=responses)
                        mock_client.get = AsyncMock(return_value=mock_poll_response)
                        mock_client_class.return_value = mock_client

                        result = await provide(
                            inference_url="http://10.77.0.39:8000/v1",
                            inference_model="gemma4-12b",
                            tenant_id="acme-corp",
                            wait_seconds=1,
                        )

                        assert result["ok"] is False
                        assert result["awaiting_operator"] is True
                        assert result["trust_granted"] is False
                        assert "next_action" in result
                        assert "operator" in result["next_action"].lower()

    @pytest.mark.asyncio
    async def test_provide_advertise_fails(self):
        """Advertise fails → provide returns error immediately."""
        with patch("adk.mesh_provider._tls_verify", return_value=True):
            with patch("adk.mesh_provider._resolve_peer_id") as mock_resolve_peer:
                mock_resolve_peer.return_value = "peer_123"

                with patch("adk.mesh_provider._get_auth_token") as mock_get_token:
                    mock_get_token.return_value = "token123"

                    with patch("httpx.AsyncClient") as mock_client_class:
                        # Mock advertise failure (404 peer not found)
                        mock_response = MagicMock()
                        mock_response.status_code = 404
                        mock_response.text = "peer not found"

                        def raise_http_error(*a, **k):
                            error = __import__("httpx").HTTPStatusError(
                                "404", request=MagicMock(), response=mock_response
                            )
                            raise error

                        mock_client = AsyncMock()
                        mock_client.__aenter__.return_value = mock_client
                        mock_client.__aexit__.return_value = None
                        mock_client.post = AsyncMock(side_effect=raise_http_error)
                        mock_client_class.return_value = mock_client

                        result = await provide(
                            inference_url="http://10.77.0.39:8000/v1",
                            inference_model="gemma4-12b",
                            tenant_id="acme-corp",
                        )

                        assert result["ok"] is False
                        assert len(result["steps"]) == 1
                        assert result["steps"][0]["ok"] is False
                        assert "404" in result["steps"][0]["error"]

    @pytest.mark.asyncio
    async def test_provide_missing_tenant_id_fails_closed(self):
        """Missing tenant_id returns error immediately (fail-closed authz)."""
        with patch("adk.mesh_provider._resolve_peer_id") as mock_resolve_peer:
            mock_resolve_peer.return_value = "peer_123"

            with patch("adk.mesh_provider._get_auth_token") as mock_get_token:
                mock_get_token.return_value = "token123"

                result = await provide(
                    inference_url="http://10.77.0.39:8000/v1",
                    inference_model="gemma4-12b",
                    tenant_id="",  # Empty: fail-closed
                )

                assert result["ok"] is False
                assert "tenant_id" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_provide_missing_auth_token_fails_closed(self):
        """Missing auth_token returns error immediately (fail-closed authz)."""
        with patch("adk.mesh_provider._resolve_peer_id") as mock_resolve_peer:
            mock_resolve_peer.return_value = "peer_123"

            with patch("adk.mesh_provider._get_auth_token") as mock_get_token:
                mock_get_token.return_value = None  # No token available

                result = await provide(
                    inference_url="http://10.77.0.39:8000/v1",
                    inference_model="gemma4-12b",
                    tenant_id="acme-corp",
                    auth_token=None,  # Explicit None
                )

                assert result["ok"] is False
                assert "auth_token required" in result["error"].lower()
                assert result["step"] == "init"


class TestFederationToken:
    """Self-service relay-federation token mint."""

    @pytest.mark.asyncio
    async def test_federation_token_success_returns_key_once(self):
        from adk.mesh_provider import federation_token

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "peer_id": "node-abc",
                "node_token": "aither_sk_live_x",
                "node_slug": "node-abc",
                "expires_in_days": 30,
            }
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await federation_token("node-abc", auth_token="tok")

            assert result["ok"] is True
            assert result["node_token"] == "aither_sk_live_x"
            assert result["node_slug"] == "node-abc"
            # Positive-path assertion: the endpoint actually got called
            called_url = mock_client.post.call_args[0][0]
            assert called_url.endswith("/v1/mesh/peers/node-abc/federation-token")

    @pytest.mark.asyncio
    async def test_federation_token_missing_auth_fails_closed(self):
        from adk.mesh_provider import federation_token

        result = await federation_token("node-abc", auth_token=None)
        assert result["ok"] is False
        assert "auth_token required" in result["error"]

    @pytest.mark.asyncio
    async def test_federation_token_missing_peer_fails_closed(self):
        from adk.mesh_provider import federation_token

        result = await federation_token("", auth_token="tok")
        assert result["ok"] is False
        assert "peer_id required" in result["error"]


class TestFluxNode:
    """Test flux_node: start Flux event-plane listener on a node."""

    @pytest.mark.asyncio
    async def test_flux_node_missing_node_id_fails_closed(self):
        from adk.mesh_provider import flux_node

        result = await flux_node(node_id="")
        assert result["ok"] is False
        assert "node_id required" in result["error"]

    @pytest.mark.asyncio
    async def test_flux_node_missing_aither_internal_secret_fails_closed(self):
        from adk.mesh_provider import flux_node

        with patch.dict("os.environ", {"AITHER_INTERNAL_SECRET": ""}, clear=False):
            result = await flux_node(node_id="spark-dgx", aither_internal_secret="")
            assert result["ok"] is False
            assert "aither_internal_secret required" in result["error"]

    @pytest.mark.asyncio
    async def test_flux_node_script_not_found(self):
        from adk.mesh_provider import flux_node

        with patch("pathlib.Path.exists", return_value=False):
            result = await flux_node(
                node_id="spark-dgx",
                aither_internal_secret="test_secret_xyz",
            )
            assert result["ok"] is False
            assert "flux-node-up.sh not found" in result["error"]

    @pytest.mark.asyncio
    async def test_flux_node_success_starts_container(self):
        from adk.mesh_provider import flux_node

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_result = MagicMock()
                    mock_result.returncode = 0
                    mock_result.stdout = """
== starting flux listener container
== FLUX OK — listener ready on port 8117
                    """
                    mock_result.stderr = ""
                    mock_run.return_value = mock_result

                    result = await flux_node(
                        flux_image="ghcr.io/aitherium/mesh-agent:latest",
                        flux_port=8117,
                        mesh_src="/opt/aitheros/mesh-src",
                        node_id="spark-dgx",
                        aither_internal_secret="test_secret_xyz",
                    )

                    assert result["ok"] is True
                    assert result["message"] == "Flux listener started and healthy"
                    assert result["container"] == "aither-flux"
                    assert result["port"] == 8117
                    assert result["node_id"] == "spark-dgx"

                    # Verify subprocess was called with correct environment
                    mock_run.assert_called_once()
                    call_args = mock_run.call_args
                    call_env = call_args[1]["env"]
                    assert call_env["NODE_ID"] == "spark-dgx"
                    assert call_env["FLUX_PORT"] == "8117"
                    assert call_env["AITHER_INTERNAL_SECRET"] == "test_secret_xyz"
                    # Never echo the secret
                    assert call_args[1]["capture_output"] is True

    @pytest.mark.asyncio
    async def test_flux_node_script_failure_returns_error(self):
        from adk.mesh_provider import flux_node

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_result = MagicMock()
                    mock_result.returncode = 1
                    mock_result.stdout = ""
                    mock_result.stderr = "ERROR: docker run failed — image not found"
                    mock_run.return_value = mock_result

                    result = await flux_node(
                        node_id="spark-dgx",
                        aither_internal_secret="test_secret_xyz",
                    )

                    assert result["ok"] is False
                    assert "exited with code 1" in result["error"]
                    assert "image not found" in result["details"]

    @pytest.mark.asyncio
    async def test_flux_node_timeout_raises_error(self):
        from adk.mesh_provider import flux_node

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("subprocess.run") as mock_run:
                    import subprocess

                    mock_run.side_effect = subprocess.TimeoutExpired("cmd", 60)

                    result = await flux_node(
                        node_id="spark-dgx",
                        aither_internal_secret="test_secret_xyz",
                    )

                    assert result["ok"] is False
                    assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_flux_node_env_resolution(self):
        from adk.mesh_provider import flux_node

        with patch.dict(
            "os.environ",
            {
                "FLUX_IMAGE": "custom-image:v1",
                "FLUX_PORT": "9000",
                "MESH_SRC": "/custom/mesh",
                "AITHER_NODE_ID": "custom-node",
                "AITHER_INTERNAL_SECRET": "env_secret_xyz",
            },
            clear=False,
        ):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.is_file", return_value=True):
                    with patch("subprocess.run") as mock_run:
                        mock_result = MagicMock()
                        mock_result.returncode = 0
                        mock_result.stdout = "OK"
                        mock_result.stderr = ""
                        mock_run.return_value = mock_result

                        # Call with some args, verify env resolution
                        result = await flux_node(
                            flux_image="override-image:v2",  # Should override env
                            node_id="custom-node",
                            # aither_internal_secret not passed, should resolve from env
                        )

                        assert result["ok"] is True
                        call_env = mock_run.call_args[1]["env"]
                        # Explicit arg overrides env
                        assert call_env["FLUX_IMAGE"] == "override-image:v2"
                        # But port should come from env
                        assert call_env["FLUX_PORT"] == "9000"
                        # And secret should resolve from env when not passed
                        assert call_env["AITHER_INTERNAL_SECRET"] == "env_secret_xyz"
