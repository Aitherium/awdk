"""The home's own calendar, to-do list and mailbox link -- no account needed.

A fresh Hearth answers "what's on my calendar?" from minute one:

* **Built-in calendar + to-do list.** ``<home>/calendar.json`` holds the owner's
  events and to-dos. Nothing leaves the machine.
* **Subscribed calendars, read-only.** ``adk home connect calendar --ics <url>``
  subscribes to the ICS feed every provider hands out without an OAuth app
  (Google "secret address in iCal format", Outlook "publish calendar", an iCloud
  public calendar), or to a CalDAV collection with an app password. Feeds are
  cached under ``<home>/calendar-cache/`` and re-read when older than
  :data:`REFRESH_S`; a failed refresh keeps the last good copy and says so.
* **Mail by app password.** ``adk home connect mail`` records an IMAP/SMTP account
  (the same servers :mod:`adk.home.transports.mail` speaks to).

Subscription addresses are bearer secrets (whoever holds a Google secret address
reads that calendar), so ``<home>/connections.json`` is written owner-only and
every listing masks the address. A password comes from the environment
(``HEARTH_MAIL_PASSWORD`` / ``HEARTH_CALDAV_PASSWORD``), then the OS keychain,
then the owner-only ``connections.json`` -- it is never printed, logged or put in
a tool result.

Only the OWNER subscribes or connects (the CLI): no agent tool takes a URL, so a
crafted email cannot make the model point the calendar reader at an address.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import json
import logging
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import ics
from .config import HomeError, home_dir

logger = logging.getLogger("adk.home.planner")

CALENDAR_NAME = "calendar.json"
CONNECTIONS_NAME = "connections.json"
CACHE_DIR = "calendar-cache"
#: A subscribed feed is read again when its cached copy is older than this.
REFRESH_S = 15 * 60.0
HTTP_TIMEOUT_S = 20.0
#: Wall-clock limit for one whole feed download (the httpx timeout is per read).
FEED_DEADLINE_S = 60.0
#: Days an agenda or an event may name: outside it the OS cannot convert the time.
MIN_YEAR, MAX_YEAR = 1971, 9000
MAX_FEED_BYTES = 10 * 1024 * 1024
#: The longest span one agenda call covers.
MAX_RANGE_DAYS = 62
#: Bytes of each unread message read for its headers and preview.
PREVIEW_BYTES = 65536
MAIL_PASSWORD_ENV = "HEARTH_MAIL_PASSWORD"
CALDAV_PASSWORD_ENV = "HEARTH_CALDAV_PASSWORD"
KEYCHAIN_SERVICE = "aither_adk"
LOCAL_SOURCE = "built-in"

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: IMAP/SMTP servers by mail domain: (imap host, smtp host, smtp port, app-password help).
MAIL_PRESETS: Dict[str, Tuple[str, str, int, str]] = {
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com", 587,
                  "create one at https://myaccount.google.com/apppasswords "
                  "(needs 2-Step Verification)"),
    "googlemail.com": ("imap.gmail.com", "smtp.gmail.com", 587,
                       "create one at https://myaccount.google.com/apppasswords"),
    "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com", 587,
                   "create one at https://account.apple.com (Sign-In and Security > "
                   "App-Specific Passwords)"),
    "me.com": ("imap.mail.me.com", "smtp.mail.me.com", 587,
               "create one at https://account.apple.com"),
    "mac.com": ("imap.mail.me.com", "smtp.mail.me.com", 587,
                "create one at https://account.apple.com"),
    "yahoo.com": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", 465,
                  "create one under Yahoo Account Security > Generate app password"),
    "fastmail.com": ("imap.fastmail.com", "smtp.fastmail.com", 465,
                     "create one under Fastmail Settings > Privacy & Security > "
                     "App passwords"),
    "outlook.com": ("outlook.office365.com", "smtp-mail.outlook.com", 587,
                    "Outlook.com no longer accepts passwords over IMAP; forward that "
                    "mailbox to one that does, or use a work account your admin allows"),
    "hotmail.com": ("outlook.office365.com", "smtp-mail.outlook.com", 587,
                    "Outlook.com no longer accepts passwords over IMAP"),
    "live.com": ("outlook.office365.com", "smtp-mail.outlook.com", 587,
                 "Outlook.com no longer accepts passwords over IMAP"),
}


class PlannerError(Exception):
    """A tool- and CLI-facing failure: ``str(exc)`` is safe to show (no secret)."""


# ── time helpers ──────────────────────────────────────────────────────────────

def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day).astimezone()


def _check_year(day: Any, what: Any) -> None:
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ValueError(f"{str(what)[:40]!r} is outside the years this calendar handles "
                         f"({MIN_YEAR}-{MAX_YEAR})")


def parse_range(text: Any, today: Optional[date] = None) -> Tuple[date, date, str]:
    """``(first day, day after the last, label)`` for an agenda request; raises
    ValueError (never OverflowError / OSError) for a day the machine cannot place."""
    try:
        first, after_last, label = _parse_range(text, today)
        _check_year(first, text)
        _check_year(after_last, text)
    except OverflowError:
        raise ValueError(f"could not read {str(text)[:40]!r} as a day or range") from None
    return first, after_last, label


def _parse_range(text: Any, today: Optional[date] = None) -> Tuple[date, date, str]:
    """``(first day, day after the last, label)`` for an agenda request.

    One day: ``today`` / ``tomorrow`` / ``yesterday`` / a weekday / ``YYYY-MM-DD``.
    A span: ``this week`` (today through Sunday), ``next week`` (Monday-Sunday),
    ``weekend``, ``next N days``, ``this month``, or ``YYYY-MM-DD..YYYY-MM-DD``.
    """
    from .connector_tools import parse_day

    today = today or datetime.now().date()
    raw = str(text or "").strip().lower().replace("_", " ")
    raw = re.sub(r"\s+", " ", re.sub(r"^(the|for|on) ", "", raw)).rstrip("?.! ")
    one = timedelta(days=1)
    if raw in ("week", "this week", "the week", "rest of the week", "rest of this week",
               "upcoming", "coming week"):
        return today, today + timedelta(days=7 - today.weekday()), "this week"
    if raw in ("next week", "the next week"):
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=7), "next week"
    if raw in ("weekend", "this weekend", "the weekend"):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:
            return today, today + one, "this weekend"
        return saturday, saturday + timedelta(days=2), "this weekend"
    if raw in ("month", "this month"):
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return today, nxt, "this month"
    m = re.fullmatch(r"(?:next |in the next )?(\d{1,2}) days?", raw)
    if m:
        days = max(1, min(int(m.group(1)), MAX_RANGE_DAYS))
        return today, today + timedelta(days=days), f"next {days} days"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|to|/)\s*(\d{4}-\d{2}-\d{2})", raw)
    if m:
        first, last = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        if last < first:
            raise ValueError("the range ends before it starts")
        if (last - first).days >= MAX_RANGE_DAYS:
            raise ValueError(f"a range covers at most {MAX_RANGE_DAYS} days")
        return first, last + one, f"{first.isoformat()} to {last.isoformat()}"
    try:
        day = parse_day(raw, today)
    except ValueError:
        raise ValueError(f"could not read {text!r} as a day or range -- use 'today', "
                         "'tomorrow', 'this week', 'next week', a weekday or "
                         "YYYY-MM-DD") from None
    return day, day + one, day.isoformat()


def parse_start(when: Any, now: Optional[float] = None) -> Tuple[Any, bool]:
    """An event start: ``(date, True)`` for a bare ``YYYY-MM-DD``, else
    ``(aware local datetime, False)`` through :func:`life_tools.parse_when`.
    Raises ValueError only (a year the OS cannot convert is refused, not a crash)."""
    try:
        start, all_day = _parse_start(when, now)
    except (OverflowError, OSError):
        raise ValueError(f"could not read {str(when)[:40]!r} as a time") from None
    _check_year(start, when)
    return start, all_day


def _parse_start(when: Any, now: Optional[float] = None) -> Tuple[Any, bool]:
    from .life_tools import parse_when

    text = str(when or "").strip()
    if _DATE_ONLY_RE.match(text):
        return date.fromisoformat(text), True
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ t]+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
                 text.lower())
    if m:
        hour, minute = int(m.group(2)), int(m.group(3) or 0)
        if m.group(4) == "pm" and hour < 12:
            hour += 12
        if m.group(4) == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            raise ValueError(f"could not read {when!r} as a time")
        day = date.fromisoformat(m.group(1))
        _check_year(day, when)
        return datetime(day.year, day.month, day.day, hour, minute).astimezone(), False
    return datetime.fromtimestamp(parse_when(when, now)).astimezone(), False


def mask_url(url: str) -> str:
    """Host plus the last four characters: enough to recognise, not to read."""
    parts = urlsplit(url)
    tail = url[-4:] if len(url) > 12 else ""
    return f"{parts.scheme}://{parts.hostname or '?'}/...{tail}"


def normalize_feed_url(url: str) -> str:
    """``webcal://`` -> ``https://``; refuse anything that is not an https feed."""
    text = str(url or "").strip()
    if not text:
        raise PlannerError("give the calendar's ICS address (it starts with https:// or "
                           "webcal://)")
    if text.lower().startswith("webcal://"):
        text = "https://" + text[len("webcal://"):]
    elif text.lower().startswith("webcals://"):
        text = "https://" + text[len("webcals://"):]
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("https", "http") or not host:
        raise PlannerError(f"{text[:40]!r} is not a calendar address -- it must start "
                           "with https:// or webcal://")
    if parts.scheme == "http" and not loopback:
        raise PlannerError("that address is plain http; a calendar address is a secret, "
                           "so only https:// (or webcal://) is accepted")
    return text


