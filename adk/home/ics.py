"""A small iCalendar (RFC 5545) reader for Hearth's subscribed calendars.

The home agent reads a calendar the owner already has -- Google's "secret address
in iCal format", an Outlook published calendar, an iCloud public calendar, a
CalDAV collection -- without an OAuth app: the provider hands out an ``.ics``
feed, and this module turns it into the events inside a time window.

What it reads: ``VEVENT`` with ``DTSTART`` / ``DTEND`` / ``DURATION`` (UTC ``Z``,
``TZID=``, floating and ``VALUE=DATE`` all-day), ``SUMMARY``, ``LOCATION``,
``STATUS:CANCELLED``, ``RRULE`` (``FREQ`` DAILY / WEEKLY / MONTHLY / YEARLY with
``INTERVAL``, ``COUNT``, ``UNTIL``, ``BYDAY`` incl. ``2MO`` / ``-1FR``,
``BYMONTHDAY``, ``BYMONTH``, ``BYSETPOS``), ``EXDATE`` and ``RECURRENCE-ID``
overrides. Anything else in a rule (``BYHOUR``, ``BYWEEKNO`` ...) makes that event
its first occurrence only, and :func:`parse_ics` counts it in ``skipped_rules`` so
the caller can say so rather than show a wrong repeat.

Time zones: ``TZID`` is resolved through :mod:`zoneinfo` (IANA names), then the
Windows names Outlook writes (``Pacific Standard Time``), then the feed's own
``VTIMEZONE`` standard offset, then the machine's local zone.

Standard library only. The text is third-party data: nothing here executes or
follows it, sizes are capped, and a malformed event is skipped, never fatal.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

#: Events read from one feed; the rest are ignored (and counted).
MAX_EVENTS = 20000
#: Candidate occurrences walked per recurring event before giving up.
MAX_STEPS = 40000
#: Rule iterations (yielding or not) one :func:`expand` call spends across ALL
#: events: a feed of thousands of rules that never match cannot hold the agent.
TOTAL_STEPS = 300000
#: Occurrences returned per event for one window.
MAX_PER_EVENT = 800

_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_DURATION_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_SUPPORTED_RULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY",
                        "BYMONTH", "BYSETPOS", "WKST"}

#: The Windows zone names Outlook / Exchange write into TZID, to IANA.
WINDOWS_ZONES = {
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London", "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev",
    "GTB Standard Time": "Europe/Bucharest", "Turkey Standard Time": "Europe/Istanbul",
    "Russian Standard Time": "Europe/Moscow", "Israel Standard Time": "Asia/Jerusalem",
    "South Africa Standard Time": "Africa/Johannesburg", "Egypt Standard Time": "Africa/Cairo",
    "Arabian Standard Time": "Asia/Dubai", "Arab Standard Time": "Asia/Riyadh",
    "India Standard Time": "Asia/Kolkata", "Pakistan Standard Time": "Asia/Karachi",
    "SE Asia Standard Time": "Asia/Bangkok", "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore", "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo", "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "E. Australia Standard Time": "Australia/Brisbane",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland", "Hawaiian Standard Time": "Pacific/Honolulu",
    "Alaskan Standard Time": "America/Anchorage", "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver", "US Mountain Standard Time": "America/Phoenix",
    "Central Standard Time": "America/Chicago", "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "Atlantic Standard Time": "America/Halifax", "Newfoundland Standard Time": "America/St_Johns",
    "Canada Central Standard Time": "America/Regina",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "SA Pacific Standard Time": "America/Bogota", "Argentina Standard Time": "America/Buenos_Aires",
    "E. South America Standard Time": "America/Sao_Paulo",
}


class IcsError(ValueError):
    """The text is not an iCalendar feed. ``str(exc)`` is safe to show the owner."""


class _LocalZone(tzinfo):
    """The machine's own zone, with the offset of the DATE asked about.

    ``datetime.now().astimezone().tzinfo`` is a fixed offset -- today's. A floating
    ``DTSTART:20260115T090000`` read in July would then show an hour off.
    """

    @staticmethod
    def _now_offset() -> timedelta:
        return datetime.now().astimezone().utcoffset() or timedelta(0)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return self._now_offset()
        try:
            return dt.replace(tzinfo=None).astimezone().utcoffset() or timedelta(0)
        except (OSError, OverflowError, ValueError):   # before 1970 on Windows
            return self._now_offset()

    def dst(self, dt: Optional[datetime]) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "local"

    def fromutc(self, dt: datetime) -> datetime:
        try:
            local = dt.replace(tzinfo=timezone.utc).astimezone()
        except (OSError, OverflowError, ValueError):
            local = dt.replace(tzinfo=timezone.utc) + self._now_offset()
        return local.replace(tzinfo=self)


_LOCAL = _LocalZone()


def local_tz() -> tzinfo:
    """The machine's zone (offset follows the date, see :class:`_LocalZone`)."""
    return _LOCAL


