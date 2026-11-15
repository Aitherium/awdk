"""Load the agent a project directory defines (``adk init`` writes ``agent.py``).

``adk run`` used to ignore the project entirely: it served a fresh agent named
``aither`` built from the identity default, so the tools the scaffold's
``agent.py`` registers (``@agent.tool``) were never on the menu, and
``-i <name>`` only renamed that empty agent (measured 2026-11-12, adk 3.8.105).
The project's ``agent.py`` is the definition; this module imports it and hands
back the ``AitherAgent`` it built.

Importing ``agent.py`` runs the project's own code, exactly as
``python agent.py`` (the scaffold's other documented entry point) does. It is
only ever the file in the directory the user ran ``adk run`` from, never a path
taken from a prompt or a tool result.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_AGENT_FILE = "agent.py"
_MODULE_NAME = "_adk_project_agent"


class ProjectAgentError(RuntimeError):
    """``agent.py`` exists but no agent could be loaded from it."""


def project_agent_path(cwd: Optional[Path] = None) -> Optional[Path]:
    """``<cwd>/agent.py`` when it exists, else None."""
    path = (Path(cwd) if cwd else Path.cwd()) / PROJECT_AGENT_FILE
    return path if path.is_file() else None


def load_project_agent(cwd: Optional[Path] = None, want: str = "") -> Optional[Any]:
    """The ``AitherAgent`` defined by ``<cwd>/agent.py``, or None when there is no file.

    Picks the module-level name ``agent`` when it is an ``AitherAgent``; otherwise
    the only ``AitherAgent`` in the module. With ``want`` set, only an agent with
    that name qualifies (None when the project defines a different one), so an
    explicit ``--identity other`` still serves ``other``.

    Raises :class:`ProjectAgentError` when the file cannot be imported, defines
    no agent, or defines several with none called ``agent``.
    """
    path = project_agent_path(cwd)
    if path is None:
        return None

    from adk.agent import AitherAgent

    root = str(path.parent.resolve())
    # The scaffold's agent.py may import its sibling tools.py.
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(path))
    if spec is None or spec.loader is None:
        raise ProjectAgentError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:  # noqa: BLE001 - user code; SystemExit included
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise ProjectAgentError(
            f"{path} failed to import: {type(exc).__name__}: {exc}") from exc

    found = [v for v in vars(module).values() if isinstance(v, AitherAgent)]
    # One object bound to two names is one agent.
    agents = list({id(a): a for a in found}.values())
    preferred = getattr(module, "agent", None)
    if isinstance(preferred, AitherAgent):
        chosen = preferred
    elif len(agents) == 1:
        chosen = agents[0]
    elif not agents:
        raise ProjectAgentError(
            f"{path} defines no AitherAgent (expected e.g. `agent = AitherAgent(\"name\")`)")
    else:
        names = ", ".join(sorted(str(getattr(a, "name", "?")) for a in agents))
        raise ProjectAgentError(
            f"{path} defines several agents ({names}); name the one to serve `agent`")

    if want and str(getattr(chosen, "name", "")) != want:
        for candidate in agents:
            if str(getattr(candidate, "name", "")) == want:
                return candidate
        return None
    return chosen
