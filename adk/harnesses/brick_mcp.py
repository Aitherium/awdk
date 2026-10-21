"""Launch a brick's stdio MCP server, tolerating a brick that is not installed.

    python -m adk.harnesses.brick_mcp <brick> [extra args...]

The Claude Code plugin (``adk/harnesses/claude_mod/.mcp.json``) lists every brick
that serves MCP (``awrelay mcp``, ``awfind mcp`` ...). A person who installed the
plugin but not a given brick must not get a red "failed to start" server on every
session, so this wrapper:

* exits 0 with one stderr line when ``AW_PLUGIN_SKIP`` names the brick, or when the
  project's nearest ``.mcp.json`` already declares the brick or the machine-wide aw
  hub (``--hub``) -- the plugin copy would serve the same tools twice;
* runs ``<brick> mcp [extra...]`` when ``<brick>`` is on PATH -- replacing this
  process on POSIX so stdio is the brick's own, and on Windows (where ``os.exec*``
  spawns a child and returns, which would close the pipe) spawning it with THIS
  process's std handles passed explicitly and relaying its exit code;
* otherwise prints ONE line to stderr and exits 0.

Why the explicit handles: on Windows ``subprocess.call(cmd)`` with no ``stdin=`` /
``stdout=`` gives the child no usable copy of a piped stdio, so it never reads the
client's ``initialize`` and the client times out (Claude Code: CONNECT_TIMEOUT after
30 s on every session). The same brick run directly answered in under 3 s.

Stdlib only; importing it has no side effects.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SKIP_ENV = "AW_PLUGIN_SKIP"
#: No console window for the child (a console-less parent's child would open one).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
#: POSIX replaces this process with the brick; elsewhere it spawns and relays. Module
#: flags (not ``os.name`` at call time) so a test can take the Windows branch without
#: patching the global ``os.name``, which turns every ``Path()`` into ``WindowsPath``.
_EXEC_IN_PLACE = os.name == "posix"
_WINDOWS = os.name == "nt"


def _tool_bin_dirs(env: Optional[dict] = None) -> list[Path]:
    """Where ``uv tool`` / ``pipx`` put isolated executables, overrides first."""
    e = env if env is not None else os.environ
    dirs = [e.get("UV_TOOL_BIN_DIR"), e.get("PIPX_BIN_DIR")]
    return [Path(d) for d in dirs if d] + [Path.home() / ".local" / "bin"]


def resolve(brick: str, env: Optional[dict] = None) -> str | None:
    """The brick's executable, or None when it is not installed (or not a brick name).

    An isolated tool install wins over PATH. Measured 2026-10-21: awgraph and awrelay
    need ``mcp>=2`` while awdk, repowise and headroom pin ``mcp<2``, so in one shared
    interpreter the bricks import a 1.x SDK and never answer ``initialize`` (Claude
    Code: CONNECTION_CLOSED every session). ``uv tool install "awgraph[mcp]"`` gives
    the brick its own env, but the shared Scripts dir sits earlier on PATH.
    """
    if not _NAME.match(brick or ""):
        return None
    for d in _tool_bin_dirs(env):
        for name in (f"{brick}.exe", brick) if _WINDOWS else (brick,):
            p = d / name
            if p.is_file() and (_WINDOWS or os.access(p, os.X_OK)):
                return str(p)
    return shutil.which(brick)


def skipped(brick: str, env: Optional[dict] = None) -> bool:
    """True when ``AW_PLUGIN_SKIP`` (comma-separated) names ``brick``."""
    raw = (env if env is not None else os.environ).get(SKIP_ENV, "")
    return brick.lower() in {p.strip().lower() for p in raw.split(",") if p.strip()}


def _hub_entry(servers: dict) -> Optional[str]:
    """The name of a server that runs the machine-wide aw hub (an arg ``--hub``)."""
    for name, spec in servers.items():
        args = spec.get("args") if isinstance(spec, dict) else None
        if isinstance(args, list) and "--hub" in [str(a) for a in args]:
            return str(name)
    return None


def mcp_already_declared(brick: str, start: Path) -> Optional[Path]:
    """The nearest ``.mcp.json`` from ``start`` upward, when it already serves ``brick``.

    Only the NEAREST config counts: it is the one Claude Code reads. It serves the
    brick when it declares a server of that name or runs the aw hub, which serves
    every installed brick from one process per machine.
    """
    for d in [start, *start.parents]:
        cfg = d / ".mcp.json"
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict) and (brick in servers or _hub_entry(servers)):
            return cfg
        return None
    return None


def _project_dir() -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(raw) if raw else Path.cwd()


def serve_empty(brick: str, why: str, stdin=None, stdout=None) -> int:
    """Answer MCP with no tools until the client hangs up.

    Exiting at once is what the client shows as CONNECTION_CLOSED: measured 2026-10-21,
    Claude Code listed all four plugin bricks as "Failed to connect" in a project whose
    .mcp.json already served them through the hub, which is the healthy case. An empty
    server reads as connected, and ``why`` rides along as its instructions.
    """
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    empty = {"tools/list": {"tools": []}, "prompts/list": {"prompts": []},
             "resources/list": {"resources": []}, "ping": {}}
    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or "id" not in msg:
            continue  # a notification: nothing to answer
        method = msg.get("method")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                      "serverInfo": {"name": brick, "version": "off"}, "instructions": why}
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        elif method in empty:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": empty[method]}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": f"{brick} is off here: {why}"}}
        out.write(json.dumps(reply) + "\n")
        out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m adk.harnesses.brick_mcp <brick> [args...]", file=sys.stderr)
        return 2
    brick, extra = args[0], args[1:]
    if skipped(brick):
        why = f"{brick} skipped by {SKIP_ENV}"
        print(f"brick_mcp: {why}", file=sys.stderr)
        return serve_empty(brick, why)
    already = mcp_already_declared(brick, _project_dir())
    if already is not None:
        why = f"{brick} is already served by {already}; the plugin copy stays off"
        print(f"brick_mcp: {why}", file=sys.stderr)
        return serve_empty(brick, why)
    exe = resolve(brick)
    if exe is None:
        why = f"{brick} is not installed (pip install {brick} to enable it)"
        print(f"brick_mcp: {brick} is not installed; skipping its MCP server "
              f"(pip install {brick} to enable it)", file=sys.stderr)
        return serve_empty(brick, why)
    cmd = [exe, "mcp", *extra]
    if _EXEC_IN_PLACE:
        os.execv(exe, cmd)  # never returns
    sys.stdout.flush()
    return subprocess.call(
        cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        creationflags=_NO_WINDOW if _WINDOWS else 0,
    )


if __name__ == "__main__":
    sys.exit(main())
