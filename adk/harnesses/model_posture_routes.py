"""``/model-postures`` on the harness daemon: host-side execution of ``awmodels``.

Genesis runs inside a Podman container with no systemd, no podman, and no root SSH key
to the DGX Spark. This module exposes the host checkout's ``awmodels.py`` tool
verbs on the host harness daemon (``:8362``) so Genesis can delegate live probes,
plans, and ordered posture switches across a clean security boundary without ever
copying host SSH keys or host transport credentials into a container.

Routes:
* ``GET  /model-postures/status``        -- ``awmodels.py status --json``
* ``GET  /model-postures/{posture}/plan`` -- ``awmodels.py plan <posture> --json``
* ``POST /model-postures/use``           -- ``awmodels.py use <posture> [--yes] --json``
                                            (owner principal only)

Same wheel boundary as :mod:`adk.harnesses.posture_routes`: ``awdk`` never imports
AitherOS code; it locates ``awmodels.py`` through :func:`adk.commands.fleet_host.find_tool`
and runs an explicit argv list with no shell. Posture names and skip hosts are strictly
allowlisted before any subprocess starts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

TOOL_REL = Path("AitherOS") / "dev" / "tools" / "awmodels.py"
POSTURE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SKIP_HOSTS = frozenset({"rtx5090", "spark"})
STATUS_TIMEOUT_S = 60.0
PLAN_TIMEOUT_S = 60.0
MIN_DEADLINE_S = 30.0
MAX_DEADLINE_S = 5400.0
DEFAULT_DEADLINE_S = 4500.0
BUSY_RC = 3

#: A detached daemon's console child opens a terminal tab that steals focus on Windows.
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_use_lock = threading.Lock()


def validate_posture_name(name: str) -> str:
    """Normalise and validate a posture name/alias; raises ValueError on invalid input."""
    cleaned = (name or "").strip().lower()
    if not POSTURE_NAME_RE.match(cleaned):
        raise ValueError(
            f"invalid posture name {name!r}; expected lowercase letters, digits, '-' or '_'"
        )
    return cleaned


def validate_skip(skip: Sequence[str] | None) -> List[str]:
    """Validate optional ``--skip`` host list against :data:`SKIP_HOSTS`."""
    if not skip:
        return []
    out: List[str] = []
    for raw in skip:
        host = str(raw or "").strip().lower()
        if not host:
            continue
        if host not in SKIP_HOSTS:
            raise ValueError(f"unknown skip host {raw!r}; allowed: {', '.join(sorted(SKIP_HOSTS))}")
        if host not in out:
            out.append(host)
    return out


def validate_deadline(deadline_s: Any) -> float:
    """Validate ``deadline_s`` bounds; raises ValueError on bad numbers or bools."""
    if deadline_s is None:
        return DEFAULT_DEADLINE_S
    if isinstance(deadline_s, bool):
        raise ValueError("deadline_s must be a number, not boolean")
    try:
        val = float(deadline_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"deadline_s must be a number, got {deadline_s!r}") from exc
    if not (MIN_DEADLINE_S <= val <= MAX_DEADLINE_S):
        raise ValueError(
            f"deadline_s must be between {MIN_DEADLINE_S:.0f} and {MAX_DEADLINE_S:.0f} seconds"
        )
    return val


def build_status_argv(tool: Path, skip: Sequence[str] = (),
                      python: Optional[str] = None) -> List[str]:
    argv = [python or sys.executable, str(tool), "status", "--json"]
    for host in validate_skip(skip):
        argv += ["--skip", host]
    return argv


def build_plan_argv(posture: str, tool: Path, offline: bool = False,
                    python: Optional[str] = None) -> List[str]:
    name = validate_posture_name(posture)
    argv = [python or sys.executable, str(tool), "plan", name, "--json"]
    if offline:
        argv.append("--offline")
    return argv


def build_use_argv(
    posture: str,
    tool: Path,
    *,
    dry_run: bool = False,
    wait: bool = True,
    deadline_s: float = DEFAULT_DEADLINE_S,
    skip: Sequence[str] = (),
    python: Optional[str] = None,
) -> List[str]:
    name = validate_posture_name(posture)
    valid_skip = validate_skip(skip)
    valid_deadline = validate_deadline(deadline_s)
    argv = [python or sys.executable, str(tool), "use", name, "--json"]
    if not dry_run:
        argv.append("--yes")
        if not wait:
            argv.append("--no-wait")
        argv += ["--deadline", f"{valid_deadline:.0f}"]
        for host in valid_skip:
            argv += ["--skip", host]
    return argv


def _try_json_dict(raw: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse JSON stdout: the whole body, else the last JSON object block/line."""
    text = (stdout or "").strip()
    if not text:
        return None
    whole = _try_json_dict(text)
    if whole is not None:
        return whole
    idx = text.rfind("\n{")
    if idx != -1:
        block = _try_json_dict(text[idx + 1:])
        if block is not None:
            return block
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            row = _try_json_dict(line)
            if row is not None:
                return row
    return None


