"""Agent Metering — Per-agent quota and cost tracking.

Per-agent budget metering with graduated quota enforcement.
Integrates with AitherOS ACTA billing service when available, falls back
to local SQLite tracking.

Features:
  - Per-agent token budgets (hourly/daily/monthly)
  - Cost-per-call tracking with model-aware pricing
  - Quota enforcement with soft/hard limits
  - Usage history for analytics
  - ACTA service integration for centralized billing

Usage:
    meter = AgentMeter(agent_name="atlas")

    # Check budget before LLM call
    if meter.can_spend(estimated_tokens=500):
        response = await llm.chat(messages)
        meter.record_usage(
            tokens=response.tokens_used,
            model=response.model,
            latency_ms=response.latency_ms,
        )

    # Get usage report
    report = meter.usage_report()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.metering")


# ─────────────────────────────────────────────────────────────────────────────
# Pricing model (approximate, per 1K tokens)
# ─────────────────────────────────────────────────────────────────────────────

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Local models (free / electricity only)
    "ollama": {"input": 0.0, "output": 0.0},
    "vllm": {"input": 0.0, "output": 0.0},
    "local": {"input": 0.0, "output": 0.0},
    # Cloud models (per 1K tokens in USD)
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3.5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "gemini-pro": {"input": 0.00025, "output": 0.0005},
    "gemini-flash": {"input": 0.000075, "output": 0.0003},
    "default": {"input": 0.001, "output": 0.002},
}


def is_metered_backend(backend_type: str) -> bool:
    """True only for Aitherium's metered cloud gateway.

    Token/cost quotas are ENFORCED on exactly one backend — the metered gateway
    (``provider="gateway"``), where Aitherium pays for the inference. Every other
    backend is the user's own inference and is NEVER capped:

      - BYO API keys: anthropic / openai / deepseek / groq / together / …
      - local runtimes: ollama / vllm / llamacpp / lmstudio
      - any unknown / custom OpenAI-compatible endpoint

    Enforcement is therefore opt-in by provenance and FAIL-OPEN: an unrecognized
    backend is treated as self-hosted (uncapped), never bricked. This is the
    inverse of an allowlist — only ``gateway`` opts into a cap.
    """
    return (backend_type or "").strip().lower() == "gateway"


class QuotaAction(str, Enum):
    """What to do when quota is exceeded."""
    ALLOW = "allow"          # Under budget
    WARN = "warn"            # Approaching limit (>80%)
    SOFT_LIMIT = "soft_limit"  # Over soft limit, may still proceed
    HARD_LIMIT = "hard_limit"  # Over hard limit, must stop


@dataclass
class UsageRecord:
    """A single usage record."""
    agent_name: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_total: int = 0
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    tool_name: str = ""
    timestamp: float = 0.0


@dataclass
class QuotaConfig:
    """Quota configuration for an agent.

    Limits are in tokens. Set to 0 for unlimited.
    """
    hourly_limit: int = 0        # 0 = unlimited
    daily_limit: int = 0         # 0 = unlimited (caps are opt-in; gateway-only)
    monthly_limit: int = 0       # 0 = unlimited
    cost_limit_usd: float = 0.0  # 0 = unlimited
    soft_limit_pct: float = 0.8  # Warn at 80%


@dataclass
class UsageReport:
    """Usage report for an agent."""
    agent_name: str
    tokens_used_hour: int = 0
    tokens_used_day: int = 0
    tokens_used_month: int = 0
    cost_usd_day: float = 0.0
    cost_usd_month: float = 0.0
    calls_today: int = 0
    calls_total: int = 0
    avg_latency_ms: float = 0.0
    top_models: list[str] = field(default_factory=list)
    quota_status: QuotaAction = QuotaAction.ALLOW


# ─────────────────────────────────────────────────────────────────────────────
# Agent Meter
# ─────────────────────────────────────────────────────────────────────────────

class AgentMeter:
    """Per-agent token and cost metering with quota enforcement.

    Tracks usage in a local SQLite database. Can optionally report
    to AitherOS ACTA billing service for centralized tracking.

    The _backend_type attribute (set by agent.py) indicates the LLM provenance:
      - "gateway": Aitherium metered cloud (cap applies)
      - "anthropic", "openai", "deepseek": BYO API keys (no cap)
      - "ollama", "vllm", "local": local/self-hosted (no cap)
    """

    def __init__(
        self,
        agent_name: str = "default",
        quota: QuotaConfig | None = None,
        db_path: str | Path | None = None,
        acta_url: str | None = None,
    ):
        self._agent = agent_name
        self._quota = quota or QuotaConfig()
        self._acta_url = acta_url
        self._backend_type = "unknown"
        self._warned_80pct = False

        if db_path is None:
            data_dir = Path.home() / ".aither" / "metering"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "usage.db"

        self._db_path = str(db_path)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    tokens_input INTEGER DEFAULT 0,
                    tokens_output INTEGER DEFAULT 0,
                    tokens_total INTEGER DEFAULT 0,
                    model TEXT DEFAULT '',
                    cost_usd REAL DEFAULT 0.0,
                    latency_ms REAL DEFAULT 0.0,
                    tool_name TEXT DEFAULT '',
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage(agent_name);
                CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(timestamp);
                CREATE INDEX IF NOT EXISTS idx_usage_agent_ts ON usage(agent_name, timestamp);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record_usage(
        self,
        tokens: int = 0,
        tokens_input: int = 0,
        tokens_output: int = 0,
        model: str = "",
        latency_ms: float = 0.0,
        tool_name: str = "",
    ) -> UsageRecord:
        """Record a usage event."""
        if tokens and not tokens_input and not tokens_output:
            # Estimate 30/70 split if only total given
            tokens_input = int(tokens * 0.3)
            tokens_output = tokens - tokens_input

        total = tokens or (tokens_input + tokens_output)
        cost = self._calculate_cost(tokens_input, tokens_output, model)
        now = time.time()

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage (agent_name, tokens_input, tokens_output, tokens_total, "
                "model, cost_usd, latency_ms, tool_name, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._agent, tokens_input, tokens_output, total,
                 model, cost, latency_ms, tool_name, now),
            )

        record = UsageRecord(
            agent_name=self._agent,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            tokens_total=total,
            model=model,
            cost_usd=cost,
            latency_ms=latency_ms,
            tool_name=tool_name,
            timestamp=now,
        )

        logger.debug(
            "Metered: agent=%s tokens=%d model=%s cost=$%.6f",
            self._agent, total, model, cost,
        )
        return record

    def can_spend(self, estimated_tokens: int = 0) -> QuotaAction:
        """Check if the agent can spend more tokens.

        Returns the appropriate quota action based on current usage. Also emits
        a one-time warning at 80% of monthly cap (if enforced on this backend).
        """
        # Caps are enforced ONLY on Aitherium's metered gateway. BYO / local /
        # custom / unknown backends pay for their own inference and are never
        # capped — fail-open so a self-hosted agent can never be bricked by a
        # stray quota (e.g. a community default or a product super-cap).
        if not is_metered_backend(self._backend_type):
            return QuotaAction.ALLOW
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400
        month_ago = now - 2592000

        with self._connect() as conn:
            # Hourly usage
            if self._quota.hourly_limit > 0:
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens_total), 0) FROM usage "
                    "WHERE agent_name = ? AND timestamp > ?",
                    (self._agent, hour_ago),
                ).fetchone()
                hourly = row[0] + estimated_tokens
                if hourly >= self._quota.hourly_limit:
                    return QuotaAction.HARD_LIMIT
                if hourly >= self._quota.hourly_limit * self._quota.soft_limit_pct:
                    return QuotaAction.WARN

            # Daily usage
            if self._quota.daily_limit > 0:
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens_total), 0) FROM usage "
                    "WHERE agent_name = ? AND timestamp > ?",
                    (self._agent, day_ago),
                ).fetchone()
                daily = row[0] + estimated_tokens
                if daily >= self._quota.daily_limit:
                    return QuotaAction.HARD_LIMIT
                if daily >= self._quota.daily_limit * self._quota.soft_limit_pct:
                    return QuotaAction.WARN

            # Monthly usage (with 80% warning for gateway-backed agents)
            if self._quota.monthly_limit > 0:
                row = conn.execute(
                    "SELECT COALESCE(SUM(tokens_total), 0) FROM usage "
                    "WHERE agent_name = ? AND timestamp > ?",
                    (self._agent, month_ago),
                ).fetchone()
                monthly = row[0] + estimated_tokens
                if monthly >= self._quota.monthly_limit:
                    return QuotaAction.HARD_LIMIT
                if monthly >= self._quota.monthly_limit * self._quota.soft_limit_pct:
                    if not self._warned_80pct and self._backend_type == "gateway":
                        pct = int(100.0 * monthly / self._quota.monthly_limit)
                        logger.warning(
                            "Agent '%s' at %d%% of monthly token cap (%d / %d tokens). "
                            "Upgrade to a paid tier or use a local/BYO backend: "
                            "https://aitherium.com/buy",
                            self._agent, pct, monthly, self._quota.monthly_limit,
                        )
                        self._warned_80pct = True
                    return QuotaAction.WARN

            # Cost limit
            if self._quota.cost_limit_usd > 0:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
                    "WHERE agent_name = ? AND timestamp > ?",
                    (self._agent, month_ago),
                ).fetchone()
                cost = row[0]
                if cost >= self._quota.cost_limit_usd:
                    return QuotaAction.HARD_LIMIT
                if cost >= self._quota.cost_limit_usd * self._quota.soft_limit_pct:
                    return QuotaAction.WARN

        return QuotaAction.ALLOW

    def usage_report(self) -> UsageReport:
        """Get a usage report for this agent."""
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400
        month_ago = now - 2592000

        with self._connect() as conn:
            # Hourly tokens
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_total), 0) FROM usage "
                "WHERE agent_name = ? AND timestamp > ?",
                (self._agent, hour_ago),
            ).fetchone()
            tokens_hour = row[0]

            # Daily tokens + cost + count
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_total), 0), COALESCE(SUM(cost_usd), 0), COUNT(*) "
                "FROM usage WHERE agent_name = ? AND timestamp > ?",
                (self._agent, day_ago),
            ).fetchone()
            tokens_day, cost_day, calls_today = row

            # Monthly tokens + cost
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_total), 0), COALESCE(SUM(cost_usd), 0) "
                "FROM usage WHERE agent_name = ? AND timestamp > ?",
                (self._agent, month_ago),
            ).fetchone()
            tokens_month, cost_month = row

            # Total calls
            row = conn.execute(
                "SELECT COUNT(*) FROM usage WHERE agent_name = ?",
                (self._agent,),
            ).fetchone()
            calls_total = row[0]

            # Average latency (last 24h)
            row = conn.execute(
                "SELECT COALESCE(AVG(latency_ms), 0) FROM usage "
                "WHERE agent_name = ? AND timestamp > ? AND latency_ms > 0",
                (self._agent, day_ago),
            ).fetchone()
            avg_latency = row[0]

            # Top models (last 24h)
            rows = conn.execute(
                "SELECT model, COUNT(*) as cnt FROM usage "
                "WHERE agent_name = ? AND timestamp > ? AND model != '' "
                "GROUP BY model ORDER BY cnt DESC LIMIT 3",
                (self._agent, day_ago),
            ).fetchall()
            top_models = [r[0] for r in rows]

        return UsageReport(
            agent_name=self._agent,
            tokens_used_hour=tokens_hour,
            tokens_used_day=tokens_day,
            tokens_used_month=tokens_month,
            cost_usd_day=cost_day,
            cost_usd_month=cost_month,
            calls_today=calls_today,
            calls_total=calls_total,
            avg_latency_ms=avg_latency,
            top_models=top_models,
            quota_status=self.can_spend(),
        )

    def reset_usage(self, older_than_days: int = 0) -> int:
        """Delete usage records. If older_than_days=0, deletes all."""
        with self._connect() as conn:
            if older_than_days > 0:
                cutoff = time.time() - (older_than_days * 86400)
                result = conn.execute(
                    "DELETE FROM usage WHERE agent_name = ? AND timestamp < ?",
                    (self._agent, cutoff),
                )
            else:
                result = conn.execute(
                    "DELETE FROM usage WHERE agent_name = ?",
                    (self._agent,),
                )
            return result.rowcount

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    def _calculate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        """Calculate cost based on model pricing."""
        # Normalize model name for lookup
        model_key = model.lower().strip()
        pricing = MODEL_PRICING.get(model_key)

        if not pricing:
            # Try prefix match
            for key, prices in MODEL_PRICING.items():
                if model_key.startswith(key):
                    pricing = prices
                    break

        if not pricing:
            pricing = MODEL_PRICING["default"]

        cost_input = (tokens_input / 1000.0) * pricing["input"]
        cost_output = (tokens_output / 1000.0) * pricing["output"]
        return cost_input + cost_output


# ─────────────────────────────────────────────────────────────────────────────
# Singleton per agent
# ─────────────────────────────────────────────────────────────────────────────

_meters: Dict[str, AgentMeter] = {}


def get_meter(agent_name: str = "default", **kwargs) -> AgentMeter:
    """Get or create a meter for an agent."""
    if agent_name not in _meters:
        _meters[agent_name] = AgentMeter(agent_name=agent_name, **kwargs)
    return _meters[agent_name]
