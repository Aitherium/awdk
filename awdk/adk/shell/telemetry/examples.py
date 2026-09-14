"""
AitherShell Telemetry Integration Examples
============================================

This file demonstrates practical integration patterns for AitherShell telemetry.
"""

import asyncio
import time
from typing import Optional
from adk.shell.events import get_pulse_client, create_query_id, PrivacyLevel
from adk.shell.metrics import get_metrics_collector
from adk.shell.telemetry_config import TelemetryContext, load_telemetry_config


# ============================================================================
# Example 1: Basic CLI Integration
# ============================================================================

async def cli_main_with_telemetry():
    """Example of integrating telemetry into CLI main entry point."""
    
    # Load configuration
    config = load_telemetry_config()
    
    # Initialize telemetry
    pulse = get_pulse_client(
        pulse_url=config.pulse_url,
        enabled=config.emit_events,
    )
    pulse.start()
    
    metrics = get_metrics_collector(enabled=config.metrics_export)
    
    try:
        # ... Run CLI ...
        await process_user_input(pulse, metrics, config)
    finally:
        # Always flush remaining events before exit
        pulse.stop()


async def process_user_input(pulse, metrics, config):
    """Process user input with telemetry."""
    while True:
        query = input("aither> ").strip()
        if not query:
            continue
        
        if query == "exit":
            break
        
        # Determine privacy level
        privacy_level = PrivacyLevel.PRIVATE.value if query.startswith("--private ") else PrivacyLevel.PUBLIC.value
        if privacy_level == PrivacyLevel.PRIVATE.value:
            query = query[10:]  # Remove --private prefix
        
        # Execute with telemetry
        await execute_query_with_telemetry(
            query,
            pulse=pulse,
            metrics=metrics,
            privacy_level=privacy_level,
        )


# ============================================================================
# Example 2: Query Execution with TelemetryContext
# ============================================================================

async def execute_query_with_telemetry(
    query_text: str,
    pulse,
    metrics,
    model: Optional[str] = None,
    effort: Optional[int] = None,
    privacy_level: str = PrivacyLevel.PUBLIC.value,
) -> Optional[str]:
    """Execute a query with automatic telemetry tracking.
    
    This is the recommended pattern:
    - Creates unique query ID
    - Tracks start time automatically
    - Records tokens and duration
    - Handles errors automatically
    - Emits metrics to Prometheus
    """
    
    query_id = create_query_id()
    
    with TelemetryContext(
        query_id=query_id,
        model=model or "gpt-4",
        effort=effort or 5,
        privacy_level=privacy_level,
    ) as ctx:
        try:
            # Simulate query execution
            result = await simulate_query_api(query_text, model, effort)
            
            # Record metrics
            ctx.tokens_used = result["tokens_used"]
            ctx.duration_ms = result["duration_ms"]
            ctx.cached = result.get("cached", False)
            
            # Update metrics collector
            metrics.record_query(
                duration_ms=result["duration_ms"],
                tokens_used=result["tokens_used"],
                model=model or "gpt-4",
                effort=effort,
                cached=result.get("cached", False),
            )
            
            return result["response"]
        
        except Exception as e:
            # Error will be automatically emitted by context
            metrics.record_error(error_type=type(e).__name__)
            raise


# ============================================================================
# Example 3: Manual Event Emission (for custom scenarios)
# ============================================================================

async def execute_complex_query(query_text: str, pulse, metrics):
    """Example of manual event emission for complex scenarios."""
    
    query_id = create_query_id()
    start_time = time.time()
    
    # Emit query started
    pulse.emit_query_started(
        query_id=query_id,
        persona="assistant",
        effort=7,
        tokens_estimated=2000,
        query_text=query_text,
        privacy_level=PrivacyLevel.PUBLIC.value,
    )
    
    try:
        # Execute query in phases
        tokens_used = 0
        
        # Phase 1: Analyze
        result1 = await analyze_query_phase(query_text)
        tokens_used += result1["tokens"]
        
        # Emit intermediate progress
        pulse.emit(
            "adk.shell.query.progress",
            {
                "query_id": query_id,
                "phase": "analyzed",
                "tokens_used": tokens_used,
            }
        )
        
        # Phase 2: Execute
        result2 = await execute_query_phase(query_text, result1["plan"])
        tokens_used += result2["tokens"]
        
        # Emit completion
        duration_ms = (time.time() - start_time) * 1000
        pulse.emit_query_completed(
            query_id=query_id,
            duration_ms=duration_ms,
            tokens_used=tokens_used,
            model="gpt-4",
            cached=False,
        )
        
        # Update metrics
        metrics.record_query(
            duration_ms=duration_ms,
            tokens_used=tokens_used,
            model="gpt-4",
        )
        
        return result2["response"]
    
    except Exception as e:
        # Emit error with retry count
        pulse.emit_error(
            error_type=type(e).__name__,
            severity="error",
            retry_count=0,
            message=str(e),
            query_id=query_id,
        )
        raise


# ============================================================================
# Example 4: Plugin Execution Tracking
# ============================================================================

