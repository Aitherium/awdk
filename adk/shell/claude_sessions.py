"""
Claude Code session management for AitherShell.
================================================

The pain this solves: a dozen Claude Code sessions across a dozen Windows
Terminal tabs, no way to see them in one place, no way to search them, and
when Windows Terminal dies it takes every session with it and each one has
to be relaunched by hand.

This module reads Claude Code's own session journals
(``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``) — read-only, it
never mutates them — and provides:

- **scan**: recover each session's AI title, last prompt, cwd, git branch and
  last-active time from a bounded tail read (fast even on multi-hundred-MB
  journals).
- **live detection**: map running ``claude`` processes to sessions via their
  working directory (psutil when available; degrades to mtime heuristics).
- **search**: full-text search across journal user/assistant text with
  per-session snippets.
- **launch/resume**: reopen chosen sessions as Windows Terminal tabs
  (``claude --resume <id>``), with graceful fallbacks off-Windows.
- **guard**: a crash watchdog that snapshots the live session set and, when
  Windows Terminal mass-kills them, records (and optionally auto-restores)
  the whole set.

CLI surface lives in ``adk.shell.cli`` (``aither sessions ...``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# How many bytes of journal tail to parse for metadata. Claude rewrites
# ai-title / last-prompt entries near the end of the journal, so a bounded
# tail is both fast and accurate.
_TAIL_BYTES = 256 * 1024

# State lives beside the rest of AitherShell's local state.
STATE_DIR = Path(os.environ.get("AITHER_HOME", Path.home() / ".aither")) / "claude_sessions"
LIVE_SNAPSHOT = STATE_DIR / "live.json"
CRASH_SNAPSHOT = STATE_DIR / "crash.json"
GUARD_PAUSE = STATE_DIR / "guard.paused"

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class SessionMeta:
    id: str
    file: str
    title: str = ""
    last_prompt: str = ""
    cwd: str = ""
    branch: str = ""
    when: Optional[datetime] = None
    live: bool = False
    pid: Optional[int] = None

    @property
    def age(self) -> str:
        if not self.when:
            return "?"
        span = datetime.now(timezone.utc) - self.when
        secs = max(span.total_seconds(), 0)
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs / 60:.0f}m ago"
        if secs < 86400:
            return f"{secs / 3600:.0f}h ago"
        return f"{secs / 86400:.0f}d ago"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "branch": self.branch,
            "lastPrompt": self.last_prompt,
            "when": self.when.isoformat() if self.when else None,
            "live": self.live,
            "file": self.file,
        }


# ─── journal parsing ─────────────────────────────────────────────────────────

def _read_tail_lines(path: Path, tail_bytes: int = _TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            raw = f.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # First line of a mid-file seek is almost certainly partial — drop it.
    if size > tail_bytes and lines:
        lines = lines[1:]
    return lines


def _head_cwd(path: Path, head_bytes: int = 64 * 1024) -> str:
    """First cwd recorded in the journal = the directory the session was
    launched from. The tail cwd drifts when the session's shell cd's around,
    and resuming from the wrong directory puts the session in the wrong
    Claude project — so launch cwd wins.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(head_bytes)
    except OSError:
        return ""
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or '"cwd"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("cwd"):
            return str(obj["cwd"])
    return ""


def parse_session_meta(path: Path) -> SessionMeta:
    """Recover a session's metadata from head + tail of its journal."""
    meta = SessionMeta(id=path.stem, file=str(path))
    for line in _read_tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        if typ == "ai-title" and obj.get("aiTitle"):
            meta.title = str(obj["aiTitle"])
        elif typ == "last-prompt" and obj.get("lastPrompt"):
            meta.last_prompt = str(obj["lastPrompt"])
        if obj.get("cwd"):
            meta.cwd = str(obj["cwd"])
        if obj.get("gitBranch"):
            meta.branch = str(obj["gitBranch"])
        if obj.get("timestamp"):
            ts = _parse_ts(obj["timestamp"])
            if ts:
                meta.when = ts
    launch_cwd = _head_cwd(path)
    if launch_cwd:
        meta.cwd = launch_cwd
    if not meta.title:
        meta.title = meta.id[:8]
    if not meta.when:
        try:
            meta.when = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            pass
    return meta


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _journal_files(projects_root: Path, scan: int) -> list[Path]:
    """Top-level session journals (UUID-named), newest first.

    Sub-agent / workflow sidechains live in nested ``subagents``/``workflows``
    directories and are not resumable — skipped by construction: real session
    journals sit directly under the project directory.
    """
    if not projects_root.is_dir():
        return []
    files: list[tuple[float, Path]] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        try:
            for f in project_dir.iterdir():
                if f.suffix == ".jsonl" and _UUID_RE.match(f.stem):
                    try:
                        files.append((f.stat().st_mtime, f))
                    except OSError:
                        continue
        except OSError:
            continue
    files.sort(key=lambda t: t[0], reverse=True)
    return [f for _, f in files[:scan]]


