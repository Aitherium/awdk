"""
AitherShell Telemetry Configuration
====================================

Manages observability settings for:
- Event emission
- Metrics collection
- Privacy modes
- Pulse integration
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from pathlib import Path
import os
import yaml


@dataclass
class TelemetryConfig:
    """Telemetry configuration."""
    
    # Event emission
    emit_events: bool = True
    
    # Pulse integration
    pulse_url: str = "http://localhost:8081"
    flush_interval_ms: int = 1000
    max_batch_size: int = 100
    
    # Metrics collection
    metrics_export: bool = True
    
    # Query tracing
    trace_queries: bool = False
    
    # Privacy defaults
    default_privacy_level: str = "public"  # public, private, redacted
    
    # Query history
    save_history: bool = True
    history_file: Optional[str] = None
    
    def __post_init__(self):
        """Initialize defaults."""
        if self.history_file is None:
            from adk.shell.config import CONFIG_DIR
            self.history_file = str(CONFIG_DIR / "history")


def load_telemetry_config() -> TelemetryConfig:
    """Load telemetry configuration from multiple sources.
    
    Resolution order (later overrides earlier):
    1. Built-in defaults
    2. ~/.aither/config.yaml (observability section)
    3. .aither.yaml (observability section)
    4. Environment variables (AITHER_TELEMETRY_*)
    
    Returns:
        TelemetryConfig instance
    """
    cfg = TelemetryConfig()
    
    # Layer 1: ~/.aither/config.yaml
    from adk.shell.config import CONFIG_FILE
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                data = yaml.safe_load(f) or {}
                if "observability" in data:
                    _apply_dict(cfg, data["observability"])
        except Exception:
            pass
    
    # Layer 2: .aither.yaml (project-local)
    project_config = Path(".aither.yaml")
    if project_config.exists():
        try:
            with open(project_config, "r") as f:
                data = yaml.safe_load(f) or {}
                if "observability" in data:
                    _apply_dict(cfg, data["observability"])
        except Exception:
            pass
    
    # Layer 3: Environment variables
    env_map = {
        "AITHER_TELEMETRY_EMIT_EVENTS": ("emit_events", "bool"),
        "AITHER_TELEMETRY_PULSE_URL": ("pulse_url", "str"),
        "AITHER_TELEMETRY_FLUSH_MS": ("flush_interval_ms", "int"),
        "AITHER_TELEMETRY_BATCH_SIZE": ("max_batch_size", "int"),
        "AITHER_TELEMETRY_METRICS": ("metrics_export", "bool"),
        "AITHER_TELEMETRY_TRACE": ("trace_queries", "bool"),
        "AITHER_TELEMETRY_PRIVACY": ("default_privacy_level", "str"),
        "AITHER_TELEMETRY_HISTORY": ("save_history", "bool"),
    }
    
    for env_key, (attr, type_) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if type_ == "bool":
                val = val.lower() in ("1", "true", "yes")
            elif type_ == "int":
                try:
                    val = int(val)
                except ValueError:
                    continue
            setattr(cfg, attr, val)
    
    return cfg


def _apply_dict(cfg: TelemetryConfig, data: dict):
    """Apply a dict to config, ignoring unknown keys."""
    for key, val in data.items():
        if hasattr(cfg, key) and val is not None:
            setattr(cfg, key, val)


def get_default_config_yaml() -> str:
    """Get default YAML configuration template."""
    return """# AitherShell Telemetry Configuration
# =====================================

observability:
  # Emit events to Pulse (startup, heartbeat, queries)
  emit_events: true
  
  # Pulse endpoint URL
  pulse_url: "http://localhost:8081"
  
  # Event flush interval (milliseconds)
  flush_interval_ms: 1000
  
  # Maximum events per batch before auto-flush
  max_batch_size: 100
  
  # Collect and export Prometheus metrics
  metrics_export: true
  
  # Enable detailed query tracing (verbose logging)
  trace_queries: false
  
  # Default privacy level for queries: public, private, redacted
  # - public: Include query text in events
  # - private: Exclude query text (keep query_id for correlation)
  # - redacted: Hash sensitive fields
  default_privacy_level: "public"
  
  # Save query history to file (can be disabled for privacy)
  save_history: true
"""


def merge_telemetry_config(user_cfg: Dict[str, Any]) -> TelemetryConfig:
    """Merge user configuration with defaults.
    
    Args:
        user_cfg: User-provided configuration dict
        
    Returns:
        Merged TelemetryConfig
    """
    cfg = TelemetryConfig()
    _apply_dict(cfg, user_cfg)
    return cfg


class TelemetryContext:
    """Context manager for telemetry tracking.
    
    Usage:
        with TelemetryContext(query_id, model="gpt-4") as ctx:
            result = execute_query()
            ctx.duration_ms = 1234
            ctx.tokens_used = 150
    """
    
    def __init__(
        self,
        query_id: str,
        persona: Optional[str] = None,
        effort: Optional[int] = None,
        model: Optional[str] = None,
        privacy_level: str = "public",
    ):
        """Initialize telemetry context.
        
        Args:
            query_id: Unique query ID
            persona: Persona name
            effort: Effort level
            model: Model name
            privacy_level: Privacy level
        """
        self.query_id = query_id
        self.persona = persona
        self.effort = effort
        self.model = model
        self.privacy_level = privacy_level
        
        # Metrics
        self.duration_ms: Optional[float] = None
        self.tokens_used: Optional[int] = None
        self.cached: bool = False
        self.error: Optional[str] = None
        
        # Timing
        import time
        self.start_time = time.time()
    
    def __enter__(self):
        """Enter context."""
        from adk.shell.events import get_pulse_client
        
        client = get_pulse_client()
        client.emit_query_started(
            self.query_id,
            persona=self.persona,
            effort=self.effort,
            model=self.model,
            privacy_level=self.privacy_level,
        )
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context and record metrics."""
        import time
        from adk.shell.events import get_pulse_client
        from adk.shell.metrics import get_metrics_collector
        
        if exc_type is None:
            # Query completed successfully
            self.duration_ms = (time.time() - self.start_time) * 1000
            
            client = get_pulse_client()
            if self.tokens_used is not None and self.model:
                client.emit_query_completed(
                    self.query_id,
                    duration_ms=self.duration_ms,
                    tokens_used=self.tokens_used,
                    model=self.model,
                    cached=self.cached,
                )
            
            # Update metrics
            collector = get_metrics_collector()
            if self.duration_ms is not None and self.tokens_used is not None:
                collector.record_query(
                    duration_ms=self.duration_ms,
                    tokens_used=self.tokens_used,
                    model=self.model,
                    effort=self.effort,
                    cached=self.cached,
                )
        else:
            # Query failed
            client = get_pulse_client()
            error_type = exc_type.__name__ if exc_type else "unknown"
            client.emit_error(
                error_type=error_type,
                severity="error",
                message=str(exc_val),
                query_id=self.query_id,
            )
            
            # Update error metrics
            collector = get_metrics_collector()
            collector.record_error(error_type=error_type)
        
        return False  # Don't suppress exceptions
