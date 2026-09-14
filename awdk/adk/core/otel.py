"""OpenTelemetry tracing exporter.

Optional integration. If the ``opentelemetry`` SDK is installed, this module
provides :class:`OTelTracer` — a :class:`~adk.core.trace.Tracer`
implementation that ships spans via OTLP/HTTP while preserving the ADK's
native :class:`Span` API.

If the SDK is not installed, importing this module raises
:class:`OTelNotInstalled`. Callers who want best-effort behaviour should use
:func:`try_build_otel_tracer`, which returns ``None`` on import failure.

Usage::

    from adk.core.otel import OTelTracer
    from adk.core.trace import set_tracer

    set_tracer(OTelTracer(endpoint="http://collector:4318/v1/traces",
                           service_name="my-agent"))
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from adk.core.logging import get_logger
from adk.core.trace import Span, Tracer

_log = get_logger("trace.otel")

# OTel attribute values must be scalar (str/bool/int/float) or a homogeneous
# sequence thereof. Everything else is coerced to ``repr()``.
_AttrScalar = (str, bool, int, float)


class OTelNotInstalled(ImportError):
    """Raised when ``opentelemetry`` is required but not importable."""


def _coerce(value: Any) -> Any:
    if isinstance(value, _AttrScalar):
        return value
    if isinstance(value, (list, tuple)) and value:
        first_type = type(value[0])
        if first_type in _AttrScalar and all(type(v) is first_type for v in value):
            return list(value)
    if isinstance(value, (list, tuple)) and not value:
        return []
    return repr(value)


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    return {k: _coerce(v) for k, v in attrs.items()}


def _import_otel():
    try:
        from opentelemetry import trace as ot_trace  # noqa: F401
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
            SpanProcessor,
        )
    except ImportError as exc:  # pragma: no cover - exercised via fallback path
        raise OTelNotInstalled(
            "opentelemetry SDK not installed. Install with "
            "`pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`"
        ) from exc
    return {
        "trace": ot_trace,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "SimpleSpanProcessor": SimpleSpanProcessor,
        "SpanProcessor": SpanProcessor,
    }


class OTelTracer(Tracer):
    """:class:`Tracer` implementation backed by the opentelemetry SDK.

    Spans yielded from :meth:`span` are the ADK's native :class:`Span` objects
    (so ``set_attr``, ``attrs``, ``duration_ms`` keep working). On exit the
    accumulated attributes are copied onto the underlying OTel span, which is
    then ended — triggering whichever exporter you configured.

    Parameters
    ----------
    endpoint:
        OTLP/HTTP endpoint (e.g., ``http://collector:4318/v1/traces``). If
        omitted and no ``provider`` is supplied, the SDK uses its env-based
        defaults (``OTEL_EXPORTER_OTLP_ENDPOINT``).
    service_name:
        ``service.name`` resource attribute.
    provider:
        Pre-built ``TracerProvider`` to use as-is (test seam). Takes
        precedence over ``endpoint``/``service_name``.
    processors:
        Extra ``SpanProcessor``s to register against the provider.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        service_name: str = "aither_adk",
        provider: Any | None = None,
        processors: Sequence[Any] | None = None,
    ) -> None:
        otel = _import_otel()
        if provider is None:
            resource = otel["Resource"].create({"service.name": service_name})
            provider = otel["TracerProvider"](resource=resource)
            if endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                except ImportError as exc:
                    raise OTelNotInstalled(
                        "opentelemetry-exporter-otlp-proto-http not installed"
                    ) from exc
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(otel["BatchSpanProcessor"](exporter))
        for proc in processors or ():
            provider.add_span_processor(proc)
        self._provider = provider
        self._otel_trace = otel["trace"]
        self._otel = self._otel_trace.get_tracer("aither_adk", tracer_provider=provider)

    @property
    def provider(self) -> Any:
        """Underlying ``TracerProvider``. Useful for tests and shutdown."""
        return self._provider

    def shutdown(self) -> None:
        """Flush and shut down the provider. Safe to call multiple times."""
        try:
            self._provider.shutdown()
        except Exception as exc:  # pragma: no cover
            _log.warning("otel.shutdown.failed: %s", exc)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        otel_span = self._otel.start_span(name, attributes=_clean(attrs))
        s = Span(name, attrs)
        ctx = otel_span.get_span_context()
        # Replace ADK-generated ids with OTel ids so logs/exports line up.
        s.trace_id = format(ctx.trace_id, "032x")
        s.span_id = format(ctx.span_id, "016x")
        try:
            yield s
        except BaseException as exc:
            try:
                otel_span.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode

                otel_span.set_status(Status(StatusCode.ERROR, str(exc)))
            except Exception:  # pragma: no cover - never let tracing mask errors
                pass
            raise
        finally:
            for k, v in s.attrs.items():
                try:
                    otel_span.set_attribute(k, _coerce(v))
                except Exception:  # pragma: no cover
                    pass
            otel_span.end()


def try_build_otel_tracer(
    *,
    endpoint: str | None = None,
    service_name: str = "aither_adk",
) -> OTelTracer | None:
    """Build an :class:`OTelTracer` if possible; return ``None`` on failure.

    Logs at WARNING when the SDK is missing so the operator knows why
    tracing is silent.
    """
    try:
        return OTelTracer(endpoint=endpoint, service_name=service_name)
    except OTelNotInstalled as exc:
        _log.log(logging.WARNING, "otel.unavailable: %s", exc)
        return None