def _zone(name: str) -> Optional[tzinfo]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown key, or no tz database on this machine
        return None


def resolve_tz(tzid: str, vtimezones: Optional[Dict[str, timedelta]] = None) -> tzinfo:
    """A ``TZID`` as a tzinfo: IANA, a Windows name, the feed's VTIMEZONE, else local."""
    name = (tzid or "").strip().strip('"')
    if not name:
        return local_tz()
    if name.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
        return timezone.utc
    found = _zone(name)
    if found is None and name in WINDOWS_ZONES:
        found = _zone(WINDOWS_ZONES[name])
    if found is not None:
        return found
    offset = (vtimezones or {}).get(name)
    if offset is not None:
        return timezone(offset)
    return local_tz()


# ── lines ─────────────────────────────────────────────────────────────────────

def unfold(text: str) -> List[str]:
    """Content lines with RFC 5545 folding (CRLF + space/tab) undone."""
    out: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def _split_line(line: str) -> Tuple[str, Dict[str, str], str]:
    """``NAME;PARAM=V:value`` -> (NAME, {PARAM: V}, value). Quoted params may hold ':'."""
    in_quote = False
    cut = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            cut = i
            break
    if cut < 0:
        return line.upper(), {}, ""
    head, value = line[:cut], line[cut + 1:]
    parts = head.split(";")
    params: Dict[str, str] = {}
    for p in parts[1:]:
        key, _, val = p.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _text(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return " ".join("".join(out).split())[:300]


def _offset(value: str) -> Optional[timedelta]:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", value.strip())
    if not m:
        return None
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)),
                      seconds=int(m.group(4) or 0))
    return -delta if m.group(1) == "-" else delta


