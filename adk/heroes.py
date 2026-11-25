"""The hero deck: agent sessions as selectable units with a status, a ticker and a mission.

AgentCraft (https://www.getagentcraft.com/docs, proprietary, docs read only, no code
copied) presents every agent session as a "hero" you command RTS-style: a status
(idle / working / awaiting / error), a one-line operation ticker, a context-usage bar,
selection hotkeys, control groups, missions with names and metrics, and achievements.
The owner's request was 1:1 parity "in our platform with awdk". This module is that
model, written from the documented behaviour on top of what awdk already produces:
the daemon's per-session ``events.jsonl`` (``HarnessEvent.to_dict`` rows, the same rows
``adk.harnesses.awsupervise_sessions`` folds for the watchdog).

Pure and stdlib-only, Python 3.10-compatible, no I/O except the two small JSON stores
(``MissionLog`` and ``ControlGroups`` when given a path). A UI (awdesk, awsh, the TUI
cockpit, the Veil) renders what this returns; none of them re-derive state.

    hero = fold_events("s-1", events, config={"harness": "claude", "model": "opus"})
    roster = Roster([hero, ...])
    roster.select_slot(1)                    # the 1-8 hotkeys, priority ordered
    roster.cycle("idle_or_awaiting", cur)    # Space; awaiting heroes come first
    groups.assign(3, ["s-1", "s-2"]); groups.recall(3, roster)

"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- vocabulary

IDLE, WORKING, AWAITING, ERROR, EXITED = "idle", "working", "awaiting", "error", "exited"
STATES = (IDLE, WORKING, AWAITING, ERROR, EXITED)

#: Selection priority: who needs the commander first. Awaiting heroes block on a human.
_PRIORITY = {AWAITING: 0, ERROR: 1, WORKING: 2, IDLE: 3, EXITED: 4}

#: Event kinds a harness may emit when it needs a human; resolved by the matching reply.
ASK_KINDS = frozenset({"permission.request", "approval.request", "input.request",
                       "question.request", "plan.request"})
ANSWER_KINDS = frozenset({"permission.response", "approval.response", "input.response",
                          "question.response", "plan.response"})
#: Tool calls that mean "I am asking the human" in the Claude Code spelling.
ASK_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

EDIT_TOOLS = ("edit", "multiedit", "write", "notebookedit", "apply_patch", "file_change")
READ_TOOLS = ("read", "glob", "grep", "ls", "view")
SHELL_TOOLS = ("bash", "shell", "powershell", "run_command", "command_execution",
               "terminal", "exec")
PATH_KEYS = ("file_path", "path", "notebook_path", "filename")

#: Context windows by model-name fragment. A UI may pass its own; this is the fallback.
DEFAULT_CONTEXT_WINDOW = 200_000
CONTEXT_WINDOWS = {"1m": 1_000_000, "opus": 200_000, "sonnet": 200_000, "haiku": 200_000}

BAND_GREEN, BAND_YELLOW, BAND_RED = "green", "yellow", "red"


def context_band(used: int, window: int) -> str:
    """green under 70 %, yellow 70-90 %, red over 90 % (the documented thresholds)."""
    if window <= 0:
        return BAND_GREEN
    ratio = used / window
    if ratio > 0.90:
        return BAND_RED
    if ratio >= 0.70:
        return BAND_YELLOW
    return BAND_GREEN


def _context_window(model: str) -> int:
    low = (model or "").lower()
    for frag, size in CONTEXT_WINDOWS.items():
        if frag in low and frag == "1m":
            return size
    return DEFAULT_CONTEXT_WINDOW


# --------------------------------------------------------------------------- the record


@dataclass
class Mission:
    """One prompt-to-completion run: the unit the quest log lists."""

    hero_id: str
    number: int
    prompt: str = ""
    name: str = ""            # thematic quest name, deterministic
    short: str = ""           # 3-word summary
    long: str = ""            # 1-sentence summary
    started: float = 0.0
    ended: float = 0.0
    status: str = "active"    # active | complete | failed
    tokens: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    subagents: int = 0
    files: list = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, (self.ended or self.started) - self.started)

    def to_dict(self) -> dict:
        row = dict(self.__dict__)
        row["duration"] = round(self.duration, 3)
        return row


@dataclass
class Hero:
    id: str
    name: str = ""
    harness: str = ""
    model: str = ""
    cwd: str = ""
    state: str = IDLE
    awaiting: str = ""            # permission | question | plan | "" when not awaiting
    ticker: str = ""
    context_used: int = 0
    context_window: int = DEFAULT_CONTEXT_WINDOW
    started: float = 0.0
    last_ts: float = 0.0
    subagents: int = 0
    mission: Optional[Mission] = None         # the running mission, if any
    last_mission: Optional[Mission] = None    # shown instead of "Idle" on reconnect
    missions: list = field(default_factory=list)
    touched: dict = field(default_factory=dict)   # path -> {"ts": float, "mode": read|write}

    @property
    def band(self) -> str:
        return context_band(self.context_used, self.context_window)

    @property
    def activity(self) -> str:
        if self.mission is not None:
            return self.mission.name
        if self.last_mission is not None:
            return self.last_mission.name
        return "Idle"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name or self.id, "harness": self.harness,
            "model": self.model, "cwd": self.cwd, "state": self.state,
            "awaiting": self.awaiting, "ticker": self.ticker, "activity": self.activity,
            "context": {"used": self.context_used, "window": self.context_window,
                        "band": self.band},
            "subagents": self.subagents, "started": self.started, "last_ts": self.last_ts,
            "mission": self.mission.to_dict() if self.mission else None,
            "last_mission": self.last_mission.to_dict() if self.last_mission else None,
        }


# --------------------------------------------------------------------------- naming

_ADJ = ("Gilded", "Silent", "Ember", "Hollow", "Rising", "Iron", "Verdant", "Crimson",
        "Wandering", "Sunken", "Radiant", "Frost", "Veiled", "Stormbound", "Ashen", "Lunar")
_NOUN = ("Refactor", "Expedition", "Reckoning", "Ascent", "Forge", "Crossing", "Vigil",
         "Harvest", "Siege", "Pilgrimage", "Gambit", "Survey", "Descent", "Rite",
         "Campaign", "Binding")


def quest_name(prompt: str, hero_id: str = "", number: int = 0) -> str:
    """A stable thematic name for a mission (same prompt, same name)."""
    digest = hashlib.sha256(f"{hero_id}|{number}|{prompt}".encode("utf-8")).digest()
    return f"{_ADJ[digest[0] % len(_ADJ)]} {_NOUN[digest[1] % len(_NOUN)]}"


def short_summary(prompt: str) -> str:
    """Deterministic three-word fallback; a caller may inject a model-written one."""
    words = [w for w in "".join(c if c.isalnum() or c in "-_" else " "
                                for c in prompt).split() if len(w) > 1]
    return " ".join(words[:3]) or "untitled"


# --------------------------------------------------------------------------- folding


def _basename(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _tool_input(event: dict) -> dict:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    inp = data.get("input")
    return inp if isinstance(inp, dict) else {}


def _path_of(inp: dict) -> str:
    for key in PATH_KEYS:
        if inp.get(key):
            return str(inp[key])
    return ""


def ticker_for(tool: str, inp: dict) -> str:
    """The one-line operation ticker: "Reading App.tsx", "Editing x", "Running git status"."""
    low = (tool or "").lower()
    path = _basename(_path_of(inp))
    if any(m in low for m in EDIT_TOOLS):
        return f"Editing {path}".strip() if path else "Editing files"
    if low in ("read", "view"):
        return f"Reading {path}".strip() if path else "Reading files"
    if low in ("grep", "glob", "search", "ls"):
        pattern = str(inp.get("pattern") or inp.get("query") or inp.get("path") or "")
        return f"Searching for {pattern[:40]}".strip() if pattern else "Searching"
    if low in SHELL_TOOLS:
        cmd = str(inp.get("command") or inp.get("cmd") or "").strip().splitlines()
        return f"Running {cmd[0][:48]}" if cmd else "Running a command"
    if low in ("task", "agent"):
        return "Delegating to a subagent"
    return f"Using {tool}" if tool else ""


def _lines(text: Any) -> int:
    return len(str(text).splitlines()) if text else 0


def _edit_delta(tool: str, inp: dict) -> tuple:
    low = (tool or "").lower()
    if "write" in low and "content" in inp:
        return _lines(inp.get("content")), 0
    if "edit" in low or "patch" in low:
        return _lines(inp.get("new_string")), _lines(inp.get("old_string"))
    return 0, 0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_tokens(event: dict) -> int:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return sum(_as_int(usage.get(key)) for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens"))


def _context_tokens(event: dict) -> int:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for key in ("context_tokens", "context_used"):
        if _as_int(data.get(key)):
            return _as_int(data.get(key))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return _as_int(usage.get("input_tokens")) + _as_int(usage.get("cache_read_input_tokens"))


def fold_events(session_id: str, events: Iterable[dict], config: Optional[dict] = None,
                summarizer: Optional[Callable[[str], tuple]] = None) -> Hero:
    """Fold one session's ``events.jsonl`` rows into a :class:`Hero`.

    ``summarizer(prompt) -> (short, long)`` lets a caller inject a model-written summary
    (routed through the caller's model gateway); the deterministic fallback is used
    when it is absent or raises.
    """
    conf = config if isinstance(config, dict) else {}
    hero = Hero(id=session_id, name=str(conf.get("name") or ""),
                harness=str(conf.get("harness") or ""), model=str(conf.get("model") or ""),
                cwd=str(conf.get("cwd") or ""))
    open_asks: dict = {}          # id -> awaiting kind
    pending_calls: dict = {}      # tool_use_id -> (tool, ticker)
    exited = False
    exit_failed = False
    errored = False
    number = 0
    current: Optional[Mission] = None

    def close(mission: Mission, ts: float, failed: bool) -> None:
        mission.ended = ts or mission.ended
        mission.status = "failed" if failed else "complete"
        short, long_ = short_summary(mission.prompt), (
            f"{mission.name}: {len(mission.files)} file(s), "
            f"+{mission.lines_added}/-{mission.lines_removed} lines, {mission.tokens} tokens.")
        if summarizer is not None:
            try:
                got = summarizer(mission.prompt)
                if got and got[0]:
                    short, long_ = str(got[0]), str(got[1] or long_)
            except Exception as exc:  # a failed summary must never break the roster
                logger.warning("mission summarizer failed, using the fallback: %s", exc)
        mission.short, mission.long = short, long_
        hero.missions.append(mission)
        hero.last_mission = mission

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        try:
            ts = float(event.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        hero.last_ts = max(hero.last_ts, ts)
        hero.started = hero.started or ts
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        if kind in ("session.starting", "session.ready"):
            hero.cwd = hero.cwd or str(data.get("cwd") or "")
            hero.harness = hero.harness or str(data.get("harness") or "")
            reported = str(data.get("model") or "")
            hero.model = reported or hero.model
            hero.name = hero.name or str(data.get("name") or "")
        elif kind == "turn.started":
            if current is not None:                      # a missing completion: close it
                close(current, ts, failed=False)
            number += 1
            prompt = str(event.get("text") or data.get("prompt") or "")
            current = Mission(hero_id=session_id, number=number, prompt=prompt, started=ts,
                              name=quest_name(prompt, session_id, number))
        elif kind == "tool.call":
            tool = str(event.get("tool") or "")
            inp = _tool_input(event)
            call_id = str(event.get("tool_use_id") or "")
            tick = ticker_for(tool, inp)
            hero.ticker = tick or hero.ticker
            if call_id:
                pending_calls[call_id] = tool
            if tool in ASK_TOOLS:
                open_asks[call_id or f"ask{len(open_asks)}"] = (
                    "plan" if tool == "ExitPlanMode" else "question")
            path_hit = _path_of(inp)
            if path_hit:
                is_write = any(m in tool.lower() for m in EDIT_TOOLS)
                prior = hero.touched.get(path_hit, {})
                hero.touched[path_hit] = {
                    "ts": ts,
                    "mode": "write" if is_write or prior.get("mode") == "write" else "read"}
            if tool.lower() in ("task", "agent"):
                hero.subagents += 1
                if current is not None:
                    current.subagents += 1
            if current is not None and any(m in tool.lower() for m in EDIT_TOOLS):
                added, removed = _edit_delta(tool, inp)
                current.lines_added += added
                current.lines_removed += removed
                path = _path_of(inp)
                if path and path not in current.files:
                    current.files.append(path)
        elif kind == "tool.result":
            open_asks.pop(str(event.get("tool_use_id") or ""), None)
            pending_calls.pop(str(event.get("tool_use_id") or ""), None)
        elif kind in ASK_KINDS:
            open_asks[str(event.get("tool_use_id") or data.get("id") or kind)] = (
                "plan" if kind.startswith("plan") else
                "question" if kind.startswith(("question", "input")) else "permission")
        elif kind in ANSWER_KINDS:
            open_asks.pop(str(event.get("tool_use_id") or data.get("id") or
                              kind.replace("response", "request")), None)
        elif kind == "turn.completed":
            failed = bool(data.get("is_error"))
            errored = failed
            hero.context_used = _context_tokens(event) or hero.context_used
            if current is not None:
                current.tokens += _usage_tokens(event)
                close(current, ts, failed)
                current = None
            hero.ticker = "" if not failed else hero.ticker
        elif kind == "error":
            errored = True
        elif kind == "session.exited":
            exited = True
            code = data.get("exit_code")
            exit_failed = isinstance(code, int) and code != 0
        if kind == "turn.started":
            errored = False
        hero.context_used = _context_tokens(event) or hero.context_used

    hero.mission = current
    hero.context_window = _context_window(hero.model)
    if exited:
        hero.state = ERROR if exit_failed else EXITED
    elif open_asks:
        hero.state = AWAITING
        hero.awaiting = sorted(set(open_asks.values()))[0] if open_asks else ""
    elif errored:
        hero.state = ERROR
    elif current is not None:
        hero.state = WORKING
    else:
        hero.state = IDLE
    return hero


def load_session(directory: Path, summarizer: Optional[Callable] = None) -> Hero:
    """Read ``<dir>/events.jsonl`` (+ ``session.json``) the daemon wrote."""
    directory = Path(directory)
    events: list = []
    log = directory / "events.jsonl"
    if log.is_file():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                events.append(row)
    conf: dict = {}
    meta = directory / "session.json"
    if meta.is_file():
        try:
            loaded = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
            raw = loaded.get("config") if isinstance(loaded, dict) else None
            conf = raw if isinstance(raw, dict) else {}
        except ValueError:
            conf = {}
    return fold_events(directory.name, events, conf, summarizer)


# --------------------------------------------------------------------------- selection


class Roster:
    """The ordered set of heroes and the RTS selection verbs over it."""

    def __init__(self, heroes: Iterable[Hero]) -> None:
        self.heroes = sorted(heroes, key=lambda h: (_PRIORITY.get(h.state, 9), h.id))
        self._by_id = {h.id: h for h in self.heroes}

    def get(self, hero_id: str) -> Optional[Hero]:
        return self._by_id.get(hero_id)

    def select_slot(self, slot: int) -> Optional[Hero]:
        """Hotkeys 1-8: the Nth hero by priority (1-based)."""
        if 1 <= slot <= 8 and slot <= len(self.heroes):
            return self.heroes[slot - 1]
        return None

    def _pool(self, kind: str) -> list:
        if kind == "idle_or_awaiting":      # Space: awaiting first
            return ([h for h in self.heroes if h.state == AWAITING] +
                    [h for h in self.heroes if h.state == IDLE])
        if kind in (IDLE, AWAITING, WORKING, ERROR):
            return [h for h in self.heroes if h.state == kind]
        raise ValueError(f"unknown cycle kind {kind!r}")

    def cycle(self, kind: str, current: Optional[str] = None) -> Optional[Hero]:
        """Space / I / Shift+I / W / E: the next hero of that kind after ``current``."""
        pool = self._pool(kind)
        if not pool:
            return None
        ids = [h.id for h in pool]
        if current in ids:
            return pool[(ids.index(current) + 1) % len(pool)]
        return pool[0]

    def first(self, kind: str) -> Optional[Hero]:
        """Ctrl+I / Ctrl+E / Ctrl+A: jump to the first hero of that kind."""
        pool = self._pool(kind)
        return pool[0] if pool else None

    def cycle_subagent(self, hero_id: str, current: int = -1) -> int:
        """Tab: the next subagent index of a hero (-1 when it has none)."""
        hero = self.get(hero_id)
        if hero is None or hero.subagents <= 0:
            return -1
        return (current + 1) % hero.subagents


class ControlGroups:
    """Ctrl+N assign, Shift+Ctrl+N add, Alt+N recall. Optionally persisted as JSON."""

    SLOTS = range(1, 10)

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.groups: dict = {n: [] for n in self.SLOTS}
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for key, ids in (raw.get("groups") or {}).items():
                    if int(key) in self.groups and isinstance(ids, list):
                        self.groups[int(key)] = [str(i) for i in ids]
            except (ValueError, OSError, AttributeError):
                self.groups = {n: [] for n in self.SLOTS}

    def _check(self, slot: int) -> None:
        if slot not in self.groups:
            raise ValueError(f"control group must be 1-9, got {slot}")

    def assign(self, slot: int, ids: Iterable[str]) -> list:
        self._check(slot)
        self.groups[slot] = list(dict.fromkeys(ids))
        self._save()
        return self.groups[slot]

    def add(self, slot: int, ids: Iterable[str]) -> list:
        self._check(slot)
        self.groups[slot] = list(dict.fromkeys([*self.groups[slot], *ids]))
        self._save()
        return self.groups[slot]

    def recall(self, slot: int, roster: Optional[Roster] = None) -> list:
        """The group's members; with a roster, only heroes that still exist."""
        self._check(slot)
        ids = self.groups[slot]
        if roster is None:
            return list(ids)
        return [i for i in ids if roster.get(i) is not None]

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"groups": {str(k): v for k, v in self.groups.items()}}),
                       encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- missions


