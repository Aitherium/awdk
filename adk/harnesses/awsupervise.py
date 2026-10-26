#!/usr/bin/env python3
"""awsupervise: catch Claude Code agents that loop or stall, and escalate.

WHY (owner ask, 2026-10-26)
---------------------------
Every Claude Code session on this host spawns subagents on DeepSeek-V4-Flash through
the local router. On 2026-10-26 a Darkmatter skull/skeleton workflow ran for hours on
that tier without getting the job done, and nobody noticed. The local research delegate
was measured making 50 tool calls of which 3 were unique. A loop is cheap to see in a
transcript and expensive to leave running, so this reads the live transcripts, names
the agents that loop or stall, and climbs a short ladder until somebody acts.

STDLIB ONLY, NO FLEET
---------------------
It has to work while WSL (the fleet VM) is wedged: no ports 8150/8151/8001, no vault.
It reads local files, writes local files, and shells out to two host CLIs (awrelay,
adk). Both queue or fail on their own when their servers are down; neither is needed
to detect anything.

WHAT IT READS
-------------
* main transcripts  ~/.claude/projects/<project>/<session>.jsonl. Any ``isSidechain``
                    entries (older Claude Code) are grouped as their own agents.
* subagents         ~/.claude/projects/<project>/<session>/subagents/**/agent-<id>.jsonl,
                    plus agent-<id>.meta.json and a workflow's journal.jsonl.
                    %TEMP%/claude/<project>/<session>/tasks/a*.output are hard links to
                    the same transcripts and are read only for an agent not found above.
* live sessions     ~/.claude/sessions/<pid>.json, verified against the process start
                    time (ctypes; no subprocess per check).
* router trace      ~/.aither/agent-trace/router-<date>.jsonl, one row per model
                    request. A row carries no session id, so a failure streak is
                    attributed to an agent by its model and by time.

DETECTORS (thresholds live in CONFIG)
-------------------------------------
a repeat_call      the same tool with the same arguments, 4+ times since the last progress
b repeat_error     the same tool error text 3+ times in the window
c no_progress      25 assistant turns with no progress signal: a Write/Edit that changed
                   content, a test or self-test command that exited 0, a git commit, a
                   todo marked completed, a final report
d oscillation      an edit undone on the same file (A -> B -> A)
e upstream_streak  3+ 401 / 5xx / timeout failures in a row on the agent's model (router
                   trace), or in its own transcript (API-error turns)
f wall_budget      a subagent running past its wall budget with no final report

THE LADDER (deduped, rate-limited, every action logged to
~/.aither/supervisor/events-<date>.jsonl)
-----------------------------------------
L1 steer  A peer-authority note in the session's steer mailbox (~/.aither/steer/<sid>/).
          steer-mailbox-drain.py --inturn delivers it at the next tool call. A subagent's
          hooks carry its PARENT's session id, so a subagent has no mailbox of its own:
          it is reached through the parent's. The note names the agent and tells the
          lead what to do with it.
L2 relay  awrelay send '#agents' "<text>" --kind finding --to <session prefix>
          (queued on disk and exit 0 when the relay is down).
L3 card   adk decide ask ... --session <sid>, with three options: stop it, re-run it on
          Opus 5.5 ("fable"), or let it continue. The owner's answer lands in that
          session's mailbox with owner authority. A card with options also rides the
          existing Discord DM fanout (AitherOS-Awask-Card-Fanout); no new secret.

An incident climbs one rung per CONFIG["climb_minutes"] while it persists. When a
DeepSeek-tier agent trips c or e, the escalation recommends the Opus 5.5 tier.

CLI
---
    python awsupervise.py --once        scan, act, exit
    python awsupervise.py --watch       scan every --interval seconds
    python awsupervise.py --dry-run     detect and print the would-be actions; write nothing
    python awsupervise.py --self-test   synthetic transcripts: every detector and the ladder

Exit 0: scanned (findings or not). 1: the self-test failed. 2: could not scan.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- config

CONFIG: dict = {
    # --- what is scanned
    "active_minutes": 45,          # a transcript written this recently is live
    "silent_max_minutes": 240,     # an unfinished subagent of a LIVE session: watched this long
    "horizon_hours": 12,           # session dirs older than this are not walked at all
    "tail_bytes": 3 * 1024 * 1024,     # bytes of each transcript read per scan, from the end
    "max_line_bytes": 2 * 1024 * 1024,  # a longer line (a huge tool result) is skipped
    # --- the rolling window (a, b, d)
    "window_calls": 50,
    # --- a. identical tool calls
    "repeat_call_min": 4,
    "repeat_call_min_by_tool": {"TaskOutput": 10, "BashOutput": 10, "Monitor": 10,
                                "TaskList": 10, "TaskGet": 10},
    "repeat_call_ignore_keys": ("description",),
    # --- b. the same error
    "repeat_error_min": 3,
    # --- c. no progress
    "no_progress_turns": 25,
    "no_progress_turns_main": 40,
    "no_progress_turns_by_type": {"Explore": 60, "Plan": 60, "claude-code-guide": 60},
    # ...and this long since the last progress (or the start): a fresh agent's first
    # reads are exploration, not a loop (measured: 33 turns in 6 minutes on Opus 5.5)
    "no_progress_min_minutes": 15,
    # --- d. oscillation
    "oscillation_min": 1,
    # --- e. upstream failure streak
    "upstream_streak_min": 3,
    "upstream_recent_minutes": 20,
    "upstream_error_kinds": ("authentication_failed", "server_error", "overloaded_error",
                             "timeout", "api_error"),
    "router_transport_errors": True,   # a reset / refused / incomplete stream counts too
    "router_tail_bytes": 4 * 1024 * 1024,
    # --- f. wall budget, minutes (0 = off)
    "wall_budget_minutes": 120,
    "wall_budget_minutes_main": 0,
    # --- the ladder
    "climb_minutes": 10,
    "start_level": {},                 # detector -> first rung (default 1)
    "resolve_grace_minutes": 30,       # a re-trip inside this keeps its rung
    "owner_hold_minutes": {"continue": 120, "stop": 360, "fable": 360},
    "max_steers_per_session_per_hour": 6,
    "max_relays_per_hour": 12,
    "max_cards_per_hour": 3,
    "relay_channel": "#agents",
    "card_urgency": "normal",
    "card_deadline": "2h",
    "subprocess_timeout_s": 90,
    # --- tiers. In this tier map the alias `opus` points at DeepSeek; `fable` is
    # claude-opus-5-5 (Opus 5.5).
    "cheap_tier_markers": ("deepseek", "local-"),
    "cheap_tier_aliases": ("opus",),
    "upgrade_alias": "fable",
    "upgrade_label": "Opus 5.5",
    "upgrade_detectors": ("no_progress", "upstream_streak"),
}

DETECTOR_CODE = {"repeat_call": "a", "repeat_error": "b", "no_progress": "c",
                 "oscillation": "d", "upstream_streak": "e", "wall_budget": "f"}

#: A session id reaches the filesystem as a directory name (same rule as the drain hook).
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
TEST_RE = re.compile(
    r"(\bpytest\b|-m\s+pytest|\bunittest\b|--self-test\b|\bself[-_]?test\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|\bvitest\b|\bjest\b|\bgo\s+test\b"
    r"|\bcargo\s+test\b|\bdotnet\s+test\b|\bctest\b|\bmake\s+(?:test|check)\b|\btox\b"
    r"|\bplaywright\s+test\b)", re.I)
COMMIT_RE = re.compile(r"\bgit\b[^\n;&|]*\b(?:commit|commit-tree)\b")
#: Errors a person caused (an interrupt, a rejection): not the agent looping.
HUMAN_ERROR_RE = re.compile(r"interrupted by user|doesn't want to proceed|user rejected", re.I)
#: Never repeat a credential into a mailbox, the relay, a card or the console.
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)[^\s'\",;]{6,})")
CREATE_NO_WINDOW = 0x08000000  # a wake must never open a console window or a tab


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", str(text or ""))


def clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: max(0, n - 3)] + "..."


def _digest(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- paths


@dataclass
class Paths:
    projects: Path
    temp_claude: Path
    sessions: Path
    labels: Path
    trace: Path
    steer: Path
    home: Path
    decisions: Path

    @classmethod
    def default(cls) -> "Paths":
        h = Path.home()

        def env(name: str, fallback: Path) -> Path:
            raw = os.environ.get(name, "").strip()
            return Path(raw) if raw else fallback

        return cls(
            projects=env("AWSUPERVISE_PROJECTS", h / ".claude" / "projects"),
            temp_claude=env("AWSUPERVISE_TEMP_CLAUDE", Path(tempfile.gettempdir()) / "claude"),
            sessions=env("AWSUPERVISE_SESSIONS", h / ".claude" / "sessions"),
            labels=env("AWRELAY_SESSION_LABELS", h / ".aither" / "session-labels"),
            trace=env("AWSUPERVISE_TRACE", h / ".aither" / "agent-trace"),
            steer=env("AITHER_STEER_DIR", h / ".aither" / "steer"),
            home=env("AWSUPERVISE_HOME", h / ".aither" / "supervisor"),
            decisions=env("AITHER_DECISIONS_DIR", h / ".aither" / "decisions"),
        )


# --------------------------------------------------------------------------- reading


def _ts(value: Any) -> float:
    """ISO-8601 (``...Z``) or epoch seconds/ms -> epoch seconds; 0.0 when unreadable."""
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def read_tail(path: Path, max_bytes: int, max_line: int) -> tuple[list, bool]:
    """The JSON objects in the last ``max_bytes`` of a JSONL file, and whether it was cut.

    A transcript grows without limit and one line can be megabytes of tool output, so
    the read is bounded; the first (partial) line of a cut read and any line still being
    written are skipped, never guessed at."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read(max_bytes)
    except OSError:
        return [], False
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw or len(raw) > max_line:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out, start > 0


