"""Standalone service-endpoint resolution for the public ADK.

This is the standalone replacement for the monorepo's internal port resolver
(which reads a server-side service registry).  The public ADK has no such
registry and must not depend on AitherOS internals, so endpoints are resolved
from environment variables with sensible localhost defaults:

* ``AITHER_<NAME>_URL`` — full base URL for a service (wins if set)
* ``AITHER_<NAME>_PORT`` — port override
* ``AITHER_SERVICE_HOST`` — host for the localhost-default URLs (default ``localhost``)

An agent built with this kit connects to a *running* AitherOS instance over the
network (or the public gateway); it never imports monorepo code.
"""

from __future__ import annotations

import os

# Conventional local ports for AitherOS services an ADK agent may connect to.
# These are defaults only — env vars always win, and callers may pass their own
# default.  Kept here so the kit works out-of-the-box against a localhost stack.
_DEFAULT_PORTS: dict[str, int] = {
    "Node": 8090,
    "Mind": 8088,
    "Pulse": 8081,
    "Sense": 8096,
    "TimeSense": 8141,
    "Flow": 8164,
    "AitherFlow": 8164,
    "Cortex": 8139,
    "Harvest": 8108,
    "Judge": 8089,
    "Trainer": 8107,
    "Evolution": 8133,
    "Reasoning": 8093,
    "A2A": 8127,
    "LlamaCpp": 8080,
}


def _env_key(name: str) -> str:
    return name.upper().replace("-", "_").replace(" ", "_")


def get_port(name: str, default: int | None = None) -> int:
    """Resolve a service port from ``AITHER_<NAME>_PORT`` env, *default*, or convention."""
    env = os.getenv(f"AITHER_{_env_key(name)}_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if default is not None:
        return int(default)
    return int(_DEFAULT_PORTS.get(name, 8080))


def get_service_url(name: str, default_port: int | None = None) -> str:
    """Resolve a service base URL.

    ``AITHER_<NAME>_URL`` wins; otherwise build ``http://<host>:<port>`` from
    ``AITHER_SERVICE_HOST`` (default ``localhost``) and the resolved port.
    """
    url = os.getenv(f"AITHER_{_env_key(name)}_URL")
    if url:
        return url.rstrip("/")
    host = os.getenv("AITHER_SERVICE_HOST", "localhost")
    return f"http://{host}:{get_port(name, default_port)}"


def llamacpp_url() -> str:
    """Resolve the local llama-server base URL (``AITHER_LLAMACPP_URL``, default :8080)."""
    return (os.getenv("AITHER_LLAMACPP_URL") or "http://127.0.0.1:8080").rstrip("/")


__all__ = ["get_port", "get_service_url", "llamacpp_url"]
