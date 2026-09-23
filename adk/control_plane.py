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


def genesis_api_base() -> str:
    """Base URL for Genesis ``/v1/...`` routes.

    An explicit ``AITHER_API_URL`` / ``AITHER_GATEWAY_URL`` is a DIRECT Genesis
    base and is used as given; otherwise the portal's Genesis proxy.
    """
    explicit = os.environ.get("AITHER_API_URL") or os.environ.get("AITHER_GATEWAY_URL")
    if explicit:
        return explicit.rstrip("/")
    return control_plane_url() + GENESIS_PROXY_PREFIX


__all__ = ["DEFAULT_CONTROL_PLANE", "GENESIS_PROXY_PREFIX", "control_plane_url", "genesis_api_base"]