class MissionLog:
    """Persisted mission history: 200 completed globally, 50 per hero, newest kept."""

    GLOBAL_CAP = 200
    PER_HERO_CAP = 50

    def __init__(self, path: Optional[Path] = None, global_cap: int = GLOBAL_CAP,
                 per_hero_cap: int = PER_HERO_CAP) -> None:
        self.path = Path(path) if path else None
        self.global_cap = global_cap
        self.per_hero_cap = per_hero_cap
        self.rows: list = []
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.rows = [r for r in loaded.get("missions", []) if isinstance(r, dict)]
            except (ValueError, OSError, AttributeError):
                self.rows = []

    def record(self, mission: Mission) -> None:
        key = (mission.hero_id, mission.number)
        self.rows = [r for r in self.rows if (r.get("hero_id"), r.get("number")) != key]
        if mission.status in ("complete", "failed"):
            self.rows.append(mission.to_dict())
        self.rows.sort(key=lambda r: r.get("ended") or 0.0)
        per: dict = {}
        kept: list = []
        for row in reversed(self.rows):                  # newest first
            hid = row.get("hero_id")
            per[hid] = per.get(hid, 0) + 1
            if per[hid] <= self.per_hero_cap:
                kept.append(row)
        kept = kept[: self.global_cap]
        self.rows = list(reversed(kept))
        self._save()

    def recent(self, n: int = 50, hero_id: Optional[str] = None) -> list:
        rows = [r for r in self.rows if hero_id in (None, r.get("hero_id"))]
        return list(reversed(rows[-n:]))

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"missions": self.rows}), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- keys + tutorial

