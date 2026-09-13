"""
Setup Plugin for AitherShell
=============================

One-command setup for Obsidian, MCP, desktop, and other integrations.
No npm, no PowerShell knowledge needed — just `/setup obsidian`.

Usage:
    /setup                     — List available setup targets
    /setup obsidian            — Install Obsidian plugin + scaffold vault + seed config
    /setup obsidian --sync     — Same + pull wiki/KG/memory data into vault
    /setup obsidian --vault PATH  — Use specific vault path
    /setup shell               — Configure shell completions + config
    /setup mcp                 — Configure MCP server for Cursor/VS Code
    /setup desktop             — Install AitherDesktop
    /setup status              — Show what's installed

Aliases: /connect, /install
"""

import asyncio
from adk._tls import tls_verify
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def _find_repo_root() -> Optional[Path]:
    """Locate the AitherOS-Fresh repo root."""
    candidates = []

    root_env = os.environ.get("AITHEROS_ROOT")
    if root_env:
        candidates.append(Path(root_env).parent)
        candidates.append(Path(root_env))

    cwd = Path.cwd()
    candidates.extend([cwd, cwd.parent, cwd.parent.parent])

    # Home- and drive-root candidates, DISCOVERED rather than hardcoded. This block
    # used to name D:/ and C:/ explicitly — one developer's drive layout, shipped in
    # a public package and meaningless on anyone else's machine.
    from adk.shell._repo_roots import candidate_repo_roots

    candidates.extend(candidate_repo_roots(include_cwd=False))

    for c in candidates:
        resolved = c.resolve()
        if (resolved / "AitherOS" / "apps" / "obsidian-aitheros").is_dir():
            return resolved
    return None


def _find_obsidian_vaults() -> List[Dict[str, Any]]:
    """Read Obsidian's config to find known vaults."""
    config_paths = []
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            config_paths.append(Path(appdata) / "obsidian" / "obsidian.json")
    elif platform.system() == "Darwin":
        config_paths.append(
            Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
        )
    else:
        config_paths.append(Path.home() / ".config" / "obsidian" / "obsidian.json")

    for p in config_paths:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                vaults = data.get("vaults", {})
                return [
                    {"id": k, "path": v.get("path", ""), "open": v.get("open", False)}
                    for k, v in vaults.items()
                    if v.get("path")
                ]
            except Exception:
                pass
    return []


def _find_npm() -> Optional[str]:
    return shutil.which("npm")


def _find_node() -> Optional[str]:
    return shutil.which("node")


async def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120) -> tuple:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Timed out"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# Canonical Aitherium cloud endpoints — see AitherOS/config/tunnel-routes.yaml.
# A laptop with no local inference points here: mcp.aitherium.com serves BOTH
# OpenAI-compatible inference (/v1/*) and the MCP protocol (/mcp).
CLOUD_API_URL = "https://mcp.aitherium.com"
CLOUD_MCP_URL = "https://mcp.aitherium.com/mcp"
CLOUD_IDENTITY_URL = "https://idp.aitherium.com"
CLOUD_PORTAL_URL = "https://api.aitherium.com"


def _write_cloud_shell_config() -> Path:
    """Point ~/.aither/shell.yaml at the Aitherium cloud (thin client, no local
    inference). Preserves unrelated keys; (re)writes only the endpoint keys.
    The AitherShell config loader reads api_url/mcp_url/identity_url from here.
    """
    cfg_dir = Path.home() / ".aither"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    shell_yaml = cfg_dir / "shell.yaml"

    existing: Dict[str, str] = {}
    if shell_yaml.exists():
        for line in shell_yaml.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            existing[key.strip()] = value.strip()

    existing.update({
        "api_url": CLOUD_API_URL,
        "mcp_url": CLOUD_MCP_URL,
        "identity_url": CLOUD_IDENTITY_URL,
    })
    body = "\n".join(f"{k}: {v}" for k, v in existing.items()) + "\n"
    shell_yaml.write_text(body, encoding="utf-8")
    return shell_yaml


