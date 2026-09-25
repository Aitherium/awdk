"""
Aither Strata Plugin for AitherShell
====================================

Shell face of ``adk.strata`` -- the unified storage abstraction (local
``~/.aither/strata`` always; S3 and the AitherOS Strata service when configured).

Usage:
    /strata backends            # Which backends are active, in priority order
    /strata ls [PREFIX]         # List keys (``tenant:name/prefix`` selects a tenant)
    /strata cat PATH            # Print a stored object (text; binary is summarised)
    /strata put PATH TEXT...    # Store TEXT at PATH
    /strata rm PATH             # Delete PATH from every backend
    /strata exists PATH         # yes / no

"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_MAX_CAT_BYTES = 64 * 1024


def _help() -> str:
    return (__doc__ or "").strip()


def _strata():
    from adk.strata import get_strata

    return get_strata()


class StrataPlugin(SlashCommand):
    name: str = "strata"
    aliases: List[str] = []
    description: str = "Strata storage — list, read, write and delete stored objects"
    category: str = "infrastructure"

    def __init__(self, *args: Any, **kwargs: Any):
        # SlashCommand is a dataclass whose __init__ does not carry a subclass's
        # class attrs onto the instance (see durability.py / secret.py).
        super().__init__(*args, **kwargs)
        self.name = "strata"
        self.aliases = []
        self.description = "Strata storage — list, read, write and delete stored objects"
        self.category = "infrastructure"

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return _help()
        sub, rest = args[0].lower(), args[1:]
        handlers = {
            "backends": self._backends,
            "stats": self._backends,
            "ls": self._ls,
            "list": self._ls,
            "cat": self._cat,
            "get": self._cat,
            "put": self._put,
            "rm": self._rm,
            "exists": self._exists,
        }
        handler = handlers.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{_help()}"
        try:
            return await handler(rest)
        except ValueError as e:  # parse_path refuses an empty path
            return f"Error: {e}"

    async def _backends(self, args: List[str]) -> str:
        return json.dumps(await _strata().stats(), indent=2)

    async def _ls(self, args: List[str]) -> str:
        prefix = args[0] if args else ""
        keys = await _strata().list(prefix)
        if not keys:
            return f"(no keys under {prefix!r})"
        return "\n".join(keys)

    async def _cat(self, args: List[str]) -> str:
        if not args:
            return "Usage: /strata cat PATH"
        data = await _strata().read(args[0])
        if data is None:
            return f"Not found: {args[0]}"
        if len(data) > _MAX_CAT_BYTES:
            return f"{args[0]}: {len(data)} bytes (too large to print; limit {_MAX_CAT_BYTES})"
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"{args[0]}: {len(data)} bytes of binary data"

    async def _put(self, args: List[str]) -> str:
        if len(args) < 2:
            return "Usage: /strata put PATH TEXT..."
        path, text = args[0], " ".join(args[1:])
        ok = await _strata().write(path, text)
        if not ok:
            return f"Write FAILED: {path}"
        return f"Stored {len(text.encode('utf-8'))} bytes at {path}"

    async def _rm(self, args: List[str]) -> str:
        if not args:
            return "Usage: /strata rm PATH"
        ok = await _strata().delete(args[0])
        return f"Deleted {args[0]}" if ok else f"Not deleted (absent or refused): {args[0]}"

    async def _exists(self, args: List[str]) -> str:
        if not args:
            return "Usage: /strata exists PATH"
        return "yes" if await _strata().exists(args[0]) else "no"