def _when(value: str, params: Dict[str, str],
          vtz: Dict[str, timedelta]) -> Tuple[Any, bool]:
    """A DTSTART/DTEND/EXDATE value -> (date | aware datetime, is_all_day)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z)?", value)
    if not m:
        raise ValueError(f"unreadable date-time {value[:32]!r}")
    naive = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                     int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    if m.group(7):
        return naive.replace(tzinfo=timezone.utc), False
    return naive.replace(tzinfo=resolve_tz(params.get("TZID", ""), vtz)), False


def _duration(value: str) -> Optional[timedelta]:
    m = _DURATION_RE.match(value.strip().upper())
    if not m:
        return None
    delta = timedelta(weeks=int(m.group(2) or 0), days=int(m.group(3) or 0),
                      hours=int(m.group(4) or 0), minutes=int(m.group(5) or 0),
                      seconds=int(m.group(6) or 0))
    return -delta if m.group(1) == "-" else delta


def _rule(value: str) -> Tuple[Dict[str, str], bool]:
    rule: Dict[str, str] = {}
    for part in value.strip().split(";"):
        key, _, val = part.partition("=")
        if key:
            rule[key.strip().upper()] = val.strip().upper()
    supported = (set(rule) <= _SUPPORTED_RULE_KEYS
                 and rule.get("FREQ") in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"))
    return rule, supported


# ── parse ─────────────────────────────────────────────────────────────────────

def parse_ics(text: str) -> Dict[str, Any]:
    """``{"events": [...], "name": str, "skipped": n, "skipped_rules": n}``.

    Each event: ``uid, title, location, start, end`` (both ``date`` for an all-day
    event, aware ``datetime`` otherwise), ``all_day, rule, exdates, recurrence_id``.
    Raises :class:`IcsError` when the text holds no ``VCALENDAR``.
    """
    lines = unfold(text or "")
    if not any(ln.strip().upper() == "BEGIN:VCALENDAR" for ln in lines[:50]):
        raise IcsError("that address did not return a calendar (no BEGIN:VCALENDAR) -- "
                       "use the calendar's ICS / iCal link, not its web page")
    vtz: Dict[str, timedelta] = {}
    # Pass 1: VTIMEZONE standard offsets, the fallback for a TZID nothing else knows.
    tzid, in_standard = "", False
    for ln in lines:
        name, _params, value = _split_line(ln)
        if name == "BEGIN" and value.upper() == "VTIMEZONE":
            tzid = ""
        elif name == "TZID" and not in_standard:
            tzid = value.strip()
        elif name == "BEGIN" and value.upper() == "STANDARD":
            in_standard = True
        elif name == "END" and value.upper() == "STANDARD":
            in_standard = False
        elif name == "TZOFFSETTO" and in_standard and tzid and tzid not in vtz:
            off = _offset(value)
            if off is not None:
                vtz[tzid] = off

    events: List[Dict[str, Any]] = []
    skipped = skipped_rules = 0
    cal_name = ""
    cur: Optional[List[Tuple[str, Dict[str, str], str]]] = None
    depth = 0
    for ln in lines:
        name, params, value = _split_line(ln)
        if name == "BEGIN":
            if value.upper() == "VEVENT" and cur is None:
                cur, depth = [], 0
            elif cur is not None:
                depth += 1          # VALARM etc. inside the event: ignore its lines
            continue
        if name == "END":
            if cur is not None and depth:
                depth -= 1
            elif cur is not None and value.upper() == "VEVENT":
                if len(events) >= MAX_EVENTS:
                    skipped += 1
                else:
                    try:
                        ev, rule_ok = _event(cur, vtz)
                    except (ValueError, OverflowError):
                        skipped += 1
                    else:
                        if ev is not None:
                            events.append(ev)
                            if not rule_ok:
                                skipped_rules += 1
                cur = None
            continue
        if cur is not None:
            if not depth:
                cur.append((name, params, value))
        elif name == "X-WR-CALNAME" and not cal_name:
            cal_name = _text(value)[:80]
    return {"events": events, "name": cal_name, "skipped": skipped,
            "skipped_rules": skipped_rules}


def _event(props: List[Tuple[str, Dict[str, str], str]],
           vtz: Dict[str, timedelta]) -> Tuple[Optional[Dict[str, Any]], bool]:
    start = end = recurrence_id = None
    all_day = False
    duration: Optional[timedelta] = None
    rule: Dict[str, str] = {}
    rule_ok = True
    exdates: Set[Any] = set()
    title = location = uid = ""
    cancelled = False
    for name, params, value in props:
        if name == "DTSTART":
            start, all_day = _when(value, params, vtz)
        elif name == "DTEND":
            end, _ = _when(value, params, vtz)
        elif name == "DURATION":
            duration = _duration(value)
        elif name == "SUMMARY":
            title = _text(value)
        elif name == "LOCATION":
            location = _text(value)
        elif name == "UID":
            uid = value.strip()[:200]
        elif name == "STATUS" and value.strip().upper() == "CANCELLED":
            cancelled = True
        elif name == "RRULE":
            rule, rule_ok = _rule(value)
            if not rule_ok:
                rule = {}
        elif name == "EXDATE":
            for one in value.split(","):
                if one.strip():
                    exdates.add(_key(_when(one, params, vtz)[0]))
        elif name == "RECURRENCE-ID":
            recurrence_id = _key(_when(value, params, vtz)[0])
    if cancelled and recurrence_id is None:
        return None, True
    if start is None:
        raise ValueError("VEVENT without DTSTART")
    if end is None or type(end) is not type(start):
        if duration is not None and duration > timedelta(0):
            end = start + duration
        else:
            end = start + timedelta(days=1) if all_day else start
    if end < start:
        end = start
    return ({"uid": uid, "title": title or "(no title)", "location": location,
             "start": start, "end": end, "all_day": all_day, "rule": rule,
             "exdates": exdates, "recurrence_id": recurrence_id,
             # A cancelled INSTANCE is kept: its recurrence id removes that one
             # occurrence from the series; it is never shown itself.
             "cancelled": cancelled}, rule_ok)


def _key(value: Any) -> Any:
    """One instant (or one day) as a comparable key across zones."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# ── recurrence ────────────────────────────────────────────────────────────────

