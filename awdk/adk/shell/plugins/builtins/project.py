"""
Project Plugin for AitherShell
================================

Manage repos, switch project contexts, and scope AI to specific codebases.

Usage:
    /project                         — Show active project + list known projects
    /project list                    — List all registered projects
    /project switch <name>           — Switch active project context
    /project add <name> <url|path>   — Register a project (clone if URL)
    /project clone <url> [name]      — Clone + register + index a repo
    /project remove <name>           — Unregister a project
    /project info [name]             — Show project details (git status, index state)
    /project index [name]            — Re-index project with CodeGraph + Repowise
    /project sync [name]             — Git pull + re-index
    /project scope                   — Show what the AI currently "sees"

Aliases: /proj, /workspace, /ws, /repo
"""

import asyncio
from adk._tls import tls_verify
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand
from adk.shell.config import CONFIG_DIR

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore

PROJECTS_FILE = CONFIG_DIR / "projects.json"


def _api_headers() -> Dict[str, str]:
    """Build auth + project scope headers for API calls."""
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    active = _get_active()
    if active:
        headers["X-Project-Name"] = active
    return headers


@dataclass
class Project:
    name: str
    path: str
    origin: str = ""          # git remote URL
    description: str = ""
    indexed: bool = False     # CodeGraph indexed?
    tenant_id: str = ""       # tenant scope (empty = default)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "origin": self.origin,
            "description": self.description,
            "indexed": self.indexed,
            "tenant_id": self.tenant_id,
            "tags": self.tags,
        }

    @staticmethod
    def from_dict(d: dict) -> "Project":
        return Project(
            name=d["name"],
            path=d["path"],
            origin=d.get("origin", ""),
            description=d.get("description", ""),
            indexed=d.get("indexed", False),
            tenant_id=d.get("tenant_id", ""),
            tags=d.get("tags", []),
        )


def _load_projects() -> Dict[str, Project]:
    if PROJECTS_FILE.exists():
        try:
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            return {k: Project.from_dict(v) for k, v in data.get("projects", {}).items()}
        except Exception:
            pass
    return {}


def _save_projects(projects: Dict[str, Project], active: str = ""):
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active": active,
        "projects": {k: v.to_dict() for k, v in projects.items()},
    }
    PROJECTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_active() -> str:
    if PROJECTS_FILE.exists():
        try:
            data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
            return data.get("active", "")
        except Exception:
            pass
    return ""


def _set_active(name: str):
    projects = _load_projects()
    _save_projects(projects, active=name)


