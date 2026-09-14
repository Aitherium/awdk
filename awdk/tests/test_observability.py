"""Tests for ADK observability stack: Chronicle, Watch, Trace, Pulse, Metrics."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Chronicle
# ─────────────────────────────────────────────────────────────────────────────

class TestJSONFormatter:
    """Test structured JSON log formatting."""

    def test_basic_format(self):
        import logging
        from adk.chronicle import JSONFormatter

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="adk.test", level=logging.INFO, pathname="", lineno=0,
            msg="Hello %s", args=("world",), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "adk.test"
        assert data["message"] == "Hello world"
        assert "timestamp" in data

    def test_extra_fields(self):
        import logging
        from adk.chronicle import JSONFormatter

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="adk.test", level=logging.WARNING, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        record.request_id = "abc123"
        record.agent = "atlas"
        output = formatter.format(record)
        data = json.loads(output)

        assert data["request_id"] == "abc123"
        assert data["agent"] == "atlas"

    def test_exception_included(self):
        import logging
        import sys
        from adk.chronicle import JSONFormatter

        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="adk.test", level=logging.ERROR, pathname="", lineno=0,
            msg="failed", args=(), exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)

        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestChronicleClient:
    """Test Chronicle client fire-and-forget behavior."""

    def test_disabled_when_no_url(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="")
        assert not client.enabled

    def test_enabled_when_url_set(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="http://localhost:8121")
        assert client.enabled

    @pytest.mark.asyncio
    async def test_log_event_disabled(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="")
        result = await client.log_event("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_log_event_success(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="http://localhost:8121")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.log_event(
                "tool_call", tool="search", agent="atlas", request_id="r1"
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_log_event_queues_on_failure(self):
        from adk.chronicle import ChronicleClient
        with tempfile.TemporaryDirectory() as tmpdir:
            client = ChronicleClient(
                chronicle_url="http://localhost:8121",
                data_dir=tmpdir,
            )
            with patch("httpx.AsyncClient", side_effect=Exception("conn refused")):
                result = await client.log_event("test_event", agent="atlas")
            assert result is False
            queue = Path(tmpdir) / "chronicle_queue.jsonl"
            assert queue.exists()
            data = json.loads(queue.read_text().strip())
            assert data["event_type"] == "test_event"

    @pytest.mark.asyncio
    async def test_log_tool_call(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="http://localhost:8121")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.log_tool_call(
                tool="web_search", agent="atlas", latency_ms=42.0,
            )
        assert result is True
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["event_type"] == "tool_call"
        assert payload["tool"] == "web_search"

    @pytest.mark.asyncio
    async def test_log_security_event(self):
        from adk.chronicle import ChronicleClient
        client = ChronicleClient(chronicle_url="http://localhost:8121")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.log_security_event(
                event="capability_denied", agent="demiurge",
                capability="exec", allowed=False, reason="sandbox blocked",
            )
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["level"] == "WARNING"
        assert payload["allowed"] is False

    @pytest.mark.asyncio
    async def test_flush_queue(self):
        from adk.chronicle import ChronicleClient
        with tempfile.TemporaryDirectory() as tmpdir:
            client = ChronicleClient(
                chronicle_url="http://localhost:8121",
                data_dir=tmpdir,
            )
            # Write queue entries
            queue = Path(tmpdir) / "chronicle_queue.jsonl"
            queue.write_text(
                json.dumps({"event_type": "test1"}) + "\n"
                + json.dumps({"event_type": "test2"}) + "\n"
            )

            mock_resp = MagicMock(status_code=200)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                sent = await client.flush_queue()
            assert sent == 2
            assert not queue.exists()


class TestConfigureLogging:
    """Test logging configuration."""

    def test_configure_installs_json_handler(self):
        import logging
        from adk.chronicle import configure_logging, _configured, JSONFormatter

        # Reset state for test
        import adk.chronicle
        adk.chronicle._configured = False

        configure_logging(level="DEBUG", json_output=True)
        root = logging.getLogger()
        assert any(
            isinstance(h.formatter, JSONFormatter)
            for h in root.handlers
        )
        # Reset
        adk.chronicle._configured = False


# ─────────────────────────────────────────────────────────────────────────────
# Trace
# ─────────────────────────────────────────────────────────────────────────────

class TestTrace:
    """Test request ID generation and propagation."""

    def test_new_trace_generates_id(self):
        from adk.trace import new_trace, get_trace_id
        tid = new_trace()
        assert tid.startswith("adk-")
        assert get_trace_id() == tid

    def test_new_trace_custom_id(self):
        from adk.trace import new_trace, get_trace_id
        tid = new_trace("custom-123")
        assert tid == "custom-123"
        assert get_trace_id() == "custom-123"

    def test_set_trace_id(self):
        from adk.trace import set_trace_id, get_trace_id
        set_trace_id("from-header-xyz")
        assert get_trace_id() == "from-header-xyz"

    @pytest.mark.asyncio
    async def test_trace_context_manager(self):
        from adk.trace import new_trace, trace_context, get_trace_id
        outer = new_trace("outer-id")
        assert get_trace_id() == "outer-id"

        async with trace_context("inner-id") as tid:
            assert tid == "inner-id"
            assert get_trace_id() == "inner-id"

        assert get_trace_id() == "outer-id"

    @pytest.mark.asyncio
    async def test_trace_middleware(self):
        from adk.trace import TraceMiddleware, get_trace_id

        captured_trace = []

        async def mock_app(scope, receive, send):
            captured_trace.append(get_trace_id())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = TraceMiddleware(mock_app)
        scope = {"type": "http", "headers": []}

        sent_messages = []
        async def send(msg):
            sent_messages.append(msg)

        await middleware(scope, AsyncMock(), send)

        assert len(captured_trace) == 1
        assert captured_trace[0].startswith("adk-")
        # Check response has X-Request-ID header
        start_msg = sent_messages[0]
        headers = dict(start_msg["headers"])
        assert b"x-request-id" in headers

    @pytest.mark.asyncio
    async def test_trace_middleware_propagates_incoming(self):
        from adk.trace import TraceMiddleware, get_trace_id

        captured_trace = []

        async def mock_app(scope, receive, send):
            captured_trace.append(get_trace_id())
            await send({"type": "http.response.start", "status": 200, "headers": []})

        middleware = TraceMiddleware(mock_app)
        scope = {"type": "http", "headers": [(b"x-request-id", b"incoming-123")]}

        await middleware(scope, AsyncMock(), AsyncMock())

        assert captured_trace[0] == "incoming-123"


# ─────────────────────────────────────────────────────────────────────────────
# Pulse
# ─────────────────────────────────────────────────────────────────────────────

class TestPulseClient:
    """Test Pulse pain signal client."""

    def test_disabled_when_no_url(self):
        from adk.pulse import PulseClient
        client = PulseClient(pulse_url="")
        assert not client.enabled

    def test_enabled_when_url_set(self):
        from adk.pulse import PulseClient
        client = PulseClient(pulse_url="http://localhost:8081")
        assert client.enabled

    @pytest.mark.asyncio
    async def test_send_pain_success(self):
        from adk.pulse import PulseClient, PainCategory
        client = PulseClient(pulse_url="http://localhost:8081")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.send_pain(
                PainCategory.AGENT_LOOP,
                "LoopGuard circuit break",
                agent="atlas",
            )
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["category"] == "agent_loop"

    @pytest.mark.asyncio
    async def test_send_pain_dedup(self):
        from adk.pulse import PulseClient, PainCategory
        client = PulseClient(pulse_url="http://localhost:8081")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            r1 = await client.send_pain(PainCategory.AGENT_LOOP, "same message")
            r2 = await client.send_pain(PainCategory.AGENT_LOOP, "same message")
        assert r1 is True
        assert r2 is False  # Deduplicated

    @pytest.mark.asyncio
    async def test_send_pain_queues_on_failure(self):
        from adk.pulse import PulseClient, PainCategory
        with tempfile.TemporaryDirectory() as tmpdir:
            client = PulseClient(pulse_url="http://localhost:8081", data_dir=tmpdir)
            with patch("httpx.AsyncClient", side_effect=Exception("conn refused")):
                result = await client.send_pain(
                    PainCategory.AGENT_ERROR, "test error", agent="atlas"
                )
            assert result is False
            queue = Path(tmpdir) / "pulse_queue.jsonl"
            assert queue.exists()

    @pytest.mark.asyncio
    async def test_send_loop_break(self):
        from adk.pulse import PulseClient
        client = PulseClient(pulse_url="http://localhost:8081")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.send_loop_break(
                agent="atlas", tool="web_search", total_calls=15,
            )
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["category"] == "agent_loop"
        assert "web_search" in payload["message"]

    @pytest.mark.asyncio
    async def test_send_quota_breach(self):
        from adk.pulse import PulseClient
        client = PulseClient(pulse_url="http://localhost:8081")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.send_quota_breach(
                agent="atlas", limit_type="daily", usage=1500, limit=1000,
            )
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_send_sandbox_violation(self):
        from adk.pulse import PulseClient
        client = PulseClient(pulse_url="http://localhost:8081")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await client.send_sandbox_violation(
                agent="demiurge", tool="shell_command", capability="exec",
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_flush_queue(self):
        from adk.pulse import PulseClient
        with tempfile.TemporaryDirectory() as tmpdir:
            client = PulseClient(pulse_url="http://localhost:8081", data_dir=tmpdir)
            queue = Path(tmpdir) / "pulse_queue.jsonl"
            queue.write_text(
                json.dumps({"category": "test1"}) + "\n"
                + json.dumps({"category": "test2"}) + "\n"
            )

            mock_resp = MagicMock(status_code=200)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            with patch("httpx.AsyncClient", return_value=mock_client):
                sent = await client.flush_queue()
            assert sent == 2


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestMetrics:
    """Test Prometheus metrics collection and export."""

    def test_record_request(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_request(latency_ms=100, status_code=200)
        m.record_request(latency_ms=200, status_code=500)
        assert m._requests_total == 2
        assert m._errors_total == 1

    def test_record_llm_call(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_llm_call(model="llama3.2", latency_ms=150, tokens=500)
        m.record_llm_call(model="llama3.2", latency_ms=200, tokens=300, success=False)
        assert m._llm_calls_total == 2
        assert m._llm_tokens_total == 800
        assert m._llm_errors_total == 1
        assert m._llm_calls_by_model["llama3.2"] == 2

    def test_record_tool_call(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_tool_call(tool="web_search", latency_ms=42)
        m.record_tool_call(tool="web_search", latency_ms=50, success=False)
        assert m._tool_calls_total == 2
        assert m._tool_calls_by_name["web_search"] == 2
        assert m._tool_errors_by_name["web_search"] == 1

    def test_record_agent_spawn(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_agent_spawn(agent_type="atlas")
        m.record_agent_spawn(agent_type="demiurge")
        m.record_agent_spawn(agent_type="atlas")
        assert m._agent_spawns_total == 3
        assert m._agent_spawns_by_type["atlas"] == 2

    def test_record_security_events(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_sandbox_block()
        m.record_loop_guard_break()
        m.record_quota_breach()
        assert m._sandbox_blocks_total == 1
        assert m._loop_guard_breaks_total == 1
        assert m._quota_breaches_total == 1

    def test_export_prometheus_format(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_request(latency_ms=100, status_code=200)
        m.record_llm_call(model="llama3.2", tokens=500, latency_ms=150)
        m.record_tool_call(tool="search", latency_ms=42)
        m.record_agent_spawn(agent_type="atlas")

        output = m.export()

        assert "adk_requests_total 1" in output
        assert "adk_llm_calls_total 1" in output
        assert "adk_llm_tokens_total 500" in output
        assert "adk_tool_calls_total 1" in output
        assert "adk_agent_spawns_total 1" in output
        assert "# TYPE adk_requests_total counter" in output
        assert "# HELP" in output

    def test_export_histogram_buckets(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_request(latency_ms=42)
        m.record_request(latency_ms=150)
        m.record_request(latency_ms=3000)

        output = m.export()
        assert 'adk_request_latency_ms_bucket{le="50"} 1' in output
        assert 'adk_request_latency_ms_bucket{le="250"} 2' in output
        assert 'adk_request_latency_ms_bucket{le="+Inf"} 3' in output
        assert "adk_request_latency_ms_count 3" in output

    def test_export_model_breakdown(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.record_llm_call(model="llama3.2", tokens=100)
        m.record_llm_call(model="gpt-4o", tokens=200)

        output = m.export()
        assert 'adk_llm_calls_by_model{model="llama3.2"} 1' in output
        assert 'adk_llm_calls_by_model{model="gpt-4o"} 1' in output
        assert 'adk_llm_tokens_by_model{model="llama3.2"} 100' in output

    def test_gauges(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        m.set_active_sessions(5)
        m.set_active_agents(3)
        output = m.export()
        assert "adk_active_sessions 5" in output
        assert "adk_active_agents 3" in output

    def test_uptime_gauge(self):
        from adk.metrics import MetricsCollector
        m = MetricsCollector()
        output = m.export()
        assert "adk_uptime_seconds" in output

    def test_singleton(self):
        from adk.metrics import get_metrics
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2


# ─────────────────────────────────────────────────────────────────────────────
# Watch
# ─────────────────────────────────────────────────────────────────────────────

class TestWatchReporter:
    """Test Watch health reporter."""

    def test_disabled_when_no_url(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="")
        assert not reporter.enabled

    def test_enabled_when_url_set(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")
        assert reporter.enabled

    def test_record_request(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")
        reporter.record_request(latency_ms=100)
        reporter.record_request(latency_ms=200, error=True)
        assert reporter._request_count == 2
        assert reporter._error_count == 1
        assert reporter._latency_samples == 2

    @pytest.mark.asyncio
    async def test_send_heartbeat_success(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await reporter.send_heartbeat()
        assert result is True
        payload = mock_client.post.call_args[1]["json"]
        assert payload["service"] == "adk"
        assert payload["status"] == "healthy"
        assert "uptime_seconds" in payload

    @pytest.mark.asyncio
    async def test_send_heartbeat_circuit_breaker(self):
        from adk.watch import WatchReporter, _CIRCUIT_BREAK_THRESHOLD
        reporter = WatchReporter(watch_url="http://localhost:8082")

        with patch("httpx.AsyncClient", side_effect=Exception("conn refused")):
            for _ in range(_CIRCUIT_BREAK_THRESHOLD):
                await reporter.send_heartbeat()

        assert reporter._circuit_open is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_recovers(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")
        reporter._circuit_open = True
        reporter._consecutive_failures = 10

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await reporter.send_heartbeat()
        assert result is True
        assert reporter._circuit_open is False
        assert reporter._consecutive_failures == 0

    def test_register_collector(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")
        reporter.register_collector(lambda: {"agent_count": 5})
        assert len(reporter._collectors) == 1

    @pytest.mark.asyncio
    async def test_build_snapshot_with_collector(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082")
        reporter.register_collector(lambda: {"agent_count": 3, "agents": ["a", "b", "c"]})

        snapshot = await reporter._build_snapshot()
        assert snapshot.agent_count == 3
        assert len(snapshot.agents) == 3

    @pytest.mark.asyncio
    async def test_start_stop(self):
        from adk.watch import WatchReporter
        reporter = WatchReporter(watch_url="http://localhost:8082", interval=1)

        with patch("httpx.AsyncClient", side_effect=Exception("skip")):
            await reporter.start()
            assert reporter._running is True
            await asyncio.sleep(0.05)
            await reporter.stop()
            assert reporter._running is False


# ─────────────────────────────────────────────────────────────────────────────
# Integration: Wiring checks
# ─────────────────────────────────────────────────────────────────────────────

class TestObservabilityWiring:
    """Verify observability is wired into existing components."""

    def test_config_has_observability_fields(self):
        from adk.config import Config
        c = Config()
        assert hasattr(c, "chronicle_url")
        assert hasattr(c, "watch_url")
        assert hasattr(c, "pulse_url")
        assert hasattr(c, "json_logging")

    def test_init_exports_observability(self):
        import adk
        # These should not raise
        _ = adk.ChronicleClient
        _ = adk.WatchReporter
        _ = adk.MetricsCollector
        _ = adk.PulseClient

    def test_server_has_metrics_import(self):
        """Verify server.py imports metrics."""
        import adk.server
        # The module should import without error

    def test_agent_has_metrics_import(self):
        """Verify agent.py has metrics wiring."""
        import adk.agent
        assert hasattr(adk.agent, 'get_metrics')

    def test_forge_has_metrics_import(self):
        """Verify forge.py has metrics wiring."""
        import adk.forge
        assert hasattr(adk.forge, 'get_metrics')
