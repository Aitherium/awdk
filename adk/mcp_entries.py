"""The one place awdk builds an MCP *client* entry (what goes in .mcp.json).

Every verb that writes an IDE's MCP config (``adk onboard`` today) takes its
entry from here, so the endpoint and transport are named once. Until
2026-09-27 onboarding hand-wrote ``npx -y aither-mcp-server`` — an npm package
that was never published (``npm view aither-mcp-server`` is E404), so every
self-serve customer's MCP setup failed at step one.

Two real endpoints exist:

* **hosted** — the Aitherium MCP gateway, streamable HTTP at
  ``https://mcp.aitherium.com/mcp``; bearer = an ``aither_sk_live_*`` key or an
  Aither ID token. The entry never carries the key itself: it references
  ``${AITHER_API_KEY}`` so a project ``.mcp.json`` is safe to commit.
* **local node** — ``awnode mcp`` (stdio; ``awnode`` is on PyPI). Only offered
  when the binary is on PATH.
"""

from __future__ import annotations

import shutil
from typing import Any

HOSTED_MCP_URL = "https://mcp.aitherium.com/mcp"
API_KEY_ENV = "AITHER_API_KEY"

#: Every stdio ``command`` an entry built here may name. A test asserts the
#: generated configs name nothing else, so an unpublished package cannot come back.
KNOWN_COMMANDS = frozenset({"awnode"})

# Each client spells env-var interpolation differently in its config file.
_ENV_REF = {
    "claude-code": "${%s}",       # Claude Code .mcp.json
    "cursor": "${env:%s}",        # ~/.cursor/mcp.json
    "vscode": "${env:%s}",        # .vscode/mcp.json
    "openclaw": "${%s}",
}


def hosted_entry(client: str = "claude-code") -> dict[str, Any]:
    """MCP entry for the hosted gateway, authenticating from ``$AITHER_API_KEY``."""
    ref = _ENV_REF.get(client, "${%s}") % API_KEY_ENV
    entry: dict[str, Any] = {
        "type": "http",
        "url": HOSTED_MCP_URL,
        "headers": {"Authorization": f"Bearer {ref}"},
    }
    return entry


def awnode_entry() -> dict[str, Any] | None:
    """stdio entry for a local awnode, or None when awnode is not installed."""
    if not shutil.which("awnode"):
        return None
    return {"type": "stdio", "command": "awnode", "args": ["mcp"]}


def is_broken_entry(entry: Any) -> bool:
    """True for an entry an older awdk wrote that can never start."""
    if not isinstance(entry, dict):
        return False
    return "aither-mcp-server" in (entry.get("args") or [])