def _git_info(path: str) -> Dict[str, str]:
    """Get git info for a path."""
    info = {"branch": "", "origin": "", "status": "", "last_commit": ""}
    git = shutil.which("git")
    if not git:
        return info
    try:
        result = subprocess.run(
            [git, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()

        result = subprocess.run(
            [git, "remote", "get-url", "origin"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["origin"] = result.stdout.strip()

        result = subprocess.run(
            [git, "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            changes = result.stdout.strip().splitlines()
            info["status"] = f"{len(changes)} changed" if changes else "clean"

        result = subprocess.run(
            [git, "log", "-1", "--format=%h %s (%ar)"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["last_commit"] = result.stdout.strip()
    except Exception:
        pass
    return info


async def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Timed out"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _auto_detect_current() -> Optional[Project]:
    """Try to detect a project from CWD git root."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run(
            [git, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            name = Path(root).name.lower().replace(" ", "-")
            origin = ""
            r2 = subprocess.run(
                [git, "remote", "get-url", "origin"],
                cwd=root, capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0:
                origin = r2.stdout.strip()
            return Project(name=name, path=root, origin=origin)
    except Exception:
        pass
    return None


@dataclass
class ProjectPlugin(SlashCommand):
    name: str = "project"
    description: str = "Manage repos, switch project contexts, scope AI to codebases"
    aliases: List[str] = field(default_factory=lambda: ["proj", "workspace", "ws", "repo"])

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='project',
            description='Manage repos, switch project contexts, scope AI to codebases',
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self._show_current()

        subcmd = args[0].lower()
        rest = args[1:]

        dispatch = {
            "list": self._list,
            "ls": self._list,
            "switch": self._switch,
            "use": self._switch,
            "add": self._add,
            "clone": self._clone,
            "remove": self._remove,
            "rm": self._remove,
            "info": self._info,
            "index": self._index,
            "sync": self._sync,
            "scope": self._scope,
            "pull": self._sync,
            "help": lambda r, c: self._help(),
        }

        handler = dispatch.get(subcmd)
        if handler:
            if asyncio.iscoroutinefunction(handler):
                return await handler(rest, ctx)
            return handler(rest, ctx)

        # If it looks like a project name, treat as switch
        projects = _load_projects()
        if subcmd in projects:
            return self._switch([subcmd], ctx)

        return f"Unknown subcommand: {subcmd}\n\n{self._help()}"

    def _help(self) -> str:
        return """📁 **Project Manager**

| Command | What it does |
|---------|-------------|
| `/project` | Show active project |
| `/project list` | List all registered projects |
| `/project switch <name>` | Switch active project context |
| `/project add <name> <path>` | Register a local project |
| `/project clone <url> [name]` | Clone + register + index a repo |
| `/project remove <name>` | Unregister a project |
| `/project info [name]` | Git status, index state, details |
| `/project index [name]` | Re-index with CodeGraph/Repowise |
| `/project sync [name]` | Git pull + re-index |
| `/project scope` | What the AI currently "sees" |

**Aliases:** `/proj`, `/workspace`, `/ws`, `/repo`

**Context switching** tells the AI which codebase to focus on.
It sets the active CodeGraph root, Repowise scope, and system prompt context.
"""

    # ─── Show current ─────────────────────────────────────────────────────

    def _show_current(self) -> str:
        active = _get_active()
        projects = _load_projects()
        lines = ["📁 **Active Project**\n"]

        if active and active in projects:
            p = projects[active]
            git = _git_info(p.path)
            lines.append(f"  **{p.name}** — `{p.path}`")
            if git["branch"]:
                lines.append(f"  Branch: `{git['branch']}` | {git['status']}")
            if git["last_commit"]:
                lines.append(f"  Last commit: {git['last_commit']}")
            if p.indexed:
                lines.append("  Index: ✅ CodeGraph indexed")
            if p.tags:
                lines.append(f"  Tags: {', '.join(p.tags)}")
        else:
            # Auto-detect from CWD
            detected = _auto_detect_current()
            if detected:
                lines.append(f"  ℹ️ Auto-detected from CWD: **{detected.name}** (`{detected.path}`)")
                lines.append("  Not registered. Run `/project add` to save it.")
            else:
                lines.append("  No active project. Run `/project list` or `/project clone <url>`")

        if projects:
            lines.append(f"\n  {len(projects)} registered project(s) — `/project list`")

        return "\n".join(lines)

    # ─── List ─────────────────────────────────────────────────────────────

    def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        projects = _load_projects()
        active = _get_active()

        if not projects:
            return "No projects registered. Use `/project add <name> <path>` or `/project clone <url>`"

        lines = ["📁 **Registered Projects**\n"]
        for name, p in sorted(projects.items()):
            marker = "▶" if name == active else " "
            exists = "✅" if Path(p.path).exists() else "❌"
            idx = "📊" if p.indexed else "  "
            lines.append(f"  {marker} {exists} {idx} **{name}** — `{p.path}`")
            if p.description:
                lines.append(f"       {p.description}")

        lines.append(f"\n  Active: **{active or '(none)'}** — `/project switch <name>`")
        return "\n".join(lines)

    # ─── Switch ───────────────────────────────────────────────────────────

    def _switch(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: `/project switch <name>`"

        name = args[0].lower()
        projects = _load_projects()

        if name not in projects:
            # Fuzzy match
            matches = [k for k in projects if name in k]
            if len(matches) == 1:
                name = matches[0]
            elif matches:
                return f"Ambiguous: {', '.join(matches)}. Be more specific."
            else:
                return f"Project `{name}` not found. Run `/project list`."

        p = projects[name]
        if not Path(p.path).exists():
            return f"❌ Path no longer exists: `{p.path}`"

        _set_active(name)

        # Set env vars so the LLM and tools pick up the context
        os.environ["AITHER_PROJECT"] = name
        os.environ["AITHER_PROJECT_PATH"] = p.path
        if p.tenant_id:
            os.environ["AITHER_TENANT_ID"] = p.tenant_id

        # Write .aither.yaml-style context for the session
        git = _git_info(p.path)

        lines = [f"✅ Switched to **{name}**\n"]
        lines.append(f"  Path: `{p.path}`")
        if git["branch"]:
            lines.append(f"  Branch: `{git['branch']}` | {git['status']}")
        if git["last_commit"]:
            lines.append(f"  Last commit: {git['last_commit']}")
        lines.append("")
        lines.append("  AI context now scoped to this project.")
        lines.append("  CodeGraph, Repowise, and file operations will target this codebase.")

        return "\n".join(lines)

    # ─── Add ──────────────────────────────────────────────────────────────

    def _add(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 1:
            # Auto-detect from CWD
            detected = _auto_detect_current()
            if detected:
                projects = _load_projects()
                projects[detected.name] = detected
                _save_projects(projects, active=_get_active() or detected.name)
                return f"✅ Registered **{detected.name}** from CWD (`{detected.path}`)"
            return "Usage: `/project add <name> <path>` or run from inside a git repo"

        name = args[0].lower()
        path = args[1] if len(args) > 1 else str(Path.cwd())
        path = str(Path(path).resolve())

        if not Path(path).exists():
            return f"❌ Path doesn't exist: `{path}`"

        projects = _load_projects()
        git = _git_info(path)

        projects[name] = Project(
            name=name,
            path=path,
            origin=git.get("origin", ""),
            description=" ".join(args[2:]) if len(args) > 2 else "",
        )
        _save_projects(projects, active=_get_active() or name)

        return f"✅ Registered **{name}** at `{path}`"

    # ─── Clone ────────────────────────────────────────────────────────────

    async def _clone(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: `/project clone <url> [name] [--index]`"

        url = args[0]
        do_index = "--index" in args
        clean_args = [a for a in args[1:] if not a.startswith("--")]
        name = clean_args[0] if clean_args else url.rstrip("/").split("/")[-1].replace(".git", "").lower()

        git = shutil.which("git")
        if not git:
            return "❌ git not found."

        # Clone into ~/aither/projects/<name>
        projects_dir = CONFIG_DIR / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        target = projects_dir / name

        lines = [f"🔧 **Cloning {name}**\n"]

        if target.exists():
            lines.append(f"  ℹ️ Directory exists: `{target}`. Pulling instead...")
            rc, out, err = await _run([git, "pull"], cwd=str(target))
            lines.append(f"  {'✅' if rc == 0 else '⚠️'} git pull")
        else:
            lines.append(f"  Cloning `{url}` → `{target}`...")
            rc, out, err = await _run([git, "clone", url, str(target)], timeout=300)
            if rc != 0:
                return f"❌ Clone failed:\n{err[:500]}"
            lines.append("  ✅ Cloned")

        # Register
        projects = _load_projects()
        projects[name] = Project(name=name, path=str(target), origin=url)
        _save_projects(projects, active=name)
        lines.append(f"  ✅ Registered and set as active project")

        # Set context
        os.environ["AITHER_PROJECT"] = name
        os.environ["AITHER_PROJECT_PATH"] = str(target)

        # Index if requested
        if do_index:
            lines.append("")
            idx_result = await self._index([name], ctx)
            lines.append(idx_result)

        lines.append(f"\n  Run `/project info {name}` for details.")
        return "\n".join(lines)

    # ─── Remove ───────────────────────────────────────────────────────────

    def _remove(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: `/project remove <name>`"

        name = args[0].lower()
        projects = _load_projects()
        active = _get_active()

        if name not in projects:
            return f"Project `{name}` not found."

        del projects[name]
        new_active = "" if active == name else active
        _save_projects(projects, active=new_active)

        return f"✅ Unregistered **{name}**. Files not deleted."

    # ─── Info ─────────────────────────────────────────────────────────────

    def _info(self, args: List[str], ctx: Dict[str, Any]) -> str:
        name = args[0].lower() if args else _get_active()
        if not name:
            return "Usage: `/project info <name>` or set an active project first"

        projects = _load_projects()
        if name not in projects:
            return f"Project `{name}` not found."

        p = projects[name]
        git = _git_info(p.path)
        path = Path(p.path)

        lines = [f"📁 **{p.name}**\n"]
        lines.append(f"  Path: `{p.path}`")
        lines.append(f"  Exists: {'✅' if path.exists() else '❌'}")
        if p.origin:
            lines.append(f"  Origin: `{p.origin}`")
        if p.description:
            lines.append(f"  Description: {p.description}")
        if p.tenant_id:
            lines.append(f"  Tenant: `{p.tenant_id}`")
        if p.tags:
            lines.append(f"  Tags: {', '.join(p.tags)}")

        lines.append(f"\n  **Git:**")
        lines.append(f"  Branch: `{git['branch'] or 'N/A'}`")
        lines.append(f"  Status: {git['status'] or 'N/A'}")
        lines.append(f"  Last: {git['last_commit'] or 'N/A'}")

        lines.append(f"\n  **Index:** {'✅ Indexed' if p.indexed else '❌ Not indexed'}")
        lines.append(f"  Run `/project index {name}` to index with CodeGraph + Repowise")

        # File stats
        if path.exists():
            py_files = list(path.rglob("*.py"))
            ts_files = list(path.rglob("*.ts")) + list(path.rglob("*.tsx"))
            lines.append(f"\n  **Files:** {len(py_files)} .py, {len(ts_files)} .ts/.tsx")

        return "\n".join(lines)

    # ─── Index ────────────────────────────────────────────────────────────

    async def _index(self, args: List[str], ctx: Dict[str, Any]) -> str:
        name = args[0].lower() if args else _get_active()
        if not name:
            return "Usage: `/project index <name>`"

        projects = _load_projects()
        if name not in projects:
            return f"Project `{name}` not found."

        p = projects[name]
        lines = [f"🔧 **Indexing {name}**\n"]

        # Try CodeGraph via Genesis API
        import httpx
        base_url = ctx.get("config", None)
        genesis = "http://localhost:8100"
        if hasattr(base_url, "url"):
            host = base_url.url.replace("http://", "").replace("https://", "").split(":")[0]
            genesis = f"http://{host}:8100"

        # CodeGraph index
        try:
            async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
                r = await c.post(
                    f"{genesis}/scope/reindex",
                    json={"path": p.path, "force": True},
                    headers=_api_headers(),
                )
                if r.status_code == 200:
                    lines.append("  ✅ CodeGraph indexed")
                else:
                    lines.append(f"  ⚠️ CodeGraph: {r.status_code} {r.text[:200]}")
        except Exception as e:
            lines.append(f"  ℹ️ CodeGraph not reachable ({e})")

        # Repowise onboard
        try:
            async with httpx.AsyncClient(timeout=120, verify=tls_verify()) as c:
                r = await c.post(
                    f"{genesis}/api/v1/tools/call",
                    json={"tool": "onboard_external_repo", "args": {"repo_url": p.origin or p.path, "name": name}},
                    headers=_api_headers(),
                )
                if r.status_code == 200:
                    lines.append("  ✅ Repowise onboarded")
                else:
                    lines.append(f"  ⚠️ Repowise: {r.status_code}")
        except Exception:
            lines.append("  ℹ️ Repowise not reachable")

        # Mark as indexed
        p.indexed = True
        _save_projects(projects, active=_get_active())

        return "\n".join(lines)

    # ─── Sync ─────────────────────────────────────────────────────────────

    async def _sync(self, args: List[str], ctx: Dict[str, Any]) -> str:
        name = args[0].lower() if args else _get_active()
        if not name:
            return "Usage: `/project sync <name>`"

        projects = _load_projects()
        if name not in projects:
            return f"Project `{name}` not found."

        p = projects[name]
        git_bin = shutil.which("git")
        if not git_bin:
            return "❌ git not found."

        lines = [f"🔧 **Syncing {name}**\n"]

        # Git pull
        rc, out, err = await _run([git_bin, "pull", "--rebase"], cwd=p.path)
        if rc == 0:
            lines.append(f"  ✅ git pull: {out.strip().splitlines()[-1] if out.strip() else 'up to date'}")
        else:
            lines.append(f"  ⚠️ git pull: {err[:200]}")

        # Re-index
        idx_result = await self._index([name], ctx)
        lines.append(idx_result)

        return "\n".join(lines)

    # ─── Scope ────────────────────────────────────────────────────────────

    def _scope(self, args: List[str], ctx: Dict[str, Any]) -> str:
        active = _get_active()
        projects = _load_projects()

        lines = ["🔍 **Current AI Scope**\n"]

        env_project = os.environ.get("AITHER_PROJECT", "")
        env_path = os.environ.get("AITHER_PROJECT_PATH", "")
        env_tenant = os.environ.get("AITHER_TENANT_ID", "")

        if active and active in projects:
            p = projects[active]
            lines.append(f"  **Active project:** {p.name}")
            lines.append(f"  **Path:** `{p.path}`")
            if p.tenant_id:
                lines.append(f"  **Tenant:** `{p.tenant_id}`")
            lines.append(f"  **Indexed:** {'✅' if p.indexed else '❌'}")
        else:
            lines.append("  No active project set.")

        lines.append(f"\n  **Environment:**")
        lines.append(f"  AITHER_PROJECT={env_project or '(not set)'}")
        lines.append(f"  AITHER_PROJECT_PATH={env_path or '(not set)'}")
        lines.append(f"  AITHER_TENANT_ID={env_tenant or '(not set)'}")

        cwd_project = _auto_detect_current()
        if cwd_project:
            lines.append(f"\n  **CWD git root:** `{cwd_project.path}` ({cwd_project.name})")

        lines.append("\n  The AI uses these to scope CodeGraph, Repowise, and file operations.")
        lines.append("  Switch with `/project switch <name>` or `/project clone <url>`.")

        return "\n".join(lines)