# ── fetching (module-level so a test can replace them) ────────────────────────

def http_get_feed(url: str) -> str:
    """One ICS feed as text. Errors name the host and status, never the address."""
    import httpx

    host = urlsplit(url).hostname or "the calendar server"
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
            status, body = _read_capped(client, "GET", url, host, headers={
                "Accept": "text/calendar, */*;q=0.5",
                "User-Agent": "aither-hearth-calendar"})
    except httpx.HTTPError as exc:
        raise PlannerError(f"could not reach {host} ({type(exc).__name__})") from None
    if status in (401, 403, 404):
        raise PlannerError(f"{host} answered HTTP {status}: the address is wrong "
                           "or was reset -- copy the calendar's ICS link again")
    if status >= 400:
        raise PlannerError(f"{host} answered HTTP {status}")
    return body.decode("utf-8", errors="replace")


def _read_capped(client: Any, method: str, url: str, host: str, **kw: Any
                 ) -> Tuple[int, bytes]:
    """``(status, body)`` read as a stream: stops at :data:`MAX_FEED_BYTES` and at
    :data:`FEED_DEADLINE_S`, so an endless or slow-drip feed cannot hold the home."""
    deadline = time.monotonic() + FEED_DEADLINE_S
    chunks: List[bytes] = []
    size = 0
    with client.stream(method, url, **kw) as resp:
        if resp.status_code >= 400:
            return resp.status_code, b""
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise PlannerError(f"the calendar at {host} is larger than "
                                   f"{MAX_FEED_BYTES // (1024 * 1024)} MB")
            if time.monotonic() > deadline:
                raise PlannerError(f"the calendar at {host} took longer than "
                                   f"{int(FEED_DEADLINE_S)} s to download")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks)


