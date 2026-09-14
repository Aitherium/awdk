"""
AitherSaga Plugin for AitherShell
===================================

Interactive storytelling from the terminal — create stories, manage characters,
generate narrative, and browse your lorebook.

Usage:
    /saga                              - List story projects
    /saga create <title> [--genre X]   - Create a new story
    /saga open <id>                    - Show project details
    /saga delete <id>                  - Delete a project
    /saga import <file>                - Import from JSON/SillyTavern
    /saga export <id>                  - Export project as JSON

    /saga characters <id>              - List characters in a project
    /saga add-char <id> <name> [--desc "..."]  - Add a character
    /saga locations <id>               - List locations
    /saga lorebook <id>                - List lorebook entries

    /saga sessions <id>                - List play sessions
    /saga play <id> [--session S]      - Generate next narrative beat
    /saga visualize <id>               - Generate scene image

    /saga status                       - Saga service health

Aliases: /story
"""

import json
from adk._tls import tls_verify
import os
import sys
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _saga_url() -> str:
    return os.environ.get(
        "AITHERSAGA_URL",
        os.environ.get("SAGA_URL", "https://localhost:8793"),
    )


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


# ============================================================================
# ANSI helpers
# ============================================================================

class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"


def _trunc(text: str, max_len: int = 60) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


# ============================================================================
# Plugin
# ============================================================================

