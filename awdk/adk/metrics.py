"""Prometheus metrics — /metrics endpoint for ADK observability.

Exports agent metering, tool execution, LLM latency, and health data
as Prometheus-compatible metrics. Works with or without the
prometheus_client library — falls back to a minimal text format exporter.

Usage:
    from adk.metrics import get_metrics, MetricsCollector

    metrics = get_metrics()
    metrics.record_llm_call(model="llama3.2", latency_ms=150, tokens=500)
    metrics.record_tool_call(tool="web_search", latency_ms=42, success=True)
    metrics.record_request(latency_ms=200, status_code=200)

    # In server.py
    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(metrics.export(), media_type="text/plain")
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("adk.metrics")


@dataclass
class _HistogramBucket:
    """Simple histogram with fixed buckets for latency tracking."""
    buckets: list[float] = field(default_factory=lambda: [
        5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000,
    ])
    counts: dict[float, int] = field(default_factory=dict)
    total: float = 0.0
    count: int = 0

    def __post_init__(self):
        for b in self.buckets:
            self.counts.setdefault(b, 0)
        self.counts[float("inf")] = 0

    def observe(self, value: float):
        self.total += value
        self.count += 1
        for b in self.buckets:
            if value <= b:
                self.counts[b] += 1
        self.counts[float("inf")] += 1


class MetricsCollector:
    """Collects and exports Prometheus-format metrics.

    Thread-safe via a simple lock. Designed for low-overhead collection.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # Counters
        self._requests_total = 0
        self._requests_by_status: Dict[int, int] = defaultdict(int)
        self._errors_total = 0

        self._llm_calls_total = 0
        self._llm_tokens_total = 0
        self._llm_calls_by_model: Dict[str, int] = defaultdict(int)
        self._llm_tokens_by_model: Dict[str, int] = defaultdict(int)
        self._llm_errors_total = 0

        self._tool_calls_total = 0
        self._tool_calls_by_name: Dict[str, int] = defaultdict(int)
        self._tool_errors_by_name: Dict[str, int] = defaultdict(int)

        self._agent_spawns_total = 0
        self._agent_spawns_by_type: Dict[str, int] = defaultdict(int)

        self._sandbox_blocks_total = 0
        self._loop_guard_breaks_total = 0
        self._quota_breaches_total = 0

        # Histograms
        self._request_latency = _HistogramBucket()
        self._llm_latency = _HistogramBucket()
        self._tool_latency = _HistogramBucket()

        # Gauges
        self._active_sessions = 0
        self._active_agents = 0
        self._start_time = time.time()

    # ─── Recording ──────────────────────────────────────────────────────

    def record_request(self, latency_ms: float = 0, status_code: int = 200):
        with self._lock:
            self._requests_total += 1
            self._requests_by_status[status_code] += 1
            if status_code >= 500:
                self._errors_total += 1
            if latency_ms > 0:
                self._request_latency.observe(latency_ms)

    def record_llm_call(
        self,
        model: str = "",
        latency_ms: float = 0,
        tokens: int = 0,
        success: bool = True,
    ):
        with self._lock:
            self._llm_calls_total += 1
            self._llm_tokens_total += tokens
            if model:
                self._llm_calls_by_model[model] += 1
                self._llm_tokens_by_model[model] += tokens
            if not success:
                self._llm_errors_total += 1
            if latency_ms > 0:
                self._llm_latency.observe(latency_ms)

    def record_tool_call(
        self,
        tool: str = "",
        latency_ms: float = 0,
        success: bool = True,
    ):
        with self._lock:
            self._tool_calls_total += 1
            if tool:
                self._tool_calls_by_name[tool] += 1
                if not success:
                    self._tool_errors_by_name[tool] += 1
            if latency_ms > 0:
                self._tool_latency.observe(latency_ms)

    def record_agent_spawn(self, agent_type: str = ""):
        with self._lock:
            self._agent_spawns_total += 1
            if agent_type:
                self._agent_spawns_by_type[agent_type] += 1

    def record_sandbox_block(self):
        with self._lock:
            self._sandbox_blocks_total += 1

    def record_loop_guard_break(self):
        with self._lock:
            self._loop_guard_breaks_total += 1

    def record_quota_breach(self):
        with self._lock:
            self._quota_breaches_total += 1

    def set_active_sessions(self, count: int):
        self._active_sessions = count

    def set_active_agents(self, count: int):
        self._active_agents = count

    # ─── Export ──────────────────────────────────────────────────────────

    def export(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines: list[str] = []

        with self._lock:
            # ── Request metrics ──
            lines.append("# HELP adk_requests_total Total HTTP requests")
            lines.append("# TYPE adk_requests_total counter")
            lines.append(f"adk_requests_total {self._requests_total}")

            for code, count in sorted(self._requests_by_status.items()):
                lines.append(f'adk_requests_by_status{{code="{code}"}} {count}')

            lines.append("# HELP adk_errors_total Total server errors (5xx)")
            lines.append("# TYPE adk_errors_total counter")
            lines.append(f"adk_errors_total {self._errors_total}")

            self._export_histogram(lines, "adk_request_latency_ms", self._request_latency,
                                   "Request latency in milliseconds")

            # ── LLM metrics ──
            lines.append("# HELP adk_llm_calls_total Total LLM calls")
            lines.append("# TYPE adk_llm_calls_total counter")
            lines.append(f"adk_llm_calls_total {self._llm_calls_total}")

            lines.append("# HELP adk_llm_tokens_total Total tokens consumed")
            lines.append("# TYPE adk_llm_tokens_total counter")
            lines.append(f"adk_llm_tokens_total {self._llm_tokens_total}")

            lines.append("# HELP adk_llm_errors_total Total LLM errors")
            lines.append("# TYPE adk_llm_errors_total counter")
            lines.append(f"adk_llm_errors_total {self._llm_errors_total}")

            for model, count in sorted(self._llm_calls_by_model.items()):
                lines.append(f'adk_llm_calls_by_model{{model="{model}"}} {count}')

            for model, tokens in sorted(self._llm_tokens_by_model.items()):
                lines.append(f'adk_llm_tokens_by_model{{model="{model}"}} {tokens}')

            self._export_histogram(lines, "adk_llm_latency_ms", self._llm_latency,
                                   "LLM call latency in milliseconds")

            # ── Tool metrics ──
            lines.append("# HELP adk_tool_calls_total Total tool calls")
            lines.append("# TYPE adk_tool_calls_total counter")
            lines.append(f"adk_tool_calls_total {self._tool_calls_total}")

            for tool, count in sorted(self._tool_calls_by_name.items()):
                lines.append(f'adk_tool_calls_by_name{{tool="{tool}"}} {count}')

            for tool, count in sorted(self._tool_errors_by_name.items()):
                lines.append(f'adk_tool_errors_by_name{{tool="{tool}"}} {count}')

            self._export_histogram(lines, "adk_tool_latency_ms", self._tool_latency,
                                   "Tool call latency in milliseconds")

            # ── Agent metrics ──
            lines.append("# HELP adk_agent_spawns_total Total agent spawns")
            lines.append("# TYPE adk_agent_spawns_total counter")
            lines.append(f"adk_agent_spawns_total {self._agent_spawns_total}")

            for agent, count in sorted(self._agent_spawns_by_type.items()):
                lines.append(f'adk_agent_spawns_by_type{{agent="{agent}"}} {count}')

            # ── Security metrics ──
            lines.append("# HELP adk_sandbox_blocks_total Sandbox capability denials")
            lines.append("# TYPE adk_sandbox_blocks_total counter")
            lines.append(f"adk_sandbox_blocks_total {self._sandbox_blocks_total}")

            lines.append("# HELP adk_loop_guard_breaks_total LoopGuard circuit breaks")
            lines.append("# TYPE adk_loop_guard_breaks_total counter")
            lines.append(f"adk_loop_guard_breaks_total {self._loop_guard_breaks_total}")

            lines.append("# HELP adk_quota_breaches_total Quota hard limit breaches")
            lines.append("# TYPE adk_quota_breaches_total counter")
            lines.append(f"adk_quota_breaches_total {self._quota_breaches_total}")

            # ── Gauges ──
            lines.append("# HELP adk_active_sessions Current active sessions")
            lines.append("# TYPE adk_active_sessions gauge")
            lines.append(f"adk_active_sessions {self._active_sessions}")

            lines.append("# HELP adk_active_agents Current active agents")
            lines.append("# TYPE adk_active_agents gauge")
            lines.append(f"adk_active_agents {self._active_agents}")

            lines.append("# HELP adk_uptime_seconds Server uptime")
            lines.append("# TYPE adk_uptime_seconds gauge")
            lines.append(f"adk_uptime_seconds {time.time() - self._start_time:.1f}")

        return "\n".join(lines) + "\n"

    def _export_histogram(
        self,
        lines: list[str],
        name: str,
        hist: _HistogramBucket,
        help_text: str,
    ):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} histogram")
        for bucket in hist.buckets:
            lines.append(f'{name}_bucket{{le="{bucket}"}} {hist.counts.get(bucket, 0)}')
        lines.append(f'{name}_bucket{{le="+Inf"}} {hist.counts.get(float("inf"), 0)}')
        lines.append(f"{name}_sum {hist.total:.1f}")
        lines.append(f"{name}_count {hist.count}")


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Get or create the module-level MetricsCollector singleton."""
    global _instance
    if _instance is None:
        _instance = MetricsCollector()
    return _instance