_CALDAV_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{start}" end="{end}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""


def caldav_get_feed(url: str, username: str, password: str) -> str:
    """Every event of one CalDAV collection (a year back, two ahead) as ONE calendar."""
    import xml.etree.ElementTree as ET

    import httpx

    host = urlsplit(url).hostname or "the CalDAV server"
    now = datetime.now(timezone.utc)
    body = _CALDAV_REPORT.format(start=(now - timedelta(days=365)).strftime("%Y%m%dT000000Z"),
                                 end=(now + timedelta(days=730)).strftime("%Y%m%dT000000Z"))
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True,
                          auth=(username, password)) as client:
            status, content = _read_capped(
                client, "REPORT", url, host, content=body.encode("utf-8"), headers={
                    "Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    except httpx.HTTPError as exc:
        raise PlannerError(f"could not reach {host} ({type(exc).__name__})") from None
    if status in (401, 403):
        raise PlannerError(f"{host} refused the sign-in (HTTP {status}) -- use an "
                           "app password, not your normal password")
    if status == 404:
        raise PlannerError(f"{host} has no calendar at that address (HTTP 404) -- give the "
                           "calendar collection's own URL")
    if status >= 400:
        raise PlannerError(f"{host} answered HTTP {status}")
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise PlannerError(f"{host} did not answer with CalDAV XML -- is that a calendar "
                           "collection URL?") from None
    inner: List[str] = []
    for node in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
        lines = ics.unfold(node.text or "")
        keep, depth = [], 0
        for ln in lines:
            upper = ln.strip().upper()
            if upper in ("BEGIN:VEVENT", "BEGIN:VTIMEZONE"):
                depth += 1
            if depth:
                keep.append(ln)
            if upper in ("END:VEVENT", "END:VTIMEZONE") and depth:
                depth -= 1
        inner.extend(keep)
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *inner, "END:VCALENDAR"]) + "\r\n"


# ── secrets ───────────────────────────────────────────────────────────────────

def _keychain_get(key: str) -> str:
    try:
        from adk.core.secrets import KeyringStore

        return KeyringStore(KEYCHAIN_SERVICE).get(key) or ""
    except Exception:  # noqa: BLE001 - no keyring package, no entry: just unset
        return ""


def _keychain_set(key: str, value: str) -> bool:
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.set_password(KEYCHAIN_SERVICE, key, value)
        return keyring.get_password(KEYCHAIN_SERVICE, key) == value
    except Exception:  # noqa: BLE001 - no keyring package or no backend
        return False


def _keychain_delete(key: str) -> None:
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.delete_password(KEYCHAIN_SERVICE, key)
    except Exception as exc:  # noqa: BLE001 - no keyring package, or no such entry
        logger.debug("hearth planner: no keychain entry removed (%s)", type(exc).__name__)


# ── the store ─────────────────────────────────────────────────────────────────

