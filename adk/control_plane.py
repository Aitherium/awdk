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


def genesis_override() -> str:
    """An explicitly configured DIRECT Genesis base, or "".

    ``AITHER_GENESIS_URL`` (preferred) or ``AITHER_API_URL``. ``AITHER_GATEWAY_URL``
    is still honoured for backward compatibility but is deprecated here: other
    modules read the same variable as the MCP gateway (``AITHER_MCP_GATEWAY_URL``),
    so setting it for one silently re-pointed the other.
    """
    global _warned_gateway_alias
    explicit = os.environ.get("AITHER_GENESIS_URL") or os.environ.get("AITHER_API_URL")
    if explicit:
        return explicit.rstrip("/")
    legacy = os.environ.get("AITHER_GATEWAY_URL", "")
    if legacy:
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
           "control_plane_url", "genesis_api_base", "genesis_data_base", "genesis_override"]