def read_head_ts(path: Path, probe: int = 65536) -> float:
    """The first timestamp in a JSONL file (when the agent started)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(probe)
    except OSError:
        return 0.0
    for raw in data.split(b"\n")[:40]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("timestamp"):
            return _ts(obj["timestamp"])
    return 0.0


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


# --------------------------------------------------------------------------- liveness


def _proc_start(pid: int) -> Optional[float]:
    """Unix start time of a live process; -1.0 when alive but the time is unknown; None
    when it is not running. ctypes on Windows: a probe that spawned wmic per session
    would feed the very process-creation leak this host already pays for."""
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return -1.0
        except OSError:
            return None
        return -1.0
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None  # 259 = STILL_ACTIVE
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return -1.0
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return (ticks - 116444736000000000) / 1e7
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return -1.0


def _alive(pid: Any, proc_start: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    started = _proc_start(pid)
    if started is None:
        return False
    if started < 0:
        return True
    try:
        claim = (int(proc_start) - 116444736000000000) / 1e7
    except (TypeError, ValueError):
        return True
    return abs(started - claim) <= 10.0  # a reused pid has a different start time


@dataclass
class Session:
    sid: str
    name: str = ""
    cwd: str = ""
    status: str = ""
    alive: bool = False


def load_sessions(paths: Paths) -> dict:
    out: dict = {}
    try:
        files = list(paths.sessions.glob("*.json"))
    except OSError:
        files = []
    for p in files:
        rec = _read_json(p)
        sid = str(rec.get("sessionId") or "")
        if not sid:
            continue
        s = out.setdefault(sid, Session(sid))
        s.name = s.name or str(rec.get("name") or "")
        s.cwd = s.cwd or str(rec.get("cwd") or "")
        if _alive(rec.get("pid"), rec.get("procStart")):
            s.alive = True
            s.status = str(rec.get("status") or "")
    try:
        labels = list(paths.labels.glob("*.json"))
    except OSError:
        labels = []
    for p in labels:
        rec = _read_json(p)
        sid = str(rec.get("session_id") or "")
        if sid and rec.get("title"):
            s = out.setdefault(sid, Session(sid))
            s.name = s.name or str(rec["title"])
    return out


# --------------------------------------------------------------------------- the agent model


@dataclass
class Call:
    id: str
    name: str
    input: dict
    ts: float
    seq: int


@dataclass
class Result:
    call_id: str
    is_error: bool
    text: str
    tur: Any
    ts: float
    seq: int


@dataclass
class Turn:
    mid: str
    ts: float
    seq: int
    stop: str = ""
    text: bool = False
    api_error: str = ""
    error_text: str = ""


@dataclass
class Agent:
    key: str
    session_id: str
    agent_id: str
    kind: str                 # "main" | "subagent"
    path: Path
    project: str = ""
    description: str = ""
    agent_type: str = ""
    model: str = ""
    cwd: str = ""
    started: float = 0.0
    last_write: float = 0.0
    finished: str = ""        # why it counts as finished; "" while running
    truncated: bool = False
    calls: list = field(default_factory=list)
    results: dict = field(default_factory=dict)
    turns: list = field(default_factory=list)
    progress: list = field(default_factory=list)   # (seq, kind, label, ts)

    @property
    def short(self) -> str:
        return self.session_id.split("-")[0]

    def label(self) -> str:
        model = self.model or "model ?"
        if self.kind == "main":
            return f"the main session ({model})"
        what = self.description or self.agent_type or "subagent"
        return f'subagent {self.agent_id} "{clip(what, 60)}" ({model})'


@dataclass
class Finding:
    agent: Agent
    detector: str
    summary: str               # one line: relay, card title material
    steer: str                 # the instruction the agent gets at L1
    evidence: list
    upgrade: bool = False


def parse_entries(agent: Agent, entries: list) -> None:
    """Fold transcript entries into calls, results and turns, in transcript order.

    One API response is written as one entry per content block, all sharing the
    message id, so a TURN is a distinct assistant message id."""
    turns: dict = {}
    seq = len(agent.calls) + len(agent.turns) + len(agent.results)
    for d in entries:
        kind = d.get("type")
        ts = _ts(d.get("timestamp"))
        if d.get("cwd") and not agent.cwd:
            agent.cwd = str(d["cwd"])
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        if kind == "assistant":
            mid = str(msg.get("id") or d.get("uuid") or "t%d" % seq)
            model = str(d.get("requestedModel") or msg.get("model") or "")
            if model and not model.startswith("<"):
                agent.model = model
            turn = turns.get(mid)
            if turn is None:
                seq += 1
                turn = turns[mid] = Turn(mid=mid, ts=ts, seq=seq)
                agent.turns.append(turn)
            if d.get("isApiErrorMessage"):
                turn.api_error = str(d.get("error") or "unknown")
                turn.error_text = clip(_text_of(msg.get("content")), 200)
            if msg.get("stop_reason"):
                turn.stop = str(msg["stop_reason"])
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    turn.text = True
                elif block.get("type") == "tool_use":
                    seq += 1
                    agent.calls.append(Call(
                        id=str(block.get("id") or "c%d" % seq), name=str(block.get("name") or "?"),
                        input=block.get("input") if isinstance(block.get("input"), dict) else {},
                        ts=ts, seq=seq))
        elif kind == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    seq += 1
                    res = Result(call_id=str(block.get("tool_use_id") or ""),
                                 is_error=bool(block.get("is_error")),
                                 text=_text_of(block.get("content")),
                                 tur=d.get("toolUseResult"), ts=ts, seq=seq)
                    agent.results[res.call_id] = res


def _completed_todos(todos: Any) -> set:
    out = set()
    for t in todos if isinstance(todos, list) else []:
        if isinstance(t, dict) and str(t.get("status")).lower() == "completed":
            out.add(str(t.get("content") or t.get("subject") or t.get("id") or ""))
    return out


def compute_progress(agent: Agent) -> None:
    """Every progress signal, in order: changed content, a passing test, a commit, a todo
    completed, a final report."""
    write_hash: dict = {}
    done_before: set = set()
    for call in agent.calls:
        res = agent.results.get(call.id)
        if res is None or res.is_error:
            continue
        name, inp = call.name, call.input
        tur = res.tur if isinstance(res.tur, dict) else {}
        kind = label = ""
        if name in ("Edit", "MultiEdit", "NotebookEdit"):
            if name != "Edit" or inp.get("old_string") != inp.get("new_string"):
                kind, label = "edit", str(inp.get("file_path") or inp.get("notebook_path") or "")
        elif name == "Write":
            path = str(inp.get("file_path") or "")
            digest = _digest(inp.get("content"))
            if tur.get("type") == "create":
                changed = True
            elif "structuredPatch" in tur:
                changed = bool(tur.get("structuredPatch"))  # empty patch: same bytes again
            else:
                changed = write_hash.get(path) != digest
            write_hash[path] = digest
            if changed:
                kind, label = "edit", path
        elif name in SHELL_TOOLS:
            cmd = str(inp.get("command") or "")
            if COMMIT_RE.search(cmd):
                kind, label = "commit", clip(cmd, 60)
            elif TEST_RE.search(cmd):
                kind, label = "test", clip(cmd, 60)
        elif name == "TodoWrite":
            if "newTodos" in tur:
                fresh = (_completed_todos(tur.get("newTodos"))
                         - _completed_todos(tur.get("oldTodos")))
            else:
                fresh = _completed_todos(inp.get("todos")) - done_before
            done_before |= _completed_todos(inp.get("todos"))
            if fresh:
                kind, label = "todo", clip(sorted(fresh)[0], 60)
        elif name == "TaskUpdate" and str(inp.get("status")).lower() == "completed":
            kind, label = "todo", str(inp.get("taskId") or "")
        elif name == "SubagentHandback":
            kind, label = "report", "handback"
        if kind:
            agent.progress.append((res.seq, kind, label, res.ts))
    for turn in agent.turns:
        if not turn.api_error and turn.stop == "end_turn" and turn.text:
            agent.progress.append((turn.seq, "report", "turn report", turn.ts))
    agent.progress.sort(key=lambda p: p[0])


# --------------------------------------------------------------------------- discovery


@dataclass
class Scan:
    now: float
    sessions: dict
    agents: list
    router: dict
    skipped: Counter = field(default_factory=Counter)


def _meta_for(jsonl: Path) -> dict:
    return _read_json(jsonl.with_name(jsonl.stem + ".meta.json"))


_JOURNALS: dict = {}


def _journal_status(wf_dir: Path, agent_id: str) -> str:
    """A workflow agent is finished when its journal says ``result`` or ``failed``."""
    path = wf_dir / "journal.jsonl"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return ""
    cached = _JOURNALS.get(path)
    if not cached or cached[0] != stamp:
        status: dict = {}
        for row in read_tail(path, 1024 * 1024, 65536)[0]:
            if row.get("agentId") and row.get("type") in ("result", "failed", "started"):
                status[str(row["agentId"])] = str(row["type"])
        cached = _JOURNALS[path] = (stamp, status)
    return cached[1].get(agent_id, "") if cached[1].get(agent_id) != "started" else ""


def _finished_by_transcript(agent: Agent) -> str:
    if any(c.name == "SubagentHandback" for c in agent.calls[-5:]):
        return "handed back"
    turns = [t for t in agent.turns if not t.api_error]
    if turns and turns[-1].stop == "end_turn" and turns[-1].text and (
            not agent.calls or agent.calls[-1].seq < turns[-1].seq):
        return "final report"
    return ""


def _new_agent(sid: str, agent_id: str, kind: str, path: Path, project: str, st) -> Agent:
    key = f"{sid}/{agent_id or 'main'}"
    return Agent(key=key, session_id=sid, agent_id=agent_id, kind=kind, path=path,
                 project=project, last_write=st.st_mtime)


def _load_agent(agent: Agent, entries: list, truncated: bool, meta: dict) -> Agent:
    parse_entries(agent, entries)
    agent.truncated = truncated
    agent.description = str(meta.get("description") or agent.description or "")
    agent.agent_type = str(meta.get("agentType") or agent.agent_type or "")
    if not agent.model and meta.get("model"):
        agent.model = str(meta["model"])
    compute_progress(agent)
    return agent


def discover(paths: Paths, cfg: dict, now: float, include_finished: bool = False) -> Scan:
    sessions = load_sessions(paths)
    scan = Scan(now=now, sessions=sessions, agents=[], router=read_router(paths, cfg, now))
    active_s = cfg["active_minutes"] * 60
    silent_s = cfg["silent_max_minutes"] * 60
    horizon_s = cfg["horizon_hours"] * 3600
    seen_ids: set = set()
    seen_files: set = set()

    def wanted(st, sid: str, subagent: bool) -> bool:
        age = now - st.st_mtime
        if age <= active_s:
            return True
        live = sessions.get(sid) and sessions[sid].alive
        return (bool(subagent and live and age <= silent_s)
                or (include_finished and age <= horizon_s))

    def file_key(p: Path, st) -> tuple:
        return (st.st_dev, st.st_ino) if st.st_ino else (str(p).lower(),)

    try:
        projects = [p for p in paths.projects.iterdir() if p.is_dir()]
    except OSError:
        projects = []
    for proj in projects:
        try:
            mains = list(proj.glob("*.jsonl"))
        except OSError:
            continue
        for main in mains:
            sid = main.stem
            try:
                st = main.stat()
            except OSError:
                continue
            live = sessions.get(sid) and sessions[sid].alive
            if now - st.st_mtime > horizon_s and not live:
                continue
            if wanted(st, sid, False):
                entries, cut = read_tail(main, cfg["tail_bytes"], cfg["max_line_bytes"])
                own = [e for e in entries if not e.get("isSidechain")]
                agent = _load_agent(_new_agent(sid, "", "main", main, proj.name, st), own, cut, {})
                agent.started = read_head_ts(main)
                if sessions.get(sid) and not agent.cwd:
                    agent.cwd = sessions[sid].cwd
                scan.agents.append(agent)
                seen_files.add(file_key(main, st))
                side: dict = {}
                for e in entries:
                    if e.get("isSidechain"):
                        side.setdefault(str(e.get("agentId") or "sidechain"), []).append(e)
                for aid, rows in side.items():
                    sa = _new_agent(sid, aid, "subagent", main, proj.name, st)
                    sa = _load_agent(sa, rows, cut, {})
                    sa.started = _ts(rows[0].get("timestamp"))
                    sa.finished = _finished_by_transcript(sa)
                    scan.agents.append(sa)
                    seen_ids.add((sid, aid))
            sub_root = proj / sid / "subagents"
            if not sub_root.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(sub_root):
                for name in files:
                    if not (name.startswith("agent-") and name.endswith(".jsonl")):
                        continue
                    p = Path(dirpath) / name
                    try:
                        pst = p.stat()
                    except OSError:
                        continue
                    aid = name[len("agent-"):-len(".jsonl")]
                    if (sid, aid) in seen_ids or not wanted(pst, sid, True):
                        continue
                    meta = _meta_for(p)
                    finished = "meta finished" if meta.get("finished") else ""
                    if not finished and Path(dirpath).parent.name == "workflows":
                        finished = _journal_status(Path(dirpath), aid)
                        finished = f"workflow {finished}" if finished else ""
                    if finished and not include_finished:
                        scan.skipped["finished"] += 1
                        seen_ids.add((sid, aid))
                        continue
                    entries, cut = read_tail(p, cfg["tail_bytes"], cfg["max_line_bytes"])
                    agent = _load_agent(_new_agent(sid, aid, "subagent", p, proj.name, pst),
                                        entries, cut, meta)
                    agent.started = read_head_ts(p)
                    agent.finished = finished or _finished_by_transcript(agent)
                    if agent.finished and not include_finished:
                        scan.skipped["finished"] += 1
                    else:
                        scan.agents.append(agent)
                    seen_ids.add((sid, aid))
                    seen_files.add(file_key(p, pst))
    # %TEMP%/claude/<project>/<sid>/tasks/a*.output: hard links of the same transcripts on
    # this host; only an agent the projects tree did not have is read from here.
    try:
        outputs = list(paths.temp_claude.glob("*/*/tasks/a*.output"))
    except OSError:
        outputs = []
    for p in outputs:
        sid, aid = p.parent.parent.name, p.stem
        try:
            pst = p.stat()
        except OSError:
            continue
        if (sid, aid) in seen_ids or file_key(p, pst) in seen_files or pst.st_size == 0:
            continue
        if not wanted(pst, sid, True):
            continue
        entries, cut = read_tail(p, cfg["tail_bytes"], cfg["max_line_bytes"])
        if not entries or not any(e.get("agentId") for e in entries[:5]):
            continue
        agent = _load_agent(_new_agent(sid, aid, "subagent", p, p.parent.parent.parent.name, pst),
                            entries, cut, {})
        agent.started = read_head_ts(p)
        agent.finished = _finished_by_transcript(agent)
        if agent.finished and not include_finished:
            scan.skipped["finished"] += 1
            continue
        scan.agents.append(agent)
        seen_ids.add((sid, aid))
    return scan


# --------------------------------------------------------------------------- router trace


@dataclass
class Streak:
    model: str
    count: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    last_ok_ts: float = 0.0
    rows: int = 0
    kinds: Counter = field(default_factory=Counter)

    def describe(self) -> str:
        kinds = ", ".join(f"{k} x{n}" for k, n in self.kinds.most_common(4))
        return (f"router: {self.count} failed requests in a row on {self.model} since "
                f"{_hm(self.first_ts)} ({kinds}), last {_hm(self.last_ts)}")


def router_failure(row: dict, cfg: dict) -> str:
    status = row.get("upstream_status")
    error = str(row.get("error") or "")
    if status == 401:
        return "401"
    if isinstance(status, int) and 500 <= status <= 599:
        return str(status)
    if status in (408, 504, 524) or re.search(r"timed? ?out|timeout", error, re.I):
        return "timeout"
    if error and cfg.get("router_transport_errors", True):
        return "transport"
    return ""


def read_router(paths: Paths, cfg: dict, now: float) -> dict:
    def days(when: float) -> list:
        out = []
        for tz in (None, timezone.utc):  # the file is named by one of them; read both
            day = datetime.fromtimestamp(when, tz).strftime("%Y-%m-%d")
            if day not in out:
                out.append(day)
        return out

    today = days(now)
    rows: list = []
    for group in (today, [d for d in days(now - 86400) if d not in today]):
        for day in group:
            p = paths.trace / f"router-{day}.jsonl"
            if p.is_file():
                rows += read_tail(p, cfg["router_tail_bytes"], cfg["max_line_bytes"])[0]
        if rows:  # yesterday's file only when today's has nothing yet (just past midnight)
            break
    seen: set = set()
    ordered = []
    for r in rows:
        key = (r.get("ts"), r.get("requested_model"), r.get("total_ms"), r.get("upstream_status"))
        if r.get("requested_model") and key not in seen:
            seen.add(key)
            ordered.append((_ts(r.get("ts")), r))
    ordered.sort(key=lambda x: x[0])
    out: dict = {}
    for t, r in ordered:
        s = out.setdefault(str(r["requested_model"]), Streak(str(r["requested_model"])))
        s.rows += 1
        kind = router_failure(r, cfg)
        if kind:
            if s.count == 0:
                s.first_ts, s.kinds = t, Counter()
            s.count += 1
            s.last_ts = t
            s.kinds[kind] += 1
        else:
            s.count, s.kinds, s.last_ok_ts = 0, Counter(), t
    return out


# --------------------------------------------------------------------------- detectors


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def _mins(seconds: float) -> str:
    m = int(max(0, seconds) // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def is_cheap(model: str, cfg: dict) -> bool:
    m = (model or "").lower()
    return any(k in m for k in cfg["cheap_tier_markers"]) or m in cfg["cheap_tier_aliases"]


def signature(call: Call, cfg: dict) -> str:
    inp = {k: v for k, v in call.input.items() if k not in cfg["repeat_call_ignore_keys"]}
    return call.name + ":" + json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)


def call_label(call: Call) -> str:
    inp = call.input
    for key in ("file_path", "notebook_path", "command", "pattern", "url", "query",
                "description", "prompt", "path", "task_id", "taskId"):
        if inp.get(key):
            arg = str(inp[key])
            if key == "file_path" and inp.get("offset"):
                arg += f" @{inp['offset']}"
            return f"{call.name}({clip(redact(arg), 90)})"
    for value in inp.values():
        if isinstance(value, str) and value:
            return f"{call.name}({clip(redact(value), 60)})"
    return f"{call.name}()"


def normalize_error(text: str) -> str:
    t = re.sub(r"</?tool_use_error>", "", str(text or "").replace("\r", ""))
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    t = " ".join(lines[:4])
    t = re.sub(r"0x[0-9a-fA-F]+|\b[0-9a-f]{8,}\b|\d+", "#", t)
    return re.sub(r"\s+", " ", t).strip()[:200].lower()


def _no_progress_need(agent: Agent, cfg: dict) -> int:
    if agent.kind == "main":
        return cfg["no_progress_turns_main"]
    return cfg["no_progress_turns_by_type"].get(agent.agent_type, cfg["no_progress_turns"])


def detect(agent: Agent, cfg: dict, now: float, router: dict) -> list:
    out: list = []
    last_seq = agent.progress[-1][0] if agent.progress else 0
    last_prog = agent.progress[-1] if agent.progress else None
    since_txt = (f"since the last progress ({last_prog[1]} at {_hm(last_prog[3])})" if last_prog
                 else "with no progress in the transcript read")
    window = agent.calls[-cfg["window_calls"]:]
    cheap = is_cheap(agent.model, cfg)

    def add(detector: str, summary: str, steer: str, evidence: list) -> None:
        out.append(Finding(agent, detector, summary, steer, evidence,
                           upgrade=cheap and detector in cfg["upgrade_detectors"]))

    # a. the same call again and again, with no progress between them
    since = [c for c in window if c.seq > last_seq]
    counts = Counter(signature(c, cfg) for c in since)
    first = {}
    for c in since:
        first.setdefault(signature(c, cfg), c)
    for sig, n in counts.most_common():
        call = first[sig]
        if n >= cfg["repeat_call_min_by_tool"].get(call.name, cfg["repeat_call_min"]):
            label = call_label(call)
            add("repeat_call", f"repeated {label} {n}x with no progress",
                f"You repeated {label} {n} times with no progress. Change approach or "
                "report BLOCKED with what you tried.",
                [f"{label} called {n} times {since_txt}",
                 f"{len(since)} tool calls {since_txt}, {len(counts)} distinct"])
            break

    # b. the same error text from the same tool
    errors: Counter = Counter()
    sample: dict = {}
    for c in window:
        r = agent.results.get(c.id)
        if r is None or not r.is_error or HUMAN_ERROR_RE.search(r.text or ""):
            continue
        norm = normalize_error(r.text)
        if norm:
            errors[(c.name, norm)] += 1
            sample.setdefault((c.name, norm), (c, r))
    for (tool, _norm), n in errors.most_common(1):
        if n >= cfg["repeat_error_min"]:
            c, r = sample[(tool, _norm)]
            err = clip(redact(r.text), 140)
            add("repeat_error", f"{tool} failed {n}x with the same error",
                f'{tool} failed {n} times with the same error: "{err}". Stop retrying it; '
                "fix the cause or report BLOCKED with what you tried.",
                [f"{tool} failed {n} times in the last {len(window)} calls with: {err}",
                 f"first: {call_label(c)}"])

    # c. turns since the last progress signal
    quiet = [t for t in agent.turns if t.seq > last_seq and not t.api_error]
    need = _no_progress_need(agent, cfg)
    quiet_since = last_prog[3] if last_prog else (agent.started or (quiet[0].ts if quiet else now))
    if len(quiet) >= need and now - quiet_since >= cfg["no_progress_min_minutes"] * 60:
        n_calls = sum(1 for c in agent.calls if c.seq > last_seq)
        add("no_progress", f"{len(quiet)} turns {since_txt}",
            f"{len(quiet)} turns {since_txt}: no changed file, passing test, commit, "
            "completed todo or report. Report what you have now, or BLOCKED with what you "
            "tried.",
            [f"{len(quiet)} assistant turns and {n_calls} tool calls {since_txt} "
             f"(threshold {need})"] + ([f"transcript tail read: {len(agent.turns)} turns"]
                                       if agent.truncated else []))

    # d. an edit undone on the same file
    pairs: dict = {}
    writes: dict = {}
    flips: Counter = Counter()
    for c in window:
        r = agent.results.get(c.id)
        if r is None or r.is_error:
            continue
        path = str(c.input.get("file_path") or "")
        edits = []
        if c.name == "Edit":
            edits = [(c.input.get("old_string"), c.input.get("new_string"))]
        elif c.name == "MultiEdit":
            edits = [(e.get("old_string"), e.get("new_string")) for e in c.input.get("edits") or []
                     if isinstance(e, dict)]
        elif c.name == "Write":
            digest = _digest(c.input.get("content"))
            hist = writes.setdefault(path, [])
            if len(hist) >= 2 and digest in hist[:-1] and hist[-1] != digest:
                flips[path] += 1
            hist.append(digest)
        for old, new in edits:
            if old is None or new is None or old == new:
                continue
            prev = pairs.setdefault(path, [])
            if any(po == new and pn == old for po, pn in prev):
                flips[path] += 1
            prev.append((old, new))
    for path, n in flips.most_common(1):
        if n >= cfg["oscillation_min"]:
            name = Path(path).name or path
            add("oscillation", f"undid its own edit to {name} {n}x (A->B->A)",
                f"You reverted your own edit to {path} {n} time(s) (A -> B -> A). Pick one "
                "version, verify it, or report BLOCKED with what you tried.",
                [f"{path}: {n} edit(s) restored an earlier text in the last {len(window)} calls"])

    # e. the upstream keeps failing: the agent's own API-error turns, then the router
    streak = 0
    kinds: Counter = Counter()
    last_err = 0.0
    for t in reversed(agent.turns):
        if not t.api_error:
            break
        if t.api_error in cfg["upstream_error_kinds"] or "timed out" in t.error_text.lower():
            streak += 1
            kinds[f"{t.api_error}: {t.error_text}"] += 1
            last_err = max(last_err, t.ts)
    recent = max(cfg["upstream_recent_minutes"], cfg["active_minutes"]) * 60
    evidence = []
    if streak >= cfg["upstream_streak_min"] and now - last_err <= recent:
        top = kinds.most_common(1)[0][0]
        evidence.append(f"{streak} API errors in a row in its transcript ({clip(top, 90)}), "
                        f"last {_hm(last_err)}")
    # A router row names no agent, so the streak is laid on an agent only when it was
    # alive during the streak (wrote something since shortly before it began), has had no
    # good turn since it began, and is mid-turn. A transcript says `model[1m]` where the
    # router logs `model`.
    st = router.get(re.sub(r"\[[^\]]*\]$", "", agent.model or ""))
    ok_turns = [t.ts for t in agent.turns if not t.api_error]
    mid_turn = agent.kind != "main" or not (agent.turns and agent.turns[-1].stop == "end_turn")
    # a running agent may hang on a request it sent just before the streak began
    grace = 0 if agent.finished else cfg["upstream_recent_minutes"] * 60
    if (st and st.count >= cfg["upstream_streak_min"]
            and now - st.last_ts <= cfg["upstream_recent_minutes"] * 60
            and agent.last_write >= st.first_ts - grace
            and (not ok_turns or max(ok_turns) < st.first_ts) and mid_turn):
        evidence.append(st.describe())
    if evidence:
        hint = (f' The lead should re-run it on {cfg["upgrade_label"]} (model '
                f'"{cfg["upgrade_alias"]}").' if cheap else "")
        add("upstream_streak", f"its model {agent.model or '?'} keeps failing upstream",
            f"Your model {agent.model or '?'} is failing upstream ({evidence[0]}). If you can "
            f"still act, report BLOCKED with where you got to.{hint}", evidence)

    # f. past the wall budget with no final report
    budget = (cfg["wall_budget_minutes"] if agent.kind == "subagent"
              else cfg["wall_budget_minutes_main"])
    if budget and not agent.finished and agent.started and now - agent.started > budget * 60:
        ran = _mins(now - agent.started)
        add("wall_budget", f"running {ran} (budget {budget}m) with no final report",
            f"You have run {ran} (budget {budget} min) with no final report. Report now: "
            "done, partial, or BLOCKED with what you tried.",
            [f"started {_hm(agent.started)}, running {ran}, last write "
             f"{_mins(now - agent.last_write)} ago, no final report (budget {budget} min)"])
    return out


# --------------------------------------------------------------------------- state + events


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def load_state(paths: Paths) -> dict:
    st = _read_json(paths.home / "state.json")
    st.setdefault("incidents", {})
    st.setdefault("sent", {"steer": {}, "relay": [], "card": []})
    st.setdefault("cards", {})
    return st


def save_state(paths: Paths, state: dict) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    tmp = paths.home / f"state.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, paths.home / "state.json")


def log_event(paths: Paths, record: dict) -> None:
    try:
        paths.home.mkdir(parents=True, exist_ok=True)
        with open(paths.home / f"events-{_today()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:  # a full disk must not stop the scan it describes
        sys.stderr.write(f"awsupervise: events log not written: {exc}\n")


# --------------------------------------------------------------------------- actions


def write_steer(steer_root: Path, sid: str, lines: list, event_id: str) -> Path:
    """One mailbox file, same shape as adk.decisions.store.write_steer: a v1 header that
    declares PEER authority on line 1, written to a temp name and published whole."""
    if not SESSION_RE.match(sid or "") or sid in (".", ".."):
        raise ValueError(f"not a session id: {sid!r}")
    event = re.sub(r"[^A-Za-z0-9-]", "-", event_id)[:64].strip("-") or "sup"
    box = steer_root / sid
    box.mkdir(parents=True, exist_ok=True)
    base = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{event}-supervise"
    target = box / f"{base}.md"
    n = 1
    while target.exists():
        target = box / f"{base}-{n}.md"
        n += 1
    header = (f'<!-- aither-steer v1 authority="peer" from="awsupervise" kind="supervisor" '
              f'event="{event}" -->')
    tmp = box / f"{target.name}.{os.getpid()}.tmp"
    tmp.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def _run(argv: list, timeout: float) -> tuple:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _cli(name: str, module: str) -> list:
    exe = shutil.which(name)
    return [exe] if exe else [sys.executable, "-m", module]


@dataclass
class Planned:
    level: int
    finding: Finding
    iid: str


def _event_id(*parts: str) -> str:
    return "sup-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def _session_name(sessions: dict, sid: str) -> str:
    s = sessions.get(sid)
    return clip(s.name, 50) if s and s.name else sid.split("-")[0]


def _recommend(findings: list) -> str:
    if any(f.upgrade for f in findings):
        return "fable"
    if any(f.detector in ("repeat_call", "repeat_error", "oscillation", "no_progress")
           for f in findings):
        return "stop"
    return "continue"


def steer_text(sid: str, items: list, sessions: dict, cfg: dict) -> list:
    by_agent: dict = {}
    for p in items:
        by_agent.setdefault(p.finding.agent.key, []).append(p.finding)
    lines = [f"# awsupervise: {len(by_agent)} agent(s) in this session look stuck", "",
             "The host supervisor reads this box's transcripts for loops and stalls. It is "
             "a peer report, not the owner: judge it on its merits.", ""]
    has_sub = False
    for findings in by_agent.values():
        agent = findings[0].agent
        has_sub = has_sub or agent.kind == "subagent"
        who = "You (the main session)" if agent.kind == "main" else agent.label()
        lines.append(f"## {who}")
        for f in findings:
            lines.append(f"- [{DETECTOR_CODE[f.detector]}] {redact(f.steer)}")
        if any(f.upgrade for f in findings):
            lines.append(f'- Recommended: re-run this task on {cfg["upgrade_label"]} '
                         f'(model "{cfg["upgrade_alias"]}").')
        lines.append("")
    lines.append("Who should act:")
    if has_sub:
        lines += ["- The named subagent: change approach now, or stop and report BLOCKED "
                  "with what you tried.",
                  "- The lead session (its parent): check it (TaskOutput / SendMessage); stop "
                  f'it (TaskStop) or re-dispatch it with model "{cfg["upgrade_alias"]}" if it '
                  "stays stuck.",
                  "- Any other agent in this session: ignore this note."]
    else:
        lines.append("- You: change approach now, or report BLOCKED with what you tried.")
    lines.append(f"(next: #agents in {cfg['climb_minutes']} min, then a card to the owner)")
    return lines


def relay_text(sid: str, items: list, sessions: dict, cfg: dict) -> str:
    parts = []
    for p in items[:4]:
        a = p.finding.agent
        who = "main" if a.kind == "main" else clip(a.description or a.agent_id, 40)
        parts.append(f"{who} ({a.model or '?'}) [{DETECTOR_CODE[p.finding.detector]}] "
                     f"{p.finding.summary}")
    more = f" (+{len(items) - 4} more)" if len(items) > 4 else ""
    rec = _recommend([p.finding for p in items])
    rec_txt = {"fable": f'recommend re-run on {cfg["upgrade_label"]} ("{cfg["upgrade_alias"]}")',
               "stop": "recommend stopping it", "continue": "watching"}[rec]
    text = (f'awsupervise: session {sid.split("-")[0]} "{_session_name(sessions, sid)}" still '
            f"stuck after a steer: " + "; ".join(parts) + more + f". {rec_txt}.")
    return clip(redact(text), 900)


def card_args(sid: str, items: list, sessions: dict, cfg: dict, agent_cmd: list) -> list:
    findings = [p.finding for p in items]
    agents: dict = {}
    for f in findings:
        agents.setdefault(f.agent.key, f.agent)
    first = next(iter(agents.values()))
    name = _session_name(sessions, sid)
    if len(agents) == 1:
        what = ("the main session" if first.kind == "main"
                else f'"{clip(first.description or first.agent_type, 50)}" ({first.agent_id[:10]})')
        title = f"Agent stuck: {what} in {name}"
    else:
        title = f"{len(agents)} agents stuck in {name}"
    ids = ", ".join("main" if a.kind == "main" else a.agent_id for a in agents.values())
    facts = []
    for f in findings:
        for ev in f.evidence[:2]:
            line = f"{f.agent.label()}: [{DETECTOR_CODE[f.detector]}] {ev}"
            facts.append(clip(redact(line), 400))
    rec = _recommend(findings)
    detail = "\n".join(
        [f"Session {sid} ({name}). awsupervise steered it through the session mailbox and "
         f"posted to {cfg['relay_channel']}; it is still stuck.", "",
         "Answering routes to that session's lead with owner authority:",
         f"- stop: the lead stops {ids} (TaskStop) and reports what it had.",
         f'- fable: the lead re-dispatches the same task with model "{cfg["upgrade_alias"]}" '
         f'({cfg["upgrade_label"]}).',
         "- continue: nothing changes; awsupervise stays quiet about it for "
         f'{cfg["owner_hold_minutes"]["continue"] // 60} h.'])
    argv = list(agent_cmd) + [
        "decide", "ask", clip(redact(title), 150),
        "--summary", clip(redact("; ".join(sorted({f.summary for f in findings}))), 300),
        "--detail", detail,
        "--option", f"stop|Stop it|The lead session stops {ids} and reports what it had; "
                    "the task stays unfinished",
        "--option", f'fable|Re-run it on {cfg["upgrade_label"]}|The lead re-dispatches the '
                    f'same task with model "{cfg["upgrade_alias"]}"; Opus tokens are spent',
        "--option", "continue|Let it continue|Nothing changes; awsupervise holds further "
                    f'escalation for {cfg["owner_hold_minutes"]["continue"] // 60} h',
        "--recommend", rec, "--default", "continue", "--deadline", cfg["card_deadline"],
        "--urgency", cfg["card_urgency"], "--kind", "decision",
        "--session", sid, "--agent", "awsupervise",
        "--subject", f"session:{sid.split('-')[0]}", "--json", "--quiet"]
    cwd = first.cwd or (sessions.get(sid).cwd if sessions.get(sid) else "")
    if cwd:
        argv += ["--cwd", cwd]
    if first.kind == "main" and first.path.is_file():
        argv += ["--transcript", str(first.path)]
    for fact in facts[:8]:
        argv += ["--fact", fact]
    return argv


class Actuator:
    """Performs (or, dry, describes) one rung for one session. Rate limits live here."""

    def __init__(self, paths: Paths, cfg: dict, state: dict, now: float, sessions: dict,
                 dry: bool = False, runner: Optional[Callable] = None) -> None:
        self.paths, self.cfg, self.state, self.now = paths, cfg, state, now
        self.sessions, self.dry = sessions, dry
        self.runner = runner or _run

    def _recent(self, stamps: list) -> list:
        return [t for t in stamps if self.now - t < 3600]

    def _allowed(self, kind: str, sid: str) -> bool:
        sent = self.state.setdefault("sent", {})
        if kind == "steer":
            per = sent.setdefault("steer", {})
            per[sid] = self._recent(per.get(sid, []))
            return len(per[sid]) < self.cfg["max_steers_per_session_per_hour"]
        sent[kind] = self._recent(sent.get(kind, []))
        cap = self.cfg["max_relays_per_hour"] if kind == "relay" else self.cfg["max_cards_per_hour"]
        return len(sent[kind]) < cap

    def _mark(self, kind: str, sid: str) -> None:
        sent = self.state.setdefault("sent", {})
        if kind == "steer":
            sent.setdefault("steer", {}).setdefault(sid, []).append(self.now)
        else:
            sent.setdefault(kind, []).append(self.now)

    def act(self, level: int, sid: str, items: list) -> dict:
        kind = {1: "steer", 2: "relay", 3: "card"}[level]
        rec: dict = {"action": kind, "level": level, "session": sid,
                     "agents": sorted({p.finding.agent.key for p in items}),
                     "detectors": sorted({p.finding.detector for p in items}),
                     "dry": self.dry}
        if not self._allowed(kind, sid):
            # A steer or relay over its cap still counts as the rung reached, so the
            # ladder keeps climbing; a card over its cap waits for the next scan.
            rec.update(result="suppressed: rate limit", advance=kind != "card")
            return rec
        if kind == "steer":
            lines = steer_text(sid, items, self.sessions, self.cfg)
            rec["text"] = "\n".join(lines)
            if self.dry:
                rec.update(result=f"would write {self.paths.steer / sid}/<stamp>-supervise.md",
                           advance=True)
                return rec
            try:
                path = write_steer(self.paths.steer, sid, lines,
                                   _event_id(sid, str(self.now), *rec["agents"]))
                rec.update(result=f"wrote {path}", path=str(path), advance=True)
            except (OSError, ValueError) as exc:
                rec.update(result=f"failed: {exc}", advance=False)
        elif kind == "relay":
            text = relay_text(sid, items, self.sessions, self.cfg)
            argv = _cli("awrelay", "awrelay") + ["send", self.cfg["relay_channel"], text,
                                                 "--kind", "finding", "--to", sid.split("-")[0]]
            rec.update(text=text, argv=argv[1:])
            if self.dry:
                rec.update(result="would send", advance=True)
                return rec
            rc, out, err = self.runner(argv, self.cfg["subprocess_timeout_s"])
            note = clip(redact((err or out).strip()), 200)
            rec.update(result=f"exit {rc}" + (f": {note}" if note else ""), advance=rc == 0)
        else:
            open_card = self.state.get("cards", {}).get(sid)
            if open_card and card_status(self.paths, open_card.get("id", "")) == "open":
                rec.update(result=f"joined open card {open_card['id']}", advance=True,
                           card=open_card["id"])
                open_card.setdefault("incidents", [])
                joined = set(open_card["incidents"]) | {p.iid for p in items}
                open_card["incidents"] = sorted(joined)
                return rec
            argv = card_args(sid, items, self.sessions, self.cfg, _cli("adk", "adk"))
            rec.update(text=argv[argv.index("ask") + 1], argv=argv[1:],
                       recommend=argv[argv.index("--recommend") + 1])
            if self.dry:
                rec.update(result="would raise a card", advance=True)
                return rec
            rc, out, err = self.runner(argv, self.cfg["subprocess_timeout_s"])
            card_id = ""
            try:
                card_id = str(json.loads(out).get("id") or "")
            except (ValueError, AttributeError):
                match = re.search(r"\bd-[a-z0-9]{3,8}\b", out or "")
                card_id = match.group(0) if match else ""
            note = clip(redact((err or "").strip()), 200)
            rec.update(result=f"exit {rc}" + (f" card {card_id}" if card_id else "")
                       + (f": {note}" if note and rc else ""), advance=rc == 0, card=card_id)
            if rc == 0 and card_id:
                self.state.setdefault("cards", {})[sid] = {
                    "id": card_id, "at": self.now, "incidents": sorted(p.iid for p in items)}
        if rec.get("advance") and not self.dry:
            self._mark(kind, sid)
        return rec


def card_status(paths: Paths, card_id: str) -> str:
    if not re.match(r"^d-[A-Za-z0-9]{2,12}$", card_id or ""):
        return ""
    rec = _read_json(paths.decisions / f"{card_id}.json")
    return str(rec.get("status") or "")


def poll_cards(paths: Paths, state: dict, cfg: dict, now: float, events: list) -> None:
    """An answered card holds the incidents it was raised for."""
    for sid, rec in list(state.get("cards", {}).items()):
        status = card_status(paths, str(rec.get("id") or ""))  # checks the id's shape first
        card = _read_json(paths.decisions / f"{rec['id']}.json") if status else {}
        if not card or status == "open":
            continue
        answer = str(card.get("answer") or "")
        if answer.isdigit():
            keys = ["stop", "fable", "continue"]
            answer = keys[int(answer) - 1] if 1 <= int(answer) <= 3 else answer
        hold = cfg["owner_hold_minutes"].get(answer or "continue", 120) * 60
        for iid in rec.get("incidents", []):
            inc = state["incidents"].get(iid)
            if inc:
                inc["hold_until"] = now + hold
                inc["owner"] = answer or status
        events.append({"action": "card-closed", "session": sid, "card": rec.get("id"),
                       "status": status, "answer": answer, "hold_min": hold // 60,
                       "incidents": rec.get("incidents", [])})
        del state["cards"][sid]


def plan(findings: list, state: dict, cfg: dict, now: float, events: list) -> list:
    incidents = state.setdefault("incidents", {})
    planned: list = []
    seen: set = set()
    climb = cfg["climb_minutes"] * 60
    for f in findings:
        iid = f"{f.agent.key}|{f.detector}"
        if iid in seen:
            continue
        seen.add(iid)
        inc = incidents.get(iid)
        grace = cfg["resolve_grace_minutes"] * 60
        if inc and inc.get("resolved") and now - inc["resolved"] > grace:
            inc = None
        if inc is None:
            inc = incidents[iid] = {"agent": f.agent.key, "session": f.agent.session_id,
                                    "detector": f.detector, "first": now, "level": 0,
                                    "level_at": 0}
            events.append({"action": "detect", "incident": iid, "summary": f.summary,
                           "evidence": [redact(e) for e in f.evidence], "model": f.agent.model,
                           "upgrade": f.upgrade})
        elif inc.get("resolved"):
            events.append({"action": "re-tripped", "incident": iid, "level": inc["level"]})
        inc.update(last=now, summary=f.summary, resolved=None, model=f.agent.model)
        hold = inc.get("hold_until") or 0
        if hold and now < hold:
            continue
        if hold:  # the owner's hold ran out and it is still stuck: ask again
            inc["hold_until"] = 0
            inc["level"], inc["level_at"] = min(inc["level"], 2), now - climb
        level = inc["level"]
        if level == 0:
            target = cfg["start_level"].get(f.detector, 1)
        elif level < 3 and now - inc["level_at"] >= climb:
            target = level + 1
        else:
            continue
        planned.append(Planned(target, f, iid))
    for iid, inc in list(incidents.items()):
        if iid not in seen and not inc.get("resolved"):
            inc["resolved"] = now
            events.append({"action": "resolved", "incident": iid, "level": inc.get("level", 0)})
        if inc.get("resolved") and now - inc["resolved"] > 86400:
            del incidents[iid]
    return planned


def execute(planned: list, state: dict, actuator: Actuator, now: float) -> list:
    groups: dict = {}
    for p in planned:
        groups.setdefault((p.finding.agent.session_id, p.level), []).append(p)
    records = []
    for (sid, level), items in sorted(groups.items()):
        rec = actuator.act(level, sid, items)
        if rec.get("advance"):
            for p in items:
                inc = state["incidents"][p.iid]
                inc["level"], inc["level_at"] = level, now
        records.append(rec)
    return records


# --------------------------------------------------------------------------- one scan


def scan_once(paths: Paths, cfg: dict, now: Optional[float] = None, dry: bool = False,
              runner: Optional[Callable] = None, include_finished: bool = False) -> dict:
    now = time.time() if now is None else now
    scan = discover(paths, cfg, now, include_finished=include_finished)
    state = load_state(paths)
    if dry:
        state = copy.deepcopy(state)
    findings: list = []
    for agent in scan.agents:
        findings += detect(agent, cfg, now, scan.router)
    live = [f for f in findings if not f.agent.finished]
    events: list = []
    poll_cards(paths, state, cfg, now, events)
    planned = plan(live, state, cfg, now, events)
    actuator = Actuator(paths, cfg, state, now, scan.sessions, dry=dry, runner=runner)
    records = execute(planned, state, actuator, now)
    steers = state.setdefault("sent", {}).setdefault("steer", {})
    for sid in list(steers):  # an hour-old stamp limits nothing; do not keep it forever
        steers[sid] = [t for t in steers[sid] if now - t < 3600]
        if not steers[sid]:
            del steers[sid]
    if not dry:
        for rec in events + records:
            log_event(paths, {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(), **rec})
        save_state(paths, state)
    return {"now": now, "scan": scan, "findings": findings, "events": events,
            "actions": records, "state": state}


# --------------------------------------------------------------------------- report


def render(result: dict, verbose: bool = False) -> str:
    scan: Scan = result["scan"]
    now = result["now"]
    live = sum(1 for s in scan.sessions.values() if s.alive)
    by_agent: dict = {}
    for f in result["findings"]:
        by_agent.setdefault(f.agent.key, []).append(f)
    out = [f"awsupervise {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S}  sessions {live} live | "
           f"agents scanned {len(scan.agents)} (finished skipped {scan.skipped['finished']}) | "
           f"{len(result['findings'])} finding(s) on {len(by_agent)} agent(s)"]
    for st in sorted(scan.router.values(), key=lambda s: -s.count):
        if st.count >= 3:
            age = _mins(now - st.last_ts)
            out.append(f"  {st.describe()} ({age} ago)")
    for key, findings in sorted(by_agent.items(), key=lambda kv: kv[0]):
        a = findings[0].agent
        ran = _mins(now - a.started) if a.started else "?"
        state = f", {a.finished}" if a.finished else ""
        out.append("")
        out.append(f'[{a.short} "{_session_name(scan.sessions, a.session_id)}"] {a.label()} '
                   f"ran {ran}, last write {_mins(now - a.last_write)} ago{state}")
        for f in findings:
            up = f'  -> {CONFIG["upgrade_label"]}' if f.upgrade else ""
            out.append(f"  {DETECTOR_CODE[f.detector]} {f.detector:<16} {redact(f.summary)}{up}")
            if verbose:
                for ev in f.evidence:
                    out.append(f"      {redact(ev)}")
    if result["events"] or result["actions"]:
        out.append("")
        dry = any(r.get("dry") for r in result["actions"])
        out.append("actions" + (" (DRY RUN: nothing written)" if dry else ""))
    for rec in result["actions"]:
        out.append(f"  L{rec['level']} {rec['action']:<5} {rec['session'].split('-')[0]} "
                   f"{','.join(DETECTOR_CODE[d] for d in rec['detectors'])} "
                   f"{len(rec['agents'])} agent(s): {rec.get('result', '')}")
        if verbose and rec.get("text"):
            for ln in str(rec["text"]).splitlines()[:40]:
                out.append(f"      | {ln}")
    for ev in result["events"]:
        if ev["action"] in ("resolved", "card-closed", "re-tripped"):
            out.append(f"  {ev['action']}: {ev.get('incident') or ev.get('card')}")
    return "\n".join(out)


def as_json(result: dict) -> dict:
    router = {m: {"streak": s.count, "since": s.first_ts, "last": s.last_ts,
                  "kinds": dict(s.kinds)}
              for m, s in result["scan"].router.items() if s.count}
    findings = [{"agent": f.agent.key, "kind": f.agent.kind,
                 "description": f.agent.description, "model": f.agent.model,
                 "detector": f.detector, "code": DETECTOR_CODE[f.detector],
                 "summary": redact(f.summary), "evidence": [redact(e) for e in f.evidence],
                 "upgrade": f.upgrade, "finished": f.agent.finished}
                for f in result["findings"]]
    return {"now": result["now"], "agents_scanned": len(result["scan"].agents),
            "router": router, "findings": findings, "actions": result["actions"],
            "events": result["events"]}


# --------------------------------------------------------------------------- lock + main


class _Lock:
    """One supervisor acting at a time (a wake and a hand-run must not both steer)."""

    def __init__(self, path: Path) -> None:
        self.path, self.fh = path, None

    def __enter__(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __exit__(self, *exc) -> None:
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError as exc:  # the lock dies with this process anyway
                sys.stderr.write(f"awsupervise: unlock failed: {exc}\n")
            self.fh.close()


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue


def main(argv: Optional[list] = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(prog="awsupervise", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="scan, act, exit")
    mode.add_argument("--watch", action="store_true", help="scan every --interval seconds")
    mode.add_argument("--self-test", action="store_true",
                      help="synthetic transcripts, no real notifications")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and print the would-be actions; write nothing")
    ap.add_argument("--interval", type=float, default=120.0)
    ap.add_argument("--include-finished", action="store_true",
                    help="with --dry-run: also judge agents that already finished (never acted on)")
    ap.add_argument("--active-minutes", type=float, default=None,
                    help=f"override the live window (default {CONFIG['active_minutes']})")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="evidence and message text")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not (a.once or a.watch or a.dry_run):
        ap.print_help()
        return 2
    cfg = dict(CONFIG)
    if a.active_minutes:
        cfg["active_minutes"] = a.active_minutes
    paths = Paths.default()
    if not paths.projects.is_dir():
        print(f"awsupervise: no transcripts at {paths.projects}", file=sys.stderr)
        return 2
    while True:
        if a.dry_run:  # writes nothing, so it takes no lock (the lock file is a write)
            result = scan_once(paths, cfg, dry=True, include_finished=a.include_finished)
        else:
            with _Lock(paths.home / "awsupervise.lock") as got:
                result = scan_once(paths, cfg) if got else None
            if result is None:
                print("awsupervise: another instance holds the lock; skipping this pass")
        if result is not None:
            print(json.dumps(as_json(result), indent=1, default=str) if a.json
                  else render(result, verbose=a.verbose or a.dry_run))
            sys.stdout.flush()
        if not a.watch:
            return 0
        try:
            time.sleep(max(15.0, a.interval))
        except KeyboardInterrupt:
            return 0


# --------------------------------------------------------------------------- self-test


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class _Tx:
    """Builds one synthetic transcript: each call() is one turn with one tool call."""

    def __init__(self, sid: str, agent_id: str, model: str, t0: float) -> None:
        self.sid, self.agent_id, self.model, self.t = sid, agent_id, model, t0
        self.rows: list = []
        self.n = 0

    def _base(self, kind: str) -> dict:
        self.t += 20
        row = {"type": kind, "sessionId": self.sid, "cwd": "C:/work/repo",
               "timestamp": _iso(self.t),
               "uuid": f"u{len(self.rows)}"}
        if self.agent_id:
            row.update(isSidechain=True, agentId=self.agent_id)
        return row

    def call(self, name: str, inp: dict, result: str = "ok", is_error: bool = False,
             tur: Any = None) -> "_Tx":
        self.n += 1
        cid = f"call_{self.agent_id or 'main'}_{self.n}"
        a = self._base("assistant")
        a["requestedModel"] = self.model
        a["message"] = {"id": f"m{self.n}-{self.agent_id}", "role": "assistant",
                        "model": self.model, "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": cid, "name": name, "input": inp}]}
        u = self._base("user")
        u["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": result,
             **({"is_error": True} if is_error else {})}]}
        if tur is None:
            tur = "Error: " + result if is_error else {"stdout": result}
        u["toolUseResult"] = tur
        self.rows += [a, u]
        return self

    def say(self, text: str, stop: str = "end_turn") -> "_Tx":
        self.n += 1
        a = self._base("assistant")
        a["message"] = {"id": f"m{self.n}-{self.agent_id}", "role": "assistant",
                        "model": self.model, "stop_reason": stop,
                        "content": [{"type": "text", "text": text}]}
        self.rows.append(a)
        return self

    def api_error(self, kind: str = "server_error", text: str = "Request timed out") -> "_Tx":
        self.n += 1
        a = self._base("assistant")
        a.update(isApiErrorMessage=True, error=kind)
        a["message"] = {"id": f"e{self.n}-{self.agent_id}", "role": "assistant",
                        "model": "<synthetic>", "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": text}]}
        self.rows.append(a)
        return self

    def write(self, path: Path, meta: Optional[dict] = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in self.rows) + "\n", encoding="utf-8")
        if meta is not None:
            path.with_name(path.stem + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return path


def _self_test(quiet: bool = False) -> int:
    """Synthetic transcripts in a temp tree: every detector, its negative, the ladder,
    dedupe, rate limits, the recommendation, dry-run purity and the mailbox format.
    The relay and card runners are fakes and subprocess is fenced off, so nothing real
    is ever notified."""
    fails: list = []
    checks = [0]

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[0] += 1
        if not quiet:
            extra = f"  [{detail}]" if detail and not ok else ""
            print(("  ok   " if ok else "  FAIL ") + name + extra)
        if not ok:
            fails.append(name)

    real_run, real_popen = subprocess.run, subprocess.Popen

    def fenced(*a, **k):
        raise AssertionError("self-test tried to start a real process")

    subprocess.run = fenced  # type: ignore[assignment]
    subprocess.Popen = fenced  # type: ignore[assignment,misc]
    tmp = Path(tempfile.mkdtemp(prefix="awsupervise-selftest-"))
    try:
        now = time.time()
        paths = Paths(projects=tmp / "projects", temp_claude=tmp / "tempclaude",
                      sessions=tmp / "sessions", labels=tmp / "labels", trace=tmp / "trace",
                      steer=tmp / "steer", home=tmp / "home", decisions=tmp / "decisions")
        for p in (paths.sessions, paths.trace, paths.decisions, paths.labels):
            p.mkdir(parents=True)
        cfg = copy.deepcopy(CONFIG)
        cfg.update(upstream_recent_minutes=180, max_cards_per_hour=1)
        s1, s2 = "11111111-aaaa-4bbb-8ccc-000000000001", "22222222-aaaa-4bbb-8ccc-000000000002"
        proj = paths.projects / "C--work-repo"
        ds, opus = "deepseek-v4-flash", "claude-opus-5-5"
        t0 = now - 25 * 60

        def meta(description: str, **extra: Any) -> dict:
            return {"agentType": "general-purpose", "description": description, **extra}

        # live sessions: this process stands in for both Claude Code processes
        me = _proc_start(os.getpid())
        claim = int(me * 1e7 + 116444736000000000) if me and me > 0 else None
        for sid, name in ((s1, "Darkmatter body shapes"), (s2, "Router lane")):
            (paths.sessions / f"{sid[:4]}.json").write_text(json.dumps(
                {"pid": os.getpid(), "sessionId": sid, "cwd": "C:/work/repo", "name": name,
                 "status": "busy", **({"procStart": str(claim)} if claim else {})}),
                encoding="utf-8")
        (paths.sessions / "dead.json").write_text(json.dumps(
            {"pid": 4194300, "sessionId": "33333333-dead", "procStart": "1"}), encoding="utf-8")

        # main transcripts (healthy: a report at the end of each turn)
        for sid in (s1, s2):
            _Tx(sid, "", opus, t0).call("Read", {"file_path": "C:/work/repo/README.md"}).say(
                "Dispatched the lanes.").write(proj / f"{sid}.jsonl")
        sub1, sub2 = proj / s1 / "subagents", proj / s2 / "subagents"
        # a. the same Read five times, no progress
        tx = _Tx(s1, "aloop", ds, t0)
        for _ in range(5):
            tx.call("Read", {"file_path": "C:/work/repo/skull.py", "description": "look again"})
        tx.write(sub1 / "agent-aloop.jsonl", meta("build:skull-shape"))
        # b. the same error three times (different descriptions, same failure)
        tx = _Tx(s1, "aerr", opus, t0)
        for i in range(3):
            tx.call("Bash", {"command": f"python build.py --step {i}", "description": f"try {i}"},
                    result=f"Exit code 1\nModuleNotFoundError: No module named 'bpy' (attempt {i})",
                    is_error=True)
        tx.write(sub1 / "agent-aerr.jsonl", meta("build:rig"))
        # c. 26 turns of distinct reads on DeepSeek: no progress, recommend Opus 5.5
        tx = _Tx(s1, "adrift", ds, t0 - 600)
        for i in range(26):
            tx.call("Read", {"file_path": f"C:/work/repo/src/m{i}.py"})
        tx.write(sub1 / "agent-adrift.jsonl", meta("build:skeleton-fit"))
        # c, negative: a FRESH agent's first 30 reads are exploration (the time floor)
        tx = _Tx(s1, "afresh", opus, now - 5 * 60)
        for i in range(30):
            tx.call("Read", {"file_path": f"C:/work/repo/new/f{i}.py"})
        fresh = tx.write(sub1 / "agent-afresh.jsonl", meta("build:fresh"))
        # d. A -> B -> A on one file
        tx = _Tx(s1, "aflip", opus, t0)
        bones = "C:/work/repo/bones.py"
        tx.call("Edit", {"file_path": bones, "old_string": "k = 1", "new_string": "k = 2"})
        tx.call("Edit", {"file_path": bones, "old_string": "k = 2", "new_string": "k = 1"})
        tx.write(sub1 / "agent-aflip.jsonl", meta("build:bones"))
        # healthy and still working: progress of every kind, then more work (on DeepSeek,
        # with good turns AFTER the router streak began, so the router does not blame it)
        tx = _Tx(s1, "agood", ds, now - 6 * 60)
        tx.call("Edit", {"file_path": "C:/work/repo/a.py", "old_string": "x", "new_string": "y"})
        tx.call("Bash", {"command": "python -m pytest tests/test_a.py -q"}, result="3 passed")
        tx.call("Bash", {"command": "git commit -m 'fix'"}, result="[develop abc] fix")
        tx.call("TodoWrite", {"todos": [{"content": "fix a", "status": "completed"}]},
                tur={"oldTodos": [{"content": "fix a", "status": "in_progress"}],
                     "newTodos": [{"content": "fix a", "status": "completed"}]})
        for i in range(3):
            tx.call("Read", {"file_path": f"C:/work/repo/b{i}.py"})
        tx.write(sub1 / "agent-agood.jsonl", meta("build:healthy"))
        # finished agents are not supervised, whatever their tail looks like
        tx = _Tx(s1, "adone", ds, t0)
        for _ in range(6):
            tx.call("Read", {"file_path": "C:/work/repo/x.py"})
        tx.write(sub1 / "agent-adone.jsonl", meta("done", finished=True))
        # e1. three API-error turns in its own transcript (DeepSeek)
        tx = _Tx(s2, "aapi", ds, t0)
        tx.call("Read", {"file_path": "C:/work/repo/y.py"})
        for _ in range(3):
            tx.api_error()
        tx.write(sub2 / "agent-aapi.jsonl", meta("build:cc5-look"))
        # e2. the router: a trailing failure streak on DeepSeek
        trace_rows = [{"ts": _iso(now - 40 * 60), "requested_model": ds,
                       "upstream_status": 200, "error": ""}]
        for i, (status, err) in enumerate(((401, ""), (503, ""), (401, "ConnectionResetError: x"),
                                           (0, "ConnectionRefusedError: y"))):
            trace_rows.append({"ts": _iso(now - (20 - i) * 60), "requested_model": ds,
                               "upstream_status": status, "error": err})
        trace_rows.append({"ts": _iso(now - 5 * 60), "requested_model": opus,
                           "upstream_status": 200, "error": ""})
        (paths.trace / f"router-{datetime.fromtimestamp(now):%Y-%m-%d}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in trace_rows) + "\n", encoding="utf-8")
        tx = _Tx(s2, "arouter", ds, now - 45 * 60)  # its last good turn predates the streak
        tx.call("Read", {"file_path": "C:/work/repo/z.py"})
        tx.write(sub2 / "agent-arouter.jsonl", meta("build:voxel"))
        tx = _Tx(s2, "arouterok", ds, now - 12 * 60)  # got through after the streak began
        tx.call("Read", {"file_path": "C:/work/repo/w.py"})
        tx.write(sub2 / "agent-arouterok.jsonl", meta("lucky"))
        # f. three hours in, still working, no report (also exercises the TEMP .output path)
        tx = _Tx(s2, "aslow", opus, now - 3 * 3600)
        tx.call("Read", {"file_path": "C:/work/repo/q.py"})
        tx.t = now - 120
        tx.call("Edit", {"file_path": "C:/work/repo/q.py", "old_string": "a", "new_string": "b"})
        out_file = tx.write(paths.temp_claude / "C--work-repo" / s2 / "tasks" / "aslow.output")
        # the same transcript under both trees must be ONE agent
        tx = _Tx(s2, "atwin", opus, t0)
        for _ in range(4):
            tx.call("Grep", {"pattern": "skull", "path": "C:/work/repo"})
        tx.write(sub2 / "agent-atwin.jsonl", meta("twin"))
        tx.write(paths.temp_claude / "C--work-repo" / s2 / "tasks" / "atwin.output")
        # an old-format main transcript with sidechain turns inside it
        tx = _Tx(s2, "aside", opus, t0)
        for _ in range(4):
            tx.call("Glob", {"pattern": "**/*.blend"})
        main2 = proj / f"{s2}.jsonl"
        with open(main2, "a", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r) for r in tx.rows) + "\n")

        # ---- liveness
        sessions = load_sessions(paths)
        check("live session verified by pid + start time",
              sessions[s1].alive and sessions[s2].alive)
        check("a dead pid is not live", not sessions["33333333-dead"].alive)

        # ---- detection
        r1 = scan_once(paths, cfg, now=now, dry=True)
        got: dict = {}
        for f in r1["findings"]:
            got.setdefault(f.agent.agent_id or f.agent.key, set()).add(f.detector)
        keys = [a.agent_id for a in r1["scan"].agents]
        check("a: identical Read x5 -> repeat_call",
              "repeat_call" in got.get("aloop", set()), str(got.get("aloop")))
        steer_a = next((f.steer for f in r1["findings"] if f.agent.agent_id == "aloop"
                        and f.detector == "repeat_call"), "")
        check("a: the steer names the call and the count",
              "Read(C:/work/repo/skull.py)" in steer_a and "5 times" in steer_a, steer_a)
        check("b: same error x3 -> repeat_error",
              "repeat_error" in got.get("aerr", set()), str(got.get("aerr")))
        check("c: 26 turns, no progress -> no_progress", "no_progress" in got.get("adrift", set()))
        check("c: DeepSeek + no_progress recommends Opus 5.5",
              any(f.upgrade for f in r1["findings"] if f.agent.agent_id == "adrift"))
        check("c: 30 turns in a fresh agent's first minutes is not a loop",
              "afresh" in keys and "no_progress" not in got.get("afresh", set()))
        fresh.unlink()  # out of the ladder runs below
        fresh.with_name(fresh.stem + ".meta.json").unlink()
        check("d: A->B->A -> oscillation",
              "oscillation" in got.get("aflip", set()), str(got.get("aflip")))
        check("e: API-error streak in its own transcript -> upstream_streak",
              "upstream_streak" in got.get("aapi", set()), str(got.get("aapi")))
        check("e: router streak on its model -> upstream_streak",
              "upstream_streak" in got.get("arouter", set()), str(got.get("arouter")))
        check("e: no router blame for an agent that got through after the streak began",
              "upstream_streak" not in got.get("arouterok", set()))
        check("e: no router blame on another model",
              not got.get("aerr", set()) & {"upstream_streak"})
        check("f: 3 h with no report -> wall_budget (read from the TEMP .output)",
              "wall_budget" in got.get("aslow", set()), str(got.get("aslow")))
        check("healthy agent (edit, pytest, commit, todo) trips nothing", not got.get("agood"))
        check("finished agent is not scanned", "adone" not in keys)
        check("a transcript under projects/ and TEMP is one agent", keys.count("atwin") == 1)
        check("sidechain turns in a main transcript are their own agent",
              "repeat_call" in got.get("aside", set()))
        check("main sessions with turn reports trip nothing",
              not any(f.agent.kind == "main" for f in r1["findings"]))
        check("dry run writes nothing", not paths.steer.exists() and not paths.home.exists())
        check("dry run plans an L1 steer per session",
              sorted((r["level"], r["session"]) for r in r1["actions"]) == [(1, s1), (1, s2)],
              str([(r["level"], r["session"]) for r in r1["actions"]]))

        # ---- the ladder, through time
        calls: list = []

        def runner(argv: list, timeout: float) -> tuple:
            calls.append(list(argv))
            if "decide" in argv:
                return 0, json.dumps({"id": f"d-t{len(calls)}", "status": "open"}), ""
            return 0, "sent", ""

        r = scan_once(paths, cfg, now=now, runner=runner)
        box1 = paths.steer / s1
        files = sorted(box1.glob("*-supervise.md")) if box1.is_dir() else []
        check("L1 writes one steer file per session",
              len(files) == 1 and len(list((paths.steer / s2).glob("*-supervise.md"))) == 1)
        text = files[0].read_text(encoding="utf-8") if files else ""
        head = re.compile(r'^\s*<!--\s*aither-steer\s+v1((?:\s+[a-z_]+="[^"\r\n]*")*)\s*-->\s*$')
        first = text.split("\n", 1)[0]
        check("steer header parses as the drain hook's v1 header, authority peer",
              bool(head.match(first)) and 'authority="peer"' in first
              and 'from="awsupervise"' in first)
        check("steer names each stuck agent and the specific instruction",
              "aloop" in text and "You repeated Read(C:/work/repo/skull.py) 5 times" in text
              and "adrift" in text and 'model "fable"' in text)
        check("L1 spent no relay or card", calls == [])
        # The real consumer, when this file sits in the repo: the drain hook must deliver
        # the note as a PEER message from awsupervise (never with owner authority).
        try:
            here = Path(__file__).resolve()
            drain_hook = Path(os.environ.get("AWSUPERVISE_DRAIN_HOOK")
                              or here.parents[3] / ".claude" / "hooks" / "steer-mailbox-drain.py")
        except IndexError:
            drain_hook = Path("")
        if drain_hook.is_file():
            import importlib.util

            spec = importlib.util.spec_from_file_location("_awsupervise_drain", drain_hook)
            hook = importlib.util.module_from_spec(spec)
            sys.modules["_awsupervise_drain"] = hook
            spec.loader.exec_module(hook)  # type: ignore[union-attr]
            saved = os.environ.get("AITHER_STEER_DIR")
            os.environ["AITHER_STEER_DIR"] = str(paths.steer)
            try:
                got_steers = hook.drain(s2)
                ctx = hook.build_context(got_steers)
            finally:
                if saved is None:
                    os.environ.pop("AITHER_STEER_DIR", None)
                else:
                    os.environ["AITHER_STEER_DIR"] = saved
            check("the real drain hook delivers it as a PEER note from awsupervise",
                  len(got_steers) == 1 and got_steers[0].authority == "peer"
                  and got_steers[0].sender == "awsupervise"
                  and "[via steer mailbox from awsupervise]" in ctx and "aslow" in ctx)
        elif not quiet:
            print(f"  skip drain-hook delivery (not found: {drain_hook})")
        log = paths.home / f"events-{_today()}.jsonl"
        rows = ([json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
                if log.is_file() else [])
        check("every action and detection is logged",
              sum(1 for x in rows if x["action"] == "steer") == 2
              and sum(1 for x in rows if x["action"] == "detect") >= 8)

        r = scan_once(paths, cfg, now=now + 60, runner=runner)
        check("dedupe: no second action inside the climb window",
              r["actions"] == [] and calls == [])

        r = scan_once(paths, cfg, now=now + 11 * 60, runner=runner)
        relays = [c for c in calls if "send" in c]
        check("L2 posts to #agents as a finding, addressed to each session",
              len(relays) == 2
              and all(c[c.index("send") + 1] == "#agents" and "finding" in c
                      and c[c.index("--to") + 1] in (s1[:8], s2[:8]) for c in relays),
              str(relays)[:300])

        calls.clear()
        r = scan_once(paths, cfg, now=now + 22 * 60, runner=runner)
        cards = [c for c in calls if "decide" in c]
        check("L3 raises one card; the second is held by the hourly cap",
              len(cards) == 1
              and any(x.get("result") == "suppressed: rate limit" for x in r["actions"]),
              str([x.get("result") for x in r["actions"]]))
        card = cards[0] if cards else []
        opts = [card[i + 1].split("|")[0] for i, v in enumerate(card) if v == "--option"]
        check("card offers stop / fable / continue, defaults to continue, routes to the session",
              opts == ["stop", "fable", "continue"]
              and card[card.index("--default") + 1] == "continue"
              and card[card.index("--session") + 1] in (s1, s2), str(opts))
        sid_card = card[card.index("--session") + 1] if card else ""
        check("card for the DeepSeek no-progress session recommends fable",
              sid_card != s1 or card[card.index("--recommend") + 1] == "fable")

        calls.clear()
        r = scan_once(paths, cfg, now=now + 23 * 60, runner=runner)
        check("an open card is not raised twice",
              not any("decide" in c and sid_card in c for c in calls))

        card_id = load_state(paths)["cards"].get(sid_card, {}).get("id", "")
        (paths.decisions / f"{card_id}.json").write_text(
            json.dumps({"id": card_id, "status": "answered", "answer": "continue"}),
            encoding="utf-8")
        calls.clear()
        r = scan_once(paths, cfg, now=now + 85 * 60, runner=runner)
        closed = [e for e in r["events"] if e["action"] == "card-closed"]
        check("an answered card is read back and holds its incidents",
              closed and closed[0]["answer"] == "continue"
              and not any(sid_card in c for c in calls), str(closed)[:200])

        # the looping agent makes progress: its incident resolves
        tx = _Tx(s1, "aloop", ds, now - 60)
        tx.call("Edit", {"file_path": "C:/work/repo/skull.py", "old_string": "r=1",
                         "new_string": "r=2"})
        with open(sub1 / "agent-aloop.jsonl", "a", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(x) for x in tx.rows) + "\n")
        r = scan_once(paths, cfg, now=now + 86 * 60, runner=runner)
        check("progress resolves the incident",
              any(e["action"] == "resolved" and e["incident"].endswith("aloop|repeat_call")
                  for e in r["events"]))

        try:
            write_steer(paths.steer, "../escape", ["x"], "e")
            check("a path-shaped session id is refused", False)
        except ValueError:
            check("a path-shaped session id is refused", True)
        check("secrets are redacted from emitted text",
              "sk-" not in redact("key sk-abcdefghijklmnopqrstu")
              and "[redacted]" in redact("token=abcdef1234"))
        check("render() reports without crashing", "finding(s)" in render(r1, verbose=True))
        check("no real process was started", True)
        _ = out_file
    except Exception as exc:  # a crash in the harness is could-not-judge, not a pass
        import traceback

        traceback.print_exc()
        fails.append(f"self-test crashed: {type(exc).__name__}: {exc}")
    finally:
        subprocess.run, subprocess.Popen = real_run, real_popen  # type: ignore[assignment,misc]
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print(f"awsupervise self-test: {len(fails)} of {checks[0]} FAILED")
        for f in fails:
            print("  FAIL", f)
        return 1
    print(f"awsupervise self-test: {checks[0]} checks, all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
