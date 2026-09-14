"""
AitherShell Plugin System
=========================

Plugins are slash commands that extend the shell.

Plugin types:
1. YAML plugins (~/.aither/plugins/*.yaml) — declarative API calls
2. Python plugins (~/.aither/plugins/*.py) — full code plugins

YAML Plugin Format:
    name: deploy-status
    description: Check deployment status
    aliases: [ds, deploy]
    action:
      type: api
      method: GET
      url: "{genesis}/deploy/status"
    output: json

Python Plugin Format:
    # ~/.aither/plugins/my_plugin.py
    from adk.shell.plugins import SlashCommand

    class DeployStatus(SlashCommand):
        name = "deploy-status"
        description = "Check deployment status"
        aliases = ["ds"]

        async def run(self, args: list[str], ctx: dict) -> str:
            # ctx has: client, config, session
            result = await ctx["client"].get("/deploy/status")
            return result.text
"""

import importlib.util
import sys

import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import logging

logger = logging.getLogger("adk.shell.plugins")


@dataclass
class SlashCommand:
    """Base class for slash commands (plugins)."""
    name: str = ""
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    hidden: bool = False

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        """Execute the command. Return text to display, or None."""
        raise NotImplementedError

    @property
    def all_names(self) -> List[str]:
        return [self.name] + self.aliases


@dataclass
class YAMLCommand(SlashCommand):
    """Slash command loaded from YAML definition."""
    action_type: str = "api"
    method: str = "GET"
    url_template: str = ""
    body_template: Optional[str] = None
    output_format: str = "text"

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        import httpx
        url = self.url_template.replace("{genesis}", ctx["config"].url)
        url = url.replace("{will}", ctx["config"].will_url)

        # Substitute {args[0]}, {args[1]}, etc.
        for i, arg in enumerate(args):
            url = url.replace(f"{{args[{i}]}}", arg)

        # Simple query param from remaining args
        if args and "{query}" in url:
            url = url.replace("{query}", " ".join(args))

        async with httpx.AsyncClient(timeout=15.0) as client:
            if self.method.upper() == "POST":
                body = {}
                if self.body_template:
                    body = yaml.safe_load(
                        self.body_template.replace("{query}", " ".join(args))
                    )
                resp = await client.post(url, json=body)
            else:
                resp = await client.get(url)

            if self.output_format == "json":
                import json
                return json.dumps(resp.json(), indent=2, default=str)
            return resp.text


class PluginRegistry:
    """Discovers and manages slash command plugins."""

    def __init__(self, plugin_dirs: List[str]):
        self._commands: Dict[str, SlashCommand] = {}
        self._dirs = [Path(d).expanduser() for d in plugin_dirs]

    def load_all(self):
        """Scan plugin directories and load all commands."""
        # Load built-in plugins first
        self._load_builtins()

        # Then user plugin directories
        for d in self._dirs:
            if not d.exists():
                continue
            # YAML plugins
            for f in d.glob("*.yaml"):
                self._load_yaml_plugin(f)
            for f in d.glob("*.yml"):
                self._load_yaml_plugin(f)
            # Python plugins
            for f in d.glob("*.py"):
                self._load_python_plugin(f)

    def _load_builtins(self):
        """Load built-in plugins shipped with AitherShell."""
        builtins_dir = Path(__file__).parent / "builtins"
        if not builtins_dir.exists():
            return
        for f in builtins_dir.glob("*.py"):
            if f.name.startswith("_"):
                continue
            self._load_python_plugin(f)

    def register(self, cmd: SlashCommand):
        """Register a slash command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def get(self, name: str) -> Optional[SlashCommand]:
        """Get a command by name or alias."""
        return self._commands.get(name)

    def list_commands(self) -> List[SlashCommand]:
        """List all unique commands (no alias duplicates)."""
        seen = set()
        result = []
        for cmd in self._commands.values():
            if id(cmd) not in seen:
                seen.add(id(cmd))
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)

    def _load_yaml_plugin(self, path: Path):
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f)
            if not data or "name" not in data:
                return
            action = data.get("action", {})
            cmd = YAMLCommand(
                name=data["name"],
                description=data.get("description", ""),
                aliases=data.get("aliases", []),
                hidden=data.get("hidden", False),
                action_type=action.get("type", "api"),
                method=action.get("method", "GET"),
                url_template=action.get("url", ""),
                body_template=action.get("body"),
                output_format=data.get("output", "text"),
            )
            self.register(cmd)
            logger.debug(f"Loaded YAML plugin: {cmd.name} from {path}")
        except Exception as e:
            logger.debug(f"Failed to load YAML plugin {path}: {e}")

    def _load_python_plugin(self, path: Path):
        try:
            spec = importlib.util.spec_from_file_location(
                f"aithershell_plugin_{path.stem}", path
            )
            if not spec or not spec.loader:
                return
            module = importlib.util.module_from_spec(spec)
            # Register BEFORE executing. `@dataclass` resolves its own field
            # types through `sys.modules.get(cls.__module__).__dict__`, so a
            # plugin module that is executed without being registered raises
            # `AttributeError: 'NoneType' object has no attribute '__dict__'`
            # from inside dataclasses — a traceback that names neither the
            # plugin nor the real cause, and which the `except` below then
            # swallowed at DEBUG. Any plugin defining a dataclass was
            # unloadable; bundle.py was the one that did.
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                # Do not leave a half-executed module behind for the next
                # importer to find and believe.
                sys.modules.pop(spec.name, None)
                raise

            # Find all SlashCommand subclasses in the module
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, SlashCommand)
                    and attr is not SlashCommand
                    and attr.name
                ):
                    cmd = attr()
                    self.register(cmd)
                    logger.debug(f"Loaded Python plugin: {cmd.name} from {path}")
        except Exception as e:
            logger.debug(f"Failed to load Python plugin {path}: {e}")
