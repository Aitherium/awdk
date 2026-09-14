"""
AitherShell Configuration
=========================

Config resolution order (later overrides earlier):
1. Built-in defaults
2. ~/.aither/config.yaml
3. .aither.yaml (project-local)
4. Environment variables (AITHER_*)
5. CLI flags
"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any

CONFIG_DIR = Path.home() / ".aither"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
PROJECT_CONFIG = Path(".aither.yaml")
SESSION_FILE = CONFIG_DIR / "session.json"
PLUGINS_DIR = CONFIG_DIR / "plugins"
PRIVATE_DIR = CONFIG_DIR / "private"


@dataclass
class AitherConfig:
    """Shell configuration with layered resolution."""

    # Connection
    # Host-side default: the genesis LB publishes PLAIN HTTP on :8001 (TLS is
    # the in-network listener), and 127.0.0.1 avoids the ::1-first resolution
    # tax. Measured: the old https://localhost:8001 default made `aither
    # --status` report UNREACHABLE while genesis answered on http://127.0.0.1.
    # Override with AITHER_URL / config for in-network or remote topologies.
    url: str = "http://127.0.0.1:8001"
    will_url: str = "https://localhost:8097"
    gateway_url: str = "https://gateway.aitherium.com"

    # Auth
    api_key: str = ""
    tenant_id: str = ""
    identity_url: str = "https://localhost:8112"

    # Auth state (populated from ~/.aither/auth.json at load time)
    auth_token: Optional[str] = None
    auth_user: Optional[Dict[str, Any]] = None

    # LLM backend: genesis (default), ollama, vllm, openai, auto
    llm_backend: str = "genesis"
    llm_url: str = ""

    # Defaults
    model: Optional[str] = None
    persona: Optional[str] = None
    effort: Optional[int] = None
    safety_level: str = "professional"
    max_tokens: Optional[int] = None

    # Behavior
    stream: bool = True
    rich_output: bool = True
    show_thinking: bool = True
    show_metadata: bool = False

    # Session
    session_id: Optional[str] = None
    last_session_id: Optional[str] = None
    auto_continue: bool = False

    # Shell
    prompt: str = "aither> "
    history_file: str = str(CONFIG_DIR / "history")
    max_history: int = 10000

    # Plugins
    plugin_dirs: list = field(default_factory=lambda: [str(PLUGINS_DIR)])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ensure_dirs():
    """Create config directories if needed."""
    for d in [CONFIG_DIR, PLUGINS_DIR, PRIVATE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> AitherConfig:
    """Load config with layered resolution."""
    _ensure_dirs()
    cfg = AitherConfig()

    # Layer 1: ~/.aither/config.yaml
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = yaml.safe_load(f) or {}
            _apply_dict(cfg, data)
        except Exception:
            pass

    # Layer 2: .aither.yaml (project-local)
    if PROJECT_CONFIG.exists():
        try:
            with open(PROJECT_CONFIG, "r") as f:
                data = yaml.safe_load(f) or {}
            _apply_dict(cfg, data)
        except Exception:
            pass

    # Layer 3: Environment variables
    env_map = {
        "AITHER_URL": "url",
        "AITHER_ORCHESTRATOR_URL": "url",
        "AITHER_WILL_URL": "will_url",
        "AITHER_GATEWAY_URL": "gateway_url",
        "AITHER_LLM_BACKEND": "llm_backend",
        "AITHER_LLM_URL": "llm_url",
        "AITHER_API_KEY": "api_key",
        "AITHER_TENANT_ID": "tenant_id",
        "AITHER_IDENTITY_URL": "identity_url",
        "AITHER_MODEL": "model",
        "AITHER_PERSONA": "persona",
        "AITHER_EFFORT": "effort",
        "AITHER_SAFETY": "safety_level",
        "AITHER_MAX_TOKENS": "max_tokens",
        "AITHER_PROMPT": "prompt",
    }
    for env_key, attr in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            # Type coercion
            current = getattr(cfg, attr)
            if isinstance(current, int) or attr in ("effort", "max_tokens"):
                try:
                    val = int(val)
                except ValueError:
                    continue
            elif isinstance(current, bool):
                val = val.lower() in ("1", "true", "yes")
            setattr(cfg, attr, val)

    # Layer 4: Load auth token from ~/.aither/auth.json
    # If no auth exists, auto-provision root (like Linux console login).
    try:
        from adk.shell.auth import AuthStore, ensure_root_profile
        token = AuthStore.get_active_token()
        if not token:
            # No valid session — provision root for local use
            ensure_root_profile()
            token = AuthStore.get_active_token()
        if token:
            cfg.auth_token = token
            if not cfg.api_key:
                cfg.api_key = token
            user = AuthStore.get_active_user()
            if user:
                cfg.auth_user = user
                if not cfg.tenant_id and user.get("tenant_id"):
                    cfg.tenant_id = user["tenant_id"]
    except Exception:
        pass  # auth module not available or broken store

    return cfg


def save_default_config():
    """Write a default config.yaml if none exists."""
    _ensure_dirs()
    if CONFIG_FILE.exists():
        return
    default = """# AitherShell Configuration
