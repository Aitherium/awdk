"""Browser-extension identity for the local daemon's read-only surfaces.

Chrome gives an UNPACKED extension (no ``key`` in its manifest) an id derived
from the folder it was loaded from: the first 32 hex digits of SHA-256 over
the absolute path, UTF-16LE on Windows, UTF-8 elsewhere, each digit mapped
0-f -> a-p. So the installer that stages Awconnect can compute exactly which
``chrome-extension://<id>`` origin is ours and allow only that one; any other
extension stays refused. A build whose manifest DOES carry a ``key`` -- and the
Chrome Web Store one, which keeps the id of its first published package -- has
a fixed id instead; both first-party ids are trusted by default.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import FrozenSet, Optional

_ID_RE = re.compile(r"^[a-p]{32}$")

# The id the manifest's public "key" produces for an unpacked build. The key
# lives in the extension's public/manifest.json (the 4.x source;
# vite copies it into dist/, and build-connect.yml keeps it in the *-unpacked-*
# zip). One of the two first-party ids; see STORE_EXTENSION_ID for the other.
PINNED_EXTENSION_ID = "hlmfknhcfhjjngckfpacgleffckpmphe"

#: The Chrome Web Store build's id. A store item keeps the id of its first
#: published package forever, so it is NOT the key's id the unpacked builds get
#: (measured 2026-10-06: the store item refused sign-in until the hosted side
#: listed this id too). Both ids are first-party and both are trusted by
#: default, so a store install reaches the same local surfaces an unpacked one
#: does.
STORE_EXTENSION_ID = "peeojgjhjficedkncdejbfnacooodbak"

#: The first-party Awconnect ids an install of our product can have.
FIRST_PARTY_EXTENSION_IDS = (PINNED_EXTENSION_ID, STORE_EXTENSION_ID)


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


def key_extension_id(key: str) -> str:
    """The id Chrome assigns to a build whose manifest carries ``key``.

    Same digest-to-letters mapping as the path form, over the DER public key the
    base64 ``key`` decodes to. Raises ValueError on a key that is not base64."""
    import base64
    import binascii

    try:
        der = base64.b64decode("".join(str(key or "").split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"manifest key is not base64: {exc}") from exc
    if not der:
        raise ValueError("manifest key is empty")
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])


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
    (comma-separated) when set, else BOTH first-party ids (the pinned unpacked
    one and the Chrome Web Store one). Exact ids only; anything malformed is
    ignored, so a typo can never widen the set."""
    env = os.environ.get("AITHER_TRUSTED_EXTENSION_IDS")
    raw = env.split(",") if env is not None else list(FIRST_PARTY_EXTENSION_IDS)
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
