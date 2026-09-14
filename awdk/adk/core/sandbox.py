"""Sandbox primitive — Security posture #5.

Tools that read/write the filesystem or spawn subprocesses default to a
sandbox directory. Escaping the sandbox requires explicit capability
grants (``FILE_READ``/``FILE_WRITE``/``SHELL_EXEC``) **and** a deliberate
``allow_outside=True`` call.

This module is intentionally small:

* :class:`Sandbox` — resolves a path *into* the sandbox root, refusing any
  path that would escape via ``..`` symlinks or absolute paths.
* :func:`safe_read_text` / :func:`safe_write_text` — capability-checked
  file helpers that route through a Sandbox.
* :func:`safe_run` — capability-checked async subprocess helper that
  pins ``cwd`` to the sandbox and forbids shell metacharacters by
  default.

The default sandbox root is ``$AITHER_SANDBOX_DIR`` or, failing that,
``~/.aither/sandbox/<pid>``. Tests usually plug in their own.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from adk.core.capability import Capability, current_context


class SandboxEscape(PermissionError):
    """Raised when a path or command would escape the sandbox."""


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Sandbox:
    """A rooted filesystem scope. All access resolves *through* :attr:`root`.

    The sandbox is created lazily on first use.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # pragma: no cover — only on read-only FS
            raise SandboxEscape(f"sandbox root not writable: {self.root}") from e

    def resolve(self, relative: str | Path) -> Path:
        """Resolve a path inside the sandbox. Refuses escapes.

        Absolute paths and ``..`` segments that climb above :attr:`root`
        raise :class:`SandboxEscape`.
        """
        p = Path(relative)
        if p.is_absolute():
            raise SandboxEscape(f"absolute path forbidden: {relative}")
        candidate = (self.root / p).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as e:
            raise SandboxEscape(
                f"path escapes sandbox root {self.root}: {relative}"
            ) from e
        return candidate

    def contains(self, path: str | Path) -> bool:
        """``True`` iff ``path`` already lives inside this sandbox."""
        try:
            Path(path).resolve().relative_to(self.root)
        except (OSError, ValueError):
            return False
        return True


# ---------------------------------------------------------------------------
# Active sandbox
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    override = os.environ.get("AITHER_SANDBOX_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".aither" / "sandbox" / str(os.getpid())


_default_sandbox: Sandbox | None = None
_active: ContextVar[Sandbox | None] = ContextVar(
    "aither_adk_sandbox", default=None
)


def get_sandbox() -> Sandbox:
    """Return the active sandbox (scope-local override, else default)."""
    sb = _active.get()
    if sb is not None:
        return sb
    global _default_sandbox
    if _default_sandbox is None:
        _default_sandbox = Sandbox(root=_default_root())
    return _default_sandbox


def set_default_sandbox(sandbox: Sandbox) -> None:
    """Replace the process-wide default sandbox."""
    global _default_sandbox
    _default_sandbox = sandbox


@contextmanager
def use_sandbox(sandbox: Sandbox) -> Iterator[Sandbox]:
    """Activate ``sandbox`` for the current scope."""
    token = _active.set(sandbox)
    try:
        yield sandbox
    finally:
        _active.reset(token)


# ---------------------------------------------------------------------------
# Capability-checked helpers
# ---------------------------------------------------------------------------


def safe_read_text(path: str | Path) -> str:
    """Read a text file from the active sandbox. Requires ``FILE_READ``."""
    current_context().check(Capability.FILE_READ)
    return get_sandbox().resolve(path).read_text(encoding="utf-8")


def safe_write_text(path: str | Path, content: str) -> Path:
    """Write a text file inside the active sandbox. Requires ``FILE_WRITE``."""
    current_context().check(Capability.FILE_WRITE)
    target = get_sandbox().resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# Characters that imply shell interpretation. Forbidden in ``safe_run``
# unless the caller opts in via ``shell=True``. We deliberately exclude
# parentheses — they are only shell-meaningful inside ``shell=True``
# invocations, and we never run with ``shell=True``. Including them
# would refuse legitimate ``python -c "print('x')"`` argv.
_SHELL_METACHARS = frozenset(";|&`$<>\n\r")


@dataclass(slots=True)
class RunResult:
    """Result of :func:`safe_run`."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def safe_run(
    argv: list[str],
    *,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
    allow_shell_metachars: bool = False,
) -> RunResult:
    """Run ``argv`` with ``cwd`` pinned to the sandbox. Requires ``SHELL_EXEC``.

    No shell expansion (``argv`` is a list, not a string). Shell
    metacharacters in any arg raise :class:`SandboxEscape` unless
    ``allow_shell_metachars=True``.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    current_context().check(Capability.SHELL_EXEC)
    if not allow_shell_metachars:
        for a in argv:
            if not isinstance(a, str):
                raise TypeError(f"argv entries must be str, got {type(a).__name__}")
            if any(c in _SHELL_METACHARS for c in a):
                raise SandboxEscape(
                    f"shell metacharacter in argv: {a!r}; "
                    "pass allow_shell_metachars=True to override"
                )
    sb = get_sandbox()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(sb.root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return RunResult(
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


__all__ = [
    "RunResult",
    "Sandbox",
    "SandboxEscape",
    "get_sandbox",
    "safe_read_text",
    "safe_run",
    "safe_write_text",
    "set_default_sandbox",
    "use_sandbox",
]
