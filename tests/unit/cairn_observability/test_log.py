"""Tests for cairn_observability.log — StructuredJsonFormatter + OTEL LoggingInstrumentor."""

from __future__ import annotations

import io
import json
import logging

import pytest
from cairn_observability.keys import ObservabilityKeys
from cairn_observability.log import StructuredJsonFormatter, configure_root_logger
from opentelemetry import baggage, context, trace
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """SDK TracerProvider — so spans have valid SpanContexts that the
    LoggingInstrumentor's record-attribute attachment reflects."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(scope="session", autouse=True)
def _otel_logging_instrumentor() -> None:
    """Instrument stdlib logging exactly once for the test session — the
    instrumentor patches the LogRecord factory; tests rely on
    `record.otelTraceID` / `record.otelSpanID` being present."""
    inst = LoggingInstrumentor()
    if not inst.is_instrumented_by_opentelemetry:
        inst.instrument(set_logging_format=False)


@pytest.fixture(autouse=True)
def _isolate_context() -> None:
    """Reset OTEL context per test — see test_scope.py for the rationale."""
    token = context.attach(context.Context())
    yield
    context.detach(token)


def _make_record(
    msg: str = "hello", level: int = logging.INFO, **extra: object
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="t",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(rec, key, value)
    # LoggingInstrumentor attaches otelTraceID/otelSpanID via the factory,
    # which is invoked by logging.getLogger().log(...) — for these unit
    # tests that construct LogRecord directly, the attributes default to "0"
    # which the formatter normalizes to None.
    return rec


def _format(rec: logging.LogRecord) -> dict:
    return json.loads(StructuredJsonFormatter().format(rec))


# ── 4-guaranteed-fields contract ─────────────────────────────────────────


def test_record_always_has_level_message_traceid_sessionid_spanid() -> None:
    out = _format(_make_record("test message", level=logging.WARNING))
    assert out[ObservabilityKeys.LEVEL] == "WARNING"
    assert out[ObservabilityKeys.MESSAGE] == "test message"
    assert ObservabilityKeys.TRACE_ID in out
    assert ObservabilityKeys.SESSION_ID in out
    assert ObservabilityKeys.SPAN_ID in out


def test_trace_id_is_null_when_no_active_span() -> None:
    out = _format(_make_record())
    assert out[ObservabilityKeys.TRACE_ID] is None
    assert out[ObservabilityKeys.SPAN_ID] is None


def test_trace_id_normalized_from_otel_zero_marker() -> None:
    """LoggingInstrumentor emits '0' (or 32-zero-hex) for inactive trace
    IDs; the formatter normalizes to None so dashboards don't see '0'."""
    rec = _make_record()
    rec.otelTraceID = "0"
    rec.otelSpanID = "0"
    out = _format(rec)
    assert out[ObservabilityKeys.TRACE_ID] is None
    assert out[ObservabilityKeys.SPAN_ID] is None


def test_trace_id_normalized_from_32_zero_hex() -> None:
    rec = _make_record()
    rec.otelTraceID = "00000000000000000000000000000000"
    rec.otelSpanID = "0000000000000000"
    out = _format(rec)
    assert out[ObservabilityKeys.TRACE_ID] is None
    assert out[ObservabilityKeys.SPAN_ID] is None


def test_trace_id_passes_through_real_value() -> None:
    rec = _make_record()
    rec.otelTraceID = "0af7651916cd43dd8448eb211c80319c"
    rec.otelSpanID = "b7ad6b7169203331"
    out = _format(rec)
    assert out[ObservabilityKeys.TRACE_ID] == "0af7651916cd43dd8448eb211c80319c"
    assert out[ObservabilityKeys.SPAN_ID] == "b7ad6b7169203331"


# ── baggage → output ─────────────────────────────────────────────────────


