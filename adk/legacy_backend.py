"""Migration for configs written before the pull-based local model server was retired.

awdk dropped that backend in 3.8.78/3.8.79: local models run on llama.cpp
(``llama-server``, OpenAI-compatible), with GGUF weights fetched from awrtifact.
Saved configs and environments written by older releases still name the old
backend, so every place that READS a backend name passes it through
:func:`migrate_backend_name` first. The old name maps to ``llamacpp`` and a
single notice is logged per process; nothing else in awdk spells the old name.

This module and ``tests/test_legacy_backend_migration.py`` are the only files
that keep the legacy value, because they must match it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, MutableMapping

logger = logging.getLogger("adk.legacy_backend")

#: The retired backend's name as old configs and env spell it.
LEGACY_BACKEND = "ollama"
#: What it maps to.
REPLACEMENT_BACKEND = "llamacpp"
#: llama-server's default OpenAI-compatible base URL.
LLAMACPP_DEFAULT_URL = "http://127.0.0.1:8080/v1"
#: The old server's port; a base URL on it is rewritten to llama-server's.
_LEGACY_PORT = ":11434"
#: Env vars older releases read for the old server's address.
LEGACY_HOST_ENVS = ("OLLAMA_HOST", "OLLAMA_URL", "OLLAMA_API_URL")
#: Deploy profiles that only existed to serve the old backend.
LEGACY_PROFILES = (f"personal-{LEGACY_BACKEND}",)
#: Config keys whose VALUE may be the old backend name.
_BACKEND_KEYS = (
    "llm_backend", "backend", "default_backend", "provider",
    "reasoning_backend", "perception_backend", "cluster_backend",
)
#: Config keys that held the old server's address.
_HOST_KEYS = ("ollama_host", "ollama_url")

_warned = False


def is_legacy(name: Any) -> bool:
    """True when *name* is the retired backend (case-insensitive)."""
    return isinstance(name, str) and name.strip().lower() == LEGACY_BACKEND


def _notice(where: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "%s names a retired local model server; using '%s' (llama-server at %s). "
        "Run `adk backend set llamacpp` to make it permanent.",
        where, REPLACEMENT_BACKEND, LLAMACPP_DEFAULT_URL,
    )


def migrate_backend_name(name: Any, where: str = "config") -> Any:
    """Return ``llamacpp`` for the retired backend name, else *name* unchanged."""
    if is_legacy(name):
        _notice(where)
        return REPLACEMENT_BACKEND
    return name


def migrate_base_url(url: str) -> str:
    """Rewrite a base URL pointing at the old server's port to llama-server's default."""
    if url and _LEGACY_PORT in url:
        return LLAMACPP_DEFAULT_URL
    return url


def migrate_config_dict(cfg: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Migrate a loaded config mapping IN PLACE and return it.

    Backend-name values become ``llamacpp``; an old host key becomes
    ``llm_base_url`` (rewritten to llama-server's port) unless one is already set.
    """
    changed = False
    for key in _BACKEND_KEYS:
        if is_legacy(cfg.get(key)):
            cfg[key] = REPLACEMENT_BACKEND
            changed = True
    for key in _HOST_KEYS:
        if key in cfg:
            old = cfg.pop(key)
            if old and not cfg.get("llm_base_url"):
                cfg["llm_base_url"] = migrate_base_url(str(old))
            changed = True
    if changed:
        _notice("saved config (~/.aither/config.yaml)")
    return cfg


def legacy_env_base_url(env: Dict[str, str] | None = None) -> str:
    """llama-server URL to use when only an old host env var is set, else ``""``."""
    env = os.environ if env is None else env
    for name in LEGACY_HOST_ENVS:
        if env.get(name):
            return LLAMACPP_DEFAULT_URL
    return ""


def _reset_for_tests() -> None:
    global _warned
    _warned = False


__all__ = [
    "LEGACY_BACKEND",
    "LEGACY_PROFILES",
    "LLAMACPP_DEFAULT_URL",
    "REPLACEMENT_BACKEND",
    "is_legacy",
    "legacy_env_base_url",
    "migrate_backend_name",
    "migrate_base_url",
    "migrate_config_dict",
]
