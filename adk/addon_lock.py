"""One install record for every kind of add-on: what it is, where it came from, its hash.

``~/.aither/addons.lock.json`` (``$AITHER_ADDON_LOCK`` overrides) holds one row per
installed add-on, keyed ``<kind>:<id>``::

    {"version": 1, "installs": {"awpack:persona": {
        "kind": "awpack", "id": "persona", "version": "0.1.0",
        "source": "shelf:/path/to/awpack/packs", "trust": "official",
        "sha256": "<hex>", "hash_of": "content", "signature": "absent",
        "dest": "/home/me/.aither/agents/persona", "installed_at": "..."}}}

Every install surface writes it through :func:`record_install` (``adk pack install``,
``adk skills install``, the agent ``install_pack`` tool, awsh ``/packs``, the desk), so
"what is installed, who shipped it, has it changed" has one answer.

Kinds: ``awpack`` (first-party agent packs), ``library`` (marketplace tool/agent packs),
``awnode-ext`` (local-gateway extensions), ``skill`` (awskills .md files), ``catalogue``
(anything ``aither add`` installed by steps: a brick, an integration, a stack, an app; its
row carries ``extra.remove_steps``, the commands that undo it).

Trust tiers: ``official`` (Aitherium built it), ``community`` (a third party; nobody here
reviewed it), ``local`` (a file or shelf on this machine nobody vouched for).

``hash_of`` says what ``sha256`` covers: ``content`` (the installed tree), ``tarball``
(the downloaded archive), ``file`` (a single file), ``manifest`` (the extension
manifest -- an extension's bytes are an image the container engine pins).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = ["KINDS", "TRUST_TIERS", "HASH_OF", "lock_path", "read_lock", "record_install",
           "forget_install", "lookup", "installs"]

KINDS = ("awpack", "library", "awnode-ext", "skill", "catalogue")
TRUST_TIERS = ("official", "community", "local")
HASH_OF = ("content", "tarball", "file", "manifest")


def lock_path() -> Path:
    """``$AITHER_ADDON_LOCK``, else ``~/.aither/addons.lock.json``."""
    env = os.environ.get("AITHER_ADDON_LOCK", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".aither" / "addons.lock.json"


def read_lock() -> dict:
    """The whole lockfile; absent or unreadable is an empty lock, never an error."""
    try:
        raw = json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict) or not isinstance(raw.get("installs"), dict):
        return {"version": 1, "installs": {}}
    raw.setdefault("version", 1)
    return raw


def _write(lock: dict) -> Path:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".addons.lock.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(lock, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # the temp file may already be gone
            os.unlink(tmp)
        raise
    return path


def record_install(kind: str, addon_id: str, *, version: str, source: str, sha256: str,
                   trust: str, hash_of: str = "content", signature: str = "absent",
                   dest: str = "", extra: "dict | None" = None) -> dict:
    """Record (or replace) the row for ``kind:addon_id``. Returns the row written.

    Raises:
        ValueError: unknown kind / trust / hash_of, an empty id, or no hash -- an
            install that cannot say what it installed is not recorded as if it could.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown add-on kind {kind!r}; expected one of {KINDS}")
    if not addon_id:
        raise ValueError("an add-on id is required")
    if trust not in TRUST_TIERS:
        raise ValueError(f"unknown trust tier {trust!r}; expected one of {TRUST_TIERS}")
    if hash_of not in HASH_OF:
        raise ValueError(f"unknown hash_of {hash_of!r}; expected one of {HASH_OF}")
    if not sha256:
        raise ValueError(f"{kind}:{addon_id}: no content hash to record")
    row = {"kind": kind, "id": addon_id, "version": str(version or "unknown"),
           "source": str(source), "sha256": str(sha256).lower(), "hash_of": hash_of,
           "trust": trust, "signature": str(signature), "dest": str(dest),
           "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if extra:
        row["extra"] = dict(extra)
    lock = read_lock()
    lock["installs"][f"{kind}:{addon_id}"] = row
    _write(lock)
    return row


def forget_install(kind: str, addon_id: str) -> bool:
    """Drop the row for ``kind:addon_id``. Returns whether one was there."""
    lock = read_lock()
    if lock["installs"].pop(f"{kind}:{addon_id}", None) is None:
        return False
    _write(lock)
    return True


def installs(kind: "str | None" = None) -> "list[dict[str, Any]]":
    """Every recorded row, optionally only one kind."""
    rows = [dict(r) for r in read_lock()["installs"].values() if isinstance(r, dict)]
    return [r for r in rows if kind is None or r.get("kind") == kind]


def lookup(addon_id: str, kind: "str | None" = None) -> "dict[str, Any] | None":
    """The row for *addon_id* (of *kind*, or the first kind that has it), else None."""
    table = read_lock()["installs"]
    for k in ((kind,) if kind else KINDS):
        row = table.get(f"{k}:{addon_id}")
        if isinstance(row, dict):
            return dict(row)
    return None