# ========================
# Edit this file to set defaults. CLI flags override these.
# Env vars (AITHER_URL, AITHER_MODEL, etc.) also override.

# LLM backend: genesis | ollama | vllm | openai | auto
#   genesis = AitherOS orchestrator (default, full pipeline)
#   ollama  = Direct Ollama connection (standalone)
#   vllm    = Direct vLLM connection (standalone)
#   openai  = Any OpenAI-compatible API (standalone)
#   auto    = Try genesis first, then auto-detect local LLM
llm_backend: genesis

# LLM URL (only used when llm_backend is not genesis)
# llm_url: http://localhost:11434     # Ollama
# llm_url: http://localhost:8199      # vLLM
# llm_url: https://api.openai.com/v1  # OpenAI

# Genesis connection (used when llm_backend is genesis)
url: https://localhost:8001
will_url: https://localhost:8097

# Default model (null = auto-select)
# model: aither-orchestrator

# Default effort level (1-10, null = auto)
# effort: 5

# Safety level: professional | casual | unrestricted
safety_level: professional

# Default persona
# persona: aither

# Streaming (disable for scripts)
stream: true

# Rich terminal output (disable for plain text)
rich_output: true

# Show model thinking/reasoning
show_thinking: true

# Show metadata (effort, neurons, elapsed)
show_metadata: false

# Shell prompt
prompt: "aither> "

# Plugin directories (list)
plugin_dirs:
  - ~/.aither/plugins
"""
    with open(CONFIG_FILE, "w") as f:
        f.write(default)


def save_config(cfg: AitherConfig):
    """Write current config to ~/.aither/config.yaml (preserving comments)."""
    _ensure_dirs()
    data = {
        "url": cfg.url,
        "gateway_url": cfg.gateway_url,
        "api_key": cfg.api_key,
        "tenant_id": cfg.tenant_id,
        "safety_level": cfg.safety_level,
        "stream": cfg.stream,
        "rich_output": cfg.rich_output,
        "show_thinking": cfg.show_thinking,
        "show_metadata": cfg.show_metadata,
    }
    if cfg.model:
        data["model"] = cfg.model
    if cfg.persona:
        data["persona"] = cfg.persona
    if cfg.effort:
        data["effort"] = cfg.effort
    if cfg.session_id:
        data["session_id"] = cfg.session_id
    # Only write non-default values
    data = {k: v for k, v in data.items() if v is not None and v != ""}
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _apply_dict(cfg: AitherConfig, data: dict):
    """Apply a dict to config, ignoring unknown keys."""
    for key, val in data.items():
        if hasattr(cfg, key) and val is not None:
            setattr(cfg, key, val)
