"""awsupervise_sessions -- the external watchdog's view of adk daemon sessions.

WHY THIS EXISTS
---------------
``awsupervise`` was built for Claude Code: it reads that harness's transcripts. The
harness daemon (``adk harness`` / awsh / AitherShell) runs its OWN coding sessions, and
nothing judged their progress -- a session could sit subscribed to a pull-request thread,
touch nothing for an hour, and every surface would report a healthy "idle" session.

A daemon session is not a different kind of agent, only a different log format, so this
module does NOT add a second watchdog. It turns one session's ``events.jsonl`` into the
SAME records ``awsupervise`` already reasons over -- ``Call``, ``Result``, ``Turn`` and
progress signals -- and lets ``detect()``, the incident ladder and the actuator run
unchanged. One detector is genuinely new here: ``stalled_on_thread`` (a session holding a
thread subscription that has neither received nor touched anything for a long time). It
reads the daemon's own table READ-ONLY (``AITHER_THREAD_SUBS_DB``, then
``~/.aither/thread-subscriptions.db``): the router owns that file, and a dry run must
never create it.

THE EVENT VOCABULARY IT READS
-----------------------------
``events.jsonl`` is append-only, one JSON object per line, monotonic ``seq``:
``session.starting`` (harness, cwd), ``session.ready`` (model, cwd), ``turn.started``,
``text.delta``, ``tool.call`` (``tool``, ``tool_use_id``, ``data.input``),
``tool.result`` (``tool_use_id``, ``data.output``, ``data.is_error``),
``turn.completed``, ``error`` and ``session.exited``. Adapters name tools in the
harness's own vocabulary (``shell``/``apply_patch`` for codex, ``bash``/``edit`` for
opencode), so a small alias set extends the progress signals; the detectors themselves
are not modified.

ESCALATION
----------
Reuses ``awsupervise.plan`` / ``execute`` / ``Actuator``. Rung 1 is a PEER-authority note
in the session's own steering mailbox (``adk.decisions.store.write_steer``); nothing here
ever stops, interrupts or kills a session. Rung 2 is the relay, rung 3 the owner's card,
exactly as for a Claude Code box.

CLI
---
    python adk/harnesses/awsupervise.py --awdk-sessions --once
    python adk/harnesses/awsupervise.py --awdk-sessions --dry-run

Nothing is read unless ``--awdk-sessions`` is passed: the default scan stays byte-for-byte
what it was.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _load_by_path(name: str, path: Path):
    """Load ``path`` as its own module, ignoring the rest of ``sys.path``."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and pickling resolve a module by name
    spec.loader.exec_module(module)
    return module


def _load_sup():
    """``awsupervise`` -- the sibling FILE, not any same-named package on sys.path.

    Run as a script, ``sys.path[0]`` is this directory but ``site-packages`` is also
    searched, so a stale installed ``adk`` can supply an ``awsupervise`` without
    ``Agent.steps``. Importing the sibling by path keeps both halves of the pair at one
    version. A package import is used only when it IS this directory.
    """
    here = Path(__file__).resolve().parent
    try:
        from adk.harnesses import awsupervise as candidate
    except ImportError:
        candidate = None
    if candidate is not None:
        found = Path(getattr(candidate, "__file__", "") or "").resolve().parent
        if found == here:
            return candidate
    return _load_by_path("_awsupervise_sibling", here / "awsupervise.py")


sup = _load_sup()

# --------------------------------------------------------------------------- vocabulary

EV_STARTING = "session.starting"
EV_READY = "session.ready"
EV_TURN_STARTED = "turn.started"
EV_TEXT = "text.delta"
EV_TOOL_CALL = "tool.call"
EV_TOOL_RESULT = "tool.result"
EV_TURN_COMPLETED = "turn.completed"
EV_ERROR = "error"
EV_EXITED = "session.exited"

#: Event kinds that are a judged STEP. A ``text.delta`` is a token, not an action: a
#: session that did real work and then streamed its final report must not push its own
#: tool calls out of the stall window. Session lifecycle rows are not steps either.
STEP_KINDS = frozenset({EV_TURN_STARTED, EV_TOOL_CALL, EV_TOOL_RESULT, EV_TURN_COMPLETED,
                        EV_ERROR, EV_EXITED})

