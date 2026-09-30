"""
Aither Learn Plugin for AitherShell (parent side)
=================================================

The guardian's window onto the family tutor: enrol a child, hand them a pair code,
read the weekly report and the transcript, queue a skill, adjust quest settings.
A thin client of the Genesis router ``/api/v1/tutor/family/*`` called with YOUR
bearer, so the router's guardian scoping applies exactly as in the parent console
(another family's learner is simply "not found").

Usage:
    /tutor learners                             — Your children in Aither Learn
    /tutor enroll ALIAS... --grade N --age-band B --consent
                                                — Enrol a child (N: 1 or 2; B: 6-7 or 8-9).
                                                  --consent confirms you read the notice.
    /tutor code LID                             — A fresh pair code (the old one stops working)
    /tutor report LID [--week YYYY-Www]         — The weekly learning report
    /tutor transcript LID [--limit N]           — What was said and done (read-only)
    /tutor assign LID SKILL [--note TEXT...]    — Put a skill in the next quest
    /tutor focus LID [SKILL...] [--note TEXT...]
                                                — This week's focus (about half of new
                                                  practice) + a coach note for hint tone.
                                                  No skills and no note clears it;
                                                  `/tutor focus LID --show` lists skills.
    /tutor set LID key=value...                 — quest_minutes (2-10), daily_cap_minutes
                                                  (5-45), focus (math|reading|mixed),
                                                  theme, ask_enabled (true|false)

Aliases: /learn
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from adk.shell.plugins import SlashCommand

try:
    from adk._tls import tls_verify
except ImportError:  # pragma: no cover - older adk without the TLS helper
    def tls_verify():  # type: ignore[no-redef]
        return True

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore

PREFIX = "/api/v1/tutor/family"
GRADES = ("1", "2")
AGE_BANDS = ("6-7", "8-9")
FOCUS = ("math", "reading", "mixed")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9 ]{1,24}$")
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_MAX_NOTE = 140
_MAX_COACH_NOTE = 280
_MAX_FOCUS_SKILLS = 6
#: settings key -> (kind, lo, hi) for ints; kind only for the rest.
_SETTINGS: Dict[str, Tuple[str, int, int]] = {
    "quest_minutes": ("int", 2, 10),
    "daily_cap_minutes": ("int", 5, 45),
    "focus": ("focus", 0, 0),
    "theme": ("str", 0, 0),
    "ask_enabled": ("bool", 0, 0),
}
NOTICE = ("Aither Learn keeps only a nickname, a grade and an age band. Answers, "
          "time spent and what the tutor said are saved so you can read them in "
          "/tutor report and /tutor transcript; nothing is shared outside your family "
          "and free chat stays off unless you turn it on. Re-run with --consent to "
          "confirm you have read this and agree as the child's guardian.")


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    """Bearer only. The tenant and the guardian come from the authenticated caller
    on the server, never from a header this client chooses."""
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(ctx: Optional[Dict[str, Any]], method: str, path: str,
                   body: Optional[dict] = None,
                   params: Optional[dict] = None) -> Tuple[int, Any]:
    import httpx

    url = f"{_genesis_url(ctx)}{PREFIX}{path}"
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as client:
        resp = await client.request(method, url, json=body, params=params, headers=_headers())
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _render_error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in — run `aither login` first (Aither Learn needs your account)."
    if status == 403:
        return f"Not allowed: {detail if isinstance(detail, str) else json.dumps(detail)}"
    if status == 404:
        return "No such learner in your family."
    if isinstance(detail, str):
        return f"Error {status}: {detail}"
    return f"Error {status}: {json.dumps(detail, default=str)}"


def _pop_flag(args: List[str], flag: str,
              default: Optional[str]) -> Tuple[Optional[str], List[str]]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
        return default, args[:i]
    return default, args


def _pop_bool(args: List[str], flag: str) -> Tuple[bool, List[str]]:
    if flag in args:
        return True, [a for a in args if a != flag]
    return False, args


def _seg(value: str) -> str:
    """One URL path segment -- a learner id can never walk the path."""
    return quote(str(value).strip(), safe="")


def _parse_setting(key: str, raw: str) -> Tuple[Optional[Any], str]:
    spec = _SETTINGS.get(key)
    if spec is None:
        return None, f"Unknown setting {key!r}; one of {', '.join(_SETTINGS)}"
    kind, lo, hi = spec
    if kind == "int":
        try:
            n = int(raw)
        except ValueError:
            return None, f"{key} must be a whole number {lo}-{hi}"
        if not lo <= n <= hi:
            return None, f"{key} must be {lo}-{hi}"
        return n, ""
    if kind == "bool":
        low = raw.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True, ""
        if low in ("false", "no", "off", "0"):
            return False, ""
        return None, f"{key} must be true or false"
    if kind == "focus":
        if raw not in FOCUS:
            return None, f"focus must be one of {', '.join(FOCUS)}"
        return raw, ""
    text = raw.strip()
    if not text or len(text) > 24:
        return None, f"{key} must be 1-24 characters"
    return text, ""


def _render_report(data: Dict[str, Any]) -> str:
    lines = [f"Minutes this week: {data.get('minutes', 0)}   "
             f"sessions: {data.get('sessions', 0)}   attempts: {data.get('attempts', 0)}   "
             f"breaks taken: {data.get('breaks_used', 0)}"]
    skills = data.get("skills") or {}
    if skills:
        lines.append("Skills:")
        for sid, s in sorted(skills.items()):
            s = s if isinstance(s, dict) else {}
            lines.append(f"  {sid}  {s.get('kid_title') or ''}  [{s.get('state') or '-'}]")
    for label, key in (("Ready to learn next", "edge"),
                       ("Worth a gentle review", "at_risk_reviews"),
                       ("Observations", "observations"),
                       ("Your notes", "notes")):
        rows = data.get(key) or []
        if rows:
            lines.append(f"{label}:")
            lines.extend(f"  - {r if isinstance(r, str) else json.dumps(r, default=str)}"
                         for r in rows)
    return "\n".join(lines)


def _render_focus(data: Dict[str, Any], catalog: bool = False) -> str:
    f = data.get("focus") or {}
    skills = f.get("skills") or []
    if not skills and not f.get("note"):
        lines = ["No weekly focus set."]
    else:
        names = ", ".join(f"{s.get('kid_title') or s.get('skill_id')} ({s.get('skill_id')})"
                          for s in skills if isinstance(s, dict)) or "none"
        state = f"until {f.get('active_until')}" if f.get("active") else "expired"
        lines = [f"Weekly focus ({state}): {names}"]
        if f.get("note"):
            lines.append(f"Coach note: {f['note']}")
    if catalog:
        rows = data.get("catalog") or []
        if rows:
            lines.append("Skills (* = ready now):")
            lines.extend(f"  {'*' if r.get('ready') else ' '} {r.get('skill_id')}  "
                         f"{r.get('kid_title') or ''}" for r in rows if isinstance(r, dict))
    return "\n".join(lines)


class TutorPlugin(SlashCommand):
    name: str = "tutor"
    aliases: List[str] = ["learn"]
    description: str = "Aither Learn — enrol your kids, pair codes, weekly reports, quest settings"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand dataclass does not carry a subclass's class attrs
        # onto the instance; without this the plugin registers under an EMPTY name.
        super().__init__(*args, **kwargs)
        self.name = "tutor"
        self.aliases = ["learn"]
        self.description = ("Aither Learn — enrol your kids, pair codes, weekly reports, "
                            "quest settings")
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower().replace("_", "-"), args[1:]
        handler = {
            "learners": self._learners,
            "list": self._learners,
            "enroll": self._enroll,
            "enrol": self._enroll,
            "code": self._code,
            "pair-code": self._code,
            "report": self._report,
            "transcript": self._transcript,
            "assign": self._assign,
            "focus": self._focus,
            "set": self._set,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {args[0]}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    async def _learners(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/learners")
        if status != 200:
            return _render_error(status, data)
        rows = data.get("learners") if isinstance(data, dict) else data
        if not rows:
            return "No learners yet — enrol one with /tutor enroll ALIAS --grade N --age-band B"
        out = []
        for r in rows:
            paired = "paired" if r.get("claimed") else "waiting for pair code"
            out.append(f"  {r.get('lid', '?')}  {r.get('alias', '')}  grade {r.get('grade', '?')}"
                       f"  ages {r.get('age_band', '?')}  ({paired})")
        return "\n".join(out)

    async def _enroll(self, args: List[str], ctx: Dict[str, Any]) -> str:
        usage = "Usage: /tutor enroll ALIAS... --grade N --age-band B --consent"
        grade, args = _pop_flag(args, "--grade", None)
        band, args = _pop_flag(args, "--age-band", None)
        consent, args = _pop_bool(args, "--consent")
        if not args or not grade or not band:
            return usage
        alias = " ".join(args).strip()
        if not _ALIAS_RE.match(alias):
            return "ALIAS is a nickname of up to 24 letters, digits or spaces."
        if grade not in GRADES:
            return f"--grade must be one of {', '.join(GRADES)}"
        if band not in AGE_BANDS:
            return f"--age-band must be one of {', '.join(AGE_BANDS)}"
        if not consent:
            return NOTICE
        body = {"alias": alias, "grade": int(grade), "age_band": band,
                "guardian_consent": {"notice_read": True, "consent": True}}
        status, data = await _request(ctx, "POST", "/learners", body)
        if status not in (200, 201):
            return _render_error(status, data)
        return (f"Enrolled {alias} ({data.get('lid', '?')}).\n"
                f"Pair code: {data.get('pair_code', '?')}  (shown once; valid until "
                f"{data.get('expires_at', '?')})\n"
                "Have your child enter it on their own sign-in at /learn.")

    async def _code(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /tutor code LID"
        status, data = await _request(ctx, "POST", f"/learners/{_seg(args[0])}/pair-code")
        if status not in (200, 201):
            return _render_error(status, data)
        return (f"Pair code: {data.get('pair_code', '?')}  (shown once; valid until "
                f"{data.get('expires_at', '?')}; any older code no longer works)")

    async def _report(self, args: List[str], ctx: Dict[str, Any]) -> str:
        week, args = _pop_flag(args, "--week", None)
        if not args:
            return "Usage: /tutor report LID [--week YYYY-Www]"
        if week is not None and not _WEEK_RE.match(week):
            return "--week looks like 2026-W40"
        params = {"week": week} if week else None
        status, data = await _request(ctx, "GET", f"/learners/{_seg(args[0])}/report",
                                      params=params)
        if status != 200:
            return _render_error(status, data)
        return _render_report(data if isinstance(data, dict) else {})

    async def _transcript(self, args: List[str], ctx: Dict[str, Any]) -> str:
        limit, args = _pop_flag(args, "--limit", "50")
        if not args:
            return "Usage: /tutor transcript LID [--limit N]"
        try:
            n = max(1, min(int(limit or "50"), 500))
        except ValueError:
            return "--limit must be a whole number"
        status, data = await _request(ctx, "GET", f"/learners/{_seg(args[0])}/transcript",
                                      params={"limit": n})
        if status != 200:
            return _render_error(status, data)
        events = data.get("events") if isinstance(data, dict) else data
        if not events:
            return "Nothing in the transcript yet."
        return "\n".join(
            f"  {e.get('ts', '')}  {e.get('actor', '')}  {e.get('kind', '')}: {e.get('text', '')}"
            if isinstance(e, dict) else f"  {e}" for e in events)

    async def _assign(self, args: List[str], ctx: Dict[str, Any]) -> str:
        usage = "Usage: /tutor assign LID SKILL [--note TEXT...]"
        note = ""
        if "--note" in args:
            i = args.index("--note")
            note, args = " ".join(args[i + 1:]).strip(), args[:i]
        if len(args) != 2:
            return usage
        if len(note) > _MAX_NOTE:
            return f"--note is limited to {_MAX_NOTE} characters"
        body: Dict[str, Any] = {"skill_id": args[1]}
        if note:
            body["note"] = note
        status, data = await _request(ctx, "POST", f"/learners/{_seg(args[0])}/assign", body)
        if status not in (200, 201):
            return _render_error(status, data)
        return f"Queued {args[1]} for the next quest ({data.get('assignment_id', '?')})."

    async def _focus(self, args: List[str], ctx: Dict[str, Any]) -> str:
        usage = "Usage: /tutor focus LID [SKILL...] [--note TEXT...]  (or LID --show)"
        note = ""
        if "--note" in args:
            i = args.index("--note")
            note, args = " ".join(args[i + 1:]).strip(), args[:i]
        show, args = _pop_bool(args, "--show")
        if not args:
            return usage
        lid, skills = args[0], list(dict.fromkeys(args[1:]))
        path = f"/learners/{_seg(lid)}/focus"
        if show:
            status, data = await _request(ctx, "GET", path)
            if status != 200:
                return _render_error(status, data)
            return _render_focus(data if isinstance(data, dict) else {}, catalog=True)
        if len(skills) > _MAX_FOCUS_SKILLS:
            return f"At most {_MAX_FOCUS_SKILLS} focus skills"
        if len(note) > _MAX_COACH_NOTE:
            return f"--note is limited to {_MAX_COACH_NOTE} characters"
        status, data = await _request(ctx, "PUT", path, {"skills": skills, "note": note})
        if status not in (200, 201):
            return _render_error(status, data)
        return _render_focus(data if isinstance(data, dict) else {})

    async def _set(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 2:
            return "Usage: /tutor set LID key=value..."
        settings: Dict[str, Any] = {}
        for pair in args[1:]:
            if "=" not in pair:
                return f"Expected key=value, got {pair!r}"
            key, raw = pair.split("=", 1)
            value, err = _parse_setting(key.strip(), raw.strip())
            if err:
                return err
            settings[key.strip()] = value
        status, data = await _request(ctx, "PATCH", f"/learners/{_seg(args[0])}",
                                      {"settings": settings})
        if status not in (200, 201):
            return _render_error(status, data)
        return "Saved: " + ", ".join(f"{k}={json.dumps(v)}" for k, v in settings.items())
