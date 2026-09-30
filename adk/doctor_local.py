"""adk-specific runtime checks for the generated `adk doctor`.

The generated _doctor.py reports the aw* stack; this module is the
package-supplied hook for checks only adk can own. The one that matters on
this box is the CLAUDE LANE: settings.json, the profile matcher, the
MicroScheduler endpoint, the credential ladder, presence freshness and git
index parity — the exact failure signature of the 2026-08-25 lane-flip
incident (profile flipped to 'anthropic', CLI lost the [1m] window, recovery
needed a hand-bridged key). The engine is aither_doctor.py in this repo; adk
shells out to it rather than duplicating the checks, so the lane diagnosis
and the unattended host probe (HOST_ONLY_GATES) can never drift apart.

DISPLAY-ONLY by contract: _doctor.report() prints these lines but its exit
code is the stack verdict, not the lane's. The lane's exit code pages via
run_fleet_gates_from_host.HOST_ONLY_GATES, which runs aither_doctor directly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_TOOLS_REL = Path("AitherOS") / "dev" / "tools"
#: The marker refresh_agent_tools.py writes into each snapshot's package dir.
_SNAPSHOT_MARKER = ".aither-snapshot.json"
_MAX_LINES = 12


def _repo_root_candidates() -> "list[tuple[str, Path]]":
    """Where the repo holding AitherOS/dev/tools may be, in precedence order.

    parents[2] alone was right only for an in-repo install. A snapshot install
    (refresh_agent_tools.py archives JUST awdk/) has no AitherOS/ beside it, so
    the lane check went silently dead (measured 2026-09-30, D-36).
    """
    out = []
    env = os.environ.get("AITHER_REPO_ROOT", "").strip()
    if env:
        out.append(("AITHER_REPO_ROOT", Path(env)))
    here = Path(__file__).resolve()
    out.append(("package checkout", here.parents[2]))
    try:
        marker = json.loads((here.parents[1] / _SNAPSHOT_MARKER).read_text(encoding="utf-8"))
        if marker.get("repo_root"):
            out.append(("snapshot marker", Path(marker["repo_root"])))
    except (OSError, ValueError, AttributeError):
        pass
    home = Path.home() / ".aither" / "repo-root"
    try:
        if home.is_file():
            out.append(("~/.aither/repo-root", Path(home.read_text(encoding="utf-8").strip())))
    except OSError:
        pass
    if sys.platform == "win32":
        out.append(("default checkout", Path("C:/AitherOS-Fresh")))
    return out


def _find_repo_tool(name: str) -> "tuple[Path | None, str, list[Path]]":
    """(path, source, tried) of the first candidate that holds ``name``."""
    tried = []
    for source, root in _repo_root_candidates():
        cand = root / _TOOLS_REL / name
        tried.append(cand)
        if cand.is_file():
            return cand, source, tried
    return None, "", tried


def _doctor_local() -> "list[str]":
    lane, source, tried = _find_repo_tool("aither_doctor.py")
    if lane is None:
        return [f"lane doctor NOT FOUND (tried {', '.join(str(t) for t in tried)}) "
                "— set AITHER_REPO_ROOT=<repo root> or re-run "
                "refresh_agent_tools.py adk"]
    head = [f"lane doctor: {lane} (via {source})"]
    try:
        proc = subprocess.run(
            [sys.executable, str(lane)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot run reports that
        return head + [f"lane doctor could not run: {type(exc).__name__}: {exc}"]
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    verdict = f"lane rc={proc.returncode}"
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES] + ["…  (full report: aither_doctor.py)"]
    return head + lines + [verdict] + _chain_lines()


def _chain_lines() -> "list[str]":
    """The platform chain's FIRST broken link (aither_chain_doctor.py), one sentence + fix.

    The lane above answers "is Claude Code wired"; this answers "why did my call get a
    bare 401/502/524" -- the distro, data disk, GPU, Identity, bearer-vs-Veil, edge,
    daemons and posture, in dependency order.
    """
    chain, _source, tried = _find_repo_tool("aither_chain_doctor.py")
    if chain is None:
        return [f"chain doctor NOT FOUND (tried {len(tried)} location(s)) "
                "— set AITHER_REPO_ROOT=<repo root>"]
    try:
        proc = subprocess.run(
            [sys.executable, str(chain), "--json"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240)
        doc = json.loads(proc.stdout or "{}")
    except Exception as exc:  # noqa: BLE001 - a doctor that cannot run reports that
        return [f"chain doctor could not run: {type(exc).__name__}: {exc}"]
    fb = doc.get("first_broken")
    if not fb:
        return [f"chain rc={doc.get('exit')}: "
                + ("every link answers" if doc.get("exit") == 0
                   else "a link could not be judged (aither_chain_doctor.py)")]
    return [f"chain FIRST BROKEN {fb.get('id')} {fb.get('name')}: {fb.get('sentence')}",
            f"  fix: {fb.get('fix')}", f"chain rc={doc.get('exit')}"]
