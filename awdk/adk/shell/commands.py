"""
AitherShell Built-in Commands
==============================

Commands that don't require CLI framework:
- help: Show help
- plugins: List/manage plugins
- config: Show/set configuration
- status: Check Genesis health
- history: Show command history
- exit: Exit shell

These are called by cli.py and shell.py.
"""

import httpx
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from adk.shell.config import AitherConfig, CONFIG_FILE, PLUGINS_DIR
from adk.shell.genesis_client import GenesisClient
from adk.shell.plugins import PluginRegistry

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """Command execution error."""
    pass


class Commands:
    """Built-in commands for AitherShell."""

    def __init__(self, config: AitherConfig):
        """Initialize commands.
        
        Args:
            config: AitherConfig instance
        """
        self.config = config
        self.genesis_client = GenesisClient(base_url=config.url)
        self.plugin_registry = PluginRegistry(config.plugin_dirs)

    async def help(self, *args) -> str:
        """Show help for built-in commands.
        
        Returns:
            Help text
        """
        return """
AitherShell Built-in Commands:

help                     Show this help
plugins [list|load]      List/reload plugins
config show              Show local configuration
config set KEY VAL       Set configuration value
status                   Check Genesis health
history [N]              Show command history
exit                     Exit shell

Workspace Management:
  workspace show           Full resolved config for your account
  workspace models         List your private models
  workspace models add X   Add a private model
  workspace models remove X Remove a private model
  workspace wills          List your custom wills
  workspace wills create   Create a custom will
  workspace wills delete   Delete a custom will
  workspace personas       List your custom personas
  workspace safety         Show your safety level
  workspace safety set X   Set safety (professional/casual/unrestricted)
  workspace agents         List your deployed agent-apps
  workspace agents deploy  Deploy an agent-app from catalog
  workspace agents remove  Undeploy an agent-app

Examples:
  aither workspace show
  aither workspace models add myModel_v2.safetensors
  aither workspace safety set unrestricted
  aither workspace agents deploy myapp
  aither workspace wills create my-will "You are a helpful assistant..."
"""

    async def plugins(self, *args) -> str:
        """List or manage plugins.
        
        Args:
            *args: Subcommand and arguments
            
        Returns:
            Plugin information
            
        Raises:
            CommandError: If command fails
        """
        subcommand = args[0] if args else "list"
        
        if subcommand == "list":
            plugins = self.plugin_registry.list_plugins()
            if not plugins:
                return "No plugins loaded."
            
            lines = ["Available plugins:\n"]
            for plugin in plugins:
                aliases = f" (aliases: {', '.join(plugin.aliases)})" if plugin.aliases else ""
                enabled = "" if plugin.enabled else " [DISABLED]"
                lines.append(f"  {plugin.name}{enabled}{aliases}")
                lines.append(f"    {plugin.description}")
            
            return "\n".join(lines)
        
        elif subcommand == "load":
            count = self.plugin_registry.load_plugins()
            return f"Loaded {count} plugins."
        
        else:
            raise CommandError(f"Unknown plugins subcommand: {subcommand}")

    async def config(self, *args) -> str:
        """Show or set configuration.
        
        Args:
            *args: Subcommand and arguments
            
        Returns:
            Configuration information
            
        Raises:
            CommandError: If command fails
        """
        if not args:
            return await self.config("show")
        
        subcommand = args[0]
        
        if subcommand == "show":
            cfg_dict = self.config.to_dict()
            # Redact sensitive values
            if cfg_dict.get("auth_token"):
                cfg_dict["auth_token"] = "[REDACTED]"
            if cfg_dict.get("api_key"):
                cfg_dict["api_key"] = "[REDACTED]"
            return json.dumps(cfg_dict, indent=2, default=str)
        
        elif subcommand == "file":
            return str(CONFIG_FILE)
        
        elif subcommand == "set":
            if len(args) < 3:
                raise CommandError("config set requires KEY and VALUE")
            
            key = args[1]
            val = args[2]
            
            # Type coercion
            if hasattr(self.config, key):
                current = getattr(self.config, key)
                if isinstance(current, bool):
                    val = val.lower() in ("1", "true", "yes")
                elif isinstance(current, int):
                    try:
                        val = int(val)
                    except ValueError:
                        raise CommandError(f"Invalid int value: {val}")
            
            setattr(self.config, key, val)
            
            # Save to ~/.aither/config.yaml
            self._save_config()
            
            return f"Set {key} = {val}"
        
        else:
            raise CommandError(f"Unknown config subcommand: {subcommand}")

    def _save_config(self) -> None:
        """Save current config to ~/.aither/config.yaml."""
        try:
            cfg_dict = self.config.to_dict()
            # Redact sensitive fields
            cfg_dict.pop("auth_token", None)
            cfg_dict.pop("auth_user", None)
            
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg_dict, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise CommandError(f"Failed to save config: {e}")

    async def status(self, *args) -> str:
        """Check Genesis health and show system status.

        Returns:
            Status information
        """
        try:
            healthy = await self.genesis_client.health_check()
            if not healthy:
                return f"Genesis ({self.config.url}): UNREACHABLE ✗"

            # Fetch detailed status
            status_data = await self.genesis_client.get_status()
            if not status_data:
                return f"Genesis ({self.config.url}): HEALTHY ✓"

            lines = [f"Genesis ({self.config.url}): HEALTHY ✓"]
            tracked = status_data.get("tracked_services", "?")
            uptime = status_data.get("uptime_seconds", 0)
            health = status_data.get("health", "unknown")
            pain = status_data.get("children_in_pain", [])

            # Format uptime
            hours, remainder = divmod(int(uptime), 3600)
            minutes, _ = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

            lines.append(f"  Health: {health}  |  Uptime: {uptime_str}  |  Services: {tracked}")
            if pain:
                lines.append(f"  In pain: {', '.join(pain[:5])}")
            return "\n".join(lines)
        except Exception as e:
            return f"Genesis ({self.config.url}): ERROR - {e}"
        finally:
            await self.genesis_client.close()

    async def history(self, *args) -> str:
        """Show command history.
        
        Args:
            *args: Optional count (default: 20)
            
        Returns:
            History text
        """
        try:
            count = int(args[0]) if args else 20
        except ValueError:
            raise CommandError(f"Invalid count: {args[0]}")
        
        history_file = Path(self.config.history_file)
        if not history_file.exists():
            return "History is empty."
        
        try:
            lines = history_file.read_text(encoding="utf-8").splitlines()
            recent = lines[-count:] if count else lines
            
            output = []
            start_idx = len(lines) - len(recent)
            for i, line in enumerate(recent, start=start_idx):
                output.append(f"{i+1:4d}  {line}")
            
            return "\n".join(output)
        except Exception as e:
            logger.error(f"Failed to read history: {e}")
            return f"Error reading history: {e}"

    # ═══════════════════════════════════════════════════════════════════════
    # WORKSPACE CONFIGURATION — self-service wills/models/agents/safety
    # ═══════════════════════════════════════════════════════════════════════

    async def workspace(self, *args) -> str:
        """Manage workspace configuration (wills, models, agents, safety).

        Usage:
          workspace show              Full resolved config
          workspace models            List private models
          workspace models add <name> Add a private model
          workspace models remove <n> Remove a private model
          workspace wills             List custom wills
          workspace wills create <id> Create a will (interactive)
          workspace wills delete <id> Delete a will
          workspace personas          List custom personas
          workspace safety            Show safety level
          workspace safety set <lvl>  Set safety (professional/casual/unrestricted)
          workspace agents            List deployed agent-apps
          workspace agents deploy <s> Deploy an agent-app from catalog
          workspace agents remove <i> Undeploy an agent-app
        """
        if not args:
            return await self.workspace("show")

        sub = args[0]

        if sub == "show":
            return await self._ws_api("GET", "/config/me")

        elif sub == "models":
            if len(args) == 1:
                return await self._ws_api("GET", "/config/me/models")
            action = args[1]
            if action == "add" and len(args) >= 3:
                name = args[2]
                current = json.loads(await self._ws_api("GET", "/config/me/models", raw=True))
                checkpoints = current.get("private_checkpoints", [])
                if name not in checkpoints:
                    checkpoints.append(name)
                current["private_checkpoints"] = checkpoints
                return await self._ws_api("PUT", "/config/me/models", body=current)
            elif action == "remove" and len(args) >= 3:
                name = args[2]
                current = json.loads(await self._ws_api("GET", "/config/me/models", raw=True))
                checkpoints = current.get("private_checkpoints", [])
                checkpoints = [c for c in checkpoints if c != name]
                current["private_checkpoints"] = checkpoints
                return await self._ws_api("PUT", "/config/me/models", body=current)
            elif action == "list":
                return await self._ws_api("GET", "/config/me/models")
            else:
                raise CommandError("Usage: workspace models [add|remove|list] <name>")

        elif sub == "wills":
            if len(args) == 1:
                return await self._ws_api("GET", "/config/me/wills")
            action = args[1]
            if action == "create" and len(args) >= 3:
                will_id = args[2]
                prompt = " ".join(args[3:]) if len(args) > 3 else "Custom will"
                body = {
                    "id": will_id,
                    "name": will_id.replace("-", " ").title(),
                    "base_prompt": prompt,
                }
                return await self._ws_api("POST", "/config/me/wills", body=body)
            elif action == "delete" and len(args) >= 3:
                will_id = args[2]
                return await self._ws_api("DELETE", f"/config/me/wills/{will_id}")
            else:
                raise CommandError("Usage: workspace wills [create|delete] <id> [prompt...]")

        elif sub == "personas":
            if len(args) == 1:
                return await self._ws_api("GET", "/config/me/personas")
            raise CommandError("Usage: workspace personas")

        elif sub == "safety":
            if len(args) == 1:
                return await self._ws_api("GET", "/config/me/safety")
            if args[1] == "set" and len(args) >= 3:
                level = args[2]
                if level not in ("professional", "casual", "unrestricted"):
                    raise CommandError("Level must be: professional, casual, or unrestricted")
                body = {"default_level": level, "allow_nsfw_models": level == "unrestricted",
                        "allow_explicit": level == "unrestricted"}
                return await self._ws_api("PUT", "/config/me/safety", body=body)
            raise CommandError("Usage: workspace safety [set <level>]")

        elif sub == "agents":
            if len(args) == 1:
                return await self._ws_api("GET", "/config/me/agents")
            action = args[1]
            if action == "deploy" and len(args) >= 3:
                slug = args[2]
                body = {"manifest_slug": slug}
                return await self._ws_api("POST", "/config/me/agents", body=body)
            elif action == "remove" and len(args) >= 3:
                agent_id = args[2]
                return await self._ws_api("DELETE", f"/config/me/agents/{agent_id}")
            else:
                raise CommandError("Usage: workspace agents [deploy|remove] <id>")

        else:
            raise CommandError(f"Unknown workspace subcommand: {sub}. Try: workspace show")

    async def _ws_api(self, method: str, path: str, body: dict = None, raw: bool = False) -> str:
        """Call the Genesis workspace config API."""
        client = await self.genesis_client._get_client()
        url = f"{self.genesis_client.base_url}{path}"
        headers = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"

        try:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "PUT":
                resp = await client.put(url, json=body, headers=headers)
            elif method == "POST":
                resp = await client.post(url, json=body, headers=headers)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                raise CommandError(f"Unsupported method: {method}")

            if resp.status_code == 401:
                raise CommandError("Not authenticated. Run: aither login")
            if resp.status_code == 403:
                raise CommandError(f"Permission denied: {resp.json().get('detail', '')}")
            if resp.status_code == 404:
                raise CommandError(f"Not found: {path}")

            if raw:
                return resp.text

            data = resp.json()
            return json.dumps(data, indent=2)
        except httpx.RequestError as e:
            raise CommandError(f"Genesis unreachable: {e}")

    async def resume(self, *args) -> str:
        """Resume a previous session.

        Usage:
            resume <session_id>

        Args:
            *args: session_id

        Returns:
            Confirmation message

        Raises:
            CommandError: If session_id is missing
        """
        if not args or not args[0]:
            raise CommandError("Usage: resume <session_id>")

        session_id = args[0]
        self.config.session_id = session_id
        self.config.last_session_id = session_id

        # Try to save, but don't fail if it doesn't work
        try:
            self._save_config()
        except Exception:
            pass

        return f"Resumed session {session_id}"

    async def research(self, *args) -> str:
        """Research a question using web sources and deliver a written report.

        Usage:
            research "<question>"

        Args:
            *args: question text

        Returns:
            Research result from the chat backend

        Raises:
            CommandError: If question is missing
        """
        if not args or not args[0]:
            raise CommandError("Usage: research <question>")

        question = " ".join(args) if len(args) > 1 else args[0]

        # Frame the question as a research prompt
        research_prompt = (
            "Research the following thoroughly using web sources and deliver "
            "a written report with citations:\n\n"
            f"{question}"
        )

        # Use the chat method with the current session_id
        kwargs = {
            "message": research_prompt,
        }
        if self.config.session_id:
            kwargs["session_id"] = self.config.session_id

        response = await self.genesis_client.chat(**kwargs)
        return response

    async def exit(self, *args) -> str:
        """Exit the shell.

        Raises:
            KeyboardInterrupt: Always raises to signal exit
        """
        raise KeyboardInterrupt()


# Command dispatcher
async def execute_command(
    config: AitherConfig,
    command: str,
    args: Optional[List[str]] = None,
) -> str:
    """Execute a built-in command.
    
    Args:
        config: AitherConfig instance
        command: Command name
        args: Command arguments
        
    Returns:
        Command output
        
    Raises:
        CommandError: If command fails
    """
    commands = Commands(config)
    args = args or []

    # Class-first lookup: instance attributes shadow same-named command
    # methods — `self.config` (an AitherConfig) hid `async def config`, so
    # `config show` failed with "config is not a command" everywhere.
    method = getattr(type(commands), command, None)
    if callable(method):
        method = method.__get__(commands, type(commands))
    else:
        method = getattr(commands, command, None)
    if not method:
        raise CommandError(f"Unknown command: {command}")

    if not callable(method):
        raise CommandError(f"{command} is not a command")
    
    try:
        return await method(*args)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error(f"Command '{command}' failed: {e}")
        raise CommandError(f"Command failed: {e}")
