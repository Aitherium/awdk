"""``adk fleet-host`` -- status and actions for the awnix fleet host (WSL2).

A thin shell over the AitherOS repo's ``AitherOS/dev/tools/fleet_host.py``: the
engine every surface shares (awdesk's Fleet window, awnode's MCP tools, the
AitherZero ``setup-awnix-fleet-host`` playbook), so this CLI cannot disagree with
them. awdk never imports AitherOS code (the wheel boundary); it runs the tool as
a subprocess from the repo found at ``$AITHEROS_ROOT``, the checkout this file
sits in, the CWD, or the shared checkout discovery (``adk.shell._repo_roots``).

    adk fleet-host status [--json]
    adk fleet-host start|stop|restart|reattach [--execute] [--force]
    adk fleet-host stop --terminate --execute
    adk fleet-host migrate [--mode preflight|rehearse|cutover] [--execute]

Every mutating verb is a DRY RUN unless ``--execute``.
Exit codes pass through: 0 ok · 1 unhealthy/failed · 2 could not judge.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

VERBS = ("status", "start", "stop", "restart", "reattach", "migrate")
TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_host.py"


def find_tool(
    env: Optional[dict] = None, here: Optional[Path] = None, cwd: Optional[Path] = None
) -> Optional[Path]:
    """Locate fleet_host.py; None when no AitherOS checkout is reachable."""
    e = os.environ if env is None else env
    h = (here or Path(__file__)).resolve()
    cands: List[Path] = []
    if e.get("AITHEROS_ROOT"):
        cands.append(Path(e["AITHEROS_ROOT"]))
    cands += [h.parents[i] for i in range(2, min(5, len(h.parents)))]
    cands.append(cwd or Path.cwd())
    try:
        from adk.shell._repo_roots import candidate_repo_roots

        cands += candidate_repo_roots(include_cwd=False)
    except Exception as exc:  # noqa: BLE001 - discovery is best effort
        print(f"note: checkout discovery skipped: {exc}", file=sys.stderr)
    for c in cands:
        if (c / TOOL_REL).is_file():
            return c / TOOL_REL
    return None


#: A summary older than this is not reported (it would read as current).
CACHE_MAX_AGE_S = 900


def cached_summary(now: Optional[float] = None) -> Optional[dict]:
    """The fleet-host summary fleet_host.py caches on every ``status``, or None.

    Cheap (one file read, never wsl.exe), so the enroll heartbeat can carry it:
    ``~/.aither/fleet-host-status.json`` (``$AITHER_HOME`` overrides the dir).
    """
    import json
    import time

    home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    try:
        doc = json.loads((home / "fleet-host-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    try:
        age = (now or time.time()) - float(doc.get("checked_epoch") or 0)
    except (TypeError, ValueError):
        return None
    if age > CACHE_MAX_AGE_S:
        return None
    return {
        k: doc.get(k) for k in ("distro", "verdict", "state", "containers_running", "checked_at")
    }


def build_argv(args: Any, tool: Path) -> List[str]:
    """The exact subprocess argv for parsed ``args``."""
    verb = getattr(args, "fleet_host_command", None) or "status"
    argv = [sys.executable, str(tool), verb]
    for flag in ("execute", "force", "terminate", "json"):
        if getattr(args, flag, False):
            argv.append(f"--{flag}")
    if verb == "migrate" and getattr(args, "mode", None):
        argv += ["--mode", args.mode]
    return argv


def cmd_fleet_host(args: Any) -> int:
    tool = find_tool()
    if tool is None:
        print(
            "fleet_host.py not found: run from the AitherOS repo or set AITHEROS_ROOT",
            file=sys.stderr,
        )
        return 2
    try:
        return subprocess.run(
            build_argv(args, tool), cwd=str(tool.parents[3]), timeout=7800, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        print("fleet_host.py timed out", file=sys.stderr)
        return 2


def register_parser(sub: Any) -> None:
    """Add ``adk fleet-host`` to the top-level subparsers."""
    p = sub.add_parser(
        "fleet-host",
        help="awnix fleet host (WSL2): status, start/stop/restart, "
        "reattach data, migrate (dry-run default)",
    )
    fsub = p.add_subparsers(dest="fleet_host_command")
    for verb in VERBS:
        v = fsub.add_parser(verb, help=f"fleet host {verb}")
        v.add_argument("--json", action="store_true")
        if verb != "status":
            v.add_argument("--execute", action="store_true", help="actually run (default: dry run)")
            v.add_argument(
                "--force",
                action="store_true",
                help="override the co-tenancy / maintenance-lock refusal",
            )
        if verb == "stop":
            v.add_argument("--terminate", action="store_true", help="also wsl --terminate")
        if verb == "migrate":
            v.add_argument(
                "--mode", default="preflight", choices=["preflight", "rehearse", "cutover"]
            )
