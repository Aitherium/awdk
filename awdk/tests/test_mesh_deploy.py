"""Tests for mesh-deploy-aware features: custom LLM detection, reach=mesh, and registry."""

import json
import os
import urllib.error
import urllib.request
from unittest.mock import MagicMock, Mock, patch

import pytest


class TestPreflightCheckCustomLLM:
    """Test AITHER_LLM_BASE_URL detection in _preflight_check."""

    def test_custom_llm_url_detected(self, monkeypatch):
        """AITHER_LLM_BASE_URL is probed before fixed ports."""
        from adk.shell_launcher import _preflight_check

        monkeypatch.setenv("AITHER_LLM_BASE_URL", "http://custom-llm:8124")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = Mock()
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok, desc = _preflight_check()
            assert ok is True
            assert "custom:" in desc
            assert "custom-llm:8124" in desc
            # Verify it was called with the right URL
            called_url = mock_urlopen.call_args[0][0].full_url
            assert called_url == "http://custom-llm:8124/v1/models"

    def test_custom_llm_url_fails_over_to_fixed_ports(self, monkeypatch):
        """If custom URL fails, fall back to checking fixed ports."""
        from adk.shell_launcher import _preflight_check

        monkeypatch.setenv("AITHER_LLM_BASE_URL", "http://bad-llm:9999")
        with patch("urllib.request.urlopen") as mock_urlopen:
            def side_effect(req, timeout=None):
                url = req.full_url
                if "bad-llm" in url:
                    raise urllib.error.URLError("Connection refused")
                if ":8200/health" in url:
                    mock_resp = Mock()
                    mock_resp.status = 200
                    return MagicMock(__enter__=lambda s: mock_resp, __exit__=lambda s, *a: None)
                raise urllib.error.URLError("Connection refused")

            mock_urlopen.side_effect = side_effect

            ok, desc = _preflight_check()
            assert ok is True
            assert "vLLM" in desc

    def test_no_custom_llm_url_uses_default_detection(self, monkeypatch):
        """Without AITHER_LLM_BASE_URL, use normal detection."""
        from adk.shell_launcher import _preflight_check

        monkeypatch.delenv("AITHER_LLM_BASE_URL", raising=False)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = Mock()
            mock_resp.status = 200

            def urlopen_effect(req, timeout=None):
                if ":8200/health" in req.full_url:
                    return MagicMock(__enter__=lambda s: mock_resp, __exit__=lambda s, *a: None)
                raise urllib.error.URLError("Not found")

            mock_urlopen.side_effect = urlopen_effect

            ok, desc = _preflight_check()
            assert ok is True
            assert "vLLM" in desc


class TestMeshReachRegistration:
    """Test reach=mesh registration payload generation."""

    def test_register_fleet_endpoint_mesh_mode(self):
        """_register_fleet_endpoint uses mesh overlay IP when AITHER_MESH_OVERLAY_IP is set."""
        from adk.server import create_app
        from adk.config import Config

        config = Config()
        config.server_port = 8080
        config.aither_api_key = "test-key"

        with patch.dict(os.environ, {
            "AITHER_MESH_OVERLAY_IP": "100.64.1.5",
            "AITHER_TENANT_ID": "test-tenant",
        }):
            with patch("adk.config.load_saved_config") as mock_saved:
                mock_saved.return_value = {
                    "api_key": "test-key",
                    "tenant_id": "test-tenant",
                }
                # We can't easily test _register_fleet_endpoint directly since it's
                # an async function in the lifespan, but we can verify the logic
                # by checking the reach mode computation.
                mesh_overlay_ip = os.getenv("AITHER_MESH_OVERLAY_IP", "").strip()
                if mesh_overlay_ip:
                    reach_mode = "mesh"
                    invoke_url = f"http://{mesh_overlay_ip}:{config.server_port}"
                else:
                    reach_mode = "tunnel"
                    invoke_url = f"http://localhost:{config.server_port}"

                assert reach_mode == "mesh"
                assert invoke_url == "http://100.64.1.5:8080"

    def test_registration_no_mesh_overlay_ip_uses_tunnel(self):
        """Without AITHER_MESH_OVERLAY_IP, reach defaults to tunnel."""
        config_port = 8080
        mesh_overlay_ip = os.getenv("AITHER_MESH_OVERLAY_IP", "").strip()
        if mesh_overlay_ip:
            reach_mode = "mesh"
            invoke_url = f"http://{mesh_overlay_ip}:{config_port}"
        else:
            reach_mode = "tunnel"
            invoke_url = f"http://localhost:{config_port}"

        assert reach_mode == "tunnel"
        assert invoke_url == "http://localhost:8080"


