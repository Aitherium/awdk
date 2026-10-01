"""Launch a brick's stdio MCP server, tolerating a brick that is not installed.

    python -m adk.harnesses.brick_mcp <brick> [extra args...]

The Claude Code plugin (``adk/harnesses/claude_mod/.mcp.json``) lists every brick
that serves MCP (``awrelay mcp``, ``awfind mcp`` ...). A person who installed the
plugin but not a given brick must not get a red "failed to start" server on every
session, so this wrapper:

* runs ``<brick> mcp [extra...]`` when ``<brick>`` is on PATH -- replacing this
  process on POSIX so stdio is the brick's own, and relaying the exit code on
  Windows (where ``os.exec*`` spawns a child and returns, which would close the pipe);
* otherwise prints ONE line to stderr and exits 0.

Stdlib only; importing it has no side effects.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def resolve(brick: str) -> str | None:
    """The brick's executable, or None when it is not installed (or not a brick name)."""
    if not _NAME.match(brick or ""):
        return None
    return shutil.which(brick)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m adk.harnesses.brick_mcp <brick> [args...]", file=sys.stderr)
        return 2
    brick, extra = args[0], args[1:]
    exe = resolve(brick)
    if exe is None:
        print(f"brick_mcp: {brick} is not installed; skipping its MCP server "
              f"(pip install {brick} to enable it)", file=sys.stderr)
        return 0
    cmd = [exe, "mcp", *extra]
    if os.name == "posix":
        os.execv(exe, cmd)  # never returns
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
