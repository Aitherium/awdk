"""Scoped filesystem browsing for AitherShell.

"File exploring" in a shell that is reachable from a browser is a security
surface, not a convenience feature. This module is therefore CONTAINMENT-FIRST:

- Browsing requires an explicit ROOT. There is no "browse anywhere" mode. If no
  root can be resolved the request is refused with a reason, never silently
  widened to ``/`` or ``C:\\``.
- Every path is ``resolve()``d (which follows symlinks) and then checked for
  containment. Checking BEFORE resolving is the classic hole: ``root/link``
  passes a prefix test while pointing at ``/etc``.
- Reads are size-capped and binary is reported as binary rather than being
  decoded into mojibake that looks like a corrupt file.

Root resolution order, most explicit first:
  1. ``AITHER_HARNESS_BROWSE_ROOTS`` (os.pathsep-separated)
  2. ``AITHER_HARNESS_ALLOWED_ROOTS`` — the same allowlist that bounds session cwd
  3. the working directories of LIVE sessions — you may browse what you are
     already running an agent inside of, and nothing else.

That third rule is what makes the default useful without being open: a desktop
user who started a session in a repo can explore that repo, and a caller who
started nothing can explore nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

#: Largest file the browser will inline, in bytes.
MAX_READ_BYTES = int(os.environ.get("AITHER_HARNESS_MAX_READ", str(512 * 1024)))

#: Entries returned per directory listing. A directory with 200k files must not
#: become a 200k-element JSON response.
MAX_ENTRIES = int(os.environ.get("AITHER_HARNESS_MAX_ENTRIES", "2000"))

#: Never listed. These leak credentials or are pure noise.
HIDDEN_NAMES = frozenset(
    {".git", "node_modules", "__pycache__", ".next", ".venv", "venv", ".mypy_cache",
     ".pytest_cache", ".ruff_cache"}
)


class FsDeniedError(PermissionError):
    """A path was refused. The message always says why."""


def _split_roots(raw: str) -> list[Path]:
    out: list[Path] = []
    for chunk in raw.split(os.pathsep):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            resolved = Path(chunk).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            out.append(resolved)
    return out


def browse_roots(session_cwds: Optional[Iterable[str]] = None) -> list[Path]:
    """Every directory the caller is permitted to browse."""
    explicit = _split_roots(os.environ.get("AITHER_HARNESS_BROWSE_ROOTS", ""))
    if explicit:
        return explicit
    allowed = _split_roots(os.environ.get("AITHER_HARNESS_ALLOWED_ROOTS", ""))
    if allowed:
        return allowed
    roots: list[Path] = []
    for cwd in session_cwds or []:
        if not cwd:
            continue
        try:
            resolved = Path(cwd).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _contained(target: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def resolve_within(path: str, roots: list[Path]) -> Path:
    """Resolve ``path`` and prove it is inside ``roots``, or raise FsDeniedError.

    ``resolve()`` happens FIRST so a symlink pointing outside a root is caught.
    A prefix check on the unresolved string would pass ``<root>/link-to-etc``.
    """
    if not roots:
        raise FsDeniedError(
            "no browsable root is configured for this daemon. Set "
            "AITHER_HARNESS_BROWSE_ROOTS, or start a session whose cwd you want to browse."
        )
    if not path:
        return roots[0]
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        raise FsDeniedError(f"cannot resolve path: {exc}") from exc
    if not _contained(target, roots):
        raise FsDeniedError(
            f"{target} is outside every browsable root "
            f"({os.pathsep.join(str(r) for r in roots)})"
        )
    return target


def list_dir(path: str, roots: list[Path]) -> dict[str, Any]:
    """One directory listing: directories first, then files, both by name."""
    target = resolve_within(path, roots)
    if not target.is_dir():
        raise FsDeniedError(f"{target} is not a directory")

    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_ENTRIES:
                    truncated = True
                    break
                if entry.name in HIDDEN_NAMES:
                    continue
                try:
                    is_dir = entry.is_dir()
                    size = 0 if is_dir else entry.stat().st_size
                except OSError:
                    # A file that vanished or denied stat mid-walk is listed
                    # with unknown size rather than dropped — a silently
                    # missing entry is worse than an imprecise one.
                    is_dir, size = False, -1
                record = {"name": entry.name, "path": str(target / entry.name),
                          "is_dir": is_dir, "size": size}
                (dirs if is_dir else files).append(record)
    except OSError as exc:
        raise FsDeniedError(f"cannot list {target}: {exc}") from exc

    dirs.sort(key=lambda r: r["name"].lower())
    files.sort(key=lambda r: r["name"].lower())
    parent = str(target.parent) if _contained(target.parent, roots) else ""
    return {
        "path": str(target),
        "parent": parent,
        "entries": dirs + files,
        # Truncation is REPORTED. A silently capped listing reads as "that
        # directory only has 2000 files".
        "truncated": truncated,
        "roots": [str(r) for r in roots],
    }


def read_file(path: str, roots: list[Path]) -> dict[str, Any]:
    """Read a file for display. Binary and oversized files are described, not decoded."""
    target = resolve_within(path, roots)
    if not target.is_file():
        raise FsDeniedError(f"{target} is not a file")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise FsDeniedError(f"cannot stat {target}: {exc}") from exc

    if size > MAX_READ_BYTES:
        return {
            "path": str(target), "size": size, "content": "", "binary": False,
            "truncated": True,
            "reason": f"file is {size} bytes; the browser inlines at most {MAX_READ_BYTES}",
        }
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise FsDeniedError(f"cannot read {target}: {exc}") from exc

    # A NUL byte in the first block is the standard binary heuristic. Decoding
    # a binary with errors="replace" produces plausible-looking garbage that
    # reads as a corrupted source file.
    if b"\x00" in raw[:8192]:
        return {"path": str(target), "size": size, "content": "", "binary": True,
                "truncated": False, "reason": "binary file"}
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", "replace")
    return {"path": str(target), "size": size, "content": content, "binary": False,
            "truncated": False, "reason": ""}
