"""N-of-M approvals for one device action: pure functions over a JSON-able dict.

An approval is created for ONE canonical action and carries its digest. It resolves:

* ``approved`` the moment ``required`` distinct allowed approvers have said yes;
* ``denied`` the moment so many allowed approvers said no that ``required`` yeses can
  no longer be reached (with 1-of-2, one no still leaves the other guardian);
* ``expired`` once ``expires_at`` passes while it was still pending.

Only ``approved`` may run, once (:func:`claim_for_run` flips it to ``executing``), and
only if the action it holds still hashes to its digest. Who counts is fixed when it is
created (``approvers`` = the eligible pids then) and re-checked at each vote by the host
(``eligible`` = the pids allowed NOW): a guardian removed from the roster stops counting.
The store is whatever dict the host persists; nothing here does I/O.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .policy import ANY_GUARDIAN, action_digest

PENDING, APPROVED, DENIED, EXPIRED, EXECUTING, EXECUTED, FAILED = (
    "pending", "approved", "denied", "expired", "executing", "executed", "failed")
OPEN = frozenset({PENDING})
#: Resolved approvals kept per store (newest kept).
KEEP = 200


class ApprovalError(ValueError):
    pass


def resolve_approvers(approvers: Any, guardians: Iterable[str]) -> List[str]:
    """The pids who may answer: every current guardian, or the named ones."""
    g = sorted({str(p) for p in guardians})
    if approvers == ANY_GUARDIAN or approvers is None:
        return g
    return sorted({str(p) for p in approvers})


def create(store: Dict[str, Any], *, action: Mapping[str, Any], household: str,
           verdict: str, required: int, approvers: List[str], requested_by: Mapping[str, Any],
           expires_s: int, now: float, summary: str, policy: Mapping[str, Any]) -> Dict[str, Any]:
    """A new pending approval in ``store`` (``{"approvals": {id: row}}``)."""
    if required < 1:
        raise ApprovalError("an approval needs at least one yes")
    if len(approvers) < required:
        raise ApprovalError(f"this needs {required} approvals but only {len(approvers)} "
                            "people may give one; nothing was asked")
    rows = store.setdefault("approvals", {})
    _prune(rows)
    aid = "ha-" + secrets.token_hex(6)
    row = {"id": aid, "household": household, "status": PENDING, "verdict": verdict,
           "action": dict(action), "digest": action_digest(action, household),
           "summary": summary, "required": int(required), "approvers": list(approvers),
           "votes": {}, "approvals": 0, "requested_by": dict(requested_by),
           "created_at": now, "expires_at": now + int(expires_s), "policy": dict(policy)}
    rows[aid] = row
    return row


def _prune(rows: Dict[str, Any]) -> None:
    done = [r for r in rows.values() if r.get("status") not in OPEN]
    if len(done) > KEEP:
        done.sort(key=lambda r: r.get("created_at", 0))
        for r in done[: len(done) - KEEP]:
            rows.pop(r["id"], None)


def refresh(row: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Expire a pending row whose time is up."""
    if row["status"] == PENDING and now >= float(row["expires_at"]):
        row["status"] = EXPIRED
        row["resolved_at"] = now
    return row


def vote(row: Dict[str, Any], *, pid: str, allow: bool, now: float,
         eligible: Iterable[str]) -> Dict[str, Any]:
    """Record ``pid``'s answer; returns the row with its new status.

    :class:`ApprovalError` when the row is not pending (or expired now) or ``pid`` may
    not answer it. A second answer from the same person replaces the first."""
    refresh(row, now)
    if row["status"] != PENDING:
        raise ApprovalError(f"this request is {row['status']}; nothing changed")
    allowed_now = set(row["approvers"]) & {str(p) for p in eligible}
    if str(pid) not in allowed_now:
        raise ApprovalError("you are not one of the people who can answer this")
    row["votes"][str(pid)] = {"allow": bool(allow), "at": now}
    yes = sum(1 for p, v in row["votes"].items() if v["allow"] and p in allowed_now)
    no = sum(1 for p, v in row["votes"].items() if not v["allow"] and p in allowed_now)
    row["approvals"] = yes
    if yes >= row["required"]:
        row["status"] = APPROVED
        row["resolved_at"] = now
    elif len(allowed_now) - no < row["required"]:
        row["status"] = DENIED
        row["resolved_at"] = now
    return row


def claim_for_run(row: Dict[str, Any], now: float) -> Dict[str, Any]:
    """The action an APPROVED row may run, once; its digest is checked again here."""
    refresh(row, now)
    if row["status"] != APPROVED:
        raise ApprovalError(f"this request is {row['status']}; nothing ran")
    if action_digest(row["action"], row["household"]) != row["digest"]:
        row["status"] = FAILED
        raise ApprovalError("the stored action no longer matches what was approved; "
                            "nothing ran")
    row["status"] = EXECUTING
    return dict(row["action"])


def finish(row: Dict[str, Any], ok: bool, result: Any, now: float) -> Dict[str, Any]:
    row["status"] = EXECUTED if ok else FAILED
    row["result"] = result
    row["finished_at"] = now
    return row


def public(row: Mapping[str, Any]) -> Dict[str, Any]:
    """What a person (or the notifier) sees of a row."""
    keys = ("id", "status", "verdict", "summary", "required", "approvals", "approvers",
            "requested_by", "created_at", "expires_at", "household", "digest")
    out = {k: row.get(k) for k in keys}
    out["votes"] = {p: v.get("allow") for p, v in (row.get("votes") or {}).items()}
    out["class"] = (row.get("policy") or {}).get("cls")
    if row.get("result") is not None:
        out["result"] = row["result"]
    return out


def pending_for(store: Mapping[str, Any], pid: str, now: float,
                mine: bool = True) -> List[Dict[str, Any]]:
    """Pending rows ``pid`` can answer (and, with ``mine``, rows they asked for)."""
    out = []
    for row in (store.get("approvals") or {}).values():
        refresh(row, now)
        if row["status"] == PENDING and (pid in row["approvers"] or (
                mine and (row.get("requested_by") or {}).get("pid") == pid)):
            out.append(public(row))
    return sorted(out, key=lambda r: r["created_at"])


def get(store: Mapping[str, Any], aid: str) -> Optional[Dict[str, Any]]:
    return (store.get("approvals") or {}).get(str(aid or "").strip())
