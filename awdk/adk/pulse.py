"""Pulse client — alert integration with AitherOS Pulse:8081.

Sends pain signals (alerts) to Pulse when critical events occur:
  - LoopGuard circuit breaks
  - Quota hard limit breaches
  - Unhandled exceptions in agent execution
  - Sandbox capability violations
  - LLM backend failures

The dark factory picks up pain signals and triggers remediation.

Usage:
    from adk.pulse import get_pulse, PainCategory

    pulse = get_pulse()
    await pulse.send_pain(
        category=PainCategory.AGENT_LOOP,
        message="LoopGuard circuit break on tool web_search",
        agent="atlas",
        severity="warning",
    )
"""

from __future__ import annotations

import json
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.pulse")

_DEFAULT_PULSE_URL = "http://localhost:8081"
_QUEUE_MAX_LINES = 500


class PainCategory(str, Enum):
    """Categories of pain signals sent to Pulse."""
    AGENT_LOOP = "agent_loop"              # LoopGuard circuit break
    QUOTA_BREACH = "quota_breach"          # Hard limit hit
    AGENT_ERROR = "agent_error"            # Unhandled exception
    SANDBOX_VIOLATION = "sandbox_violation" # Capability denied
    LLM_FAILURE = "llm_failure"            # Backend unreachable/error
    SECURITY = "security"                  # Security event
    HEALTH_DEGRADED = "health_degraded"    # Service health drop


class PulseClient:
    """Fire-and-forget alert client for AitherOS Pulse."""

    def __init__(
        self,
        pulse_url: str = "",
        data_dir: str | Path | None = None,
        source: str = "adk",
    ):
        self.pulse_url = (
            pulse_url
            or os.getenv("AITHER_PULSE_URL", "")
        ).rstrip("/")
        self._data_dir = Path(
            data_dir or os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
        )
        self._queue_path = self._data_dir / "pulse_queue.jsonl"
        self._source = source
        self._enabled: bool | None = None

        # Dedup: don't spam the same pain signal
        self._recent_pains: dict[str, float] = {}
        self._dedup_window = 300  # 5 minutes

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        self._enabled = bool(self.pulse_url)
        return self._enabled

    async def send_pain(
        self,
        category: PainCategory | str,
        message: str,
        *,
        agent: str = "",
        request_id: str = "",
        severity: str = "warning",
        details: dict | None = None,
    ) -> bool:
        """Send a pain signal to Pulse. Returns True if sent.

        Deduplicates: same category+message within 5min window is suppressed.
        """
        if not self.enabled:
            return False

        cat = category.value if isinstance(category, PainCategory) else category

        # Dedup check
        dedup_key = f"{cat}:{message[:100]}"
        now = time.time()
        last_sent = self._recent_pains.get(dedup_key, 0)
        if now - last_sent < self._dedup_window:
            logger.debug("Pain signal suppressed (dedup): %s", dedup_key)
            return False
        self._recent_pains[dedup_key] = now

        # Prune old dedup entries
        self._recent_pains = {
            k: v for k, v in self._recent_pains.items()
            if now - v < self._dedup_window
        }

        payload = {
            "source": self._source,
            "category": cat,
            "message": message,
            "severity": severity,
            "timestamp": now,
        }
        if agent:
            payload["agent"] = agent
        if request_id:
            payload["request_id"] = request_id
        if details:
            payload["details"] = details

        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self.pulse_url}/api/v1/pain",
                    json=payload,
                )
                if resp.status_code < 300:
                    return True
                logger.debug("Pulse pain signal returned %d", resp.status_code)
        except Exception as exc:
            logger.debug("Pulse pain signal failed, queuing: %s", exc)

        self._queue_offline(payload)
        return False

    async def send_loop_break(
        self,
        *,
        agent: str,
        tool: str,
        total_calls: int,
        request_id: str = "",
    ) -> bool:
        """Convenience: send a LoopGuard circuit break pain signal."""
        return await self.send_pain(
            PainCategory.AGENT_LOOP,
            f"LoopGuard circuit break: tool={tool}, calls={total_calls}",
            agent=agent,
            request_id=request_id,
            severity="warning",
            details={"tool": tool, "total_calls": total_calls},
        )

    async def send_quota_breach(
        self,
        *,
        agent: str,
        limit_type: str,
        usage: float,
        limit: float,
        request_id: str = "",
    ) -> bool:
        """Convenience: send a quota breach pain signal."""
        return await self.send_pain(
            PainCategory.QUOTA_BREACH,
            f"Quota {limit_type} breach: {usage:.1f}/{limit:.1f}",
            agent=agent,
            request_id=request_id,
            severity="critical",
            details={"limit_type": limit_type, "usage": usage, "limit": limit},
        )

    async def send_agent_error(
        self,
        *,
        agent: str,
        error: str,
        error_type: str = "",
        request_id: str = "",
    ) -> bool:
        """Convenience: send an unhandled agent error pain signal."""
        return await self.send_pain(
            PainCategory.AGENT_ERROR,
            f"Agent error: {error[:200]}",
            agent=agent,
            request_id=request_id,
            severity="error",
            details={"error_type": error_type, "error": error[:500]},
        )

    async def send_sandbox_violation(
        self,
        *,
        agent: str,
        tool: str,
        capability: str,
        request_id: str = "",
    ) -> bool:
        """Convenience: send a sandbox capability violation pain signal."""
        return await self.send_pain(
            PainCategory.SANDBOX_VIOLATION,
            f"Sandbox denied: tool={tool}, capability={capability}",
            agent=agent,
            request_id=request_id,
            severity="warning",
            details={"tool": tool, "capability": capability},
        )

    def _queue_offline(self, payload: dict):
        """Write to JSONL disk queue for retry."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
            _cap_jsonl(self._queue_path, _QUEUE_MAX_LINES)
        except Exception as exc:
            logger.debug("Failed to queue Pulse data: %s", exc)

    async def flush_queue(self) -> int:
        """Try to send queued pain signals. Returns count sent."""
        if not self._queue_path.exists() or not self.enabled:
            return 0

        try:
            lines = self._queue_path.read_text(encoding="utf-8").strip().split("\n")
        except Exception:
            return 0

        if not lines or lines == [""]:
            return 0

        sent = 0
        remaining = []

        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                for line in lines:
                    try:
                        payload = json.loads(line)
                        resp = await client.post(
                            f"{self.pulse_url}/api/v1/pain",
                            json=payload,
                        )
                        if resp.status_code < 300:
                            sent += 1
                        else:
                            remaining.append(line)
                    except Exception:
                        remaining.append(line)
        except Exception:
            remaining = lines

        if remaining:
            self._queue_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            self._queue_path.unlink(missing_ok=True)

        return sent


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: PulseClient | None = None


def get_pulse() -> PulseClient:
    """Get or create the module-level PulseClient singleton."""
    global _instance
    if _instance is None:
        _instance = PulseClient()
    return _instance


def _cap_jsonl(path: Path, max_lines: int):
    """Cap a JSONL file at max_lines, keeping the newest."""
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except Exception:
        pass