def _month_days(year: int, month: int) -> int:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def _in_month(year: int, month: int, rule: Dict[str, str], default_day: int) -> List[int]:
    """The days of one month a MONTHLY / YEARLY rule picks, ascending."""
    last = _month_days(year, month)
    days: List[int] = []
    byday = [b for b in rule.get("BYDAY", "").split(",") if b]
    bymonthday = [int(b) for b in rule.get("BYMONTHDAY", "").split(",")
                  if re.fullmatch(r"[+-]?\d{1,2}", b)]
    if byday:
        for token in byday:
            m = _BYDAY_RE.match(token)
            if not m:
                continue
            want = _WEEKDAY[m.group(2)]
            hits = [d for d in range(1, last + 1) if date(year, month, d).weekday() == want]
            if m.group(1):
                n = int(m.group(1))
                if n and -len(hits) <= n <= len(hits):
                    days.append(hits[n - 1] if n > 0 else hits[n])
            else:
                days.extend(hits)
        if bymonthday:
            allowed = {d if d > 0 else last + 1 + d for d in bymonthday}
            days = [d for d in days if d in allowed]
    elif bymonthday:
        days = [d if d > 0 else last + 1 + d for d in bymonthday]
    else:
        days = [default_day]
    days = sorted({d for d in days if 1 <= d <= last})
    setpos = rule.get("BYSETPOS", "")
    if setpos and re.fullmatch(r"[+-]?\d{1,3}", setpos) and days:
        n = int(setpos)
        days = [days[n - 1 if n > 0 else n]] if n and -len(days) <= n <= len(days) else []
    return days


