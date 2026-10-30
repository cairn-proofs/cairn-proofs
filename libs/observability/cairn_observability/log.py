"""StructuredJsonFormatter — JSON log records with OTEL-driven trace correlation (Obs 6).

Every log record produced by this formatter contains at minimum the four
guaranteed fields (level, message, traceId, sessionId; the latter two
default to null when no trace context is active). Pipeline-specific
fields attached via `opentelemetry.baggage.set_baggage(...)` are
serialized alongside, as are per-record `extra={...}` fields passed by
the caller.

Trace correlation is provided by
`opentelemetry.instrumentation.logging.LoggingInstrumentor`, which
patches the stdlib `LogRecord` factory to attach `otelTraceID`,
`otelSpanID`, and `otelServiceName` attributes to every record.
`configure_root_logger()` enables the instrumentation idempotently;
the formatter then reads those attributes and renames them to
`traceId` / `spanId` per the project's `ObservabilityKeys` vocabulary.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import baggage
from opentelemetry.instrumentation.logging import LoggingInstrumentor

from cairn_observability.keys import ALLOWED_BAGGAGE_KEYS, ObservabilityKeys
from cairn_observability.tracing import configure_tracing

# Standard LogRecord attributes we never want to splat into the JSON output.
_LOG_RECORD_RESERVED: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        # LoggingInstrumentor attrs — read explicitly below, then suppressed
        # so they don't double-appear under their otel* names.
        "otelTraceID",
        "otelSpanID",
        "otelServiceName",
        "otelTraceSampled",
    }
)


class StructuredJsonFormatter(logging.Formatter):
    """Render LogRecords as single-line JSON with guaranteed base fields.

    Output schema per Obs 6:
      { "level": str, "message": str, "traceId": str|null,
        "sessionId": str|null, "spanId": str|null,
        ...baggage..., ...extras... }
    """

    def format(self, record: logging.LogRecord) -> str:
        # Base fields — guaranteed presence (Obs 6).
        trace_id = getattr(record, "otelTraceID", None)
        span_id = getattr(record, "otelSpanID", None)
        # OTEL emits "0" for inactive trace/span IDs; normalize to None so
        # operators don't see spurious "0" values in dashboards.
        if trace_id in ("0", "00000000000000000000000000000000"):
            trace_id = None
        if span_id in ("0", "0000000000000000"):
            span_id = None

        bg = baggage.get_all() or {}
        session_id = bg.get(ObservabilityKeys.SESSION_ID)

        payload: dict[str, Any] = {
            ObservabilityKeys.LEVEL: record.levelname,
            ObservabilityKeys.MESSAGE: record.getMessage(),
            ObservabilityKeys.TRACE_ID: trace_id,
            ObservabilityKeys.SESSION_ID: session_id if isinstance(session_id, str) else None,
            ObservabilityKeys.SPAN_ID: span_id,
        }

        # Baggage entries (pipeline-specific fields set by observability_scope
        # or by deeper handler code via baggage.set_baggage). Restricted to
        # the closed `ALLOWED_BAGGAGE_KEYS` vocabulary so producer-asserted
        # W3C baggage (extracted from EventBridge events) cannot inject
        # arbitrary keys into structured log output.
        for key, value in bg.items():
            if key in ALLOWED_BAGGAGE_KEYS and key not in payload:
                payload[key] = value

        # Per-record `extra={...}` fields surface as record attributes.
        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_RESERVED or key.startswith("_") or key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


_INSTRUMENTOR = LoggingInstrumentor()


def _otel_log_hook(span: Any, record: logging.LogRecord) -> None:
    """Copy the active span's trace/span IDs onto each LogRecord.

    Passed as `log_hook` to `LoggingInstrumentor.instrument(...)`. In the pinned
    `opentelemetry-instrumentation-logging==0.63b1`, the patched LogRecord
    factory short-circuits when `set_logging_format=False` AND no `log_hook` is
    callable (`if not set_logging_format and not callable(_log_hook): return
    record`), so `record.otelTraceID` is never attached and the formatter emits
    `traceId: null` even with a recording span active (Defect B). Supplying this
    hook makes the factory invoke it on every record, so we attach
    `otelTraceID`/`otelSpanID` when the span context is valid (a recording span)
    and leave them absent otherwise — preserving the robust `traceId: null`
    behavior when uninstrumented. We keep `set_logging_format=False` to avoid
    `logging.basicConfig()`, which would install a second stream handler that
    double-logs.
    """
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        record.otelTraceID = format(ctx.trace_id, "032x")
        record.otelSpanID = format(ctx.span_id, "016x")


def configure_root_logger(level: int = logging.INFO) -> None:
    """Install StructuredJsonFormatter on the root logger + enable OTEL log instrumentation.

    Idempotent: safe to call multiple times in a reused Lambda execution
    environment. `LoggingInstrumentor().instrument()` is itself idempotent
    via Powertools-style "already instrumented" detection.

    Robust to layer pre-instrumentation (design.md Defect C, secondary
    interaction): the ADOT layer's auto-instrumentation may call
    `LoggingInstrumentor().instrument()` itself — with no `log_hook` and
    `set_logging_format=False` (the 0.63b1 no-op record factory) — during
    bootstrap, before this function runs. A bare `is_instrumented_by_opentelemetry`
    guard would then skip our `instrument(...)`, so `_otel_log_hook` would never
    register and `traceId` would stay null. We therefore (re)instrument with our
    hook whenever the active hook is not already `_otel_log_hook`: uninstrument
    first (the only way to swap the class-level `_log_hook` cleanly), then
    re-instrument with the hook. We keep `set_logging_format=False` (never
    `OTEL_PYTHON_LOG_CORRELATION`/`set_logging_format=True`, which calls
    `logging.basicConfig()` and double-logs). When our hook is already the active
    one, this is a no-op — preserving idempotency (a second call must not churn
    the factory) and parallel-safety (no module-level mutable state beyond the
    read-only `_INSTRUMENTOR` singleton + `_otel_log_hook` constant).

    Also installs the SDK `TracerProvider` (idempotently, via
    `configure_tracing()`): the deployment uses a collector-only ADOT layer, so
    the application owns provider installation. Wiring it here means every
    handler's existing module-init `configure_root_logger()` call bootstraps both
    structured logging and tracing in one place — the single observability
    bootstrap seam.
    """
    configure_tracing()

    if not _INSTRUMENTOR.is_instrumented_by_opentelemetry:
        _INSTRUMENTOR.instrument(set_logging_format=False, log_hook=_otel_log_hook)
    elif LoggingInstrumentor._log_hook is not _otel_log_hook:
        _INSTRUMENTOR.uninstrument()
        _INSTRUMENTOR.instrument(set_logging_format=False, log_hook=_otel_log_hook)

    root = logging.getLogger()
    root.setLevel(level)
    formatter = StructuredJsonFormatter()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
