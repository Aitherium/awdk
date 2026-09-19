"""Read-only view of awrise's on-disk state, plus argv-only control.

awrise (the host-clock routine scheduler) keeps everything under ``AWRISE_HOME``
(default ``~/.aither/awrise``): ``jobs.json`` and an append-only ledger. This
module is the ONE place the harness daemon reads those files. It never writes
under ``AWRISE_HOME`` and never imports ``awrise``: a consumer must keep working
on a box where awrise is not installed, and "not installed" is an ordinary
answer here (``installed: false``), never an exception.

Mutation is not done through the files either. The only way this module changes
anything is by building an argv LIST for the awrise CLI (``enable``, ``disable``,
``run``); the daemon spawns that list and propagates the exit code. A shell
string is never built, and a job name is validated by ``_NAME_RE`` before it
reaches a path join or an argv element, so a name can never become an option or
a traversal.

Two ``jobs.json`` shapes exist on disk and both are read:

* schema 2 — ``{"schema": 2, "jobs": {name: {...}}}``, what awrise writes.
* schema 1 — a bare ``{name: {interval, command, last_run, last_status}}`` map,
  what older installs left behind (awrise migrates it in memory on load; a
  reader that only knows schema 2 would report zero jobs for a live file). The
  same field mapping awrise applies is applied here, for DISPLAY only.

Liveness is read from the ledger, not from ``jobs.json``: the newest ``tick``
row proves the host clock fired, and a ``started`` row with no closing row is a
wake still running. A green job list with no ticks is the failure this exists
to surface, so ``clock_stale`` rides on every list answer.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "AWRISE_HOME",
    "CLOCK_STALE_S",
    "MAX_INTERVAL_S",
    "VERBS",
    "awrise_home",
    "build_argv",
    "get_wake",
    "read_jobs",
    "read_ledger",
    "resolve_bin",
    "snapshot",
    "valid_name",
]

#: Job names accepted anywhere a name reaches a path or an argv. First character
#: alphanumeric, so a name can never start with ``-`` (an option to the CLI) or
#: ``.`` (a traversal segment); 64 characters at most.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The awrise CLI verbs the daemon may spawn. Anything else is refused by name.
VERBS = ("enable", "disable", "run")

#: The host clock fires every minute; five minutes without a ``tick`` row is a
#: clock that has stopped, whatever ``jobs.json`` says.
CLOCK_STALE_S = 300.0

#: Widest interval this reader will render or project a due time for. awrise's
#: grammar has no ceiling, but ``float()`` also accepts ``"inf"`` and ``1e308``,
#: and both blow up ``int()`` / ``timedelta`` (OverflowError) — one such value
#: in ``jobs.json`` must mark THAT job, never take the whole list down. A
#: century is far past any wake that means anything and far below the overflow.
MAX_INTERVAL_S = 100 * 365.25 * 86400.0

#: Lowest sort key for a ledger row whose ``ts`` is missing or unparseable.
_TS_FLOOR = datetime.min.replace(tzinfo=timezone.utc)

#: Ledger events that close a wake opened by a ``started`` row.
_CLOSING_EVENTS = frozenset({"finished", "missed", "reconciled", "orphaned"})

#: Every field a job answer carries, in this order, defaulting to ``None`` so a
#: consumer never sees a KeyError for a field awrise has not stamped yet.
_JOB_FIELDS = (
    "enabled", "every", "interval_s", "run", "timeout_s", "overlap", "detach", "cwd",
    "missed", "at", "report", "executor",
    "last_wake_id", "last_started_at", "last_finished_at", "last_state", "last_reason",
    "consecutive_failures", "created_at", "updated_at",
)

#: The ledger projection every consumer sees. Missing keys are ``None``.
_ROW_FIELDS = (
    "ts", "job", "event", "state", "reason", "wake_id", "pass_id",
    "duration_s", "exit_code", "output_tail", "pid",
)

AWRISE_HOME = os.environ.get("AWRISE_HOME") or str(Path.home() / ".aither" / "awrise")


# ── names, home, binary ──────────────────────────────────────────────────────


def valid_name(name: Any) -> bool:
    """True only for a job name that may reach a path or an argv."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def awrise_home() -> Path:
    """``AWRISE_HOME`` from the daemon's OWN environment, read at call time.

    Read per call (not cached at import) so a test can point it at a tmp dir
    and so an operator's env change takes effect on restart. Never taken from
    a request payload.
    """
    return Path(os.environ.get("AWRISE_HOME") or Path.home() / ".aither" / "awrise")


def resolve_bin() -> Optional[str]:
    """Absolute path of the awrise CLI, or None when there is nothing to spawn.

    ``AWRISE_BIN`` wins (a venv the daemon cannot see on PATH), then
    ``shutil.which``. The result must be an existing regular file: a dangling
    env var or a directory named ``awrise`` is "not installed", not a crash in
    ``Popen``.
    """
    candidate = (os.environ.get("AWRISE_BIN") or "").strip() or shutil.which("awrise")
    if not candidate:
        return None
    resolved = os.path.abspath(candidate)
    if not Path(resolved).is_file():
        return None
    return resolved


def build_argv(binary: str, verb: str, name: str) -> list[str]:
    """The exact argv the daemon spawns. A list, never a string; refuses by name."""
    if verb not in VERBS:
        raise ValueError(f"unsupported awrise verb: {verb!r}")
    if not valid_name(name):
        raise ValueError("invalid wake name")
    return [binary, verb, "--name", name]


# ── time helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO string -> aware UTC datetime. Naive input is read as UTC. Bad -> None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _seconds_to_every(seconds: float) -> str:
    """Render seconds the way awrise does (``2h``, ``900s``, ``1.5s``)."""
    for unit, span in (("w", 604800.0), ("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
        if seconds >= span and seconds % span == 0:
            return f"{int(seconds // span)}{unit}"
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds}s"


def _interval_seconds(value: Any) -> Optional[float]:
    """``interval_s`` / v1 ``interval`` as a usable float, else None.

    None for anything that is not a finite positive number within
    ``MAX_INTERVAL_S``: strings that are not numbers, booleans, NaN, ``inf``,
    ``1e308``. Every caller treats None as "interval unreadable" for that ONE
    job, so a bad value never raises out of a list answer.
    """
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_INTERVAL_S:
        return None
    return seconds


def _failure_count(value: Any) -> Optional[int]:
    """``consecutive_failures`` as a non-negative int, else None.

    awrise writes an int; a hand-edited ``"3"`` still reads as 3, while a
    list, a bool, NaN or a negative number reads as None (unreadable) — never
    as something a ``>=`` can raise on.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 and value == int(value) else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


def _ts_key(row: dict[str, Any]) -> datetime:
    """Sortable ``ts`` for a ledger row; unparseable sorts lowest, never raises."""
    return _parse_ts(row.get("ts")) or _TS_FLOOR


# ── jobs.json ────────────────────────────────────────────────────────────────


def _empty_job(name: str) -> dict[str, Any]:
    job: dict[str, Any] = {"name": name}
    for field in _JOB_FIELDS:
        job[field] = None
    job["error"] = None
    return job


def _project_v2(name: str, raw: Any) -> dict[str, Any]:
    job = _empty_job(name)
    if not isinstance(raw, dict):
        job["error"] = "job record is not an object"
        return job
    for field in _JOB_FIELDS:
        if field in raw:
            job[field] = raw[field]
    if job["enabled"] is None:
        job["enabled"] = True
    if job["consecutive_failures"] is None:
        job["consecutive_failures"] = 0
    else:
        failures = _failure_count(job["consecutive_failures"])
        if failures is None:
            job["error"] = "consecutive_failures unreadable"
            failures = 0
        job["consecutive_failures"] = failures
    if job["interval_s"] is not None and _interval_seconds(job["interval_s"]) is None:
        job["error"] = job["error"] or "interval unreadable"
    return job


def _project_v1(name: str, raw: Any) -> dict[str, Any]:
    """The mapping awrise's v1 migration applies, without importing awrise."""
    job = _empty_job(name)
    job["enabled"] = True
    job["consecutive_failures"] = 0
    if not isinstance(raw, dict):
        job["error"] = "job record is not an object"
        return job
    job["run"] = raw.get("command")
    seconds = _interval_seconds(raw.get("interval"))
    if seconds is not None:
        job["interval_s"] = seconds
        job["every"] = _seconds_to_every(seconds)
    else:
        job["error"] = "interval unreadable"
    started = _parse_ts(raw.get("last_run"))
    job["last_started_at"] = _iso(started) if started else None
    status = raw.get("last_status")
    job["last_state"] = status
    job["last_reason"] = "migrated_from_v1" if status else None
    return job


def _flag_name(job: dict[str, Any]) -> dict[str, Any]:
    """A job whose name fails the gate is LISTED (the count stays honest) but
    marked, since no route or argv may ever carry that name."""
    if job.get("error") is None and not valid_name(job["name"]):
        job["error"] = "name not addressable"
    return job


_V1_MIGRATION = "jobs.json is v1 (in-memory mapping); run `awrise list` to migrate it on disk"


def read_jobs(home: Optional[Path] = None) -> dict[str, Any]:
    """Read ``jobs.json``. Never raises; never writes.

    Returns ``{installed, schema, migration, error, jobs}`` where ``jobs`` maps
    name -> projected job. ``installed`` is False only when the file is absent.
    """
    base = home or awrise_home()
    path = base / "jobs.json"
    out: dict[str, Any] = {"installed": False, "schema": None, "migration": None,
                           "error": None, "jobs": {}}
    try:
        if not path.is_file():
            return out
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        out["installed"] = True
        out["error"] = f"jobs.json unreadable: {exc}"
        return out
    out["installed"] = True
    try:
        raw = json.loads(text)
    except ValueError as exc:
        out["error"] = f"jobs.json unreadable: {exc}"
        return out
    if not isinstance(raw, dict):
        out["error"] = "jobs.json unreadable: top level is not an object"
        return out
    if raw.get("schema") == 2:
        jobs = raw.get("jobs")
        if not isinstance(jobs, dict):
            out["error"] = "jobs.json unreadable: unsupported schema 2 (jobs is not an object)"
            return out
        out["schema"] = 2
        out["jobs"] = {str(n): _flag_name(_project_v2(str(n), j)) for n, j in jobs.items()}
        return out
    if "schema" in raw or "jobs" in raw or not all(isinstance(v, dict) for v in raw.values()):
        out["error"] = f"jobs.json unreadable: unsupported schema {raw.get('schema')!r}"
        return out
    out["schema"] = 1
    out["migration"] = _V1_MIGRATION
    out["jobs"] = {str(n): _flag_name(_project_v1(str(n), j)) for n, j in raw.items()}
    return out


# ── ledger ───────────────────────────────────────────────────────────────────


def ledger_files(home: Optional[Path] = None) -> list[Path]:
    """Ledger files newest-first: ``ledger/*.jsonl``, else a flat ``ledger.jsonl``."""
    base = home or awrise_home()
    daily = base / "ledger"
    files: list[Path] = []
    try:
        if daily.is_dir():
            files = sorted((p for p in daily.glob("*.jsonl") if p.is_file()), reverse=True)
    except OSError:
        files = []
    if files:
        return files
    flat = base / "ledger.jsonl"
    try:
        return [flat] if flat.is_file() else []
    except OSError:
        return []


def _iter_file_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    """(rows oldest-first, skipped count). A half-written line is skipped."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    skipped += 1
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    skipped += 1
    except OSError:
        return rows, skipped
    return rows, skipped


def _all_rows(home: Optional[Path]) -> tuple[list[dict[str, Any]], int]:
    """Every ledger row NEWEST-first, plus how many lines were unreadable."""
    out: list[dict[str, Any]] = []
    skipped = 0
    for path in ledger_files(home):
        rows, bad = _iter_file_rows(path)
        skipped += bad
        out.extend(reversed(rows))
    return out, skipped


def project_row(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in _ROW_FIELDS}


def read_ledger(
    home: Optional[Path] = None,
    *,
    job: Optional[str] = None,
    limit: int = 50,
    since: Optional[str] = None,
    event: Optional[str] = None,
) -> dict[str, Any]:
    """Projected rows newest-first. ``{"rows": [...], "count": n, "skipped": m}``."""
    limit = max(0, min(int(limit), 500))
    since_dt = _parse_ts(since) if since else None
    rows, skipped = _all_rows(home)
    out: list[dict[str, Any]] = []
    for row in rows:
        if job is not None and row.get("job") != job:
            continue
        if event and row.get("event") != event:
            continue
        if since_dt is not None:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < since_dt:
                continue
        out.append(project_row(row))
        if len(out) >= limit:
            break
    return {"rows": out, "count": len(out), "skipped": skipped}


def _last_tick(rows_newest_first: list[dict[str, Any]]) -> Optional[str]:
    for row in rows_newest_first:
        if row.get("event") == "tick":
            ts = _parse_ts(row.get("ts"))
            return _iso(ts) if ts else None
    return None


def _open_wakes(rows_newest_first: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """job -> its newest ``started`` row that no later closing row has matched."""
    started: dict[str, dict[str, Any]] = {}
    for row in reversed(rows_newest_first):  # oldest-first, like awrise
        wake = row.get("wake_id")
        if not wake or not isinstance(wake, str):
            # awrise writes ``w-<8>``; a list or an object here is a malformed
            # row, and it must not become a dict key (TypeError: unhashable).
            continue
        ev = row.get("event")
        if ev == "started":
            started[wake] = row
        elif ev in _CLOSING_EVENTS:
            started.pop(wake, None)
    per_job: dict[str, dict[str, Any]] = {}
    for row in started.values():
        name = row.get("job")
        if not isinstance(name, str):
            continue
        prev = per_job.get(name)
        if prev is None or _ts_key(row) >= _ts_key(prev):
            per_job[name] = row
    return per_job


def _next_due_at(job: dict[str, Any]) -> Optional[str]:
    """``last_started_at + interval_s`` for interval jobs only; else None.

    Deliberately NOT awrise's due-ness: anchored (``at``) jobs, never-run jobs
    and the ``missed`` policy stay awrise's authority.
    """
    if job.get("at") is not None:
        return None
    last = _parse_ts(job.get("last_started_at"))
    if last is None:
        return None
    interval = _interval_seconds(job.get("interval_s"))
    if interval is None:
        return None
    try:
        return _iso(last + timedelta(seconds=interval))
    except OverflowError:  # a last_started_at near datetime.max
        return None


# ── the answers the daemon serves ────────────────────────────────────────────


def _decorate(job: dict[str, Any], open_by_job: dict[str, dict[str, Any]]) -> dict[str, Any]:
    live = open_by_job.get(job["name"])
    job["running"] = live is not None
    job["running_wake_id"] = live.get("wake_id") if live else None
    started = _parse_ts(live.get("ts")) if live else None
    job["running_since"] = _iso(started) if started else None
    job["next_due_at"] = _next_due_at(job)
    return job


def snapshot(home: Optional[Path] = None, now: Optional[datetime] = None,
             state: Optional[str] = None) -> dict[str, Any]:
    """The full list answer: jobs, counts, clock liveness. Never raises."""
    base = home or awrise_home()
    current = now or _now()
    jobs = read_jobs(base)
    rows, skipped = _all_rows(base) if jobs["installed"] else ([], 0)
    open_by_job = _open_wakes(rows)
    wakes = [_decorate(j, open_by_job) for j in jobs["jobs"].values()]
    wakes.sort(key=lambda j: j["name"])

    last_tick = _last_tick(rows)
    any_enabled = any(bool(j.get("enabled")) for j in wakes)
    clock_stale = False
    if jobs["installed"]:
        if last_tick is None:
            clock_stale = any_enabled
        else:
            tick_dt = _parse_ts(last_tick)
            clock_stale = tick_dt is None or (current - tick_dt).total_seconds() > CLOCK_STALE_S

    failing = sum(1 for j in wakes if (_failure_count(j.get("consecutive_failures")) or 0) >= 1)
    disabled = sum(1 for j in wakes if j.get("enabled") is False)
    running = sum(1 for j in wakes if j.get("running"))

    if state == "failing":
        wakes = [j for j in wakes if (_failure_count(j.get("consecutive_failures")) or 0) >= 1]
    elif state == "running":
        wakes = [j for j in wakes if j.get("running")]
    elif state == "disabled":
        wakes = [j for j in wakes if j.get("enabled") is False]

    return {
        "installed": jobs["installed"],
        "home": str(base),
        "schema": jobs["schema"],
        "migration": jobs["migration"],
        "count": len(jobs["jobs"]),
        "failing": failing,
        "disabled": disabled,
        "running": running,
        "last_tick_at": last_tick,
        "clock_stale": clock_stale,
        "ledger_skipped": skipped,
        "wakes": wakes,
        "error": jobs["error"],
    }


def get_wake(name: str, home: Optional[Path] = None, now: Optional[datetime] = None,
             recent: int = 10) -> Optional[dict[str, Any]]:
    """One job plus its last ``recent`` ledger rows, or None when it does not exist.

    Callers distinguish "no such job" from "no awrise" with ``read_jobs``'s
    ``installed`` — this returns None for both.
    """
    if not valid_name(name):
        return None
    base = home or awrise_home()
    jobs = read_jobs(base)
    job = jobs["jobs"].get(name)
    if job is None:
        return None
    rows, _skipped = _all_rows(base)
    job = _decorate(job, _open_wakes(rows))
    job["recent"] = [project_row(r) for r in rows if r.get("job") == name][:max(0, recent)]
    job["schema"] = jobs["schema"]
    return job
