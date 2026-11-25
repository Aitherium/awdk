"""Run ``adk/harnesses/brick_mcp.py`` without importing the ``adk`` package.

``python -m adk.harnesses.brick_mcp`` imports ``adk/__init__`` (agent, llm, tools)
and ``adk/harnesses/__init__`` (session manager) before the stdlib-only wrapper
runs. Measured 2026-11-16 on the owner's host: 44 such children (4 bricks x 11
harness sessions) held 38 MB private each, 1.67 GB in all, while every one only
answered an empty MCP server because the hub already serves the bricks.

This launcher loads the wrapper by path (it sits two levels up when the plugin
lives inside adk, in a checkout or a wheel) and falls back to the module form when
the plugin was copied elsewhere (a marketplace install), so behaviour is unchanged.
"""

import runpy
import sys
from pathlib import Path

_WRAPPER = Path(__file__).resolve().parents[2] / "brick_mcp.py"

if __name__ == "__main__":
    if _WRAPPER.is_file():
        sys.argv = [str(_WRAPPER), *sys.argv[1:]]
        runpy.run_path(str(_WRAPPER), run_name="__main__")
    else:
        sys.argv = ["brick_mcp", *sys.argv[1:]]
        runpy.run_module("adk.harnesses.brick_mcp", run_name="__main__", alter_sys=True)