class SagaPlugin(SlashCommand):
    name: str = "saga"
    aliases: List[str] = ["story"]
    description: str = "Interactive storytelling engine"
    category: str = "creative"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='saga',
            description='Interactive storytelling engine',
            aliases=['story'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._list_projects([], ctx)

        sub = args[0].lower()
        dispatch = {
            "list": self._list_projects,
            "ls": self._list_projects,
            "create": self._create,
            "new": self._create,
            "open": self._open,
            "show": self._open,
            "delete": self._delete,
            "rm": self._delete,
            "import": self._import,
            "export": self._export,
            "characters": self._characters,
            "chars": self._characters,
            "add-char": self._add_character,
            "locations": self._locations,
            "lorebook": self._lorebook,
            "lore": self._lorebook,
            "write": self._write,
            "editor": self._write,
            "chapters": self._chapters,
            "manuscript": self._manuscript,
            "ms": self._manuscript,
            "outline": self._outline,
            "stats": self._stats,
            "continue": self._ai_continue,
            "rewrite": self._ai_rewrite,
            "expand": self._ai_expand,
            "suggest": self._ai_suggest,
            "brainstorm": self._ai_brainstorm,
            "sessions": self._sessions,
            "play": self._play,
            "generate": self._play,
            "visualize": self._visualize,
            "status": self._status,
            "health": self._status,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)

        # If it looks like a project ID, show it
        if len(sub) > 8:
            return await self._open([sub], ctx)

        return self._help_text()

    # ── Projects ──────────────────────────────────────────────────────────

    async def _list_projects(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get("/projects")

        if resp.status_code != 200:
            return f"{_C.RED}Failed to list projects: {resp.status_code}{_C.RESET}"

        projects = resp.json()
        if isinstance(projects, dict):
            projects = projects.get("projects", projects.get("items", []))
        if not projects:
            return (
                f"{_C.DIM}No stories yet.{_C.RESET}\n"
                f"Create one: {_C.CYAN}/saga create \"My Story\" --genre fantasy{_C.RESET}"
            )

        lines = [f"{_C.BOLD}Stories ({len(projects)}){_C.RESET}\n"]
        for p in projects:
            pid = p.get("id", "?")[:12]
            title = p.get("title", "Untitled")
            genre = p.get("genre", "")
            desc = _trunc(p.get("description", ""), 50)
            genre_tag = f" {_C.MAGENTA}[{genre}]{_C.RESET}" if genre else ""
            lines.append(f"  {_C.CYAN}{pid}{_C.RESET}  {_C.BOLD}{title}{_C.RESET}{genre_tag}")
            if desc:
                lines.append(f"          {_C.DIM}{desc}{_C.RESET}")
        lines.append(f"\n{_C.DIM}Open: /saga open <id>{_C.RESET}")
        return "\n".join(lines)

    async def _create(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga create \"Title\" [--genre fantasy] [--desc \"...\"]{_C.RESET}"

        title_parts = []
        genre = None
        description = None
        i = 0
        while i < len(args):
            if args[i] == "--genre" and i + 1 < len(args):
                genre = args[i + 1]
                i += 2
            elif args[i] in ("--desc", "--description") and i + 1 < len(args):
                description = args[i + 1]
                i += 2
            else:
                title_parts.append(args[i])
                i += 1

        title = " ".join(title_parts)
        if not title:
            return f"Usage: {_C.CYAN}/saga create \"Title\"{_C.RESET}"

        body: Dict[str, Any] = {"title": title}
        if genre:
            body["genre"] = genre
        if description:
            body["description"] = description

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.post("/projects", json=body)

        if resp.status_code not in (200, 201):
            return f"{_C.RED}Failed to create project: {resp.status_code} {resp.text[:200]}{_C.RESET}"

        data = resp.json()
        pid = data.get("id", data.get("project_id", "?"))
        return (
            f"{_C.GREEN}Story created{_C.RESET}\n"
            f"  ID:    {_C.CYAN}{pid}{_C.RESET}\n"
            f"  Title: {_C.BOLD}{title}{_C.RESET}\n"
            f"\nNext: {_C.DIM}/saga open {pid}{_C.RESET}"
        )

    async def _open(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga open <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}")

        if resp.status_code == 404:
            return f"{_C.RED}Project not found: {pid}{_C.RESET}"
        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        p = resp.json()
        lines = [
            f"{_C.BOLD}{p.get('title', 'Untitled')}{_C.RESET}",
            f"  {_C.DIM}ID:{_C.RESET} {_C.CYAN}{p.get('id', '?')}{_C.RESET}",
        ]
        if p.get("genre"):
            lines.append(f"  {_C.DIM}Genre:{_C.RESET} {_C.MAGENTA}{p['genre']}{_C.RESET}")
        if p.get("description"):
            lines.append(f"  {_C.DIM}Desc:{_C.RESET} {p['description']}")

        chars = p.get("characters", [])
        locs = p.get("locations", [])
        lore = p.get("lorebook", [])
        lines.append(
            f"  {_C.DIM}Characters:{_C.RESET} {len(chars)}  "
            f"{_C.DIM}Locations:{_C.RESET} {len(locs)}  "
            f"{_C.DIM}Lore:{_C.RESET} {len(lore)}"
        )

        if chars:
            lines.append(f"\n  {_C.BOLD}Characters:{_C.RESET}")
            for ch in chars[:10]:
                lines.append(f"    {_C.YELLOW}{ch.get('name', '?')}{_C.RESET}  {_C.DIM}{_trunc(ch.get('description', ''), 40)}{_C.RESET}")

        settings = p.get("settings", {})
        if settings:
            fmt = settings.get("format", "story")
            model = settings.get("default_model", "default")
            lines.append(f"\n  {_C.DIM}Format:{_C.RESET} {fmt}  {_C.DIM}Model:{_C.RESET} {model}")

        lines.append(
            f"\n{_C.DIM}Commands: /saga characters {p.get('id', '?')[:12]} | "
            f"/saga play {p.get('id', '?')[:12]}{_C.RESET}"
        )
        return "\n".join(lines)

    async def _delete(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga delete <project_id>{_C.RESET}"

        pid = args[0]
        # Confirm unless --force
        if "--force" not in args and "-f" not in args:
            return (
                f"{_C.YELLOW}Delete project {pid}?{_C.RESET}\n"
                f"Run {_C.CYAN}/saga delete {pid} --force{_C.RESET} to confirm."
            )

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.delete(f"/projects/{pid}")

        if resp.status_code == 404:
            return f"{_C.RED}Project not found: {pid}{_C.RESET}"
        if resp.status_code not in (200, 204):
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        return f"{_C.GREEN}Deleted project {pid}{_C.RESET}"

    async def _import(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx
        from pathlib import Path

        if not args:
            return f"Usage: {_C.CYAN}/saga import <file.json>{_C.RESET}"

        filepath = Path(args[0]).expanduser()
        if not filepath.exists():
            return f"{_C.RED}File not found: {filepath}{_C.RESET}"

        try:
            content = filepath.read_text(encoding="utf-8")
            payload = json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            return f"{_C.RED}Failed to read file: {e}{_C.RESET}"

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=30, verify=tls_verify()
        ) as c:
            resp = await c.post("/projects/import", json=payload)

        if resp.status_code not in (200, 201):
            return f"{_C.RED}Import failed: {resp.status_code} {resp.text[:200]}{_C.RESET}"

        data = resp.json()
        pid = data.get("id", data.get("project_id", "?"))
        title = data.get("title", filepath.stem)
        return (
            f"{_C.GREEN}Imported{_C.RESET}\n"
            f"  ID:    {_C.CYAN}{pid}{_C.RESET}\n"
            f"  Title: {_C.BOLD}{title}{_C.RESET}"
        )

    async def _export(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga export <project_id> [output_file]{_C.RESET}"

        pid = args[0]
        outfile = args[1] if len(args) > 1 else f"saga-{pid[:12]}.json"

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=30, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/export")

        if resp.status_code != 200:
            return f"{_C.RED}Export failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        from pathlib import Path
        Path(outfile).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return f"{_C.GREEN}Exported to {outfile}{_C.RESET}"

    # ── Characters ────────────────────────────────────────────────────────

    async def _characters(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga characters <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/characters")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        chars = resp.json()
        if isinstance(chars, dict):
            chars = chars.get("characters", chars.get("items", []))
        if not chars:
            return (
                f"{_C.DIM}No characters yet.{_C.RESET}\n"
                f"Add one: {_C.CYAN}/saga add-char {pid[:12]} \"Name\" --desc \"...\"{_C.RESET}"
            )

        lines = [f"{_C.BOLD}Characters ({len(chars)}){_C.RESET}\n"]
        for ch in chars:
            cid = ch.get("id", "?")[:12]
            name = ch.get("name", "?")
            kind = ch.get("kind", "character")
            desc = _trunc(ch.get("description", ""), 45)
            kind_color = _C.MAGENTA if kind == "narrator" else _C.YELLOW
            lines.append(f"  {_C.CYAN}{cid}{_C.RESET}  {kind_color}{name}{_C.RESET}  {_C.DIM}{desc}{_C.RESET}")
        return "\n".join(lines)

    async def _add_character(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if len(args) < 2:
            return f"Usage: {_C.CYAN}/saga add-char <project_id> <name> [--desc \"...\"] [--kind character]{_C.RESET}"

        pid = args[0]
        name_parts = []
        description = ""
        kind = "character"
        i = 1
        while i < len(args):
            if args[i] in ("--desc", "--description") and i + 1 < len(args):
                description = args[i + 1]
                i += 2
            elif args[i] == "--kind" and i + 1 < len(args):
                kind = args[i + 1]
                i += 2
            else:
                name_parts.append(args[i])
                i += 1

        name = " ".join(name_parts)
        if not name:
            return f"Usage: {_C.CYAN}/saga add-char <project_id> <name>{_C.RESET}"

        body: Dict[str, Any] = {"name": name, "kind": kind}
        if description:
            body["description"] = description

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.post(f"/projects/{pid}/characters", json=body)

        if resp.status_code not in (200, 201):
            return f"{_C.RED}Failed: {resp.status_code} {resp.text[:200]}{_C.RESET}"

        data = resp.json()
        cid = data.get("id", data.get("character_id", "?"))
        return f"{_C.GREEN}Added character{_C.RESET} {_C.YELLOW}{name}{_C.RESET}  {_C.DIM}({cid}){_C.RESET}"

    # ── Locations ─────────────────────────────────────────────────────────

    async def _locations(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga locations <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/locations")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        locs = resp.json()
        if isinstance(locs, dict):
            locs = locs.get("locations", locs.get("items", []))
        if not locs:
            return f"{_C.DIM}No locations defined.{_C.RESET}"

        lines = [f"{_C.BOLD}Locations ({len(locs)}){_C.RESET}\n"]
        for loc in locs:
            lid = loc.get("id", "?")[:12]
            name = loc.get("name", "?")
            atmo = _trunc(loc.get("atmosphere", loc.get("description", "")), 40)
            lines.append(f"  {_C.CYAN}{lid}{_C.RESET}  {_C.GREEN}{name}{_C.RESET}  {_C.DIM}{atmo}{_C.RESET}")
        return "\n".join(lines)

    # ── Lorebook ──────────────────────────────────────────────────────────

    async def _lorebook(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga lorebook <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/lorebook")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        entries = resp.json()
        if isinstance(entries, dict):
            entries = entries.get("lorebook", entries.get("entries", entries.get("items", [])))
        if not entries:
            return f"{_C.DIM}No lorebook entries.{_C.RESET}"

        lines = [f"{_C.BOLD}Lorebook ({len(entries)}){_C.RESET}\n"]
        for e in entries:
            eid = e.get("id", "?")[:12]
            name = e.get("name", "?")
            keywords = ", ".join(e.get("keywords", [])[:5])
            enabled = f"{_C.GREEN}on{_C.RESET}" if e.get("enabled", True) else f"{_C.RED}off{_C.RESET}"
            lines.append(
                f"  {_C.CYAN}{eid}{_C.RESET}  {_C.BOLD}{name}{_C.RESET}  "
                f"{_C.DIM}[{keywords}]{_C.RESET}  {enabled}"
            )
        return "\n".join(lines)

    # ── Sessions & Narrative ──────────────────────────────────────────────

    async def _sessions(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga sessions <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/sessions")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        sessions = resp.json()
        if isinstance(sessions, dict):
            sessions = sessions.get("sessions", sessions.get("items", []))
        if not sessions:
            return (
                f"{_C.DIM}No play sessions yet.{_C.RESET}\n"
                f"Start one: {_C.CYAN}/saga play {pid[:12]}{_C.RESET}"
            )

        lines = [f"{_C.BOLD}Sessions ({len(sessions)}){_C.RESET}\n"]
        for s in sessions:
            sid = s.get("id", s.get("session_id", "?"))[:12]
            created = s.get("created_at", "?")
            if isinstance(created, str) and len(created) > 10:
                created = created[:10]
            turns = s.get("turn_count", s.get("message_count", "?"))
            lines.append(f"  {_C.CYAN}{sid}{_C.RESET}  {_C.DIM}{created}{_C.RESET}  {turns} turns")

        lines.append(f"\n{_C.DIM}Continue: /saga play {pid[:12]} --session <session_id>{_C.RESET}")
        return "\n".join(lines)

    async def _play(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga play <project_id> [prompt] [--session S]{_C.RESET}"

        pid = args[0]
        session_id = None
        prompt_parts = []
        i = 1
        while i < len(args):
            if args[i] in ("--session", "-s") and i + 1 < len(args):
                session_id = args[i + 1]
                i += 2
            else:
                prompt_parts.append(args[i])
                i += 1

        prompt = " ".join(prompt_parts) if prompt_parts else None

        body: Dict[str, Any] = {"project_id": pid}
        if session_id:
            body["session_id"] = session_id
        if prompt:
            body["prompt"] = prompt

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=120, verify=tls_verify()
        ) as c:
            resp = await c.post("/engine/generate", json=body)

        if resp.status_code != 200:
            return f"{_C.RED}Generation failed: {resp.status_code} {resp.text[:300]}{_C.RESET}"

        # Handle SSE or JSON response
        content_type = resp.headers.get("content-type", "")
        if "event-stream" in content_type:
            # For non-streaming httpx, the full body is already buffered
            text = resp.text
            # Extract data lines from SSE
            narrative_parts = []
            for line in text.split("\n"):
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                        narrative_parts.append(chunk.get("text", chunk.get("content", "")))
                    except json.JSONDecodeError:
                        narrative_parts.append(payload)
            narrative = "".join(narrative_parts)
        else:
            data = resp.json()
            narrative = data.get("text", data.get("content", data.get("narrative", json.dumps(data, indent=2))))

        if not narrative:
            return f"{_C.DIM}No narrative generated.{_C.RESET}"

        lines = [f"{_C.MAGENTA}--- Narrative ---{_C.RESET}\n"]
        lines.append(narrative.strip())
        lines.append(f"\n{_C.MAGENTA}--- end ---{_C.RESET}")
        if not session_id:
            lines.append(f"\n{_C.DIM}Continue with: /saga play {pid[:12]} \"your action\"{_C.RESET}")
        return "\n".join(lines)

    async def _visualize(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga visualize <project_id> [scene description]{_C.RESET}"

        pid = args[0]
        scene_desc = " ".join(args[1:]) if len(args) > 1 else None

        body: Dict[str, Any] = {"project_id": pid}
        if scene_desc:
            body["description"] = scene_desc

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=60, verify=tls_verify()
        ) as c:
            resp = await c.post("/engine/visualize", json=body)

        if resp.status_code != 200:
            return f"{_C.RED}Visualization failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        image_url = data.get("url", data.get("image_url", data.get("path", "?")))
        prompt_used = data.get("prompt", "")
        lines = [f"{_C.GREEN}Scene generated{_C.RESET}"]
        if prompt_used:
            lines.append(f"  {_C.DIM}Prompt:{_C.RESET} {_trunc(prompt_used, 70)}")
        lines.append(f"  {_C.DIM}Image:{_C.RESET} {image_url}")
        return "\n".join(lines)

    # ── Status ────────────────────────────────────────────────────────────

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(
                base_url=_saga_url(), headers=_headers(), timeout=10, verify=tls_verify()
            ) as c:
                resp = await c.get("/health")
        except Exception as e:
            return f"{_C.RED}Saga service unreachable{_C.RESET}: {e}"

        if resp.status_code == 200:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            version = data.get("version", "?")
            return f"{_C.GREEN}Saga service is healthy{_C.RESET}  {_C.DIM}v{version}{_C.RESET}  {_C.DIM}{_saga_url()}{_C.RESET}"
        return f"{_C.RED}Saga returned {resp.status_code}{_C.RESET}"

    # ── Help ──────────────────────────────────────────────────────────────

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        return self._help_text()

    @staticmethod
    def _help_text() -> str:
        return (
            f"{_C.BOLD}AitherSaga — Interactive Storytelling{_C.RESET}\n\n"
            f"  {_C.CYAN}/saga{_C.RESET}                              List story projects\n"
            f"  {_C.CYAN}/saga create{_C.RESET} <title> [--genre X]   Create a new story\n"
            f"  {_C.CYAN}/saga open{_C.RESET} <id>                    Show project details\n"
            f"  {_C.CYAN}/saga delete{_C.RESET} <id> [--force]        Delete a project\n"
            f"  {_C.CYAN}/saga import{_C.RESET} <file.json>           Import from file\n"
            f"  {_C.CYAN}/saga export{_C.RESET} <id> [output_file]    Export as JSON\n"
            f"\n"
            f"  {_C.CYAN}/saga characters{_C.RESET} <id>              List characters\n"
            f"  {_C.CYAN}/saga add-char{_C.RESET} <id> <name>         Add a character\n"
            f"  {_C.CYAN}/saga locations{_C.RESET} <id>               List locations\n"
            f"  {_C.CYAN}/saga lorebook{_C.RESET} <id>                List lorebook entries\n"
            f"\n"
            f"  {_C.CYAN}/saga sessions{_C.RESET} <id>                List play sessions\n"
            f"  {_C.CYAN}/saga play{_C.RESET} <id> [prompt]           Generate narrative\n"
            f"  {_C.CYAN}/saga visualize{_C.RESET} <id> [scene]       Generate scene image\n"
            f"\n"
            f"  {_C.BOLD}Manuscript{_C.RESET}\n"
            f"  {_C.CYAN}/saga write{_C.RESET} <id>                   Open TUI editor\n"
            f"  {_C.CYAN}/saga manuscript{_C.RESET} <id>              Manuscript overview\n"
            f"  {_C.CYAN}/saga chapters{_C.RESET} <id>                List chapters\n"
            f"  {_C.CYAN}/saga outline{_C.RESET} <id>                 Plot outline\n"
            f"  {_C.CYAN}/saga stats{_C.RESET} <id>                   Writing statistics\n"
            f"\n"
            f"  {_C.BOLD}AI Writing Tools{_C.RESET}\n"
            f"  {_C.CYAN}/saga continue{_C.RESET} <id> <ch> <sc>      Continue writing\n"
            f"  {_C.CYAN}/saga rewrite{_C.RESET} <id> <ch> <sc> \"text\" \"instruction\"\n"
            f"  {_C.CYAN}/saga expand{_C.RESET} <id> <ch> <sc> \"text\"\n"
            f"  {_C.CYAN}/saga suggest{_C.RESET} <id>                 Plot suggestions\n"
            f"  {_C.CYAN}/saga brainstorm{_C.RESET} <id> \"topic\"      Brainstorm ideas\n"
            f"\n"
            f"  {_C.CYAN}/saga status{_C.RESET}                       Service health\n"
            f"  {_C.CYAN}/saga help{_C.RESET}                         This message\n"
            f"\n"
            f"  {_C.DIM}Aliases: /story{_C.RESET}"
        )

    # ── Manuscript Commands ───────────────────────────────────────────

    async def _write(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        """Launch the TUI writing editor."""
        if not args:
            return f"Usage: {_C.CYAN}/saga write <project_id>{_C.RESET}"

        try:
            from adk.shell.tui.saga_writer import SagaWriter, TEXTUAL_AVAILABLE
        except ImportError:
            return (
                f"{_C.RED}TUI not available.{_C.RESET}\n"
                f"Install: {_C.CYAN}pip install textual>=0.86.0{_C.RESET}"
            )

        if not TEXTUAL_AVAILABLE:
            return (
                f"{_C.RED}textual package not found.{_C.RESET}\n"
                f"Install: {_C.CYAN}pip install textual>=0.86.0{_C.RESET}"
            )

        app = SagaWriter(project_id=args[0], saga_url=_saga_url())
        await app.run_async()
        return None

    async def _manuscript(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Show manuscript overview."""
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga manuscript <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/manuscript")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        ms = resp.json()
        lines = [f"{_C.BOLD}Manuscript{_C.RESET}  {_C.DIM}({ms.get('total_word_count', 0)} words){_C.RESET}\n"]

        settings = ms.get("settings", {})
        lines.append(
            f"  POV: {settings.get('default_pov', '?')}  "
            f"Tense: {settings.get('tense', '?')}  "
            f"Chapters: {ms.get('chapter_count', 0)}"
        )

        goals = ms.get("goals", {})
        target = goals.get("total_word_target", 80000)
        progress = ms.get("total_word_count", 0) / target if target else 0
        bar_len = 20
        filled = int(progress * bar_len)
        bar = f"{'█' * filled}{'░' * (bar_len - filled)}"
        lines.append(
            f"\n  [{bar}] {progress:.0%}  "
            f"({ms.get('total_word_count', 0)}/{target})"
        )
        lines.append(
            f"  Daily: {goals.get('daily_word_target', 1000)}w  "
            f"Streak: {goals.get('streak_days', 0)}d"
        )

        chapters = ms.get("chapters", [])
        if chapters:
            lines.append(f"\n  {_C.BOLD}Chapters:{_C.RESET}")
            for ch in chapters:
                status_icon = "◆" if ch.get("status") == "final" else "◇"
                lines.append(
                    f"    {status_icon} {_C.CYAN}{ch.get('title', 'Untitled')}{_C.RESET}  "
                    f"{_C.DIM}{ch.get('word_count', 0)}w  "
                    f"{ch.get('scene_count', 0)} scenes{_C.RESET}"
                )

        lines.append(f"\n{_C.DIM}Write: /saga write {pid[:12]}{_C.RESET}")
        return "\n".join(lines)

    async def _chapters(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """List manuscript chapters."""
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga chapters <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/manuscript/chapters")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        chapters = data.get("chapters", [])
        if not chapters:
            return f"{_C.DIM}No chapters. Start writing: /saga write {pid[:12]}{_C.RESET}"

        lines = [f"{_C.BOLD}Chapters ({len(chapters)}){_C.RESET}\n"]
        for ch in chapters:
            lines.append(
                f"  {_C.CYAN}{ch.get('id', '?')[:12]}{_C.RESET}  "
                f"Act {ch.get('act', 1)}  "
                f"{_C.BOLD}{ch.get('title', 'Untitled')}{_C.RESET}  "
                f"{_C.DIM}{ch.get('word_count', 0)}w  "
                f"{ch.get('scene_count', 0)} scenes  "
                f"[{ch.get('status', 'draft')}]{_C.RESET}"
            )
            if ch.get("synopsis"):
                lines.append(f"          {_C.DIM}{_trunc(ch['synopsis'], 50)}{_C.RESET}")
        return "\n".join(lines)

    async def _outline(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Show plot outline."""
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga outline <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/manuscript/outline")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        beats = data.get("outline", [])
        if not beats:
            return f"{_C.DIM}No outline beats defined.{_C.RESET}"

        lines = [f"{_C.BOLD}Plot Outline ({len(beats)} beats){_C.RESET}\n"]
        current_act = 0
        for b in beats:
            act = b.get("act", 1)
            if act != current_act:
                current_act = act
                lines.append(f"\n  {_C.BOLD}Act {act}{_C.RESET}")

            done = "✓" if b.get("completed") else "○"
            btype = b.get("beat_type", "scene")
            type_color = _C.MAGENTA if btype in ("climax", "crisis", "midpoint") else _C.DIM
            lines.append(
                f"    {done} {_C.BOLD}{b.get('title', '?')}{_C.RESET}  "
                f"{type_color}[{btype}]{_C.RESET}"
            )
            if b.get("description"):
                lines.append(f"       {_C.DIM}{_trunc(b['description'], 55)}{_C.RESET}")
        return "\n".join(lines)

    async def _stats(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Show writing statistics."""
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga stats <project_id>{_C.RESET}"

        pid = args[0]
        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=15, verify=tls_verify()
        ) as c:
            resp = await c.get(f"/projects/{pid}/manuscript/stats")

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        s = resp.json()
        goals = s.get("goals", {})
        progress = s.get("progress", 0)
        bar_len = 25
        filled = int(progress * bar_len)
        bar = f"{'█' * filled}{'░' * (bar_len - filled)}"

        lines = [
            f"{_C.BOLD}Writing Stats{_C.RESET}\n",
            f"  Total:    {_C.BOLD}{s.get('total_word_count', 0):,}{_C.RESET} words",
            f"  Chapters: {s.get('chapter_count', 0)}  Scenes: {s.get('scene_count', 0)}",
            f"\n  [{bar}] {progress:.0%}",
            f"  Target: {goals.get('total_word_target', 80000):,} words",
            f"  Daily:  {goals.get('daily_word_target', 1000):,} words/day",
            f"  Streak: {_C.GREEN}{goals.get('streak_days', 0)}{_C.RESET} days "
            f"(best: {goals.get('best_streak', 0)})",
        ]

        if goals.get("deadline"):
            lines.append(f"  Deadline: {goals['deadline']}")

        chapters = s.get("chapters", [])
        if chapters:
            lines.append(f"\n  {_C.BOLD}Per Chapter:{_C.RESET}")
            for ch in chapters:
                lines.append(
                    f"    {ch.get('title', '?'):20s}  "
                    f"{ch.get('word_count', 0):>6,}w  "
                    f"[{ch.get('status', 'draft')}]"
                )

        return "\n".join(lines)

    # ── AI Writing Tool Commands ──────────────────────────────────────

    async def _ai_continue(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Continue writing a scene."""
        import httpx

        if len(args) < 3:
            return f"Usage: {_C.CYAN}/saga continue <project_id> <chapter_id> <scene_id> [--words N]{_C.RESET}"

        pid, ch_id, sc_id = args[0], args[1], args[2]
        word_count = 200
        for i, a in enumerate(args):
            if a == "--words" and i + 1 < len(args):
                word_count = int(args[i + 1])

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=120, verify=tls_verify()
        ) as c:
            resp = await c.post("/writing/continue", json={
                "project_id": pid, "chapter_id": ch_id,
                "scene_id": sc_id, "word_count": word_count,
            })

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code} {resp.text[:200]}{_C.RESET}"

        data = resp.json()
        return (
            f"{_C.MAGENTA}--- Continue ({data.get('word_count', '?')} words) ---{_C.RESET}\n\n"
            f"{data.get('text', '')}\n\n"
            f"{_C.MAGENTA}--- end ---{_C.RESET}"
        )

    async def _ai_rewrite(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Rewrite selected text."""
        import httpx

        if len(args) < 5:
            return (
                f"Usage: {_C.CYAN}/saga rewrite <project_id> <ch_id> <sc_id> "
                f"\"selected text\" \"instruction\"{_C.RESET}"
            )

        pid, ch_id, sc_id = args[0], args[1], args[2]
        selected = args[3]
        instruction = args[4] if len(args) > 4 else "improve clarity and flow"

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=120, verify=tls_verify()
        ) as c:
            resp = await c.post("/writing/rewrite", json={
                "project_id": pid, "chapter_id": ch_id,
                "scene_id": sc_id, "selected_text": selected,
                "instruction": instruction,
            })

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        return (
            f"{_C.MAGENTA}--- Rewrite ---{_C.RESET}\n\n"
            f"{data.get('text', '')}\n\n"
            f"{_C.DIM}({data.get('original_words', '?')} -> {data.get('new_words', '?')} words){_C.RESET}"
        )

    async def _ai_expand(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Expand a passage."""
        import httpx

        if len(args) < 4:
            return f"Usage: {_C.CYAN}/saga expand <project_id> <ch_id> <sc_id> \"text\" [--words N]{_C.RESET}"

        pid, ch_id, sc_id = args[0], args[1], args[2]
        selected = args[3]
        target = 500
        for i, a in enumerate(args):
            if a == "--words" and i + 1 < len(args):
                target = int(args[i + 1])

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=120, verify=tls_verify()
        ) as c:
            resp = await c.post("/writing/expand", json={
                "project_id": pid, "chapter_id": ch_id,
                "scene_id": sc_id, "selected_text": selected,
                "target_words": target,
            })

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        return (
            f"{_C.MAGENTA}--- Expanded ({data.get('word_count', '?')} words) ---{_C.RESET}\n\n"
            f"{data.get('text', '')}\n\n"
            f"{_C.MAGENTA}--- end ---{_C.RESET}"
        )

    async def _ai_suggest(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Get plot suggestions."""
        import httpx

        if not args:
            return f"Usage: {_C.CYAN}/saga suggest <project_id> [--count N]{_C.RESET}"

        pid = args[0]
        count = 3
        for i, a in enumerate(args):
            if a == "--count" and i + 1 < len(args):
                count = int(args[i + 1])

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=60, verify=tls_verify()
        ) as c:
            resp = await c.post("/writing/suggest", json={
                "project_id": pid, "count": count,
            })

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        suggestions = data.get("suggestions", [])
        if not suggestions:
            return f"{_C.DIM}No suggestions generated.{_C.RESET}"

        lines = [f"{_C.BOLD}Plot Suggestions{_C.RESET}\n"]
        for i, s in enumerate(suggestions, 1):
            title = s.get("title", f"Suggestion {i}")
            desc = s.get("description", "")
            stype = s.get("type", "")
            lines.append(f"  {_C.YELLOW}{i}. {title}{_C.RESET}  {_C.DIM}[{stype}]{_C.RESET}")
            if desc:
                lines.append(f"     {desc}")
        return "\n".join(lines)

    async def _ai_brainstorm(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Brainstorm ideas."""
        import httpx

        if len(args) < 2:
            return f"Usage: {_C.CYAN}/saga brainstorm <project_id> \"topic\"{_C.RESET}"

        pid = args[0]
        topic = " ".join(args[1:])

        async with httpx.AsyncClient(
            base_url=_saga_url(), headers=_headers(), timeout=60, verify=tls_verify()
        ) as c:
            resp = await c.post("/writing/brainstorm", json={
                "project_id": pid, "topic": topic,
            })

        if resp.status_code != 200:
            return f"{_C.RED}Failed: {resp.status_code}{_C.RESET}"

        data = resp.json()
        ideas = data.get("ideas", [])
        if not ideas:
            return f"{_C.DIM}No ideas generated.{_C.RESET}"

        lines = [f"{_C.BOLD}Brainstorm: {topic}{_C.RESET}\n"]
        for i, idea in enumerate(ideas, 1):
            lines.append(f"  {_C.YELLOW}{i}. {idea.get('title', '?')}{_C.RESET}")
            if idea.get("description"):
                lines.append(f"     {idea['description']}")
            pros = idea.get("pros", [])
            cons = idea.get("cons", [])
            if pros:
                lines.append(f"     {_C.GREEN}+{_C.RESET} {', '.join(pros)}")
            if cons:
                lines.append(f"     {_C.RED}-{_C.RESET} {', '.join(cons)}")
        return "\n".join(lines)
