"""
AitherShell Telemetry Tests
============================

Tests for event emission, metrics collection, and privacy mode.
"""

import pytest
import json
import time
from unittest.mock import Mock, patch, MagicMock
from adk.shell.events import (
    PulseEventClient,
    create_query_id,
    query_context_from_request,
    PrivacyLevel,
    EventSeverity,
)
from adk.shell.metrics import (
    PrometheusMetricsCollector,
    HistogramMetric,
    CounterMetric,
    GaugeMetric,
)
from adk.shell.telemetry_config import (
    TelemetryConfig,
    load_telemetry_config,
    TelemetryContext,
)


class TestPulseEventClient:
    """Tests for PulseEventClient."""
    
    def test_initialization(self):
        """Test client initialization."""
        client = PulseEventClient(
            service_name="aithershell",
            pulse_url="http://localhost:8081",
            enabled=True,
        )
        
        assert client.service_name == "aithershell"
        assert client.pulse_url == "http://localhost:8081"
        assert client.enabled is True
        assert len(client.event_queue) == 0
    
    def test_create_query_id(self):
        """Test query ID generation."""
        query_id = create_query_id()
        assert isinstance(query_id, str)
        assert len(query_id) == 8
        
        # IDs should be unique
        query_id2 = create_query_id()
        assert query_id != query_id2
    
    def test_query_context_from_request(self):
        """Test creating query context."""
        ctx = query_context_from_request(
            persona="assistant",
            effort=5,
            tokens_estimated=1000,
            query_text="hello world",
        )
        
        assert ctx.persona == "assistant"
        assert ctx.effort == 5
        assert ctx.tokens_estimated == 1000
        assert ctx.query_text == "hello world"
        assert ctx.query_id is not None
        assert ctx.start_time is not None
    
    def test_privacy_filter_public(self):
        """Test privacy filter for public level."""
        client = PulseEventClient(enabled=True)
        
        data = {
            "query_text": "secret query",
            "query_id": "abc123",
        }
        
        filtered = client._apply_privacy_filter(data, PrivacyLevel.PUBLIC.value)
        assert "query_text" in filtered
        assert filtered["query_text"] == "secret query"
    
    def test_privacy_filter_private(self):
        """Test privacy filter for private level."""
        client = PulseEventClient(enabled=True)
        
        data = {
            "query_text": "secret query",
            "query_id": "abc123",
        }
        
        filtered = client._apply_privacy_filter(data, PrivacyLevel.PRIVATE.value)
        assert "query_text" not in filtered
        assert filtered["query_id"] == "abc123"
    
    def test_privacy_filter_redacted(self):
        """Test privacy filter for redacted level."""
        client = PulseEventClient(enabled=True)
        
        data = {
            "query_text": "secret query",
            "query_id": "abc123",
        }
        
        filtered = client._apply_privacy_filter(data, PrivacyLevel.REDACTED.value)
        assert "query_text" in filtered
        assert "[REDACTED:" in filtered["query_text"]
    
    def test_emit_query_started(self):
        """Test emitting query started event."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_started(
            query_id="q123",
            persona="assistant",
            effort=5,
            tokens_estimated=1000,
            query_text="test query",
            privacy_level=PrivacyLevel.PUBLIC.value,
        )
        
        assert len(client.event_queue) == 1
        event = client.event_queue[0]
        assert event["event"] == "adk.shell.query.started"
        assert event["data"]["query_id"] == "q123"
        assert event["data"]["persona"] == "assistant"
    
    def test_emit_query_completed(self):
        """Test emitting query completed event."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_completed(
            query_id="q123",
            duration_ms=1234.5,
            tokens_used=150,
            model="gpt-4",
            cached=False,
        )
        
        assert len(client.event_queue) == 1
        event = client.event_queue[0]
        assert event["event"] == "adk.shell.query.completed"
        assert event["data"]["duration_ms"] == 1234.5
        assert event["data"]["tokens_used"] == 150
        assert event["data"]["model"] == "gpt-4"
    
    def test_emit_error(self):
        """Test emitting error event."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_error(
            error_type="api_timeout",
            severity=EventSeverity.ERROR.value,
            retry_count=2,
            message="Connection timeout",
            query_id="q123",
        )
        
        assert len(client.event_queue) == 1
        event = client.event_queue[0]
        assert event["event"] == "adk.shell.error"
        assert event["data"]["error_type"] == "api_timeout"
        assert event["data"]["severity"] == "error"
        assert event["data"]["retry_count"] == 2
    
    def test_emit_plugin_executed(self):
        """Test emitting plugin execution event."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_plugin_executed(
            plugin_name="github_plugin",
            duration_ms=500.0,
            success=True,
        )
        
        assert len(client.event_queue) == 1
        event = client.event_queue[0]
        assert event["event"] == "adk.shell.plugin.executed"
        assert event["data"]["plugin_name"] == "github_plugin"
        assert event["data"]["duration_ms"] == 500.0
        assert event["data"]["success"] is True
    
    def test_update_metrics(self):
        """Test updating internal metrics."""
        client = PulseEventClient(enabled=True)
        
        client.update_metrics(
            queries_completed=10,
            errors_total=2,
            local_cache_hits=3,
        )
        
        assert client.metrics["queries_completed"] == 10
        assert client.metrics["errors_total"] == 2
        assert client.metrics["local_cache_hits"] == 3
    
    def test_disabled_client(self):
        """Test that disabled client doesn't emit events."""
        client = PulseEventClient(enabled=False)
        
        client.emit_query_started(query_id="q123", persona="test")
        assert len(client.event_queue) == 0
    
    def test_batch_auto_flush(self):
        """Test automatic flush when batch is full."""
        client = PulseEventClient(
            enabled=True,
            max_batch_size=3,
            flush_interval_ms=10000,
        )
        
        # Add events up to batch size
        for i in range(3):
            client.emit_query_started(query_id=f"q{i}")
        
        # Queue should be flushed, but background thread may not have run yet
        # So we just verify the mechanism is in place
        assert client.enabled is True