#: Tool-name markers for an edit-shaped call in a harness that does not use the Claude
#: Code spelling (codex ``apply_patch``, opencode ``edit``).
EDIT_MARKERS = ("edit", "write", "patch", "file_change")
#: Input keys that name the file an edit-shaped call changed.
EDIT_PATH_KEYS = ("file_path", "path", "notebook_path", "filename")
#: Tool names that run a command, in the harnesses' own words.
SHELL_NAMES = ("bash", "shell", "powershell", "run_command", "command_execution",
               "terminal", "exec")
#: Tools ``awsupervise.compute_progress`` already judges; the alias pass leaves them be so
#: a no-op edit (old_string == new_string) is not counted twice, or counted at all.
NATIVE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "Bash", "PowerShell",
                "TodoWrite", "TaskUpdate")


# --------------------------------------------------------------------------- paths


def sessions_root(root: Optional[Path] = None) -> Path:
    """Where the daemon keeps ``<session-id>/events.jsonl`` on this host."""
    if root:
        return Path(root)
    env = os.environ.get("AITHER_HARNESS_ROOT", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "harness-sessions"


# --------------------------------------------------------------------------- the reader


def _output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _input_of(event: dict) -> dict:
    data = event.get("data")
    inp = data.get("input") if isinstance(data, dict) else None
    return inp if isinstance(inp, dict) else {}


def _seq_of(event: dict, index: int) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else index


def _turn_number(event: dict) -> int:
    """The row's ``turn``, or 0 when an adapter wrote something non-numeric.

    One malformed row is a fact about that row, never a reason to abort a pass over every
    other session (the caller still sees the event, just not its turn label).
    """
    try:
        return int(event.get("turn"))
    except (TypeError, ValueError):
        return 0


def _step_of(kind: str, ts: float, event: dict) -> dict:
    """One judged step. A tool call carries what it was asked to touch."""
    if kind == EV_TOOL_CALL:
        return {"kind": kind, "ts": ts, "tool": str(event.get("tool") or "?"),
                "input": _input_of(event)}
    return {"kind": kind, "ts": ts}


def agent_from_events(session_id: str, events: list, path: Path, *,
                      last_write: float = 0.0, truncated: bool = False,
                      config: Optional[dict] = None) -> sup.Agent:
    """Fold one session's event log into the record ``awsupervise.detect`` consumes.

    ``config`` is the session's ``session.json`` ``config`` object, when it is readable:
    it is the only source of the harness/model for a session whose log was cut before its
    ``session.starting`` line.
    """
    conf = config if isinstance(config, dict) else {}
    agent = sup.Agent(key=f"{session_id}/main", session_id=session_id, agent_id="",
                      kind="main", path=Path(path), project="awdk",
                      last_write=float(last_write or 0.0), truncated=bool(truncated),
                      agent_type=str(conf.get("harness") or ""),
                      cwd=str(conf.get("cwd") or ""), model=str(conf.get("model") or ""))
    turns: dict = {}
    open_turn: Optional[sup.Turn] = None
    pending: list = []           # calls with no tool_use_id, matched by name/order
    last_ts = agent.last_write
    first_ts = 0.0
    for index, event in enumerate(events, 1):
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        seq = _seq_of(event, index)
        ts = sup._ts(event.get("ts")) or last_ts
        last_ts = ts
        first_ts = first_ts or ts
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind in STEP_KINDS:
            agent.steps.append(_step_of(kind, ts, event))

        if kind == EV_STARTING:
            agent.started = agent.started or ts
            agent.cwd = agent.cwd or str(data.get("cwd") or "")
            agent.agent_type = agent.agent_type or str(data.get("harness") or "")
            binding = data.get("model_binding")
            if isinstance(binding, dict) and binding.get("expected_model"):
                agent.model = agent.model or str(binding["expected_model"])
        elif kind == EV_READY:
            agent.cwd = agent.cwd or str(data.get("cwd") or "")
            reported = str(data.get("model") or "")
            if reported:  # what the harness says it resolved beats the configured name
                agent.model = reported
        elif kind == EV_TURN_STARTED:
            number = _turn_number(event)
            open_turn = sup.Turn(mid=f"t{number}:{seq}", ts=ts, seq=seq)
            turns[number] = open_turn
            agent.turns.append(open_turn)
        elif kind == EV_TEXT:
            if open_turn is not None and str(event.get("text") or "").strip():
                open_turn.text = True
        elif kind == EV_TOOL_CALL:
            call_id = str(event.get("tool_use_id") or "") or f"c{seq}"
            call = sup.Call(id=call_id, name=str(event.get("tool") or "?"),
                            input=_input_of(event), ts=ts, seq=seq)
            agent.calls.append(call)
            if not event.get("tool_use_id"):
                pending.append(call)
        elif kind == EV_TOOL_RESULT:
            call_id = str(event.get("tool_use_id") or "")
            if not call_id and pending:
                # an adapter that pairs by position, not by id: bind to an unbound call
                tool = str(event.get("tool") or "")
                match = next((c for c in pending if tool and c.name == tool), pending[0])
                pending.remove(match)
                call_id = match.id
            result = sup.Result(call_id=call_id, is_error=bool(data.get("is_error")),
                                text=_output_text(data.get("output")), tur=None,
                                ts=ts, seq=seq)
            agent.results[result.call_id] = result
        elif kind == EV_TURN_COMPLETED:
            turn = turns.get(_turn_number(event)) or open_turn
            if turn is not None and not data.get("is_error"):
                turn.stop = "end_turn"
        elif kind == EV_ERROR:
            # An error event is the harness's own verdict on a turn: judge it exactly like
            # a Claude Code API-error turn, so a streak of them still reads as upstream.
            agent.turns.append(sup.Turn(mid=f"e{seq}", ts=ts, seq=seq, api_error="api_error",
                                        error_text=str(event.get("text") or "")))
        elif kind == EV_EXITED:
            code = data.get("exit_code")
            agent.finished = (f"failed (exit {code})" if isinstance(code, int) and code
                              else "exited")
    # The first event of the log is the session's start when a cut tail lost its
    # ``session.starting``; never the LAST event, which would reset the clock every pass.
    agent.started = agent.started or first_ts or agent.last_write
    sup.compute_progress(agent)
    _alias_progress(agent)
    return agent


def _alias_progress(agent: sup.Agent) -> None:
    """Progress signals for a tool vocabulary ``compute_progress`` does not know.

    A codex ``apply_patch`` or an opencode ``bash`` IS a changed file or a passing test;
    without this the no_progress detector reads a working session as a stalled one. Tools
    already in the Claude Code spelling are skipped: compute_progress judged them, and a
    no-op Write must not become progress here.
    """
    for call in agent.calls:
        if call.name in NATIVE_TOOLS:
            continue
        result = agent.results.get(call.id)
        if result is None or result.is_error:
            continue
        name = (call.name or "").lower()
        inp = call.input if isinstance(call.input, dict) else {}
        kind = label = ""
        if any(marker in name for marker in EDIT_MARKERS):
            path = next((str(inp[k]) for k in EDIT_PATH_KEYS if inp.get(k)), "")
            if not path and isinstance(inp.get("changes"), list) and inp["changes"]:
                first = inp["changes"][0]
                path = str(first.get("path") or "") if isinstance(first, dict) else ""
            if path:
                kind, label = "edit", path
        elif name in SHELL_NAMES or "command" in inp:
            cmd = str(inp.get("command") or "")
            if sup.COMMIT_RE.search(cmd):
                kind, label = "commit", sup.clip(cmd, 60)
            elif sup.TEST_RE.search(cmd):
                kind, label = "test", sup.clip(cmd, 60)
        if kind:
            agent.progress.append((result.seq, kind, label, result.ts))
    agent.progress.sort(key=lambda p: p[0])


# --------------------------------------------------------------------------- discovery


def _session_config(directory: Path) -> dict:
    try:
        raw = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    config = raw.get("config") if isinstance(raw, dict) else None
    return config if isinstance(config, dict) else {}


def discover_sessions(cfg: dict, now: Optional[float] = None,
                      root: Optional[Path] = None,
                      include_finished: bool = False) -> sup.Scan:
    """Every daemon session worth judging, as ``awsupervise`` Agent records.

    A session whose log has not grown for ``silent_max_minutes`` is skipped ("stale"): a
    dormant session is not a stalled one, and judging ancient logs every pass would page
    about a machine that already rebooted. Finished sessions are skipped too unless the
    caller asked for them (a dry run with ``--include-finished``).
    """
    now = time.time() if now is None else now
    base = sessions_root(root)
    scan = sup.Scan(now=now, sessions={}, agents=[], router={})
    try:
        directories = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return scan
    silent_s = cfg["silent_max_minutes"] * 60
    for directory in directories:
        log = directory / "events.jsonl"
        try:
            stat = log.stat()
        except OSError:
            scan.skipped["no-log"] += 1
            continue
        age = now - stat.st_mtime
        if age > silent_s and not (include_finished and age <= cfg["horizon_hours"] * 3600):
            scan.skipped["stale"] += 1
            continue
        events, cut = sup.read_tail(log, cfg["tail_bytes"], cfg["max_line_bytes"])
        if not events:
            scan.skipped["empty"] += 1
            continue
        keep = cfg["awdk_tail_events"]
        tail = events[-keep:] if isinstance(keep, int) and keep > 0 else events
        try:
            agent = agent_from_events(directory.name, tail, log, last_write=stat.st_mtime,
                                      truncated=cut or len(tail) < len(events),
                                      config=_session_config(directory))
        except Exception as exc:  # one corrupt log must never blind the whole pass
            scan.skipped["unreadable"] += 1
            sys.stderr.write(f"awsupervise: {directory.name}: unreadable event log: "
                             f"{type(exc).__name__}: {exc}\n")
            continue
        if agent.finished and not include_finished:
            scan.skipped["finished"] += 1
            continue
        scan.agents.append(agent)
    return scan


# --------------------------------------------------------------------------- drift check


def _touches(inp: Any) -> bool:
    """Did this tool call name something -- a path, a command, a URL, a query?"""
    if not isinstance(inp, dict):
        return False
    for value in inp.values():
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def default_subs_path() -> Path:
    """Mirror of ``thread_subs.default_db_path``: the daemon's own table location."""
    env = os.environ.get("AITHER_THREAD_SUBS_DB", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "thread-subscriptions.db"


class ReadOnlySubscriptions:
    """A read-only view of the daemon's sqlite subscription table.

    The watchdog judges subscriptions; it never owns them. Opening the router's file
    read-write would let a dry run create it, and a watchdog competing for SQLite's write
    lock with the daemon is a deadlock waiting to happen. An absent file means no
    subscriptions exist yet, which is exactly the no-drift answer.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def threads_of(self, subscriber: Any) -> list:
        sid = str(subscriber or "").strip()
        if not sid or not self.path.is_file():
            return []
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                   timeout=5)
        except sqlite3.DatabaseError as exc:
            sys.stderr.write(f"awsupervise: thread subscriptions unreadable: {exc}\n")
            return []
        try:
            cur = conn.execute(
                "SELECT surface, thread_id, title, created, last_seen, delivered FROM subs "
                "WHERE subscriber=? ORDER BY last_seen DESC", (sid,))
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        except sqlite3.DatabaseError as exc:
            sys.stderr.write(f"awsupervise: thread subscriptions unreadable: {exc}\n")
            return []
        finally:
            conn.close()


def open_thread_subscriptions(cfg: dict) -> Any:
    """The table detector g reads, or None when it is disabled or does not exist."""
    if not cfg.get("thread_subs_enabled", True):
        return None
    raw = str(cfg.get("thread_subs_db") or "").strip()
    path = Path(raw) if raw else default_subs_path()
    return ReadOnlySubscriptions(path) if path.is_file() else None


def threads_of(subs: Any, session_id: str) -> list:
    """A session's thread subscriptions; a malformed id or a broken table reads as none.

    A session id the table refuses is a fact about the session, not a reason to abort a
    watchdog pass over every other session -- but it is said out loud, never swallowed.
    """
    try:
        return list(subs.threads_of(session_id) or [])
    except Exception as exc:  # noqa: BLE001 - a lookup must never crash the watchdog
        sys.stderr.write(f"awsupervise: subscriptions unreadable for {session_id}: {exc}\n")
        return []


def stalled_on_thread(agent: sup.Agent, subs: Any, cfg: dict,
                      now: float) -> Optional[sup.Finding]:
    """g. subscribed to a thread, touching nothing, and nothing delivered for too long.

    Two conditions, both required: the last ``thread_stall_steps`` steps contain no tool
    call naming anything to work on, and the freshest thread this session holds has not
    been delivered to for ``thread_stall_minutes``. A finished or never-started session
    is not drifting; it is done.
    """
    if subs is None or agent.finished or not agent.steps:
        return None
    threads = threads_of(subs, agent.session_id)
    if not threads:
        return None
    stall_s = cfg["thread_stall_minutes"] * 60
    newest = max(float(t.get("last_seen") or 0) for t in threads)
    if now - newest < stall_s:
        return None
    keep = max(1, int(cfg["thread_stall_steps"]))
    steps = agent.steps[-keep:]
    if any(step.get("kind") == EV_TOOL_CALL and _touches(step.get("input"))
           for step in steps):
        return None
    label = sup.clip(sup.redact(", ".join(
        f"{t.get('surface')}:{t.get('thread_id')}" for t in threads[:3])), 120)
    waited = sup._mins(now - newest)
    evidence = [f"thread {t.get('surface')}:{t.get('thread_id')} last delivered "
                f"{sup._mins(now - float(t.get('last_seen') or 0))} ago, "
                f"{t.get('delivered', 0)} delivered" for t in threads[:5]]
    evidence += [f"step {step.get('kind')}"
                 + (f" {step.get('tool')}" if step.get("tool") else "")
                 for step in steps[-3:]]
    return sup.Finding(
        agent, "stalled_on_thread",
        f"subscribed to {label} with no delivery for {waited} and no tool call touching "
        f"anything in its last {len(steps)} steps",
        f"You are subscribed to {label}. Nothing has been delivered for {waited}, and your "
        f"last {len(steps)} steps touched no file, command, thread or URL. Work the thread "
        "now (reply, edit, or run what it needs), or unsubscribe with thread.unsubscribe "
        "so the mail stops queuing, then report what you did.",
        evidence)


def thread_drift(agents: list, subs: Any, cfg: dict, now: float) -> list:
    out: list = []
    for agent in agents:
        finding = stalled_on_thread(agent, subs, cfg, now)
        if finding is not None:
            out.append(finding)
    return out


# --------------------------------------------------------------------------- escalation


def write_session_steer(steer_root: Path, sid: str, lines: list,
                        event_id: str) -> Optional[Path]:
    """L1 for a daemon session: the decisions store's PEER-authority mailbox writer.

    ``steer_root`` is accepted for the Actuator's signature and ignored: the store owns
    the mailbox path (``AITHER_STEER_DIR`` / ``~/.aither/steer``), which is the same
    directory, and one owner for the path is what keeps the drain hook and the writer from
    drifting apart.
    """
    _ = steer_root
    from adk.decisions.store import write_steer as store_write_steer

    return store_write_steer(sid, lines, suffix="supervise", sender="awsupervise",
                             authority="peer", origin_id=event_id, kind="supervisor")


class SessionActuator(sup.Actuator):
    """The same ladder, with L1 landing in the daemon session's own steering mailbox."""

    def __init__(self, paths: sup.Paths, cfg: dict, state: dict, now: float,
                 dry: bool = False, runner: Optional[Callable] = None,
                 steer_writer: Optional[Callable] = None) -> None:
        super().__init__(paths, cfg, state, now, {}, dry=dry, runner=runner,
                         steer_writer=steer_writer or write_session_steer,
                         steer_where="session event log")


# --------------------------------------------------------------------------- one pass


def scan_sessions_once(cfg: dict, now: Optional[float] = None, dry: bool = False,
                       root: Optional[Path] = None, subs: Any = None,
                       paths: Optional[sup.Paths] = None,
                       runner: Optional[Callable] = None,
                       include_finished: bool = False) -> dict:
    """One watchdog pass over the daemon's sessions: detect, then climb the shared ladder.

    ``subs`` is a ``thread_subs.ThreadSubscriptions`` table (a test convenience). When it
    is None -- every production entry point -- the daemon's own table is read through a
    read-only view (:func:`open_thread_subscriptions`), so detector g runs without a
    caller having to remember it. With no table on disk, or ``thread_subs_enabled``
    False, the drift detector stays off and the other detectors still run.
    """
    now = time.time() if now is None else now
    paths = paths or sup.Paths.default()
    if subs is None:
        subs = open_thread_subscriptions(cfg)
    scan = discover_sessions(cfg, now=now, root=root, include_finished=include_finished)
    findings: list = []
    for agent in scan.agents:
        findings += sup.detect(agent, cfg, now, {})
    findings += thread_drift(scan.agents, subs, cfg, now)
    live = [f for f in findings if not f.agent.finished]
    state = sup.load_state(paths)
    if dry:
        state = copy.deepcopy(state)
    events: list = []
    sup.poll_cards(paths, state, cfg, now, events)
    planned = sup.plan(live, state, cfg, now, events)
    actuator = SessionActuator(paths, cfg, state, now, dry=dry, runner=runner)
    records = sup.execute(planned, state, actuator, now)
    steers = state.setdefault("sent", {}).setdefault("steer", {})
    for sid in list(steers):
        steers[sid] = [t for t in steers[sid] if now - t < 3600]
        if not steers[sid]:
            del steers[sid]
    if not dry:
        for record in events + records:
            sup.log_event(paths, {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                                  **record})
        sup.save_state(paths, state)
    return {"now": now, "scan": scan, "findings": findings, "events": events,
            "actions": records, "state": state}


# --------------------------------------------------------------------------- report


def render_sessions(result: dict, verbose: bool = False) -> str:
    scan: sup.Scan = result["scan"]
    now = result["now"]
    by_session: dict = {}
    for finding in result["findings"]:
        by_session.setdefault(finding.agent.session_id, []).append(finding)
    out = [f"awsupervise awdk-sessions {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S}  "
           f"sessions scanned {len(scan.agents)} (finished {scan.skipped['finished']}, "
           f"stale {scan.skipped['stale']}, empty {scan.skipped['empty']}) | "
           f"{len(result['findings'])} finding(s) on {len(by_session)} session(s)"]
    for sid, findings in sorted(by_session.items()):
        agent = findings[0].agent
        ran = sup._mins(now - agent.started) if agent.started else "?"
        state = f", {agent.finished}" if agent.finished else ""
        harness = agent.agent_type or "?"
        out.append("")
        out.append(f'[{sid[:12]} "{harness}"] the main session ({agent.model or "model ?"}) '
                   f"ran {ran}, last write {sup._mins(now - agent.last_write)} ago{state}")
        for finding in findings:
            up = f'  -> {sup.CONFIG["upgrade_label"]}' if finding.upgrade else ""
            out.append(f"  {sup.DETECTOR_CODE[finding.detector]} {finding.detector:<16} "
                       f"{sup.redact(finding.summary)}{up}")
            if verbose:
                for ev in finding.evidence:
                    out.append(f"      {sup.redact(ev)}")
    if result["events"] or result["actions"]:
        out.append("")
        dry = any(r.get("dry") for r in result["actions"])
        out.append("actions" + (" (DRY RUN: nothing written)" if dry else ""))
    for record in result["actions"]:
        out.append(f"  L{record['level']} {record['action']:<5} {record['session'][:12]} "
                   f"{','.join(sup.DETECTOR_CODE[d] for d in record['detectors'])} "
                   f"{len(record['agents'])} agent(s): {record.get('result', '')}")
        if verbose and record.get("text"):
            for line in str(record["text"]).splitlines()[:40]:
                out.append(f"      | {line}")
    for event in result["events"]:
        if event["action"] in ("resolved", "card-closed", "re-tripped"):
            out.append(f"  {event['action']}: {event.get('incident') or event.get('card')}")
    return "\n".join(out)


def as_json_sessions(result: dict) -> dict:
    findings = [{"session": f.agent.session_id, "harness": f.agent.agent_type,
                 "model": f.agent.model, "detector": f.detector,
                 "code": sup.DETECTOR_CODE[f.detector], "summary": sup.redact(f.summary),
                 "evidence": [sup.redact(e) for e in f.evidence], "upgrade": f.upgrade,
                 "finished": f.agent.finished}
                for f in result["findings"]]
    return {"now": result["now"], "sessions_scanned": len(result["scan"].agents),
            "skipped": dict(result["scan"].skipped), "findings": findings,
            "actions": result["actions"], "events": result["events"]}