async def execute_plugin_with_telemetry(
    plugin_name: str,
    pulse,
    metrics,
    *args,
    **kwargs
) -> Optional[str]:
    """Execute a plugin with telemetry tracking."""
    
    start_time = time.time()
    
    try:
        # Load and execute plugin
        plugin = load_plugin(plugin_name)
        result = await plugin.execute(*args, **kwargs)
        
        # Emit plugin execution event
        duration_ms = (time.time() - start_time) * 1000
        pulse.emit_plugin_executed(
            plugin_name=plugin_name,
            duration_ms=duration_ms,
            success=True,
        )
        
        return result
    
    except Exception as e:
        # Emit plugin error
        duration_ms = (time.time() - start_time) * 1000
        pulse.emit_plugin_executed(
            plugin_name=plugin_name,
            duration_ms=duration_ms,
            success=False,
            error_message=str(e),
        )
        
        # Also emit general error
        pulse.emit_error(
            error_type="plugin_execution_failed",
            severity="error",
            message=f"Plugin {plugin_name} failed: {e}",
        )
        raise


# ============================================================================
# Example 5: Cost Tracking
# ============================================================================

async def execute_query_with_cost_tracking(query_text: str, pulse, metrics):
    """Track cloud compute costs for queries."""
    
    model_costs = {
        "gpt-4": {"per_1k_tokens": 0.03},
        "gpt-3.5-turbo": {"per_1k_tokens": 0.002},
        "ollama:neural": {"per_1k_tokens": 0.0},  # Local
    }
    
    model = "gpt-4"
    
    with TelemetryContext(
        create_query_id(),
        model=model,
    ) as ctx:
        result = await simulate_query_api(query_text, model, 5)
        
        ctx.tokens_used = result["tokens_used"]
        ctx.duration_ms = result["duration_ms"]
        
        # Calculate cost
        tokens_in_k = result["tokens_used"] / 1000.0
        cost_per_token = model_costs[model]["per_1k_tokens"] / 1000.0
        cost_usd = tokens_in_k * model_costs[model]["per_1k_tokens"]
        
        # Update cost gauge
        current_cost = metrics.gauges["aithershell_cloud_compute_costs_usd_total"].value
        metrics.set_cost(current_cost + cost_usd)
        
        # Emit cost event
        pulse.emit(
            "adk.shell.query.cost",
            {
                "query_id": ctx.query_id,
                "model": model,
                "tokens_used": result["tokens_used"],
                "cost_usd": cost_usd,
            }
        )
        
        return result["response"]


# ============================================================================
# Example 6: Privacy Mode in Action
# ============================================================================

async def process_query_with_privacy_check(
    query_text: str,
    pulse,
    metrics,
):
    """Process query with privacy awareness."""
    
    # Detect if query contains sensitive patterns
    sensitive_keywords = ["password", "secret", "api_key", "token", "credit"]
    is_sensitive = any(kw in query_text.lower() for kw in sensitive_keywords)
    
    privacy_level = PrivacyLevel.PRIVATE.value if is_sensitive else PrivacyLevel.PUBLIC.value
    
    with TelemetryContext(
        create_query_id(),
        privacy_level=privacy_level,
    ) as ctx:
        if is_sensitive:
            print("[Private Mode] Query text will not be stored in history")
        
        result = await simulate_query_api(query_text, "gpt-4", 5)
        ctx.tokens_used = result["tokens_used"]
        ctx.duration_ms = result["duration_ms"]
        
        return result["response"]


# ============================================================================
# Example 7: Metrics Export Endpoint (for daemon mode)
# ============================================================================

async def setup_metrics_endpoint(app):
    """Setup Prometheus /metrics endpoint for daemon mode."""
    
    from fastapi import FastAPI
    from adk.shell.metrics import get_metrics_collector
    
    @app.get("/metrics")
    async def metrics_endpoint():
        """Export metrics in Prometheus text format."""
        collector = get_metrics_collector()
        return collector.to_prometheus_text()
    
    @app.get("/metrics.json")
    async def metrics_json_endpoint():
        """Export metrics as JSON."""
        collector = get_metrics_collector()
        return collector.to_json()


# ============================================================================
# Stub Functions (for demonstration)
# ============================================================================

async def simulate_query_api(query_text: str, model: str, effort: int) -> dict:
    """Simulate API query execution."""
    # Simulate network delay
    await asyncio.sleep(0.1)
    
    # Estimate tokens (rough)
    token_estimate = len(query_text.split()) * 1.3
    
    return {
        "response": f"Response to: {query_text}",
        "tokens_used": int(token_estimate) + 20,
        "duration_ms": 150 + (effort * 10),
        "cached": False,
    }


async def analyze_query_phase(query_text: str) -> dict:
    """Simulate query analysis phase."""
    await asyncio.sleep(0.05)
    return {
        "tokens": 50,
        "plan": "analyzed",
    }


async def execute_query_phase(query_text: str, plan: str) -> dict:
    """Simulate query execution phase."""
    await asyncio.sleep(0.05)
    return {
        "response": "Result",
        "tokens": 100,
    }


def load_plugin(name: str):
    """Stub for plugin loading."""
    class Plugin:
        async def execute(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return "Plugin result"
    return Plugin()


# ============================================================================
# Main Example
# ============================================================================

if __name__ == "__main__":
    asyncio.run(cli_main_with_telemetry())