#: The hotkey table every front end binds to and the ``?`` overlay renders. ``verb`` names
#: the :class:`Roster` / :class:`ControlGroups` call (or a UI panel) the key triggers.
HOTKEYS = (
    ("1-8", "Select hero by priority slot", "selection", "Roster.select_slot"),
    ("Space", "Cycle idle and awaiting heroes, awaiting first", "selection",
     "Roster.cycle:idle_or_awaiting"),
    ("I", "Cycle idle heroes", "selection", "Roster.cycle:idle"),
    ("Shift+I", "Cycle awaiting heroes", "selection", "Roster.cycle:awaiting"),
    ("W", "Cycle working heroes", "selection", "Roster.cycle:working"),
    ("E", "Cycle heroes in error", "selection", "Roster.cycle:error"),
    ("Ctrl+I", "Jump to the first idle hero", "selection", "Roster.first:idle"),
    ("Ctrl+E", "Jump to the first hero in error", "selection", "Roster.first:error"),
    ("Ctrl+A", "Jump to the first awaiting hero", "selection", "Roster.first:awaiting"),
    ("Ctrl+1-9", "Assign selection to a control group", "groups", "ControlGroups.assign"),
    ("Shift+Ctrl+1-9", "Add selection to a control group", "groups", "ControlGroups.add"),
    ("Alt+1-9", "Recall a control group", "groups", "ControlGroups.recall"),
    ("Enter", "Open or close the side panel", "panels", "panel:side"),
    ("Escape", "Close the panel or deselect", "panels", "panel:close"),
    ("Tab", "Cycle the selected hero's subagents", "selection", "Roster.cycle_subagent"),
    ("`", "War table: file activity across heroes", "panels", "file_activity"),
    ("M", "Mission control", "panels", "MissionLog.recent"),
    ("T", "Activity timeline", "panels", "panel:timeline"),
    ("Ctrl+F", "Search across every hero", "panels", "panel:search"),
    ("V", "Fog of war", "map", "fog_of_war"),
    ("H", "Territory heatmap (no hero selected)", "map", "territory_heatmap"),
    ("F9", "Quest log", "panels", "MissionLog.recent"),
    ("F12", "Message log", "panels", "panel:messages"),
    ("B", "Notice board", "panels", "panel:notices"),
    ("Y", "Approve the pending request", "decisions", "answer:approve"),
    ("N", "Deny the pending request", "decisions", "answer:deny"),
    ("?", "Shortcuts overlay", "help", "panel:shortcuts"),
    ("F1", "Field manual", "help", "panel:manual"),
)

