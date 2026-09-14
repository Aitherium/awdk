"""
Agent Plugin for AitherShell
==============================

Dispatch tasks to AitherOS agents and manage workspace agent fleet.

Usage:
    /agent <name> <task...>        -- Dispatch a task to a named agent
    /agents                        -- List workspace agents
    /agents fleet                  -- Show federated fleet from portal
    /agents deploy <slug>          -- Deploy an agent to workspace
    /agents remove <slug>          -- Remove an agent from workspace
    /agents catalog                -- Browse available agent catalog
    /agents whoami                 -- Show current scope and identity
    /agents install <path>         -- Install agent package to ~/.aitheros/
    /agents run <name>             -- Run an installed agent interactively
    /agents local                  -- List locally installed agents
    /agents register <name>        -- Register agent with Genesis
    /agents uninstall <name>       -- Remove installed agent
    /agents info <name>            -- Show agent identity details

Aliases: /a (for /agent)
"""

import json
from adk._tls import tls_verify
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")


def _api_headers() -> Dict[str, str]:
    """Auth + scope headers for Genesis API calls."""
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
        if profile and profile.get("workspace_id"):
            headers["X-Workspace-ID"] = profile["workspace_id"]
    tenant = os.environ.get("AITHER_TENANT_ID")
    if tenant and "X-Tenant-ID" not in headers:
        headers["X-Tenant-ID"] = tenant
    workspace = os.environ.get("AITHER_WORKSPACE_ID")
    if workspace and "X-Workspace-ID" not in headers:
        headers["X-Workspace-ID"] = workspace
    return headers


