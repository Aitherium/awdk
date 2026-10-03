"""awsh tools for the KV relay on this machine: status, approve a mesh holder, add holders.

Appended to ``mcp_stdio.TOOLS``. They run in the awsh stdio server, which is the owner's
OS user on the relay host, and talk to the relay over loopback with the master token from
its state file (0600). No tool returns a token: a mesh holder collects its join token
from the relay itself once approved, and ``elastic`` hands tokens straight to the launcher.
"""

from __future__ import annotations

from typing import Any


def _mesh():
    from adk import kvholder_mesh

    return kvholder_mesh


def _status(a: dict) -> Any:
    try:
        return _mesh().status()
    except (OSError, RuntimeError) as e:
        return {"error": str(e)}


def _pending(a: dict) -> Any:
    try:
        return {"pending": _mesh().pending()}
    except (OSError, RuntimeError) as e:
        return {"error": str(e)}


def _join(a: dict) -> Any:
    code = str(a.get("code") or "").strip()
    if not code:
        return {"error": "code is required (see awsh_kvholder_pending)"}
    try:
        return _mesh().decide(code, approve=str(a.get("decision") or "approve") != "deny")
    except (OSError, RuntimeError) as e:
        return {"error": str(e)}


def _elastic(a: dict) -> Any:
    try:
        return _mesh().elastic(
            count=int(a.get("count") or 1),
            minutes=int(a.get("minutes") or 30),
            max_mb=int(a.get("max_mb") or 4096),
            workflow=str(a.get("workflow") or "kvholder-runner.yml"),
            ref=str(a.get("ref") or "develop"),
            dry_run=a.get("dry_run", True) is not False,
        )
    except (OSError, RuntimeError, ValueError) as e:
        return {"error": str(e)}


TOOLS: list = [
    {"name": "awsh_kvholder_status",
     "description": "The KV relay on this machine (adk kvholder phone): attached holders, "
                    "their memory, the mesh door and its pending requests. No secrets.",
     "schema": {"type": "object", "properties": {}},
     "fn": _status},
    {"name": "awsh_kvholder_pending",
     "description": "Holders on the LAN/mesh waiting at the relay's door "
                    "(adk kvholder serve --mesh), each with its 6-letter code.",
     "schema": {"type": "object", "properties": {}},
     "fn": _pending},
    {"name": "awsh_kvholder_join",
     "description": "Approve (default) or deny a waiting mesh holder by its code. Approving "
                    "mints a single-use join token the holder collects itself; the token is "
                    "never returned here.",
     "schema": {"type": "object", "properties": {
         "code": {"type": "string", "description": "the code the holder printed"},
         "decision": {"type": "string", "enum": ["approve", "deny"]}},
         "required": ["code"]},
     "fn": _join},
    {"name": "awsh_kvholder_elastic",
     "description": "Add on-demand holders (CI runners via awrun, else gh workflow run), one "
                    "join token each, to a relay started with --via tunnel. dry_run defaults "
                    "to TRUE: pass dry_run=false to launch. Count is capped at 16.",
     "schema": {"type": "object", "properties": {
         "count": {"type": "integer", "description": "holders to add (default 1, max 16)"},
         "minutes": {"type": "integer", "description": "how long each lends (default 30)"},
         "max_mb": {"type": "integer", "description": "memory each lends (default 4096)"},
         "workflow": {"type": "string", "description": "default kvholder-runner.yml"},
         "ref": {"type": "string", "description": "default develop"},
         "dry_run": {"type": "boolean", "description": "default true"}}},
     "fn": _elastic},
]