class Planner:
    """``calendar.json`` (events, to-dos) + ``connections.json`` (feeds, mail)."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else home_dir()
        self.path = self.root / CALENDAR_NAME
        self.connections_path = self.root / CONNECTIONS_NAME
        self.cache_dir = self.root / CACHE_DIR
        self._lock = threading.RLock()

    # ── files ────────────────────────────────────────────────────────────────
    def _read(self, path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Never replace an unreadable file with an empty one: that would drop
            # the owner's events.
            raise HomeError(f"{path} is unreadable ({exc}); fix or move it") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, path: Path, data: Dict[str, Any], private: bool = False) -> None:
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if private:
            from adk._private_file import write_private_text

            write_private_text(path, text)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _calendar(self) -> Dict[str, Any]:
        data = self._read(self.path)
        data.setdefault("events", [])
        data.setdefault("todos", [])
        return data

    def _connections(self) -> Dict[str, Any]:
        data = self._read(self.connections_path)
        data.setdefault("subscriptions", [])
        data.setdefault("secrets", {})
        return data

    # ── local events ─────────────────────────────────────────────────────────
    def add_event(self, start: Any, title: str, minutes: int = 30, all_day: bool = False,
                  location: str = "") -> Dict[str, Any]:
        title = " ".join(str(title or "").split())
        if not title:
            raise PlannerError("`title` is empty")
        minutes = int(minutes or 30)
        if not 1 <= minutes <= 24 * 60:
            raise PlannerError("duration_min must be between 1 and 1440")
        row: Dict[str, Any] = {"id": "ev-" + secrets.token_hex(3), "title": title[:200],
                               "location": " ".join(str(location or "").split())[:200],
                               "created_at": time.time()}
        row.update(self._start_fields(start, all_day, minutes))
        with self._lock:
            data = self._calendar()
            data["events"].append(row)
            self._write(self.path, data)
        return row

    @staticmethod
    def _start_fields(start: Any, all_day: bool, minutes: int) -> Dict[str, Any]:
        if all_day or not isinstance(start, datetime):
            day = start.date() if isinstance(start, datetime) else start
            return {"all_day": True, "date": day.isoformat()}
        return {"all_day": False, "start": start.astimezone().isoformat(timespec="minutes"),
                "minutes": minutes}

    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._calendar()["events"] if isinstance(e, dict)]

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        wanted = str(event_id or "").strip().lower()
        return next((e for e in self.events() if str(e.get("id", "")).lower() == wanted), None)

    def move_event(self, event_id: str, start: Any, all_day: bool = False,
                   minutes: int = 0) -> Dict[str, Any]:
        wanted = str(event_id or "").strip().lower()
        with self._lock:
            data = self._calendar()
            for row in data["events"]:
                if str(row.get("id", "")).lower() == wanted:
                    keep = int(minutes or row.get("minutes") or 30)
                    for key in ("all_day", "date", "start", "minutes"):
                        row.pop(key, None)
                    row.update(self._start_fields(start, all_day, keep))
                    self._write(self.path, data)
                    return dict(row)
        raise PlannerError(self._no_event(event_id))

    def delete_event(self, event_id: str) -> Dict[str, Any]:
        wanted = str(event_id or "").strip().lower()
        with self._lock:
            data = self._calendar()
            for i, row in enumerate(data["events"]):
                if str(row.get("id", "")).lower() == wanted:
                    del data["events"][i]
                    self._write(self.path, data)
                    return row
        raise PlannerError(self._no_event(event_id))

    @staticmethod
    def _no_event(event_id: str) -> str:
        return (f"no built-in calendar event has the id {str(event_id)[:40]!r} -- ids come "
                "from calendar_agenda; events from a subscribed calendar are read-only "
                "and are changed in that calendar")

    def _local_occurrences(self, first: date, after_last: date) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        win_start, win_end = _midnight(first), _midnight(after_last)
        for row in self.events():
            try:
                if row.get("all_day"):
                    day = date.fromisoformat(str(row.get("date")))
                    if first <= day < after_last:
                        out.append({"id": row["id"], "title": row.get("title", ""),
                                    "location": row.get("location", ""), "all_day": True,
                                    "start": day, "end": day + timedelta(days=1),
                                    "calendar": LOCAL_SOURCE})
                    continue
                start = datetime.fromisoformat(str(row.get("start"))).astimezone()
                end = start + timedelta(minutes=int(row.get("minutes") or 30))
            except (TypeError, ValueError):
                continue
            if start < win_end and end > win_start:
                out.append({"id": row["id"], "title": row.get("title", ""),
                            "location": row.get("location", ""), "all_day": False,
                            "start": start, "end": end, "calendar": LOCAL_SOURCE})
        return out

    # ── subscriptions ────────────────────────────────────────────────────────
    def subscriptions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s for s in self._connections()["subscriptions"] if isinstance(s, dict)]

    def _cache_path(self, sub_id: str) -> Path:
        return self.cache_dir / f"{sub_id}.ics"

    def _caldav_password(self, sub: Dict[str, Any], conn: Dict[str, Any]) -> str:
        key = f"caldav:{sub.get('id')}"
        return ((os.environ.get(CALDAV_PASSWORD_ENV) or "").strip()
                or _keychain_get(key) or str(conn["secrets"].get(key) or ""))

    def _fetch(self, sub: Dict[str, Any], conn: Dict[str, Any], password: str = "") -> str:
        if sub.get("kind") == "caldav":
            pw = password or self._caldav_password(sub, conn)
            if not pw:
                raise PlannerError(f"no app password for {sub.get('name')}: set "
                                   f"{CALDAV_PASSWORD_ENV} or connect it again")
            return caldav_get_feed(sub["url"], str(sub.get("username") or ""), pw)
        return http_get_feed(sub["url"])

    def subscribe(self, url: str, name: str = "", kind: str = "ics", username: str = "",
                  password: str = "") -> Dict[str, Any]:
        """Fetch + parse the feed FIRST; only a calendar that reads is saved."""
        from adk._private_file import write_private_text

        url = normalize_feed_url(url)
        kind = "caldav" if kind == "caldav" else "ics"
        if kind == "caldav" and not (username and password):
            raise PlannerError("CalDAV needs --user and an app password")
        with self._lock:
            conn = self._connections()
            for old in conn["subscriptions"]:
                if old.get("url") == url:
                    raise PlannerError(f"already subscribed as {old.get('name')!r} "
                                       f"(id {old.get('id')})")
            sub: Dict[str, Any] = {"id": "cal-" + secrets.token_hex(3), "kind": kind,
                                   "url": url, "username": username,
                                   "name": "", "added_at": time.time()}
            text = self._fetch(sub, conn, password)
            try:
                parsed = ics.parse_ics(text)
            except ics.IcsError as exc:
                raise PlannerError(str(exc)) from None
            taken = {str(s.get("name", "")).lower() for s in conn["subscriptions"]}
            base = (" ".join(str(name or "").split()) or parsed.get("name")
                    or urlsplit(url).hostname or "calendar")[:60]
            label, n = base, 2
            while label.lower() in taken or label.lower() == LOCAL_SOURCE:
                label, n = f"{base} {n}", n + 1
            sub.update({"name": label, "last_ok": time.time(), "last_error": "",
                        "events": len(parsed["events"])})
            write_private_text(self._cache_path(sub["id"]), text)
            if kind == "caldav":
                key = f"caldav:{sub['id']}"
                if not _keychain_set(key, password):
                    conn["secrets"][key] = password
            conn["subscriptions"].append(sub)
            self._write(self.connections_path, conn, private=True)
        return self.public_subscription(sub)

    def unsubscribe(self, ref: str) -> Dict[str, Any]:
        wanted = str(ref or "").strip().lower()
        with self._lock:
            conn = self._connections()
            for i, sub in enumerate(conn["subscriptions"]):
                if wanted in (str(sub.get("id", "")).lower(), str(sub.get("name", "")).lower()):
                    del conn["subscriptions"][i]
                    conn["secrets"].pop(f"caldav:{sub.get('id')}", None)
                    self._write(self.connections_path, conn, private=True)
                    if sub.get("kind") == "caldav":
                        _keychain_delete(f"caldav:{sub.get('id')}")
                    try:
                        self._cache_path(str(sub.get("id"))).unlink()
                    except OSError as exc:      # the subscription is gone either way
                        logger.debug("hearth calendar: cached copy not removed: %s", exc)
                    return self.public_subscription(sub)
        raise PlannerError(f"no subscribed calendar {str(ref)[:40]!r} -- `adk home calendar "
                           "subscriptions` lists them")

    @staticmethod
    def public_subscription(sub: Dict[str, Any]) -> Dict[str, Any]:
        """A subscription without its address (masked) -- what a listing may show."""
        return {"id": sub.get("id"), "name": sub.get("name"), "kind": sub.get("kind"),
                "address": mask_url(str(sub.get("url") or "")),
                "events": sub.get("events", 0),
                "last_refreshed": (datetime.fromtimestamp(float(sub["last_ok"]))
                                   .strftime("%Y-%m-%d %H:%M") if sub.get("last_ok") else ""),
                "last_error": sub.get("last_error") or ""}

    def refresh(self, force: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Re-read every feed whose copy is older than :data:`REFRESH_S` (all with
        ``force``). A failure keeps the cached copy and records ``last_error``."""
        from adk._private_file import write_private_text

        now = time.time() if now is None else now
        with self._lock:
            conn = self._connections()
            due = [dict(sub) for sub in conn["subscriptions"]
                   if force or now - max(float(sub.get("last_ok") or 0),
                                         float(sub.get("last_try") or 0)) >= REFRESH_S]
        # Fetch WITHOUT the lock and without holding the file's content: a download
        # takes seconds, and a connect / unsubscribe made meanwhile (this process or
        # another -- serve builds a Planner per tick) must not be written over.
        status: Dict[str, Dict[str, Any]] = {}
        for sub in due:
            row: Dict[str, Any] = {"last_try": now}
            try:
                text = self._fetch(sub, conn)
                parsed = ics.parse_ics(text)
                write_private_text(self._cache_path(str(sub["id"])), text)
            except (PlannerError, ics.IcsError) as exc:
                row["last_error"] = str(exc)[:200]
            except Exception as exc:  # noqa: BLE001 - a bad URL / disk error is one feed's
                row["last_error"] = f"refresh failed ({type(exc).__name__})"
            else:
                row.update({"last_ok": now, "last_error": "", "events": len(parsed["events"])})
            if row.get("last_error"):
                logger.warning("hearth calendar: refresh of %s failed: %s",
                               sub.get("id"), row["last_error"])
            status[str(sub["id"])] = row
        out: List[Dict[str, Any]] = []
        if not status:
            return out
        with self._lock:
            conn = self._connections()          # re-read: merge ONLY the status fields
            live = set()
            for sub in conn["subscriptions"]:
                live.add(str(sub.get("id")))
                row = status.get(str(sub.get("id")))
                if row:
                    sub.update(row)
                    out.append(self.public_subscription(sub))
            self._write(self.connections_path, conn, private=True)
        for gone in set(status) - live:         # unsubscribed while it was downloading
            try:
                self._cache_path(gone).unlink()
            except OSError as exc:
                logger.debug("hearth calendar: cached copy not removed: %s", exc)
        return out

    def _subscribed_occurrences(self, first: date, after_last: date
                                ) -> Tuple[List[Dict[str, Any]], List[str]]:
        out: List[Dict[str, Any]] = []
        warnings: List[str] = []
        win_start, win_end = _midnight(first), _midnight(after_last)
        for sub in self.subscriptions():
            name = str(sub.get("name") or "calendar")
            if sub.get("last_error"):
                since = (datetime.fromtimestamp(float(sub["last_ok"])).strftime("%Y-%m-%d %H:%M")
                         if sub.get("last_ok") else "never")
                warnings.append(f"{name} could not be refreshed ({sub['last_error']}); "
                                f"showing the copy from {since}")
            try:
                parsed = ics.parse_ics(self._cache_path(str(sub.get("id")))
                                       .read_text(encoding="utf-8"))
            except (OSError, ics.IcsError):
                warnings.append(f"{name} has no readable copy yet -- run `adk home calendar "
                                "refresh`")
                continue
            if parsed.get("skipped_rules"):
                warnings.append(f"{name}: {parsed['skipped_rules']} repeating event(s) use a "
                                "rule this reader does not expand; only their first date "
                                "is shown")
            stats: Dict[str, int] = {}
            for occ in ics.expand(parsed, win_start, win_end, stats):
                occ["calendar"] = name
                occ["id"] = ""
                out.append(occ)
            if stats.get("exhausted"):
                warnings.append(f"{name} has more repeating events than this reader "
                                "expands in one go; some may be missing")
        return out, warnings

    def agenda(self, first: date, after_last: date, refresh: bool = True
               ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Local + subscribed events in ``[first, after_last)`` as display rows."""
        problems: List[str] = []
        if refresh and self.subscriptions():
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - never hides the built-in calendar
                logger.warning("hearth calendar: refresh failed: %s", type(exc).__name__)
                problems.append(f"subscribed calendars could not be refreshed "
                                f"({type(exc).__name__}); showing the saved copies")
        subscribed, warnings = self._subscribed_occurrences(first, after_last)
        warnings = problems + warnings
        rows = sorted(self._local_occurrences(first, after_last) + subscribed,
                      key=ics._sort_key)
        shown = [row for o in rows for row in display_rows(o, first, after_last)]
        shown.sort(key=lambda r: r["day"])          # stable: order within a day is kept
        return shown, warnings

    # ── to-dos ───────────────────────────────────────────────────────────────
    def add_todo(self, text: str, due: str = "") -> Dict[str, Any]:
        text = " ".join(str(text or "").split())
        if not text:
            raise PlannerError("`text` is empty")
        due = str(due or "").strip()
        if due:
            try:
                due = parse_range(due)[0].isoformat()
            except ValueError as exc:
                raise PlannerError(str(exc)) from None
        row = {"id": "td-" + secrets.token_hex(3), "text": text[:300], "due": due,
               "done": False, "created_at": time.time(), "done_at": 0.0}
        with self._lock:
            data = self._calendar()
            data["todos"].append(row)
            self._write(self.path, data)
        return row

    def todos(self, include_done: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [t for t in self._calendar()["todos"] if isinstance(t, dict)]
        return rows if include_done else [t for t in rows if not t.get("done")]

    def _todo_change(self, todo_id: str, remove: bool) -> Dict[str, Any]:
        wanted = str(todo_id or "").strip().lower()
        with self._lock:
            data = self._calendar()
            for i, row in enumerate(data["todos"]):
                same_text = str(row.get("text", "")).lower() == wanted and not row.get("done")
                if str(row.get("id", "")).lower() == wanted or same_text:
                    if remove:
                        del data["todos"][i]
                    else:
                        row["done"], row["done_at"] = True, time.time()
                    self._write(self.path, data)
                    return dict(row)
        raise PlannerError(f"no to-do has the id {str(todo_id)[:40]!r} -- ids come from "
                           "todo_list")

    def done_todo(self, todo_id: str) -> Dict[str, Any]:
        return self._todo_change(todo_id, remove=False)

    def delete_todo(self, todo_id: str) -> Dict[str, Any]:
        return self._todo_change(todo_id, remove=True)

    # ── mail account ─────────────────────────────────────────────────────────
    def mail_account(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            acct = self._connections().get("mail")
        return dict(acct) if isinstance(acct, dict) and acct.get("user") else None

    def mail_password(self) -> str:
        env = (os.environ.get(MAIL_PASSWORD_ENV) or "").strip()
        if env:
            return env
        with self._lock:
            stored = str(self._connections()["secrets"].get("mail") or "")
        return _keychain_get(MAIL_PASSWORD_ENV) or stored

    def connect_mail(self, user: str, password: str, imap_host: str = "",
                     smtp_host: str = "", imap_port: int = 993, smtp_port: int = 0,
                     verify: bool = True) -> Dict[str, Any]:
        """Check the IMAP sign-in, THEN save the account (password: keychain, else
        the owner-only connections file)."""
        user = str(user or "").strip()
        if "@" not in user:
            raise PlannerError("give your full email address (--user you@example.com)")
        domain = user.rsplit("@", 1)[1].lower()
        preset = MAIL_PRESETS.get(domain)
        imap_host = (imap_host or (preset[0] if preset else "")).strip()
        smtp_host = (smtp_host or (preset[1] if preset else "") or imap_host).strip()
        smtp_port = int(smtp_port or (preset[2] if preset else 587))
        if not imap_host:
            raise PlannerError(f"no known mail servers for {domain} -- pass --imap-host (and "
                               "--smtp-host if it differs)")
        if not password:
            raise PlannerError("no app password given" + (f": {preset[3]}" if preset else ""))
        acct = {"user": user, "imap_host": imap_host, "imap_port": int(imap_port or 993),
                "smtp_host": smtp_host, "smtp_port": smtp_port, "from": user}
        if verify:
            conn = imap_login(acct, password)
            _quiet_logout(conn)
        with self._lock:
            data = self._connections()
            data["mail"] = acct
            if _keychain_set(MAIL_PASSWORD_ENV, password):
                data["secrets"].pop("mail", None)
                acct["password_in"] = "keychain"
            else:
                data["secrets"]["mail"] = password
                acct["password_in"] = "connections.json (owner-only)"
            self._write(self.connections_path, data, private=True)
        return dict(acct)

    def disconnect_mail(self) -> bool:
        with self._lock:
            data = self._connections()
            had = bool(data.pop("mail", None))
            data["secrets"].pop("mail", None)
            if had:
                self._write(self.connections_path, data, private=True)
                _keychain_delete(MAIL_PASSWORD_ENV)
        return had


# ── display ───────────────────────────────────────────────────────────────────

def display_rows(occ: Dict[str, Any], first: date, after_last: date
                 ) -> List[Dict[str, str]]:
    """One occurrence as the rows a tool result / the CLI shows (local time): one
    row per day it covers inside ``[first, after_last)``. A timed event that runs
    past midnight shows ``22:00-24:00``, then ``00:00-02:00`` on the next day (and
    ``all day`` on any day in between), so no day's row claims a wrong end."""
    start, end = occ["start"], occ["end"]
    if occ["all_day"]:
        first_day, last_day = start, max(start, end - timedelta(days=1))
    else:
        local_start, local_end = start.astimezone(), end.astimezone()
        first_day, last_day = local_start.date(), local_end.date()
        if local_end > local_start and local_end.time() == datetime.min.time():
            last_day -= timedelta(days=1)            # ends exactly at midnight
    rows: List[Dict[str, str]] = []
    day = max(first_day, first)
    while day <= last_day and day < after_last and len(rows) < MAX_RANGE_DAYS:
        if occ["all_day"]:
            when, until = "all day", ""
        else:
            starts_today, ends_today = day == first_day, day == last_day
            if not starts_today and not ends_today:
                when, until = "all day", ""
            else:
                when = local_start.strftime("%H:%M") if starts_today else "00:00"
                until = ((local_end.strftime("%H:%M") if local_end.date() == day else "24:00")
                         if ends_today else "24:00")
                if local_end <= local_start:
                    until = ""
        row = {"day": day.isoformat(), "weekday": _WEEKDAYS[day.weekday()].capitalize(),
               "start": when, "end": until, "title": occ["title"],
               "location": occ.get("location") or "", "calendar": occ.get("calendar") or ""}
        if occ.get("id"):
            row["id"] = occ["id"]
        rows.append(row)
        day += timedelta(days=1)
    return rows


def format_agenda(rows: List[Dict[str, str]], label: str = "") -> str:
    """The CLI's plain-text agenda, grouped by day."""
    if not rows:
        return f"Nothing on the calendar{' ' + label if label else ''}."
    lines: List[str] = []
    day = ""
    for r in rows:
        if r["day"] != day:
            day = r["day"]
            lines.append(f"{r['weekday']} {day}")
        span = r["start"] + (f"-{r['end']}" if r.get("end") else "")
        tail = f"  [{r['calendar']}]" if r.get("calendar") else ""
        ident = f"  ({r['id']})" if r.get("id") else ""
        place = f" @ {r['location']}" if r.get("location") else ""
        lines.append(f"  {span:<11}  {r['title']}{place}{tail}{ident}")
    return "\n".join(lines)


# ── mail (IMAP read, SMTP send; stdlib, run in a worker thread) ───────────────

def imap_login(acct: Dict[str, Any], password: str) -> Any:
    host, port = str(acct["imap_host"]), int(acct.get("imap_port") or 993)
    ctx = ssl.create_default_context()
    try:
        if port == 143:
            conn = imaplib.IMAP4(host, port, timeout=30)
            conn.starttls(ssl_context=ctx)
        else:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise PlannerError(f"could not reach the mail server {host}:{port} "
                           f"({type(exc).__name__})") from None
    try:
        conn.login(str(acct["user"]), password)
    except imaplib.IMAP4.error:
        domain = str(acct["user"]).rsplit("@", 1)[-1].lower()
        hint = MAIL_PRESETS.get(domain, ("", "", 0, "create one in your mail account's "
                                         "security settings"))[3]
        raise PlannerError(f"{host} refused the sign-in for {acct['user']}: it needs an APP "
                           f"PASSWORD, not your normal password -- {hint}") from None
    return conn


def _quiet_logout(conn: Any) -> None:
    """Close an IMAP session; a server that already hung up is not an error."""
    try:
        conn.logout()
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.debug("hearth mail: IMAP logout failed: %s", type(exc).__name__)


def _preview(msg: Any) -> str:
    """The first 200 characters of a message's readable text (decoded)."""
    try:
        from .transports.mail import body_text

        text = body_text(msg)
    except Exception as exc:  # noqa: BLE001 - a cut or malformed message: no preview
        logger.debug("hearth mail: no preview (%s)", type(exc).__name__)
        text = ""
    return " ".join(str(text or "").split())[:200]


def _header(value: Any) -> str:
    try:
        return " ".join(str(make_header(decode_header(str(value or "")))).split())[:200]
    except (ValueError, LookupError):
        return " ".join(str(value or "").split())[:200]


def imap_unread(acct: Dict[str, Any], password: str, count: int
                ) -> Tuple[List[Dict[str, str]], int]:
    """The newest ``count`` unread messages (headers + a short preview), unseen kept."""
    conn = imap_login(acct, password)
    try:
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            raise PlannerError("the mail server did not answer the unread search")
        ids = (data[0] or b"").split()
        messages: List[Dict[str, str]] = []
        for num in reversed(ids[-count:]):
            # The first 64 KB of the WHOLE message, parsed: the preview is the decoded
            # text part (base64 / quoted-printable / HTML), not raw transfer encoding.
            status, parts = conn.fetch(num, f"(BODY.PEEK[]<0.{PREVIEW_BYTES}>)")
            if status != "OK":
                continue
            blobs = [p[1] for p in parts if isinstance(p, tuple) and len(p) > 1]
            head = email.message_from_bytes(blobs[0] if blobs else b"",
                                            policy=email.policy.default)
            preview = _preview(head)
            messages.append({"from": _header(head.get("From")),
                             "subject": _header(head.get("Subject")),
                             "date": _header(head.get("Date")), "preview": preview})
        return messages, len(ids)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise PlannerError(f"reading the mailbox failed ({type(exc).__name__})") from None
    finally:
        _quiet_logout(conn)


def smtp_send(acct: Dict[str, Any], password: str, recipients: List[str], subject: str,
              body: str) -> None:
    host, port = str(acct.get("smtp_host") or acct["imap_host"]), int(acct.get("smtp_port") or 587)
    msg = EmailMessage()
    msg["From"] = str(acct.get("from") or acct["user"])
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(str(body or ""))
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            conn: Any = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            conn = smtplib.SMTP(host, port, timeout=30)
            conn.ehlo()
            if not conn.has_extn("starttls"):
                conn.quit()
                raise PlannerError(f"{host} does not offer STARTTLS; nothing was sent")
            conn.starttls(context=ctx)
            conn.ehlo()
        try:
            conn.login(str(acct["user"]), password)
            conn.send_message(msg)
        finally:
            try:
                conn.quit()
            except (smtplib.SMTPException, OSError) as exc:   # the message already went
                logger.debug("hearth mail: SMTP quit failed: %s", type(exc).__name__)
    except smtplib.SMTPAuthenticationError:
        raise PlannerError(f"{host} refused the sign-in for {acct['user']}; nothing was "
                           "sent -- check the app password") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise PlannerError(f"sending through {host} failed ({type(exc).__name__}); nothing "
                           "was sent") from None


__all__ = ["Planner", "PlannerError", "parse_range", "parse_start", "format_agenda",
           "display_rows", "mask_url", "normalize_feed_url", "MAIL_PRESETS", "REFRESH_S",
           "LOCAL_SOURCE"]