def find_tool() -> Optional[Path]:
    from adk.commands.fleet_host import find_tool as _find

    return _find(rel=TOOL_REL)


def _run_cli(argv: List[str], tool: Path, timeout: float,
             popen: Any = subprocess.Popen) -> tuple[int, str, str]:
    proc = popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tool.parents[3]),
        creationflags=_NOWIN,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 124, "", f"awmodels.py timed out after {timeout:.0f}s"


def _missing_tool_error() -> str:
    return (
        f"no AitherOS checkout with {TOOL_REL.as_posix()} on this host "
        "(set AITHEROS_ROOT)"
    )


def run_status(skip: Sequence[str] = (), tool: Optional[Path] = None,
               popen: Any = subprocess.Popen) -> Dict[str, Any]:
    """Run ``awmodels.py status --json`` on the host."""
    argv_tool = tool or find_tool()
    if argv_tool is None:
        return {"ok": False, "rc": 2, "status": None, "error": _missing_tool_error()}
    argv_tool = Path(argv_tool)
    argv = build_status_argv(argv_tool, skip=skip)
    rc, stdout, stderr = _run_cli(argv, argv_tool, STATUS_TIMEOUT_S, popen=popen)
    doc = _parse_json(stdout)
    out: Dict[str, Any] = {"ok": doc is not None, "rc": rc, "status": doc}
    if doc is None:
        out["error"] = (stderr or "").strip()[-400:] or "awmodels.py status printed no JSON"
    return out


def run_plan(posture: str, offline: bool = False, tool: Optional[Path] = None,
             popen: Any = subprocess.Popen) -> Dict[str, Any]:
    """Run ``awmodels.py plan <posture> --json`` on the host."""
    argv_tool = tool or find_tool()
    if argv_tool is None:
        return {"ok": False, "rc": 2, "plan": None, "error": _missing_tool_error()}
    argv_tool = Path(argv_tool)
    argv = build_plan_argv(posture, argv_tool, offline=offline)
    rc, stdout, stderr = _run_cli(argv, argv_tool, PLAN_TIMEOUT_S, popen=popen)
    doc = _parse_json(stdout)
    out: Dict[str, Any] = {"ok": doc is not None, "rc": rc, "plan": doc}
    if doc is None:
        out["error"] = (stderr or "").strip()[-400:] or "awmodels.py plan printed no JSON"
    return out


