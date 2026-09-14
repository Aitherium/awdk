"""Per-SESSION model binding for coding harnesses.

``adk claude-model use <profile>`` is a GLOBAL switch: it rewrites the ``env``
block of ``~/.claude/settings.json``, so every Claude Code process on the box
moves at once. That is fine for one human at one terminal and useless for a
shell-of-shells, where the whole point is tab 1 on DeepSeek Pro, tab 2 on the
local qwen, tab 3 on stock Anthropic — concurrently.

This module resolves a profile into a **process environment** instead, which the
session manager hands to that one child process. Nothing global is mutated, so
two sessions never fight over the same file.

The trap that makes this non-obvious
------------------------------------
``env`` in ``settings.json`` OVERRIDES variables exported into Claude Code's
process environment (documented in ``claude_model_profile.py``'s header, learned
from the Kimi integration). So exporting ``ANTHROPIC_MODEL`` is NOT sufficient
while a global profile is active — settings.json silently wins and the session
runs the global model while the UI claims otherwise.

The fix is to spawn with ``--setting-sources project,local``, which drops the
USER settings file (where the global profile lives) while keeping project and
local settings — so per-session env is authoritative and CLAUDE.md/project
config still applies. :func:`ModelBinding.claude_setting_sources` returns that
value and the Claude adapter passes it on every spawn.

Verification hook: Claude Code's ``system``/``init`` event reports the model it
actually resolved. :func:`ModelBinding.expected_model` is what that field must
equal — the session asserts it and emits a loud NOTICE on mismatch rather than
letting a silently-wrong model run a whole task.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

#: Variables the profile system owns. A binding sets ALL of them or none —
#: a partial set leaves the previous provider's values live on the rest, which
#: is how "the main chat works but subagents fail" happens.
MANAGED_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
)

_TOOL_RELPATH = Path("AitherOS") / "dev" / "tools" / "claude_model_profile.py"


class ProfileError(RuntimeError):
    """A profile could not be resolved. Always raised loudly — never defaulted.

    A model binding that silently falls back to stock Anthropic when the
    requested provider is unreachable bills the wrong account and produces
    wildly different behaviour under the same session label.
    """


def _repo_root_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    candidates = [
        # .../<repo>/awdk/adk/harnesses/models.py -> parents[3] == repo root
        here.parents[3],
        here.parents[2],
        Path.cwd(),
    ]
    env_root = os.environ.get("AITHEROS_ROOT")
    if env_root:
        candidates.insert(0, Path(env_root))
    return [c for c in candidates if c]


def find_profile_tool() -> Optional[Path]:
    """Locate ``claude_model_profile.py``, or None when outside the monorepo."""
    for root in _repo_root_candidates():
        candidate = root / _TOOL_RELPATH
        if candidate.exists():
            return candidate
    return None


_tool_module: Optional[ModuleType] = None


def load_profile_tool() -> ModuleType:
    """Import the profile tool as a module (cached).

    Raises ProfileError when it cannot be found — an unresolvable profile system
    must be visible, not silently degraded into "stock Anthropic".
    """
    global _tool_module
    if _tool_module is not None:
        return _tool_module
    path = find_profile_tool()
    if path is None:
        raise ProfileError(
            f"Cannot find {_TOOL_RELPATH.as_posix()}. Run inside the AitherOS "
            "monorepo or set AITHEROS_ROOT."
        )
    spec = importlib.util.spec_from_file_location("_aither_claude_profiles", path)
    if spec is None or spec.loader is None:
        raise ProfileError(f"Cannot import profile tool at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _tool_module = module
    return module


@dataclass
class ModelBinding:
    """A resolved per-session model choice."""

    profile: str
    #: Environment overlay for the child process. Empty for the stock profile.
    env: dict[str, str] = field(default_factory=dict)
    #: The model id the harness should report once running ("" = provider default).
    expected_model: str = ""
    transport: str = "native"
    context_window: int = 0
    description: str = ""
    #: Where the credential came from, for display. Never the credential itself.
    credential_source: str = ""

    @property
    def is_stock(self) -> bool:
        return not self.env

    def claude_setting_sources(self) -> str:
        """``--setting-sources`` value for a Claude Code spawn.

        Drops ``user`` whenever this binding overrides the model, so the global
        profile in ``~/.claude/settings.json`` cannot override our process env.
        A stock binding keeps every source — it is asking for exactly the
        machine's own configuration.
        """
        return "project,local" if self.env else "user,project,local"

    def redacted(self) -> dict[str, Any]:
        """Display form. Credential values are replaced, never echoed."""
        safe_env = {
            k: ("<set>" if k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY") else v)
            for k, v in self.env.items()
        }
        return {
            "profile": self.profile,
            "expected_model": self.expected_model,
            "transport": self.transport,
            "context_window": self.context_window,
            "description": self.description,
            "credential_source": self.credential_source,
            "env": safe_env,
        }


def list_profiles() -> dict[str, dict[str, Any]]:
    """All known profiles, as ``{name: profile_dict}``."""
    return dict(load_profile_tool().load_profiles())


def resolve_binding(profile_name: str, *, bridge_url: str = "") -> ModelBinding:
    """Resolve a profile name into a per-session :class:`ModelBinding`.

    ``anthropic`` (or an empty name) yields a stock binding with no overrides.
    Any other name resolves its credential; a missing credential raises
    :class:`ProfileError` rather than quietly running on the wrong provider.
    """
    name = (profile_name or "anthropic").strip()
    tool = load_profile_tool()
    profiles = tool.load_profiles()
    if name not in profiles:
        known = ", ".join(sorted(profiles))
        raise ProfileError(f"Unknown model profile '{name}'. Known: {known}")

    profile = profiles[name]
    if profile.get("clears_all"):
        return ModelBinding(
            profile=name,
            env={},
            expected_model="",
            transport=str(profile.get("transport") or "native"),
            description=str(profile.get("description") or ""),
            credential_source="machine default",
        )

    token, source = tool.resolve_auth(profile)
    if not token:
        raise ProfileError(
            f"No credential for profile '{name}': {source}. "
            f"Set {profile.get('auth_secret') or 'the bridge token'} first."
        )

    if profile.get("transport") == "bridge":
        base_url = bridge_url or tool.DEFAULT_BRIDGE_URL
    else:
        base_url = profile["base_url"]

    model = str(profile["model"])
    subagent = profile.get("subagent_model") or profile.get("haiku_model") or model
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": str(profile.get("fable_model") or model),
        "ANTHROPIC_DEFAULT_SONNET_MODEL": str(profile.get("sonnet_model") or model),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": str(
            profile.get("haiku_model") or profile.get("subagent_model") or model
        ),
        "CLAUDE_CODE_SUBAGENT_MODEL": str(subagent),
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(profile.get("context_window") or 200000),
        "CLAUDE_CODE_EFFORT_LEVEL": str(profile.get("effort") or "high"),
    }
    return ModelBinding(
        profile=name,
        env=env,
        expected_model=model,
        transport=str(profile.get("transport") or "native"),
        context_window=int(profile.get("context_window") or 0),
        description=str(profile.get("description") or ""),
        credential_source=source,
    )


def apply_binding(base_env: dict[str, str], binding: ModelBinding) -> dict[str, str]:
    """Overlay ``binding`` onto ``base_env``, returning a new dict.

    Every managed variable is cleared first. Overlaying without clearing is how
    a switch half-applies: the new profile sets eight vars, the ninth keeps the
    previous provider's value, and one scenario silently talks to the old API.
    """
    env = dict(base_env)
    for var in MANAGED_VARS:
        env.pop(var, None)
    env.update(binding.env)
    return env
