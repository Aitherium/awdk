"""
AitherShell Plugin System
==========================

Plugin registry and execution engine for:
- YAML plugins (~/.aither/plugins/*.yaml)
- Python plugins (~/.aither/plugins/*.py)
- Capability checking (RBAC)
- Error handling and recovery
"""

import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

import yaml

logger = logging.getLogger(__name__)


class PluginError(Exception):
    """Base plugin error."""
    pass


class PluginLoadError(PluginError):
    """Plugin failed to load."""
    pass


class PluginExecutionError(PluginError):
    """Plugin failed during execution."""
    pass


class PluginCapabilityError(PluginError):
    """Plugin requires unauthorized capabilities."""
    pass


class Plugin:
    """
    Represents a single plugin.
    
    Attributes:
        name: Plugin name
        aliases: List of command aliases
        description: Short description
        action: API call or script path
        requires_capabilities: List of required capabilities
        enabled: Whether plugin is enabled
    """

    def __init__(
        self,
        name: str,
        aliases: List[str],
        description: str,
        action: str,
        requires_capabilities: Optional[List[str]] = None,
        enabled: bool = True,
    ):
        """Initialize plugin.
        
        Args:
            name: Plugin name
            aliases: Command aliases
            description: Short description
            action: API endpoint or script path
            requires_capabilities: Required capabilities (RBAC)
            enabled: Whether plugin is enabled
            
        Raises:
            ValueError: If name or action is empty
        """
        if not name or not name.strip():
            raise ValueError("Plugin name cannot be empty")
        if not action or not action.strip():
            raise ValueError("Plugin action cannot be empty")
        
        self.name = name
        self.aliases = aliases or []
        self.description = description or ""
        self.action = action
        self.requires_capabilities = requires_capabilities or []
        self.enabled = enabled

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict."""
        return {
            "name": self.name,
            "aliases": self.aliases,
            "description": self.description,
            "action": self.action,
            "requires_capabilities": self.requires_capabilities,
            "enabled": self.enabled,
        }


class PluginRegistry:
    """
    Plugin registry for loading and executing plugins.
    
    Features:
    - Load YAML and Python plugins
    - Capability checking
    - Plugin execution with error handling
    """

    def __init__(self, plugin_dirs: Optional[List[str]] = None):
        """Initialize plugin registry.
        
        Args:
            plugin_dirs: List of directories to search for plugins
        """
        self.plugin_dirs = plugin_dirs or [str(Path.home() / ".aither" / "plugins")]
        self.plugins: Dict[str, Plugin] = {}
        self.user_capabilities: List[str] = []
        self._load_all_plugins()

    def set_user_capabilities(self, capabilities: List[str]) -> None:
        """Set user capabilities for RBAC checks.
        
        Args:
            capabilities: List of capabilities (e.g., ["read", "write", "admin"])
        """
        self.user_capabilities = capabilities
        logger.debug(f"User capabilities: {capabilities}")

    def load_plugins(self) -> int:
        """Load all plugins from configured directories.
        
        Returns:
            Number of plugins loaded
            
        Raises:
            PluginLoadError: If plugin loading fails critically
        """
        self.plugins.clear()
        count = 0
        
        for plugin_dir in self.plugin_dirs:
            plugin_path = Path(plugin_dir)
            if not plugin_path.exists():
                logger.debug(f"Plugin directory not found: {plugin_path}")
                continue
            
            # Load YAML plugins
            for yaml_file in plugin_path.glob("*.yaml"):
                try:
                    self._load_yaml_plugin(yaml_file)
                    count += 1
                except PluginLoadError as e:
                    logger.warning(f"Failed to load {yaml_file}: {e}")
            
            # Load Python plugins
            for py_file in plugin_path.glob("*.py"):
                try:
                    self._load_python_plugin(py_file)
                    count += 1
                except PluginLoadError as e:
                    logger.warning(f"Failed to load {py_file}: {e}")
        
        logger.info(f"Loaded {count} plugins")
        return count

    def _load_all_plugins(self) -> None:
        """Internal method to load all plugins on init."""
        try:
            self.load_plugins()
        except Exception as e:
            logger.error(f"Plugin loading error: {e}")

    def _load_yaml_plugin(self, yaml_file: Path) -> None:
        """Load a YAML plugin file.
        
        Args:
            yaml_file: Path to YAML file
            
        Raises:
            PluginLoadError: If YAML is invalid
        """
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            
            if not isinstance(data, dict):
                raise PluginLoadError(f"{yaml_file}: YAML must be a dict")
            
            name = data.get("name")
            if not name:
                raise PluginLoadError(f"{yaml_file}: missing 'name'")
            
            action = data.get("action")
            if not action:
                raise PluginLoadError(f"{yaml_file}: missing 'action'")
            
            plugin = Plugin(
                name=name,
                aliases=data.get("aliases", []),
                description=data.get("description", ""),
                action=action,
                requires_capabilities=data.get("requires_capabilities", []),
                enabled=data.get("enabled", True),
            )
            
            self.plugins[name] = plugin
            for alias in plugin.aliases:
                self.plugins[alias] = plugin
            
            logger.debug(f"Loaded YAML plugin: {name}")
            
        except yaml.YAMLError as e:
            raise PluginLoadError(f"{yaml_file}: YAML error: {e}")
        except Exception as e:
            raise PluginLoadError(f"{yaml_file}: {e}")

    def _load_python_plugin(self, py_file: Path) -> None:
        """Load a Python plugin file.
        
        Args:
            py_file: Path to Python file
            
        Raises:
            PluginLoadError: If Python module is invalid
        """
        try:
            # Load module
            spec = importlib.util.spec_from_file_location(
                py_file.stem,
                py_file,
            )
            if spec is None or spec.loader is None:
                raise PluginLoadError(f"{py_file}: could not load spec")
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            # Look for plugin definition
            if not hasattr(module, "PLUGIN"):
                logger.debug(f"Python module {py_file.name} has no PLUGIN attribute, skipping")
                return
            
            plugin_data = getattr(module, "PLUGIN")
            if not isinstance(plugin_data, dict):
                raise PluginLoadError(f"{py_file}: PLUGIN must be a dict")
            
            name = plugin_data.get("name")
            if not name:
                raise PluginLoadError(f"{py_file}: PLUGIN missing 'name'")
            
            action = plugin_data.get("action")
            if not action:
                raise PluginLoadError(f"{py_file}: PLUGIN missing 'action'")
            
            plugin = Plugin(
                name=name,
                aliases=plugin_data.get("aliases", []),
                description=plugin_data.get("description", ""),
                action=action,
                requires_capabilities=plugin_data.get("requires_capabilities", []),
                enabled=plugin_data.get("enabled", True),
            )
            
            self.plugins[name] = plugin
            for alias in plugin.aliases:
                self.plugins[alias] = plugin
            
            logger.debug(f"Loaded Python plugin: {name}")
            
        except Exception as e:
            raise PluginLoadError(f"{py_file}: {e}")

    def get_plugin(self, name: str) -> Optional[Plugin]:
        """Get plugin by name or alias.
        
        Args:
            name: Plugin name or alias
            
        Returns:
            Plugin instance or None if not found
        """
        return self.plugins.get(name)

    def list_plugins(self) -> List[Plugin]:
        """Get all unique plugins (no duplicates from aliases).
        
        Returns:
            List of plugin instances
        """
        seen = set()
        result = []
        for plugin in self.plugins.values():
            if plugin.name not in seen:
                seen.add(plugin.name)
                result.append(plugin)
        return result

    async def execute_plugin(
        self,
        name: str,
        args: Optional[List[str]] = None,
    ) -> str:
        """Execute a plugin.
        
        Args:
            name: Plugin name or alias
            args: Command arguments
            
        Returns:
            Plugin output
            
        Raises:
            PluginCapabilityError: If user lacks required capabilities
            PluginExecutionError: If plugin execution fails
        """
        plugin = self.get_plugin(name)
        if not plugin:
            raise PluginExecutionError(f"Plugin not found: {name}")
        
        if not plugin.enabled:
            raise PluginExecutionError(f"Plugin is disabled: {name}")
        
        # Check capabilities
        if plugin.requires_capabilities:
            missing = set(plugin.requires_capabilities) - set(self.user_capabilities)
            if missing:
                raise PluginCapabilityError(
                    f"Plugin '{name}' requires capabilities: {', '.join(missing)}"
                )
        
        try:
            # Check if action is a URL (API call)
            if plugin.action.startswith("http://") or plugin.action.startswith("https://"):
                return await self._execute_api_plugin(plugin, args)
            
            # Otherwise, execute as script
            return await self._execute_script_plugin(plugin, args)
            
        except Exception as e:
            raise PluginExecutionError(f"Plugin execution failed: {e}")

    async def _execute_api_plugin(
        self,
        plugin: Plugin,
        args: Optional[List[str]],
    ) -> str:
        """Execute an API-based plugin.
        
        Args:
            plugin: Plugin instance
            args: Command arguments
            
        Returns:
            API response
            
        Raises:
            PluginExecutionError: If API call fails
        """
        try:
            import httpx
            
            # Build query string from args
            query_params = {}
            if args:
                for arg in args:
                    if "=" in arg:
                        key, val = arg.split("=", 1)
                        query_params[key] = val
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(plugin.action, params=query_params)
                response.raise_for_status()
                
                # Try to parse as JSON
                try:
                    data = response.json()
                    return json.dumps(data, indent=2)
                except Exception:
                    return response.text
                    
        except Exception as e:
            raise PluginExecutionError(f"API call failed: {e}")

    async def _execute_script_plugin(
        self,
        plugin: Plugin,
        args: Optional[List[str]],
    ) -> str:
        """Execute a script-based plugin.
        
        Args:
            plugin: Plugin instance
            args: Command arguments
            
        Returns:
            Script output
            
        Raises:
            PluginExecutionError: If script fails
        """
        try:
            script_path = Path(plugin.action)
            if not script_path.exists():
                raise PluginExecutionError(f"Script not found: {plugin.action}")
            
            cmd = [str(script_path)]
            if args:
                cmd.extend(args)
            
            # Execute script with timeout
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=30.0,
                )
                
                if process.returncode != 0:
                    error_msg = stderr.decode("utf-8", errors="replace")
                    raise PluginExecutionError(f"Script failed: {error_msg}")
                
                return stdout.decode("utf-8", errors="replace")
                
            except asyncio.TimeoutError:
                process.kill()
                raise PluginExecutionError("Script execution timed out")
                
        except PluginExecutionError:
            raise
        except Exception as e:
            raise PluginExecutionError(f"Script execution failed: {e}")


# Import asyncio for execute_script_plugin
import asyncio
