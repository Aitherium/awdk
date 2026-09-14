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
    "Ollama": 11434,
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


def ollama_url() -> str:
    """Resolve the Ollama base URL from env, defaulting to ``http://localhost:11434``.

    Ollama sets ``OLLAMA_HOST`` to its *bind* address (often ``0.0.0.0:11434``),
    which is not a valid connection target on Windows/macOS — rewrite to localhost.
    """
    raw = os.getenv("OLLAMA_URL") or os.getenv("OLLAMA_HOST") or ""
    if not raw:
        return "http://localhost:11434"
    if raw.startswith(("http://", "https://")):
        scheme, host_part = raw.split("://", 1)
    else:
        scheme, host_part = "http", raw
    if host_part.startswith("0.0.0.0"):
        host_part = "localhost" + host_part[len("0.0.0.0"):]
    return f"{scheme}://{host_part}"


__all__ = ["get_port", "get_service_url", "ollama_url"]