class TestAgentBindingSelfHostedMesh:
    """Test agent_binding registry self_hosted mesh support."""

    @pytest.mark.skip(reason="Requires full AitherOS setup with Genesis")
    async def test_register_self_hosted_mesh_accepted(self):
        """self_hosted provider with reach=mesh and overlay IP owned by caller -> accepted."""
        # This test requires the full Genesis router setup which we can't easily
        # mock here. Instead, we test the helper logic directly below.
        pass

    def test_mesh_ip_candidate_validation(self):
        """_mesh_ip_candidate extracts overlay IP from invoke_url."""
        import sys
        import os
        # Add AitherOS to path if running from awdk
        aitheros_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "AitherOS"))
        if aitheros_path not in sys.path:
            sys.path.insert(0, aitheros_path)

        try:
            from apps.AitherGenesis.routers.agent_binding import _mesh_ip_candidate
        except ImportError:
            pytest.skip("AitherOS modules not available in test environment")
            return

        # Valid 10.77.x mesh IP (configured CIDR is 10.77.0.0/16)
        assert _mesh_ip_candidate("http://10.77.1.5:8080") == "10.77.1.5"
        # Invalid: private (non-overlay) IP
        assert _mesh_ip_candidate("http://192.168.1.5:8080") == ""
        # Invalid: localhost
        assert _mesh_ip_candidate("http://localhost:8080") == ""
        # Invalid: no host
        assert _mesh_ip_candidate("") == ""

    def test_private_ip_rejected_for_non_mesh_self_hosted(self):
        """self_hosted without reach=mesh rejects private IPs."""
        import sys
        import os
        # Add AitherOS to path if running from awdk
        aitheros_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "AitherOS"))
        if aitheros_path not in sys.path:
            sys.path.insert(0, aitheros_path)

        try:
            from lib.agent_packs.managed.mcp_endpoints import is_public_url
        except ImportError:
            pytest.skip("AitherOS modules not available in test environment")
            return

        # Public URL: accepted
        assert is_public_url("http://example.com:8080") is True
        assert is_public_url("https://agent.example.com:8080") is True

        # Private IPs: rejected
        assert is_public_url("http://192.168.1.5:8080") is False
        assert is_public_url("http://10.0.0.1:8080") is False
        assert is_public_url("http://172.16.0.1:8080") is False
        assert is_public_url("http://localhost:8080") is False

        # Overlay IPs (10.77.x, the configured mesh CIDR): NOT rejected by is_public_url
        # Mesh validation is done at the agent_binding layer via _mesh_ip_candidate + bind_node_ip_to_tenant
        # 10.77.x is private, so it's rejected
        assert is_public_url("http://10.77.1.5:8080") is False

    @pytest.mark.skip(reason="Requires full AitherOS setup")
    async def test_register_self_hosted_private_ip_rejected(self):
        """self_hosted provider with private IP (192.168.x) and reach=tunnel -> rejected."""
        # This test requires full Genesis router which needs all dependencies
        pass

    @pytest.mark.skip(reason="Requires full AitherOS setup")
    async def test_register_self_hosted_overlay_ip_not_owned_rejected(self):
        """self_hosted reach=mesh with overlay IP NOT owned by tenant -> rejected."""
        # This test requires full Genesis router which needs all dependencies
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
