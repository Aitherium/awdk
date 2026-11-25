"""Where the control plane is — one answer shared by the CLI and the fleet drivers.

The control plane is the SERVER-RENDERED portal (``https://api.aitherium.com``
by default; ``AITHER_PORTAL_URL`` / ``AITHER_ELYSIUM_URL`` override). Genesis
publishes no host port, so a customer reaches its API only through the portal's
``/api/genesis`` proxy — never ``http://localhost:8001`` (the fleet speaks TLS
and that port is not published). See ``adk.cli._control_plane`` for the
measured history behind the default.
"""

from __future__ import annotations

import os

DEFAULT_CONTROL_PLANE = "https://api.aitherium.com"
GENESIS_PROXY_PREFIX = "/api/genesis"


def control_plane_url() -> str:
    """Base URL of the control plane (no trailing slash)."""
    return (
        os.environ.get("AITHER_PORTAL_URL")
        or os.environ.get("AITHER_ELYSIUM_URL")
        or DEFAULT_CONTROL_PLANE
    ).rstrip("/")


#: The portal's generic Genesis bridge (auth forwarded, no path allowlist). Data-plane
#: clients (session/file/lockbox sync, packs, training) use it: their routes are not on
#: the browser-facing ``/api/genesis`` allowlist.
GENESIS_BRIDGE_PREFIX = "/api/bridge/genesis"

_warned_gateway_alias = False


#: Hosts that serve MCP tools only. Every Genesis HTTP route there is a 404, so a
#: "Genesis base" naming one of them is a misconfiguration, never a Genesis base.
MCP_GATEWAY_HOSTS = frozenset({"mcp.aitherium.com", "gateway.aitherium.com"})
MCP_GATEWAY_LOCAL_PORT = 8182


def names_mcp_gateway(url: str) -> bool:
    """True when ``url`` points at an MCP gateway rather than at Genesis.

    A user-level ``AITHER_API_URL=https://mcp.aitherium.com`` (other parts of the
    kit read that variable as the MCP gateway) made every Genesis call 404: notebook
    lists, packs and twins each grew a private workaround. This is the one shared
    answer: the public MCP hosts, the local gateway port, and whatever host
    ``AITHER_MCP_URL`` / ``AITHER_MCP_GATEWAY_URL`` name.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if not host:
        return False
    hosts = set(MCP_GATEWAY_HOSTS)
    for var in ("AITHER_MCP_URL", "AITHER_MCP_GATEWAY_URL"):
        try:
            other = urlsplit((os.environ.get(var) or "").strip())
            if other.hostname:
                hosts.add(other.hostname.lower())
                if other.port and host == other.hostname.lower() and port == other.port:
                    return True
        except ValueError:
            continue
    if host in ("127.0.0.1", "localhost", "::1"):
        return port == MCP_GATEWAY_LOCAL_PORT
    return host in hosts


def genesis_override() -> str:
    """An explicitly configured DIRECT Genesis base, or "".

    ``AITHER_GENESIS_URL`` (preferred) or ``AITHER_API_URL``. ``AITHER_GATEWAY_URL``
    is still honoured for backward compatibility but is deprecated here: other
    modules read the same variable as the MCP gateway (``AITHER_MCP_GATEWAY_URL``),
    so setting it for one silently re-pointed the other.
    """
    global _warned_gateway_alias
    for var in ("AITHER_GENESIS_URL", "AITHER_API_URL"):
        explicit = (os.environ.get(var) or "").strip()
        if explicit and not names_mcp_gateway(explicit):
            return explicit.rstrip("/")
    legacy = os.environ.get("AITHER_GATEWAY_URL", "")
    if legacy and not names_mcp_gateway(legacy):
        if not _warned_gateway_alias:
            _warned_gateway_alias = True
            import warnings
            warnings.warn(
                "AITHER_GATEWAY_URL as a Genesis base is deprecated; set AITHER_GENESIS_URL "
                "(Genesis) or AITHER_MCP_GATEWAY_URL (MCP gateway) instead",
                DeprecationWarning, stacklevel=3)
        return legacy.rstrip("/")
    return ""


def genesis_api_base() -> str:
    """Base URL for Genesis ``/v1/...`` routes the portal allowlists.

    An explicit override (see ``genesis_override``) is a DIRECT Genesis base and is
    used as given; otherwise the portal's ``/api/genesis`` proxy.
    """
    return genesis_override() or control_plane_url() + GENESIS_PROXY_PREFIX


def genesis_data_base() -> str:
    """Base URL for Genesis data-plane routes (sync, packs, training).

    Direct override first, else the portal's generic bridge. Never
    ``http://localhost:8001``: Genesis publishes no host port and speaks TLS.
    """
    return genesis_override() or control_plane_url() + GENESIS_BRIDGE_PREFIX


__all__ = ["DEFAULT_CONTROL_PLANE", "GENESIS_PROXY_PREFIX", "GENESIS_BRIDGE_PREFIX",
           "control_plane_url", "genesis_api_base", "genesis_data_base", "genesis_override",
           "names_mcp_gateway"]
