"""Settings Sync — mirror the local agent's settings to the user's portal profile.

Sibling of :mod:`adk.session_sync`. Where session-sync pushes conversation
history, settings-sync keeps the agent's *configuration* (LLM backend choice,
enabled packs, safe config prefs, external MCP servers) in step with the user's
profile on api.aitherium.com — so the same settings follow the user across
devices and into the Awconnect browser extension.

Decision (locked with the owner): **the portal profile is the source of truth.**

  * On startup the agent PULLS ``preferences.adk`` and applies it over the local
    ``~/.aither/config.yaml`` cache (:func:`SettingsSyncClient.pull_and_apply`).
  * Every admin-console mutation PUSHES a fresh snapshot up
    (:func:`SettingsSyncClient.push_snapshot`), debounced and fail-soft.
  * Offline → keep the local cache; the next successful pull reconciles.

Storage mapping (verified in awkit-backend/routers/settings_api.py):
  * per-user settings  → ``GET/PUT /api/settings/preferences`` (merge semantics,
    ``require_write`` RBAC). We namespace under ``preferences.adk`` so we never
    stomp other apps' prefs. NOTE: ``/api/settings/llm`` is deployment-global +
    mutates ``os.environ`` — it is the WRONG target for per-user sync and is
    deliberately not used here.

Secrets policy: provider API keys and MCP auth headers are device-local secrets
and are NEVER included in the pushed snapshot.

Environment:
  * ``AITHER_SETTINGS_SYNC``   — ``true`` / ``false`` / ``auto`` (default ``auto``:
    on when a portal token is resolvable, off otherwise).
  * ``AITHER_PORTAL_URL``      — portal base (default ``https://api.aitherium.com``).
  * ``AITHER_SETTINGS_SYNC_DEBOUNCE`` — push debounce seconds (default ``3.0``).
  * ``AITHER_SETTINGS_SYNC_TIMEOUT``  — HTTP timeout seconds (default ``15.0``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

from adk.admin_api import (
    _CONFIG_ALLOWLIST,
    _load_mcp_servers,
    _load_provider_keys,
    _save_mcp_servers,
    _url_is_safe,
)
from adk.config import load_saved_config, save_saved_config

log = logging.getLogger("adk.settings_sync")

_PREFS_PATH = "/api/settings/preferences"


def _default_portal_url() -> str:
    return (
        os.environ.get("AITHER_PORTAL_URL")
        or os.environ.get("AITHER_ELYSIUM_URL")
        or "https://api.aitherium.com"
    ).rstrip("/")


def _resolve_token() -> str:
    """Resolve the portal bearer (env → auth.json active profile)."""
    for env in ("AITHER_PORTAL_TOKEN", "AITHERIUM_API_KEY", "AITHER_API_KEY", "AITHER_SYNC_TOKEN"):
        val = os.environ.get(env, "").strip()
        if val:
            return val
    try:
        from adk.auth import resolve_credentials
        creds = resolve_credentials()
        tok = (creds.access_token or "").strip()
        # A local-root placeholder token isn't a real portal credential.
        if tok and tok != "aither_root_local":
            return tok
    except Exception:  # noqa: BLE001 — auth resolution is best-effort
        pass
    return ""


def build_snapshot() -> dict:
    """Gather the syncable settings — NO secrets (keys / MCP headers excluded)."""
    saved = load_saved_config()
    servers = _load_mcp_servers()
    llm = {}
    if saved.get("llm_provider"):
        llm["provider"] = saved["llm_provider"]
    if saved.get("model"):
        llm["model"] = saved["model"]
    return {
        "llm": llm,
        "packs": list(saved.get("required_packs") or []),
        "config": {k: saved[k] for k in _CONFIG_ALLOWLIST if k in saved},
        # url/name/id only — auth headers are device-local secrets, never synced.
        "mcp_servers": [
            {"id": s.get("id"), "url": s.get("url"), "name": s.get("name")}
            for s in servers
        ],
        "updated_at": time.time(),
    }


class SettingsSyncClient:
    """Push/pull the agent's settings to/from the portal profile."""

    def __init__(
        self,
        portal_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        *,
        debounce_seconds: Optional[float] = None,
        timeout_seconds: Optional[float] = None,
    ):
        self.portal_url = (portal_url or _default_portal_url()).rstrip("/")
        self.auth_token = auth_token if auth_token is not None else _resolve_token()
        self.debounce = (
            debounce_seconds
            if debounce_seconds is not None
            else float(os.environ.get("AITHER_SETTINGS_SYNC_DEBOUNCE", "3.0"))
        )
        self.timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("AITHER_SETTINGS_SYNC_TIMEOUT", "15.0"))
        )
        self._last_push = 0.0

    @staticmethod
    def is_enabled(token: str | None = None) -> bool:
        mode = os.environ.get("AITHER_SETTINGS_SYNC", "auto").strip().lower()
        if mode in ("false", "0", "off", "no"):
            return False
        tok = token if token is not None else _resolve_token()
        if mode in ("true", "1", "on", "yes"):
            return True
        # auto: on only when we actually have a portal credential.
        return bool(tok)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = (
                self.auth_token if self.auth_token.startswith("Bearer ")
                else f"Bearer {self.auth_token}"
            )
        return headers

    async def push_snapshot(self, *, force: bool = False) -> dict:
        """PUT the current settings snapshot to the portal profile (debounced)."""
        if not self.auth_token:
            return {"ok": False, "reason": "no-token"}
        now = time.time()
        if not force and (now - self._last_push) < self.debounce:
            return {"ok": True, "skipped": "debounced"}
        self._last_push = now
        body = {"preferences": {"adk": build_snapshot()}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.put(
                    f"{self.portal_url}{_PREFS_PATH}", json=body, headers=self._headers()
                )
            if resp.status_code >= 400:
                log.warning("settings push failed: %s %s", resp.status_code, resp.text[:200])
                return {"ok": False, "status": resp.status_code}
            return {"ok": True}
        except (httpx.HTTPError, OSError) as exc:
            log.warning("settings push error: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def fetch_profile(self) -> dict:
        if not self.auth_token:
            return {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    f"{self.portal_url}{_PREFS_PATH}", headers=self._headers()
                )
            if resp.status_code >= 400:
                log.info("settings pull got %s (starting from local cache)", resp.status_code)
                return {}
            return (resp.json() or {}).get("preferences", {}) or {}
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.info("settings pull error (using local cache): %s", exc)
            return {}

    async def pull_and_apply(self, agent: Any = None) -> dict:
        """Portal source-of-truth pull: apply ``preferences.adk`` over local cache."""
        prefs = await self.fetch_profile()
        adk = prefs.get("adk") if isinstance(prefs, dict) else None
        if not isinstance(adk, dict):
            return {"applied": False, "reason": "no adk prefs on profile"}

        applied: list[str] = []

        # LLM backend
        llm = adk.get("llm") or {}
        provider = (llm.get("provider") or "").strip()
        model = llm.get("model") or None
        if provider:
            save_saved_config({"llm_provider": provider, **({"model": model} if model else {})})
            if agent is not None:
                try:
                    key = _load_provider_keys().get(provider) or None
                    agent.llm.switch_backend(provider, api_key=key, model=model)
                except Exception as exc:  # noqa: BLE001
                    log.warning("apply llm backend '%s' failed: %s", provider, exc)
            applied.append("llm")

        # Config allowlist (scalar prefs only)
        cfg = adk.get("config") or {}
        safe_cfg = {k: v for k, v in cfg.items() if k in _CONFIG_ALLOWLIST}
        if safe_cfg:
            save_saved_config(safe_cfg)
            applied.append("config")

        # Enabled packs
        packs = adk.get("packs")
        if isinstance(packs, list):
            save_saved_config({"required_packs": packs})
            applied.append("packs")
            if agent is not None and hasattr(agent, "_load_discovered_packs"):
                try:
                    agent._load_discovered_packs()
                except Exception as exc:  # noqa: BLE001
                    log.warning("pack reload on pull failed: %s", exc)

        # External MCP servers — persist, and register SSRF-safe ones into the
        # live agent. Servers whose URL resolves to a private/loopback host are
        # persisted but NOT auto-registered (Athena SSRF guard still applies even
        # to the user's own profile entries).
        servers = adk.get("mcp_servers")
        if isinstance(servers, list) and servers:
            _save_mcp_servers(servers)
            applied.append("mcp_servers")
            if agent is not None:
                await self._register_mcp_servers(agent, servers)

        return {"applied": bool(applied), "sections": applied}

    @staticmethod
    async def _register_mcp_servers(agent: Any, servers: list[dict]) -> None:
        try:
            from adk.core.mcp import mcp_tools
        except ImportError:
            return
        for s in servers:
            url = (s.get("url") or "").strip()
            if not url:
                continue
            safe, reason = _url_is_safe(url)
            if not safe:
                log.warning("skip auto-registering MCP server %s: %s", url, reason)
                continue
            try:
                tools = await mcp_tools(url, headers=s.get("headers"))
                for t in tools:
                    agent._tools.register(
                        t.__call__ if hasattr(t, "__call__") else t,
                        name=getattr(t, "name", None),
                        description=getattr(t, "description", ""),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP register on pull failed for %s: %s", url, exc)


def build_client() -> Optional[SettingsSyncClient]:
    """Construct a client if settings-sync is enabled and a token is resolvable."""
    token = _resolve_token()
    if not SettingsSyncClient.is_enabled(token):
        return None
    return SettingsSyncClient(auth_token=token or None)