#: The first-launch walkthrough. ``waits_for`` is the action the person must perform before
#: the step advances; None means a "Next" button.
TUTORIAL = (
    {"id": "welcome", "title": "Welcome", "waits_for": None,
     "text": "Every agent session is a hero you can command from here."},
    {"id": "commands", "title": "Commands", "waits_for": "spawn",
     "text": "Open the command grid and spawn your first hero."},
    {"id": "first-prompt", "title": "Send a prompt", "waits_for": "prompt",
     "text": "Give your hero something to do."},
    {"id": "observe", "title": "Watch it work", "waits_for": None,
     "text": "The ticker shows what it is doing; a yellow glow means it needs you."},
    {"id": "status", "title": "The status widget", "waits_for": None,
     "text": "Idle, working, awaiting and error at a glance, plus context use."},
    {"id": "done", "title": "You are set", "waits_for": None,
     "text": "Press ? any time for every shortcut."},
)


def shortcut_help(group: Optional[str] = None) -> list:
    """Rows for the ``?`` overlay, optionally one group."""
    return [{"key": k, "does": d, "group": g, "verb": v}
            for k, d, g, v in HOTKEYS if group in (None, g)]


class Tutorial:
    """Walks :data:`TUTORIAL`; steps with ``waits_for`` advance only on that action."""

    def __init__(self, done: Iterable[str] = ()) -> None:
        self.index = len(TUTORIAL) if set(done) >= {t["id"] for t in TUTORIAL} else 0

    @property
    def step(self) -> Optional[dict]:
        return TUTORIAL[self.index] if self.index < len(TUTORIAL) else None

    @property
    def finished(self) -> bool:
        return self.index >= len(TUTORIAL)

    def advance(self, action: str = "next") -> bool:
        """Move on if ``action`` satisfies the current step ("next" only for free steps)."""
        step = self.step
        if step is None:
            return False
        need = step["waits_for"]
        if (need is None and action == "next") or (need is not None and action == need):
            self.index += 1
            return True
        return False


