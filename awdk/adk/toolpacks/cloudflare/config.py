"""Cloudflare pack configuration + secret resolution.

Resolution order for the API token (and account/zone ids):

    environment variable            CLOUDFLARE_API_TOKEN | CF_API_TOKEN
      -> adk pack settings          packs_settings.cloudflare in the saved config
      -> adk provider-key store     ~/.aither/provider_keys.json  (0600)
      -> AitherSecrets vault        https://127.0.0.1:8111/secrets/<NAME>

The token is NEVER hardcoded, never logged and never returned to the agent —
every tool routes outbound text through ``redact()`` and only ever shows
``mask_key()`` output (see the repo secret-safety rules).

The account MCP server (mcp.cloudflare.com) needs browser OAuth, which a
headless agent cannot perform; therefore account operations use the REST API v4
with this token. The DOCS MCP server needs no credential at all.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("cloudflare_pack")

API_BASE = "https://api.cloudflare.com/client/v4"
GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
DOCS_MCP_URL = "https://docs.mcp.cloudflare.com/mcp"

DEFAULTS = {
    "api_base": API_BASE,
    "vault_url": "https://127.0.0.1:8111",
    "account_id": "",
    "zone_id": "",
    "compatibility_date": "2025-01-01",
}

# In-process cache so we do not hammer the vault on every tool call.
_SECRET_CACHE: dict[str, str] = {}


# ── settings ────────────────────────────────────────────────────────────


def _pack_settings() -> dict:
    """Best-effort read of the adk per-pack settings (absent outside adk)."""
    try:
        from adk.config import load_saved_config  # type: ignore
        all_settings = load_saved_config().get("packs_settings") or {}
        s = all_settings.get("cloudflare")
        return dict(s) if isinstance(s, dict) else {}
    except Exception:  # noqa: BLE001 — config store is optional context
        return {}


def setting(name: str, *envs: str) -> str:
    """env (first non-empty of *envs) -> pack settings -> DEFAULTS."""
    for env in envs:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    val = str(_pack_settings().get(name) or "").strip()
    return val or str(DEFAULTS.get(name, ""))


def api_base() -> str:
    return setting("api_base", "CLOUDFLARE_API_BASE").rstrip("/")


def compatibility_date() -> str:
    return setting("compatibility_date", "CLOUDFLARE_COMPATIBILITY_DATE")


# ── secret resolution ───────────────────────────────────────────────────


def _provider_key(name: str) -> str:
    """adk-local provider key store (~/.aither/provider_keys.json, 0600)."""
    try:
        p = Path.home() / ".aither" / "provider_keys.json"
        if not p.exists():
            return ""
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    # accept either a flat key or a nested {"cloudflare": {...}} block
    block = data.get("cloudflare")
    if isinstance(block, dict):
        for k in (name, name.lower(), name.replace("CLOUDFLARE_", "").lower()):
            v = str(block.get(k) or "").strip()
            if v:
                return v
    elif isinstance(block, str) and name in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        return block.strip()
    return str(data.get(name) or "").strip()


def _internal_secret() -> str:
    """AitherSecrets API key: env, else the fleet .env next to the repo root."""
    val = os.environ.get("AITHER_INTERNAL_SECRET", "").strip()
    if val:
        return val
    here = Path(__file__).resolve()
    # .../<root>/AitherOS/lib/agents/packs/cloudflare/config.py
    candidates = [p / ".env" for p in list(here.parents)[3:7]]
    for env_file in candidates:
        try:
            if not env_file.is_file():
                continue
            for line in env_file.read_text(errors="ignore").splitlines():
                if line.strip().startswith("AITHER_INTERNAL_SECRET="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _vault_secret(name: str) -> str:
    """Fetch a secret from the AitherSecrets vault over plain HTTP.

    Fleet-only convenience: fails soft to '' everywhere the vault is not
    configured/reachable (i.e. outside the fleet). No monorepo dependency —
    the pack ships standalone in the awdk wheel and never imports a
    private in-fleet helper; the token is expected via env or provider keys.
    """
    key = _internal_secret()
    if not key:
        return ""
    try:
        import httpx
        url = setting("vault_url", "AITHER_SECRETS_URL").rstrip("/")
        from adk._tls import tls_verify
        r = httpx.get(f"{url}/secrets/{name}", headers={"X-API-Key": key},
                      verify=tls_verify(), timeout=8)
        if r.status_code != 200:
            return ""
        body = r.json()
        return str(body.get("value") or "").strip() if isinstance(body, dict) else ""
    except Exception:  # noqa: BLE001 — vault optional outside the fleet
        return ""


def _resolve(setting_name: str, envs: tuple[str, ...], vault_name: str) -> str:
    cached = _SECRET_CACHE.get(vault_name)
    if cached is not None:
        return cached
    val = setting(setting_name, *envs)
    if not val:
        val = _provider_key(vault_name)
    if not val:
        val = _vault_secret(vault_name)
    _SECRET_CACHE[vault_name] = val
    return val


def api_token() -> str:
    """Cloudflare API token: env -> pack settings -> provider keys -> vault."""
    return _resolve("api_token", ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"),
                    "CLOUDFLARE_API_TOKEN")


def account_id() -> str:
    return _resolve("account_id", ("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID"),
                    "CLOUDFLARE_ACCOUNT_ID")


def zone_id() -> str:
    return _resolve("zone_id", ("CLOUDFLARE_ZONE_ID", "CF_ZONE_ID"),
                    "CLOUDFLARE_ZONE_ID")


def clear_cache() -> None:
    """Drop the in-process secret cache (after rotating a token)."""
    _SECRET_CACHE.clear()


# ── redaction ───────────────────────────────────────────────────────────


def mask_key(key: str) -> str:
    if not key:
        return ""
    return "***" if len(key) <= 8 else key[:4] + "..." + key[-4:]


def mask_id(ident: str) -> str:
    """Account/zone ids are semi-sensitive: show enough to disambiguate."""
    if not ident:
        return ""
    return "***" if len(ident) <= 8 else ident[:6] + "..." + ident[-4:]


def redact(text: object) -> str:
    """Never echo the API token: mask the live token and any Bearer/token= form
    in any outbound error or detail string (httpx exception text can carry the
    request headers/URL)."""
    s = str(text)
    tok = _SECRET_CACHE.get("CLOUDFLARE_API_TOKEN") or ""
    if tok:
        s = s.replace(tok, "***")
    secret = os.environ.get("AITHER_INTERNAL_SECRET", "")
    if secret:
        s = s.replace(secret, "***")
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{12,}", r"\1***", s)
    s = re.sub(r"(?i)((?:api_)?token|x-api-key|x-auth-key)[=:\s\"']+[A-Za-z0-9_\-\.]{12,}",
               r"\1=***", s)
    # Cloudflare error bodies name the acting credential by id
    # ("Actor 'com.cloudflare.api.token.<hex>' does not have permission ..."),
    # which fingerprints the token even though it is not the token value.
    s = re.sub(r"(com\.cloudflare\.api\.token\.)[0-9a-f]{8,}", r"\1***", s)
    return s