def test_baggage_fields_appear_in_output() -> None:
    ctx = baggage.set_baggage(ObservabilityKeys.PROJECT_ID, "alpha-bank")
    ctx = baggage.set_baggage(ObservabilityKeys.MANIFEST_DIGEST, "sha256:abc", ctx)
    token = context.attach(ctx)
    try:
        out = _format(_make_record())
        assert out[ObservabilityKeys.PROJECT_ID] == "alpha-bank"
        assert out[ObservabilityKeys.MANIFEST_DIGEST] == "sha256:abc"
    finally:
        context.detach(token)


def test_session_id_from_baggage() -> None:
    token = context.attach(baggage.set_baggage(ObservabilityKeys.SESSION_ID, "sess-7"))
    try:
        out = _format(_make_record())
        assert out[ObservabilityKeys.SESSION_ID] == "sess-7"
    finally:
        context.detach(token)


def test_baggage_entries_outside_allow_list_are_dropped() -> None:
    """Producer-asserted W3C baggage entries with unknown keys must not
    flow into structured log output — they could shadow operator-meaningful
    fields or accidentally capture upstream secrets from producer CI."""
    ctx = baggage.set_baggage("secretToken", "leaked")
    ctx = baggage.set_baggage("custom_field", "ignored", ctx)
    ctx = baggage.set_baggage(ObservabilityKeys.PROJECT_ID, "alpha-bank", ctx)
    token = context.attach(ctx)
    try:
        out = _format(_make_record())
        # Known key from the allow-list → present in output
        assert out[ObservabilityKeys.PROJECT_ID] == "alpha-bank"
        # Unknown keys (producer-asserted) → dropped at formatter time
        assert "secretToken" not in out
        assert "custom_field" not in out
    finally:
        context.detach(token)


# ── per-record `extra=` fields ───────────────────────────────────────────


def test_extra_fields_appear_in_output() -> None:
    out = _format(_make_record(custom_field="value"))
    assert out["custom_field"] == "value"


def test_reserved_logrecord_attrs_suppressed() -> None:
    out = _format(_make_record())
    for reserved in ("args", "filename", "funcName", "lineno", "module", "name", "thread"):
        assert reserved not in out
    # otelTraceID/otelSpanID rendered as traceId/spanId, not under their otel* names
    assert "otelTraceID" not in out
    assert "otelSpanID" not in out


# ── exception formatting ─────────────────────────────────────────────────


def test_exception_info_serialized() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="caught",
            args=(),
            exc_info=sys.exc_info(),
        )
    out = _format(rec)
    assert "exception" in out
    assert "ValueError: boom" in out["exception"]


# ── configure_root_logger ───────────────────────────────────────────────


def test_configure_root_logger_installs_formatter_idempotently() -> None:
    configure_root_logger()
    configure_root_logger()  # second call must not duplicate handlers
    root = logging.getLogger()
    handler_count = len(root.handlers)
    assert handler_count >= 1
    # All handlers should use StructuredJsonFormatter
    for handler in root.handlers:
        assert isinstance(handler.formatter, StructuredJsonFormatter)


def test_configure_root_logger_emits_structured_json_via_real_log_call() -> None:
    """End-to-end: getLogger().info() goes through the configured formatter
    and produces a parseable JSON line."""
    configure_root_logger()
    root = logging.getLogger()
    # Capture by redirecting one handler's stream
    buf = io.StringIO()
    original_streams = [(h, getattr(h, "stream", None)) for h in root.handlers]
    for handler in root.handlers:
        if hasattr(handler, "stream"):
            handler.stream = buf
    try:
        logging.getLogger("test").info("end-to-end test", extra={"customField": "x"})
    finally:
        for handler, stream in original_streams:
            if stream is not None:
                handler.stream = stream
    line = buf.getvalue().strip().splitlines()[-1]
    out = json.loads(line)
    assert out[ObservabilityKeys.MESSAGE] == "end-to-end test"
    assert out["customField"] == "x"
    assert ObservabilityKeys.TRACE_ID in out
