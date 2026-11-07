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
import re
import subprocess
import sys
from pathlib import Path

from adk import _attention as att

_TOOLS_REL = Path("AitherOS") / "dev" / "tools"
#: The marker refresh_agent_tools.py writes into each snapshot's package dir.
_SNAPSHOT_MARKER = ".aither-snapshot.json"
#: The generated report indents local lines by two columns.
_WIDTH = 78
#: A fix text that is a shell command rather than prose.
_COMMAND = re.compile(r"^(python3?|adk|awgit|podman|wsl|pip)\s")


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
    # No hard-coded checkout path: this module ships on PyPI. A machine whose repo is
    # not beside the package sets AITHER_REPO_ROOT or writes ~/.aither/repo-root.
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


def _show_all() -> bool:
    """``adk doctor --all``: the generated intercept passes no args, so read argv."""
    return "--all" in sys.argv[1:]


def _lane_next(lines: "list[str]", lane: Path) -> str:
    """The lane doctor's own remedy (``Run: <cmd>``), else its full report command."""
    for ln in lines:
        m = re.search(r"\bRun:\s*(.+?)\s*$", ln)
        if m:
            cmd = m.group(1)
            if cmd.startswith("aither_doctor.py"):
                cmd = f"python {lane.parent / cmd}"
            return f"run `{cmd}`"
    return f"run `python {lane}`"


def _as_action(fix: str) -> str:
    """A chain ``fix:`` text as an action: a command gets ``run `...```, prose stays."""
    cmd = fix.strip().split("   #", 1)[0].strip()
    if _COMMAND.match(cmd):
        return att.run(cmd)
    return cmd


def _doctor_local() -> "list[str]":
    """Display lines for the generated doctor: the next action first, lists capped.

    Line 1 of this block is ``Next: ...`` (the chain's first broken link, else the
    lane's own remedy) or ``Nothing needs you``. Lane output is capped at five
    lines + ``+N more``; ``adk doctor --all`` shows everything unfitted.
    """
    show_all = _show_all()
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
    # Problems first: a FAIL line outranks the ok lines it would otherwise sit under.
    ranked = sorted(lines, key=lambda ln: 0 if re.search(r"\[(FAIL|WARN|DEAD)", ln) else 1)
    shown, hidden = att.cap(ranked, show_all=show_all)
    body = list(shown)
    if hidden:
        body.append(att.more_line(hidden, "adk doctor --all", indent="", width=_WIDTH))
    verdict = f"lane rc={proc.returncode}"
    chain = _chain_lines()
    if chain and chain[0].startswith("chain FIRST BROKEN") and len(chain) > 1:
        nxt = att.next_line(_as_action(chain[1].split("fix:", 1)[-1]), width=_WIDTH)
    elif proc.returncode:
        nxt = att.next_line(_lane_next(lines, lane), width=_WIDTH)
    else:
        nxt = att.next_line(None, width=_WIDTH)
    out = [nxt] + head + body + [verdict] + chain
    if show_all:
        return out
    return [out[0]] + [att.fit(ln, _WIDTH) for ln in out[1:]]


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
