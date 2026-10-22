"""The aw* command-line tools an agent should know about, checked WITHOUT spawning them.

The SessionStart tool card used to start each CLI with ``--version`` (nine processes per
session). On Windows every process creation leaks kernel pool until reboot, so the
check is now static and runs only when the discovery index is rebuilt:

* the console script resolves on PATH (``shutil.which``), and
* its entry point's module is importable (``importlib.util.find_spec``), which catches
  an editable install whose source tree was removed.

It cannot catch a shim that is present but refuses to execute (exit 126); a
tooling check that is allowed to spawn has to find that one.
"""

from __future__ import annotations

import importlib.util
import shutil
from typing import Callable, Optional

#: name -> the ONE verb an agent should reach for.
TOOL_VERBS: dict[str, str] = {
    "awgit": 'awgit blob-commit --base origin/develop -m "..." <paths>',
    "awgraph": "awgraph callers <symbol>",
    "awm": "awm recall --scope aitherium:<you>:<project> --query <words>",
    "awrelay": "awrelay send '#agents' \"<text>\" --kind finding",
    "awrepl": "awrepl run '<python>'   (state survives calls)",
    "awmine": "awmine report   (what past sessions measured)",
    "awtoll": "awtoll scan   (what each tool call costs context)",
    "awdecide": "awdecide ask   (MCP: decide / decide_outcome)",
    "awfind": 'awfind q "<query>"   (web search, ~300 tokens back)',
    "adk": 'adk discover query "<task>"   (tools, skills, packs for a task)',
}

#: One-line purpose per CLI, used as the card text the ranker searches.
TOOL_PURPOSE: dict[str, str] = {
    "awgit": "commit safely in a shared worktree: leases, blob-commit of exact paths",
    "awgraph": "code graph: callers, calls and symbol search over an indexed repo",
    "awm": "agent memory: remember and recall facts across sessions",
    "awrelay": "message other agent sessions on a shared channel",
    "awrepl": "persistent python repl whose state survives tool calls",
    "awmine": "mine past session transcripts for outcomes and lessons",
    "awtoll": "measure the context cost of each tool call",
    "awdecide": "raise and record decisions with outcomes",
    "awfind": "web search returning a compact answer",
    "adk": "agent toolkit: discover tools, skills and packs; run agents and harnesses",
}

OK, BROKEN = "ok", "broken"

#: tool -> (status, detail)
ProbeFn = Callable[[str], "tuple[str, str]"]


def _entry_module(tool: str) -> Optional[str]:
    """The module behind a console script, from installed distribution metadata."""
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - stdlib on every supported Python
        return None
    try:
        eps = entry_points()
        group = (eps.select(group="console_scripts") if hasattr(eps, "select")
                 else eps.get("console_scripts", []))  # type: ignore[attr-defined]
        for ep in group:
            if ep.name == tool:
                return ep.value.split(":", 1)[0].strip()
    except Exception:  # noqa: BLE001 - broken metadata is "unknown", not a crash
        return None
    return None


def static_probe(tool: str) -> "tuple[str, str]":
    """(status, detail) for one CLI without starting it."""
    if shutil.which(tool) is None:
        return BROKEN, "not on PATH"
    module = _entry_module(tool)
    if module is None:
        return OK, "on PATH (no python entry point to check)"
    try:
        spec = importlib.util.find_spec(module.split(".")[0])
    except (ImportError, ValueError) as exc:
        return BROKEN, f"entry point {module} unimportable: {type(exc).__name__}"
    if spec is None:
        return BROKEN, f"entry point {module} unimportable (source tree removed?)"
    return OK, ""


def probe_all(probe: ProbeFn = static_probe) -> dict[str, "tuple[str, str]"]:
    out: dict[str, tuple[str, str]] = {}
    for name in TOOL_VERBS:
        try:
            out[name] = probe(name)
        except Exception as exc:  # noqa: BLE001 - one bad probe is not the receipt
            out[name] = (BROKEN, type(exc).__name__)
    return out


__all__ = ["BROKEN", "OK", "TOOL_PURPOSE", "TOOL_VERBS", "probe_all", "static_probe"]