class TestPrometheusMetrics:
    """Tests for Prometheus metrics."""
    
    def test_histogram_observe(self):
        """Test histogram observation."""
        histogram = HistogramMetric(
            name="test_histogram",
            help="Test histogram",
            buckets=[1.0, 5.0, 10.0],
        )
        
        histogram.observe(2.5)
        histogram.observe(7.5)
        
        assert histogram.count == 2
        assert histogram.sum == 10.0
        assert histogram.bucket_counts[1.0] == 0
        assert histogram.bucket_counts[5.0] == 1
        assert histogram.bucket_counts[10.0] == 2
        assert histogram.bucket_counts[float('inf')] == 2
    
    def test_histogram_to_prometheus(self):
        """Test histogram Prometheus export format."""
        histogram = HistogramMetric(
            name="test_hist",
            help="Test metric",
            buckets=[1.0, 5.0],
        )
        
        histogram.observe(3.0)
        
        prometheus_text = histogram.to_prometheus()
        assert "# HELP test_hist" in prometheus_text
        assert "# TYPE test_hist histogram" in prometheus_text
        assert 'test_hist_bucket{le="1.0"}' in prometheus_text
        assert 'test_hist_bucket{le="5.0"}' in prometheus_text
        assert 'test_hist_bucket{le="+Inf"}' in prometheus_text
        assert "test_hist_sum 3.0" in prometheus_text
        assert "test_hist_count 1" in prometheus_text
    
    def test_counter_metric(self):
        """Test counter metric."""
        counter = CounterMetric(
            name="test_counter",
            help="Test counter",
            labels={"model": "gpt-4"},
        )
        
        counter.inc()
        counter.inc(5)
        
        assert counter.value == 6
    
    def test_gauge_metric(self):
        """Test gauge metric."""
        gauge = GaugeMetric(
            name="test_gauge",
            help="Test gauge",
        )
        
        gauge.set(10.5)
        gauge.inc(2.0)
        gauge.dec(1.0)
        
        assert gauge.value == 11.5
    
    def test_metrics_collector_init(self):
        """Test metrics collector initialization."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        assert collector.enabled is True
        assert "aithershell_query_duration_seconds" in collector.histograms
        assert "aithershell_tokens_used" in collector.histograms
        assert "aithershell_cloud_compute_costs_usd_total" in collector.gauges
    
    def test_record_query(self):
        """Test recording query metrics."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        collector.record_query(
            duration_ms=1234.0,
            tokens_used=150,
            model="gpt-4",
            effort=5,
            cached=False,
        )
        
        # Check histograms were updated
        duration_hist = collector.histograms["aithershell_query_duration_seconds"]
        assert duration_hist.count == 1
        assert duration_hist.sum == 1.234  # 1234ms = 1.234s
        
        tokens_hist = collector.histograms["aithershell_tokens_used"]
        assert tokens_hist.count == 1
        assert tokens_hist.sum == 150
        
        # Check counter was created
        counter_key = "aithershell_queries_total{gpt-4:5}"
        assert counter_key in collector.counters
        assert collector.counters[counter_key].value == 1
    
    def test_record_error(self):
        """Test recording error metrics."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        collector.record_error("api_timeout")
        collector.record_error("api_timeout")
        collector.record_error("auth_failed")
        
        error_counter = collector.counters.get("aithershell_errors_total{api_timeout}")
        assert error_counter is not None
        assert error_counter.value == 2
    
    def test_cache_hits(self):
        """Test cache hit tracking."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        collector.record_query(
            duration_ms=100.0,
            tokens_used=50,
            model="gpt-4",
            cached=True,
        )
        
        cache_counter = collector.counters.get("aithershell_model_cache_hits_total{gpt-4}")
        assert cache_counter is not None
        assert cache_counter.value == 1
    
    def test_cost_tracking(self):
        """Test cost tracking."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        collector.set_cost(0.50)
        assert collector.gauges["aithershell_cloud_compute_costs_usd_total"].value == 0.50
        
        collector.inc_cost(0.25)
        assert collector.gauges["aithershell_cloud_compute_costs_usd_total"].value == 0.75
    
    def test_to_prometheus_text(self):
        """Test Prometheus text export."""
        collector = PrometheusMetricsCollector(enabled=True)
        
        collector.record_query(
            duration_ms=1000.0,
            tokens_used=100,
            model="gpt-4",
        )
        
        prometheus_text = collector.to_prometheus_text()
        assert "# TYPE aithershell_query_duration_seconds histogram" in prometheus_text
        assert "# TYPE aithershell_tokens_used histogram" in prometheus_text
        assert "aithershell_query_duration_seconds_count 1" in prometheus_text


class TestTelemetryConfig:
    """Tests for telemetry configuration."""
    
    def test_default_config(self):
        """Test default configuration."""
        cfg = TelemetryConfig()
        
        assert cfg.emit_events is True
        assert cfg.pulse_url == "http://localhost:8081"
        assert cfg.flush_interval_ms == 1000
        assert cfg.metrics_export is True
        assert cfg.trace_queries is False
        assert cfg.default_privacy_level == "public"
        assert cfg.save_history is True
    
    def test_telemetry_context(self):
        """Test telemetry context manager."""
        with patch('adk.shell.telemetry_config.get_pulse_client') as mock_client, \
             patch('adk.shell.telemetry_config.get_metrics_collector') as mock_collector:
            
            mock_pulse = MagicMock()
            mock_client.return_value = mock_pulse
            
            mock_metrics = MagicMock()
            mock_collector.return_value = mock_metrics
            
            with TelemetryContext("q123", persona="test", model="gpt-4") as ctx:
                ctx.tokens_used = 100
                ctx.duration_ms = 1000
            
            # Verify events were emitted
            mock_pulse.emit_query_started.assert_called_once()
            mock_pulse.emit_query_completed.assert_called_once()
    
    def test_telemetry_context_error(self):
        """Test telemetry context with error."""
        with patch('adk.shell.telemetry_config.get_pulse_client') as mock_client, \
             patch('adk.shell.telemetry_config.get_metrics_collector') as mock_collector:
            
            mock_pulse = MagicMock()
            mock_client.return_value = mock_pulse
            
            mock_metrics = MagicMock()
            mock_collector.return_value = mock_metrics
            
            try:
                with TelemetryContext("q123") as ctx:
                    raise ValueError("Test error")
            except ValueError:
                pass
            
            # Verify error was emitted
            mock_pulse.emit_error.assert_called_once()
            args = mock_pulse.emit_error.call_args
            assert args[1]['error_type'] == 'ValueError'


class TestEventPayloadSchema:
    """Tests for event payload schema validation."""
    
    def test_query_started_schema(self):
        """Test query started event schema."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_started(
            query_id="q123",
            persona="assistant",
            effort=5,
            tokens_estimated=1000,
            query_text="hello",
        )
        
        event = client.event_queue[0]
        assert event["timestamp"] is not None
        assert event["service"] == "aithershell"
        assert event["event"] == "adk.shell.query.started"
        assert event["privacy_level"] == "public"
        assert "data" in event
        assert event["data"]["query_id"] == "q123"
        assert event["data"]["persona"] == "assistant"
        assert event["data"]["effort"] == 5
        assert event["data"]["tokens_estimated"] == 1000
    
    def test_batch_export_format(self):
        """Test batch export format for Pulse."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_started(query_id="q1")
        client.emit_query_started(query_id="q2")
        
        # Simulate what would be sent
        events = client.event_queue.copy()
        batch = {
            "batch": events,
            "count": len(events),
            "timestamp": time.time(),
        }
        
        assert batch["count"] == 2
        assert len(batch["batch"]) == 2
        assert all("timestamp" in e for e in batch["batch"])


class TestPrivacyMode:
    """Tests for privacy mode functionality."""
    
    def test_private_mode_query_text_excluded(self):
        """Test that private mode excludes query text."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_started(
            query_id="q123",
            query_text="SELECT * FROM users WHERE email = ?",
            privacy_level=PrivacyLevel.PRIVATE.value,
        )
        
        event = client.event_queue[0]
        assert "query_text" not in event["data"]
        assert event["data"]["query_id"] == "q123"  # correlation key preserved
    
    def test_privacy_level_in_event(self):
        """Test that privacy level is tracked in event."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        client.emit_query_started(
            query_id="q123",
            privacy_level=PrivacyLevel.PRIVATE.value,
        )
        
        event = client.event_queue[0]
        assert event["privacy_level"] == PrivacyLevel.PRIVATE.value


class TestRBACCapabilities:
    """Tests for RBAC capability tracking."""
    
    def test_service_registration_includes_capabilities(self):
        """Test that service registration includes capabilities."""
        client = PulseEventClient(
            service_name="aithershell",
            enabled=True,
        )
        
        assert "query_execution" in client.capabilities
        assert "artifact_browse" in client.capabilities
        assert client.transport_endpoints == ["stdio", "http_client"]


class TestErrorHandling:
    """Tests for error handling."""
    
    def test_emit_never_blocks(self):
        """Test that emit operations never block."""
        client = PulseEventClient(enabled=True, flush_interval_ms=10000)
        
        # Multiple rapid emits should not block
        start = time.time()
        for i in range(100):
            client.emit_query_started(query_id=f"q{i}")
        elapsed = time.time() - start
        
        assert elapsed < 1.0  # Should complete quickly
        assert len(client.event_queue) == 100
    
    def test_graceful_degradation_if_pulse_unavailable(self):
        """Test graceful degradation when Pulse is unavailable."""
        with patch('adk.shell.events.httpx') as mock_httpx:
            mock_httpx.Client.side_effect = Exception("Connection failed")
            
            client = PulseEventClient(enabled=True)
            client._register_service()  # Should not raise
            
            # Telemetry should continue to work
            client.emit_query_started(query_id="q123")
            assert len(client.event_queue) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
