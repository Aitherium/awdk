"""Enrolment installs this device's file-index job -- no per-device setup.

Owner ask 2026-11-07: every device onboarded to the mesh keeps its files indexed in
Strata (MCP ``storage_files_*``, the awdesk Strata page, awsh) automatically. The index
itself is the ``awstorage`` brick; this module only makes sure it is installed and that
its recurring ``files sync`` is registered, right after ``adk pair`` / ``adk enroll``
registered the device (so the job indexes under the REGISTERED node id and pushes with
the device's own token -- ``awstorage files schedule`` refuses an unenrolled box).

Install ladder: ``awstorage`` importable by THIS interpreter -> ``pip install`` into it
-> when that interpreter is PEP 668 "externally managed" (or pip is refused), a private
venv at ``~/.aither/venvs/awstorage``. The schedule (awrise wake, else Task Scheduler /
systemd user timer / launchd / cron) and the index directory (off a low system drive)
are ``awstorage files schedule``'s decisions, not this module's.

Opt out with ``--no-autostart`` (the same flag that skips the heartbeat autostart) or
``AITHER_NO_FILE_INDEX=1``. Never raises: the outcome is ONE line enrolment prints, and a
failure names what failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

#: The first awstorage with `files schedule` / `files sync` and the node-token push.
REQUIREMENT = "awstorage>=0.8.0"


def venv_dir() -> Path:
    return Path.home() / ".aither" / "venvs" / "awstorage"


def _venv_python(d: Path) -> Path:
    return d / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(argv: list, **kw: Any) -> Any:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 600)
    return subprocess.run(argv, **kw)  # noqa: S603 - fixed argv, no shell


def _has_schedule(run: Callable[..., Any], py: str) -> bool:
    r = run([py, "-c", "import awstorage.autoindex"])
    return getattr(r, "returncode", 1) == 0


def _out(r: Any) -> str:
    return f"{getattr(r, 'stdout', '') or ''}\n{getattr(r, 'stderr', '') or ''}"


def ensure_awstorage(run: Optional[Callable[..., Any]] = None,
                     python: Optional[str] = None) -> Dict[str, str]:
    """{"python": interpreter that has awstorage, "how": ...} or {"python": "", "detail"}."""
    run = run or _run
    py = python or sys.executable
    if _has_schedule(run, py):
        return {"python": py, "how": "present"}
    venv_py = _venv_python(venv_dir())
    if venv_py.is_file() and _has_schedule(run, str(venv_py)):
        return {"python": str(venv_py), "how": "venv"}
    r = run([py, "-m", "pip", "install", "--upgrade", REQUIREMENT])
    if getattr(r, "returncode", 1) == 0 and _has_schedule(run, py):
        return {"python": py, "how": "pip"}
    why = _out(r)
    # PEP 668 (Debian/Ubuntu/Fedora system python) or any other refusal: a private venv.
    v = run([py, "-m", "venv", str(venv_dir())])
    if getattr(v, "returncode", 1) != 0:
        return {"python": "", "detail": ("pip refused and no venv could be made: "
                                         + (_out(v).strip() or why.strip()))[:240]}
    r2 = run([str(venv_py), "-m", "pip", "install", "--upgrade", REQUIREMENT])
    if getattr(r2, "returncode", 1) == 0 and _has_schedule(run, str(venv_py)):
        return {"python": str(venv_py), "how": "venv" + (" (PEP 668)" if
                                                          "externally-managed" in why else "")}
    return {"python": "", "detail": f"{REQUIREMENT} did not install: {_out(r2).strip()}"[:240]}


def install(args: Any = None, run: Optional[Callable[..., Any]] = None,
            python: Optional[str] = None) -> Dict[str, Any]:
    """Install awstorage and register the recurring file-index sync. Never raises."""
    if getattr(args, "no_autostart", False) or os.environ.get("AITHER_NO_FILE_INDEX") == "1":
        return {"file_index": "off", "detail": "--no-autostart / AITHER_NO_FILE_INDEX=1"}
    run = run or _run
    try:
        got = ensure_awstorage(run, python)
        if not got.get("python"):
            return {"file_index": "failed", "detail": got.get("detail", "")}
        r = run([got["python"], "-m", "awstorage", "files", "schedule", "--json"])
        try:
            out = json.loads(getattr(r, "stdout", "") or "{}")
        except ValueError:
            out = {"ok": False, "detail": _out(r).strip()[:240]}
        if not isinstance(out, dict):
            out = {"ok": False, "detail": f"unexpected schedule output: {out!r}"[:240]}
        out["file_index"] = "scheduled" if out.get("ok") and getattr(r, "returncode", 1) == 0 \
            else "failed"
        out["installed"] = got.get("how", "")
        return out
    except Exception as exc:  # noqa: BLE001 - enrolment succeeded; say what failed
        return {"file_index": "failed", "detail": f"{type(exc).__name__}: {exc}"[:240]}


def summary_line(res: Dict[str, Any]) -> str:
    state = res.get("file_index")
    if state == "scheduled":
        return (f"File index: every {res.get('every_hours')}h via {res.get('scheduler')}, as"
                f" node {res.get('node')} -> {res.get('index_dir')}")
    if state == "off":
        return "File index: off (--no-autostart / AITHER_NO_FILE_INDEX=1)"
    return (f"File index: NOT scheduled ({res.get('detail') or 'unknown'}) -- retry:"
            " python -m awstorage files schedule")


def main(argv: Optional[list] = None) -> int:
    res = install()
    print(json.dumps(res) if "--json" in (argv or sys.argv[1:]) else summary_line(res))
    return 0 if res.get("file_index") == "scheduled" else 1


if __name__ == "__main__":
    sys.exit(main())
