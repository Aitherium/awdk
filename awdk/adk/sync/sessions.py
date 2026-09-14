"""
Session Sync Client — Synchronize local shell sessions to cloud portal.

This module enables push-on-save session sync for AitherShell conversations.
Sessions are persisted locally in ~/.aither/conversations/*.json and can be
pushed to a remote portal/gateway endpoint via SessionSyncClient.

Architecture:
  1. Local: ConversationStore in ~/.aither/conversations/*.json (always active)
  2. Cloud: Genesis /v1/sessions/* endpoints (POST sync, GET list, DELETE)
  3. Client: SessionSyncClient (this module) + session_sync_integration.py
  4. Hook: ConversationStore.set_sync_hook() fires async push after each save
  5. Auth: Bearer token from ~/.aither/auth.json or AITHER_SYNC_TOKEN env

Supported modes:
  - Direct push: POST to {gateway}/v1/sessions/sync after a session is updated
  - Batch sync: Periodic or manual sync of all local sessions
  - Debounced: Updates are debounced (configurable, default 5s) to avoid hammering

Environment:
  - AITHER_SESSION_SYNC=true/false — enable/disable sync (default: false, must opt-in)
  - AITHER_GATEWAY_URL=http://localhost:8001 — sync endpoint (default: local Genesis)
  - AITHER_SYNC_TOKEN=Bearer ... — override token (default: from ~/.aither/auth.json)
  - AITHER_SYNC_DEBOUNCE=5.0 — debounce window in seconds
  - AITHER_SYNC_TIMEOUT=30.0 — HTTP timeout in seconds

Configuration:
  Sessions are synced ONLY if AITHER_SESSION_SYNC=true (fail soft, must opt-in).
  Sync failures are logged but do NOT block local operation.
  Bearer token resolution: env → auth.json profiles.default.access_token → legacy access_token

Integration points (in awdk):
  - adk/conversations.py: ConversationStore._save() calls registered async hook
  - adk/session_sync_integration.py: enable_session_sync() wires client into store
  - adk/auth.py: Credentials + AuthStore patterns (compatible)

Integration points (in AitherOS):
  - AitherGenesis/routers/sessions_sync.py: /v1/sessions/sync, GET, DELETE endpoints
  - AitherGenesis/genesis_service.py: router mounted with _include() (line ~2640)
  - lib/clients/strata.py: StrataClient for encrypted storage (aither://warm/sessions/...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import httpx

log = logging.getLogger("adk.session_sync")


@dataclass
class SessionSyncConfig:
    """Configuration for session syncing."""
    enabled: bool = False  # Master switch: AITHER_SESSION_SYNC
    gateway_url: str = "http://localhost:8001"  # AITHER_GATEWAY_URL
    debounce_seconds: float = 5.0  # Debounce window for multiple saves
    timeout_seconds: float = 30.0  # HTTP timeout
    fail_soft: bool = True  # Don't raise on sync failures; only log


class SessionSyncClient:
    """Client for pushing local sessions to a remote portal/gateway.

    Typical usage:
        client = SessionSyncClient(
            gateway_url="https://portal.aitherium.com",
            auth_token="Bearer ...",  # from ~/.aither/auth.json
        )
        # Push after a session is saved locally
        result = await client.sync_session(session_id, agent_name, messages, ...)
        # Or batch-sync all local sessions
        await client.sync_all_from_disk(conversations_dir)
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8001",
        auth_token: Optional[str] = None,
        config: Optional[SessionSyncConfig] = None,
    ):
        """
        Initialize the session sync client.

        Args:
            gateway_url: Base URL of the portal/gateway (e.g., http://localhost:8001)
            auth_token: Bearer token for authentication (e.g., "Bearer aither_sk_live_...")
            config: Optional SessionSyncConfig; if None, loads from environment
        """
        self.gateway_url = gateway_url.rstrip("/")
        self.auth_token = auth_token or os.environ.get("AITHER_SYNC_TOKEN", "")
        self.config = config or self._load_config()
        self._pending_syncs: Dict[str, float] = {}  # session_id -> debounce_time

    @staticmethod
    def _load_config() -> SessionSyncConfig:
        """Load config from environment variables."""
        enabled = os.environ.get("AITHER_SESSION_SYNC", "false").lower() in ("true", "1")
        gateway_url = os.environ.get(
            "AITHER_GATEWAY_URL",
            "http://localhost:8001"
        )
        debounce = float(os.environ.get("AITHER_SYNC_DEBOUNCE", "5.0"))
        timeout = float(os.environ.get("AITHER_SYNC_TIMEOUT", "30.0"))

        return SessionSyncConfig(
            enabled=enabled,
            gateway_url=gateway_url,
            debounce_seconds=debounce,
            timeout_seconds=timeout,
        )

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with authentication."""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            if not self.auth_token.startswith("Bearer "):
                headers["Authorization"] = f"Bearer {self.auth_token}"
            else:
                headers["Authorization"] = self.auth_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated request to the gateway.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Endpoint path (e.g., "/v1/sessions/sync")
            data: Request body as dict (will be JSON-encoded)

        Returns:
            Response JSON or error dict with "error" key set
        """
        url = f"{self.gateway_url}{path}"
        headers = self._build_headers()
        body = json.dumps(data).encode("utf-8") if data else None

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                verify=True,
            ) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, content=body, headers=headers)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=headers)
                else:
                    resp = await client.request(method, url, content=body, headers=headers)

                if resp.status_code >= 400:
                    error_msg = resp.text[:500]
                    log.warning(
                        "Gateway %s %s returned %d: %s",
                        method, path, resp.status_code, error_msg
                    )
                    return {
                        "error": True,
                        "status": resp.status_code,
                        "detail": error_msg,
                    }

                try:
                    return resp.json()
                except Exception:
                    return {"ok": True, "raw": resp.text[:200]}

        except Exception as e:
            error_msg = str(e)
            log.warning("Request to %s %s failed: %s", method, path, error_msg)
            return {"error": True, "detail": error_msg}

    async def sync_session(
        self,
        session_id: str,
        agent_name: str,
        messages: List[Dict[str, Any]],
        created_at: float,
        updated_at: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Sync a single session to the gateway.

        Args:
            session_id: Unique session identifier (UUID)
            agent_name: Name of the agent this session is with
            messages: List of message dicts (role, content, timestamp)
            created_at: Unix timestamp of session creation
            updated_at: Unix timestamp of last update
            metadata: Optional metadata dict (e.g., context info)

        Returns:
            Response dict from /v1/sessions/sync ({"synced": 1, "failed": 0, ...})
            On error, returns {"error": True, ...}
        """
        if not self.config.enabled:
            log.debug("Session sync disabled — skipping push for %s", session_id)
            return {"skipped": True, "reason": "disabled"}

        session_data = {
            "session_id": session_id,
            "agent_name": agent_name,
            "messages": messages,
            "created_at": created_at,
            "updated_at": updated_at,
            "metadata": metadata or {},
        }

        request_body = {"sessions": [session_data]}

        try:
            result = await self._request("POST", "/v1/sessions/sync", request_body)
            if result.get("error"):
                log.warning(
                    "Failed to sync session %s: %s",
                    session_id, result.get("detail", "unknown error")
                )
                if not self.config.fail_soft:
                    raise RuntimeError(f"Session sync failed: {result.get('detail')}")
            else:
                log.debug("Synced session %s (synced=%d)", session_id, result.get("synced", 0))
            return result
        except Exception as e:
            log.warning("Session sync raised exception: %s", e)
            if not self.config.fail_soft:
                raise
            return {"error": True, "detail": str(e)}

    async def sync_batch(
        self,
        sessions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Sync multiple sessions in a single batch.

        Args:
            sessions: List of session dicts (each with session_id, agent_name, messages, etc.)

        Returns:
            Response dict from /v1/sessions/sync
        """
        if not self.config.enabled:
            log.debug("Session sync disabled — skipping batch push")
            return {"skipped": True, "reason": "disabled"}

        request_body = {"sessions": sessions}

        try:
            result = await self._request("POST", "/v1/sessions/sync", request_body)
            if result.get("error"):
                log.warning(
                    "Failed to sync batch (%d sessions): %s",
                    len(sessions), result.get("detail", "unknown error")
                )
                if not self.config.fail_soft:
                    raise RuntimeError(f"Batch sync failed: {result.get('detail')}")
            else:
                log.debug(
                    "Synced batch (synced=%d, failed=%d)",
                    result.get("synced", 0), result.get("failed", 0)
                )
            return result
        except Exception as e:
            log.warning("Batch sync raised exception: %s", e)
            if not self.config.fail_soft:
                raise
            return {"error": True, "detail": str(e)}

    async def list_sessions(self) -> Dict[str, Any]:
        """
        List synced sessions in the cloud.

        Returns:
            Response dict with "sessions" list (metadata only)
        """
        try:
            result = await self._request("GET", "/v1/sessions")
            if result.get("error"):
                log.warning("Failed to list sessions: %s", result.get("detail"))
            return result
        except Exception as e:
            log.warning("List sessions failed: %s", e)
            return {"error": True, "detail": str(e)}

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """
        Fetch a synced session from the cloud (full history).

        Args:
            session_id: Session UUID

        Returns:
            Full session dict with messages array
        """
        try:
            result = await self._request("GET", f"/v1/sessions/{session_id}")
            if result.get("error"):
                log.warning("Failed to get session %s: %s", session_id, result.get("detail"))
            return result
        except Exception as e:
            log.warning("Get session failed: %s", e)
            return {"error": True, "detail": str(e)}

    async def delete_session(self, session_id: str) -> Dict[str, Any]:
        """
        Delete a synced session from the cloud.

        Args:
            session_id: Session UUID

        Returns:
            Response dict (204 No Content returns {"ok": True})
        """
        try:
            result = await self._request("DELETE", f"/v1/sessions/{session_id}")
            if result.get("error"):
                log.warning("Failed to delete session %s: %s", session_id, result.get("detail"))
            return result
        except Exception as e:
            log.warning("Delete session failed: %s", e)
            return {"error": True, "detail": str(e)}

    async def sync_all_from_disk(
        self,
        conversations_dir: Path | str,
    ) -> Dict[str, Any]:
        """
        Batch-sync all local conversations from disk.

        Args:
            conversations_dir: Path to ~/.aither/conversations/ (or custom dir)

        Returns:
            Summary: {"synced": N, "failed": M, "skipped": K, ...}
        """
        conversations_dir = Path(conversations_dir)
        if not conversations_dir.exists():
            log.warning("Conversations directory not found: %s", conversations_dir)
            return {"error": True, "detail": f"Directory not found: {conversations_dir}"}

        sessions_to_sync = []
        failed_to_load = 0

        for session_file in conversations_dir.glob("*.json"):
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                sessions_to_sync.append(data)
            except Exception as e:
                log.warning("Failed to load session from %s: %s", session_file, e)
                failed_to_load += 1

        if not sessions_to_sync:
            log.info("No sessions to sync from %s", conversations_dir)
            return {"synced": 0, "failed": 0, "skipped": failed_to_load, "detail": "No valid sessions"}

        result = await self.sync_batch(sessions_to_sync)
        if failed_to_load > 0:
            result.setdefault("errors", []).append({
                "phase": "load",
                "count": failed_to_load
            })

        return result

    async def pull_sessions(
        self,
        watermark: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Pull remote sessions newer than watermark.

        Fetches GET /v1/sessions?since={watermark} and returns all sessions
        with updated_at >= watermark, sorted by updated_at ascending (oldest first).

        Args:
            watermark: Unix timestamp. Fetch sessions updated after this time.

        Returns:
            List of session dicts: [
                {
                    "session_id": "...",
                    "agent_name": "...",
                    "messages": [...],
                    "created_at": 1234567890.0,
                    "updated_at": 1234567890.5,
                    "metadata": {...}
                },
                ...
            ]
            Empty list on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Session sync disabled — skipping pull")
            return []

        try:
            path = f"/v1/sessions?since={watermark}"
            result = await self._request("GET", path)

            if result.get("error"):
                status = result.get("status", 500)
                # Gracefully handle 404, 403, 507 (fail-soft)
                if status in (404, 403, 507):
                    log.warning(
                        "Pull sessions returned %d: %s",
                        status, result.get("detail", "unknown")
                    )
                    return []
                else:
                    log.warning(
                        "Pull sessions error (HTTP %d): %s",
                        status, result.get("detail", "unknown")
                    )
                    return []

            sessions = result.get("sessions", [])
            log.debug("Pulled %d sessions (watermark=%.1f)", len(sessions), watermark)
            return sessions

        except Exception as e:
            log.warning("Pull sessions failed: %s", e)
            return []

    async def merge_remote_sessions(
        self,
        store,  # ConversationStore (avoid circular import)
        remote_sessions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Merge remote sessions into the local store using last-writer-wins.

        For each remote session:
          - If no local session exists, create it (always merge)
          - If local exists but remote is significantly newer (>60s), replace local
          - Otherwise, keep local (local is newer or recent)

        After all merges succeed, updates the pull watermark to max(remote.updated_at).
        On any error during merge, watermark is NOT updated (partial failure safe).

        Args:
            store: ConversationStore instance
            remote_sessions: List of session dicts from pull_sessions()

        Returns:
            Dict with keys:
              - "merged": count of sessions merged
              - "skipped": count of sessions kept (local was newer)
              - "errors": list of error dicts per session
        """
        merged = 0
        skipped = 0
        errors = []

        # Accumulate results before updating watermark (atomic on watermark)
        max_remote_updated = 0.0

        for remote in remote_sessions:
            try:
                session_id = remote.get("session_id")
                if not session_id:
                    errors.append({
                        "session_id": "(missing)",
                        "reason": "Missing session_id"
                    })
                    continue

                remote_updated_at = remote.get("updated_at", 0.0)
                max_remote_updated = max(max_remote_updated, remote_updated_at)

                # Check if local session exists (without creating)
                local = store._load(session_id)

                # Last-writer-wins logic
                if local is None:
                    # No local session — always merge (create it)
                    await store.merge_remote_session(session_id, remote)
                    merged += 1
                    log.debug(
                        "Created local session %s from remote",
                        session_id
                    )
                elif (
                    remote_updated_at > local.updated_at
                    and (remote_updated_at - local.updated_at) > 60.0
                ):
                    # Remote is significantly newer (>60s) — merge it
                    await store.merge_remote_session(session_id, remote)
                    merged += 1
                    log.debug(
                        "Merged remote session %s (remote_updated=%.1f, local_updated=%.1f)",
                        session_id, remote_updated_at, local.updated_at
                    )
                else:
                    # Local is newer or recent — keep it
                    skipped += 1
                    log.debug(
                        "Kept local session %s (remote_updated=%.1f, local_updated=%.1f)",
                        session_id, remote_updated_at, local.updated_at
                    )

            except Exception as e:
                errors.append({
                    "session_id": remote.get("session_id", "(unknown)"),
                    "reason": str(e)
                })

        # Only update watermark if there were no errors (atomic on error)
        if not errors and merged > 0:
            await store.set_pull_watermark(max_remote_updated)
            log.debug("Updated pull watermark to %.1f", max_remote_updated)

        return {
            "merged": merged,
            "skipped": skipped,
            "errors": errors,
        }

    def debounce_sync(
        self,
        session_id: str,
    ) -> float:
        """
        Check if a session should be synced based on debounce window.

        Useful for integrating with on-save hooks: call this before pushing,
        and only sync if it returns a future time (now + debounce_seconds).

        Args:
            session_id: Session ID

        Returns:
            Unix timestamp when the session is eligible for sync.
            If <= now, it's safe to sync immediately.
        """
        now = time.time()
        last_sync_time = self._pending_syncs.get(session_id, 0.0)
        next_eligible = last_sync_time + self.config.debounce_seconds

        if now >= next_eligible:
            self._pending_syncs[session_id] = now
            return now

        return next_eligible


async def create_sync_client_from_auth(
    auth_file: Optional[Path] = None,
    gateway_url: Optional[str] = None,
) -> Optional[SessionSyncClient]:
    """
    Create a SessionSyncClient from stored auth.json credentials.

    Args:
        auth_file: Path to ~/.aither/auth.json (default: ~/.aither/auth.json)
        gateway_url: Override gateway URL (default: from auth.json or env)

    Returns:
        SessionSyncClient or None if auth not found / sync disabled
    """
    if auth_file is None:
        auth_file = Path.home() / ".aither" / "auth.json"

    if not auth_file.exists():
        log.debug("No auth file at %s", auth_file)
        return None

    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read auth file: %s", e)
        return None

    # Extract access token from the active profile
    access_token = auth_data.get("profiles", {}).get("default", {}).get("access_token")
    if not access_token:
        # Fallback: check for flat 'access_token' key (legacy format)
        access_token = auth_data.get("access_token")

    if not access_token:
        log.debug("No access_token in auth.json")
        return None

    url = gateway_url or os.environ.get("AITHER_GATEWAY_URL", "http://localhost:8001")

    return SessionSyncClient(gateway_url=url, auth_token=access_token)
