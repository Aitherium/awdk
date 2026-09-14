"""
Fleet client — one fail-soft read surface for the whole command center.
========================================================================

Every HQ pane, the inbox, the palette and the watchtower read the fleet
through this module. Design rules:

- **Fail soft, per source.** Every call returns a :class:`SourceState`; a dead
  service is a dimmed tile with its error, never an exception that kills the
  cockpit.
- **Short timeouts.** These are dashboard reads (3s default), not work calls.
- **TLS via the internal CA** (``adk._tls.tls_verify``); verification is never disabled.
- Genesis is HTTP on :8001 (ground truth); everything else is HTTPS-first.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from adk._tls import tls_verify

DEFAULT_TIMEOUT = 3.0

# The core fleet map: name -> (base_url, health_path). Kept deliberately
# small and local-first; AITHER_* env vars override bases where they exist.
CORE_SERVICES: dict[str, tuple[str, str]] = {
    "genesis":        ("http://localhost:8001", "/health"),
    "microscheduler": ("https://localhost:8150", "/health"),
    "pulse":          ("https://localhost:8081", "/health"),
    "commcore":       ("https://localhost:8205", "/health"),
    "worker":         ("https://localhost:8159", "/health"),
    "secrets":        ("https://localhost:8111", "/health"),
    "graph":          ("https://localhost:8154", "/health"),
}


@dataclass
class SourceState:
    """One fleet read: either data, or an error string — never both unset."""
    ok: bool
    data: Any = None
    error: str = ""
    latency_ms: int = 0

    @classmethod
    def fail(cls, error: str) -> "SourceState":
        return cls(ok=False, error=error)


@dataclass
class FleetSnapshot:
    """Everything the HQ home screen shows, gathered concurrently."""
    services: dict = field(default_factory=dict)      # name -> SourceState
    llm_queue: SourceState = None
    sessions_live: int = 0
    sessions_total: int = 0
    crash_pending: bool = False
    taken_at: float = 0.0


class FleetClient:
    """Async, short-timeout, per-source-degrading fleet reader."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, verify=tls_verify(), follow_redirects=True
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def get_json(self, url: str, **kwargs) -> SourceState:
        """GET a JSON endpoint into a SourceState (never raises)."""
        start = time.monotonic()
        try:
            client = await self._http()
            resp = await client.get(url, **kwargs)
            latency = int((time.monotonic() - start) * 1000)
            if resp.status_code >= 400:
                return SourceState(False, error=f"HTTP {resp.status_code}",
                                   latency_ms=latency)
            return SourceState(True, data=resp.json(), latency_ms=latency)
        except httpx.ConnectError:
            return SourceState.fail("unreachable")
        except httpx.TimeoutException:
            return SourceState.fail(f"timeout >{self.timeout:g}s")
        except Exception as exc:  # ssl errors, bad json, ...
            return SourceState.fail(type(exc).__name__)

    async def post_json(self, url: str, json: Any = None, timeout: float = None,
                        **kwargs) -> SourceState:
        start = time.monotonic()
        try:
            client = await self._http()
            resp = await client.post(url, json=json,
                                     timeout=timeout or self.timeout, **kwargs)
            latency = int((time.monotonic() - start) * 1000)
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))[:120]
                except Exception:
                    pass
                return SourceState(False, error=f"HTTP {resp.status_code} {detail}".strip(),
                                   latency_ms=latency)
            return SourceState(True, data=resp.json(), latency_ms=latency)
        except httpx.ConnectError:
            return SourceState.fail("unreachable")
        except httpx.TimeoutException:
            return SourceState.fail("timeout")
        except Exception as exc:
            return SourceState.fail(type(exc).__name__)

    # ── fleet health ─────────────────────────────────────────────────────

    async def service_health(self) -> dict[str, SourceState]:
        async def probe(name, base, path):
            return name, await self.get_json(base + path)
        results = await asyncio.gather(
            *(probe(n, b, p) for n, (b, p) in CORE_SERVICES.items())
        )
        return dict(results)

    async def llm_queue(self) -> SourceState:
        """Rich queue status (queued/processing/VRAM/models); falls back to
        the older /queue/stats shape if /llm/queue is absent."""
        base, _ = CORE_SERVICES["microscheduler"]
        state = await self.get_json(base + "/llm/queue")
        if state.ok:
            return state
        return await self.get_json(base + "/queue/stats")

    async def llm_backends(self) -> SourceState:
        base, _ = CORE_SERVICES["microscheduler"]
        return await self.get_json(base + "/llm/backends/snapshot")

    async def genesis_services(self) -> SourceState:
        """Genesis's live per-service registry (status/health_score/pain)."""
        base, _ = CORE_SERVICES["genesis"]
        return await self.get_json(base + "/services")

    # ── pulse alerts / pain ──────────────────────────────────────────────

    async def alerts(self, min_severity: float = 0.0, limit: int = 50) -> SourceState:
        base, _ = CORE_SERVICES["pulse"]
        return await self.get_json(base + "/alerts",
                                   params={"min_severity": min_severity})

    async def pain(self) -> SourceState:
        base, _ = CORE_SERVICES["pulse"]
        return await self.get_json(base + "/pain")

    # ── inbox: mail (CommCore :8205) ─────────────────────────────────────
    # /mail/* is internal-only (no cf-ray header = internal caller), so
    # localhost reads need no bearer. Relay /v1/* wants the shell's token.

    def _comm_base(self) -> str:
        base, _ = CORE_SERVICES["commcore"]
        return base

    @staticmethod
    def _bearer() -> dict:
        try:
            from adk.shell.auth import AuthStore
            token = AuthStore.get_active_token()
            return {"Authorization": f"Bearer {token}"} if token else {}
        except Exception:
            return {}

    @staticmethod
    def default_nick() -> str:
        """Relay nick: env override, else the logged-in username."""
        import os
        nick = os.getenv("AITHER_RELAY_NICK", "")
        if nick:
            return nick
        try:
            from adk.shell.auth import AuthStore
            user = AuthStore.get_active_user() or {}
            return user.get("username") or ""
        except Exception:
            return ""

    async def mail_inbox(self, unread_only: bool = True, limit: int = 25) -> SourceState:
        return await self.get_json(
            self._comm_base() + "/mail/inbox",
            params={"unread_only": unread_only, "limit": limit},
        )

    async def mail_mark_read(self, message_id: str) -> SourceState:
        return await self.post_json(self._comm_base() + f"/mail/read/{message_id}")

    # ── inbox: relay mentions/DMs ────────────────────────────────────────

    async def relay_unread(self, nick: str) -> SourceState:
        return await self.get_json(
            self._comm_base() + "/v1/unread", params={"nick": nick},
            headers=self._bearer(),
        )

    async def relay_notifications(self, nick: str, unread_only: bool = True,
                                  limit: int = 25) -> SourceState:
        return await self.get_json(
            self._comm_base() + f"/v1/notifications/{nick}",
            params={"unread_only": unread_only, "limit": limit},
            headers=self._bearer(),
        )

    async def relay_notifications_read(self, nick: str, ids: list) -> SourceState:
        return await self.post_json(
            self._comm_base() + f"/v1/notifications/{nick}/read",
            json={"notification_ids": ids}, headers=self._bearer(),
        )

    async def relay_dm_partners(self, nick: str) -> SourceState:
        return await self.get_json(
            self._comm_base() + "/v1/dms/partners", params={"nick": nick},
            headers=self._bearer(),
        )

    async def relay_send_dm(self, to_nick: str, content: str) -> SourceState:
        return await self.post_json(
            self._comm_base() + "/v1/dms",
            json={"to_nick": to_nick, "content": content}, headers=self._bearer(),
        )

    # ── agents / forge / routines ────────────────────────────────────────

    ATLAS_BASE = "https://localhost:8778"
    FORGE_BASE = "https://localhost:8222"

    async def agents_roster(self) -> SourceState:
        """Atlas's live roster; falls back to Genesis /agents."""
        state = await self.get_json(self.ATLAS_BASE + "/agents/roster")
        if state.ok and (state.data or {}).get("roster"):
            return state
        base, _ = CORE_SERVICES["genesis"]
        return await self.get_json(base + "/agents")

    async def ask_agent(self, agent: str, message: str, effort: int = 5,
                        timeout: float = 600.0) -> SourceState:
        """Blocking ask via Genesis /agent/sync. Generous timeout — reasoning
        turns are slow, and disconnecting the client CANCELS the run."""
        base, _ = CORE_SERVICES["genesis"]
        return await self.post_json(
            base + "/agent/sync",
            json={"message": message, "persona": agent, "effort_level": effort,
                  "max_effort": effort},
            timeout=timeout,
            headers=self._bearer(),
        )

    async def forge_dispatches(self) -> SourceState:
        return await self.get_json(self.FORGE_BASE + "/forge/dispatches")

    async def forge_dispatch(self, dispatch_id: str) -> SourceState:
        return await self.get_json(self.FORGE_BASE + f"/forge/dispatches/{dispatch_id}")

    async def routines_stats(self) -> SourceState:
        base, _ = CORE_SERVICES["worker"]
        return await self.get_json(base + "/routines/stats")

    # ── one-shot HQ snapshot ─────────────────────────────────────────────

    async def snapshot(self) -> FleetSnapshot:
        from adk.shell import claude_sessions as cs
        services_t = asyncio.create_task(self.service_health())
        queue_t = asyncio.create_task(self.llm_queue())
        snap = FleetSnapshot(taken_at=time.time())
        # Local session state is cheap and synchronous.
        try:
            sessions = cs.scan_sessions(scan=80, top=50)
            snap.sessions_live = sum(1 for s in sessions if s.live)
            snap.sessions_total = len(sessions)
            snap.crash_pending = cs.pending_crash() is not None
        except Exception:
            pass
        snap.services = await services_t
        snap.llm_queue = await queue_t
        return snap
