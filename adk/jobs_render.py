"""Human renderers for ``adk jobs list|status`` (local jobs and remote expeditions).

Pure functions: rows in, display lines out, so the layout is testable without a
job store or a server. Every screen leads with the next action and caps its
lists at five (``--all`` shows the rest); see :mod:`adk._attention`.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Sequence

from adk import _attention as att

#: Status words that mean "this needs the reader", ranked first.
FAILED = frozenset({"failed", "error", "errored", "timeout", "timed_out"})
#: Stopped and waiting on someone: ranked after failures.
WAITING = frozenset({"paused", "blocked", "awaiting_input", "needs_input"})
#: Still working: nothing to do yet.
ACTIVE = frozenset({"running", "in_progress", "active", "queued", "pending"})


def _rank(status: str) -> int:
    s = (status or "").lower()
    if s in FAILED:
        return 0
    if s in WAITING:
        return 1
    if s in ACTIVE:
        return 2
    return 3


def rank_by_status(rows: Sequence[Dict[str, Any]], key: str = "status") -> List[Dict[str, Any]]:
    """Failed, then waiting, then active, then the rest; stable within a rank."""
    return sorted(rows, key=lambda r: _rank(str(r.get(key, ""))))


def _remote_flag(remote: bool) -> str:
    return " --remote" if remote else ""


def render_job_list(jobs: Sequence[Dict[str, Any]], *, remote: bool = False,
                    show_all: bool = False) -> List[str]:
    """``adk jobs list`` / ``adk jobs list --remote``.

    Args:
        jobs: Local rows (``id``, ``status``, ``query``, ``remote_id``) or remote
            expeditions (``id``, ``status``, ``title``).
        remote: Render the remote (expedition) table.
        show_all: ``--all`` -- every row, no cap.
    """
    flag = _remote_flag(remote)
    ranked = rank_by_status(jobs)
    failed = [j for j in ranked if _rank(str(j.get("status", ""))) == 0]
    waiting = [j for j in ranked if _rank(str(j.get("status", ""))) == 1]
    active = [j for j in ranked if _rank(str(j.get("status", ""))) == 2]
    idw = 20 if remote else 8

    out: List[str] = []
    if failed:
        jid = str(failed[0].get("id", ""))[:idw]
        out.append(att.next_line(att.run(f"adk jobs status {jid}{flag}"), "it failed"))
    elif waiting:
        jid = str(waiting[0].get("id", ""))[:idw]
        out.append(att.next_line(att.run(f"adk jobs status {jid}{flag}"),
                                 f"it is {waiting[0].get('status')}"))
    elif active:
        out.append(att.fit(f"{att.IDLE} {len(active)} running."))
    else:
        out.append(att.IDLE)

    title = "Expeditions / Jobs" if remote else "Local Jobs"
    out.append(f"{title} ({len(jobs)})")
    if remote:
        out.append(f"  {'ID':<20s} {'Status':<12s} Title")
        rows = [f"{str(j.get('id', 'unknown'))[:20]:<20s} "
                f"{str(j.get('status', 'unknown'))[:12]:<12s} {j.get('title', '') or ''}"
                for j in ranked]
    else:
        out.append(f"  {'ID':<8s} {'Status':<10s} {'Synced':<6s} Query")
        rows = [f"{str(j.get('id', ''))[:8]:<8s} {str(j.get('status', ''))[:10]:<10s} "
                f"{'yes' if j.get('remote_id') else '-':<6s} {j.get('query') or ''}"
                for j in ranked]
    shown, hidden = att.cap(rows, show_all=show_all)
    out.extend(att.fit(f"  {r}") for r in shown)
    if hidden:
        out.append(att.more_line(hidden, f"adk jobs list{flag} --all"))
    if failed:
        jid = str(failed[0].get("id", ""))[:idw]
        noun = "job failed" if len(failed) == 1 else "jobs failed"
        out.append(att.count_line(len(failed), noun, att.run(f"adk jobs status {jid}{flag}")))
    return out


def render_local_status(job: Dict[str, Any]) -> List[str]:
    """``adk jobs status <id>`` for a local job: next action, then the record."""
    jid = str(job.get("id", ""))[:8]
    status = str(job.get("status", ""))
    out: List[str] = []
    if _rank(status) == 0:
        query = shlex.quote(str(job.get("query") or ""))
        out.append(att.next_line(att.run(f"adk jobs start {query}"), "retry it"))
    elif status in ("running", "queued"):
        out.append(att.fit(f"{att.IDLE} It is {status}; steer it with "
                           f"`adk jobs steer {jid} \"<message>\"`."))
    else:
        out.append(att.IDLE)
    out.append(f"Job {jid}  [{status}]")
    out.append(f"Query: {job.get('query')}")
    if job.get("remote_id"):
        out.append(f"Synced → {job.get('remote_url', '')}/expedition/{job['remote_id']}")
    if job.get("error"):
        out.append(f"Error: {job['error']}")
    if job.get("result"):
        out.append("")
        out.append(str(job["result"]))
    return out


def render_expedition_status(exp_id: str, status: Dict[str, Any],
                             tasks: Sequence[Dict[str, Any]], *,
                             show_all: bool = False) -> List[str]:
    """``adk jobs status <id> --remote``: next action, fields, tasks ranked and capped."""
    state = str(status.get("status", ""))
    ranked = rank_by_status(tasks)
    failed_tasks = [t for t in ranked if _rank(str(t.get("status", ""))) == 0]
    steer = att.run(f"adk jobs steer {exp_id} \"<what to change>\" --remote")

    out: List[str] = []
    if _rank(state) == 0:
        out.append(att.next_line(steer, f"expedition {state}"))
    elif failed_tasks:
        out.append(att.next_line(steer, f"{len(failed_tasks)} task(s) failed"))
    elif _rank(state) == 1:
        out.append(att.next_line(steer, f"it is {state}"))
    elif _rank(state) == 2:
        out.append(att.fit(f"{att.IDLE} Running; follow it: `adk jobs watch {exp_id}`."))
    else:
        out.append(att.IDLE)

    out.append(att.fit(f"Expedition: {exp_id}"))
    for key, value in status.items():
        if key != "id":
            out.append(att.fit(f"  {key:.<20s} {value}"))
    if ranked:
        out.append("")
        out.append(f"Tasks ({len(ranked)})")
        steps = []
        for t in ranked:
            line = f"[{str(t.get('status', 'unknown'))}] {t.get('id', 'unknown')} " \
                   f"{t.get('title', '') or ''}".rstrip()
            detail = ""
            if t.get("error"):
                detail = f"error: {t['error']}"
            elif t.get("result_summary"):
                detail = f"result: {t['result_summary']}"
            steps.append((line, detail))
        out.extend(att.numbered(steps, show_all=show_all,
                                more_cmd=f"adk jobs status {exp_id} --remote --all"))
    if failed_tasks:
        noun = "task failed" if len(failed_tasks) == 1 else "tasks failed"
        out.append(att.count_line(len(failed_tasks), noun, steer))
    return out


__all__ = [
    "render_expedition_status", "render_job_list", "render_local_status", "rank_by_status",
]
