"""Pack invocation scope — data/config isolation for pack-driven tool calls.

When the admin console's pack UI bridge invokes a tool on behalf of a pack
(``POST /admin/packs/{pack_id}/tools/{tool}/invoke``), the call runs inside a
*pack scope*: a contextvar carrying the pack id and a per-pack data root
(``~/.aither/packs/<pack_id>/data``). While a scope is active:

* the built-in file tools (``file_read`` / ``file_write`` / ``file_edit`` /
  ``file_list``) are jailed to the pack's data root — a pack UI can never read
  the owner's unrelated files through them (fail-closed: scope set ⇒ ONLY the
  data root is allowed, regardless of AITHER_ALLOWED_ROOTS);
* any ``session_id`` argument is namespaced ``pack-<pack_id>-…`` by the invoke
  endpoint, so pack activity never lands in (or reads) the owner's chat
  sessions.

Limitations (documented, not hidden): pack-shipped tools are in-process Python
— they SHOULD resolve paths via :func:`get_pack_scope`'s ``data_root`` but the
jail cannot be hard-enforced on arbitrary pack code. Hard isolation is the
containerized service-pack path.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_PACK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


@dataclass(frozen=True)
class PackScope:
    pack_id: str
    data_root: Path


_current_scope: ContextVar[PackScope | None] = ContextVar("aither_pack_scope", default=None)


def get_pack_scope() -> PackScope | None:
    """The active pack scope, or None outside pack-bridge invocations."""
    return _current_scope.get()


def pack_data_root(pack_id: str) -> Path:
    """Per-pack data directory (created on demand)."""
    root = Path.home() / ".aither" / "packs" / pack_id / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def valid_pack_id(pack_id: str) -> bool:
    """Reject ids that could escape the per-pack directory layout."""
    return bool(_PACK_ID_RE.match(pack_id or ""))


@contextmanager
def pack_scope(pack_id: str):
    """Run the enclosed block inside *pack_id*'s data scope."""
    if not valid_pack_id(pack_id):
        raise ValueError(f"invalid pack id: {pack_id!r}")
    token = _current_scope.set(PackScope(pack_id=pack_id, data_root=pack_data_root(pack_id)))
    try:
        yield _current_scope.get()
    finally:
        _current_scope.reset(token)


def path_in_scope(path: str | Path) -> bool:
    """True if *path* is inside the active scope's data root (or no scope set)."""
    scope = get_pack_scope()
    if scope is None:
        return True
    try:
        return Path(path).resolve().is_relative_to(scope.data_root.resolve())
    except (OSError, ValueError):
        return False
