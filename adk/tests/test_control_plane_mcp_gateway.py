"""An env var naming the MCP gateway is never taken as a Genesis base.

Measured 2026-11-14: a user-level AITHER_API_URL=https://mcp.aitherium.com sent the
notebook list to the MCP gateway (HTTP 404) and the platform read as unreachable.
"""

import pytest

from adk import control_plane as cp

_VARS = ("AITHER_GENESIS_URL", "AITHER_API_URL", "AITHER_GATEWAY_URL",
         "AITHER_MCP_URL", "AITHER_MCP_GATEWAY_URL", "AITHER_PORTAL_URL",
         "AITHER_ELYSIUM_URL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("url", [
    "https://mcp.aitherium.com", "https://gateway.aitherium.com/",
    "http://127.0.0.1:8182", "http://localhost:8182/mcp",
])
def test_mcp_gateway_urls_are_recognised(url):
    assert cp.names_mcp_gateway(url)


@pytest.mark.parametrize("url", [
    "https://aitheros-genesis:8001", "https://api.aitherium.com/api/bridge/genesis",
    "http://127.0.0.1:8001", "", "not a url",
])
def test_genesis_urls_are_not_mcp(url):
    assert not cp.names_mcp_gateway(url)


def test_configured_mcp_host_is_recognised(monkeypatch):
    monkeypatch.setenv("AITHER_MCP_GATEWAY_URL", "https://my-gw.example.com/mcp")
    assert cp.names_mcp_gateway("https://my-gw.example.com")


def test_api_url_naming_mcp_falls_back_to_the_bridge(monkeypatch):
    monkeypatch.setenv("AITHER_API_URL", "https://mcp.aitherium.com")
    assert cp.genesis_override() == ""
    assert cp.genesis_data_base() == cp.DEFAULT_CONTROL_PLANE + cp.GENESIS_BRIDGE_PREFIX


def test_good_genesis_url_wins_over_mcp_api_url(monkeypatch):
    monkeypatch.setenv("AITHER_GENESIS_URL", "https://mcp.aitherium.com")
    monkeypatch.setenv("AITHER_API_URL", "https://aitheros-genesis:8001/")
    assert cp.genesis_override() == "https://aitheros-genesis:8001"


def test_real_genesis_override_is_kept(monkeypatch):
    monkeypatch.setenv("AITHER_GENESIS_URL", "https://aitheros-genesis:8001")
    assert cp.genesis_override() == "https://aitheros-genesis:8001"


def test_a_local_gateway_on_another_port_named_by_env_is_the_gateway(monkeypatch):
    """Review LR-ad51162e: a local MCP gateway on a non-8182 port, named by
    AITHER_MCP_URL, is recognised (the env match runs before the 8182 rule)."""
    from adk import control_plane as cp

    monkeypatch.setenv("AITHER_MCP_URL", "http://127.0.0.1:9182/mcp")
    monkeypatch.delenv("AITHER_MCP_GATEWAY_URL", raising=False)
    assert cp.names_mcp_gateway("http://127.0.0.1:9182")
    assert not cp.names_mcp_gateway("http://127.0.0.1:8001")
