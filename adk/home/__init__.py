"""adk.home -- Agent Home: download and host your own agent.

Pick a model (local Bonsai / llama.cpp / Ollama, or your own DeepSeek / OpenAI /
Anthropic key), edit its persona files, choose the harness that runs it (adk's
native loop, Claude Code, OpenClaw or Hermes), and let it join games through
:mod:`adk.games`, where it explores, remembers and improves across sessions.

CLI: ``adk home …`` (or ``python -m adk.home …``). Guide: ``docs/agent-home.md``.
"""

from .config import (
    HomeConfig,
    HomeError,
    compose_system_prompt,
    home_dir,
    init_home,
    load_config,
    save_config,
)
from .entitlement import PACK_ID, LicenseWouldDropPacks, has_pack, install_license

__all__ = [
    "HomeConfig",
    "HomeError",
    "LicenseWouldDropPacks",
    "PACK_ID",
    "compose_system_prompt",
    "has_pack",
    "home_dir",
    "init_home",
    "install_license",
    "load_config",
    "save_config",
]
