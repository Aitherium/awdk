"""
App Plugin for AitherShell
============================

Manage workspace apps created via api.aitherium.com.

For non-technical users: create an app via the portal wizard, then run
``aither app pull <slug>`` to get it running locally with one command.

Usage:
    /app pull <slug>           -- Pull and start a workspace app locally
    /app list                  -- List your workspace apps
    /app status [slug]         -- Check app health
    /app stop <slug>           -- Stop a local app
    /app start <slug>          -- Start a stopped app
    /app logs <slug>           -- Tail app logs
    /app update <slug>         -- Pull latest version and restart
    /app open <slug>           -- Open in browser
    /app delete <slug>         -- Stop and remove all data

Aliases: /apps, /workspace-app
"""

import asyncio
from adk._tls import tls_verify
import json
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore

# ── Paths ─────────────────────────────────────────────────────────────────

AITHER_DIR = Path.home() / ".aither"
APPS_DIR = AITHER_DIR / "apps"
APPS_STATE_FILE = AITHER_DIR / "apps-state.json"

# ── Portal / Genesis URLs ─────────────────────────────────────────────────

PORTAL_URL = os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
GENESIS_URL = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")


def _api_headers() -> Dict[str, str]:
    """Build auth + scope headers for API calls."""
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    tenant = os.environ.get("AITHER_TENANT_ID")
    if tenant and "X-Tenant-ID" not in headers:
        headers["X-Tenant-ID"] = tenant
    return headers


# ── State persistence ─────────────────────────────────────────────────────

def _load_apps_state() -> Dict[str, Any]:
    """Load local apps state from disk."""
    if APPS_STATE_FILE.exists():
        try:
            return json.loads(APPS_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"apps": {}}


def _save_apps_state(state: Dict[str, Any]) -> None:
    """Persist local apps state to disk."""
    APPS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPS_STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# ── ANSI helpers ──────────────────────────────────────────────────────────

class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def _step(msg: str) -> None:
    print(f"  {_C.CYAN}>{_C.RESET} {msg}")


def _ok(msg: str) -> None:
    print(f"  {_C.GREEN}*{_C.RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_C.YELLOW}!{_C.RESET} {msg}")


def _err(msg: str) -> None:
    print(f"  {_C.RED}x{_C.RESET} {msg}")


# ── Docker helpers ────────────────────────────────────────────────────────

