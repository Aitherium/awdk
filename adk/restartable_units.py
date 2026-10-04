"""The units THIS host lets its owner restart remotely (``restart-lane``).

The list is the host's own: a file the host image writes (one systemd unit per line,
``#`` comments allowed), never something the server sends. The device publishes it in
its heartbeat so the server can refuse a unit that is not on it, and the device checks
it again before running ``systemctl restart <unit>``. Unit names are validated; the
list is capped.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

__all__ = ["UNIT_RE", "MAX_UNITS", "units_file", "restartable_units"]

UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,95}\.service$")
MAX_UNITS = 32
_DEFAULT_FILE = "/etc/aither/restartable-units"


def units_file() -> Path:
    return Path(os.environ.get("AITHER_RESTARTABLE_UNITS_FILE") or _DEFAULT_FILE)


def restartable_units(path: Optional[Path] = None) -> List[str]:
    """Valid unit names from the host's file, in order, deduplicated, at most 32.
    A missing or unreadable file means none."""
    try:
        text = (path or units_file()).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: List[str] = []
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip()
        if name and UNIT_RE.match(name) and name not in out:
            out.append(name)
        if len(out) >= MAX_UNITS:
            break
    return out
