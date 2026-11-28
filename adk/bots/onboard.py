"""Self-setup onboarding wizard and BYOK key storage for declarative bots.

Keys are stored exclusively in ``provider_keys.json`` (under ``AITHER_DATA_DIR``
when set, otherwise ``~/.aither/provider_keys.json``) with ``0o600`` POSIX
permissions — NEVER written into ``bot.yaml`` or ``.env.example``.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any, Callable

from adk.bots.manifest import (
    BOT_BACKENDS,
    BOT_STYLES,
    _default_model_for,
    load_bot_manifest,
    scaffold_bot,
    slugify_bot_id,
)
from adk.bots.validator import validate_bot

_ENV_MAP: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "aither": ("AITHER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


def _normalize_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p == "google":
        return "gemini"
    return p


def provider_keys_path(keys_path: str | Path | None = None) -> Path:
    """Resolve ``provider_keys.json`` honoring ``AITHER_DATA_DIR`` before ``~/.aither``."""
    if keys_path is not None:
        return Path(keys_path)
    data_dir = os.environ.get("AITHER_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "provider_keys.json"
    return Path.home() / ".aither" / "provider_keys.json"


def load_provider_keys(keys_path: str | Path | None = None) -> dict[str, str]:
    """Load saved BYOK provider keys from disk safely."""
    p = provider_keys_path(keys_path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def save_provider_key(
    provider: str, api_key: str, *, keys_path: str | Path | None = None
) -> Path:
    """Persist a BYOK *api_key* for *provider* into ``provider_keys.json`` (chmod 0600)."""
    norm = _normalize_provider(provider)
    if not norm:
        raise ValueError("Provider name must be non-empty")
    secret = (api_key or "").strip()
    if not secret:
        raise ValueError("API key must be non-empty")

    p = provider_keys_path(keys_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load_provider_keys(p)
    data[norm] = secret
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(p, 0o600)
    return p


def resolve_provider_key(
    provider: str, *, keys_path: str | Path | None = None
) -> str:
    """Resolve an API key from environment or ``provider_keys.json`` (normalizing gemini/google)."""
    norm = _normalize_provider(provider)
    if not norm:
        return ""
    for env_name in _ENV_MAP.get(norm, (f"{norm.upper()}_API_KEY",)):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val

    keys = load_provider_keys(keys_path)
    if norm == "gemini":
        return (keys.get("gemini") or keys.get("google") or "").strip()
    return (keys.get(norm) or "").strip()


def run_bot_onboard(
    *,
    name: str = "",
    style: str = "",
    target_dir: str | Path | None = None,
    backend: str = "auto",
    model: str = "",
    instructions: str = "",
    description: str = "",
    api_key: str = "",
    keys_path: str | Path | None = None,
    non_interactive: bool = False,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] | None = None,
    overwrite: bool = False,
    model_sha256: str | None = None,
    shared_ro_mmap: bool = False,
    target_surface: str = "standard",
) -> dict[str, Any]:
    """Run the bot setup/onboarding flow (interactive or non-interactive)."""
    get_secret = secret_fn or getpass.getpass

    resolved_style = (style or "").strip().lower()
    if not resolved_style:
        if non_interactive:
            resolved_style = "gemini"
        else:
            ans = (input_fn("Bot style [gemini/openai] (gemini): ") or "gemini").strip().lower()
            resolved_style = ans
    if resolved_style not in BOT_STYLES:
        raise ValueError(f"Invalid bot style {resolved_style!r}; expected {BOT_STYLES}")

    resolved_name = (name or "").strip()
    if not resolved_name:
        if non_interactive:
            resolved_name = f"my-{resolved_style}-bot"
        else:
            resolved_name = (input_fn("Bot name (my-aither-bot): ") or "my-aither-bot").strip()

    resolved_backend = (backend or "").strip().lower() or "auto"
    if not non_interactive and not backend:
        ans_b = (
            input_fn(f"Backend {BOT_BACKENDS} (auto): ") or "auto"
        ).strip().lower()
        resolved_backend = ans_b
    if resolved_backend not in BOT_BACKENDS:
        raise ValueError(f"Invalid backend {resolved_backend!r}; expected {BOT_BACKENDS}")

    resolved_model = (model or "").strip() or _default_model_for(
        resolved_style, resolved_backend
    )
    if not non_interactive and not model:
        ans_m = input_fn(f"Model ({resolved_model}): ").strip()
        if ans_m:
            resolved_model = ans_m

    resolved_instructions = (instructions or "").strip()
    if not non_interactive and not resolved_instructions:
        ans_i = input_fn("System instructions (leave blank for default): ").strip()
        if ans_i:
            resolved_instructions = ans_i

    bot_slug = slugify_bot_id(resolved_name)
    dest = Path(target_dir) if target_dir is not None else (Path.cwd() / bot_slug)

    key_saved = False
    saved_keys_file: Path | None = None
    supplied_key = (api_key or "").strip()
    if not supplied_key and not non_interactive and resolved_backend in ("gemini", "openai"):
        existing = resolve_provider_key(resolved_backend, keys_path=keys_path)
        if not existing:
            supplied_key = (
                get_secret(f"Enter {resolved_backend} API key (optional, stored in provider_keys.json): ")
                or ""
            ).strip()

    if supplied_key:
        target_provider = (
            resolved_backend
            if resolved_backend in ("gemini", "openai", "aither")
            else resolved_style
        )
        saved_keys_file = save_provider_key(
            target_provider, supplied_key, keys_path=keys_path
        )
        key_saved = True

    bot_path = scaffold_bot(
        resolved_name,
        resolved_style,
        dest,
        backend=resolved_backend,
        model=resolved_model,
        instructions=resolved_instructions or None,
        description=description,
        overwrite=overwrite,
        model_sha256=model_sha256,
        shared_ro_mmap=shared_ro_mmap,
        target_surface=target_surface,
    )
    manifest = load_bot_manifest(bot_path)
    validation = validate_bot(bot_path)
    return {
        "ok": validation.ok,
        "bot_dir": str(bot_path),
        "bot_id": manifest.id,
        "style": manifest.style,
        "backend": manifest.backend,
        "model": manifest.model,
        "validation": validation,
        "key_saved": key_saved,
        "keys_path": str(saved_keys_file) if saved_keys_file else "",
    }


def bot_main(argv: list[str] | None = None) -> int:
    """Delegate to ``adk.bots.cli_entry.bot_main``."""
    from adk.bots.cli_entry import bot_main as _bot_main

    return _bot_main(argv)
