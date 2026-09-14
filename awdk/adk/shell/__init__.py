"""
AitherShell - The Kernel Shell for AitherOS
============================================

The primary programmable interface to AitherOS.
Like bash is to Linux, AitherShell is to AitherOS.

CLI usage:
    aither-py                               # Interactive shell (Python build)
    aither-py "question"                    # Single query
    aither-py --print "question"            # Script mode (plain text)
    aither-py --json "question"             # Script mode (JSON)
    echo "prompt" | aither-py --print       # Stdin pipe
    aither-py --private                     # Private mode
    aither-py --will aither-prime           # Switch persona

Note: the user-facing ``aither`` binary is the npm ``@aitheros/shell-cli``
TypeScript build. The Python package installs as ``aither-py`` to avoid
clobbering it.

Library usage:
    from adk.shell import AitherConfig, GenesisClient, Commands
    cfg = AitherConfig.load()
    gc = GenesisClient(cfg)
"""

from __future__ import annotations

__version__ = "1.18.1"

# Public symbols are imported lazily on attribute access so that the
# package stays cheap to import (the CLI submodules pull in click,
# argcomplete, httpx, prompt_toolkit, etc.).

_PUBLIC: dict[str, tuple[str, str]] = {
    # name -> (module, attr)
    "AitherConfig": ("adk.shell.config", "AitherConfig"),
    "load_config": ("adk.shell.config", "load_config"),
    "save_config": ("adk.shell.config", "save_config"),
    "save_default_config": ("adk.shell.config", "save_default_config"),
    "AuthStore": ("adk.shell.auth", "AuthStore"),
    "AuthError": ("adk.shell.auth", "AuthError"),
    "ensure_root_profile": ("adk.shell.auth", "ensure_root_profile"),
    "GenesisClient": ("adk.shell.genesis_client", "GenesisClient"),
    "GenesisError": ("adk.shell.genesis_client", "GenesisError"),
    "GenesisConnectionError": ("adk.shell.genesis_client", "GenesisConnectionError"),
    "GenesisTimeoutError": ("adk.shell.genesis_client", "GenesisTimeoutError"),
    "Commands": ("adk.shell.commands", "Commands"),
    "CommandError": ("adk.shell.commands", "CommandError"),
    "execute_command": ("adk.shell.commands", "execute_command"),
    "Plugin": ("adk.shell.plugins", "Plugin"),
    "PluginError": ("adk.shell.plugins", "PluginError"),
    "PluginLoadError": ("adk.shell.plugins", "PluginLoadError"),
    "PluginExecutionError": ("adk.shell.plugins", "PluginExecutionError"),
    "PluginCapabilityError": ("adk.shell.plugins", "PluginCapabilityError"),
    "cli": ("adk.shell.cli", "cli"),
}

__all__ = [
    "__version__",
    *_PUBLIC.keys(),
]


def __getattr__(name: str):  # PEP 562 lazy attribute access
    target = _PUBLIC.get(name)
    if target is None:
        raise AttributeError(f"module 'adk.shell' has no attribute {name!r}")
    import importlib
    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # cache
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
