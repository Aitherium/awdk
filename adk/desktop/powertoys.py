"""Drive PowerToys through its settings files -- the seam that needs no fork.

PowerToys keeps a general ``settings.json`` (with an ``enabled`` map of module
name -> bool) under ``%LOCALAPPDATA%/Microsoft/PowerToys`` and hot-reloads it.
Writes are atomic (temp file + replace) so a half-written file never reaches
the runner. Stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional


def default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Microsoft" / "PowerToys"


class PowerToysError(RuntimeError):
    pass


class PowerToys:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else default_root()

    @property
    def general_path(self) -> Path:
        return self.root / "settings.json"

    def installed(self) -> bool:
        return self.general_path.is_file()

    def _read_general(self) -> dict:
        if not self.installed():
            raise PowerToysError(f"PowerToys settings not found at {self.general_path}")
        try:
            data = json.loads(self.general_path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise PowerToysError(f"unreadable {self.general_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PowerToysError("general settings is not a JSON object")
        return data

    def _write_general(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".aither-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.general_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def modules(self) -> Dict[str, bool]:
        enabled = self._read_general().get("enabled", {})
        return {str(k): bool(v) for k, v in enabled.items()}

    def runs_elevated(self) -> bool:
        """False means PowerToys cannot act on admin windows (Windows UIPI)."""
        return bool(self._read_general().get("run_elevated", False))

    def set_enabled(self, module: str, on: bool) -> bool:
        """Enable/disable a known module. Returns the previous state."""
        data = self._read_general()
        enabled = data.get("enabled")
        if not isinstance(enabled, dict) or module not in enabled:
            raise PowerToysError(f"unknown PowerToys module: {module!r}")
        previous = bool(enabled[module])
        enabled[module] = bool(on)
        self._write_general(data)
        return previous

    def module_settings(self, module: str) -> dict:
        path = self.root / module / "settings.json"
        if not path.is_file():
            raise PowerToysError(f"no settings for module {module!r} at {path}")
        return json.loads(path.read_text(encoding="utf-8-sig"))


def program_path(exe_name: str) -> Optional[Path]:
    """Where a PowerToys executable lives (machine or per-user install), or None."""
    local = os.environ.get("LOCALAPPDATA", "")
    roots = [os.environ.get("ProgramFiles", ""),
             os.path.join(local, "PowerToys") if local else ""]
    for root in roots:
        if not root:
            continue
        for candidate in (Path(root) / "PowerToys" / exe_name, Path(root) / exe_name):
            if candidate.is_file():
                return candidate
    return None