class AgentPlugin(SlashCommand):
    """Dispatch tasks to agents via /agent <name> <task>."""
    name: str = "agent"
    aliases: List[str] = ["a"]
    description: str = "Dispatch a task to a named AitherOS agent"
    category: str = "agents"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='agent',
            description='Dispatch a task to a named AitherOS agent',
            aliases=['a'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        agent_name = args[0].lower()
        rest = list(args[1:])

        # /agent <name> --expedition <id> <task…> — charge the run to an
        # expedition budget envelope (AgentForge reads context.expedition_id).
        expedition_id = ""
        if "--expedition" in rest:
            i = rest.index("--expedition")
            if i + 1 >= len(rest):
                return "Usage: /agent <name> --expedition <id> <task>"
            expedition_id = rest[i + 1]
            del rest[i:i + 2]

        task = " ".join(rest)

        if not task:
            return f"Usage: /agent {agent_name} [--expedition <id>] <task>\n  Example: /agent {agent_name} find all infrastructure services"

        return await self._dispatch(agent_name, task, expedition_id=expedition_id)

    async def _dispatch(self, agent_name: str, task: str, expedition_id: str = "") -> str:
        import httpx

        url = f"{_genesis_url()}/forge/dispatch/sync"
        payload = {
            # ForgeDispatchRequest's field is agent_type — the old "agent" key
            # was silently DROPPED by pydantic, so every named dispatch actually
            # ran agent_type="auto" (MCTS routing) instead of the named agent.
            "agent_type": agent_name,
            "task": task,
            "parent_agent": "system",
            "effort_level": 5,
        }
        if expedition_id:
            payload["context"] = {"expedition_id": expedition_id}

        try:
            async with httpx.AsyncClient(timeout=120, verify=tls_verify()) as c:
                r = await c.post(url, json=payload, headers=_api_headers())
                if r.status_code != 200:
                    return f"Dispatch failed (HTTP {r.status_code}): {r.text[:300]}"

                data = r.json()
                result = data.get("result") or data.get("answer") or data.get("response") or ""
                status = data.get("status", "unknown")
                agent = data.get("resolved_agent") or data.get("agent_type") or agent_name
                tokens = data.get("tokens_used", 0)

                lines = [f"**{agent}** [{status}]"]
                if tokens:
                    lines[0] += f" ({tokens} tokens)"
                lines.append("")
                lines.append(result if result else "(no response)")

                if data.get("error"):
                    lines.append(f"\nError: {data['error']}")

                return "\n".join(lines)

        except httpx.TimeoutException:
            return f"Agent dispatch timed out after 120s. The task may still be running — check `/agents`."
        except Exception as e:
            return f"Dispatch error: {e}"

    def get_help(self) -> str:
        return """**Agent Dispatch**

| Command | Description |
|---------|-------------|
| `/agent <name> <task>` | Dispatch a task to a named agent |
| `/agent <name> --expedition <id> <task>` | Dispatch charged to an expedition budget |
| `/agents` | List workspace agents |
| `/agents fleet` | Show federated fleet |
| `/agents deploy <slug>` | Deploy agent to workspace |
| `/agents remove <slug>` | Remove agent from workspace |
| `/agents catalog` | Browse available agents |
| `/agents whoami` | Show current scope |

**Examples:**
  `/agent atlas list all infrastructure services`
  `/agent demiurge write a health check endpoint`
  `/agent iris summarize today's activity`
"""


class AgentsPlugin(SlashCommand):
    """Manage workspace agent fleet via /agents."""
    name: str = "agents"
    aliases: List[str] = []
    description: str = "List and manage workspace agents"
    category: str = "agents"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='agents',
            description='List and manage workspace agents',
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._list_agents()

        sub = args[0].lower()
        dispatch = {
            "fleet": self._fleet,
            "deploy": self._deploy,
            "remove": self._remove,
            "catalog": self._catalog,
            "whoami": self._whoami,
            "help": self._help,
            "install": self._install,
            "run": self._run_agent,
            "local": self._list_local,
            "ls": self._list_local,
            "register": self._register,
            "uninstall": self._uninstall,
            "info": self._agent_info,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:])

        return await self._list_agents()

    async def _list_agents(self) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/workspace/agents",
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"Failed to list agents: HTTP {r.status_code}"

                data = r.json()
                agents = data.get("agents", [])
                if not agents:
                    return "No agents deployed. Use `/agents catalog` to browse, `/agents deploy <slug>` to add."

                lines = [f"**Workspace Agents** ({len(agents)})\n"]
                for a in agents:
                    status = a.get("status", "unknown")
                    marker = "+" if status in ("available", "online", "running") else "-"
                    agent_type = a.get("type", "")
                    type_tag = f" [{agent_type}]" if agent_type else ""
                    desc = a.get("description") or a.get("role") or ""
                    desc_text = f" — {desc[:60]}" if desc else ""
                    lines.append(f"  {marker} **{a.get('slug', '?')}**{type_tag}{desc_text}")

                return "\n".join(lines)

        except Exception as e:
            return f"Error: {e}"

    async def _fleet(self, args: List[str]) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/workspace/agents",
                    headers=_api_headers(),
                )
                local = r.json().get("agents", []) if r.status_code == 200 else []

            # Also try portal fleet
            portal_agents = []
            try:
                async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as c:
                    veil_url = os.environ.get("AITHER_VEIL_URL", "http://localhost:3000")
                    r2 = await c.get(
                        f"{veil_url}/api/aitherconnect/fleet",
                        headers=_api_headers(),
                    )
                    if r2.status_code == 200:
                        portal_data = r2.json()
                        portal_agents = portal_data.get("agents", [])
            except Exception:
                pass

            # Fetch workspace cycles with fleet bindings
            cycle_bindings: Dict[str, List[str]] = {}  # agent_id -> [cycle summaries]
            try:
                async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as c:
                    # Query all workspaces for cycles — Genesis iterates them
                    r3 = await c.get(
                        f"{_genesis_url()}/fleet/agents",
                        headers=_api_headers(),
                    )
                    if r3.status_code == 200:
                        fleet_agents = r3.json().get("agents", [])
                        for fa in fleet_agents:
                            name = fa.get("name", "")
                            invoke = fa.get("invoke_url", "")
                            if invoke and name:
                                cycle_bindings.setdefault(name, []).append(invoke)
            except Exception:
                pass

            lines = ["**Fleet Overview**\n"]
            lines.append(f"  Local workspace: {len(local)} agents")
            if portal_agents:
                lines.append(f"  Portal federated: {len(portal_agents)} agents")
            if cycle_bindings:
                lines.append(f"  Cycle-bound agents: {len(cycle_bindings)}")
            lines.append("")

            if local:
                lines.append("**Local:**")
                for a in local:
                    slug = a.get("slug", "?")
                    line = f"  - {slug} ({a.get('type', 'identity')})"
                    if slug in cycle_bindings:
                        line += f" [fleet: {cycle_bindings[slug][0][:40]}...]"
                    lines.append(line)

            if portal_agents:
                lines.append("\n**Federated:**")
                for a in portal_agents[:20]:
                    name = a.get("name", "?")
                    node = a.get("node_id", "?")
                    url = a.get("invoke_url", "")
                    line = f"  - {name} (node: {node})"
                    if url:
                        line += f" @ {url[:50]}"
                    lines.append(line)

            return "\n".join(lines)

        except Exception as e:
            return f"Fleet error: {e}"

    async def _deploy(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/agents deploy <slug>`\n  Example: `/agents deploy atlas`"

        import httpx
        slug = args[0].lower()

        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                r = await c.post(
                    f"{_genesis_url()}/workspace/agents/deploy",
                    json={"slug": slug},
                    headers=_api_headers(),
                )
                if r.status_code == 409:
                    return f"Agent '{slug}' is already deployed in this workspace."
                if r.status_code == 404:
                    return f"Agent '{slug}' not found. Use `/agents catalog` to see available agents."
                if r.status_code != 200:
                    return f"Deploy failed (HTTP {r.status_code}): {r.text[:200]}"

                data = r.json()
                return f"Deployed **{slug}** to workspace ({data.get('type', 'identity')})"

        except Exception as e:
            return f"Deploy error: {e}"

    async def _remove(self, args: List[str]) -> str:
        if not args:
            return "Usage: `/agents remove <slug>`"

        import httpx
        slug = args[0].lower()

        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.delete(
                    f"{_genesis_url()}/workspace/agents/{slug}",
                    headers=_api_headers(),
                )
                if r.status_code == 404:
                    return f"Agent '{slug}' is not in the workspace roster."
                if r.status_code != 200:
                    return f"Remove failed (HTTP {r.status_code}): {r.text[:200]}"

                return f"Removed **{slug}** from workspace."

        except Exception as e:
            return f"Remove error: {e}"

    async def _catalog(self, args: List[str]) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/workspace/agents/catalog",
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"Catalog error: HTTP {r.status_code}"

                data = r.json()
                catalog = data.get("catalog", [])
                if not catalog:
                    return "No agents available in catalog."

                deployed_count = data.get("deployed_count", 0)
                lines = [f"**Agent Catalog** ({len(catalog)} available, {deployed_count} deployed)\n"]

                for a in catalog:
                    marker = "[deployed]" if a.get("deployed") else ""
                    desc = a.get("description", "")[:50]
                    lines.append(f"  - **{a.get('slug', '?')}** {marker} — {desc}")

                lines.append(f"\nDeploy with: `/agents deploy <slug>`")
                return "\n".join(lines)

        except Exception as e:
            return f"Catalog error: {e}"

    async def _whoami(self, args: List[str]) -> str:
        tenant = os.environ.get("AITHER_TENANT_ID", "(not set)")
        workspace = os.environ.get("AITHER_WORKSPACE_ID", "(not set)")
        user = os.environ.get("AITHER_USER_ID", "(not set)")

        lines = [
            "**Current Scope**",
            f"  Tenant:    {tenant}",
            f"  Workspace: {workspace}",
            f"  User:      {user}",
        ]

        # Try loading from ~/.aither/scope.env
        scope_file = os.path.expanduser("~/.aither/scope.env")
        if os.path.isfile(scope_file):
            lines.append(f"\n  Scope file: {scope_file}")
            try:
                with open(scope_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, _, val = line.partition("=")
                            lines.append(f"    {key.strip()} = {val.strip()}")
            except Exception:
                pass

        # Check portal token
        token_file = os.path.expanduser("~/.aither/portal.token")
        if os.path.isfile(token_file):
            lines.append(f"\n  Portal token: {token_file} (present)")
        else:
            lines.append(f"\n  Portal token: not found (run `aithershell login`)")

        return "\n".join(lines)

    # ── Agent Lifecycle (install / run / local / register / uninstall / info) ──

    @staticmethod
    def _aitheros_home() -> Path:
        return Path.home() / ".aitheros"

    async def _install(self, args: List[str]) -> str:
        """Install an agent package from a local directory or zip."""
        if not args:
            return (
                "Usage: `/agents install <path>`\n"
                "  Path to an extracted agent package directory or .zip"
            )
        src = Path(args[0]).expanduser().resolve()
        if not src.exists():
            return f"Path not found: {src}"

        # If it's a zip, extract to a temp dir
        extract_dir = src
        if src.suffix == ".zip":
            import tempfile
            import zipfile
            tmp = Path(tempfile.mkdtemp(prefix="aither-agent-"))
            try:
                with zipfile.ZipFile(src) as zf:
                    zf.extractall(tmp)
            except zipfile.BadZipFile:
                return f"Invalid zip file: {src}"
            # Find the package root (may be nested one level)
            subdirs = [d for d in tmp.iterdir() if d.is_dir()]
            extract_dir = subdirs[0] if len(subdirs) == 1 else tmp

        identity_file = extract_dir / "identity.yaml"
        if not identity_file.exists():
            return f"No identity.yaml found in {extract_dir}. Not a valid agent package."

        # Read identity to get agent name
        try:
            import yaml
            data = yaml.safe_load(identity_file.read_text(encoding="utf-8")) or {}
        except Exception as e:
            return f"Failed to parse identity.yaml: {e}"
        agent_name = data.get("name", extract_dir.name)
        agent_id = agent_name.lower().replace(" ", "-")

        home = self._aitheros_home()
        # Copy identity
        id_dest = home / "identities"
        id_dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(identity_file, id_dest / f"{agent_id}.yaml")

        # Copy tool pack if present
        toolpack = extract_dir / ".toolpack.yaml"
        if toolpack.exists():
            pack_dest = home / "packs" / agent_id
            pack_dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(toolpack, pack_dest / ".toolpack.yaml")

        # Copy config.yaml if present
        config_file = extract_dir / "config.yaml"
        if config_file.exists():
            cfg_dest = home / "agents" / agent_id
            cfg_dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_file, cfg_dest / "config.yaml")

        return (
            f"Installed **{agent_name}** (`{agent_id}`)\n"
            f"  Identity: {id_dest / f'{agent_id}.yaml'}\n"
            f"  Run with: `/agents run {agent_id}`"
        )

    async def _run_agent(self, args: List[str]) -> str:
        """Run an installed agent interactively."""
        if not args:
            return "Usage: `/agents run <name>`"

        agent_name = args[0].lower()

        # Try loading via ADK
        try:
            from adk.identity import load_identity
            from adk.agent import AitherAgent

            identity = load_identity(agent_name)
            no_data = (not identity.system_prompt
                       and not identity.description)
            if identity.name == agent_name and no_data:
                return (
                    f"Agent '{agent_name}' not found. "
                    "Use `/agents local` to see installed agents."
                )

            # Validate the agent loads successfully
            AitherAgent(
                name=identity.name,
                identity=identity,
                load_packs=True,
            )
            return (
                f"Agent **{identity.name}** loaded.\n"
                f"  Role: {identity.role}\n"
                f"  Description: {identity.description[:100] or '(none)'}\n"
                f"  Skills: {', '.join(identity.skills[:5]) or '(none)'}\n\n"
                f"Use `/agent {agent_name} <task>` to dispatch."
            )
        except ImportError:
            return (
                "awdk is not installed. Install it to "
                "run agents locally:\n"
                "  `pip install awdk`\n\n"
                "Or run the agent script directly:\n"
                "  `python agent.py` (from the package directory)"
            )
        except Exception as e:
            return f"Failed to load agent '{agent_name}': {e}"

    async def _list_local(self, args: List[str]) -> str:
        """List locally installed agents from ~/.aitheros/identities/."""
        id_dir = self._aitheros_home() / "identities"
        if not id_dir.exists():
            return "No agents installed locally. Use `/agents install <path>` to install one."

        files = sorted(id_dir.glob("*.yaml")) + sorted(id_dir.glob("*.yml"))
        if not files:
            return "No agents installed locally. Use `/agents install <path>` to install one."

        lines = [f"**Locally Installed Agents** ({len(files)})\n"]
        for f in files:
            try:
                import yaml
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                name = data.get("name", f.stem)
                role = data.get("role", "assistant")
                desc = data.get("description", "")[:50]
                lines.append(f"  - **{f.stem}** ({role}) — {desc or name}")
            except Exception:
                lines.append(f"  - **{f.stem}** (parse error)")

        # Also check ADK bundled identities
        try:
            from adk.identity import list_identities
            adk_ids = list_identities()
            local_stems = {f.stem for f in files}
            bundled = [i for i in adk_ids if i not in local_stems]
            if bundled:
                lines.append(f"\n**ADK Bundled** ({len(bundled)}): {', '.join(bundled[:10])}")
                if len(bundled) > 10:
                    lines.append(f"  ... and {len(bundled) - 10} more")
        except ImportError:
            pass

        return "\n".join(lines)

    async def _register(self, args: List[str]) -> str:
        """Register a local agent with Genesis."""
        if not args:
            return "Usage: `/agents register <name>`"

        agent_name = args[0].lower()
        id_file = self._aitheros_home() / "identities" / f"{agent_name}.yaml"
        if not id_file.exists():
            return (
                f"Agent '{agent_name}' not found locally. "
                "Install it first with `/agents install`."
            )

        try:
            import yaml
            data = yaml.safe_load(id_file.read_text(encoding="utf-8")) or {}
        except Exception as e:
            return f"Failed to read identity: {e}"

        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                r = await c.post(
                    f"{_genesis_url()}/agents/store/register",
                    json={
                        "slug": agent_name,
                        "name": data.get("name", agent_name),
                        "role": data.get("role", "assistant"),
                        "description": data.get("description", ""),
                        "skills": data.get("skills", []),
                        "type": "user-installed",
                        "identity_yaml": yaml.dump(data),
                    },
                    headers=_api_headers(),
                )
                if r.status_code == 200:
                    return f"Registered **{agent_name}** with Genesis."
                if r.status_code == 409:
                    return f"Agent '{agent_name}' is already registered."
                return f"Registration failed (HTTP {r.status_code}): {r.text[:200]}"
        except Exception as e:
            return f"Cannot reach Genesis: {e}"

    async def _uninstall(self, args: List[str]) -> str:
        """Remove a locally installed agent."""
        if not args:
            return "Usage: `/agents uninstall <name>`"

        agent_name = args[0].lower()
        home = self._aitheros_home()
        removed = []

        id_file = home / "identities" / f"{agent_name}.yaml"
        if id_file.exists():
            id_file.unlink()
            removed.append("identity")

        pack_dir = home / "packs" / agent_name
        if pack_dir.exists():
            shutil.rmtree(pack_dir)
            removed.append("tool packs")

        cfg_dir = home / "agents" / agent_name
        if cfg_dir.exists():
            shutil.rmtree(cfg_dir)
            removed.append("config")

        if not removed:
            return f"Agent '{agent_name}' not found locally."
        return f"Uninstalled **{agent_name}** (removed: {', '.join(removed)})"

    async def _agent_info(self, args: List[str]) -> str:
        """Show details about an installed agent."""
        if not args:
            return "Usage: `/agents info <name>`"

        agent_name = args[0].lower()

        # Try local first
        id_file = self._aitheros_home() / "identities" / f"{agent_name}.yaml"
        source = "local"
        data = None

        if id_file.exists():
            try:
                import yaml
                data = yaml.safe_load(id_file.read_text(encoding="utf-8")) or {}
            except Exception as e:
                return f"Failed to parse identity: {e}"
        else:
            # Try ADK bundled
            try:
                from adk.identity import load_identity
                identity = load_identity(agent_name)
                if identity.name != agent_name or identity.system_prompt or identity.description:
                    data = {
                        "name": identity.name, "role": identity.role,
                        "description": identity.description, "skills": identity.skills,
                        "system_prompt": identity.system_prompt[:200] + (
                        "..." if len(identity.system_prompt) > 200 else ""
                    ),
                        "capabilities": identity.capabilities, "version": identity.version,
                    }
                    source = "adk-bundled"
            except ImportError:
                pass

        if not data:
            return f"Agent '{agent_name}' not found."

        lines = [f"**{data.get('name', agent_name)}** ({source})"]
        if data.get("role"):
            lines.append(f"  Role: {data['role']}")
        if data.get("description"):
            lines.append(f"  Description: {data['description'][:120]}")
        if data.get("skills"):
            skills = data["skills"][:8]
            lines.append(f"  Skills: {', '.join(skills)}")
        spirit = data.get("spirit_snapshot", {})
        if spirit:
            if spirit.get("core_trait"):
                lines.append(f"  Core trait: {spirit['core_trait']}")
            if spirit.get("temperament"):
                lines.append(f"  Temperament: {spirit['temperament']}")
        if data.get("system_prompt"):
            prompt = data["system_prompt"][:150]
            ellipsis = "..." if len(data["system_prompt"]) > 150 else ""
            lines.append(f"  System prompt: {prompt}{ellipsis}")
        if data.get("capabilities"):
            lines.append(f"  Capabilities: {', '.join(data['capabilities'][:5])}")
        if data.get("version"):
            lines.append(f"  Version: {data['version']}")
        return "\n".join(lines)

    async def _help(self, args: List[str]) -> str:
        return self.get_help()

    def get_help(self) -> str:
        return """**Agent Management**

| Command | Description |
|---------|-------------|
| `/agent <name> <task>` | Dispatch a task to a named agent |
| `/agent <name> --expedition <id> <task>` | Dispatch charged to an expedition budget |
| `/agents` | List workspace agents |
| `/agents fleet` | Show federated fleet |
| `/agents deploy <slug>` | Deploy agent to workspace |
| `/agents remove <slug>` | Remove agent from workspace |
| `/agents catalog` | Browse available agents |
| `/agents whoami` | Show current scope |
| `/agents install <path>` | Install agent package locally |
| `/agents run <name>` | Load an installed agent |
| `/agents local` | List locally installed agents |
| `/agents register <name>` | Register agent with Genesis |
| `/agents uninstall <name>` | Remove installed agent |
| `/agents info <name>` | Show agent identity details |

**Examples:**
  `/agent atlas list all infrastructure services`
  `/agents install ./my-agent/`
  `/agents run my-agent`
"""
