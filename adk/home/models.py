"""Model choice for Agent Home: local (Bonsai / llama.cpp / Ollama) or BYO key.

Every choice maps onto a provider ``adk.llm.LLMRouter`` already knows, so the
agent's model is built by the same code every adk agent uses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import HomeError, ModelConfig


@dataclass(frozen=True)
class Preset:
    mode: str           # local | byo
    router: str         # LLMRouter provider name
    base_url: str
    model: str
    key_env: str = ""
    hint: str = ""


PRESETS: Dict[str, Preset] = {
    # local -- nothing leaves the machine
    "bonsai": Preset("local", "bonsai", "http://127.0.0.1:8080/v1", "bonsai-selfhost",
                     hint="install: curl -fsSL https://aitherium.com/install-bonsai.sh | sh"
                          "  (or `adk bonsai-local` for the Docker build on :8090)"),
    "llamacpp": Preset("local", "llamacpp", "http://127.0.0.1:8080/v1", "",
                       hint="run: llama-server -m <model.gguf> --port 8080"),
    "ollama": Preset("local", "ollama", "http://localhost:11434", "gemma4:4b",
                     hint="install Ollama, then: ollama pull gemma4:4b"),
    # bring your own key
    "deepseek": Preset("byo", "deepseek", "https://api.deepseek.com/v1", "deepseek-chat",
                       key_env="DEEPSEEK_API_KEY"),
    "openai": Preset("byo", "openai", "https://api.openai.com/v1", "gpt-4o-mini",
                     key_env="OPENAI_API_KEY"),
    "anthropic": Preset("byo", "anthropic", "", "claude-sonnet-4-6",
                        key_env="ANTHROPIC_API_KEY"),
}

LOCAL = tuple(k for k, p in PRESETS.items() if p.mode == "local")
BYO = tuple(k for k, p in PRESETS.items() if p.mode == "byo")


def choose_model(provider: str, model: str = "", base_url: str = "",
                 api_key_env: str = "") -> ModelConfig:
    """Validate a choice and fill defaults. Never touches a key's value."""
    p = PRESETS.get(provider)
    if p is None:
        raise HomeError(f"unknown model provider {provider!r}; local: "
                        f"{', '.join(LOCAL)}; bring-your-own-key: {', '.join(BYO)}")
    if api_key_env and not api_key_env.replace("_", "").isalnum():
        raise HomeError("--key-env takes the NAME of an environment variable "
                        "(e.g. DEEPSEEK_API_KEY), never the key itself")
    return ModelConfig(mode=p.mode, provider=provider, model=model or p.model,
                       base_url=base_url or p.base_url,
                       api_key_env=api_key_env or p.key_env)


def describe(cfg: ModelConfig) -> Dict[str, Any]:
    p = PRESETS.get(cfg.provider)
    key_set = bool(cfg.api_key_env and os.environ.get(cfg.api_key_env))
    return {"mode": cfg.mode, "provider": cfg.provider, "model": cfg.model,
            "base_url": cfg.base_url, "api_key_env": cfg.api_key_env,
            "api_key_present": key_set if cfg.mode == "byo" else None,
            "hint": p.hint if p else ""}


def build_llm(cfg: ModelConfig) -> Any:
    """-> an ``adk.llm.LLMRouter`` for this choice. BYO without a key raises."""
    p = PRESETS.get(cfg.provider)
    if p is None:
        raise HomeError(f"unknown model provider {cfg.provider!r}")
    api_key: Optional[str] = None
    if cfg.mode == "byo":
        api_key = os.environ.get(cfg.api_key_env or p.key_env, "")
        if not api_key:
            raise HomeError(f"{cfg.provider} needs a key: set the environment "
                            f"variable {cfg.api_key_env or p.key_env}")
    from adk.llm import LLMRouter

    return LLMRouter(provider=p.router, base_url=cfg.base_url or None,
                     api_key=api_key, model=cfg.model or None)


def probe(cfg: ModelConfig, timeout: float = 3.0) -> Dict[str, Any]:
    """Is the chosen model reachable? Local: list models. BYO: key present."""
    if cfg.mode == "byo":
        ok = bool(os.environ.get(cfg.api_key_env))
        return {"ok": ok, "detail": "key present" if ok else
                f"set {cfg.api_key_env} in your environment"}
    import httpx

    url = cfg.base_url.rstrip("/")
    url = f"{url}/api/tags" if cfg.provider == "ollama" else f"{url}/models"
    try:
        r = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        p = PRESETS[cfg.provider]
        return {"ok": False, "detail": f"{url} unreachable ({type(exc).__name__}). "
                                       f"{p.hint}"}
    return {"ok": r.status_code == 200, "detail": f"{url} -> {r.status_code}"}