def _candidates(first: datetime, rule: Dict[str, str], budget: List[int],
                hint: Optional[datetime] = None,
                stop: Optional[datetime] = None) -> Iterator[datetime]:
    """Naive wall-clock candidate starts of a rule, ascending, from ``first`` on.

    ``budget`` (one int in a list, shared by a whole :func:`expand` call) is spent
    on every iteration, matching or not: a rule that never matches ends the walk.
    ``hint`` lets a rule without COUNT start near the window instead of at a
    DTSTART decades back (it never skips past ``hint``). Past ``stop`` (the end of
    the window) the walk ends, so a rule that never matches costs a few steps.
    """
    freq = rule.get("FREQ", "")
    try:
        interval = max(1, min(int(rule.get("INTERVAL", "1") or 1), 100000))
    except ValueError:
        interval = 1
    clock = (first.hour, first.minute, first.second)
    ahead = hint is not None and hint > first
    try:
        if freq == "DAILY":
            wanted = {_WEEKDAY[b[-2:]] for b in rule.get("BYDAY", "").split(",")
                      if b[-2:] in _WEEKDAY}
            months = {int(b) for b in rule.get("BYMONTH", "").split(",")
                      if b.isdigit() and 1 <= int(b) <= 12}
            cur = first
            if ahead:
                cur = first + timedelta(days=((hint - first).days // interval) * interval)
            while budget[0] > 0 and (stop is None or cur <= stop):
                budget[0] -= 1
                if (not wanted or cur.weekday() in wanted) and (
                        not months or cur.month in months):
                    yield cur
                cur = cur + timedelta(days=interval)
        elif freq == "WEEKLY":
            wanted_list = sorted({_WEEKDAY[b[-2:]] for b in rule.get("BYDAY", "").split(",")
                                  if b[-2:] in _WEEKDAY}) or [first.weekday()]
            # WKST decides which days share a "week" when INTERVAL > 1.
            wkst = _WEEKDAY.get(rule.get("WKST", "MO"), 0)
            offsets = sorted((wd - wkst) % 7 for wd in wanted_list)
            months = {int(b) for b in rule.get("BYMONTH", "").split(",")
                      if b.isdigit() and 1 <= int(b) <= 12}
            week = first - timedelta(days=(first.weekday() - wkst) % 7)
            if ahead:
                week = week + timedelta(weeks=((hint - week).days // (7 * interval)) * interval)
            while budget[0] > 0 and (stop is None or week <= stop):
                budget[0] -= 1
                for off in offsets:
                    cand = week + timedelta(days=off)
                    if cand >= first and (not months or cand.month in months):
                        yield cand
                week = week + timedelta(weeks=interval)
        elif freq in ("MONTHLY", "YEARLY"):
            year, month = first.year, first.month
            bymonth = sorted({int(b) for b in rule.get("BYMONTH", "").split(",")
                              if b.isdigit() and 1 <= int(b) <= 12})
            if ahead and freq == "MONTHLY":
                base = year * 12 + month - 1
                total = base + (((hint.year * 12 + hint.month - 1) - base) // interval) * interval
                year, month = total // 12, total % 12 + 1
            elif ahead:
                year = first.year + ((hint.year - first.year) // interval) * interval
            while year <= 9998 and budget[0] > 0:
                if stop is not None and (year, month if freq == "MONTHLY" else 1) > (
                        stop.year, stop.month if freq == "MONTHLY" else 12):
                    return
                budget[0] -= 1
                months_now = [month] if freq == "MONTHLY" else (bymonth or [first.month])
                for mo in months_now:
                    if freq == "MONTHLY" and bymonth and mo not in bymonth:
                        continue
                    for day in _in_month(year, mo, rule, first.day):
                        cand = datetime(year, mo, day, *clock)
                        if cand >= first:
                            yield cand
                if freq == "MONTHLY":
                    total = (year * 12 + month - 1) + interval
                    year, month = total // 12, total % 12 + 1
                else:
                    year += interval
    except OverflowError:       # walked past year 9999
        return


def _until(rule: Dict[str, str], tz: tzinfo) -> Optional[datetime]:
    raw = rule.get("UNTIL", "")
    if not raw:
        return None
    try:
        value, is_day = _when(raw, {}, {})
    except ValueError:
        return None
    if is_day:
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=tz)
    if raw.endswith("Z"):
        return value
    return value.replace(tzinfo=tz)


def expand(parsed: Dict[str, Any], window_start: datetime, window_end: datetime,
           stats: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """Every occurrence overlapping ``[window_start, window_end)`` (aware datetimes).

    Each: ``{"title", "location", "all_day", "start", "end"}`` -- ``start`` / ``end``
    are ``date`` for an all-day event (end exclusive), aware ``datetime`` otherwise.
    Sorted by start. An event whose dates cannot be computed is skipped, never
    fatal; ``stats`` (when given) counts ``skipped`` events and sets ``exhausted``
    when the :data:`TOTAL_STEPS` budget ran out before every rule was walked.
    """
    events = parsed.get("events") or []
    overridden: Dict[str, Set[Any]] = {}
    for ev in events:
        if ev.get("recurrence_id") is not None and ev.get("uid"):
            overridden.setdefault(ev["uid"], set()).add(ev["recurrence_id"])
    day_start, day_end = window_start.astimezone().date(), window_end.astimezone()
    last_day = (day_end - timedelta(microseconds=1)).date()
    out: List[Dict[str, Any]] = []
    budget = [TOTAL_STEPS]
    counts = stats if stats is not None else {}

    def overlaps(start: Any, end: Any, all_day: bool) -> bool:
        if all_day:
            return start <= last_day and (end > day_start or start == day_start)
        return start < window_end and (end > window_start or start == end >= window_start)

    def one(ev: Dict[str, Any]) -> None:
        start, end, all_day = ev["start"], ev["end"], ev["all_day"]
        span = end - start
        rule = ev.get("rule") or {}
        if not rule or ev.get("recurrence_id") is not None:
            if not ev.get("cancelled") and overlaps(start, end, all_day):
                out.append(_occurrence(ev, start, end))
            return
        tz = (start.tzinfo if isinstance(start, datetime) else None) or local_tz()
        first = (datetime(start.year, start.month, start.day) if all_day
                 else start.replace(tzinfo=None))
        until = _until(rule, tz)
        try:
            count = int(rule["COUNT"]) if rule.get("COUNT") else 0
        except ValueError:
            count = 0
        skip = set(ev.get("exdates") or ()) | overridden.get(ev.get("uid") or "", set())
        skip_days = {k for k in skip if not isinstance(k, datetime)}
        hint = None
        if not count:      # COUNT counts from DTSTART, so only an uncounted rule may jump
            edge = window_start.astimezone(tz).replace(tzinfo=None)
            hint = edge - span - timedelta(days=2)
        seen = emitted = 0
        stop = window_end.astimezone(tz).replace(tzinfo=None) + timedelta(days=2)
        for cand in _candidates(first, rule, budget, hint, stop):
            seen += 1
            if seen > MAX_STEPS or (count and seen > count) or emitted >= MAX_PER_EVENT:
                break
            occ_start: Any = cand.date() if all_day else cand.replace(tzinfo=tz)
            aware = occ_start if not all_day else cand.replace(tzinfo=tz)
            if until is not None and aware > until:
                break
            if (occ_start > last_day) if all_day else (occ_start >= window_end):
                break
            if _key(occ_start) in skip or (not all_day and cand.date() in skip_days):
                continue
            occ_end = occ_start + span
            if overlaps(occ_start, occ_end, all_day):
                out.append(_occurrence(ev, occ_start, occ_end))
                emitted += 1

    for ev in events:
        if budget[0] <= 0:
            counts["exhausted"] = 1
            break
        try:
            one(ev)
        except (OverflowError, ValueError, OSError, TypeError):
            counts["skipped"] = counts.get("skipped", 0) + 1
    if budget[0] <= 0:
        counts["exhausted"] = 1
    out.sort(key=_sort_key)
    return out


def _occurrence(ev: Dict[str, Any], start: Any, end: Any) -> Dict[str, Any]:
    return {"title": ev["title"], "location": ev.get("location") or "",
            "all_day": bool(ev["all_day"]), "start": start, "end": end}


def _sort_key(occ: Dict[str, Any]) -> Tuple[datetime, int, str]:
    start = occ["start"]
    if isinstance(start, datetime):
        return start.astimezone(timezone.utc), 1, occ["title"]
    return (datetime(start.year, start.month, start.day, tzinfo=local_tz())
            .astimezone(timezone.utc), 0, occ["title"])


__all__ = ["IcsError", "parse_ics", "expand", "unfold", "resolve_tz", "local_tz",
           "WINDOWS_ZONES"]