# --------------------------------------------------------------------------- map overlays


def file_activity(heroes: Iterable[Hero], since: float = 0.0) -> dict:
    """The war table: every file any hero touched, who, and whether anyone wrote it."""
    table: dict = {}
    for hero in heroes:
        for path, info in hero.touched.items():
            if info.get("ts", 0.0) < since:
                continue
            row = table.setdefault(path, {"heroes": [], "writers": [], "last_ts": 0.0})
            if hero.id not in row["heroes"]:
                row["heroes"].append(hero.id)
            if info.get("mode") == "write" and hero.id not in row["writers"]:
                row["writers"].append(hero.id)
            row["last_ts"] = max(row["last_ts"], info.get("ts", 0.0))
    return table


def _territory(path: str, depth: int) -> str:
    parts = [p for p in str(path).replace("\\", "/").split("/") if p and not p.endswith(":")]
    return "/".join(parts[:-1][:depth]) or "."


def territory_heatmap(heroes: Iterable[Hero], now: float, half_life_s: float = 600.0,
                      depth: int = 2) -> dict:
    """Where activity is concentrated: directory -> heat in [0, 1], recent writes hottest.

    Each touch adds 0.5 ** (age / half_life); writes count double; the result is scaled
    so the hottest directory is 1.0. Empty input gives an empty map (nothing to show).
    """
    heat: dict = {}
    for hero in heroes:
        for path, info in hero.touched.items():
            age = max(0.0, now - float(info.get("ts", 0.0)))
            weight = (2.0 if info.get("mode") == "write" else 1.0) * 0.5 ** (age / half_life_s)
            key = _territory(path, depth)
            heat[key] = heat.get(key, 0.0) + weight
    peak = max(heat.values(), default=0.0)
    return {k: round(v / peak, 4) for k, v in sorted(heat.items())} if peak > 0 else {}


