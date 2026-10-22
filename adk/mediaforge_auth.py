"""The ONE credential helper for every awdk caller of media-forge.

media-forge's read gate (media-forge ``app/read_gate.py``) is default deny: a caller passes with the
owner key or with ``X-Internal-Key`` matching the engine's internal key (``AITHER_INTERNAL_SECRET``,
its rotation twin, ``AITHER_MASTER_KEY``, or the host key file ``~/.aither/internal_key.txt``).
Uncredentialed callers only work while the engine keeps its fleet-compat routes open.

This is the awdk twin of the monorepo's ``lib.integration.mediaforge_ops.mediaforge_headers``: awdk
ships to PyPI and may not import the monorepo, so the logic lives here too (env, then the host key
file -- no vault client). The key is read fresh per call, never logged; with no key the request
still goes out and ONE warning says so.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("adk.mediaforge_auth")

_KEY_ENVS = ("AITHER_INTERNAL_SECRET", "AITHER_MASTER_KEY")
_HOST_KEY_FILE = os.path.join("~", ".aither", "internal_key.txt")
_WARNED = False


def _internal_key() -> str:
    for name in _KEY_ENVS:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    try:
        with open(os.path.expanduser(_HOST_KEY_FILE), encoding="utf-8") as fh:
            key = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return key if len(key) >= 16 else ""


def mediaforge_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """``extra`` plus ``X-Internal-Key`` / ``X-Caller-Type: platform`` when a key is available."""
    global _WARNED
    headers = dict(extra or {})
    if any(k.lower() == "x-internal-key" for k in headers):
        return headers
    key = _internal_key()
    if key:
        headers["X-Internal-Key"] = key
        if not any(k.lower() == "x-caller-type" for k in headers):
            headers["X-Caller-Type"] = "platform"
    elif not _WARNED:
        _WARNED = True
        logger.warning(
            "media-forge caller has no internal key (AITHER_INTERNAL_SECRET / "
            "AITHER_MASTER_KEY / %s): sending uncredentialed; this breaks when media-forge "
            "closes its fleet-compat routes", _HOST_KEY_FILE)
    return headers
