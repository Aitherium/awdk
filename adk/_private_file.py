"""Owner-only files: restrict a path to the current user, or refuse to write it.

One helper for every file the home agent keeps that another local user must not
read or rewrite -- the local channel token, ``owner.json``, the paused-turn
approval store, the receipts signing key and the receipts log.

POSIX: ``chmod 600``. Windows: ``icacls <path> /inheritance:r /grant:r <user>:F``,
so the file does not keep whatever its folder hands down (a home outside the
profile inherits read access for every local user from the drive root).

Fail closed: when the restriction cannot be applied, :class:`PrivateFileError` is
raised and :func:`write_private_text` leaves no file behind -- never a silently
world-readable one.
"""

from __future__ import annotations

import getpass
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Union

__all__ = ["PrivateFileError", "owner_principal", "restrict_owner_only",
           "create_private_empty", "write_private_text"]

logger = logging.getLogger("adk.private_file")

PathLike = Union[str, "os.PathLike[str]"]


class PrivateFileError(PermissionError):
    """A file could not be restricted to its owner; nothing secret was written."""


def owner_principal() -> str:
    """The Windows principal ``icacls`` grants (``DOMAIN\\user`` when known)."""
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain else user


def restrict_owner_only(path: PathLike) -> None:
    """Owner-only access to ``path``, or :class:`PrivateFileError`."""
    path = Path(path)
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise PrivateFileError(f"cannot chmod 600 {path} ({type(exc).__name__})") from exc
        return
    who = owner_principal()
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise PrivateFileError(f"cannot restrict {path} to {who} "
                               f"({type(exc).__name__})") from exc
    if proc.returncode != 0:
        raise PrivateFileError(f"icacls could not restrict {path} to {who} "
                               f"(exit {proc.returncode})")


def create_private_empty(path: PathLike) -> Path:
    """Create ``path`` EMPTY (exclusive, 0600) and restrict it before anything lands.

    Raises :class:`FileExistsError` when it exists, and :class:`PrivateFileError`
    (after removing the empty file) when it cannot be restricted.
    """
    path = Path(path)
    os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        restrict_owner_only(path)
    except PrivateFileError:
        try:
            path.unlink()
        except OSError as exc:  # the file is empty: nothing secret is left behind
            logger.debug("private file: could not remove empty %s: %s", path, exc)
        raise
    return path


def write_private_text(path: PathLike, text: str, encoding: str = "utf-8") -> Path:
    """Atomically replace ``path`` with ``text``, owner-only.

    The temp file is created empty and restricted BEFORE the content is written,
    so the content never sits in a file carrying the folder's inherited ACL.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    create_private_empty(tmp)
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path