# ─── live detection ──────────────────────────────────────────────────────────

def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p or ""))


def claude_process_cwds() -> dict[str, int]:
    """Map normalized cwd -> count of running claude CLI processes there.

    Requires psutil for the cwd lookup; returns {} when unavailable so
    callers degrade to journal-mtime heuristics.
    """
    try:
        import psutil
    except ImportError:
        return {}
    counts: dict[str, int] = {}
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("claude"):
                continue
            cwd = proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        if cwd:
            key = _norm_path(cwd)
            counts[key] = counts.get(key, 0) + 1
    return counts


def claude_process_count() -> int:
    """Total running claude CLI processes (0 if psutil missing → -1 unknown)."""
    try:
        import psutil
    except ImportError:
        return -1
    n = 0
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower().startswith("claude"):
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return n


def terminal_host_running() -> bool:
    """Is Windows Terminal (or any obvious terminal host) alive?"""
    try:
        import psutil
    except ImportError:
        return True
    hosts = {"windowsterminal.exe", "wt.exe"}
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() in hosts:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def mark_live(sessions: list[SessionMeta]) -> list[SessionMeta]:
    """Flag sessions that are (best-effort) currently open.

    Process cmdlines don't carry the session id, so liveness is inferred at
    cwd granularity: if N claude processes run in a directory, the N
    most-recently-active journals for that directory are marked live.
    """
    counts = claude_process_cwds()
    if not counts:
        return sessions
    by_cwd: dict[str, list[SessionMeta]] = {}
    for s in sessions:
        if s.cwd:
            by_cwd.setdefault(_norm_path(s.cwd), []).append(s)
    for cwd, n in counts.items():
        candidates = sorted(
            by_cwd.get(cwd, []),
            key=lambda s: s.when or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        for s in candidates[:n]:
            s.live = True
    return sessions


# ─── scanning / filtering ────────────────────────────────────────────────────

def scan_sessions(
    projects_root: Optional[Path] = None,
    scan: int = 120,
    top: int = 50,
    per_dir: bool = False,
    text_filter: str = "",
    lookback_hours: float = 0,
    include_missing: bool = False,
    exclude_session: str = "",
    detect_live: bool = True,
) -> list[SessionMeta]:
    root = projects_root or DEFAULT_PROJECTS_ROOT
    sessions = [parse_session_meta(f) for f in _journal_files(root, scan)]
    sessions = [s for s in sessions if s.cwd]
    if not include_missing:
        sessions = [s for s in sessions if Path(s.cwd).is_dir()]
    if exclude_session:
        sessions = [s for s in sessions if s.id != exclude_session]
    if text_filter:
        needle = text_filter.lower()
        sessions = [
            s for s in sessions
            if needle in f"{s.title} {s.cwd} {s.branch} {s.last_prompt}".lower()
        ]
    if lookback_hours > 0:
        cut = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        sessions = [s for s in sessions if s.when and s.when >= cut]
    sessions.sort(key=lambda s: s.when or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if per_dir:
        seen: set[str] = set()
        uniq = []
        for s in sessions:
            key = _norm_path(s.cwd)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(s)
        sessions = uniq
    sessions = sessions[:top]
    if detect_live:
        mark_live(sessions)
    return sessions


def expand_selection(spec: str, count: int) -> list[int]:
    """'1,3,5-7' / 'all' / 'a' -> 1-based indices, bounded and unique."""
    if not spec or not spec.strip():
        return []
    if re.fullmatch(r"\s*(a|all)\s*", spec, re.IGNORECASE):
        return list(range(1, count + 1))
    out: list[int] = []
    for tok in re.split(r"[,\s]+", spec.strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.extend(range(a, b + 1))
        elif tok.isdigit():
            out.append(int(tok))
    seen: set[int] = set()
    result = []
    for i in out:
        if 1 <= i <= count and i not in seen:
            seen.add(i)
            result.append(i)
    return result


# ─── full-text search ────────────────────────────────────────────────────────

@dataclass
class SearchHit:
    session: SessionMeta
    matches: int = 0
    snippets: list[str] = field(default_factory=list)


def _extract_text(obj: dict) -> str:
    """Human text from a user/assistant journal entry (tool noise excluded)."""
    if obj.get("type") not in ("user", "assistant"):
        return ""
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts)
    return ""


def _snippet(text: str, needle_lower: str, radius: int = 60) -> str:
    idx = text.lower().find(needle_lower)
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle_lower) + radius)
    snip = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snip}{suffix}"


