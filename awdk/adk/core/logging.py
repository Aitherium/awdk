"""Structured JSON logging.

One logger factory. Emits JSON to stdout. Ships to Chronicle if
``AITHER_CHRONICLE_URL`` is set (handler is registered lazily and never
blocks the caller).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # any extra=... kwargs land in __dict__; pick them up
        for key, value in record.__dict__.items():
            if key in _LOGRECORD_RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, default=str)


_LOGRECORD_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }
)


def _ensure_configured() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("aither_adk")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
    level = os.environ.get("AITHER_ADK_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a JSON-formatted logger under the ``aither_adk`` namespace."""
    _ensure_configured()
    if not name.startswith("aither_adk"):
        name = f"aither_adk.{name}"
    return logging.getLogger(name)
