"""Life tools for the home agent: reminders, follow-ups and "what did you do?".

These are the only tools besides ``web`` and ``decisions`` that ``adk home serve``
gives its agent. Every row is local, in ``<home>/followups.json``, so the owner
can list and cancel anything the agent promised to do later::

    remind_me(when, text)                          one reminder DM at `when`
    follow_up(when, text)                          one agent turn at `when`, DMed to you
    follow_up_recurring(when, text, recurring)     the same, repeating (ALWAYS ASKS)
    list_followups()                               what is scheduled
    cancel_followup(id)                            stop one
    receipts(n)                                    the last n signed receipts

WHY ``follow_up_recurring`` IS ITS OWN TOOL. The approval plane
(``adk.approval``) gates by tool NAME. A repeating job is the one life action
that keeps acting without being asked again, so it must pause for the owner's
yes; a one-shot reminder must not, or "remind me in 5 minutes" would need a
second message. One name per risk is the only way the existing gate can tell
them apart.

WHY ONE-SHOTS REFUSE IN AN UNATTENDED TURN. A due follow-up runs as an agent
turn with nobody watching. If that turn could call the ungated ``follow_up``
again, a prompt-injected page could make it re-schedule itself forever -- an
unapproved repeating job. While :attr:`FollowupStore.unattended` is set (serve
sets it for the turns ``fire_due`` starts), ``remind_me`` and ``follow_up``
refuse; ``follow_up_recurring`` still pauses for the owner's yes.

``when`` accepts ``in 5 minutes`` / ``5m`` / ``2 hours`` / ``in 3 days``, an ISO
date-time (``2026-09-30T09:00``), ``tomorrow [at] 9:00``, or a unix timestamp.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import home_dir

FOLLOWUPS_NAME = "followups.json"

#: Words that make a one-shot request a repeating one.
REPEAT_RE = re.compile(
    r"\b(every|each|weekly|daily|hourly|recurring|repeat(?:ing|edly)?)\b", re.I)

def guess_recurring(text: str) -> str:
    """hourly / daily / weekly from plain words; weekly when it names a weekday."""
    low = (text or "").lower()
    if re.search(r"\bhour(ly)?\b", low):
        return "hourly"
    if re.search(r"\b(daily|every ?day|each day|every (morning|night|evening))\b", low):
        return "daily"
    return "weekly"


#: Seconds between runs of a recurring row.
RECURRING: Dict[str, int] = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 7 * 86400,
}

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 7 * 86400, "week": 7 * 86400, "weeks": 7 * 86400,
}
_REL_RE = re.compile(r"^(?:in\s+)?(\d+(?:\.\d+)?)\s*([a-z]+)$")
_TOMORROW_RE = re.compile(r"^tomorrow(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$")


def followups_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / FOLLOWUPS_NAME


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
#: "tuesday 9:00", "every tuesday at 9am", "next friday", "9am", "at 17:30".
_WEEKDAY_RE = re.compile(
    r"^(?:every |each |next |on )?(?:(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*)?"
    r"\s*(?:at\s*)?(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$")


def _weekday_or_clock(text: str, now: float) -> Optional[float]:
    """The next matching weekday and/or clock time, local time; ``None`` if not one."""
    text = re.sub(r"\s+", " ", text.replace(",", " ")).strip()
    m = _WEEKDAY_RE.match(text)
    if not m or not (m.group(1) or m.group(2)):
        return None
    base = datetime.fromtimestamp(now)
    hour, minute = 9, 0
    if m.group(2):
        hour, minute = int(m.group(2)), int(m.group(3) or 0)
        if hour > 23 or minute > 59:
            return None
        if m.group(4) == "pm" and hour < 12:
            hour += 12
        if m.group(4) == "am" and hour == 12:
            hour = 0
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m.group(1):
        want = next(i for i, d in enumerate(_WEEKDAYS) if d.startswith(m.group(1)[:3]))
        days = (want - base.weekday()) % 7
        target = target + timedelta(days=days)
        if target.timestamp() <= now:
            target += timedelta(days=7)
    elif target.timestamp() <= now:
        target += timedelta(days=1)
    return target.timestamp()


def parse_when(when: Any, now: Optional[float] = None) -> float:
    """``when`` as a unix timestamp. Raises ValueError with a usable message."""
    now = time.time() if now is None else now
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        return float(when)
    text = str(when or "").strip().lower()
    if not text:
        raise ValueError("`when` is empty")
    if text == "now":
        return now
    if re.fullmatch(r"\d{9,11}(?:\.\d+)?", text):
        return float(text)
    m = _REL_RE.match(text)
    if m and m.group(2) in _UNITS:
        return now + float(m.group(1)) * _UNITS[m.group(2)]
    m = _TOMORROW_RE.match(text)
    if m:
        base = datetime.fromtimestamp(now) + timedelta(days=1)
        if m.group(1):
            hour = int(m.group(1)) % 24
            if m.group(3) == "pm" and hour < 12:
                hour += 12
            if m.group(3) == "am" and hour == 12:
                hour = 0
            base = base.replace(hour=hour, minute=int(m.group(2) or 0),
                                second=0, microsecond=0)
        return base.timestamp()
    ts = _weekday_or_clock(text, now)
    if ts is not None:
        return ts
    try:
        # 3.10's fromisoformat has no "Z"; treat it as UTC explicitly.
        iso = text.upper().replace("Z", "+00:00") if text.endswith("z") else text
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    raise ValueError(f"could not read {when!r} as a time -- use 'in 10 minutes', "
                     "'2 hours', 'tomorrow 9:00' or an ISO date-time")


class FollowupStore:
    """``followups.json``: a list of rows, rewritten atomically under a lock.

    Row: ``{id, kind: remind|follow_up, text, when_ts, recurring, status:
    pending|done|cancelled, created_at, fired_at, fired_count}``.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else followups_path()
        self._lock = threading.RLock()
        #: True while an unattended (follow-up) turn runs: one-shots refuse then.
        self.unattended = False

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            # A corrupt file must not be silently replaced with an empty list:
            # that would drop every promise the agent made.
            raise RuntimeError(f"{self.path} is unreadable ({exc}); fix or move it") from exc
        rows = data.get("followups", []) if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict)]

    def _save(self, rows: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"followups": rows}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load()

    def add(self, kind: str, text: str, when_ts: float, recurring: str = "") -> Dict[str, Any]:
        recurring = (recurring or "").strip().lower()
        if recurring and recurring not in RECURRING:
            raise ValueError(f"recurring must be one of {', '.join(RECURRING)}")
        row = {"id": secrets.token_hex(3), "kind": kind, "text": str(text),
               "when_ts": float(when_ts), "recurring": recurring, "status": "pending",
               "created_at": time.time(), "fired_at": 0.0, "fired_count": 0}
        with self._lock:
            rows = self._load()
            rows.append(row)
            self._save(rows)
        return row

    def cancel(self, row_id: str) -> bool:
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("id") == row_id and r.get("status") == "pending":
                    r["status"] = "cancelled"
                    self._save(rows)
                    return True
        return False

    def claim_due(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Mark every due row as fired and SAVE, then return the claimed copies.

        At-most-once: the write happens before the caller sends anything, so a
        crash between claim and send loses that one DM rather than repeating it
        on every restart. A recurring row moves its ``when_ts`` past ``now``
        (missed runs collapse into one) and stays pending.
        """
        now = time.time() if now is None else now
        claimed: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._load()
            for r in rows:
                if r.get("status") != "pending" or float(r.get("when_ts") or 0) > now:
                    continue
                r["fired_at"] = now
                r["fired_count"] = int(r.get("fired_count") or 0) + 1
                step = RECURRING.get(str(r.get("recurring") or ""))
                if step:
                    nxt = float(r["when_ts"])
                    while nxt <= now:
                        nxt += step
                    r["when_ts"] = nxt
                else:
                    r["status"] = "done"
                claimed.append(dict(r))
            if claimed:
                self._save(rows)
        return claimed


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def build_life_tools(store: FollowupStore,
                     receipts_path: Optional[Path] = None) -> List[Callable[..., Any]]:
    """The life tools, bound to one store. Names are the tool names."""

    def _unattended_refusal() -> Optional[str]:
        if not store.unattended:
            return None
        return json.dumps({"error": "a follow-up turn cannot schedule another reminder or "
                                    "follow-up by itself; tell the owner and let them ask"})

    def _repeat_refusal(when: str, text: str) -> Optional[str]:
        """A one-shot tool asked for a repeat: point at the tool that asks the owner.

        Measured live: asked for "a weekly reminder every tuesday", a small model
        picked remind_me, so the owner's yes (which only follow_up_recurring asks
        for) never happened.
        """
        if REPEAT_RE.search(f"{when} {text}"):
            return json.dumps({
                "error": "this repeats -- call follow_up_recurring (recurring: hourly, "
                         "daily or weekly); it asks the owner first",
                # Hearth turns this into the owner's yes/no card itself, so a small
                # model that never calls follow_up_recurring still gets it right.
                "suggest": {"tool": "follow_up_recurring",
                            "args": {"when": when, "text": text,
                                     "recurring": guess_recurring(f"{when} {text}")}}})
        return None

    def remind_me(when: str, text: str) -> str:
        """Schedule a reminder DM to the owner.

        when: when to remind, e.g. 'in 10 minutes', '2 hours', 'tomorrow 9:00'
        text: what to remind the owner about
        """
        refused = _unattended_refusal() or _repeat_refusal(when, text)
        if refused:
            return refused
        try:
            row = store.add("remind", text, parse_when(when))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"ok": True, "id": row["id"], "at": _fmt(row["when_ts"])})

    def follow_up(when: str, text: str) -> str:
        """Schedule ONE follow-up: at `when` you get a turn with `text` and DM the owner.

        when: when to follow up, e.g. 'in 1 hour', 'tomorrow 9:00'
        text: what to do or check at that time
        """
        refused = _unattended_refusal() or _repeat_refusal(when, text)
        if refused:
            return refused
        try:
            row = store.add("follow_up", text, parse_when(when))
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"ok": True, "id": row["id"], "at": _fmt(row["when_ts"])})

    def follow_up_recurring(when: str, text: str, recurring: str) -> str:
        """Schedule a REPEATING follow-up (needs the owner's yes).

        when: the first run, e.g. 'tomorrow 9:00'
        text: what to do or check each time
        recurring: hourly, daily or weekly
        """
        try:
            row = store.add("follow_up", text, parse_when(when), recurring=recurring)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"ok": True, "id": row["id"], "first": _fmt(row["when_ts"]),
                           "recurring": row["recurring"]})

    def list_followups() -> str:
        """List the reminders and follow-ups that are still scheduled."""
        rows = [r for r in store.rows() if r.get("status") == "pending"]
        return json.dumps([{"id": r["id"], "kind": r.get("kind"), "text": r.get("text"),
                            "at": _fmt(float(r.get("when_ts") or 0)),
                            "recurring": r.get("recurring") or ""} for r in rows])

    def cancel_followup(id: str) -> str:  # noqa: A002 - the tool's public argument name
        """Cancel a scheduled reminder or follow-up by its id.

        id: the id shown by list_followups
        """
        ok = store.cancel(str(id))
        return json.dumps({"ok": ok} if ok else {"error": f"no pending follow-up {id!r}"})

    def receipts(n: int = 10) -> str:
        """The last n signed receipts: what you actually did (tool calls, DMs sent).

        n: how many recent receipts to return (default 10)
        """
        from adk import receipts as rc

        rows = rc.tail(max(1, min(int(n or 10), 50)), path=receipts_path)
        return json.dumps([{"seq": r.get("seq"), "ts": r.get("ts"),
                            "kind": r.get("kind"), "name": r.get("name"),
                            "args": r.get("args_preview"), "result": r.get("result_preview"),
                            "approval": r.get("approval"), "signed": r.get("signed")}
                           for r in rows])

    return [remind_me, follow_up, follow_up_recurring, list_followups,
            cancel_followup, receipts]


#: Tool names that pause for the owner's numbered yes. ``relay_*`` senders are
#: listed even though serve never registers the relay category: if a pack or an
#: env toolpack ever adds them, they arrive already gated.
#: ``calendar_add`` / ``mail_send`` / ``todo_add`` are the Hearth connector tools
#: that act on the owner's real accounts (:mod:`adk.home.connector_tools`); every
#: connector tool that is not read-only is here (a test asserts the subset).
#: ``tutor_assign`` / ``tutor_set_focus`` change a child's learning plan
#: (:mod:`adk.home.tutor_tools`).
ALWAYS_ASK = ("follow_up_recurring", "relay_send", "relay_reply_in_thread",
              "send_relay_message", "send_email", "send_user_email",
              "calendar_add", "mail_send", "todo_add", "tutor_assign", "tutor_set_focus")
