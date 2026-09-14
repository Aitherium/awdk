#!/usr/bin/env python3
"""Detect and remove orphan `aither` console_script binaries left behind by
older awdk installs.

Why this exists:
  pip install --force-reinstall does NOT remove .exe wrappers that the new
  version of a package no longer defines. So if awdk@0.16.0 once shipped
  `aither = adk.cli:main`, the `aither.exe` wrapper persists in Scripts/ even
  after upgrading to 0.17.0+ (which dropped that entry). That orphan then
  shadows the real `aither` REPL from the npm @aitheros/shell-cli package.

Usage:
  python scripts/check_no_aither_clobber.py            # report only
  python scripts/check_no_aither_clobber.py --fix      # delete orphans

Exit codes:
  0 = clean (no orphans)
  1 = orphans found (use --fix to remove)
"""
from __future__ import annotations

import argparse
import shutil
import sys
import sysconfig
from pathlib import Path


def find_scripts_dir() -> Path:
    """Locate the active Python's Scripts/ (Windows) or bin/ (POSIX)."""
    return Path(sysconfig.get_paths()["scripts"])


def find_orphans(scripts_dir: Path) -> list[Path]:
    """Find aither* binaries that don't belong to any current Python package.

    Allowed (registered by current packages):
      - aither-py.exe / aither-py-script.py  (aithershell)
      - adk*, adk-bug*, adk-serve*           (awdk)

    Anything else matching `aither*` from Python Scripts/ is suspect.
    """
    allowed_stems = {"aither-py"}  # add any future legitimate Python `aither-*` here
    orphans: list[Path] = []
    if not scripts_dir.exists():
        return orphans
    for p in scripts_dir.glob("aither*"):
        # Skip awnode/aitheros (separate AitherOS packages)
        if p.stem.startswith(("awnode", "aitheros", "aither-desktop")):
            continue
        # Skip allowed Python wrappers
        if p.stem in allowed_stems or p.stem.removesuffix("-script") in allowed_stems:
            continue
        # The npm shell-cli lives under %APPDATA%\npm, not Python Scripts/.
        # ANY `aither` binary in Python Scripts/ is an orphan.
        orphans.append(p)
    return orphans


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="Delete orphans")
    args = ap.parse_args()

    scripts_dir = find_scripts_dir()
    orphans = find_orphans(scripts_dir)

    if not orphans:
        print(f"OK: no orphan aither* binaries in {scripts_dir}")
        return 0

    print(f"FOUND {len(orphans)} orphan(s) in {scripts_dir}:")
    for p in orphans:
        print(f"  - {p.name}")

    if not args.fix:
        print("\nRun with --fix to delete. The `aither` command should resolve to")
        print("the npm @aitheros/shell-cli REPL, not a Python wrapper.")
        return 1

    for p in orphans:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"  deleted: {p.name}")
        except OSError as exc:
            print(f"  FAILED to delete {p.name}: {exc}", file=sys.stderr)
            return 2
    print("\nCleanup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
