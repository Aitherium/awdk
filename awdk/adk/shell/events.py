"""
AitherShell Pulse Event System
===============================

Manages Pulse event emission for telemetry tracking:
- Service registration and heartbeat
- Query lifecycle events (started, completed, errors)
- Plugin execution tracking
- Privacy mode support

Thread-safe, non-blocking event emission with graceful degradation.
"""

import json
import sys
import threading
import time
import uuid
import socket
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from dataclasses import dataclass, asdict
from enum import Enum
import logging

# Configure logging to stderr to avoid stdout pollution
logger = logging.getLogger("adk.shell.telemetry")
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s [TELEMETRY] %(levelname)s: %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.WARNING)  # Only show warnings and errors by default


class EventSeverity(Enum):
    """Event severity levels."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class PrivacyLevel(Enum):
    """Privacy levels for event emission."""
    PUBLIC = "public"
    PRIVATE = "private"
    REDACTED = "redacted"


@dataclass
class QueryContext:
    """Context for query execution tracking."""
    query_id: str
    persona: Optional[str] = None
    effort: Optional[int] = None
    tokens_estimated: Optional[int] = None
    model: Optional[str] = None
    privacy_level: str = PrivacyLevel.PUBLIC.value
    start_time: Optional[float] = None
    query_text: Optional[str] = None  # Only stored if privacy_level != PRIVATE


class PulseEventClient:
    """
    Non-blocking Pulse event client.
    
    Features:
    - Batched event emission (queues events and flushes periodically)
    - Service registration and heartbeat
    - Privacy mode support (filters query_text based on privacy_level)
    - Graceful degradation (never blocks query execution)
    - Thread-safe async emission
    """

    def __init__(
        self,
        service_name: str = "aithershell",
        pulse_url: str = "http://localhost:8081",
        flush_interval_ms: int = 1000,
        max_batch_size: int = 100,
        enabled: bool = True,
    ):
        """Initialize Pulse event client.
        
        Args:
            service_name: Name of the service ("aithershell")
            pulse_url: Base URL for Pulse endpoint
            flush_interval_ms: How often to flush batched events
            max_batch_size: Maximum events per batch before auto-flush
            enabled: Whether event emission is enabled
        """
        self.service_name = service_name
        self.pulse_url = pulse_url.rstrip("/")
        self.flush_interval_ms = flush_interval_ms
        self.max_batch_size = max_batch_size
        self.enabled = enabled
        
        # Internal state
        self.event_queue: List[Dict[str, Any]] = []
        self.queue_lock = threading.Lock()
        self.registered = False
        self.consecutive_failures = 0
        self.last_flush_time = time.time()
        self.daemon_thread: Optional[threading.Thread] = None
        self.running = False
        
        # Service metadata
        self.hostname = self._get_hostname()
        self.client_version = "1.0.0"
        self.transport_endpoints = ["stdio", "http_client"]
        self.capabilities = ["query_execution", "artifact_browse"]
        
        # Metrics (updated via health reports)
        self.metrics = {
            "queries_completed": 0,
            "errors_total": 0,
            "local_cache_hits": 0,
            "uptime_seconds": 0,
        }
        self.start_time = time.time()

    def _get_hostname(self) -> str:
        """Get hostname for client identification."""
        try:
            return socket.gethostname()
        except Exception:
            return "unknown"

    def start(self) -> None:
        """Start background event flush thread and register service."""
        if not self.enabled:
            return
        
        if self.running:
            return
        
        self.running = True
        
        # Register service
        self._register_service()
        
        # Start background flush thread
        self.daemon_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self.daemon_thread.start()
        
        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()

    def stop(self) -> None:
        """Stop event collection and flush remaining events."""
        if not self.enabled or not self.running:
            return
        
        self.running = False
        
        # Flush any remaining events
        self._flush()
        
        # Wait for daemon thread to finish
        if self.daemon_thread and self.daemon_thread.is_alive():
            self.daemon_thread.join(timeout=2.0)

    def emit(
        self,
        event_name: str,
        data: Dict[str, Any],
        privacy_level: str = PrivacyLevel.PUBLIC.value,
    ) -> None:
        """Emit an event to Pulse.
        
        Args:
            event_name: Event name (e.g., "adk.shell.query.started")
            data: Event data payload
            privacy_level: Privacy level for this event
        """
        if not self.enabled:
            return
        
        try:
            # Filter private data if needed
            filtered_data = self._apply_privacy_filter(data, privacy_level)
            
            # Create event object
            event = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "service": self.service_name,
                "event": event_name,
                "privacy_level": privacy_level,
                "data": filtered_data,
            }
            
            # Queue for batch emission
            with self.queue_lock:
                self.event_queue.append(event)
                
                # Auto-flush if batch is full
                if len(self.event_queue) >= self.max_batch_size:
                    self._flush_unsafe()
        except Exception as e:
            logger.warning(f"Failed to emit event {event_name}: {e}")

    def _apply_privacy_filter(
        self, data: Dict[str, Any], privacy_level: str
    ) -> Dict[str, Any]:
        """Filter sensitive data based on privacy level.
        
        Args:
            data: Original event data
            privacy_level: Privacy level (public, private, redacted)
            
        Returns:
            Filtered event data
        """
        if privacy_level == PrivacyLevel.PRIVATE.value:
            # Remove query text but keep query_id as correlation key
            filtered = data.copy()
            filtered.pop("query_text", None)
            return filtered
        elif privacy_level == PrivacyLevel.REDACTED.value:
            # Hash sensitive fields
            filtered = data.copy()
            if "query_text" in filtered:
                filtered["query_text"] = f"[REDACTED:{hash(filtered['query_text'])}]"
            return filtered
        else:
            return data

    def emit_query_started(
        self,
        query_id: str,
        persona: Optional[str] = None,
        effort: Optional[int] = None,
        tokens_estimated: Optional[int] = None,
        query_text: Optional[str] = None,
        privacy_level: str = PrivacyLevel.PUBLIC.value,
    ) -> None:
        """Emit query started event.
        
        Args:
            query_id: Unique query identifier
            persona: Persona used for query
            effort: Effort level (1-10)
            tokens_estimated: Estimated token usage
            query_text: User query text (may be filtered)
            privacy_level: Privacy level (filters query_text if PRIVATE)
        """
        self.emit(
            "adk.shell.query.started",
            {
                "query_id": query_id,
                "persona": persona,
                "effort": effort,
                "tokens_estimated": tokens_estimated,
                "query_text": query_text,
            },
            privacy_level=privacy_level,
        )

    def emit_query_completed(
        self,
        query_id: str,
        duration_ms: float,
        tokens_used: int,
        model: str,
        cached: bool = False,
    ) -> None:
        """Emit query completed event.
        
        Args:
            query_id: Unique query identifier
            duration_ms: Query execution duration in milliseconds
            tokens_used: Actual tokens consumed
            model: Model used for query
            cached: Whether response was from cache
        """
        self.emit(
            "adk.shell.query.completed",
            {
                "query_id": query_id,
                "duration_ms": duration_ms,
                "tokens_used": tokens_used,
                "model": model,
                "cached": cached,
            },
        )

    def emit_error(
        self,
        error_type: str,
        severity: str = EventSeverity.ERROR.value,
        retry_count: int = 0,
        message: Optional[str] = None,
        query_id: Optional[str] = None,
    ) -> None:
        """Emit error event.
        
        Args:
            error_type: Type of error (e.g., "api_timeout", "auth_failed")
            severity: Severity level (info, warning, error, critical)
            retry_count: Number of retries attempted
            message: Error message (optional, may be filtered)
            query_id: Associated query ID (optional)
        """
        self.emit(
            "adk.shell.error",
            {
                "error_type": error_type,
                "severity": severity,
                "retry_count": retry_count,
                "message": message,
                "query_id": query_id,
            },
        )

    def emit_plugin_executed(
        self,
        plugin_name: str,
        duration_ms: float,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        """Emit plugin execution event.
        
        Args:
            plugin_name: Name of the plugin
            duration_ms: Execution duration in milliseconds
            success: Whether plugin executed successfully
            error_message: Error message if failed (optional)
        """
        self.emit(
            "adk.shell.plugin.executed",
            {
                "plugin_name": plugin_name,
                "duration_ms": duration_ms,
                "success": success,
                "error_message": error_message,
            },
        )

    def _register_service(self) -> None:
        """Register service with Pulse."""
        try:
            import httpx
            
            payload = {
                "service_name": self.service_name,
                "port": 0,  # Client port (not applicable)
                "container_name": f"client:{self.hostname}",
                "transport_endpoints": self.transport_endpoints,
                "environment": "local",
                "client_version": self.client_version,
                "capabilities": self.capabilities,
            }
            
            # Non-blocking POST
            def post_register():
                try:
                    with httpx.Client(timeout=2.0) as client:
                        response = client.post(
                            f"{self.pulse_url}/services/register",
                            json=payload,
                        )
                        if response.status_code in (200, 201):
                            self.registered = True
                            logger.debug(f"Service registered: {self.service_name}")
                        else:
                            logger.warning(
                                f"Service registration failed: {response.status_code}"
                            )
                except Exception as e:
                    logger.debug(f"Service registration error: {e}")
            
            thread = threading.Thread(target=post_register, daemon=True)
            thread.start()
        except ImportError:
            logger.debug("httpx not available for service registration")

    def _heartbeat_loop(self) -> None:
        """Periodic heartbeat to Pulse (every 30 seconds)."""
        while self.running:
            time.sleep(30)
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        """Send health report to Pulse."""
        if not self.registered or not self.enabled:
            return
        
        try:
            import httpx
            
            # Update uptime
            self.metrics["uptime_seconds"] = int(time.time() - self.start_time)
            
            payload = {
                "service_name": self.service_name,
                "status": "healthy" if self.consecutive_failures == 0 else "degraded",
                "consecutive_failures": self.consecutive_failures,
                "last_query_duration_ms": 0,  # Updated by metrics system
                "metrics": self.metrics,
            }
            
            def post_heartbeat():
                try:
                    with httpx.Client(timeout=2.0) as client:
                        response = client.post(
                            f"{self.pulse_url}/health/report",
                            json=payload,
                        )
                        if response.status_code not in (200, 201):
                            self.consecutive_failures += 1
                        else:
                            self.consecutive_failures = 0
                except Exception as e:
                    self.consecutive_failures += 1
                    logger.debug(f"Heartbeat error: {e}")
            
            thread = threading.Thread(target=post_heartbeat, daemon=True)
            thread.start()
        except ImportError:
            pass

    def _flush_loop(self) -> None:
        """Background thread that periodically flushes events."""
        while self.running:
            time.sleep(self.flush_interval_ms / 1000.0)
            self._flush()

    def _flush(self) -> None:
        """Flush queued events to Pulse."""
        with self.queue_lock:
            self._flush_unsafe()

    def _flush_unsafe(self) -> None:
        """Flush events without lock (must be called within lock)."""
        if not self.event_queue or not self.enabled:
            return
        
        events_to_send = self.event_queue.copy()
        self.event_queue.clear()
        
        def post_events():
            try:
                import httpx
                
                payload = {
                    "batch": events_to_send,
                    "count": len(events_to_send),
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
                
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(
                        f"{self.pulse_url}/metrics",
                        json=payload,
                    )
                    if response.status_code not in (200, 201, 202):
                        # Re-queue failed events
                        with self.queue_lock:
                            self.event_queue.extend(events_to_send)
                        logger.debug(f"Event flush failed: {response.status_code}")
                    else:
                        logger.debug(f"Flushed {len(events_to_send)} events")
            except Exception as e:
                # Re-queue failed events
                with self.queue_lock:
                    self.event_queue.extend(events_to_send)
                logger.debug(f"Event flush error: {e}")
        
        # Non-blocking send
        thread = threading.Thread(target=post_events, daemon=True)
        thread.start()

    def update_metrics(self, **kwargs) -> None:
        """Update internal metrics (queries_completed, errors_total, etc.)."""
        with self.queue_lock:
            for key, value in kwargs.items():
                if key in self.metrics:
                    self.metrics[key] = value


# Global instance
_global_client: Optional[PulseEventClient] = None
_client_lock = threading.Lock()


def get_pulse_client(
    service_name: str = "aithershell",
    pulse_url: str = "http://localhost:8081",
    flush_interval_ms: int = 1000,
    enabled: bool = True,
) -> PulseEventClient:
    """Get or create global Pulse event client.
    
    Args:
        service_name: Service name
        pulse_url: Pulse URL
        flush_interval_ms: Flush interval
        enabled: Whether to enable events
        
    Returns:
        Global PulseEventClient instance
    """
    global _global_client
    
    if _global_client is None:
        with _client_lock:
            if _global_client is None:
                _global_client = PulseEventClient(
                    service_name=service_name,
                    pulse_url=pulse_url,
                    flush_interval_ms=flush_interval_ms,
                    enabled=enabled,
                )
    
    return _global_client


def create_query_id() -> str:
    """Create a unique query identifier."""
    return str(uuid.uuid4())[:8]


def query_context_from_request(
    query_id: Optional[str] = None,
    persona: Optional[str] = None,
    effort: Optional[int] = None,
    tokens_estimated: Optional[int] = None,
    query_text: Optional[str] = None,
    privacy_level: str = PrivacyLevel.PUBLIC.value,
) -> QueryContext:
    """Create a QueryContext from request parameters.
    
    Args:
        query_id: Query ID (generated if not provided)
        persona: Persona name
        effort: Effort level
        tokens_estimated: Estimated token count
        query_text: Query text
        privacy_level: Privacy level
        
    Returns:
        QueryContext instance
    """
    return QueryContext(
        query_id=query_id or create_query_id(),
        persona=persona,
        effort=effort,
        tokens_estimated=tokens_estimated,
        query_text=query_text,
        privacy_level=privacy_level,
        start_time=time.time(),
    )
