#!/usr/bin/env python3
"""Identify (and prove) the published awdk releases that leak the moat.

Every release before 2.0.0 predates the distribution-time moat exclusion and
ships ``adk/nanogpt.py`` plus ungated fleet/forge/channels/cron — i.e. the
proprietary capabilities the open-core split is meant to fence off. They should
be **yanked** (recommended) or deleted on PyPI.

PyPI has no public API/CLI for yank or delete — it is a deliberate,
authenticated, irreversible action performed by a project owner. This tool
therefore:

  * lists which published versions are leaky (default), and
  * with ``--verify``, downloads each wheel and runs the moat guard to *prove*
    the leak (no guessing), and
  * prints the exact "Manage" URLs + a checklist for the human to action.

Usage:
    python scripts/yank_leaky_releases.py            # list leaky versions
    python scripts/yank_leaky_releases.py --verify   # download + prove each leak
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
import zipfile

PROJECT = "awdk"
CUTOFF = (2, 0, 0)  # first release with the moat exclusion
MANAGE_URL = f"https://pypi.org/manage/project/{PROJECT}/releases/"


def _version_tuple(v: str) -> tuple:
    parts = []
    for chunk in v.replace("a", ".").replace("b", ".").replace("rc", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _fetch_releases() -> dict:
    url = f"https://pypi.org/pypi/{PROJECT}/json"
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 (trusted host)
        return json.load(resp)["releases"]


def _wheel_leaks(url: str) -> list[str]:
    """Download a wheel and return the moat violations it contains."""
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
        blob = resp.read()
    leaks: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if name.endswith("adk/nanogpt.py"):
                leaks.append("nanogpt.py (training IP)")
            if "/identities/" in name and name.endswith(".yaml") and not name.endswith("aither.yaml"):
                leaks.append(f"premium identity {name.rsplit('/', 1)[-1]}")
            if name.startswith("aither_adk/"):
                leaks.append("internal moat package")
    return sorted(set(leaks))


def main(argv: list[str]) -> int:
    verify = "--verify" in argv
    releases = _fetch_releases()

    leaky = sorted(
        (v for v in releases if _version_tuple(v) < CUTOFF),
        key=_version_tuple,
    )

    print(f"{PROJECT}: {len(releases)} published versions, "
          f"{len(leaky)} predate the 2.0.0 moat exclusion.\n")

    if verify:
        print("Verifying leaks (downloading wheels)…\n")
        for v in leaky:
            files = releases[v]
            wheel = next((f for f in files if f["filename"].endswith(".whl")), None)
            if not wheel:
                print(f"  {v}: (no wheel — sdist only)")
                continue
            try:
                leaks = _wheel_leaks(wheel["url"])
                tag = ", ".join(leaks) if leaks else "clean?!"
                print(f"  {v}: {tag}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {v}: <download failed: {exc}>")
        print()

    print("LEAKY VERSIONS TO YANK/DELETE:")
    print("  " + ", ".join(leaky))
    print()
    print("HOW TO REMOVE (owner action — requires PyPI login + 2FA):")
    print(f"  1. Open {MANAGE_URL}")
    print("  2. For each version above: Options → 'Yank' (recommended; keeps")
    print("     pinned installs working but hides from new resolves) with reason:")
    print("     'Leaks proprietary modules; superseded by 2.0.0 (open-core).'")
    print("  3. Or 'Delete' for permanent removal (breaks pinned installs;")
    print("     version numbers cannot be reused). Prefer Yank unless legally required.")
    print()
    print("  Note: PyPI provides no API/CLI for this — it must be done in the UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