def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose_cmd() -> List[str]:
    """Return the docker compose command (v2 preferred)."""
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return ["docker", "compose"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ["docker-compose"]


# ══════════════════════════════════════════════════════════════════════════
# Plugin classes
# ══════════════════════════════════════════════════════════════════════════


class AppPlugin(SlashCommand):
    """Manage workspace apps — pull, start, stop, update."""

    name: str = "app"
    aliases: List[str] = ["apps", "workspace-app"]
    description: str = "Pull and manage workspace apps from api.aitherium.com"
    category: str = "workspace"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='app',
            description='Pull and manage workspace apps from api.aitherium.com',
            aliases=['apps', 'workspace-app'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._list(args)

        sub = args[0].lower()
        dispatch = {
            "pull": self._pull,
            "list": self._list,
            "remote": self._remote,
            "available": self._remote,
            "status": self._status,
            "stop": self._stop,
            "start": self._start,
            "logs": self._logs,
            "update": self._update,
            "open": self._open,
            "delete": self._delete,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:])

        # If first arg looks like a slug, treat as pull
        if not sub.startswith("-"):
            return await self._pull([sub] + args[1:])

        return self.get_help()

    # ── /app remote — list apps available in your portal workspace ───────

    async def _remote(self, args: List[str]) -> str:
        """List apps available in your portal/Genesis workspace."""
        import httpx

        headers = _api_headers()
        apps_found: List[Dict[str, Any]] = []

        # Try Genesis deployments
        for base_url in [GENESIS_URL, f"{PORTAL_URL}/api/bridge/genesis"]:
            try:
                async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                    r = await c.get(f"{base_url}/apps/deployments", headers=headers)
                    if r.status_code == 200:
                        data = r.json()
                        items = data if isinstance(data, list) else data.get("deployments", data.get("workspaces", []))
                        for d in items:
                            apps_found.append({
                                "slug": d.get("slug", d.get("workspace_id", "?")),
                                "name": d.get("name", d.get("slug", "?")),
                                "status": d.get("status", "unknown"),
                                "url": d.get("endpoint_url", d.get("subdomain_url", "")),
                            })
                        break
            except (httpx.ConnectError, httpx.TimeoutException):
                continue

        # Also check catalog
        if not apps_found:
            for base_url in [GENESIS_URL, f"{PORTAL_URL}/api/bridge/genesis"]:
                try:
                    async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                        r = await c.get(f"{base_url}/apps/catalog", headers=headers)
                        if r.status_code == 200:
                            catalog = r.json()
                            for item in (catalog if isinstance(catalog, list) else []):
                                apps_found.append({
                                    "slug": item.get("slug", "?"),
                                    "name": item.get("name", "?"),
                                    "status": "available",
                                    "url": "",
                                })
                            break
                except (httpx.ConnectError, httpx.TimeoutException):
                    continue

        if not apps_found:
            return (
                "No apps found in your workspace.\n\n"
                "  Create one at api.aitherium.com/portal/agents/create\n"
                "  Then run: `/app pull <slug>`"
            )

        # Compare with local
        local_state = _load_apps_state()
        local_slugs = set(local_state.get("apps", {}).keys())

        lines = [f"{_C.BOLD}Available Workspace Apps{_C.RESET}\n"]
        for app in apps_found:
            slug = app["slug"]
            installed = slug in local_slugs
            marker = f"{_C.GREEN}*{_C.RESET}" if installed else f"{_C.GRAY}-{_C.RESET}"
            status = f" [{app['status']}]" if app["status"] != "unknown" else ""
            local_tag = f" {_C.DIM}(installed){_C.RESET}" if installed else ""
            lines.append(f"  {marker} {_C.BOLD}{slug}{_C.RESET} — {app['name']}{status}{local_tag}")
            if app.get("url"):
                lines.append(f"    {_C.DIM}{app['url']}{_C.RESET}")

        lines.append("")
        lines.append(f"  To install locally: {_C.CYAN}/app pull <slug>{_C.RESET}")
        return "\n".join(lines)

    # ── /app pull <slug> ──────────────────────────────────────────────────

    async def _pull(self, args: List[str]) -> str:
        if not args:
            # No slug — show available apps
            remote = await self._remote([])
            return (
                f"{remote}\n\n"
                f"Usage: `/app pull <slug>`\n"
                f"  Pick a slug from the list above, or create a new app at\n"
                f"  api.aitherium.com/portal/agents/create"
            )

        slug = args[0].lower().strip()
        app_dir = APPS_DIR / slug

        print(f"\n{_C.BOLD}Pulling workspace app: {slug}{_C.RESET}\n")

        # 1. Check Docker
        _step("Checking Docker...")
        if not _docker_available():
            _err("Docker is not running.")
            print()
            if platform.system() == "Windows":
                print("  Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/")
            elif platform.system() == "Darwin":
                print("  Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/")
            else:
                print("  Install Docker: https://docs.docker.com/engine/install/")
            return ""
        _ok("Docker is running")

        # 2. Check auth
        _step("Checking authentication...")
        token = None
        if AuthStore:
            token = AuthStore.get_active_token()
        if not token:
            _warn("Not authenticated. Opening browser for sign-in...")
            print()
            print(f"  Visit: {PORTAL_URL}/login")
            print("  After signing in, run this command again.")
            print()
            print("  Or authenticate via CLI:")
            print("    aither setup")
            return ""
        _ok("Authenticated")

        # 3. Fetch app spec from portal/Genesis
        _step(f"Fetching app spec for '{slug}'...")
        spec = await self._fetch_app_spec(slug)
        if not spec:
            _err(f"App '{slug}' not found.")
            print()
            print("  Make sure you've created this app at api.aitherium.com/portal/agents/create")
            print("  The slug is shown in the wizard (e.g., 'acme-hub')")
            return ""
        _ok(f"Found: {spec.get('name', slug)}")

        # 4. Scaffold the app
        _step("Setting up project...")
        if app_dir.exists():
            _warn(f"Directory exists: {app_dir}")
            _step("Updating existing project...")
        else:
            app_dir.mkdir(parents=True, exist_ok=True)

        await self._scaffold_app(slug, spec, app_dir)
        _ok(f"Project ready at {app_dir}")

        # 5. Docker compose up
        _step("Starting containers...")
        compose = _compose_cmd()
        port = spec.get("port", 8900)

        try:
            proc = subprocess.run(
                compose + ["-f", str(app_dir / "docker-compose.yml"), "up", "-d", "--build"],
                cwd=str(app_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                _err("Docker compose failed:")
                print(f"  {proc.stderr[:500]}")
                return ""
        except subprocess.TimeoutExpired:
            _err("Docker compose timed out after 5 minutes")
            return ""

        _ok("Containers started")

        # 6. Wait for health
        _step("Waiting for app to be ready...")
        healthy = await self._wait_for_health(port, timeout=60)
        if healthy:
            _ok("App is healthy")
        else:
            _warn("App may still be starting — check `/app status` in a moment")

        # 7. Save state
        state = _load_apps_state()
        state["apps"][slug] = {
            "name": spec.get("name", slug),
            "port": port,
            "dir": str(app_dir),
            "status": "running" if healthy else "starting",
            "url": f"http://localhost:{port}",
        }
        _save_apps_state(state)

        # 8. Open browser
        url = f"http://localhost:{port}"
        print()
        print(f"  {_C.GREEN}{_C.BOLD}{spec.get('name', slug)} is ready!{_C.RESET}")
        print(f"  {_C.CYAN}{url}{_C.RESET}")
        print()

        try:
            webbrowser.open(url)
        except Exception:
            pass

        return ""

    async def _fetch_app_spec(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch the app spec from Genesis or portal."""
        import httpx

        headers = _api_headers()

        # Try Genesis first (local AitherOS)
        for base_url in [GENESIS_URL, f"{PORTAL_URL}/api/bridge/genesis"]:
            try:
                async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                    # Try self-hosted config endpoint
                    r = await c.post(
                        f"{base_url}/apps/self-hosted/generate-config",
                        json={"slug": slug},
                        headers=headers,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        return {
                            "name": slug,
                            "slug": slug,
                            "port": 8900,
                            "env": data.get("env", ""),
                            "docker_compose": data.get("docker_compose", ""),
                            "quickstart": data.get("quickstart_sh", ""),
                            "source": "genesis-config",
                        }
            except (httpx.ConnectError, httpx.TimeoutException):
                continue

        # Try scaffold-download (get full project as files dict)
        for base_url in [GENESIS_URL, f"{PORTAL_URL}/api/bridge/genesis"]:
            try:
                async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                    r = await c.post(
                        f"{base_url}/apps/scaffold",
                        json={
                            "slug": slug,
                            "name": slug,
                            "description": "",
                            "llm_provider": "portal",
                        },
                        headers=headers,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        return {
                            "name": data.get("name", slug),
                            "slug": slug,
                            "port": 8900,
                            "files": data.get("files", {}),
                            "source": "genesis-scaffold",
                        }
            except (httpx.ConnectError, httpx.TimeoutException):
                continue

        # Try portal deployments API
        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.get(
                    f"{PORTAL_URL}/api/bridge/genesis/apps/deployments",
                    headers=headers,
                )
                if r.status_code == 200:
                    deployments = r.json()
                    items = deployments if isinstance(deployments, list) else deployments.get("deployments", [])
                    for d in items:
                        if d.get("slug") == slug or d.get("deployment_id", "").endswith(slug):
                            return {
                                "name": d.get("name", slug),
                                "slug": slug,
                                "port": d.get("port", 8900),
                                "image": d.get("image", ""),
                                "source": "deployment",
                                **d,
                            }
        except (httpx.ConnectError, httpx.TimeoutException):
            pass

        return None

    async def _scaffold_app(self, slug: str, spec: Dict[str, Any], app_dir: Path) -> None:
        """Write app files to disk from spec."""
        source = spec.get("source", "")

        if source == "genesis-config":
            # Write .env and docker-compose from generated config
            env = spec.get("env", "")
            if env:
                (app_dir / ".env").write_text(env, encoding="utf-8")
            compose = spec.get("docker_compose", "")
            if compose:
                (app_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")
            quickstart = spec.get("quickstart", "")
            if quickstart:
                qs_path = app_dir / "quickstart.sh"
                qs_path.write_text(quickstart, encoding="utf-8")
                try:
                    qs_path.chmod(0o755)
                except OSError:
                    pass
            (app_dir / "data").mkdir(exist_ok=True)

        elif source == "genesis-scaffold":
            # Write individual files from files dict
            files = spec.get("files", {})
            for rel_path, content in files.items():
                fp = app_dir / rel_path
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")
            (app_dir / "data").mkdir(exist_ok=True)

        else:
            # Deployment record — generate minimal compose to pull the image
            image = spec.get("image", f"aitherium/{slug}:latest")
            port = spec.get("port", 8900)
            compose = f"""services:
  {slug}:
    image: {image}
    ports:
      - "{port}:{port}"
    environment:
      - APP_ID={slug}
      - DEPLOYMENT_MODE=self-hosted
      - AITHERIUM_PORTAL_URL={PORTAL_URL}
    volumes:
      - app-data:/app/data
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant-data:/qdrant/storage
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/healthz"]
      interval: 10s
      timeout: 3s
      retries: 3

volumes:
  app-data:
  qdrant-data:
"""
            (app_dir / "docker-compose.yml").write_text(compose, encoding="utf-8")
            (app_dir / "data").mkdir(exist_ok=True)

    async def _wait_for_health(self, port: int, timeout: int = 60) -> bool:
        """Poll health endpoint until it responds or timeout."""
        import httpx

        url = f"http://localhost:{port}/api/health"
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3) as c:
                    r = await c.get(url)
                    if r.status_code < 300:
                        return True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(2)

        return False

    # ── /app list ─────────────────────────────────────────────────────────

    async def _list(self, args: List[str]) -> str:
        state = _load_apps_state()
        apps = state.get("apps", {})

        if not apps:
            return (
                "No workspace apps installed.\n\n"
                "  Create one at api.aitherium.com/portal/agents/create\n"
                "  Then run: `/app pull <slug>`"
            )

        lines = [f"{_C.BOLD}Workspace Apps{_C.RESET}\n"]
        for slug, info in apps.items():
            status = info.get("status", "unknown")
            marker = _C.GREEN + "+" if status == "running" else _C.GRAY + "-"
            name = info.get("name", slug)
            url = info.get("url", f"http://localhost:{info.get('port', 8900)}")
            lines.append(f"  {marker}{_C.RESET} {_C.BOLD}{slug}{_C.RESET} ({name})")
            lines.append(f"    {_C.DIM}{url}{_C.RESET}")

        return "\n".join(lines)

    # ── /app status ───────────────────────────────────────────────────────

    async def _status(self, args: List[str]) -> str:
        state = _load_apps_state()
        apps = state.get("apps", {})

        if args:
            slug = args[0].lower()
            if slug not in apps:
                return f"App '{slug}' not found locally. Run `/app list` to see installed apps."
            info = apps[slug]
            port = info.get("port", 8900)
            healthy = await self._wait_for_health(port, timeout=5)
            status = "running" if healthy else "stopped"
            info["status"] = status
            _save_apps_state(state)
            return (
                f"**{info.get('name', slug)}** ({slug})\n"
                f"  Status: {status}\n"
                f"  URL: http://localhost:{port}\n"
                f"  Dir: {info.get('dir', 'unknown')}"
            )

        # All apps
        lines = []
        for slug, info in apps.items():
            port = info.get("port", 8900)
            healthy = await self._wait_for_health(port, timeout=3)
            status = "running" if healthy else "stopped"
            info["status"] = status
            lines.append(f"  {slug}: {status} (port {port})")
        _save_apps_state(state)
        return "\n".join(lines) if lines else "No apps installed."

    # ── /app stop ─────────────────────────────────────────────────────────

    async def _stop(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app stop <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found. Run `/app list`."

        app_dir = Path(info.get("dir", APPS_DIR / slug))
        compose_file = app_dir / "docker-compose.yml"

        if not compose_file.exists():
            return f"No docker-compose.yml found at {app_dir}"

        compose = _compose_cmd()
        proc = subprocess.run(
            compose + ["-f", str(compose_file), "down"],
            cwd=str(app_dir),
            capture_output=True, text=True, timeout=60,
        )

        info["status"] = "stopped"
        _save_apps_state(state)

        if proc.returncode == 0:
            return f"Stopped **{slug}**."
        return f"Stop may have failed:\n{proc.stderr[:300]}"

    # ── /app start ────────────────────────────────────────────────────────

    async def _start(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app start <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found. Run `/app list`."

        app_dir = Path(info.get("dir", APPS_DIR / slug))
        compose_file = app_dir / "docker-compose.yml"

        if not compose_file.exists():
            return f"No docker-compose.yml found at {app_dir}"

        compose = _compose_cmd()
        proc = subprocess.run(
            compose + ["-f", str(compose_file), "up", "-d"],
            cwd=str(app_dir),
            capture_output=True, text=True, timeout=120,
        )

        if proc.returncode == 0:
            info["status"] = "running"
            _save_apps_state(state)
            port = info.get("port", 8900)
            return f"Started **{slug}** at http://localhost:{port}"
        return f"Start failed:\n{proc.stderr[:300]}"

    # ── /app logs ─────────────────────────────────────────────────────────

    async def _logs(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app logs <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found."

        app_dir = Path(info.get("dir", APPS_DIR / slug))
        compose_file = app_dir / "docker-compose.yml"

        compose = _compose_cmd()
        proc = subprocess.run(
            compose + ["-f", str(compose_file), "logs", "--tail", "50"],
            cwd=str(app_dir),
            capture_output=True, text=True, timeout=15,
        )

        return proc.stdout[-2000:] if proc.stdout else proc.stderr[:500] or "No logs available."

    # ── /app update ───────────────────────────────────────────────────────

    async def _update(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app update <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found."

        app_dir = Path(info.get("dir", APPS_DIR / slug))
        compose_file = app_dir / "docker-compose.yml"

        compose = _compose_cmd()

        # Pull latest images
        subprocess.run(
            compose + ["-f", str(compose_file), "pull"],
            cwd=str(app_dir),
            capture_output=True, text=True, timeout=120,
        )

        # Restart
        subprocess.run(
            compose + ["-f", str(compose_file), "up", "-d", "--build"],
            cwd=str(app_dir),
            capture_output=True, text=True, timeout=300,
        )

        info["status"] = "running"
        _save_apps_state(state)
        return f"Updated **{slug}** to latest version."

    # ── /app open ─────────────────────────────────────────────────────────

    async def _open(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app open <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found."

        port = info.get("port", 8900)
        url = f"http://localhost:{port}"

        try:
            webbrowser.open(url)
            return f"Opening {url}"
        except Exception:
            return f"Open: {url}"

    # ── /app delete ───────────────────────────────────────────────────────

    async def _delete(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/app delete <slug>`"

        slug = args[0].lower()
        state = _load_apps_state()
        info = state.get("apps", {}).get(slug)

        if not info:
            return f"App '{slug}' not found."

        app_dir = Path(info.get("dir", APPS_DIR / slug))
        compose_file = app_dir / "docker-compose.yml"

        # Stop containers
        if compose_file.exists():
            compose = _compose_cmd()
            subprocess.run(
                compose + ["-f", str(compose_file), "down", "-v"],
                cwd=str(app_dir),
                capture_output=True, text=True, timeout=60,
            )

        # Remove directory
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)

        # Remove from state
        del state["apps"][slug]
        _save_apps_state(state)

        return f"Deleted **{slug}** and removed all data."

    # ── /app help ─────────────────────────────────────────────────────────

    async def _help(self, args: List[str]) -> str:
        return self.get_help()

    def get_help(self) -> str:
        return """**Workspace Apps**

| Command | Description |
|---------|-------------|
| `/app pull <slug>` | Pull and start a workspace app locally |
| `/app pull` | Show available apps + prompt to pick one |
| `/app remote` | List apps in your portal workspace |
| `/app list` | List locally installed apps |
| `/app status [slug]` | Check app health |
| `/app start <slug>` | Start a stopped app |
| `/app stop <slug>` | Stop a running app |
| `/app logs <slug>` | Tail app logs |
| `/app update <slug>` | Pull latest version and restart |
| `/app open <slug>` | Open in browser |
| `/app delete <slug>` | Stop and remove all data |

**Getting Started:**
  1. Create an app at api.aitherium.com/portal/agents/create
  2. Run: `/app pull` to see available apps
  3. Run: `/app pull acme-hub` to install
  4. Your app starts at http://localhost:8900

**Multiple Apps:**
  You can pull multiple apps — each runs on its own port.
  `/app pull acme-hub` → port 8900
  `/app pull photo-assistant` → port 8901
  `/app list` shows all installed apps.
"""
