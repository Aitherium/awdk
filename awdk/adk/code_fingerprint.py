"""Fingerprint of the ``adk`` package source, so a probe can tell whether the code on disk
is the code that is RUNNING.

A long-lived daemon that imported its modules before an edit keeps serving the old code
while every file-level check (grep, a bind-mount inspect, a version string read from
package metadata) reports the fix as present. The only honest signal is one the process
computes about itself at startup and a probe recomputes from disk later: if the two
differ, the process predates an edit and must be replaced.

One algorithm, two callers. The daemon reads ``STARTUP`` (computed once at import, i.e.
at launch) into ``/health``; the capability checker calls ``compute()`` against the tree.
Two copies of this algorithm would drift and report a false mismatch or a false match,
so both sides import this module.

Content-hashed, not mtime-based: a checkout that rewrites files with identical content
must not restart the daemon, and a file whose mtime went backwards must not read as
current.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def compute(root: Path | None = None) -> str:
    """Return ``sha256:<24 hex>:<file count>`` over every ``*.py`` under ``root``.

    Paths are hashed relative to ``root`` in sorted order alongside their bytes, so a
    rename, an addition and an edit all change the value. ``__pycache__`` is skipped —
    bytecode is a derivative of exactly the files already hashed.
    """
    base = Path(root) if root is not None else PACKAGE_ROOT
    digest = hashlib.sha256()
    files = sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()[:24]}:{len(files)}"


# The fingerprint of the code that is running: computed once, at import time, which for
# the daemon is launch time. Reading it later returns the launch value on purpose.
STARTUP = compute()