def fog_of_war(heroes: Iterable[Hero], areas: Iterable[str], depth: int = 2) -> dict:
    """Fog toggle data: ``area -> visible``. An area is visible while a WORKING or
    AWAITING hero's cwd sits at or under it, or one of its touched files does."""
    active = [hr for hr in heroes if hr.state in (WORKING, AWAITING)]
    lit = set()
    for hero in active:
        if hero.cwd:
            lit.add(_territory(hero.cwd.rstrip("/\\") + "/_", depth))
        for path in hero.touched:
            lit.add(_territory(path, depth))
    return {a: any(a == l or a.startswith(l + "/") or l.startswith(a + "/")
                   for l in lit) for a in areas}   # noqa: E741 - area/lit, not I/l/O


def intent_conflicts(local: Iterable[Hero], remote_touches: Iterable[dict]) -> list:
    """Soft, non-blocking warnings: a remote hero touched a file a local hero is editing.

    ``remote_touches`` rows: ``{"hero": id, "owner": who, "path": p}``. Only files a local
    hero has WRITTEN while it is working are conflicts; reads never warn.
    """
    editing: dict = {}
    for hero in local:
        if hero.state != WORKING:
            continue
        for path, info in hero.touched.items():
            if info.get("mode") == "write":
                editing.setdefault(path, []).append(hero.id)
    out = []
    for touch in remote_touches:
        path = str(touch.get("path") or "")
        if path in editing:
            out.append({"path": path, "remote": touch.get("hero"), "owner": touch.get("owner"),
                        "local": list(editing[path]), "blocking": False})
    return out


# --------------------------------------------------------------------------- achievements

TIERS = {  # rarity -> (toast seconds, hero reaction)
    "common": (5, "wave"), "uncommon": (6, "wave"),
    "rare": (7, "jump"), "legendary": (10, "dance"),
}


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    description: str
    category: str      # discovery | mastery | secret
    tier: str          # common | uncommon | rare | legendary
    metric: str        # key in the stats dict
    at_least: float

    @property
    def secret(self) -> bool:
        return self.category == "secret"


def _a(id_: str, title: str, desc: str, cat: str, tier: str, metric: str, n: float
       ) -> Achievement:
    return Achievement(id_, title, desc, cat, tier, metric, n)


