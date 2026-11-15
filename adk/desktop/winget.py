"""winget as READ-ONLY typed tools: what is installed, what has an upgrade.

winget 1.29 has no JSON output for ``list``, so this parses its fixed-width table:
the header line names the columns, a rule of dashes follows, and each column's
start is the header word's DISPLAY-CELL offset -- not its character index. A CJK
name ("AI*Shoujo/AI*少女") takes two cells per character and winget pads by cells,
so slicing by character index shifted every column after it (measured 2026-11-09).

Nothing here installs, upgrades or uninstalls. ``--accept-source-agreements`` and
``--disable-interactivity`` are passed so a first-run source prompt can never block
the hard timeout. Stdlib only, Python 3.10 compatible.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adk.desktop.sysinternals import Runner, decode_output, default_runner

__all__ = ["WingetError", "WingetResult", "winget_path", "parse_table", "list_installed",
           "list_upgradable", "NO_PACKAGES_EXIT"]

#: APPINSTALLER_CLI_ERROR_NO_APPLICATIONS_FOUND (0x8A150014): an empty answer, not a failure.
NO_PACKAGES_EXIT = (0x8A150014, 0x8A150014 - (1 << 32))
_QUERY = re.compile(r"^(?!-)[^\x00-\x1f\"]{1,200}$")
_RULE = re.compile(r"^-{10,}\s*$")


class WingetError(RuntimeError):
    """winget is missing or the request is malformed."""


@dataclass
class WingetResult:
    argv: List[str]
    exit_code: Optional[int]
    rows: List[Dict[str, str]] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""
    truncated: bool = False


def winget_path() -> Optional[str]:
    """winget.exe on PATH, else the per-user App Execution Alias, else None."""
    found = shutil.which("winget")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    alias = Path(local, "Microsoft", "WindowsApps", "winget.exe") if local else None
    return str(alias) if alias is not None and alias.exists() else None


def _cells(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _blank_at(line: str, cell: int) -> bool:
    """Pure: is display cell ``cell`` of ``line`` a space (or past the end)?"""
    pos = 0
    for ch in line:
        width = _cells(ch)
        if pos <= cell < pos + width:
            return ch.isspace()
        pos += width
    return True


def _slice_cells(line: str, starts: List[int]) -> List[str]:
    """Pure: cut ``line`` at display-cell offsets ``starts`` (first must be 0)."""
    out: List[str] = []
    bounds = starts[1:] + [10 ** 9]
    col, buf, pos = 0, [], 0
    for ch in line:
        while pos >= bounds[col]:
            out.append("".join(buf))
            buf, col = [], col + 1
        buf.append(ch)
        pos += _cells(ch)
    out.append("".join(buf))
    while len(out) < len(starts):
        out.append("")
    return [c.strip() for c in out[: len(starts)]]


def parse_table(text: str) -> Tuple[List[Dict[str, str]], bool]:
    """Pure: winget's first table -> (rows keyed by lower-cased header, truncated?).

    Rows end at a blank line; a ``<...>`` note sets ``truncated``; a line with no
    Id cell (the "3 upgrades available." footer) is not a row.
    """
    lines = text.splitlines()
    rule = next((i for i, ln in enumerate(lines) if _RULE.match(ln) and i > 0), None)
    if rule is None:
        return [], False
    header = lines[rule - 1]
    starts: List[int] = []
    names: List[str] = []
    pos = 0
    prev_space = True
    for ch in header:
        if not ch.isspace() and prev_space:
            starts.append(pos)
            names.append("")
        if not ch.isspace():
            names[-1] += ch
        prev_space = ch.isspace()
        pos += _cells(ch)
    if not starts or starts[0] != 0:
        return [], False
    keys = [n.lower() for n in names]
    rows: List[Dict[str, str]] = []
    truncated = False
    for line in lines[rule + 1:]:
        if not line.strip():
            break
        if line.lstrip().startswith("<"):
            truncated = True
            continue
        # winget pads every cell with at least one space, so a real row has a blank
        # cell just before the Id column; prose ("3 upgrades available.") does not.
        if len(starts) > 1 and not _blank_at(line, starts[1] - 1):
            continue
        row = dict(zip(keys, _slice_cells(line, starts)))
        if len(keys) > 1 and not row.get(keys[1]):
            continue
        rows.append(row)
    return rows, truncated


def _run(extra: List[str], query: str, limit: int, runner: Optional[Runner],
         exe: Optional[str], timeout: float) -> WingetResult:
    found = exe or winget_path()
    if not found:
        raise WingetError("winget not found (App Installer missing?)")
    argv = [found, "list"]
    q = (query or "").strip()
    if q:
        if not _QUERY.match(q):
            raise WingetError(f"bad query: {query!r}")
        argv += ["--query", q]
    argv += extra
    n = int(limit or 0)
    if n:
        argv += ["--count", str(max(1, min(n, 1000)))]
    argv += ["--accept-source-agreements", "--disable-interactivity"]
    try:
        proc = (runner or default_runner)(argv, timeout)
    except subprocess.TimeoutExpired:
        return WingetResult(argv, None, timed_out=True, error=f"timed out after {timeout}s")
    out = decode_output(proc.stdout or b"")
    rows, truncated = parse_table(out)
    code = proc.returncode
    if code in NO_PACKAGES_EXIT:
        return WingetResult(argv, code, [], truncated=False)
    err = "" if code == 0 else (decode_output(proc.stderr or b"").strip()
                                or out.strip()[-300:] or f"exit {code}")
    return WingetResult(argv, code, rows, error=err, truncated=truncated)


def list_installed(query: str = "", limit: int = 0, *, runner: Optional[Runner] = None,
                   exe: Optional[str] = None, timeout: float = 120.0) -> WingetResult:
    """Installed packages (winget list): name, id, version, available, source."""
    return _run([], query, limit, runner, exe, timeout)


def list_upgradable(query: str = "", *, runner: Optional[Runner] = None,
                    exe: Optional[str] = None, timeout: float = 180.0) -> WingetResult:
    """Installed packages with a newer version available (winget list --upgrade-available)."""
    return _run(["--upgrade-available"], query, 0, runner, exe, timeout)


def to_dict(r: WingetResult) -> Dict[str, Any]:
    """The tool-layer receipt for one run."""
    ok = not r.timed_out and not r.error
    return {"ok": ok, "tool": "winget", "exit_code": r.exit_code, "timed_out": r.timed_out,
            "rows": r.rows, "count": len(r.rows), "truncated": r.truncated,
            "error": r.error}