@dataclass
class SetupPlugin(SlashCommand):
    name: str = "setup"
    description: str = "One-command setup for Obsidian, MCP, desktop, shell"
    aliases: List[str] = field(default_factory=lambda: ["connect", "install"])

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='setup',
            description='One-command setup for Obsidian, MCP, desktop, shell',
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self._help()

        subcmd = args[0].lower()
        rest = args[1:]

        dispatch = {
            "obsidian": self._setup_obsidian,
            "shell": self._setup_shell,
            "mcp": self._setup_mcp,
            "desktop": self._setup_desktop,
            "node": self._setup_node,
            "zero": self._setup_zero,
            "connect": self._setup_connect,
            "vllm": self._setup_vllm,
            "kvcache": self._setup_vllm,
            "aitherium": self._setup_aitherium,
            "aither": self._setup_aitherium,
            "all": self._setup_all,
            "status": self._status,
            "help": lambda r, c: self._help(),
        }

        handler = dispatch.get(subcmd)
        if not handler:
            return f"Unknown target: {subcmd}\n\n{self._help()}"

        return await handler(rest, ctx) if asyncio.iscoroutinefunction(handler) else handler(rest, ctx)

    def _help(self) -> str:
        return """🔧 **AitherOS Setup**

| Command | What it does |
|---------|-------------|
| `/setup obsidian` | Install Obsidian plugin, scaffold vault, seed config |
| `/setup obsidian --sync` | Same + pull wiki/KG/memories into vault |
| `/setup shell` | Configure shell completions + ~/.aither/config.yaml |
| `/setup mcp` | Generate MCP server config for Cursor/VS Code |
| `/setup desktop` | Install AitherDesktop (system tray + hotkeys) |
| `/setup node` | Start awnode (MCP/tooling hub) via Docker |
| `/setup zero` | Bootstrap AitherZero PowerShell automation |
| `/setup connect` | Install Awconnect (SDK + `aither` shell + MCP) |
| `/setup connect --remote` | Cloud thin client — auth + route API/MCP/inference to the cloud (laptop) |
| `/setup vllm` | Deploy vLLM + KVCache for local inference |
| `/setup aitherium` | Full platform bootstrap (all of the above) |
| `/setup status` | Show what's currently installed |
| `/setup all` | Run every setup target in sequence |
"""

    # ─── Obsidian ──────────────────────────────────────────────────────────

    async def _setup_obsidian(self, args: List[str], ctx: Dict[str, Any]) -> str:
        lines: List[str] = []
        sync_data = "--sync" in args
        vault_path = None

        # Parse --vault PATH
        for i, a in enumerate(args):
            if a == "--vault" and i + 1 < len(args):
                vault_path = args[i + 1]

        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find AitherOS repo. Set AITHEROS_ROOT or run from the repo directory."

        plugin_src = repo / "AitherOS" / "apps" / "obsidian-aitheros"
        lines.append("🔧 **Setting up Obsidian + AitherOS plugin**\n")

        # Step 1: Find/create vault
        lines.append("**Step 1: Locate vault**")
        if vault_path:
            vault = Path(vault_path)
        else:
            vaults = _find_obsidian_vaults()
            open_vaults = [v for v in vaults if v["open"]]
            vault = Path(open_vaults[0]["path"]) if open_vaults else (
                Path(vaults[0]["path"]) if vaults else None
            )
            if vault:
                lines.append(f"  ✅ Auto-detected vault: `{vault}`")
            else:
                vault = repo
                if (vault / ".obsidian").is_dir():
                    lines.append(f"  ℹ️ Using repo root as vault: `{vault}`")
                else:
                    vault = repo / "AitherVault"
                    lines.append(f"  ℹ️ Creating new vault: `{vault}`")

        vault = vault.resolve()
        obsidian_dir = vault / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        (obsidian_dir / "plugins").mkdir(exist_ok=True)

        # Step 2: Build plugin
        lines.append("\n**Step 2: Build plugin**")
        node = _find_node()
        if not node:
            return "❌ Node.js not found. Install it: https://nodejs.org/"

        npm = _find_npm()
        if not (plugin_src / "node_modules").exists() and npm:
            lines.append("  Installing dependencies...")
            rc, out, err = await _run([npm, "install", "--silent"], cwd=str(plugin_src))
            if rc != 0:
                lines.append(f"  ⚠️ npm install warning: {err[:200]}")

        lines.append("  Building...")
        esbuild = plugin_src / "esbuild.config.mjs"
        rc, out, err = await _run([node, str(esbuild), "production"], cwd=str(plugin_src))
        main_js = plugin_src / "main.js"
        if main_js.exists():
            sz = main_js.stat().st_size
            lines.append(f"  ✅ Built main.js ({sz:,} bytes)")
        else:
            return f"❌ Build failed:\n{err[:500]}"

        # Step 3: Install plugin files
        lines.append("\n**Step 3: Install to vault**")
        dest = obsidian_dir / "plugins" / "obsidian-aitheros"
        dest.mkdir(parents=True, exist_ok=True)

        for fname in ("manifest.json", "main.js", "styles.css"):
            src = plugin_src / fname
            if src.exists():
                shutil.copy2(str(src), str(dest / fname))

        lines.append(f"  ✅ Plugin installed → `{dest}`")

        # Auto-enable
        cp_json = obsidian_dir / "community-plugins.json"
        plugins = []
        if cp_json.exists():
            try:
                plugins = json.loads(cp_json.read_text(encoding="utf-8"))
            except Exception:
                plugins = []
        if "obsidian-aitheros" not in plugins:
            plugins.append("obsidian-aitheros")
        cp_json.write_text(json.dumps(plugins), encoding="utf-8")
        lines.append("  ✅ Plugin auto-enabled")

        # Step 4: Seed data.json
        lines.append("\n**Step 4: Configure plugin**")
        base_url = ctx.get("config", {})
        base = getattr(base_url, "url", "http://localhost:8001") if hasattr(base_url, "url") else "http://localhost:8001"
        host = base.replace("http://", "").replace("https://", "").split(":")[0]

        settings = {
            "baseUrl": host,
            "lyraWikiPort": 8270,
            "knowledgeGraphPort": 8196,
            "memoryHubPort": 8185,
            "genesisPort": 8100,
            "apiKey": "",
            "useTLS": False,
            "syncFolder": "AitherOS/Wiki",
            "memoriesFolder": "AitherOS/Memories",
            "kgFolder": "AitherOS/KnowledgeGraph",
            "scopeFolder": "AitherOS/Scope",
            "defaultProject": "default",
            "autoSync": True,
            "syncIntervalMinutes": 30,
        }
        (dest / "data.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        lines.append("  ✅ Settings seeded with correct ports")

        # Step 5: Scaffold folders
        lines.append("\n**Step 5: Scaffold vault folders**")
        folders = [
            "AitherOS/Wiki", "AitherOS/KnowledgeGraph", "AitherOS/Memories",
            "AitherOS/Scope", "AitherOS/Agents", "AitherOS/Notebooks",
        ]
        for d in folders:
            (vault / d).mkdir(parents=True, exist_ok=True)

        welcome = vault / "AitherOS" / "Welcome.md"
        if not welcome.exists():
            welcome.write_text(
                "# 🧠 AitherOS Knowledge Vault\n\n"
                "Use **Ctrl+P** → `AitherOS: Check Service Health` to verify connections.\n\n"
                "- 🧠 Brain icon → Graph Explorer\n"
                "- 🔍 Scan-eye icon → AitherScope (codebase viz)\n"
                "- `AitherOS: Sync Wiki Pages` → Pull wiki content\n",
                encoding="utf-8",
            )
        lines.append(f"  ✅ {len(folders)} folders created + Welcome.md")

        # Step 6: Sync data
        if sync_data:
            lines.append("\n**Step 6: Sync data**")
            await self._sync_obsidian_data(vault, host, lines)

        # Summary
        lines.append("\n---")
        lines.append("🚀 **Done!** Restart Obsidian to load the plugin.")
        lines.append("  **Ctrl+P** → `AitherOS: Check Service Health` to verify.")
        return "\n".join(lines)

    async def _sync_obsidian_data(self, vault: Path, host: str, lines: List[str]):
        """Pull wiki/KG/memory data into the vault."""
        import httpx

        # Wiki
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"http://{host}:8270/graph/obsidian?project=default")
                pages = r.json().get("pages", [])
                wiki_dir = vault / "AitherOS" / "Wiki"
                count = 0
                for p in pages:
                    fname = p["title"].replace("/", "_").replace("\\", "_") + ".md"
                    (wiki_dir / fname).write_text(p["content"], encoding="utf-8")
                    count += 1
                lines.append(f"  ✅ Synced {count} wiki pages")
        except Exception:
            lines.append("  ℹ️ LyraWiki not reachable — skipping wiki sync")

        # KG entities
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"http://{host}:8196/api/v1/entities?limit=500")
                entities = r.json().get("entities", [])
                kg_dir = vault / "AitherOS" / "KnowledgeGraph"
                count = 0
                for e in entities:
                    fname = e["name"].replace("/", "_").replace("\\", "_") + ".md"
                    body = f"---\ntags: [{e.get('type', '')}]\n---\n# {e['name']}\n\n{e.get('description', '')}\n"
                    (kg_dir / fname).write_text(body, encoding="utf-8")
                    count += 1
                lines.append(f"  ✅ Synced {count} KG entities")
        except Exception:
            lines.append("  ℹ️ KnowledgeGraph not reachable — skipping")

        # Memories
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"http://{host}:8185/api/v1/memories?limit=100")
                memories = r.json().get("memories", [])
                mem_dir = vault / "AitherOS" / "Memories"
                count = 0
                for m in memories:
                    fname = f"memory_{m['id']}.md"
                    body = f"---\ntags: [memory, {m.get('type', '')}]\n---\n# {m.get('summary', '')}\n\n{m.get('content', '')}\n"
                    (mem_dir / fname).write_text(body, encoding="utf-8")
                    count += 1
                lines.append(f"  ✅ Synced {count} memories")
        except Exception:
            lines.append("  ℹ️ MemoryHub not reachable — skipping")

    # ─── Shell ─────────────────────────────────────────────────────────────

    async def _setup_shell(self, args: List[str], ctx: Dict[str, Any]) -> str:
        from adk.shell.config import ensure_config
        from adk.shell.completions import install_completions

        lines = ["🔧 **Configuring AitherShell**\n"]

        config_path = ensure_config()
        lines.append(f"  ✅ Config: `{config_path}`")

        try:
            installed = install_completions()
            lines.append(f"  ✅ Shell completions: {installed}")
        except Exception as e:
            lines.append(f"  ℹ️ Completions: {e}")

        lines.append("\n  Run `aither` to start an interactive session.")
        return "\n".join(lines)

    # ─── MCP ──────────────────────────────────────────────────────────────

    async def _setup_mcp(self, args: List[str], ctx: Dict[str, Any]) -> str:
        lines = ["🔧 **MCP Server Configuration**\n"]

        repo = _find_repo_root()
        base = ctx.get("config", {})
        url = getattr(base, "url", "http://localhost:8001") if hasattr(base, "url") else "http://localhost:8001"

        # Generate MCP config for common editors
        mcp_config = {
            "mcpServers": {
                "aitheros": {
                    "command": "npx",
                    "args": ["-y", "@aitheros/mcp-server"],
                    "env": {
                        "AITHEROS_URL": url,
                        "AITHEROS_API_KEY": "${AITHER_API_KEY}",
                    },
                }
            }
        }

        # Also show direct HTTP config
        lines.append("**Option 1: HTTP MCP (recommended)**")
        lines.append("Add to your editor's MCP settings:\n")
        lines.append("```json")
        lines.append(json.dumps({
            "mcpServers": {
                "aitheros": {
                    "url": f"{url.rstrip('/')}/mcp",
                    "transport": "streamable-http",
                }
            }
        }, indent=2))
        lines.append("```\n")

        lines.append("**Option 2: stdio MCP**")
        lines.append("```json")
        lines.append(json.dumps(mcp_config, indent=2))
        lines.append("```\n")

        # Try to auto-write for VS Code
        vscode_settings = Path.home() / ".vscode" / "settings.json"
        if vscode_settings.exists():
            lines.append(f"  VS Code settings found at: `{vscode_settings}`")
            lines.append("  Add the above to your settings manually.")

        return "\n".join(lines)

    # ─── Desktop ──────────────────────────────────────────────────────────

    async def _setup_desktop(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find repo root."

        desktop_dir = repo / "AitherOS" / "apps" / "AitherDesktop"
        if not desktop_dir.exists():
            return "❌ AitherDesktop not found at expected path."

        lines = ["🔧 **Installing AitherDesktop**\n"]

        pip = shutil.which("pip") or shutil.which("pip3")
        if not pip:
            return "❌ pip not found."

        lines.append("  Installing via pip...")
        rc, out, err = await _run([pip, "install", "-e", str(desktop_dir)], timeout=180)
        if rc == 0:
            lines.append("  ✅ AitherDesktop installed")
            lines.append("  Run `aither-desktop` or `python -m aither_desktop` to launch.")
            lines.append("  **Win+A** → toggle overlay")
        else:
            lines.append(f"  ❌ Install failed: {err[:300]}")

        return "\n".join(lines)

    # ─── Node ──────────────────────────────────────────────────────────────

    async def _setup_node(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Start awnode via Docker Compose."""
        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find repo root."

        lines = ["🔧 **Setting up awnode**\n"]
        docker = shutil.which("docker")
        if not docker:
            return "❌ Docker not found. Install Docker Desktop: https://docker.com/get-started"

        compose_file = repo / ".DEPLOYMENT" / "compose" / "docker-compose.aitheros.yml"
        if not compose_file.exists():
            compose_file = repo / ".DEPLOYMENT" / "compose" / "docker-compose.node.yml"

        if not compose_file.exists():
            return f"❌ No compose file found in `{repo}/.DEPLOYMENT/compose`"

        lines.append("  Starting awnode container...")
        rc, out, err = await _run(
            [docker, "compose", "-f", str(compose_file), "--project-directory", str(repo), "up", "-d", "aither-node"],
            cwd=str(repo), timeout=180,
        )
        if rc == 0:
            lines.append("  ✅ awnode started")
            lines.append("  Health: https://localhost:8090/health")
            lines.append("  MCP endpoint: https://localhost:8090/mcp")
        else:
            lines.append(f"  ❌ Failed: {err[:300]}")

        return "\n".join(lines)

    # ─── Zero ─────────────────────────────────────────────────────────────

    async def _setup_zero(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Bootstrap AitherZero PowerShell automation framework."""
        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find repo root."

        lines = ["🔧 **Bootstrapping AitherZero**\n"]

        pwsh = shutil.which("pwsh")
        if not pwsh:
            return "❌ PowerShell 7 not found. Install: https://aka.ms/install-powershell"

        bootstrap = repo / "bootstrap.ps1"
        if not bootstrap.exists():
            return f"❌ bootstrap.ps1 not found at `{repo}`"

        profile = "Minimal"
        if "--full" in args:
            profile = "Standard"

        lines.append(f"  Running bootstrap (profile: {profile})...")
        rc, out, err = await _run(
            [pwsh, "-NoProfile", "-File", str(bootstrap),
             "-Mode", "New", "-InstallProfile", profile, "-NonInteractive"],
            cwd=str(repo), timeout=300,
        )
        if rc == 0:
            lines.append("  ✅ AitherZero bootstrapped")
            lines.append("  Run `/zero` to browse 170+ automation scripts")
        else:
            lines.append(f"  ⚠️ Bootstrap finished with warnings")
            if err:
                lines.append(f"  {err[:300]}")

        return "\n".join(lines)

    # ─── Connect ──────────────────────────────────────────────────────────

    async def _setup_connect(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Install Awconnect: the SDK + the `aither` shell + MCP wiring.

        With ``--remote`` (or ``--cloud``) this configures a thin client that
        authenticates to the Aitherium cloud and routes inference + API + MCP
        there — no local inference needed. Use that on a laptop.

            /setup connect --remote     # cloud thin client (recommended on a laptop)
            /setup connect              # local/dev install
            /setup connect --no-node    # skip the optional local Docker node
        """
        remote = "--remote" in args or "--cloud" in args
        lines = ["🔧 **Installing Awconnect**" + (" (cloud)" if remote else "") + "\n"]

        repo = _find_repo_root()
        pip = shutil.which("pip") or shutil.which("pip3")
        if not pip:
            return "❌ pip not found. Install Python 3.10+ first: https://python.org"

        # ── 1. awdk — agent runtime + `adk`/`aither-py` + `mcp setup` ──
        sdk_dir = repo / "awdk" if repo else None
        if sdk_dir and (sdk_dir / "pyproject.toml").exists():
            lines.append("  Installing awdk[shell,node] from source...")
            rc, out, err = await _run([pip, "install", "-e", f"{sdk_dir}[shell,node]"], timeout=300)
        else:
            lines.append("  Installing awdk[shell,node] from PyPI...")
            rc, out, err = await _run([pip, "install", "awdk[shell,node]"], timeout=300)
        lines.append(f"  {'✅' if rc == 0 else '❌'} awdk (SDK + node MCP server)")
        if rc != 0 and err:
            lines.append(f"     {err[:200]}")

        # ── 2. The `aither` REPL — npm @aitherium/shell-cli (the terminal) ──
        npm = _find_npm()
        if npm:
            cli_dir = repo / ".PRODUCTS" / ".AITHERSHELL" / "cli" if repo else None
            if cli_dir and (cli_dir / "package.json").exists():
                lines.append("  Building the `aither` shell from source (npm)...")
                rc, out, err = await _run([npm, "install"], cwd=str(cli_dir), timeout=300)
                if rc == 0:
                    rc, out, err = await _run([npm, "run", "build"], cwd=str(cli_dir), timeout=300)
                if rc == 0:
                    rc, out, err = await _run([npm, "link"], cwd=str(cli_dir), timeout=120)
                lines.append(f"  {'✅' if rc == 0 else '❌'} aither shell (npm link)")
            else:
                lines.append("  Installing the `aither` shell from npm...")
                rc, out, err = await _run([npm, "install", "-g", "@aitherium/shell-cli"], timeout=300)
                lines.append(f"  {'✅' if rc == 0 else '❌'} @aitherium/shell-cli")
            if rc != 0 and err:
                lines.append(f"     {err[:200]}")
        else:
            lines.append("  ⚠️ npm not found — skipping the `aither` REPL.")
            lines.append("     The Python shell is available as `aither-py`. For the full")
            lines.append("     `aither` terminal, install Node.js 18+: https://nodejs.org")

        # ── 3. Configure endpoints ──
        if remote:
            shell_yaml = _write_cloud_shell_config()
            lines.append("")
            lines.append("  ☁️ **Cloud client configured** (no local inference):")
            lines.append(f"     `{shell_yaml}`")
            lines.append(f"     • inference + chat → {CLOUD_API_URL}")
            lines.append(f"     • MCP tools        → {CLOUD_MCP_URL}")
            lines.append(f"     • identity         → {CLOUD_IDENTITY_URL}")
        else:
            lines.append("")
            shell_result = await self._setup_shell(args, ctx)
            lines.append(shell_result)

        # ── 4. Optional local Node (Docker) for offline/basic tools ──
        docker = shutil.which("docker")
        if docker and repo and not remote and "--no-node" not in args:
            lines.append("")
            node_result = await self._setup_node(args, ctx)
            lines.append(node_result)

        # ── 5. Next steps ──
        lines.append("\n---")
        lines.append("🚀 **Awconnect ready!** Next:")
        if remote:
            lines.append(f"  1. `aither login`                       — authorize this device at {CLOUD_PORTAL_URL}")
            lines.append("  2. `aither mcp setup --mode remote`     — wire Claude Code/Cursor to your workspace")
            lines.append("  3. `aither`                             — chat with cloud inference + full MCP tools")
            lines.append("\n  Tip: set `AITHER_REQUIRE_AUTH=1` so the shell always uses your login (no local root).")
        else:
            lines.append("  • `aither login`   — authenticate   ·   `aither` — start chatting")
        return "\n".join(lines)

    # ─── vLLM + KVCache ───────────────────────────────────────────────────

    async def _setup_vllm(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Deploy vLLM with KVCache for local inference."""
        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find repo root."

        docker = shutil.which("docker")
        if not docker:
            return "❌ Docker not found. Install Docker Desktop with GPU support."

        lines = ["🔧 **Setting up vLLM + KVCache**\n"]

        # Check for NVIDIA GPU
        nvidia = shutil.which("nvidia-smi")
        if nvidia:
            rc, out, err = await _run([nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"])
            if rc == 0 and out.strip():
                lines.append(f"  GPU detected: {out.strip().splitlines()[0]}")
            else:
                lines.append("  ⚠️ nvidia-smi found but no GPU detected")
        else:
            lines.append("  ⚠️ No NVIDIA GPU detected — vLLM needs a CUDA GPU")
            lines.append("  For CPU-only, use `/setup node` (Ollama backend)")
            return "\n".join(lines)

        compose_file = repo / ".DEPLOYMENT" / "compose" / "docker-compose.aitheros.yml"
        if not compose_file.exists():
            return "❌ .DEPLOYMENT/compose/docker-compose.aitheros.yml not found"

        # Start vLLM service
        model = "Qwen/Qwen3-8B"
        for i, a in enumerate(args):
            if a == "--model" and i + 1 < len(args):
                model = args[i + 1]

        lines.append(f"  Model: {model}")
        lines.append("  Starting vLLM container (this may pull a large image)...")

        rc, out, err = await _run(
            [docker, "compose", "-f", str(compose_file), "--project-directory", str(repo), "--profile", "gpu", "up", "-d", "aither-vllm"],
            cwd=str(repo), timeout=600,
        )
        if rc == 0:
            lines.append("  ✅ vLLM container started")
        else:
            lines.append(f"  ❌ Failed: {err[:300]}")
            return "\n".join(lines)

        # Start MicroScheduler (routes to vLLM)
        lines.append("  Starting MicroScheduler...")
        rc, out, err = await _run(
            [docker, "compose", "-f", str(compose_file), "--project-directory", str(repo), "up", "-d", "aither-scheduler"],
            cwd=str(repo), timeout=120,
        )
        lines.append(f"  {'✅' if rc == 0 else '❌'} MicroScheduler")

        # Health check
        lines.append("\n  Waiting for vLLM to load model...")
        import httpx
        for attempt in range(30):
            try:
                async with httpx.AsyncClient(timeout=5, verify=tls_verify()) as c:
                    r = await c.get("http://localhost:8000/health")
                    if r.status_code == 200:
                        lines.append("  ✅ vLLM healthy!")
                        break
            except Exception:
                pass
            await asyncio.sleep(10)
        else:
            lines.append("  ⏳ vLLM still loading — check `docker logs aither-vllm`")

        lines.append("\n  **Endpoints:**")
        lines.append("  - vLLM: http://localhost:8000/v1/chat/completions")
        lines.append("  - Scheduler: http://localhost:8001 (effort-based routing)")
        lines.append("  - KVCache: managed internally by vLLM")
        return "\n".join(lines)

    # ─── Aitherium / Full Platform ────────────────────────────────────────

    async def _setup_aitherium(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Full AitherOS platform bootstrap — everything in one shot."""
        repo = _find_repo_root()
        if not repo:
            return "❌ Can't find repo root. Clone it first:\n  `git clone https://github.com/aitheros/AitherOS-Fresh`"

        lines = ["🚀 **AitherOS Full Platform Setup**\n"]
        lines.append(f"  Repo: `{repo}`\n")

        # Phase 1: Bootstrap AitherZero
        lines.append("━━━ Phase 1: AitherZero ━━━")
        result = await self._setup_zero(["--full"] if "--full" in args else [], ctx)
        lines.append(result)

        # Phase 2: Shell + SDK
        lines.append("\n━━━ Phase 2: Awconnect (SDK + Shell) ━━━")
        result = await self._setup_connect(args, ctx)
        lines.append(result)

        # Phase 3: Docker services
        docker = shutil.which("docker")
        if docker:
            compose = repo / ".DEPLOYMENT" / "compose" / "docker-compose.aitheros.yml"
            if compose.exists():
                lines.append("\n━━━ Phase 3: Core Services ━━━")
                lines.append("  Starting core Docker services...")
                rc, out, err = await _run(
                    [docker, "compose", "-f", str(compose), "--project-directory", str(repo), "up", "-d"],
                    cwd=str(repo), timeout=300,
                )
                if rc == 0:
                    lines.append("  ✅ Core services started")
                else:
                    lines.append(f"  ⚠️ Some services may have failed: {err[:200]}")
        else:
            lines.append("\n  ⚠️ Docker not found — skipping service startup")

        # Phase 4: GPU (optional)
        nvidia = shutil.which("nvidia-smi")
        if nvidia and "--no-gpu" not in args:
            lines.append("\n━━━ Phase 4: GPU / vLLM ━━━")
            result = await self._setup_vllm(args, ctx)
            lines.append(result)
        else:
            lines.append("\n  ℹ️ Skipping GPU setup (no NVIDIA GPU or --no-gpu)")

        # Phase 5: Obsidian (optional)
        if "--with-obsidian" in args:
            lines.append("\n━━━ Phase 5: Obsidian ━━━")
            result = await self._setup_obsidian(["--sync"], ctx)
            lines.append(result)

        # Phase 6: MCP config
        lines.append("\n━━━ Phase 6: MCP Configuration ━━━")
        result = await self._setup_mcp(args, ctx)
        lines.append(result)

        # Final status
        lines.append("\n" + "═" * 50)
        lines.append("🎉 **AitherOS Platform Ready**")
        lines.append("")
        lines.append("  `aither`          → Interactive AI shell")
        lines.append("  `aither 'hello'`  → Quick query")
        lines.append("  `/status`         → Check all services")
        lines.append("  `/setup status`   → What's installed")
        if nvidia:
            lines.append("  GPU inference active at localhost:8000")
        lines.append("")
        return "\n".join(lines)

    # ─── All ──────────────────────────────────────────────────────────────

    async def _setup_all(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Run all setup targets."""
        return await self._setup_aitherium(["--with-obsidian"] + args, ctx)

    # ─── Status ────────────────────────────────────────────────────────────

    def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        lines = ["📊 **AitherOS Integration Status**\n"]

        # Shell
        config_dir = Path.home() / ".aither"
        config_file = config_dir / "config.yaml"
        lines.append(f"  **AitherShell:** {'✅ Configured' if config_file.exists() else '❌ Not configured'} (`/setup shell`)")

        # Obsidian
        vaults = _find_obsidian_vaults()
        obs_installed = False
        for v in vaults:
            plugin = Path(v["path"]) / ".obsidian" / "plugins" / "obsidian-aitheros" / "main.js"
            if plugin.exists():
                obs_installed = True
                lines.append(f"  **Obsidian:** ✅ Plugin installed in `{v['path']}`")
                break
        if not obs_installed:
            # Check repo root
            repo = _find_repo_root()
            if repo and (repo / ".obsidian" / "plugins" / "obsidian-aitheros" / "main.js").exists():
                lines.append(f"  **Obsidian:** ✅ Plugin installed in repo vault")
            else:
                lines.append("  **Obsidian:** ❌ Not installed (`/setup obsidian`)")

        # Desktop
        desktop = shutil.which("aither-desktop")
        lines.append(f"  **Desktop:** {'✅ Installed' if desktop else '❌ Not installed'} (`/setup desktop`)")

        # Docker services
        docker = shutil.which("docker")
        if docker:
            try:
                result = subprocess.run(
                    [docker, "ps", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=10,
                )
                containers = result.stdout.strip().splitlines() if result.returncode == 0 else []
                node_up = any("node" in c for c in containers)
                genesis_up = any("genesis" in c for c in containers)
                vllm_up = any("vllm" in c for c in containers)
                sched_up = any("scheduler" in c for c in containers)
                lines.append(f"  **awnode:** {'✅ Running' if node_up else '❌ Not running'} (`/setup node`)")
                lines.append(f"  **Genesis:** {'✅ Running' if genesis_up else '❌ Not running'}")
                lines.append(f"  **vLLM:** {'✅ Running' if vllm_up else '⚪ Not running'} (`/setup vllm`)")
                lines.append(f"  **Scheduler:** {'✅ Running' if sched_up else '❌ Not running'}")
            except Exception:
                lines.append("  **Docker:** ⚠️ Error checking containers")
        else:
            lines.append("  **Docker:** ❌ Not installed")

        # AitherZero
        pwsh = shutil.which("pwsh")
        lines.append(f"  **AitherZero:** {'✅ pwsh available' if pwsh else '❌ PowerShell 7 not found'} (`/setup zero`)")

        # SDK
        import importlib.util

        repo = _find_repo_root()
        sdk_installed = importlib.util.find_spec("adk") is not None and importlib.util.find_spec("adk.client") is not None

        lines.append(
            "  **ADK Client:** ✅ Installed"
            if sdk_installed
            else "  **ADK Client:** ❌ Not installed (`pip install awdk`)"
        )

        # MCP
        lines.append("  **MCP:** Run `/setup mcp` to get config snippet")

        # Repo
        lines.append(f"  **Repo root:** {'`' + str(repo) + '`' if repo else '❌ Not found'}")

        return "\n".join(lines)