#: The metrics a stats dict carries; see :func:`stats_from`.
ACHIEVEMENTS = (
    _a("first-hero", "First Light", "Spawn your first hero.", "discovery", "common",
       "heroes_spawned", 1),
    _a("first-mission", "Marching Orders", "Complete a mission.", "discovery", "common",
       "missions_complete", 1),
    _a("first-approval", "The Seal", "Answer an approval request.", "discovery", "common",
       "approvals_answered", 1),
    _a("first-handoff", "Passing the Torch", "Hand work off to another hero.",
       "discovery", "uncommon", "handoffs", 1),
    _a("first-fork", "Two Roads", "Fork a hero with its context.", "discovery",
       "uncommon", "forks", 1),
    _a("first-group", "Rally Point", "Assign a control group.", "discovery", "common",
       "groups_assigned", 1),
    _a("first-loop", "Clockwork", "Start a recurring loop.", "discovery", "uncommon",
       "loops_started", 1),
    _a("first-campaign", "Grand Design", "Approve a campaign.", "discovery", "uncommon",
       "campaigns_approved", 1),
    _a("first-skill", "Scroll Reader", "Install a skill.", "discovery", "uncommon",
       "skills_installed", 1),
    _a("first-channel", "Long Reach", "Pair a chat channel.", "discovery", "uncommon",
       "channels_paired", 1),
    _a("first-remote", "Across the Wire", "Open a remote-access tunnel.", "discovery",
       "uncommon", "tunnels_opened", 1),
    _a("first-voice", "Speak Up", "Send a prompt by voice.", "discovery", "common",
       "voice_prompts", 1),
    _a("war-council", "War Council", "Five heroes working at once.", "mastery", "rare",
       "peak_concurrent", 5),
    _a("grand-army", "Grand Army", "Eight heroes working at once.", "mastery",
       "legendary", "peak_concurrent", 8),
    _a("speed-run", "Lightning Strike", "Finish a mission in under 30 seconds.",
       "mastery", "uncommon", "fastest_mission_s_inv", 1),
    _a("marathon", "Long Watch", "A mission that runs past 30 minutes.", "mastery",
       "rare", "longest_mission_s", 1800),
    _a("token-whale", "Deep Well", "More than 100,000 tokens in one mission.", "mastery",
       "rare", "max_mission_tokens", 100_000),
    _a("ten-missions", "Seasoned", "Complete 10 missions.", "mastery", "common",
       "missions_complete", 10),
    _a("hundred-missions", "Veteran", "Complete 100 missions.", "mastery", "rare",
       "missions_complete", 100),
    _a("lines-1k", "Mason", "Change 1,000 lines.", "mastery", "uncommon", "lines_changed",
       1000),
    _a("lines-10k", "Architect", "Change 10,000 lines.", "mastery", "rare",
       "lines_changed", 10_000),
    _a("subagent-swarm", "Swarm Caller", "Run 10 subagents in one mission.", "mastery",
       "uncommon", "max_mission_subagents", 10),
    _a("merge-5", "Quartermaster", "Merge five campaign tasks.", "mastery", "uncommon",
       "merges", 5),
    _a("night-owl", "Night Owl", "Complete a mission between 2 and 4 am.", "secret",
       "rare", "night_missions", 1),
    _a("clean-sweep", "Clean Sweep", "A day with no failed missions across 10.", "secret",
       "uncommon", "clean_streak", 10),
    _a("phoenix", "Phoenix", "Finish a mission right after a failed one.", "secret",
       "uncommon", "recoveries", 1),
    _a("polyglot", "Polyglot", "Use three different harnesses.", "secret", "rare",
       "harnesses_used", 3),
    _a("context-edge", "Edge of the Map", "Push a hero past 90 % context.", "secret",
       "uncommon", "context_red_seen", 1),
    _a("summit", "Summit", "Complete a campaign end to end.", "mastery", "legendary",
       "campaigns_complete", 1),
    _a("completionist", "Completionist", "Unlock 25 achievements.", "mastery", "legendary",
       "achievements_unlocked", 25),
)


