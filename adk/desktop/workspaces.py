"""PowerToys Workspaces: list the saved desktop sets, launch one (WRITE).

The saved sets live in ``%LOCALAPPDATA%/Microsoft/PowerToys/Workspaces/
workspaces.json`` as ``{"workspaces": [{"id", "name", "applications": [...],
"monitor-configuration": [...], ...}]}`` (measured 2026-11-09, PowerToys 0.9x).
Launching runs ``PowerToys.WorkspacesLauncher.exe <id>`` -- the same command the
Workspaces editor's Launch button and its desktop shortcuts run -- which opens
every app in the set and moves it into place. Stdlib only, Python 3.10 compatible.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from adk.desktop.powertoys import PowerToysError, default_root, program_path

__all__ = ["workspaces_path", "list_workspaces", "resolve_workspace", "launch_workspace",
           "LAUNCHER_EXE"]

LAUNCHER_EXE = "PowerToys.WorkspacesLauncher.exe"
CREATE_NO_WINDOW = 0x08000000

#: Starts the launcher and returns its pid. Tests pass a fake; nothing waits on it,
#: because the launcher lives as long as the apps take to open.
Spawner = Callable[[Sequence[str]], int]


def _default_spawn(argv: Sequence[str]) -> int:
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,  # noqa: S603
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW, close_fds=True)
    return int(proc.pid)


def workspaces_path(root: Optional[Path] = None) -> Path:
    """The saved-workspaces file under the PowerToys settings root."""
    return Path(root or default_root()) / "Workspaces" / "workspaces.json"


def _summary(ws: Dict[str, Any]) -> Dict[str, Any]:
    apps = [a for a in (ws.get("applications") or []) if isinstance(a, dict)]
    return {
        "id": str(ws.get("id") or ""),
        "name": str(ws.get("name") or ""),
        "apps": len(apps),
        "app_names": [str(a.get("application") or a.get("title") or "") for a in apps],
        "monitors": len(ws.get("monitor-configuration") or []),
        "move_existing_windows": bool(ws.get("move-existing-windows", False)),
        "created": ws.get("creation-time"),
        "last_launched": ws.get("last-launched-time"),
    }


def list_workspaces(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every saved workspace as a summary row. Raises PowerToysError when unreadable."""
    path = workspaces_path(root)
    if not path.is_file():
        raise PowerToysError(f"no PowerToys Workspaces file at {path} "
                             "(Workspaces never saved, or PowerToys not installed)")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PowerToysError(f"unreadable {path}: {exc}") from exc
    rows = doc.get("workspaces") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise PowerToysError(f"{path} has no 'workspaces' list")
    return [_summary(w) for w in rows if isinstance(w, dict)]


def resolve_workspace(rows: List[Dict[str, Any]], ref: str) -> Dict[str, Any]:
    """Pure: an id (braces optional) or a case-insensitive exact name -> one row."""
    want = (ref or "").strip()
    if not want:
        raise PowerToysError("workspace is required (an id or a name from the list)")
    bare = want.strip("{}").lower()
    by_id = [r for r in rows if r["id"].strip("{}").lower() == bare]
    if by_id:
        return by_id[0]
    by_name = [r for r in rows if r["name"].strip().lower() == want.lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise PowerToysError(f"{len(by_name)} workspaces are named {want!r}; pass the id")
    raise PowerToysError(f"no workspace {want!r}; known: "
                         + ", ".join(repr(r["name"]) for r in rows))


def launch_workspace(ref: str, *, dry_run: bool = True, root: Optional[Path] = None,
                     exe: Optional[str] = None,
                     spawn: Optional[Spawner] = None) -> Dict[str, Any]:
    """WRITE: open every app in workspace ``ref`` and place it (dry run by default).

    Raises:
        PowerToysError: unknown workspace, ambiguous name, or launcher missing.
    """
    row = resolve_workspace(list_workspaces(root), ref)
    launcher = exe or str(program_path(LAUNCHER_EXE) or "")
    if not launcher:
        raise PowerToysError(f"{LAUNCHER_EXE} not found (PowerToys not installed?)")
    argv = [launcher, row["id"]]
    receipt: Dict[str, Any] = {"ok": True, "dry_run": bool(dry_run), "workspace": row,
                               "argv": argv}
    if dry_run:
        return receipt
    try:
        receipt["pid"] = (spawn or _default_spawn)(argv)
    except OSError as exc:
        raise PowerToysError(f"could not start {LAUNCHER_EXE}: {exc}") from exc
    return receipt
