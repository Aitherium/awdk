"""Tests for adk.mesh — conductor URL resolution, WireGuard, and Headscale."""

import pytest
from unittest.mock import patch, MagicMock
from adk.mesh import _resolve_conductor_url, _tailscale_up


class TestConductorURLResolution:
    """Test conductor URL fallback logic."""

    def test_resolve_internal_hostname_resolvable(self):
        """When internal hostname resolves, use default."""
        # Mock socket.getaddrinfo to succeed for aitheros-conductor
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                ("", "", 0, "", ("127.0.0.1", 8193))
            ]
            result = _resolve_conductor_url("https://aitheros-conductor:8193")
            assert result == "https://aitheros-conductor:8193"

    def test_resolve_internal_hostname_not_resolvable(self):
        """When internal hostname doesn't resolve, fall back to public."""
        # Mock socket.getaddrinfo to fail with gaierror
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            import socket

            mock_getaddrinfo.side_effect = socket.gaierror(
                "Name or service not known"
            )
            result = _resolve_conductor_url("https://aitheros-conductor:8193")
            # Public tunnel serves on 443 (https, no port) — NOT the internal :8193.
            assert result == "https://conductor.aitherium.com"

    def test_resolve_fallback_is_https_no_port(self):
        """The public fallback is always https://conductor.aitherium.com with no
        port, regardless of the internal scheme/port — the CF tunnel is 443-only."""
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            import socket

            mock_getaddrinfo.side_effect = socket.gaierror(
                "Name or service not known"
            )
            result = _resolve_conductor_url(
                "http://aitheros-conductor:8193"
            )
            assert result == "https://conductor.aitherium.com"

    def test_resolve_public_hostname_resolvable(self):
        """Public hostname should always resolve (or use as-is)."""
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                ("", "", 0, "", ("1.2.3.4", 8193))
            ]
            result = _resolve_conductor_url(
                "https://conductor.aitherium.com:8193"
            )
            assert result == "https://conductor.aitherium.com:8193"

    def test_resolve_localhost_resolvable(self):
        """Localhost should resolve."""
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                ("", "", 0, "", ("127.0.0.1", 8193))
            ]
            result = _resolve_conductor_url("https://localhost:8193")
            assert result == "https://localhost:8193"


