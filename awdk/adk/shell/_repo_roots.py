"""Discovery of a local AitherOS/AitherZero checkout.

WHY THIS EXISTS
    Four call sites in adk/shell each ended their candidate list with a hardcoded
    an absolute drive path (and in one case a second drive letter). That is one
    developer's drive layout, shipped to every ``pip install awdk`` — useless
    to everyone else and noise in a public package.

    The intent behind it was reasonable: "also look at the root of a drive". So do
    that GENERICALLY — enumerate the drive roots that actually exist on this machine
    — instead of naming one. The owner's D: checkout is still found, and so is any
    other user's, on any letter.

    ``AITHEROS_REPO`` is honoured first so the whole search can be short-circuited
    explicitly, which is what a container or CI run should do.
"""

from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Iterator

# Directory names a checkout is plausibly called.
_REPO_DIR_NAMES = ("AitherOS-Fresh", "AitherOS")


def _drive_roots() -> Iterator[Path]:
    """Existing filesystem roots to probe. On POSIX this is just '/'.

    ORDER MATTERS. A plain A→Z scan is wrong on a machine with more than one
    checkout: this box has both C:\\AitherOS-Fresh and D:\\AitherOS-Fresh, and
    alphabetical order silently flipped resolution from D: (what the old hardcoded
    fallback picked) to C:. So probe the drive this code is RUNNING from first,
    then the current working directory's drive, then the rest alphabetically —
    "look nearest to yourself" preserves the previous behaviour without naming a
    letter.
    """
    if os.name != "nt":
        yield Path("/")
        return

    preferred: list[str] = []
    for probe in (Path(__file__).resolve(), Path.cwd()):
        drive = probe.drive.rstrip(":").upper()
        if drive and drive not in preferred:
            preferred.append(drive)

    ordered = preferred + [c for c in string.ascii_uppercase if c not in preferred]
    for letter in ordered:
        root = Path(f"{letter}:/")
        try:
            if root.exists():
                yield root
        except OSError:
            # Unreadable / disconnected network drive — skip rather than raise.
            continue


def candidate_repo_roots(include_cwd: bool = True) -> list[Path]:
    """Plausible AitherOS checkout roots, most explicit first.

    Order: $AITHEROS_REPO, the cwd, home-relative names, then <drive>:/<name> for
    every drive root that exists. Existence is NOT checked here beyond the drive
    scan — callers apply their own marker test (a compose file, a module manifest,
    a script directory), because "is this a checkout" differs per call site.
    """
    out: list[Path] = []

    env = os.environ.get("AITHEROS_REPO")
    if env:
        out.append(Path(env))

    if include_cwd:
        out.append(Path.cwd())

    home = Path.home()
    for name in _REPO_DIR_NAMES:
        out.append(home / name)

    for root in _drive_roots():
        for name in _REPO_DIR_NAMES:
            out.append(root / name)

    # De-duplicate, preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p).rstrip("\\/").lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique
