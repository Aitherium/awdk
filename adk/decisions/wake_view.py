"""The scheduler behind a card: awask's read-only window onto awrise wakes.

WHY THIS IS HERE. awask is where the owner meets a decision. When awrise raises
``Wake 'nightly-sync' has failed 3x in a row``, the card carries the streak, the
run line and an output tail — and nothing else. The next question is always
about the SCHEDULER, not the card: is the clock even ticking, what else is
failing, when is this due again. Until now answering it meant leaving awask for
another surface, and the most common answer ("nothing has run at all") was the
one the card could not express.

ONE READER, AND IT IS THE DAEMON. Everything here goes through the awdk harness
daemon's ``/wakes`` window (:8362) — the same window Discord, awdesk and
AitherDesktop read. awask deliberately does NOT parse ``jobs.json`` and does NOT
spawn the ``awrise`` CLI:

  * a second parser is a rival implementation that drifts, and this package has
    already paid for that class once;
  * ``running``, ``next_due_at`` and clock liveness are derived from the awrise
    LEDGER, not from the job file, so a file reader cannot compute them at all —
    it would render a green list for a dead scheduler;
  * the daemon holds the bearer check and the argv control, so a read surface
    that bypasses it is a second security boundary to get right.

READ-ONLY, ON PURPOSE. awask already has exactly one write path into awrise: the
``wake-failed`` card recipe, whose answer becomes ``awrise disable --name <job>``
or ``awrise run --name <job>`` through ``steerback``. Adding enable/disable here
would be a SECOND write path with a second authorization story, for a surface
whose whole job is to present the decision rather than take it. Answer the card.

NOTHING HERE RAISES. awrise absent, the daemon down, a daemon too old to have a
``/wakes`` window, a token that is missing or refused — each is a distinct,
named line, and each is reported as an absence of knowledge rather than as an
absence of jobs. "I could not look" is never "nothing is wrong".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from adk.decisions.steerback import harness_token, harness_url

#: A read must never hold the owner's terminal. The daemon answers /wakes from
#: files it already has in hand, so this is generous.
TIMEOUT_SECONDS = 5.0

#: The job-name gate, identical to the daemon's own: first character
#: alphanumeric, so a name can never become an option ("-name") nor escape its
#: path segment ("../x"). Applied BEFORE the name reaches a URL.
_NAME_MAX = 64

#: ``last_state`` values that mean the wake did not succeed. Taken from the
#: awrise ledger's closed set plus the spellings other surfaces use, so a
#: vocabulary drift cannot silently turn a failure into a blank.
FAILED_STATES = frozenset({
    "failure", "failed", "timeout", "timed_out", "error", "orphaned", "missed",
})


def valid_name(name: str) -> bool:
    """The same gate the daemon applies before any path join."""
    if not name or len(name) > _NAME_MAX:
        return False
    if not (name[0].isascii() and name[0].isalnum()):
        return False
    return all(c.isascii() and (c.isalnum() or c in "._-") for c in name)


# ── the read ────────────────────────────────────────────────────────────────

def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The daemon's own ``detail`` string, or "" if the body says nothing.

    A body read must never mask the status: any failure here returns "" and the
    caller falls back to judging by code alone.
    """
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable error body is still an error
        return ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail.strip()
        if isinstance(detail, dict) and isinstance(detail.get("error"), str):
            return detail["error"].strip()
    return ""


def fetch(path: str) -> dict[str, Any]:
    """GET one daemon path. Returns a dict that ALWAYS carries ``error``.

    ``error`` is None only when the daemon answered 200 with a JSON object. Every
    other outcome names itself, because the four failures below are four
    different actions for the owner and they are indistinguishable once they are
    flattened into an empty list:

      no token          -> the daemon never saw a request
      404 on /wakes     -> the daemon is RUNNING OLD CODE; restart it
      401/403           -> the token is stale; re-mint it
      unreachable       -> the daemon is down
    """
    token = harness_token()
    if not token:
        return {"error": "no harness token (AITHER_HARNESS_TOKEN or "
                         "~/.aither/harness_token)", "installed": None}
    request = urllib.request.Request(f"{harness_url()}{path}", method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # TWO different 404s arrive on this path and they are OPPOSITE
            # diagnoses, so the body decides rather than the status:
            #
            #   FastAPI's own {"detail": "Not Found"} -> the ROUTE is absent:
            #     the daemon PROCESS is older than its source. Measured live
            #     2026-09-18, when every /wakes call 404'd while the file on
            #     disk defined all seven routes — a bind mount (and an editable
            #     install) makes the FILE current, not the PROCESS.
            #   the daemon's own {"detail": "no such wake: x"} -> the ROUTE is
            #     there and the JOB is not: a typo, not an outage.
            #
            # Collapsing them tells an owner to restart a healthy daemon
            # because they misspelled a job name.
            detail = _error_detail(exc)
            if detail and not detail.lower().startswith("not found"):
                return {"error": detail, "installed": True}
            return {"error": "daemon has no /wakes window "
                             "(restart the harness daemon)", "installed": None}
        if exc.code in (401, 403):
            return {"error": f"daemon refused the token (HTTP {exc.code})",
                    "installed": None}
        if exc.code == 503:
            return {"error": "awrise not installed on this host", "installed": False}
        return {"error": f"daemon answered HTTP {exc.code}", "installed": None}
    except (urllib.error.URLError, OSError) as exc:
        return {"error": f"daemon unreachable: {exc}", "installed": None}
    except Exception as exc:  # noqa: BLE001 - a read must never raise at a CLI
        return {"error": f"daemon read failed: {exc}", "installed": None}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # A 200 with a body that is not JSON is what a RESTARTING daemon (or
        # something in front of it) answers. It is an unreadable answer, never
        # an empty one.
        return {"error": f"daemon answer was not JSON ({exc})", "installed": None}
    if not isinstance(data, dict):
        return {"error": "daemon answer was not an object", "installed": None}
    data.setdefault("error", None)
    return data


def snapshot(failing_only: bool = False) -> dict[str, Any]:
    """The wake list as the daemon sees it. Never raises, never fabricates."""
    return fetch("/wakes?state=failing" if failing_only else "/wakes")


def one(name: str) -> dict[str, Any]:
    """One job plus its recent ledger rows. The name is gated before the URL."""
    if not valid_name(name):
        return {"error": f"invalid wake name: {name!r}", "installed": None}
    return fetch("/wakes/" + urllib.parse.quote(name, safe=""))


# ── rendering: the clock is stated BEFORE any job row ────────────────────────

def clock_was_measured(data: dict[str, Any]) -> bool:
    """Did this answer actually MEASURE the clock?

    ``clock_stale`` is ``False`` in the daemon's EMPTY answer too — an errored
    read, or one on a host where awrise's presence is still unknown, carries the
    same ``False`` a healthy tick does. Branching on that flag without this
    predicate is how an absence becomes a health claim, and on 2026-09-18 three
    surfaces printed "clock ok" directly above their own error line.
    """
    return data.get("installed") is True and not data.get("error")


def _age(stamp: str) -> str:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - when).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def clock_line(data: dict[str, Any]) -> str:
    """The FIRST line of any wake view: is the scheduler's clock alive?

    "ok" is reserved for a MEASURED read that carries an actual tick. Everything
    else says which kind of not-ok it is, because "silent since 09:00",
    "never ticked" and "I could not look" are three different problems.
    """
    if not clock_was_measured(data):
        if data.get("error"):
            return f"clock liveness unknown - {data['error']}"
        if data.get("installed") is False:
            return "clock liveness unknown - awrise is not installed here"
        return "clock liveness unknown - the daemon answered nothing about awrise"
    tick = data.get("last_tick_at")
    if not tick:
        return ("clock never ticked - enabled wakes with no runner is exactly "
                "the failure that hides itself")
    if data.get("clock_stale"):
        return f"clock SILENT since {tick} ({_age(tick)})"
    age = _age(tick)
    return f"clock ok - last tick {tick}" + (f" ({age})" if age else "")


def job_line(job: dict[str, Any]) -> str:
    """One row: name, schedule, last state and the failure streak."""
    name = str(job.get("name") or "?")
    every = str(job.get("every") or job.get("interval_s") or "?")
    state = str(job.get("last_state") or "never run")
    streak = job.get("consecutive_failures") or 0
    bits = [f"  {name}", f"every {every}", state]
    if not job.get("enabled", True):
        bits.append("DISABLED")
    if job.get("running"):
        bits.append("running now")
    if streak:
        bits.append(f"failed {streak}x in a row")
    if job.get("next_due_at") and not job.get("running"):
        bits.append(f"due {job['next_due_at']}")
    return "  ".join(bits)


def render(data: dict[str, Any], failing_only: bool = False) -> str:
    """The whole view. The clock first, then the rows, then what is missing."""
    lines = [clock_line(data)]
    if data.get("error"):
        # Stated a second time next to the rows: the clock line above explains
        # the clock, this explains why the LIST may be empty or partial.
        lines.append(f"! {data['error']}")
    if data.get("schema") == 1:
        lines.append("! jobs.json is still v1 - run `awrise list` on the host to migrate it")
    jobs = data.get("wakes")
    if not isinstance(jobs, list):
        jobs = []
    if jobs:
        lines.append("")
        lines.extend(job_line(j) for j in jobs if isinstance(j, dict))
    elif data.get("installed") is True and not data.get("error"):
        lines.append("")
        lines.append("  no failing wakes" if failing_only else "  no wakes scheduled")
    if data.get("installed") is True and not data.get("error"):
        lines.append("")
        lines.append(f"  {data.get('count', len(jobs))} wakes - "
                     f"{data.get('failing', 0)} failing, "
                     f"{data.get('running', 0)} running, "
                     f"{data.get('disabled', 0)} disabled")
        lines.append("  a failing wake raises a card here: `awask list`")
    return "\n".join(lines)


def render_one(job: dict[str, Any]) -> str:
    """One job in full, with the ledger rows that explain its state."""
    if job.get("error"):
        return f"! {job['error']}"
    lines = [job_line(job).strip()]
    if job.get("run"):
        lines.append(f"  run: {job['run']}")
    if job.get("last_reason"):
        lines.append(f"  reason: {job['last_reason']}")
    recent = job.get("recent")
    if isinstance(recent, list) and recent:
        lines.append("")
        lines.append("  recent:")
        for row in recent[:10]:
            if not isinstance(row, dict):
                continue
            bits = [str(row.get("ts") or "?"), str(row.get("state") or "?")]
            if row.get("reason"):
                bits.append(str(row["reason"]))
            if row.get("exit_code") is not None:
                bits.append(f"exit {row['exit_code']}")
            lines.append("    " + "  ".join(bits))
    tail = str(job.get("output_tail") or "").strip()
    if tail:
        lines.append("")
        lines.append("  output tail:")
        lines.extend("    " + ln for ln in tail.splitlines()[-20:])
    return "\n".join(lines)


# ── the exit code ───────────────────────────────────────────────────────────

def exit_code(data: dict[str, Any]) -> int:
    """0 measured and healthy - 1 a real problem - 2 could not judge.

    The 2 is the point: a read that learned nothing must NOT exit 0. A caller
    that scripts `awask wakes || page-me` would otherwise be silenced by exactly
    the outage it is watching for.
    """
    if not clock_was_measured(data):
        return 2
    if data.get("clock_stale") or not data.get("last_tick_at"):
        return 1
    if (data.get("failing") or 0) > 0:
        return 1
    return 0


def view(name: Optional[str] = None, failing_only: bool = False,
         as_json: bool = False) -> tuple[str, int]:
    """One call for a CLI: ``(text, exit_code)``. Never raises."""
    if name:
        data = one(name)
        text = json.dumps(data, indent=2) if as_json else render_one(data)
        if data.get("error"):
            return text, 2
        return text, (1 if (data.get("consecutive_failures") or 0) else 0)
    data = snapshot(failing_only=failing_only)
    text = json.dumps(data, indent=2) if as_json else render(data, failing_only)
    return text, exit_code(data)
