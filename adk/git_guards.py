"""Install awgit's commit guards before an awdk agent's first commit in a repo.

The same check the awsh Claude Code plugin runs as a PreToolUse hook, for agents that
commit through awdk's own tools (``git_commit``, ``shell_exec``). One implementation:
this module loads the plugin's stdlib-only ``hooks/awgit_ensure.py`` by path, because
the plugin cache ships that file without the adk package around it.

Fail-soft by contract: never raises, never blocks the commit, a silent no-op when
awgit (with ``hooks ensure``) is not installed. Once per repo per process.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import List, Optional

_HOOK = Path(__file__).parent / "harnesses" / "claude_mod" / "hooks" / "awgit_ensure.py"
_mod: Optional[ModuleType] = None
_SESSION = f"adk-{os.getpid()}"
_log = logging.getLogger(__name__)


def _load() -> Optional[ModuleType]:
    global _mod
    if _mod is None:
        try:
            spec = importlib.util.spec_from_file_location("_adk_awgit_ensure", _HOOK)
            if spec is None or spec.loader is None:
                return None
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _mod = m
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            _log.debug("awgit ensure hook unavailable: %s", exc)
            return None
    return _mod


def commit_dirs(command: str, cwd: str) -> List[str]:
    m = _load()
    if m is None:
        return []
    try:
        return list(m.commit_dirs(command, cwd))
    except Exception as exc:  # noqa: BLE001
        _log.debug("awgit ensure: command parse failed: %s", exc)
        return []


def ensure_for_command(command: str, cwd: Optional[str] = None) -> None:
    """Before a shell command that runs ``git commit``: ensure the guards in its repo."""
    if os.environ.get("AWGIT_ENSURE_DISABLE") == "1":
        return
    dirs = commit_dirs(command, cwd or os.getcwd())
    if dirs:
        ensure_repo(*dirs)


def ensure_repo(*dirs: str) -> None:
    """Ensure the guards in these repo dirs (cached per process)."""
    if os.environ.get("AWGIT_ENSURE_DISABLE") == "1":
        return
    m = _load()
    if m is None:
        return
    try:
        m.ensure([os.path.abspath(d) for d in dirs], _SESSION)
    except Exception as exc:  # noqa: BLE001 -- never block the commit
        _log.debug("awgit ensure skipped for %s: %s", dirs, exc)
