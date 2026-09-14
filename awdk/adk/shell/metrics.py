"""
AitherShell Prometheus Metrics System
======================================

Collects and exports Prometheus-compatible metrics:
- Query metrics (count, duration, tokens)
- Error tracking
- Model performance
- Cost tracking
- Cache hit rates

Supports both direct Prometheus scraping (/metrics endpoint)
and batch export to Pulse.
"""

import threading
import time
from typing import Dict, Optional, List
from enum import Enum
from dataclasses import dataclass, field
import logging
import sys

logger = logging.getLogger("adk.shell.telemetry")


class MetricType(Enum):
    """Prometheus metric types."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class HistogramBucket:
    """Histogram bucket configuration."""
    le: float  # Less than or equal boundary
    count: int = 0  # Cumulative count in bucket


@dataclass
class HistogramMetric:
    """Histogram metric with buckets."""
    name: str
    help: str
    buckets: List[float]
    bucket_counts: Dict[float, int] = field(default_factory=dict)
    sum: float = 0.0
    count: int = 0
    
    def __post_init__(self):
        # Initialize bucket counts
        for bucket in self.buckets:
            self.bucket_counts[bucket] = 0
        self.bucket_counts[float('inf')] = 0
    
    def observe(self, value: float) -> None:
        """Record a value in the histogram.
        
        Args:
            value: Value to observe
        """
        self.sum += value
        self.count += 1
        
        # Update bucket counts
        for bucket in self.buckets:
            if value <= bucket:
                self.bucket_counts[bucket] += 1
        self.bucket_counts[float('inf')] += 1
    
    def to_prometheus(self) -> str:
        """Export to Prometheus text format.
        
        Returns:
            Prometheus-formatted metric lines
        """
        lines = [f"# HELP {self.name} {self.help}"]
        lines.append(f"# TYPE {self.name} histogram")
        
        # Export buckets
        for bucket in sorted(self.buckets):
            count = self.bucket_counts.get(bucket, 0)
            lines.append(f'{self.name}_bucket{{le="{bucket}"}} {count}')
        
        # +Inf bucket
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self.bucket_counts[float("inf")]}')
        
        # Sum and count
        lines.append(f"{self.name}_sum {self.sum}")
        lines.append(f"{self.name}_count {self.count}")
        
        return "\n".join(lines)


@dataclass
class CounterMetric:
    """Counter metric."""
    name: str
    help: str
    labels: Dict[str, str] = field(default_factory=dict)
    value: int = 0
    
    def inc(self, amount: int = 1) -> None:
        """Increment counter.
        
        Args:
            amount: Amount to increment by
        """
        self.value += amount
    
    def to_prometheus(self) -> str:
        """Export to Prometheus text format.
        
        Returns:
            Prometheus-formatted metric lines
        """
        label_str = ""
        if self.labels:
            label_pairs = [f'{k}="{v}"' for k, v in self.labels.items()]
            label_str = "{" + ",".join(label_pairs) + "}"
        
        return f"{self.name}{label_str} {self.value}"


@dataclass
class GaugeMetric:
    """Gauge metric."""
    name: str
    help: str
    labels: Dict[str, str] = field(default_factory=dict)
    value: float = 0.0
    
    def set(self, value: float) -> None:
        """Set gauge value.
        
        Args:
            value: Value to set
        """
        self.value = value
    
    def inc(self, amount: float = 1.0) -> None:
        """Increment gauge.
        
        Args:
            amount: Amount to increment by
        """
        self.value += amount
    
    def dec(self, amount: float = 1.0) -> None:
        """Decrement gauge.
        
        Args:
            amount: Amount to decrement by
        """
        self.value -= amount
    
    def to_prometheus(self) -> str:
        """Export to Prometheus text format.
        
        Returns:
            Prometheus-formatted metric lines
        """
        label_str = ""
        if self.labels:
            label_pairs = [f'{k}="{v}"' for k, v in self.labels.items()]
            label_str = "{" + ",".join(label_pairs) + "}"
        
        return f"{self.name}{label_str} {self.value}"


class PrometheusMetricsCollector:
    """
    Collects and manages Prometheus metrics for AitherShell.
    
    Metrics:
    1. Counter: aithershell_queries_total{model="...", effort="..."}
    2. Histogram: aithershell_query_duration_seconds (buckets: 1, 5, 10)
    3. Histogram: aithershell_tokens_used (buckets: 500, 2000, 8000)
    4. Counter: aithershell_errors_total{error_type="..."}
    5. Counter: aithershell_model_cache_hits_total{model="..."}
    6. Gauge: aithershell_cloud_compute_costs_usd_total
    """
    
    def __init__(self, enabled: bool = True):
        """Initialize metrics collector.
        
        Args:
            enabled: Whether metrics collection is enabled
        """
        self.enabled = enabled
        self.lock = threading.Lock()
        
        # Metrics storage
        self.counters: Dict[str, CounterMetric] = {}
        self.gauges: Dict[str, GaugeMetric] = {}
        self.histograms: Dict[str, HistogramMetric] = {}
        
        # Initialize metrics
        self._init_metrics()
    
    def _init_metrics(self) -> None:
        """Initialize all metrics."""
        with self.lock:
            # 1. Query count with model and effort labels
            # We'll use a counter per model+effort combination
            
            # 2. Query duration histogram (seconds)
            self.histograms["aithershell_query_duration_seconds"] = HistogramMetric(
                name="aithershell_query_duration_seconds",
                help="Query execution duration in seconds",
                buckets=[1.0, 5.0, 10.0],
            )
            
            # 3. Tokens used histogram
            self.histograms["aithershell_tokens_used"] = HistogramMetric(
                name="aithershell_tokens_used",
                help="Tokens used per query",
                buckets=[500, 2000, 8000],
            )
            
            # 6. Cloud compute costs gauge
            self.gauges["aithershell_cloud_compute_costs_usd_total"] = GaugeMetric(
                name="aithershell_cloud_compute_costs_usd_total",
                help="Total cloud compute costs in USD",
                value=0.0,
            )
    
    def record_query(
        self,
        duration_ms: float,
        tokens_used: int,
        model: Optional[str] = None,
        effort: Optional[int] = None,
        cached: bool = False,
    ) -> None:
        """Record a completed query.
        
        Args:
            duration_ms: Query duration in milliseconds
            tokens_used: Tokens consumed
            model: Model used
            effort: Effort level
            cached: Whether response was cached
        """
        if not self.enabled:
            return
        
        with self.lock:
            # 1. Increment query counter
            counter_key = f"aithershell_queries_total{{{model or 'unknown'}:{effort or 0}}}"
            if counter_key not in self.counters:
                labels = {}
                if model:
                    labels["model"] = model
                if effort:
                    labels["effort"] = str(effort)
                self.counters[counter_key] = CounterMetric(
                    name="aithershell_queries_total",
                    help="Total queries executed",
                    labels=labels,
                )
            self.counters[counter_key].inc()
            
            # 2. Record duration (convert ms to seconds)
            duration_sec = duration_ms / 1000.0
            self.histograms["aithershell_query_duration_seconds"].observe(duration_sec)
            
            # 3. Record tokens used
            self.histograms["aithershell_tokens_used"].observe(tokens_used)
            
            # 5. Track cache hits if applicable
            if cached and model:
                cache_hit_key = f"aithershell_model_cache_hits_total{{{model}}}"
                if cache_hit_key not in self.counters:
                    self.counters[cache_hit_key] = CounterMetric(
                        name="aithershell_model_cache_hits_total",
                        help="Cache hits per model",
                        labels={"model": model},
                    )
                self.counters[cache_hit_key].inc()
    
    def record_error(
        self,
        error_type: str,
    ) -> None:
        """Record an error.
        
        Args:
            error_type: Type of error (e.g., "api_timeout")
        """
        if not self.enabled:
            return
        
        with self.lock:
            # 4. Increment error counter
            error_key = f"aithershell_errors_total{{{error_type}}}"
            if error_key not in self.counters:
                self.counters[error_key] = CounterMetric(
                    name="aithershell_errors_total",
                    help="Total errors by type",
                    labels={"error_type": error_type},
                )
            self.counters[error_key].inc()
    
    def set_cost(self, cost_usd: float) -> None:
        """Set total cloud compute cost.
        
        Args:
            cost_usd: Total cost in USD
        """
        if not self.enabled:
            return
        
        with self.lock:
            self.gauges["aithershell_cloud_compute_costs_usd_total"].set(cost_usd)
    
    def inc_cost(self, delta_usd: float) -> None:
        """Increment total cloud compute cost.
        
        Args:
            delta_usd: Cost delta in USD
        """
        if not self.enabled:
            return
        
        with self.lock:
            self.gauges["aithershell_cloud_compute_costs_usd_total"].inc(delta_usd)
    
    def to_prometheus_text(self) -> str:
        """Export all metrics to Prometheus text format.
        
        Returns:
            Prometheus-compatible metric text
        """
        with self.lock:
            lines = []
            
            # Counters
            for counter in self.counters.values():
                lines.append(f"# HELP {counter.name} {counter.help}")
                lines.append(f"# TYPE {counter.name} counter")
                lines.append(counter.to_prometheus())
                lines.append("")
            
            # Gauges
            for gauge in self.gauges.values():
                lines.append(f"# HELP {gauge.name} {gauge.help}")
                lines.append(f"# TYPE {gauge.name} gauge")
                lines.append(gauge.to_prometheus())
                lines.append("")
            
            # Histograms
            for histogram in self.histograms.values():
                lines.append(f"# HELP {histogram.name} {histogram.help}")
                lines.append(f"# TYPE {histogram.name} histogram")
                lines.append(histogram.to_prometheus())
                lines.append("")
            
            return "\n".join(lines)
    
    def to_json(self) -> Dict:
        """Export metrics as JSON for batch transmission.
        
        Returns:
            Dictionary with all metrics
        """
        with self.lock:
            metrics = {
                "counters": {},
                "gauges": {},
                "histograms": {},
                "timestamp": time.time(),
            }
            
            for key, counter in self.counters.items():
                metrics["counters"][key] = {
                    "value": counter.value,
                    "labels": counter.labels,
                }
            
            for key, gauge in self.gauges.items():
                metrics["gauges"][key] = {
                    "value": gauge.value,
                    "labels": gauge.labels,
                }
            
            for key, histogram in self.histograms.items():
                metrics["histograms"][key] = {
                    "sum": histogram.sum,
                    "count": histogram.count,
                    "buckets": histogram.bucket_counts,
                }
            
            return metrics


# Global instance
_global_collector: Optional[PrometheusMetricsCollector] = None
_collector_lock = threading.Lock()


def get_metrics_collector(enabled: bool = True) -> PrometheusMetricsCollector:
    """Get or create global metrics collector.
    
    Args:
        enabled: Whether to enable metrics collection
        
    Returns:
        Global PrometheusMetricsCollector instance
    """
    global _global_collector
    
    if _global_collector is None:
        with _collector_lock:
            if _global_collector is None:
                _global_collector = PrometheusMetricsCollector(enabled=enabled)
    
    return _global_collector
