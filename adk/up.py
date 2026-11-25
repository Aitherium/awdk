"""``adk up`` — one command to bring up the Aither World local harness stack.

Detects installed ``aw*`` bricks, ensures the local runtime daemons (``hookd`` :8369,
``awsh`` harness daemon :8362, ``aw-hub`` :47933 when ``awnode`` is installed),
auto-wires detected coding agents (Google Antigravity, Claude Code) via their own
``setup`` / ``doctor`` doors, and prints a live status report.

Usage:
    adk up                      # check bricks, start daemons, wire agents, verify
    adk up --status             # read-only live probe (starts/writes nothing)
    adk up --dry-run            # show what would start and wire; touch nothing
    adk up --down               # stop daemons started by `adk up`
    adk up --repo /path/to/repo # also wire workspace hooks (.agents/hooks.json)
    adk up --json               # machine-readable report
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

CORE_BRICKS: tuple[tuple[str, str], ...] = (
    ("awdk", "pip install awdk"),
    ("awgit", "pip install awgit"),
    ("awgraph", "pip install awgraph"),
    ("awrelay", "pip install awrelay"),
    ("awsettings", "pip install awsettings"),
    ("awnode", "pip install awnode"),
    ("awm", "pip install awm"),
    ("awfocus", "pip install awfocus"),
    ("awask", "pip install awask"),
    ("awdecide", "pip install awdecide"),
)

DAEMON_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "hookd",
        "label": "hookd (in-process hook & awgov PDP runner)",
        "health_url": "http://127.0.0.1:8369/health",
        "pid_file": "hookd.pid",
        "optional": False,
    },
    {
        "id": "awsh",
        "label": "awsh (multi-harness session daemon)",
        "health_url": "http://127.0.0.1:8362/health",
        "pid_file": "awsh-daemon.pid",
        "optional": False,
    },
    {
        "id": "aw-hub",
        "label": "aw-hub (shared aw* brick MCP hub)",
        "health_url": "http://127.0.0.1:47933/health",
        "pid_file": "aw-hub.pid",
        "optional": True,  # requires awnode
    },
    {
        "id": "gateway",
        "label": "AitherOS gateway (optional fleet MCP)",
        "health_url": "http://127.0.0.1:8182/health",
        "pid_file": "",
        "optional": True,
        "read_only": True,
    },
)


def _aither_home() -> Path:
    raw = os.environ.get("AITHER_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".aither"


def _run_dir() -> Path:
    d = _aither_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_http(url: str, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (OSError, urllib.error.URLError) as exc:
        return False, str(getattr(exc, "reason", exc))[:80]


def check_bricks() -> list[dict[str, Any]]:
    """Inspect which aw* bricks are installed on this machine."""
    rows: list[dict[str, Any]] = []
    for name, install_hint in CORE_BRICKS:
        cli_path = shutil.which(name) or (shutil.which("adk") if name == "awdk" else None)
        version = ""
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
        mod_present = bool(importlib.util.find_spec("adk" if name == "awdk" else name))
        installed = bool(cli_path or version or mod_present)
        rows.append(
            {
                "id": name,
                "installed": installed,
                "version": version,
                "path": cli_path or "",
                "install_hint": "" if installed else install_hint,
            }
        )
    return rows


def _spawn_detached(argv: list[str], pid_path: Optional[Path] = None) -> tuple[bool, str]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed internal argv
        if pid_path is not None:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")
        return True, f"spawned pid {proc.pid}"
    except OSError as exc:
        return False, str(exc)


def _wait_health(url: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    last = "not answering"
    while time.monotonic() < deadline:
        ok, detail = probe_http(url, timeout=1.0)
        if ok:
            return True, detail
        last = detail
        time.sleep(0.25)
    return False, last


def _daemon_start_argv(daemon_id: str) -> Optional[list[str]]:
    py = sys.executable
    if daemon_id == "hookd":
        hookd_py = Path(__file__).resolve().parent / "harnesses" / "hookd.py"
        return [py, "-S", str(hookd_py), "ensure"]
    if daemon_id == "awsh":
        return [py, "-m", "adk.harnesses.daemon", "--host", "127.0.0.1", "--port", "8362"]
    if daemon_id == "aw-hub":
        if not importlib.util.find_spec("awnode"):
            return None
        return [py, "-m", "awnode.hub", "serve", "--port", "47933"]
    return None


def ensure_daemons(
    *,
    dry_run: bool = False,
    status_only: bool = False,
    probe=probe_http,
) -> list[dict[str, Any]]:
    """Probe and (unless dry_run/status_only) start local harness daemons."""
    results: list[dict[str, Any]] = []
    for spec in DAEMON_SPECS:
        did = spec["id"]
        url = spec["health_url"]
        ok, detail = probe(url)
        row: dict[str, Any] = {
            "id": did,
            "label": spec["label"],
            "url": url,
            "optional": bool(spec.get("optional")),
            "up": ok,
            "action": "already-up" if ok else "down",
            "detail": detail,
        }
        if ok or status_only or spec.get("read_only"):
            results.append(row)
            continue

        argv = _daemon_start_argv(did)
        if argv is None:
            row["action"] = "skipped"
            row["detail"] = "not installed on this host"
            results.append(row)
            continue

        if dry_run:
            row["action"] = "would-start"
            row["detail"] = " ".join(argv)
            results.append(row)
            continue

        if did == "hookd":
            try:
                subprocess.run(argv, capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                row["action"] = "failed"
                row["detail"] = str(exc)
                results.append(row)
                continue
            up_now, det = _wait_health(url, timeout_s=4.0)
            row["up"] = up_now
            row["action"] = "started" if up_now else "failed"
            row["detail"] = det
            results.append(row)
            continue

        pid_path = _run_dir() / spec["pid_file"] if spec.get("pid_file") else None
        spawned, spawn_detail = _spawn_detached(argv, pid_path)
        if not spawned:
            row["action"] = "failed"
            row["detail"] = spawn_detail
            results.append(row)
            continue
        up_now, det = _wait_health(url, timeout_s=5.0)
        row["up"] = up_now
        row["action"] = "started" if up_now else "starting"
        row["detail"] = det if up_now else f"{spawn_detail}; {det}"
        results.append(row)
    return results


def stop_daemons(*, dry_run: bool = False) -> list[dict[str, Any]]:
    """Stop daemons whose PID files were written by ``adk up``."""
    out: list[dict[str, Any]] = []
    rdir = _aither_home() / "run"
    for spec in DAEMON_SPECS:
        pf = spec.get("pid_file")
        if not pf:
            continue
        path = rdir / pf
        if not path.is_file():
            out.append({"id": spec["id"], "action": "not-managed", "pid": None})
            continue
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            out.append({"id": spec["id"], "action": "invalid-pid", "pid": None})
            continue
        if dry_run:
            out.append({"id": spec["id"], "action": "would-stop", "pid": pid})
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            path.unlink(missing_ok=True)
            out.append({"id": spec["id"], "action": "stopped", "pid": pid})
        except OSError as exc:
            path.unlink(missing_ok=True)
            out.append({"id": spec["id"], "action": "already-stopped", "pid": pid, "detail": str(exc)})
    return out


def detect_and_wire_agents(
    *,
    dry_run: bool = False,
    status_only: bool = False,
    repo_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Detect installed coding agents and wire (or audit) their awdk plugins."""
    rows: list[dict[str, Any]] = []
    home = Path.home()

    # 1. Google Antigravity
    ag_cfg = Path(os.environ.get("ANTIGRAVITY_CONFIG_DIR", "").strip() or (home / ".gemini" / "config"))
    ag_present = ag_cfg.is_dir() or (home / ".gemini" / "antigravity").is_dir()
    if ag_present:
        from adk.antigravity_setup import doctor_antigravity, setup_antigravity

        if status_only or dry_run:
            rep = doctor_antigravity(ag_cfg, run_live=False)
            rows.append(
                {
                    "agent": "antigravity",
                    "detected": True,
                    "action": "checked" if status_only else "would-setup",
                    "config_dir": str(ag_cfg),
                    "doctor_exit": rep.exit_code,
                    "violations": [f.code for f in rep.findings if f.severity == "violation"],
                }
            )
        else:
            res = setup_antigravity(ag_cfg, repo_root=repo_root)
            rep = doctor_antigravity(ag_cfg, run_live=True)
            rows.append(
                {
                    "agent": "antigravity",
                    "detected": True,
                    "action": "wired",
                    "config_dir": str(ag_cfg),
                    "plugin_dir": res["plugin_dir"],
                    "skills_count": res["skills_count"],
                    "doctor_exit": rep.exit_code,
                    "violations": [f.code for f in rep.findings if f.severity == "violation"],
                }
            )
    else:
        rows.append({"agent": "antigravity", "detected": False, "action": "absent"})

    # 2. Claude Code
    claude_present = (home / ".claude").is_dir() or bool(shutil.which("claude"))
    if claude_present:
        try:
            from adk.harnesses import mod as claude_mod

            st = claude_mod.status()
            rows.append(
                {
                    "agent": "claude-code",
                    "detected": True,
                    "action": "active" if st.get("active") else "detected (run `adk claude setup`)",
                    "plugin_version": st.get("plugin_version") or "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"agent": "claude-code", "detected": True, "action": "error", "detail": str(exc)})
    else:
        rows.append({"agent": "claude-code", "detected": False, "action": "absent"})

    return rows


def detect_harnesses() -> list[dict[str, Any]]:
    try:
        from adk.harnesses.registry import detect

        return [
            {"id": r["id"], "label": r["label"], "installed": bool(r["installed"]), "transport": r["transport"]}
            for r in detect(with_version=False)
        ]
    except Exception:  # noqa: BLE001
        return []


def run_up(
    *,
    dry_run: bool = False,
    status_only: bool = False,
    down: bool = False,
    no_daemons: bool = False,
    no_agents: bool = False,
    repo_root: Optional[Path] = None,
) -> dict[str, Any]:
    if down:
        stopped = stop_daemons(dry_run=dry_run)
        return {"ok": True, "mode": "down", "dry_run": dry_run, "daemons": stopped}

    bricks = check_bricks()
    daemons = [] if no_daemons else ensure_daemons(dry_run=dry_run, status_only=status_only)
    agents = [] if no_agents else detect_and_wire_agents(
        dry_run=dry_run, status_only=status_only, repo_root=repo_root
    )
    harnesses = detect_harnesses()

    failed_daemons = [d["id"] for d in daemons if not d["optional"] and d["action"] == "failed"]
    agent_violations = [
        f"{a['agent']}:{','.join(a.get('violations') or [])}"
        for a in agents
        if a.get("action") == "wired" and a.get("doctor_exit", 0) != 0
    ]
    ok = not failed_daemons and not agent_violations
    return {
        "ok": ok,
        "mode": "status" if status_only else ("dry-run" if dry_run else "up"),
        "bricks": bricks,
        "daemons": daemons,
        "agents": agents,
        "harnesses_installed": [h["id"] for h in harnesses if h["installed"]],
        "failed_daemons": failed_daemons,
        "agent_violations": agent_violations,
    }


def _print_report(rep: dict[str, Any]) -> None:
    if rep.get("mode") == "down":
        print("adk up --down:")
        for d in rep.get("daemons") or []:
            print(f"  {d['id']:10s} {d['action']} (pid={d.get('pid')})")
        return

    print(f"awdk 4.0 stack ({rep['mode']}):")
    bricks = rep.get("bricks") or []
    present = [b for b in bricks if b["installed"]]
    missing = [b for b in bricks if not b["installed"]]
    print(f"  bricks    : {len(present)}/{len(bricks)} installed ({', '.join(b['id'] for b in present)})")
    if missing:
        print(f"              optional missing: {', '.join(b['id'] for b in missing)}")
    for d in rep.get("daemons") or []:
        tag = "UP  " if d["up"] else ("OPT " if d["optional"] else "DOWN")
        print(f"  daemon    : [{tag}] {d['id']:10s} {d['action']:12s} {d['detail']}")
    for a in rep.get("agents") or []:
        if not a.get("detected"):
            continue
        extra = f" (skills={a['skills_count']}, doctor={a['doctor_exit']})" if "skills_count" in a else ""
        print(f"  agent     : {a['agent']:12s} {a['action']}{extra}")
    installed_h = rep.get("harnesses_installed") or []
    print(f"  harnesses : {', '.join(installed_h) if installed_h else '(none)'}")
    print(f"  verdict   : {'READY' if rep['ok'] else 'DEGRADED'}")


def add_parser(subparsers: Any) -> Any:
    p = subparsers.add_parser(
        "up",
        help="Bring up the local Aither World harness stack (bricks, daemons, agent plugins)",
    )
    p.add_argument("--status", action="store_true", help="Read-only health probe; start or write nothing")
    p.add_argument("--dry-run", action="store_true", help="Print plan without starting daemons or editing files")
    p.add_argument("--down", action="store_true", help="Stop daemons started by `adk up`")
    p.add_argument("--no-daemons", action="store_true", help="Skip starting/checking local daemons")
    p.add_argument("--no-agents", action="store_true", help="Skip auto-wiring coding agent plugins")
    p.add_argument("--repo", default="", help="Also wire workspace hooks in this repository")
    p.add_argument("--json", dest="json_output", action="store_true")
    return p


def cmd_up(args: Any) -> int:
    repo = Path(args.repo) if getattr(args, "repo", "") else None
    rep = run_up(
        dry_run=bool(getattr(args, "dry_run", False)),
        status_only=bool(getattr(args, "status", False)),
        down=bool(getattr(args, "down", False)),
        no_daemons=bool(getattr(args, "no_daemons", False)),
        no_agents=bool(getattr(args, "no_agents", False)),
        repo_root=repo,
    )
    if getattr(args, "json_output", False):
        print(json.dumps(rep, indent=2))
    else:
        _print_report(rep)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="adk-up")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--down", action="store_true")
    ap.add_argument("--no-daemons", action="store_true")
    ap.add_argument("--no-agents", action="store_true")
    ap.add_argument("--repo", default="")
    ap.add_argument("--json", dest="json_output", action="store_true")
    raise SystemExit(cmd_up(ap.parse_args()))
