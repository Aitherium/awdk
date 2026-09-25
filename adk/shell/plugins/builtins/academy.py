"""
Aither Academy Plugin for AitherShell
=====================================

Classes, lessons, the classroom site and the self-host connectivity wizard from the
shell. A thin window onto the Genesis router ``/api/v1/academy/*`` -- the same surface
the portal panels use -- called with YOUR bearer, so the router's per-teacher tenant
scoping and role checks apply exactly as they do in the portal.

Usage:
    /academy status                           — Is the Academy router up?
    /academy classes                          — List your classes
    /academy new-class NAME... [--grade G] [--subject S]
                                              — Create a class (G: K-2, 3-5, 6-8, 9-12)
    /academy lessons CLASS_ID                 — List a class's lessons
    /academy create-lesson CLASS_ID TOPIC... --grade G [--duration MIN] [--strategy S]
                                              — Draft a lesson (S: proficiency_level,
                                                learning_modality, pace, interest)
    /academy publish-site CLASS_ID            — Publish the classroom site
    /academy connect PATH                     — Start the connectivity wizard
                                                (PATH: adk, awnode, tunnel)

Underscore spellings (create_lesson, list_classes, publish_site,
connectivity_wizard) are accepted as the same subcommands.

Aliases: /classroom
"""

import json
import os
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

PREFIX = "/api/v1/academy"
GRADES = ("K-2", "3-5", "6-8", "9-12")
STRATEGIES = ("proficiency_level", "learning_modality", "pace", "interest")
WIZARD_PATHS = ("adk", "awnode", "tunnel")


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    """Bearer only. The tenant comes from the authenticated caller on the server,
    never from a header this client chooses."""
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
        return "Not signed in — run `aither login` first (Academy needs your account)."
    if status == 403:
        return f"Not allowed: {detail if isinstance(detail, str) else json.dumps(detail)}"
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


def _seg(value: str) -> str:
    """One URL path segment -- an id can never walk the path."""
    return quote(value, safe="")


class AcademyPlugin(SlashCommand):
    name: str = "academy"
    aliases: List[str] = ["classroom"]
    description: str = "Aither Academy — classes, lessons, classroom site, self-host wizard"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass whose __init__ does not carry a
        # subclass's class attrs onto the instance; without this the registry
        # registers the plugin under an EMPTY name (see durability.py).
        super().__init__(*args, **kwargs)
        self.name = "academy"
        self.aliases = ["classroom"]
        self.description = "Aither Academy — classes, lessons, classroom site, self-host wizard"
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower().replace("_", "-"), args[1:]
        handler = {
            "status": self._status,
            "classes": self._classes,
            "list-classes": self._classes,
            "new-class": self._new_class,
            "lessons": self._lessons,
            "create-lesson": self._create_lesson,
            "publish-site": self._publish_site,
            "connect": self._connect,
            "connectivity-wizard": self._connect,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {args[0]}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/health")
        if status != 200:
            return _render_error(status, data)
        return json.dumps(data, indent=2, default=str)

    async def _classes(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/classes")
        if status != 200:
            return _render_error(status, data)
        rows = data.get("classes") or []
        if not rows:
            return "No classes yet — create one with /academy new-class NAME"
        return "\n".join(
            f"  {c.get('id', '?')}  {c.get('name', '')}"
            f"  [{c.get('grade_level') or '-'}]  students={c.get('student_count', 0)}"
            for c in rows)

    async def _new_class(self, args: List[str], ctx: Dict[str, Any]) -> str:
        grade, args = _pop_flag(args, "--grade", None)
        subject, args = _pop_flag(args, "--subject", None)
        if not args:
            return "Usage: /academy new-class NAME... [--grade G] [--subject S]"
        if grade is not None and grade not in GRADES:
            return f"--grade must be one of {', '.join(GRADES)}"
        body: Dict[str, Any] = {"name": " ".join(args)}
        if grade:
            body["grade_level"] = grade
        if subject:
            body["subject"] = subject
        status, data = await _request(ctx, "POST", "/classes", body)
        if status not in (200, 201):
            return _render_error(status, data)
        return f"Created class {data.get('id', '?')}: {data.get('name', body['name'])}"

    async def _lessons(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /academy lessons CLASS_ID"
        status, data = await _request(ctx, "GET", f"/classes/{_seg(args[0])}/lessons")
        if status != 200:
            return _render_error(status, data)
        rows = data.get("lessons") if isinstance(data, dict) else data
        if not rows:
            return "No lessons in this class yet."
        return "\n".join(f"  {r.get('id', '?')}  {r.get('topic', '')}  ({r.get('status', '?')})"
                         for r in rows)

    async def _create_lesson(self, args: List[str], ctx: Dict[str, Any]) -> str:
        usage = ("Usage: /academy create-lesson CLASS_ID TOPIC... --grade G "
                 "[--duration MIN] [--strategy S]")
        grade, args = _pop_flag(args, "--grade", None)
        duration, args = _pop_flag(args, "--duration", "45")
        strategy, args = _pop_flag(args, "--strategy", "proficiency_level")
        if len(args) < 2 or not grade:
            return usage
        if grade not in GRADES:
            return f"--grade must be one of {', '.join(GRADES)}"
        if strategy not in STRATEGIES:
            return f"--strategy must be one of {', '.join(STRATEGIES)}"
        try:
            minutes = int(duration or "45")
        except ValueError:
            return "--duration must be a whole number of minutes"
        body = {"topic": " ".join(args[1:]), "grade_level": grade,
                "duration_minutes": minutes, "differentiation_strategy": strategy}
        status, data = await _request(ctx, "POST", f"/classes/{_seg(args[0])}/lessons", body)
        if status not in (200, 201):
            return _render_error(status, data)
        return (f"Drafted lesson {data.get('id', '?')}: {data.get('topic', body['topic'])} "
                f"({data.get('status', 'draft')})")

    async def _publish_site(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /academy publish-site CLASS_ID"
        status, data = await _request(ctx, "POST", f"/classes/{_seg(args[0])}/site/publish")
        if status not in (200, 201, 202):
            return _render_error(status, data)
        return json.dumps(data, indent=2, default=str)

    async def _connect(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args or args[0] not in WIZARD_PATHS:
            return f"Usage: /academy connect PATH   (PATH: {', '.join(WIZARD_PATHS)})"
        status, data = await _request(ctx, "POST", "/connectivity/wizard/start",
                                      {"path": args[0]})
        if status not in (200, 201):
            return _render_error(status, data)
        return json.dumps(data, indent=2, default=str)
