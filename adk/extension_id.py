"""Browser-extension identity for the local daemon's read-only surfaces.

Chrome gives an UNPACKED extension (no ``key`` in its manifest) an id derived
from the folder it was loaded from: the first 32 hex digits of SHA-256 over
the absolute path, UTF-16LE on Windows, UTF-8 elsewhere, each digit mapped
0-f -> a-p. So the installer that stages Awconnect can compute exactly which
``chrome-extension://<id>`` origin is ours and allow only that one; any other
extension stays refused. A Chrome Web Store build has a fixed id, supplied via
``AITHER_EXTENSION_IDS``.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import FrozenSet, Optional

_ID_RE = re.compile(r"^[a-p]{32}$")

# The id every Awconnect install has: both manifests carry the same public "key",
# so store and unpacked builds alike get this id. It is the default of
# AITHER_TRUSTED_EXTENSION_IDS, the only extension origins that may take part in
# sign-in handoff. Mirrors awconnect/shared/extension-id.js.
PINNED_EXTENSION_ID = "hlmfknhcfhjjngckfpacgleffckpmphe"


def allowlist_path() -> Path:
    base = os.environ.get("AITHER_HOME")
    home = Path(base) if base else Path.home() / ".aither"
    return home / "awconnect" / "allowed_extension_ids"


def unpacked_extension_id(folder: "str | os.PathLike[str]", *, windows: Optional[bool] = None) -> str:
    """The id Chrome assigns to an unpacked extension loaded from *folder*."""
    win = sys.platform == "win32" if windows is None else windows
    path = str(folder) if windows is not None else os.path.abspath(str(folder))
    data = path.encode("utf-16-le" if win else "utf-8")
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(data).hexdigest()[:32])


def allow_extension_id(ext_id: str, path: Optional[Path] = None) -> Path:
    """Record *ext_id* as an allowed extension for this user's daemon."""
    if not _ID_RE.match(ext_id or ""):
        raise ValueError(f"not a Chrome extension id: {ext_id!r}")
    path = path or allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()}
    except OSError:
        existing = set()
    if ext_id not in existing:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(ext_id + "\n")
    return path


def trusted_extension_ids() -> FrozenSet[str]:
    """Ids allowed to mint a sign-in handoff: ``AITHER_TRUSTED_EXTENSION_IDS``
    (comma-separated) when set, else the pinned Awconnect id. Exact ids only;
    anything malformed is ignored, so a typo can never widen the set."""
    env = os.environ.get("AITHER_TRUSTED_EXTENSION_IDS")
    raw = env.split(",") if env is not None else [PINNED_EXTENSION_ID]
    return frozenset(i.strip() for i in raw if _ID_RE.match(i.strip()))


def trusted_extension_origins() -> FrozenSet[str]:
    return frozenset(f"chrome-extension://{i}" for i in trusted_extension_ids())


def allowed_extension_ids() -> FrozenSet[str]:
    """Ids from ``AITHER_EXTENSION_IDS`` (comma-separated), the allowlist file and
    the trusted set. Anything that is not a well-formed id is ignored."""
    raw = [s.strip() for s in os.environ.get("AITHER_EXTENSION_IDS", "").split(",")]
    raw += list(trusted_extension_ids())
    try:
        raw += [ln.strip() for ln in allowlist_path().read_text(encoding="utf-8").splitlines()]
    except OSError:
        pass
    return frozenset(i for i in raw if _ID_RE.match(i))


def allowed_extension_origins() -> FrozenSet[str]:
    return frozenset(f"chrome-extension://{i}" for i in allowed_extension_ids())
