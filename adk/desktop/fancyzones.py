"""Opt-in FancyZones seam: apply a saved layout through ``FancyZonesCLI.exe``.

Never the default arrangement path -- ``adk.desktop.windows`` moves windows
directly. The CLI (shipped with PowerToys 0.102+) rewrites
``applied-layouts.json`` and broadcasts the private reload message itself, so
FancyZones picks the layout up without a restart.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from adk.desktop.powertoys import PowerToysError

__all__ = ["PowerToysError", "cli_path", "available", "apply_layout"]

CREATE_NO_WINDOW = 0x08000000
_UUID = re.compile(r"^\{?[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}?$")


def cli_path() -> Optional[Path]:
    """Where ``FancyZonesCLI.exe`` lives, or None."""
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [os.environ.get("ProgramFiles", ""),
             os.path.join(local, "PowerToys") if local else ""]
    for root in roots:
        if not root:
            continue
        for candidate in (Path(root) / "PowerToys" / "FancyZonesCLI.exe",
                          Path(root) / "FancyZonesCLI.exe"):
            if candidate.is_file():
                return candidate
    return None


def available() -> bool:
    """True when the FancyZones CLI is installed."""
    return os.name == "nt" and cli_path() is not None


def apply_layout(uuid: str, *, monitor: int = -1, dry_run: bool = True,
                 timeout: float = 15.0) -> dict:
    """Apply FancyZones layout ``uuid`` (``--monitor N`` when ``monitor >= 0``).

    Args:
        uuid: The layout's uuid from ``custom-layouts.json``.
        monitor: FancyZones CLI monitor number (1-based); -1 lets the CLI choose.
        dry_run: Return the command without running it. Default True.
        timeout: Seconds before the CLI is abandoned.

    Returns:
        Receipt dict with ``argv`` and, when run, ``returncode``/``stdout``.

    Raises:
        PowerToysError: bad uuid, CLI missing, timeout or non-zero exit.
    """
    if not _UUID.match((uuid or "").strip()):
        raise PowerToysError(f"not a layout uuid: {uuid!r}")
    exe = cli_path()
    if exe is None:
        raise PowerToysError("FancyZonesCLI.exe not found (PowerToys not installed?)")
    argv = [str(exe), "set-layout", uuid.strip()]
    if monitor is not None and int(monitor) >= 0:
        argv += ["--monitor", str(int(monitor))]
    if dry_run:
        return {"ok": True, "dry_run": True, "layout": "fancyzones", "argv": argv}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PowerToysError(f"FancyZonesCLI failed: {exc}") from exc
    if proc.returncode != 0:
        raise PowerToysError(f"FancyZonesCLI exit {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return {"ok": True, "dry_run": False, "layout": "fancyzones", "argv": argv,
            "returncode": 0, "stdout": proc.stdout.strip()[:2000]}