def run_use(
    posture: str,
    *,
    dry_run: bool = False,
    wait: bool = True,
    deadline_s: float = DEFAULT_DEADLINE_S,
    skip: Sequence[str] = (),
    tool: Optional[Path] = None,
    popen: Any = subprocess.Popen,
) -> Dict[str, Any]:
    """Run ``awmodels.py use <posture> [--yes] --json`` on the host."""
    argv_tool = tool or find_tool()
    if argv_tool is None:
        return {
            "ok": False,
            "rc": 2,
            "posture": posture,
            "dry_run": dry_run,
            "applied": False,
            "busy": False,
            "steps": [],
            "log": [],
            "error": _missing_tool_error(),
        }
    argv_tool = Path(argv_tool)
    valid_deadline = validate_deadline(deadline_s)
    argv = build_use_argv(
        posture,
        argv_tool,
        dry_run=dry_run,
        wait=wait,
        deadline_s=valid_deadline,
        skip=skip,
    )
    timeout = PLAN_TIMEOUT_S if dry_run else (valid_deadline + 60.0)
    rc, stdout, stderr = _run_cli(argv, argv_tool, timeout, popen=popen)
    doc = _parse_json(stdout)
    if doc is None:
        return {
            "ok": False,
            "rc": rc,
            "posture": posture,
            "dry_run": dry_run,
            "applied": False,
            "busy": rc == BUSY_RC,
            "steps": [],
            "log": [],
            "error": (stderr or "").strip()[-400:] or "awmodels.py use printed no JSON",
        }
    return {
        "ok": True,
        "rc": rc,
        "posture": str(doc.get("posture") or posture),
        "dry_run": bool(doc.get("dry_run", dry_run)),
        "applied": bool(doc.get("applied", False)),
        "busy": bool(doc.get("busy", rc == BUSY_RC)),
        "steps": list(doc.get("steps") or []),
        "log": list(doc.get("log") or []),
        **({"fit": doc["fit"]} if "fit" in doc else {}),
        **({"active": doc["active"]} if "active" in doc else {}),
    }


def mount(app: Any, auth: Any) -> None:
    """Mount ``/model-postures/*`` routes onto the harness daemon FastAPI app."""
    from fastapi import Body, Depends, HTTPException, Query

    @app.get("/model-postures/status")
    def model_postures_status(
        skip: List[str] = Query(default=[]),
        principal: Any = Depends(auth),
    ) -> Dict[str, Any]:
        del principal  # auth already verified bearer + path scope
        try:
            valid_skip = validate_skip(skip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        res = run_status(skip=valid_skip)
        if not res.get("ok"):
            raise HTTPException(status_code=503, detail=res.get("error") or "status probe failed")
        return res

    @app.get("/model-postures/{posture}/plan")
    def model_postures_plan(
        posture: str,
        offline: bool = False,
        principal: Any = Depends(auth),
    ) -> Dict[str, Any]:
        del principal
        try:
            name = validate_posture_name(posture)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        res = run_plan(name, offline=offline)
        if not res.get("ok"):
            err = str(res.get("error") or "plan failed")
            if "unknown posture" in err.lower():
                raise HTTPException(status_code=404, detail=err)
            raise HTTPException(status_code=503, detail=err)
        return res

    @app.post("/model-postures/use")
    def model_postures_use(
        body: Dict[str, Any] = Body(...),
        principal: Any = Depends(auth),
    ) -> Dict[str, Any]:
        if getattr(principal, "plan", "") != "owner":
            raise HTTPException(
                status_code=403,
                detail="model posture switches require the owner principal",
            )
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        try:
            name = validate_posture_name(str(body.get("posture") or ""))
            valid_skip = validate_skip(body.get("skip") or [])
            valid_deadline = validate_deadline(body.get("deadline_s"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        dry_run = bool(body.get("dry_run", False))
        wait = bool(body.get("wait", True))
        if dry_run:
            res = run_use(
                name,
                dry_run=True,
                wait=False,
                deadline_s=valid_deadline,
                skip=valid_skip,
            )
            if not res.get("ok"):
                err = str(res.get("error") or "dry-run failed")
                if "unknown posture" in err.lower():
                    raise HTTPException(status_code=404, detail=err)
                raise HTTPException(status_code=503, detail=err)
            return res
        if not _use_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a model posture switch is already running")
        try:
            res = run_use(
                name,
                dry_run=False,
                wait=wait,
                deadline_s=valid_deadline,
                skip=valid_skip,
            )
        finally:
            _use_lock.release()
        if not res.get("ok"):
            err = str(res.get("error") or "posture switch failed")
            if "unknown posture" in err.lower():
                raise HTTPException(status_code=404, detail=err)
            raise HTTPException(status_code=503, detail=err)
        return res
