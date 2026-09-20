"""The awsh mod for Claude Code: where it lives, how it is switched on, both ways.

``claude_mod/`` beside this file is a Claude Code plugin. Its hooks module answers
an ``aw`` subagent's model steps from a session on this daemon, so every harness
and backend the daemon can drive shows up inside Claude Code as a native subagent.
That is one direction. This module is the other two halves of the job:

* **install / status / uninstall** -- what ``adk harness mod`` runs. A function-hooks
  module only loads when ``CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`` is in the env block
  of the user's Claude Code settings, and only from a plugin Claude Code knows about.
  Both are recorded in files the user owns, so both are reported by ``status`` rather
  than assumed.
* **the reverse direction** -- a Claude Code session the DAEMON starts gets the same
  mod (:func:`apply_to_launch`), so a session opened from awsh can itself hand work
  to another harness. It cannot rely on the user-level install: a session bound to a
  backend runs with ``--setting-sources`` that excludes ``user``, which drops the
  user's plugins and the env block along with the profile it is there to drop.

DEPTH. A Claude Code started by the mod can start the mod again. Each level takes a
model choosing to, so it is not a loop by construction -- but nothing else bounds the
spend if one does. The session owner the mod sends carries its depth
(``claude-code:<agent>@d<N>``); the daemon turns that into ``AITHER_AW_DEPTH`` for the
child, and the mod refuses to open a session at :data:`MAX_DEPTH`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

#: The env var Claude Code reads before it will load any function-hooks module.
HOOKS_ENV = "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"

#: Set to ``0`` to keep the mod out of daemon-started Claude Code sessions.
OPT_OUT_ENV = "AITHER_AW_MOD"

#: How deep a chain of mod-started Claude Code sessions is, for the child to read.
DEPTH_ENV = "AITHER_AW_DEPTH"

#: The mod refuses to open a session from a Claude Code already this deep.
#: ``register.ts`` holds the same number; ``tests/test_harness_mod.py`` pins the pair.
MAX_DEPTH = 2

#: Harnesses that ARE Claude Code, and so can load a Claude Code plugin.
CLAUDE_HARNESSES = frozenset({"claude", "claude-tty"})

PLUGIN_NAME = "awsh"
MARKETPLACE_NAME = "awsh"

_OWNER_DEPTH = re.compile(r"^claude-code:.*@d(\d+)$")


def mod_dir() -> Path:
    """The plugin directory that ships beside this module."""
    return Path(__file__).resolve().parent / "claude_mod"


def mod_present() -> bool:
    """Whether the plugin's load-bearing files are actually there.

    A wheel build that dropped the non-Python files would leave ``mod_dir()``
    pointing at nothing while every Python import still succeeds.
    """
    root = mod_dir()
    return all(
        (root / rel).is_file()
        for rel in (".claude-plugin/plugin.json", "hooks/hooks.json",
                    "hooks/register.ts", "agents/aw.md")
    )


def depth_of_owner(owner: str) -> int:
    """The chain depth a session's ``owner`` declares; 0 for anything else."""
    found = _OWNER_DEPTH.match(owner or "")
    return int(found.group(1)) if found else 0


def apply_to_launch(harness_id: str, owner: str, env: dict[str, str],
                    extra_args: list[str]) -> list[str]:
    """Give a daemon-started Claude Code session the mod. Returns the new argv tail.

    ``env`` is changed in place and must already be scrubbed of nested-session
    markers: the scrub removes every ``CLAUDE_CODE_*`` name, :data:`HOOKS_ENV`
    included, so this has to run after it.
    """
    depth = depth_of_owner(owner)
    if depth:
        env[DEPTH_ENV] = str(depth)
    if harness_id not in CLAUDE_HARNESSES:
        return extra_args
    if os.environ.get(OPT_OUT_ENV, "1").strip() == "0" or not mod_present():
        return extra_args
    env[HOOKS_ENV] = "1"
    if "--plugin-dir" in extra_args and str(mod_dir()) in extra_args:
        return extra_args
    return [*extra_args, "--plugin-dir", str(mod_dir())]


# ── install / status / uninstall ────────────────────────────────────────────


def claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _read_settings(path: Path) -> dict[str, Any]:
    """The settings object. A file that exists but does not parse RAISES.

    Returning ``{}`` for it would make the next write replace the user's whole
    settings file with one env var.
    """
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a JSON object")
    return loaded


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.awsh-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _claude(*args: str) -> tuple[int, str]:
    binary = shutil.which("claude")
    if binary is None:
        return 127, "claude is not on PATH"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [binary, *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, check=False,
    )
    return done.returncode, (done.stdout + done.stderr).strip()


def _plugin_known() -> Optional[bool]:
    """Whether Claude Code lists the plugin. None when it could not be asked."""
    code, out = _claude("plugin", "list")
    if code != 0:
        return None
    return f"{PLUGIN_NAME}@{MARKETPLACE_NAME}" in out


def status() -> dict[str, Any]:
    path = claude_settings_path()
    try:
        env = _read_settings(path).get("env") or {}
        settings_error = ""
    except (OSError, ValueError) as exc:
        env, settings_error = {}, str(exc)
    hooks_on = str(env.get(HOOKS_ENV, "")) == "1"
    known = _plugin_known()
    return {
        "mod_dir": str(mod_dir()),
        "mod_present": mod_present(),
        "settings": str(path),
        "settings_error": settings_error,
        "function_hooks_enabled": hooks_on,
        "plugin_installed": known,
        "daemon_sessions_get_mod": os.environ.get(OPT_OUT_ENV, "1").strip() != "0",
        "max_depth": MAX_DEPTH,
        "active": bool(mod_present() and hooks_on and known),
        "note": "restart Claude Code after a change: the env block is read at launch",
    }


def install() -> dict[str, Any]:
    """Switch the mod on for the user's own Claude Code sessions."""
    if not mod_present():
        return {"ok": False, "error": f"the plugin files are missing from {mod_dir()}"}
    path = claude_settings_path()
    try:
        settings = _read_settings(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"refusing to touch unreadable settings: {exc}"}
    steps: list[str] = []
    env = settings.setdefault("env", {})
    if str(env.get(HOOKS_ENV, "")) != "1":
        env[HOOKS_ENV] = "1"
        _write_settings(path, settings)
        steps.append(f"set env.{HOOKS_ENV}=1 in {path}")
    if _plugin_known() is not True:
        code, out = _claude("plugin", "marketplace", "add", str(mod_dir()))
        steps.append(f"marketplace add -> {code}")
        if code != 0 and "already" not in out.lower():
            return {"ok": False, "steps": steps, "error": out[-400:]}
        code, out = _claude("plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}")
        steps.append(f"plugin install -> {code}")
        if code != 0:
            return {"ok": False, "steps": steps, "error": out[-400:]}
    return {"ok": True, "steps": steps or ["already installed"], **status()}


def uninstall() -> dict[str, Any]:
    """Remove the plugin. The env var stays: other mods may depend on it."""
    code, out = _claude("plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}")
    return {"ok": code == 0, "detail": out[-400:], **status()}
