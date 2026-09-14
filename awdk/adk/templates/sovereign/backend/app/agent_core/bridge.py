"""SpiritBridge — optional uplink to AitherSpirit / MemoryHub / WorkingMemory.

When the host AitherOS services are reachable, high-confidence local
memories can be replicated up the stack. When they are unreachable, the
circuit breaker trips and the bridge silently degrades — local-only
mode continues working.

This client makes ZERO mandatory assumptions about the remote schema —
it posts a generic envelope that AitherSpirit / MemoryHub already
accept (`/api/v1/memory/store`), and ignores their response shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from .tiers import MemoryTier

log = logging.getLogger("agent_core.bridge")


def tls_verify() -> bool:
    """Verify TLS by default; ``AITHER_TLS_VERIFY=false`` disables (dev only).

    Self-contained (no adk dependency) so this scaffolded template runs
    standalone in a generated project.
    """
    return os.environ.get("AITHER_TLS_VERIFY", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )

_DEFAULT_TIMEOUT = 5.0


class _CircuitBreaker:
    """Tiny circuit breaker — half-open after `reset_after` seconds."""

    def __init__(self, threshold: int = 5, reset_after: float = 60.0):
        self.threshold = threshold
        self.reset_after = reset_after
        self.failures = 0
        self.opened_at: float | None = None

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if (time.time() - self.opened_at) > self.reset_after:
            # Half-open: allow one trial
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()


class SpiritBridge:
    """HTTP bridge to AitherSpirit / MemoryHub / AitherWorkingMemory."""

    def __init__(
        self,
        spirit_url: str | None = None,
        memory_hub_url: str | None = None,
        working_memory_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.spirit_url = (spirit_url or os.environ.get("SPIRIT_URL") or "").rstrip("/")
        self.memory_hub_url = (
            memory_hub_url
            or os.environ.get("MEMORYHUB_URL")
            or os.environ.get("MEMORY_URL")
            or ""
        ).rstrip("/")
        self.working_memory_url = (
            working_memory_url
            or os.environ.get("WORKING_MEMORY_URL")
            or ""
        ).rstrip("/")
        self.timeout = timeout
        self._breaker = _CircuitBreaker()
        if httpx is None:
            log.debug("httpx not installed — SpiritBridge is no-op")

    @property
    def enabled(self) -> bool:
        return httpx is not None and bool(
            self.spirit_url or self.memory_hub_url or self.working_memory_url
        )

    async def push(
        self,
        tenant_id: str,
        content: str,
        *,
        tier: MemoryTier = MemoryTier.WORKING,
        confidence: float = 0.5,
        sources: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Best-effort push. Returns True on success, False on degraded skip."""
        if not self.enabled or self._breaker.is_open():
            return False
        # Target order: Spirit (preferred) -> MemoryHub -> WorkingMemory.
        targets: list[str] = []
        if self.spirit_url:
            targets.append(f"{self.spirit_url}/api/v1/memory/store")
        if self.memory_hub_url:
            targets.append(f"{self.memory_hub_url}/api/v1/memory/store")
        if tier == MemoryTier.WORKING and self.working_memory_url:
            targets.append(f"{self.working_memory_url}/api/v1/memory/store")
        payload = {
            "tenant_id": tenant_id,
            "content": content,
            "tier": tier.value,
            "confidence": confidence,
            "sources": sources or [],
            "metadata": metadata or {},
            "source": "agent_core",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=tls_verify()) as client:
                for url in targets:
                    try:
                        r = await client.post(url, json=payload)
                        if r.status_code < 500:
                            self._breaker.record_success()
                            return True
                    except Exception:
                        continue
            self._breaker.record_failure()
            return False
        except Exception as e:
            log.debug("SpiritBridge.push failed: %s", e)
            self._breaker.record_failure()
            return False

    async def pull(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Best-effort remote recall. Returns [] on degraded skip."""
        if not self.enabled or self._breaker.is_open():
            return []
        targets: list[str] = []
        if self.spirit_url:
            targets.append(f"{self.spirit_url}/api/v1/memory/query")
        if self.memory_hub_url:
            targets.append(f"{self.memory_hub_url}/api/v1/memory/query")
        payload = {"tenant_id": tenant_id, "query": query, "limit": limit}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=tls_verify()) as client:
                for url in targets:
                    try:
                        r = await client.post(url, json=payload)
                        if r.status_code == 200:
                            data = r.json()
                            self._breaker.record_success()
                            return data.get("memories") or data.get("results") or []
                    except Exception:
                        continue
            self._breaker.record_failure()
            return []
        except Exception as e:
            log.debug("SpiritBridge.pull failed: %s", e)
            self._breaker.record_failure()
            return []

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "breaker_open": self._breaker.is_open(),
            "failures": self._breaker.failures,
            "spirit_url": self.spirit_url or None,
            "memory_hub_url": self.memory_hub_url or None,
            "working_memory_url": self.working_memory_url or None,
        }
