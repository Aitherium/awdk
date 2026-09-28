"""Agent Home configuration: one folder the owner can read, edit and back up.

Layout (default ``~/.aither/agent-home``, override with ``AITHER_AGENT_HOME``)::

    home.json                 name, model choice, harness choice
    persona/system_prompt.md  the agent's system prompt -- edit freely
    persona/persona.md        who the agent is: voice, values, goals
    persona/rules.md          hard rules the agent must keep
    harness/                  generated configs for Claude Code / OpenClaw / Hermes
    games/                    what the agent learned per game room
    memory/                   the agent's local memory database

No secret VALUE is ever written here: a BYO key is referenced by the name of
the environment variable that holds it (``api_key_env``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

HOME_ENV = "AITHER_AGENT_HOME"
CONFIG_NAME = "home.json"
PERSONA_FILES = ("system_prompt.md", "persona.md", "rules.md")


class HomeError(RuntimeError):
    """Agent Home is not set up, or a choice is invalid. The message says the fix."""


def home_dir() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw) if raw else Path.home() / ".aither" / "agent-home"


@dataclass
class ModelConfig:
    mode: str = "local"          # local | byo
    provider: str = "bonsai"     # bonsai | llamacpp | ollama | deepseek | openai | anthropic
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""        # name of the env var holding a BYO key

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HarnessConfig:
    kind: str = "aither"         # aither | claude | openclaw | hermes
    mcp_url: str = ""
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HomeConfig:
    name: str = "my-agent"
    model: ModelConfig = field(default_factory=ModelConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    created_at: float = 0.0
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeConfig":
        m = data.get("model") or {}
        h = data.get("harness") or {}
        return cls(
            name=str(data.get("name") or "my-agent"),
            model=ModelConfig(**{k: m[k] for k in ModelConfig().to_dict() if k in m}),
            harness=HarnessConfig(
                kind=str(h.get("kind") or "aither"),
                mcp_url=str(h.get("mcp_url") or ""),
                options=dict(h.get("options") or {})),
            created_at=float(data.get("created_at") or 0.0),
            version=int(data.get("version") or 1),
        )


def config_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / CONFIG_NAME


def is_initialized(root: Optional[Path] = None) -> bool:
    return config_path(root).is_file()


def load_config(root: Optional[Path] = None) -> HomeConfig:
    path = config_path(root)
    if not path.is_file():
        raise HomeError(f"no Agent Home at {path.parent} -- run `adk home init` first")
    try:
        return HomeConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError) as exc:
        raise HomeError(f"{path} is not valid Agent Home config ({exc}); "
                        "fix it or re-run `adk home init --force`") from exc


def save_config(cfg: HomeConfig, root: Optional[Path] = None) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


DEFAULT_SYSTEM_PROMPT = """\
You are {name}, a personal agent that runs on its owner's own machine.
You can talk, use tools, and join games. In a game you observe, choose one
action at a time, and remember what worked so you play better next time.
Be concise. Never invent facts about the game world you have not observed.
"""

DEFAULT_PERSONA = """\
# {name}

- Voice: warm, curious, a little playful.
- Values: honesty, curiosity, fair play.
- Goals: help my owner; explore every game I join; get better each session.
"""

DEFAULT_RULES = """\
- Never share my owner's keys, tokens or files with anyone in a game.
- Follow each game's rules and its community's code of conduct.
- In a shared room, be kind to other players.
"""


def init_home(name: str = "my-agent", root: Optional[Path] = None,
              force: bool = False) -> HomeConfig:
    """Create the folder, config and editable persona files. Idempotent."""
    base = root or home_dir()
    if is_initialized(base) and not force:
        return load_config(base)
    cfg = HomeConfig(name=name, created_at=time.time())
    save_config(cfg, base)
    pdir = base / "persona"
    pdir.mkdir(parents=True, exist_ok=True)
    for fname, template in (("system_prompt.md", DEFAULT_SYSTEM_PROMPT),
                            ("persona.md", DEFAULT_PERSONA),
                            ("rules.md", DEFAULT_RULES)):
        p = pdir / fname
        if force or not p.exists():
            p.write_text(template.format(name=name), encoding="utf-8")
    for sub in ("harness", "games", "memory"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return cfg


def persona_dir(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "persona"


def read_persona(root: Optional[Path] = None) -> Dict[str, str]:
    pdir = persona_dir(root)
    out: Dict[str, str] = {}
    for fname in PERSONA_FILES:
        p = pdir / fname
        out[fname] = p.read_text(encoding="utf-8") if p.is_file() else ""
    return out


def write_persona_file(fname: str, text: str, root: Optional[Path] = None) -> Path:
    if fname not in PERSONA_FILES:
        raise HomeError(f"persona file must be one of {PERSONA_FILES}, got {fname!r}")
    p = persona_dir(root) / fname
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def compose_system_prompt(root: Optional[Path] = None) -> str:
    """system_prompt.md + persona.md + rules.md, in that order."""
    files = read_persona(root)
    parts = [files["system_prompt.md"].strip()]
    if files["persona.md"].strip():
        parts.append("## Who you are\n" + files["persona.md"].strip())
    if files["rules.md"].strip():
        parts.append("## Rules you always keep\n" + files["rules.md"].strip())
    return "\n\n".join(p for p in parts if p)