def search_sessions(
    query: str,
    projects_root: Optional[Path] = None,
    days: float = 30,
    scan: int = 400,
    max_sessions: int = 20,
    max_snippets: int = 3,
) -> list[SearchHit]:
    """Case-insensitive substring search over session conversation text.

    Linear streaming scan bounded by ``days`` (journal mtime) and ``scan``
    (newest N journals) so it stays fast without an index. The raw line is
    substring-prefiltered before any JSON parsing.
    """
    root = projects_root or DEFAULT_PROJECTS_ROOT
    needle = query.lower()
    cutoff = time.time() - days * 86400 if days > 0 else 0
    hits: list[SearchHit] = []
    for f in _journal_files(root, scan):
        try:
            if cutoff and f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        hit: Optional[SearchHit] = None
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle not in line.lower():
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    text = _extract_text(obj)
                    if not text or needle not in text.lower():
                        continue
                    if hit is None:
                        hit = SearchHit(session=parse_session_meta(f))
                    hit.matches += 1
                    if len(hit.snippets) < max_snippets:
                        snip = _snippet(text, needle)
                        if snip:
                            hit.snippets.append(snip)
        except OSError:
            continue
        if hit:
            hits.append(hit)
            if len(hits) >= max_sessions:
                break
    # Newest session first within the result set.
    hits.sort(
        key=lambda h: h.session.when or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return hits


# ─── transcript extraction ───────────────────────────────────────────────────

def session_transcript(
    path: Path | str,
    max_turns: int = 30,
    tail_bytes: int = 512 * 1024,
) -> list[tuple[str, str]]:
    """Last ``max_turns`` (role, text) conversation turns from a journal tail.

    Only human-visible text (user prompts, assistant prose) — tool calls and
    results are skipped. Used by the browser preview pane.
    """
    turns: list[tuple[str, str]] = []
    for line in _read_tail_lines(Path(path), tail_bytes):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        text = _extract_text(obj)
        if not text or not text.strip():
            continue
        # System-reminder blocks inside user entries are harness noise.
        if obj.get("type") == "user" and text.lstrip().startswith("<system-reminder>"):
            continue
        turns.append((str(obj.get("type")), text.strip()))
    return turns[-max_turns:]


def iter_journal_turns(path: Path | str, from_byte: int = 0):
    """Yield (role, text, timestamp, end_byte) for each conversational entry
    from ``from_byte`` onward. Powers incremental session ingest: journals are
    append-only, so the caller persists ``end_byte`` as its watermark.
    """
    pos = from_byte
    with open(path, "rb") as f:
        f.seek(from_byte)
        for raw in f:
            pos += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            text = _extract_text(obj)
            if not text or not text.strip():
                continue
            if obj.get("type") == "user" and text.lstrip().startswith("<system-reminder>"):
                continue
            yield str(obj.get("type")), text.strip(), obj.get("timestamp") or "", pos


# ─── launching ───────────────────────────────────────────────────────────────

def resume_command(session: SessionMeta) -> str:
    return f"claude --resume {session.id}"


def resume_here(session: SessionMeta) -> int:
    """Run ``claude --resume`` for this session in the CURRENT terminal,
    blocking until Claude exits. This is what lets the browser hop between
    sessions from a single window instead of spawning tabs."""
    if not session.cwd or not Path(session.cwd).is_dir():
        return 1
    if sys.platform == "win32":
        # claude is an npm shim (.cmd/.ps1) — let pwsh resolve it from PATH.
        return subprocess.call(
            ["pwsh", "-NoLogo", "-Command", resume_command(session)], cwd=session.cwd
        )
    return subprocess.call(["claude", "--resume", session.id], cwd=session.cwd)


def launch_sessions(
    sessions: Iterable[SessionMeta],
    separate_windows: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Reopen sessions; returns human-readable lines describing what launched.

    Windows Terminal tabs by default; separate pwsh windows if wt is absent;
    on non-Windows platforms prints the commands to run instead of spawning.
    """
    chosen = [s for s in sessions if s.cwd and Path(s.cwd).is_dir()]
    lines: list[str] = []
    if not chosen:
        return ["nothing to launch (no sessions with an existing working directory)"]
    if dry_run or sys.platform != "win32":
        for s in chosen:
            lines.append(f'cd "{s.cwd}"  &&  {resume_command(s)}')
        return lines

    wt = shutil.which("wt")

    def tab_args(s: SessionMeta) -> list[str]:
        return [
            "new-tab", "-d", s.cwd, "--title", s.title or s.id[:8],
            "pwsh", "-NoExit", "-Command", resume_command(s),
        ]

    if not wt:
        for s in chosen:
            subprocess.Popen(
                ["pwsh", "-NoExit", "-Command", resume_command(s)],
                cwd=s.cwd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            lines.append(f"{s.title}  ({s.cwd})  [new console]")
        return lines

    if separate_windows:
        for s in chosen:
            subprocess.Popen([wt, "-w", "new", *tab_args(s)])
            lines.append(f"{s.title}  ({s.cwd})  [new window]")
        return lines

    args: list[str] = [wt]
    for i, s in enumerate(chosen):
        if i:
            args.append(";")
        args.extend(tab_args(s))
        lines.append(f"{s.title}  ({s.cwd})")
    subprocess.Popen(args)
    return lines


# ─── crash guard ─────────────────────────────────────────────────────────────

def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def snapshot_live(projects_root: Optional[Path] = None) -> dict:
    """Record the currently-live session set (one guard tick's ground truth)."""
    sessions = scan_sessions(projects_root=projects_root, scan=120, top=50, detect_live=True)
    live = [s for s in sessions if s.live]
    payload = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "claude_processes": claude_process_count(),
        "sessions": [s.to_dict() for s in live],
    }
    _write_json(LIVE_SNAPSHOT, payload)
    return payload


def detect_crash(
    prev: Optional[dict],
    now_count: int,
    terminal_alive: bool,
    min_sessions: int = 3,
) -> bool:
    """Mass-death heuristic: ≥min_sessions were live, now zero remain and the
    terminal host is gone. Closing one or two tabs on purpose never trips it."""
    if not prev:
        return False
    prev_count = prev.get("claude_processes")
    if not isinstance(prev_count, int) or prev_count < min_sessions:
        return False
    return now_count == 0 and not terminal_alive


def record_crash(prev: dict) -> None:
    payload = dict(prev)
    payload["crashed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(CRASH_SNAPSHOT, payload)


def pending_crash() -> Optional[dict]:
    """A recorded crash that hasn't been restored yet, if any."""
    return _read_json(CRASH_SNAPSHOT)


def clear_crash() -> None:
    try:
        CRASH_SNAPSHOT.unlink()
    except OSError:
        pass


def snapshot_sessions(snapshot: dict) -> list[SessionMeta]:
    out = []
    for d in snapshot.get("sessions", []):
        if not isinstance(d, dict) or not d.get("id"):
            continue
        out.append(
            SessionMeta(
                id=d["id"],
                file=d.get("file", ""),
                title=d.get("title", ""),
                cwd=d.get("cwd", ""),
                branch=d.get("branch", ""),
                last_prompt=d.get("lastPrompt", ""),
                when=_parse_ts(d.get("when")),
            )
        )
    return out


def guard_loop(
    interval: float = 20.0,
    auto_restore: bool = True,
    min_sessions: int = 3,
    projects_root: Optional[Path] = None,
    on_event=None,
) -> None:
    """Watchdog loop: snapshot live sessions; on mass-death, record the crash
    and (optionally) relaunch the whole set. Runs until interrupted.

    Must run OUTSIDE the terminal being guarded (scheduled task / hidden
    process) or it dies with the crash it is meant to survive.
    """
    emit = on_event or (lambda msg: None)
    prev: Optional[dict] = _read_json(LIVE_SNAPSHOT)
    while True:
        if GUARD_PAUSE.exists():
            emit("paused")
            time.sleep(interval)
            continue
        now_count = claude_process_count()
        terminal_alive = terminal_host_running()
        if detect_crash(prev, now_count, terminal_alive, min_sessions=min_sessions):
            record_crash(prev)
            emit(f"crash detected — {len(prev.get('sessions', []))} session(s) lost")
            if auto_restore:
                sessions = snapshot_sessions(prev)
                lines = launch_sessions(sessions)
                clear_crash()
                emit("auto-restored: " + "; ".join(lines))
            prev = None
            time.sleep(interval)
            continue
        if now_count > 0:
            prev = snapshot_live(projects_root=projects_root)
        time.sleep(interval)


# ─── guard install (Windows scheduled task) ──────────────────────────────────

GUARD_TASK_NAME = "AitherShell Claude Session Guard"


def install_guard(auto_restore: bool = True) -> str:
    """Register a hidden at-logon scheduled task running the guard daemon."""
    if sys.platform != "win32":
        raise RuntimeError("guard install is Windows-only (schtasks)")
    flag = "" if auto_restore else " --no-auto-restore"
    # Run this file directly (it is stdlib-only by design) rather than
    # `-m adk.shell`: the scheduled task starts in system32, where -m would
    # import whatever awdk pip has installed — possibly an older build
    # without this module.
    inner = f'"{sys.executable}" "{Path(__file__).resolve()}" --daemon{flag}'
    # Hide the daemon window with the GUI-subsystem shim, not `conhost --headless`.
    # conhost.exe is itself a CONSOLE-subsystem binary, so an interactive-logon task
    # pointed at it is the shape this repo's convention (shim, or S4U) exists to
    # replace — and it is what the on-host checker flags. One mechanism everywhere
    # beats three that each need their own argument.
    #
    # The payload goes in a wrapper .cmd so /TR stays under the 261-char cap that
    # schtasks silently enforces by failing the whole create: `inner` already holds
    # two absolute paths and the shim adds a third.
    from adk.llamacpp_setup import hidden_task_run, write_hidden_launch_shim

    home = Path.home() / ".aither"
    shim = write_hidden_launch_shim(home)
    wrapper = home / "aithershell-guard.cmd"
    wrapper.write_text(f"@echo off\r\n{inner}\r\n", encoding="utf-8")
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", GUARD_TASK_NAME,
        "/SC", "ONLOGON",
        "/TR", hidden_task_run(shim, wrapper),
        "/RL", "LIMITED",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"schtasks failed: {res.stderr.strip() or res.stdout.strip()}")
    # Fire it now so protection starts without a re-logon.
    subprocess.run(["schtasks", "/Run", "/TN", GUARD_TASK_NAME], capture_output=True, text=True)
    return GUARD_TASK_NAME


def uninstall_guard() -> bool:
    if sys.platform != "win32":
        return False
    subprocess.run(["schtasks", "/End", "/TN", GUARD_TASK_NAME], capture_output=True, text=True)
    res = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", GUARD_TASK_NAME],
        capture_output=True, text=True,
    )
    return res.returncode == 0


# Standalone guard daemon entry: the at-logon scheduled task runs this file
# directly so it works regardless of which awdk version pip has.
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Claude Code session crash guard")
    ap.add_argument("--daemon", action="store_true", help="run the watchdog loop")
    ap.add_argument("--interval", type=float, default=20.0)
    ap.add_argument("--min-sessions", type=int, default=3)
    ap.add_argument("--no-auto-restore", action="store_true")
    ns = ap.parse_args()
    if not ns.daemon:
        ap.error("nothing to do (use --daemon, or drive this via `aither sessions guard`)")
    guard_loop(
        interval=ns.interval,
        auto_restore=not ns.no_auto_restore,
        min_sessions=ns.min_sessions,
        on_event=lambda msg: print(f"[guard] {msg}", flush=True),
    )
