"""Who locks this file: PowerToys File Locksmith's CLI as a READ-ONLY typed tool.

``FileLocksmithCLI.exe --json <paths>`` prints ``{"processes": [{"pid", "name",
"user", "files"}]}`` (measured 2026-11-09 against a file held open by python.exe).
The CLI also has ``--kill``; this module never passes it, and refuses any path
that could be read as a flag. Stdlib only, Python 3.10 compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from adk.desktop.powertoys import program_path
from adk.desktop.sysinternals import Runner, decode_output, default_runner

__all__ = ["LocksmithError", "LockResult", "who_locks", "build_argv", "MAX_PATHS"]

EXE_NAME = "FileLocksmithCLI.exe"
MAX_PATHS = 32


class LocksmithError(RuntimeError):
    """File Locksmith is missing or the request is malformed."""


@dataclass
class LockResult:
    argv: List[str]
    exit_code: Optional[int]
    processes: List[Dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""


def build_argv(exe: str, paths: Sequence[str]) -> List[str]:
    """Pure: the read-only command line. Raises LocksmithError on a bad path list.

    Every path must be ABSOLUTE: a relative path resolves against the daemon's cwd,
    and an absolute Windows path can never start with '-' (so never ``--kill``).
    """
    clean = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not clean:
        raise LocksmithError("paths is required (one or more absolute file/folder paths)")
    if len(clean) > MAX_PATHS:
        raise LocksmithError(f"at most {MAX_PATHS} paths per call, got {len(clean)}")
    for p in clean:
        if p.startswith("-") or not (os.path.isabs(p) or _looks_windows_abs(p)):
            raise LocksmithError(f"not an absolute path: {p!r}")
    return [exe, "--json", *clean]


def _looks_windows_abs(p: str) -> bool:
    """C:\\x or C:/x or \\\\server\\share -- absolute on Windows even when tested elsewhere."""
    return (len(p) >= 3 and p[0].isalpha() and p[1] == ":" and p[2] in "\\/") or \
        p.startswith("\\\\")


def who_locks(paths: Sequence[str], *, runner: Optional[Runner] = None,
              exe: Optional[str] = None, timeout: float = 30.0) -> LockResult:
    """Which processes hold ``paths`` open. Never kills anything.

    Raises:
        LocksmithError: File Locksmith not installed, or a bad path list.
    """
    found = exe or (str(program_path(EXE_NAME) or ""))
    if not found:
        raise LocksmithError(f"{EXE_NAME} not found (PowerToys with File Locksmith "
                             "installed?)")
    argv = build_argv(found, paths)
    try:
        proc = (runner or default_runner)(argv, timeout)
    except subprocess.TimeoutExpired:
        return LockResult(argv, None, timed_out=True, error=f"timed out after {timeout}s")
    out = decode_output(proc.stdout or b"").strip()
    try:
        doc = json.loads(out) if out else {}
    except ValueError:
        return LockResult(argv, proc.returncode,
                          error=f"unparseable output: {out[:200]!r}")
    rows = doc.get("processes") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return LockResult(argv, proc.returncode,
                          error=f"no 'processes' list in output: {out[:200]!r}")
    err = "" if proc.returncode == 0 else (
        decode_output(proc.stderr or b"").strip() or f"exit {proc.returncode}")
    return LockResult(argv, proc.returncode,
                      [r for r in rows if isinstance(r, dict)], error=err)
