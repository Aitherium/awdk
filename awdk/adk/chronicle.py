"""Chronicle client — structured JSON logging with AitherOS Chronicle integration.

Provides structured logging for the ADK that:
  1. Emits JSON-formatted log records to stdout (container-friendly)
  2. Forwards log entries to AitherOS Chronicle:8121 when available
  3. Queues offline when Chronicle is unreachable (JSONL disk queue)
  4. Propagates request_id / trace_id across all log entries

Every tool call, agent spawn, LLM request, and security event gets a
structured log entry — feeding Chronicle's search, alerting, and training
data pipelines.

Usage:
    from adk.chronicle import get_chronicle, configure_logging

    # At startup — installs JSON formatter + Chronicle handler
    configure_logging(level="INFO")

    # Per-request structured logging
    chronicle = get_chronicle()
    await chronicle.log_event("tool_call", tool="web_search", agent="atlas",
                              latency_ms=42, request_id="abc123")

    # Or use standard Python logging (auto-formatted as JSON)
    import logging
    logger = logging.getLogger("adk.agent")
    logger.info("Agent started", extra={"agent": "atlas", "request_id": "abc123"})
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.chronicle")

_DEFAULT_CHRONICLE_URL = "http://localhost:8121"
_QUEUE_MAX_LINES = 2000


# ─────────────────────────────────────────────────────────────────────────────
# JSON Log Formatter
# ─────────────────────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for container/sidecar aggregation.

    Outputs one JSON object per line with:
      timestamp, level, logger, message, module, funcName,
      plus any extra fields (request_id, agent, tool, etc.)
    """

    # Fields from LogRecord we always include
    _BASE_FIELDS = {"timestamp", "level", "logger", "message", "module", "funcName"}
    # Fields from LogRecord we never include (noise)
    _SKIP_FIELDS = {
        "name", "msg", "args", "created", "relativeCreated", "thread",
        "threadName", "msecs", "pathname", "filename", "lineno",
        "exc_info", "exc_text", "stack_info", "levelname", "levelno",
        "funcName", "module", "processName", "process", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
        }

        # Include any extra fields the caller passed
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in self._SKIP_FIELDS or key in self._BASE_FIELDS:
                continue
            # Only include serializable extras
            try:
                json.dumps(val)
                entry[key] = val
            except (TypeError, ValueError):
                entry[key] = str(val)

        # Exception info
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# Chronicle Client
# ─────────────────────────────────────────────────────────────────────────────

class ChronicleClient:
    """Structured event logger that forwards to AitherOS Chronicle.

    Fire-and-forget: never blocks, never raises, queues offline.
    """

    def __init__(
        self,
        chronicle_url: str = "",
        data_dir: str | Path | None = None,
        source: str = "adk",
    ):
        self.chronicle_url = (
            chronicle_url
            or os.getenv("AITHER_CHRONICLE_URL", "")
        ).rstrip("/")
        self._data_dir = Path(
            data_dir or os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
        )
        self._queue_path = self._data_dir / "chronicle_queue.jsonl"
        self._source = source
        self._enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        self._enabled = bool(self.chronicle_url)
        return self._enabled

    async def log_event(
        self,
        event_type: str,
        *,
        request_id: str = "",
        agent: str = "",
        session_id: str = "",
        level: str = "INFO",
        **fields,
    ) -> bool:
        """Send a structured event to Chronicle. Returns True if sent.

        Fire-and-forget — never raises, queues offline on failure.
        """
        if not self.enabled:
            return False

        payload = {
            "source": self._source,
            "event_type": event_type,
            "timestamp": time.time(),
            "level": level,
        }
        if request_id:
            payload["request_id"] = request_id
        if agent:
            payload["agent"] = agent
        if session_id:
            payload["session_id"] = session_id

        # Merge extra fields
        payload.update(fields)

        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self.chronicle_url}/api/v1/ingest",
                    json=payload,
                )
                if resp.status_code < 300:
                    return True
                logger.debug("Chronicle ingest returned %d", resp.status_code)
        except Exception as exc:
            logger.debug("Chronicle ingest failed, queuing: %s", exc)

        self._queue_offline(payload)
        return False

    async def log_tool_call(
        self,
        *,
        tool: str,
        agent: str = "",
        request_id: str = "",
        arguments: dict | None = None,
        result_size: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
        error: str = "",
    ) -> bool:
        """Log a tool invocation."""
        return await self.log_event(
            "tool_call",
            request_id=request_id,
            agent=agent,
            tool=tool,
            arguments_keys=list((arguments or {}).keys()),
            result_size=result_size,
            latency_ms=latency_ms,
            success=success,
            error=error,
        )

    async def log_llm_call(
        self,
        *,
        model: str,
        agent: str = "",
        request_id: str = "",
        tokens_used: int = 0,
        latency_ms: float = 0.0,
        provider: str = "",
        success: bool = True,
        error: str = "",
    ) -> bool:
        """Log an LLM invocation."""
        return await self.log_event(
            "llm_call",
            request_id=request_id,
            agent=agent,
            model=model,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            provider=provider,
            success=success,
            error=error,
        )

    async def log_agent_spawn(
        self,
        *,
        agent: str,
        parent_agent: str = "",
        request_id: str = "",
        effort: int = 0,
        task_preview: str = "",
    ) -> bool:
        """Log an agent dispatch/spawn event."""
        return await self.log_event(
            "agent_spawn",
            request_id=request_id,
            agent=agent,
            parent_agent=parent_agent,
            effort=effort,
            task_preview=task_preview[:200],
        )

    async def log_security_event(
        self,
        *,
        event: str,
        agent: str = "",
        request_id: str = "",
        capability: str = "",
        action: str = "",
        allowed: bool = True,
        reason: str = "",
    ) -> bool:
        """Log a security-relevant event (capability check, sandbox block, etc.)."""
        return await self.log_event(
            "security",
            request_id=request_id,
            agent=agent,
            level="WARNING" if not allowed else "INFO",
            security_event=event,
            capability=capability,
            action=action,
            allowed=allowed,
            reason=reason,
        )

    def _queue_offline(self, payload: dict):
        """Write to JSONL disk queue for retry when Chronicle comes back."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._queue_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=str) + "\n")
            _cap_jsonl(self._queue_path, _QUEUE_MAX_LINES)
        except Exception as exc:
            logger.debug("Failed to queue Chronicle data: %s", exc)

    async def flush_queue(self) -> int:
        """Try to send queued entries to Chronicle. Returns count sent."""
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
                            f"{self.chronicle_url}/api/v1/ingest",
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

_instance: ChronicleClient | None = None


def get_chronicle() -> ChronicleClient:
    """Get or create the module-level ChronicleClient singleton."""
    global _instance
    if _instance is None:
        _instance = ChronicleClient()
    return _instance


# ─────────────────────────────────────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────────────────────────────────────

_configured = False


def configure_logging(
    level: str = "INFO",
    json_output: bool = True,
) -> None:
    """Install JSON formatter on the root logger for structured output.

    Call once at startup (e.g. in server.py or CLI entry point).
    Idempotent — safe to call multiple times.
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicate output
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        ))

    root.addHandler(handler)


def _cap_jsonl(path: Path, max_lines: int):
    """Cap a JSONL file at max_lines, keeping the newest."""
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except Exception:
        pass