def stats_from(heroes: Iterable[Hero], counters: Optional[dict] = None) -> dict:
    """Derive the metrics the catalogue reads from live heroes plus UI/event counters."""
    heroes = list(heroes)
    stats = dict(counters or {})
    missions = [m for h in heroes for m in h.missions]
    done = [m for m in missions if m.status == "complete"]
    stats["heroes_spawned"] = max(int(stats.get("heroes_spawned", 0)), len(heroes))
    stats["missions_complete"] = max(int(stats.get("missions_complete", 0)), len(done))
    stats["peak_concurrent"] = max(int(stats.get("peak_concurrent", 0)),
                                   sum(1 for h in heroes if h.state == WORKING))
    stats["longest_mission_s"] = max([m.duration for m in done] + [0])
    fast = [m.duration for m in done if m.duration > 0]
    stats["fastest_mission_s_inv"] = 1 if fast and min(fast) < 30 else 0
    stats["max_mission_tokens"] = max([m.tokens for m in missions] + [0])
    stats["max_mission_subagents"] = max([m.subagents for m in missions] + [0])
    stats["lines_changed"] = sum(m.lines_added + m.lines_removed for m in missions)
    stats["harnesses_used"] = len({h.harness for h in heroes if h.harness})
    stats["context_red_seen"] = int(any(h.band == BAND_RED for h in heroes))
    stats["night_missions"] = sum(
        1 for m in done if m.ended and 2 <= time.localtime(m.ended).tm_hour < 4)
    ordered = sorted(missions, key=lambda m: m.ended or 0.0)
    streak = best = 0
    recoveries = 0
    prev_failed = False
    for m in ordered:
        if m.status == "failed":
            streak = 0
            prev_failed = True
        elif m.status == "complete":
            streak += 1
            best = max(best, streak)
            recoveries += 1 if prev_failed else 0
            prev_failed = False
    stats["clean_streak"] = max(int(stats.get("clean_streak", 0)), best)
    stats["recoveries"] = max(int(stats.get("recoveries", 0)), recoveries)
    return stats


def evaluate(stats: dict, unlocked: Iterable[str] = ()) -> list:
    """Achievements newly earned by ``stats`` that are not already in ``unlocked``.

    ``achievements_unlocked`` is folded in as it grows so the 25-unlock achievement
    can fire in the same pass that crosses the threshold.
    """
    have = set(unlocked)
    earned: list = []
    for _ in range(2):                               # second pass settles the meta one
        view = dict(stats)
        view["achievements_unlocked"] = len(have) + len(earned)
        for ach in ACHIEVEMENTS:
            if ach.id in have or any(e.id == ach.id for e in earned):
                continue
            if float(view.get(ach.metric, 0) or 0) >= ach.at_least:
                earned.append(ach)
    return earned


def trophy_card(ach: Achievement, unlocked_count: int, total: Optional[int] = None) -> str:
    """The 600x315 shareable card as an SVG string (no raster dependency)."""
    total = total or len(ACHIEVEMENTS)
    colors = {"common": "#8a8f98", "uncommon": "#3fa66b", "rare": "#3b82f6",
              "legendary": "#e0b030"}
    accent = colors.get(ach.tier, "#8a8f98")

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="315" '
        'viewBox="0 0 600 315" role="img" aria-label="' + esc(ach.title) + '">'
        '<rect width="600" height="315" rx="18" fill="#12141a"/>'
        f'<rect x="6" y="6" width="588" height="303" rx="14" fill="none" stroke="{accent}" '
        'stroke-width="3"/>'
        f'<circle cx="92" cy="110" r="46" fill="{accent}" opacity="0.18"/>'
        f'<text x="92" y="126" text-anchor="middle" font-size="46" fill="{accent}" '
        'font-family="sans-serif">&#9733;</text>'
        f'<text x="170" y="96" font-size="30" font-weight="700" fill="#f4f4f6" '
        f'font-family="sans-serif">{esc(ach.title)}</text>'
        f'<text x="170" y="130" font-size="16" fill="#b6bac4" font-family="sans-serif">'
        f'{esc(ach.description)}</text>'
        f'<text x="170" y="164" font-size="14" font-weight="700" fill="{accent}" '
        f'font-family="sans-serif">{esc(ach.tier.upper())}</text>'
        '<text x="30" y="285" font-size="14" fill="#8a8f98" font-family="sans-serif">'
        f'{unlocked_count} of {total} unlocked</text>'
        '<text x="570" y="285" text-anchor="end" font-size="14" fill="#8a8f98" '
        'font-family="sans-serif">AitherOS</text></svg>')


def gallery(unlocked: Iterable[str], category: Optional[str] = None) -> list:
    """The Hall of Glory rows: locked secrets are masked, locked others stay readable."""
    have = set(unlocked)
    rows = []
    for ach in ACHIEVEMENTS:
        if category and ach.category != category:
            continue
        is_on = ach.id in have
        masked = ach.secret and not is_on
        rows.append({"id": ach.id, "unlocked": is_on, "tier": ach.tier,
                     "category": ach.category,
                     "title": "???" if masked else ach.title,
                     "description": "Keep exploring..." if masked else ach.description})
    return rows


def _main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="List heroes from the harness daemon's sessions.")
    ap.add_argument("--sessions-root", required=True, help="dir holding <session-id>/events.jsonl")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.sessions_root)
    if not root.is_dir():
        print(f"no such sessions root: {root}")
        return 2
    roster = Roster(load_session(d) for d in sorted(root.iterdir()) if d.is_dir())
    if a.json:
        print(json.dumps([h.to_dict() for h in roster.heroes], indent=2))
    else:
        for i, h in enumerate(roster.heroes, 1):
            print(f"{i:>2} {h.state:<8} {h.id:<24} {h.band:<6} {h.ticker or h.activity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
