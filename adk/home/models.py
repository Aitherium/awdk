"""Model choice for Agent Home: local (Bonsai / llama.cpp / Ollama / awnode) or BYO key.

Every choice maps onto a provider ``adk.llm.LLMRouter`` already knows, so the
agent's model is built by the same code every adk agent uses.
"""

from __future__ import annotations

import os
import sys
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


#: awnode's default bind (``awnode start --port``, default 8090) + its OpenAI root.
AWNODE_URL = "http://127.0.0.1:8090/v1"

PRESETS: Dict[str, Preset] = {
    # local -- nothing leaves the machine
    # hint filled per OS by bonsai_install_hint(): `curl | sh` has no `sh` on Windows.
    "bonsai": Preset("local", "bonsai", "http://127.0.0.1:8080/v1", "bonsai-selfhost"),
    "llamacpp": Preset("local", "llamacpp", "http://127.0.0.1:8080/v1", "",
                       hint="run: llama-server -m <model.gguf> --port 8080"),
    "ollama": Preset("local", "ollama", "http://localhost:11434", "gemma4:4b",
                     hint="install Ollama, then: ollama pull gemma4:4b"),
    # awnode: this machine's own gateway (OpenAI-compatible /v1 on :8090). It routes
    # to whichever local backend it found (Bonsai, llama.cpp, vLLM, Ollama) and
    # resolves the unpinned model "auto" to one that backend actually serves.
    # Loopback callers need no token (awnode _require_node_owner).
    "awnode": Preset("local", "llamacpp", AWNODE_URL, "auto",
                     hint="install: pip install awnode; run: awnode start (serves "
                          ":8090 and routes to the local model it finds)"),
    # bring your own key
    "deepseek": Preset("byo", "deepseek", "https://api.deepseek.com/v1", "deepseek-chat",
                       key_env="DEEPSEEK_API_KEY"),
    "openai": Preset("byo", "openai", "https://api.openai.com/v1", "gpt-4o-mini",
                     key_env="OPENAI_API_KEY"),
    "anthropic": Preset("byo", "anthropic", "", "claude-sonnet-4-6",
                        key_env="ANTHROPIC_API_KEY"),
}

BONSAI_SH = "https://aitherium.com/install-bonsai.sh"
BONSAI_PS1 = "https://aitherium.com/install-bonsai.ps1"


def bonsai_install_hint(platform: str = "") -> str:
    """The Bonsai install line for THIS OS. The installer picks CPU / Vulkan / CUDA
    itself, serves llama-server on 127.0.0.1:8080 as ``bonsai-selfhost``."""
    plat = platform or sys.platform
    if plat.startswith("win"):
        return ("install (PowerShell): iwr " + BONSAI_PS1 + " -OutFile install-bonsai.ps1; "
                "powershell -ExecutionPolicy Bypass -File .\\install-bonsai.ps1"
                "  (CPU or GPU is detected; -Backend cpu|vulkan|cuda forces one)")
    return ("install: curl -fsSL " + BONSAI_SH + " -o install-bonsai.sh && "
            "sh install-bonsai.sh  (CPU or GPU is detected; --backend cpu|vulkan|cuda forces one)")


def hint_for(provider: str, platform: str = "") -> str:
    if provider == "bonsai":
        return bonsai_install_hint(platform)
    p = PRESETS.get(provider)
    return p.hint if p else ""


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
            "hint": hint_for(cfg.provider) if p else ""}


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
        return {"ok": False, "detail": f"{url} unreachable ({type(exc).__name__}). "
                                       f"{hint_for(cfg.provider)}"}
    if cfg.provider == "awnode" and r.status_code == 200:
        return _awnode_verdict(url, r)
    return {"ok": r.status_code == 200, "detail": f"{url} -> {r.status_code}"}


def _awnode_verdict(url: str, r: Any) -> Dict[str, Any]:
    """awnode answers /v1/models 200 with an EMPTY list when no backend is up --
    the gateway is alive but every chat would 503. That is not a usable model."""
    try:
        data = r.json().get("data") or []
    except (ValueError, AttributeError):
        return {"ok": False, "detail": f"{url} -> 200 but not JSON: is this awnode?"}
    served = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
    if not served:
        return {"ok": False, "detail": f"{url} -> 200, but awnode serves no model: start "
                                       "a local backend it can route to (Bonsai / "
                                       "llama-server / vLLM / Ollama), then re-check"}
    backends = sorted({str(m.get("owned_by")) for m in data
                       if isinstance(m, dict) and m.get("owned_by")})
    return {"ok": True, "models": served,
            "detail": f"{url} -> 200, {len(served)} model(s) via "
                      f"{', '.join(backends) or 'awnode'}"}
