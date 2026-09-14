"""Tracing primitives.

One ``Tracer`` API: ``tracer.span(name, **attrs)`` context manager.

Default tracer is a no-op (zero overhead). If ``AITHER_OTLP_ENDPOINT`` is set
we attempt to use the optional ``opentelemetry`` SDK; if it isn't installed,
we fall back to no-op rather than failing — that's the only fallback allowed
in this package, and it's logged at WARNING.

If ``AITHER_CHRONICLE_URL`` is set we additionally tee spans to Chronicle
(stub in slice A: spans logged as structured events; real HTTP ship in slice B).
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from adk.core.logging import get_logger

_log = get_logger("trace")


class Tracer:
    """Base tracer protocol. Subclasses override :meth:`span`."""

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator["Span"]:
        raise NotImplementedError


class Span:
    """A single trace span. Use :meth:`set_attr` to add attributes mid-span."""

    __slots__ = ("name", "attrs", "trace_id", "span_id", "start_ns")

    def __init__(self, name: str, attrs: dict[str, Any]) -> None:
        self.name = name
        self.attrs = dict(attrs)
        self.trace_id = uuid.uuid4().hex
        self.span_id = uuid.uuid4().hex[:16]
        self.start_ns = time.perf_counter_ns()

    def set_attr(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter_ns() - self.start_ns) / 1_000_000


class NoOpTracer(Tracer):
    """Zero-overhead default. Yields a real :class:`Span` so attrs still work."""

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        yield Span(name, attrs)


class LoggingTracer(Tracer):
    """Emit each span as a structured log line on exit. Useful for dev."""

    def __init__(self, logger_name: str = "trace") -> None:
        self._log = get_logger(logger_name)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        s = Span(name, attrs)
        try:
            yield s
        finally:
            self._log.info(
                "span",
                extra={
                    "span_name": s.name,
                    "trace_id": s.trace_id,
                    "span_id": s.span_id,
                    "duration_ms": s.duration_ms,
                    **s.attrs,
                },
            )


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """Return the process-wide tracer. Picks based on env vars on first call."""
    global _tracer
    if _tracer is not None:
        return _tracer
    endpoint = os.environ.get("AITHER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from adk.core.otel import try_build_otel_tracer

            built = try_build_otel_tracer(
                endpoint=endpoint,
                service_name=os.environ.get("AITHER_SERVICE_NAME", "aither_adk"),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("otel.build.failed: %s", exc)
            built = None
        if built is not None:
            _tracer = built
            return _tracer
        # OTel requested but unavailable — log spans instead of going dark.
        _tracer = LoggingTracer()
        return _tracer
    if os.environ.get("AITHER_TRACE") == "log":
        _tracer = LoggingTracer()
    else:
        _tracer = NoOpTracer()
    return _tracer


def set_tracer(tracer: Tracer) -> None:
    """Override the process-wide tracer (tests, custom integrations)."""
    global _tracer
    _tracer = tracer
