"""Guided backend onboarding — actionable setup help for every LLM provider.

The owner directive behind this module: when someone tries to use a backend that
isn't configured yet (no API key, server not running, model not pulled), the
error must GUIDE them step-by-step instead of dead-ending. Every "no key" /
"no backend available" raise site in adk should route its message through here.

Usage:
    from adk.llm.onboarding import guide, missing_key_message, provider_menu
    raise RuntimeError(missing_key_message("moonshot"))
    print(guide("ollama"))
    print(provider_menu())

Keep guides SHORT (a terminal error, not a manual): where to get the key/server,
the exact command to configure it, the exact command to test it.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# ── Guide registry ──────────────────────────────────────────────────────────
# kind: "cloud" (hosted API, needs key) | "gateway" (Aitherium hosted) |
#       "local" (self-hosted server/runtime, needs a running endpoint)
PROVIDER_GUIDES: Dict[str, Dict[str, object]] = {
    "moonshot": {
        "label": "Moonshot Kimi (kimi-k3)",
        "kind": "cloud",
        "key_env": "MOONSHOT_API_KEY",
        "key_url": "https://platform.moonshot.ai/console/api-keys",
        "steps": [
            "Create an account + API key: https://platform.moonshot.ai (Console → API Keys)",
            "Configure:  adk keys set moonshot sk-...   (stores + vault-syncs; or export MOONSHOT_API_KEY=sk-...)",
            "Test:       adk ask \"hello\" --backend moonshot",
        ],
        "aliases": ["kimi", "kimi-k3"],
    },
    "deepseek": {
        "label": "DeepSeek (deepseek-chat / deepseek-reasoner)",
        "kind": "cloud",
        "key_env": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
        "steps": [
            "Create an API key: https://platform.deepseek.com/api_keys",
            "Configure:  adk keys set deepseek sk-...   (stores + vault-syncs; or export DEEPSEEK_API_KEY=sk-...)",
            "Test:       adk ask \"hello\" --backend deepseek",
        ],
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "kind": "cloud",
        "key_env": "ANTHROPIC_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
        "steps": [
            "Create an API key: https://console.anthropic.com/settings/keys",
            "Configure:  adk keys set anthropic sk-ant-...   (stores + vault-syncs; or export ANTHROPIC_API_KEY=...)",
            "Test:       adk ask \"hello\" --backend anthropic",
        ],
        "aliases": ["claude"],
    },
    "openai": {
        "label": "OpenAI",
        "kind": "cloud",
        "key_env": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "steps": [
            "Create an API key: https://platform.openai.com/api-keys",
            "Configure:  adk keys set openai sk-...   (stores + vault-syncs; or export OPENAI_API_KEY=sk-...)",
            "Test:       adk ask \"hello\" --backend openai",
        ],
    },
    "gemini": {
        "label": "Google Gemini",
        "kind": "cloud",
        "key_env": "GEMINI_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "steps": [
            "Create an API key: https://aistudio.google.com/apikey",
            "Configure:  export GEMINI_API_KEY=...",
            "Test:       adk ask \"hello\" --backend gemini",
        ],
        "aliases": ["google"],
    },
    "groq": {
        "label": "Groq",
        "kind": "cloud",
        "key_env": "GROQ_API_KEY",
        "key_url": "https://console.groq.com/keys",
        "steps": [
            "Create an API key: https://console.groq.com/keys",
            "Configure:  adk keys set groq gsk_...   (stores + vault-syncs; or export GROQ_API_KEY=gsk_...)",
        ],
    },
    "together": {
        "label": "Together AI",
        "kind": "cloud",
        "key_env": "TOGETHER_API_KEY",
        "key_url": "https://api.together.xyz/settings/api-keys",
        "steps": [
            "Create an API key: https://api.together.xyz/settings/api-keys",
            "Configure:  adk keys set together <key>   (stores + vault-syncs)",
        ],
    },
    "gateway": {
        "label": "Aitherium gateway (hosted brain — free tier)",
        "kind": "gateway",
        "key_env": "AITHER_API_KEY",
        "key_url": "https://gateway.aitherium.com",
        "steps": [
            "Register (free):  aither register    (device flow, no card)",
            "Or grab a key at https://gateway.aitherium.com then: export AITHER_API_KEY=aither_sk_...",
            "Test:             adk ask \"hello\"   (gateway is the default fallback)",
        ],
        "aliases": ["aitherium", "aither"],
    },
    "ollama": {
        "label": "Ollama (local, easiest self-hosted)",
        "kind": "local",
        "steps": [
            "Install: https://ollama.com/download  (winget install Ollama.Ollama / brew install ollama)",
            "Start + pull a model:  ollama serve   then   ollama pull gemma4:4b",
            "Configure:  adk backend set ollama    (auto-detects localhost:11434)",
            "Test:       adk ask \"hello\" --backend ollama",
        ],
    },
    "vllm": {
        "label": "vLLM (local GPU server, OpenAI-compatible)",
        "kind": "local",
        "steps": [
            "Easiest: let adk provision it —  adk node bootstrap   (detects your GPU, serves a fitting model)",
            "Manual:  docker run --gpus all -p 8000:8000 vllm/vllm-openai --model <hf-model>",
            "Configure:  adk backend set vllm --base-url http://localhost:8000/v1 --model <served-model>",
        ],
    },
    "llamacpp": {
        "label": "llama.cpp (CPU-friendly local server)",
        "kind": "local",
        "steps": [
            "Get llama.cpp:  https://github.com/ggml-org/llama.cpp/releases  (or adk node bootstrap on CPU boxes)",
            "Serve OpenAI-compatible:  llama-server -m model.gguf --port 8080",
            "Configure:  adk backend set llamacpp --base-url http://localhost:8080/v1",
        ],
    },
    "lmstudio": {
        "label": "LM Studio (desktop app, local server)",
        "kind": "local",
        "steps": [
            "Install LM Studio: https://lmstudio.ai — download a model, enable the local server",
            "Configure:  adk backend set lmstudio --base-url http://localhost:1234/v1",
        ],
    },
    "genesis": {
        "label": "AitherOS Genesis (your own fleet)",
        "kind": "local",
        "steps": [
            "Requires a running AitherOS deployment (Genesis on :8001).",
            "Configure:  adk backend set genesis --base-url https://localhost:8001/v1",
        ],
    },
}

# Fleet/local MODEL names people try as if they were providers — map each to the
# provider guide that actually serves it, with a hint line.
MODEL_HINTS: Dict[str, str] = {
    "bonsai": "vllm",
    "nemotron": "gateway",
    "nemotron-orchestrator": "gateway",
    "aither-orchestrator": "gateway",
    "gemma4": "ollama",
    "gemma4-12b": "gateway",
    "qwen3.6": "gateway",
    "qwen36": "gateway",
    "kimi-k3": "moonshot",
    "kimi": "moonshot",
}

_ALIAS_INDEX: Dict[str, str] = {}
for _name, _g in PROVIDER_GUIDES.items():
    _ALIAS_INDEX[_name] = _name
    for _a in _g.get("aliases", []):  # type: ignore[union-attr]
        _ALIAS_INDEX[str(_a)] = _name


def resolve_name(name: str) -> Optional[str]:
    """Resolve a provider name, alias, or known model name to a guide key."""
    n = (name or "").strip().lower()
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n]
    if n in MODEL_HINTS:
        return MODEL_HINTS[n]
    # Longest prefix wins so "gemma4-12b" (gateway) beats "gemma4" (ollama).
    for prefix in sorted(MODEL_HINTS, key=len, reverse=True):
        if n.startswith(prefix):
            return MODEL_HINTS[prefix]
    return None


def guide(name: str) -> str:
    """Full setup guide for a provider (or a model name that maps to one)."""
    key = resolve_name(name)
    if not key:
        return provider_menu(header=f"Unknown backend '{name}'. Available backends:")
    g = PROVIDER_GUIDES[key]
    lines: List[str] = [f"{g['label']} — setup:"]
    n = (name or "").strip().lower()
    if n in MODEL_HINTS and key != n:
        lines.insert(0, f"'{name}' is a model served via the '{key}' backend.")
    for i, step in enumerate(g["steps"], 1):  # type: ignore[index]
        lines.append(f"  {i}. {step}")
    return "\n".join(lines)


def missing_key_message(name: str) -> str:
    """Error message for a cloud provider selected without its API key —
    states the problem AND the exact fix, never a bare 'no key'."""
    key = resolve_name(name) or name
    g = PROVIDER_GUIDES.get(key)
    if not g or g.get("kind") == "local":
        return guide(name)
    env = g.get("key_env", f"{key.upper()}_API_KEY")
    return (
        f"{g['label']}: no API key configured ({env} not set).\n" + guide(key)
    )


def provider_menu(header: str = "Available LLM backends:") -> str:
    """One-line-per-provider menu, grouped cloud/gateway/local, with the
    guide command so the user always has a next step."""
    groups = {"gateway": [], "cloud": [], "local": []}  # type: Dict[str, List[str]]
    for name, g in PROVIDER_GUIDES.items():
        groups[str(g["kind"])].append(f"  {name:<10} {g['label']}")
    out = [header]
    for kind, title in (("gateway", "Hosted (free tier)"), ("cloud", "Cloud APIs"), ("local", "Self-hosted / local")):
        out.append(f"{title}:")
        out.extend(sorted(groups[kind]))
    out.append("Setup help for any of them:  adk backend guide <name>")
    return "\n".join(out)
