"""Tests for login-time workspace endpoint auto-discovery (adk.cli)."""

from adk.cli import _derive_cloud_endpoints


def test_aitherium_cloud_portal():
    eps = _derive_cloud_endpoints("https://api.aitherium.com")
    assert eps == {
        "api_url": "https://mcp.aitherium.com",
        "mcp_url": "https://mcp.aitherium.com/mcp",
        "inference_url": "https://mcp.aitherium.com/v1",
        "identity_url": "https://idp.aitherium.com",
    }


def test_aitherium_cloud_idp_subdomain():
    eps = _derive_cloud_endpoints("https://idp.aitherium.com")
    assert eps is not None
    assert eps["mcp_url"] == "https://mcp.aitherium.com/mcp"
    # mcp.aitherium.com serves BOTH inference (/v1) and MCP (/mcp).
    assert eps["inference_url"].endswith("/v1")


def test_apex_domain_matches():
    assert _derive_cloud_endpoints("https://aitherium.com") is not None


def test_localhost_returns_none():
    # Local dev must not be clobbered with cloud endpoints.
    assert _derive_cloud_endpoints("http://localhost:8115") is None
    assert _derive_cloud_endpoints("https://127.0.0.1:8115") is None


def test_unknown_host_returns_none():
    # Bespoke/sovereign hosts aren't guessable — leave config untouched.
    assert _derive_cloud_endpoints("https://identity.example.com") is None


def test_lab_lookalike_is_not_aitherium():
    # Guard against a naive substring match (e.g. "aitherium.com.evil.test").
    assert _derive_cloud_endpoints("https://aitherium.com.evil.test") is None
