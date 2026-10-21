"""Make sure awgit's commit guards are installed in every repo an agent commits to.

WHY: on 2026-10-21, in a working tree ~10 agents share, one agent committed from a
private index read before a peer's commit and silently reverted all of it. awgit's
stale-revert and mass-delete guards would have refused that commit -- but they had
never been installed in that repo, because installing them was a step someone had
to remember. This hook removes the step: before the first ``git commit`` an agent
runs against a repo in a session, it runs ``awgit hooks ensure`` on that repo
(idempotent: installs or upgrades the chained hooks, keeps LFS and other existing
hooks as ``<hook>.org``).

Run BY PATH (the plugin cache holds only this directory):

    python awgit_ensure.py pre-bash        # PreToolUse(Bash|PowerShell): stdin = hook JSON
    python awgit_ensure.py session-start   # SessionStart: the session's cwd repo

awdk agents import the same functions (``adk.git_guards``), so there is one parser.

Contract: stdlib only, ALWAYS exit 0, nothing on stdout, never blocks a command.
Once per repo per session (cached in a temp file keyed by the session id), so the
cost after the first commit is one small file read. No awgit with ``hooks ensure``
on the box -> silent no-op (an old awgit's ``hooks install`` is NOT a fallback: it
installs a lease gate that rejects every commit where agents do not take leases).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional

TIMEOUT_S = 25.0
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_GIT_GLOBAL_WITH_ARG = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                        "--config-env", "--super-prefix"}
_CD_WORDS = {"cd", "pushd", "set-location", "push-location", "sl"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def _norm_path(p: str, base: str) -> str:
    """Resolve a shell path (Git Bash ``/d/x``, ``~``, relative) against ``base``."""
    p = p.strip().strip("'\"")
    if not p:
        return base
    p = os.path.expanduser(p)
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    if os.name == "nt" and m:
        p = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.normpath(p)


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def commit_dirs(command: str, cwd: str) -> List[str]:
    """Directories in which ``command`` runs ``git commit`` (empty when it does not).

    Follows ``cd X &&`` / ``Set-Location X;`` across segments and ``git -C X``
    (cumulative, like git). Deliberately cheap and forgiving: a missed exotic form
    only means the guards get installed on the next ordinary commit instead.
    """
    if "commit" not in command or "git" not in command:
        return []
    here = cwd
    out: List[str] = []
    for seg in _SEGMENT_SPLIT.split(command):
        toks = _tokens(seg.strip().lstrip("({").strip())
        while toks and _ENV_ASSIGN.match(toks[0]):
            toks = toks[1:]
        if not toks:
            continue
        head = toks[0].lower()
        if head in _CD_WORDS:
            args = [t for t in toks[1:] if not t.startswith("-")]
            if args:
                here = _norm_path(args[0], here)
            continue
        if Path(head).name not in ("git", "git.exe"):
            continue
        d = here
        i = 1
        while i < len(toks):
            t = toks[i]
            if t == "-C" and i + 1 < len(toks):
                d = _norm_path(toks[i + 1], d)
                i += 2
                continue
            if t in _GIT_GLOBAL_WITH_ARG and i + 1 < len(toks):
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            if t == "commit":
                out.append(d)
            break
    return out


def repo_root(start: str) -> Optional[str]:
    """Nearest ancestor holding ``.git`` (dir or worktree file), without spawning git."""
    p = Path(start)
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return str(d)
    return None


def _awgit_argv() -> Optional[List[str]]:
    exe = shutil.which("awgit")
    if exe:
        return [exe]
    try:
        found = importlib.util.find_spec("awgit") is not None
    except (ImportError, ValueError):
        found = False
    return [sys.executable, "-m", "awgit.cli"] if found else None


def _cache_file(session: str) -> Path:
    key = hashlib.sha1((session or "no-session").encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"awgit-ensure-{key}.json"


def _load(cache: Path) -> dict:
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure(dirs: Iterable[str], session: str = "") -> List[str]:
    """Run ``awgit hooks ensure`` once per repo per session; returns the dirs ensured now."""
    cache = _cache_file(session)
    done = _load(cache)
    todo = [d for d in dict.fromkeys(dirs) if d and d.lower() not in done and os.path.isdir(d)]
    if not todo:
        return []
    argv = _awgit_argv()
    if argv is None:
        return []
    ran: List[str] = []
    for d in todo:
        try:
            p = subprocess.run([*argv, "hooks", "ensure", "--repo", d, "--quiet"], cwd=d,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, timeout=TIMEOUT_S, **_no_window())
        except (OSError, subprocess.SubprocessError):
            continue
        if "invalid choice" in (p.stderr or ""):
            # awgit predates `hooks ensure`; remember so we do not pay for it per commit
            done[d.lower()] = "awgit-too-old"
            continue
        done[d.lower()] = "ok" if p.returncode == 0 else f"rc={p.returncode}"
        ran.append(d)
    try:
        cache.write_text(json.dumps(done), encoding="utf-8")
    except OSError as exc:  # uncached: the next commit simply re-runs the idempotent ensure
        print(f"awgit-ensure: cache not written ({exc})", file=sys.stderr)
    return ran


def _payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}


def main(argv: List[str]) -> int:
    if os.environ.get("AWGIT_ENSURE_DISABLE") == "1":
        return 0
    mode = argv[1] if len(argv) > 1 else ""
    try:
        data = _payload()
        session = str(data.get("session_id") or "")
        cwd = str(data.get("cwd") or os.getcwd())
        if mode == "pre-bash":
            cmd = str((data.get("tool_input") or {}).get("command") or "")
            dirs = commit_dirs(cmd, cwd)
            if dirs:
                ensure(dirs, session)
        elif mode == "session-start":
            root = repo_root(cwd)
            if root:
                ensure([root], session)
    except Exception as exc:  # noqa: BLE001 -- a guard installer must never break the agent
        print(f"awgit-ensure: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
