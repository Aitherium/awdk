"""
Sprite Plugin for AitherShell
=============================

Your AI companion creature (AitherSprite) — check on it, care for it, talk
to it, watch it evolve. State lives server-side (Genesis /api/v1/aither-sprite);
this plugin is just a window into the terrarium.

Usage:
    /sprite                       — Show your creature (ASCII render + needs)
    /sprite hatch [NAME]          — Hatch a new sprite
    /sprite teach [KIND] <title> :: <content>
                                  — Teach it knowledge (this IS feeding it);
                                    KIND = fact|skill|link|lore (default fact)
    /sprite mind [N]              — Its knowledge base (wiki), newest first
    /sprite forget <id>           — It forgets one entry
    /sprite play|clean|rest       — Care actions (feed = legacy snack)
    /sprite talk <message>        — Talk to it (mood-tuned reply)
    /sprite history [N]           — Recent care/evolution events
    /sprite revive                — Wake a dormant sprite
    /sprite art                   — Generated-art status (media-forge renders)

Aliases: /pet
"""

import os
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify
from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


async def _get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=40, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=40, verify=tls_verify()) as c:
        resp = await c.post(f"{_genesis_url()}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _request_delete(path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=40, verify=tls_verify()) as c:
        resp = await c.delete(f"{_genesis_url()}{path}", headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _resolve_entry_id(prefix: str) -> Optional[str]:
    """The mind list shows 8-char id prefixes — resolve one back to the full
    entry id (unique-prefix match against the newest 200 entries)."""
    data = await _get(f"{BASE}/me/knowledge", params={"limit": 200})
    matches = [e["id"] for e in data.get("entries", [])
               if str(e.get("id", "")).startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


BASE = "/api/v1/aither-sprite"

# Mood → (eyes, mouth) for the ASCII creature. Same mood labels as the engine.
_FACES: Dict[str, tuple] = {
    "joyful": ("^", "▽"), "content": ("•", "‿"), "curious": ("o", "o"),
    "sleepy": ("-", "~"), "grumpy": ("¬", "⌒"), "sad": (";", "⌢"),
    "dormant": ("_", "_"),
}

_STAGE_BODY: Dict[str, List[str]] = {
    "egg": [
        "    .-\"\"-.   ",
        "   /  {c}  \\  ",
        "   \\      /  ",
        "    '-..-'   ",
    ],
    "baby": [
        "    .--.    ",
        "   ( {e}{e} )   ",
        "   (  {m}  )   ",
        "    `--'    ",
    ],
    "child": [
        "   /\\ .--. /\\  ",
        "  (  ( {e}{e} )  ) ",
        "   \\ (  {m}  ) /  ",
        "     `----'    ",
    ],
    "teen": [
        "   /\\ .---. /\\  ",
        "  (  ( {e} {e} )  ) ",
        "   \\ (  {m}   ) /  ",
        "     `-----'  ~ ",
    ],
    "adult": [
        "  /\\  .----.  /\\ ",
        " (  )( {e}  {e} )(  )",
        "  \\/ (  {m}    ) \\/ ",
        "      `------'   ",
    ],
    "elder": [
        "  /\\  .----.  /\\ ",
        " (  )( {e}  {e} )(  )",
        "  \\/ ( ={m}=   ) \\/ ",
        "   *  `------' *  ",
    ],
}


def _render_creature(status: Dict[str, Any]) -> str:
    stage = status.get("stage", "baby")
    mood = status.get("mood_label", "content")
    eyes, mouth = _FACES.get(mood, _FACES["content"])
    body = _STAGE_BODY.get(stage, _STAGE_BODY["baby"])
    crack = "*" if stage == "egg" else ""
    return "\n".join(
        line.replace("{e}", eyes).replace("{m}", mouth).replace("{c}", crack or " ")
        for line in body
    )


def _bar(value: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, value)) * width)
    return "█" * filled + "░" * (width - filled)


def _render_status(s: Dict[str, Any]) -> str:
    needs = s.get("needs", {})
    known = s.get("knowledge_count", 0)
    lines = [
        "",
        _render_creature(s),
        "",
        f"  {s.get('name', 'Sprite')} — {s.get('stage')} ({s.get('form')}), "
        f"{s.get('age_days', 0):.1f} days old — feeling {s.get('mood_label')}"
        f" — 📚 {known} learned",
        "",
        f"  📚 Curious {_bar(needs.get('hunger', 0))} {round(needs.get('hunger', 0) * 100):3d}%",
        f"  ⚡ Energy  {_bar(needs.get('energy', 0))} {round(needs.get('energy', 0) * 100):3d}%",
        f"  🫧 Clean   {_bar(needs.get('hygiene', 0))} {round(needs.get('hygiene', 0) * 100):3d}%",
        f"  💙 Bond    {_bar(needs.get('bond', 0))} {round(needs.get('bond', 0) * 100):3d}%",
    ]
    if s.get("dormant"):
        lines.append("\n  💤 Dormant — `/sprite revive` to wake it (bond penalty applies)")
    return "\n".join(lines)


_KNOWLEDGE_KINDS = ("fact", "skill", "link", "lore")
_KIND_ICONS = {"fact": "💡", "skill": "🛠", "link": "🔗", "lore": "📜"}


def _parse_teach(rest: List[str]) -> Optional[Dict[str, str]]:
    """`teach [KIND] <title> :: <content>` — kind optional, content optional."""
    if not rest:
        return None
    kind = "fact"
    if rest[0].lower() in _KNOWLEDGE_KINDS:
        kind = rest[0].lower()
        rest = rest[1:]
    text = " ".join(rest).strip()
    if not text:
        return None
    title, _, content = text.partition("::")
    title = title.strip()
    if not title:
        return None
    return {"kind": kind, "title": title[:120], "content": content.strip()[:4000]}


class SpritePlugin(SlashCommand):
    name: str = "sprite"
    aliases: List[str] = ["pet"]
    description: str = "AitherSprite — your companion creature (care, talk, evolve)"
    category: str = "fun"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='sprite',
            description='AitherSprite — your companion creature (care, talk, evolve)',
            aliases=['pet'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        import httpx

        sub = (args[0].lower() if args else "status")
        rest = args[1:]
        try:
            if sub in ("status", "show", "me"):
                return _render_status(await _get(f"{BASE}/me/status"))
            if sub == "hatch":
                name = " ".join(rest).strip() or "Sprite"
                s = await _post(f"{BASE}/hatch", {"name": name})
                return f"🐣 {s.get('name')} hatched!\n" + _render_status(s)
            if sub in ("feed", "play", "clean", "rest"):
                s = await _post(f"{BASE}/me/care", {"action": sub})
                return _render_status(s)
            if sub == "teach":
                parsed = _parse_teach(rest)
                if not parsed:
                    return ("Teach it something: /sprite teach [fact|skill|link|lore] "
                            "<title> :: <content>")
                data = await _post(f"{BASE}/me/teach", parsed)
                entry = data.get("entry", {})
                icon = _KIND_ICONS.get(entry.get("kind", "fact"), "💡")
                tail = _render_status(data["sprite"]) if data.get("sprite") else ""
                return f"  {icon} learned: {entry.get('title', parsed['title'])}\n{tail}"
            if sub in ("mind", "knowledge", "wiki"):
                limit = int(rest[0]) if rest and rest[0].isdigit() else 20
                data = await _get(f"{BASE}/me/knowledge", params={"limit": limit})
                entries = data.get("entries", [])
                if not entries:
                    return ("  (an empty mind — teach it: /sprite teach <title> :: "
                            "<content>)")
                lines = [f"  📚 The Mind — {data.get('count', len(entries))} things known:"]
                for e in entries:
                    icon = _KIND_ICONS.get(e.get("kind", "fact"), "💡")
                    lines.append(f"   {icon} [{e.get('id', '?')[:8]}] {e.get('title', '')}")
                lines.append("   (/sprite forget <id> to remove one)")
                return "\n".join(lines)
            if sub == "forget":
                if not rest:
                    return "Which one? /sprite forget <id> (see /sprite mind)"
                entry_id = await _resolve_entry_id(rest[0])
                if not entry_id:
                    return f"No unique entry matches {rest[0]!r} — check /sprite mind"
                s = await _request_delete(f"{BASE}/me/knowledge/{entry_id}")
                return "  🫧 forgotten.\n" + _render_status(s)
            if sub == "talk":
                message = " ".join(rest).strip()
                if not message:
                    return "Say something: /sprite talk <message>"
                data = await _post(f"{BASE}/me/talk", {"message": message})
                reply = data.get("reply", "…")
                tail = _render_status(data["sprite"]) if data.get("sprite") else ""
                return f'  💬 "{reply}"\n{tail}'
            if sub == "history":
                limit = int(rest[0]) if rest and rest[0].isdigit() else 15
                data = await _get(f"{BASE}/me/history", params={"limit": limit})
                events = data.get("events", [])
                if not events:
                    return "  (no history yet)"
                import datetime as _dt
                lines = ["  Recent events:"]
                for e in events:
                    ts = _dt.datetime.fromtimestamp(e["timestamp"]).strftime("%m-%d %H:%M")
                    detail = f" {e['detail']}" if e.get("detail") else ""
                    lines.append(f"   {ts}  {e['kind']}{detail}")
                return "\n".join(lines)
            if sub == "revive":
                s = await _post(f"{BASE}/me/revive")
                return "💫 Revived!\n" + _render_status(s)
            if sub == "art":
                data = await _get(f"{BASE}/me/appearance")
                assets = data.get("assets", {})
                ready = sum(1 for a in assets.values() if a.get("status") == "ready")
                lines = [f"  🎨 Art: {ready}/{len(assets)} renders ready "
                         f"(stage {data.get('stage')}/{data.get('form')})"]
                for key, a in sorted(assets.items()):
                    mark = {"ready": "✓", "failed": "✗"}.get(a.get("status"), "…")
                    lines.append(f"   [{mark}] {key}")
                return "\n".join(lines)
            return self.get_help()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return "No sprite yet — hatch one: /sprite hatch <name>"
            if e.response.status_code in (409, 422, 429):
                try:
                    return f"⚠ {e.response.json().get('detail', 'conflict')}"
                except ValueError:
                    return "⚠ conflict"
            return f"Sprite service error: {e.response.status_code}"
        except httpx.HTTPError as e:
            return f"Cannot reach the sprite service: {e}"