class TestHeadscaleTailscaleUp:
    """Test Headscale tunnel transport (NAT-friendly alternative to
    raw WireGuard)."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_constructs_correct_command(
        self, mock_which, mock_run
    ):
        """tailscale up command is constructed with correct flags."""
        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )

        _tailscale_up(
            headscale_url="https://headscale.aitherium.com",
            auth_key="tskey-api-abc123def456",
            hostname="aither-node-123",
        )

        # Verify subprocess.run was called with correct command
        expected_cmd = [
            "/usr/bin/tailscale",
            "up",
            "--login-server",
            "https://headscale.aitherium.com",
            "--authkey",
            "tskey-api-abc123def456",
            "--hostname",
            "aither-node-123",
        ]
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == expected_cmd
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_success_returns_output(
        self, mock_which, mock_run
    ):
        """Successful tailscale up returns combined stdout+stderr."""
        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Connected.",
            stderr="[notice] some notice",
        )

        output = _tailscale_up(
            headscale_url="https://headscale.example.com",
            auth_key="key123",
            hostname="node-1",
        )

        assert "Connected." in output
        assert "[notice] some notice" in output

    @patch("shutil.which")
    def test_tailscale_up_binary_not_found(self, mock_which):
        """RuntimeError raised when tailscale binary not found."""
        mock_which.return_value = None

        with pytest.raises(RuntimeError, match="Tailscale.*not found"):
            _tailscale_up(
                headscale_url="https://headscale.aitherium.com",
                auth_key="key",
                hostname="node",
            )

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_returns_error_to_caller(self, mock_which, mock_run):
        """Non-zero return code from tailscale up is logged but output
        returned."""
        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: connection refused",
        )

        # _tailscale_up doesn't raise on non-zero return;
        # it returns the output
        output = _tailscale_up(
            headscale_url="https://headscale.aitherium.com",
            auth_key="key",
            hostname="node",
        )
        assert "connection refused" in output

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_timeout_handling(self, mock_which, mock_run):
        """Timeout during tailscale up raises RuntimeError."""
        import subprocess

        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.side_effect = subprocess.TimeoutExpired("tailscale", 30)

        with pytest.raises(RuntimeError, match="timed out"):
            _tailscale_up(
                headscale_url="https://headscale.aitherium.com",
                auth_key="key",
                hostname="node",
            )

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_generic_exception(self, mock_which, mock_run):
        """Generic exception during tailscale up raises RuntimeError."""
        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.side_effect = Exception("some error")

        with pytest.raises(RuntimeError, match="failed"):
            _tailscale_up(
                headscale_url="https://headscale.aitherium.com",
                auth_key="key",
                hostname="node",
            )

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_tailscale_up_no_internal_hostnames_in_url(
        self, mock_which, mock_run
    ):
        """Headscale URL is passed as-is (no container hostname leakage)."""
        mock_which.return_value = "/usr/bin/tailscale"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )

        _tailscale_up(
            headscale_url="https://headscale.aitherium.com",
            auth_key="key",
            hostname="aither-node-xyz",
        )

        # Verify the command doesn't include aitheros-* container hostnames
        args, _ = mock_run.call_args
        cmd = args[0]
        assert "aitheros-" not in " ".join(cmd)
        # Public hostname is preserved
        assert "headscale.aitherium.com" in " ".join(cmd)


class TestSelfServiceAutoJoin:
    """The conductor auto-issues a headscale key in the onboard response so the
    customer never handles one; adk must auto-join headscale off that response."""

    @pytest.mark.asyncio
    async def test_join_auto_uses_conductor_issued_key(self, monkeypatch):
        import adk.mesh as m

        monkeypatch.delenv("AITHER_MESH_TRANSPORT", raising=False)
        monkeypatch.delenv("AITHER_HEADSCALE_AUTH_KEY", raising=False)
        # Onboard returns an auto-issued headscale key + url (NAT'd node).
        async def fake_onboard(*a, **k):
            return {
                "overlay_ip": "10.77.9.9",
                "node_id_assigned": "n1",
                "headscale_auth_key": "conductor-issued-key",
                "headscale_url": "https://headscale.aitherium.com",
            }
        monkeypatch.setattr(m, "onboard", fake_onboard)
        monkeypatch.setattr(m, "generate_keypair", lambda: ("priv", "pub"))
        captured = {}
        def fake_up(url, key, host):
            captured.update(url=url, key=key, host=host)
            return "Success."
        monkeypatch.setattr(m, "_tailscale_up", fake_up)

        # No transport flag, no env — must still pick headscale because the
        # conductor issued a key (the whole self-service point).
        report = await m.join("https://conductor.aitherium.com", node_id="n1")
        assert report["transport"] == "headscale"
        assert captured["key"] == "conductor-issued-key"
        assert captured["url"] == "https://headscale.aitherium.com"

    @pytest.mark.asyncio
    async def test_join_stays_wireguard_without_issued_key(self, monkeypatch):
        import adk.mesh as m

        monkeypatch.delenv("AITHER_MESH_TRANSPORT", raising=False)
        monkeypatch.delenv("AITHER_HEADSCALE_AUTH_KEY", raising=False)

        async def fake_onboard(*a, **k):
            return {"overlay_ip": "10.77.1.1", "node_id_assigned": "n2"}
        monkeypatch.setattr(m, "onboard", fake_onboard)
        monkeypatch.setattr(m, "generate_keypair", lambda: ("priv", "pub"))
        # Stub the raw-WireGuard chain so the default path completes.
        monkeypatch.setattr(m, "fetch_server_pubkey",
                            _async_return(("srvpub", "1.2.3.4:51820")))
        monkeypatch.setattr(m, "_wg_conf", lambda *a, **k: "conf")
        monkeypatch.setattr(m, "write_config", lambda *a, **k: "/tmp/wg.conf")
        monkeypatch.setattr(m, "bring_up", lambda *a, **k: "up")
        monkeypatch.setattr(m, "has_handshake", lambda *a, **k: True)
        monkeypatch.setattr(m, "_tailscale_up",
                            lambda *a, **k: pytest.fail("should not use headscale"))
        report = await m.join("https://conductor.aitherium.com", node_id="n2")
        assert report["transport"] == "wireguard"


class TestTailscaleKeyRedaction:
    """The auth key must never leak into logs/exceptions."""

    @patch("shutil.which", return_value="/usr/bin/tailscale")
    @patch("subprocess.run")
    def test_timeout_does_not_leak_key(self, mock_run, _which):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["tailscale", "up", "--authkey", "SUPERSECRETKEY"], timeout=30)
        with pytest.raises(RuntimeError) as ei:
            _tailscale_up("https://headscale.aitherium.com", "SUPERSECRETKEY", "h")
        assert "SUPERSECRETKEY" not in str(ei.value)

    @patch("shutil.which", return_value="/usr/bin/tailscale")
    @patch("subprocess.run")
    def test_generic_error_does_not_leak_key(self, mock_run, _which):
        mock_run.side_effect = OSError("boom SUPERSECRETKEY in argv")
        with pytest.raises(RuntimeError) as ei:
            _tailscale_up("https://headscale.aitherium.com", "SUPERSECRETKEY", "h")
        assert "SUPERSECRETKEY" not in str(ei.value)


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f
