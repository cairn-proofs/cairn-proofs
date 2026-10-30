"""Production-path trace-correlation contract.

This file drives the **actual production logging path** —
`configure_root_logger()` then a log call inside `observability_scope(...)`
with a real SDK `TracerProvider` and a recording span active — and asserts
the emitted structured record's `traceId` is the span's real, non-zero,
32-hex trace ID.

`configure_root_logger()` calls
`_INSTRUMENTOR.instrument(set_logging_format=False)` with an `_otel_log_hook`
that copies the active span's IDs onto each record. In the pinned
`opentelemetry-instrumentation-logging==0.63b1`, the installed record factory
short-circuits when `set_logging_format=False` and no callable hook is present
(`if not set_logging_format and not callable(_log_hook): return record`), and
`record.otelTraceID` is only assigned inside the `if set_logging_format:`
branch — so the `_otel_log_hook` is what attaches `record.otelTraceID` without
forcing `set_logging_format=True`.

`test_emitted_record_carries_real_nonzero_traceid_via_production_path`
exercises that path end-to-end against the same `configure_root_logger()`
wiring the deployed Lambdas use, not the `set_logging_format=True` workaround.

Parallel-safety / global-pollution discipline: `LoggingInstrumentor` is a
process-global singleton and `configure_root_logger()` only (re)instruments
when `is_instrumented_by_opentelemetry` is False (the production idempotency
guard). The sibling suites (`test_log.py`, `test_scope.py`) install a
session-autouse `TracerProvider` and instrument with `set_logging_format=False`.
Each test here snapshots the instrumentor state, the LogRecord factory, and the
root logger's handlers, drives a freshly-(re)instrumented production state, then
restores everything on teardown — so nothing leaks across tests within an xdist
worker and no global state is mutated past the test boundary.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator

import opentelemetry.trace as trace_api
import pytest
from cairn_observability import scope as _scope_mod
from cairn_observability.keys import ObservabilityKeys
from cairn_observability.log import (
    StructuredJsonFormatter,
    _otel_log_hook,
    configure_root_logger,
)
from cairn_observability.scope import observability_scope
from cairn_observability.tracing import configure_tracing
from opentelemetry import context, trace
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.trace import (
    SynchronousMultiSpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import (
    INVALID_SPAN,
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_ALL_ZEROS = "00000000000000000000000000000000"


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """Install a real SDK TracerProvider — the production equivalent is the
    ADOT layer's auto-instrument wrapper. Without it, spans are NonRecording
    with all-zeros trace IDs."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(autouse=True)
def _isolate_context() -> Iterator[None]:
    """Reset OTEL context per test — see test_scope.py for the rationale."""
    token = context.attach(context.Context())
    yield
    context.detach(token)


@pytest.fixture
def _production_logging() -> Iterator[list[logging.Handler]]:
    """Drive a freshly-(re)instrumented production logging state, then restore.

    `configure_root_logger()` only calls `_INSTRUMENTOR.instrument(...)` when
    the instrumentor is NOT already instrumented (its production idempotency
    guard). The sibling session fixtures may have already instrumented with
    `set_logging_format=False`, so we uninstrument first to force
    `configure_root_logger()` to run the *real* production `instrument(...)`
    call. On teardown we uninstrument, restore the
    prior instrumented state, the LogRecord factory, and the root logger's
    handlers/level — leaving no global pollution and staying xdist-parallel-safe.

    Yields the list of handlers that the production `configure_root_logger()`
    call installed on the root logger (the set difference vs. the pre-existing
    handlers). Tests assert on precisely this set rather than scanning all root
    handlers, because pytest's logging plugin and prior sibling tests can leave
    unrelated `LogCaptureHandler`s on root — some even carrying a
    StructuredJsonFormatter from a prior `configure_root_logger()` call.
    """
    inst = LoggingInstrumentor()
    was_instrumented = inst.is_instrumented_by_opentelemetry
    saved_factory = logging.getLogRecordFactory()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level

    if was_instrumented:
        inst.uninstrument()

    # Start from a clean root logger so the production path runs exactly as a
    # cold-started Lambda does (no root handlers). pytest's logging plugin and
    # the sibling suites may have left handlers attached; we restore them in
    # teardown.
    root.handlers[:] = []

    # Production wiring: configure_root_logger() (re)instruments the LogRecord
    # factory via _INSTRUMENTOR.instrument(...) exactly as the deployed Lambdas
    # do at handler entry.
    configure_root_logger()
    production_handlers = list(root.handlers)
    try:
        yield production_handlers
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        if was_instrumented:
            inst.instrument(set_logging_format=False)
        logging.setLogRecordFactory(saved_factory)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _attach_capturing_handler(captured: list[str], level: int = logging.INFO) -> logging.Handler:
    """Attach a handler to the root logger that formats records through the
    production StructuredJsonFormatter and captures the JSON lines.

    The handler is added to the root logger so it sees records that propagate
    up the real production logging path (mirroring how CloudWatch's stream
    handler does in the deployed runtime). The caller restores root handlers
    via the `_production_logging` fixture teardown.

    Records from the `opentelemetry.*` logger namespace are excluded: the
    production `configure_tracing()` installs a `BatchSpanProcessor` whose OTLP
    exporter logs `WARNING`/`ERROR` diagnostics (e.g. "Transient error" /
    "Failed to export span batch") when the collector's localhost OTLP receiver
    is unreachable — which it always is in unit tests, and transiently can be in
    production. Those are SDK infrastructure diagnostics, not application log
    records, and they propagate to the root logger; capturing them would make
    `captured[-1]` an exporter warning rather than the application line these
    tests assert on. Filtering by logger namespace keeps the capture scoped to
    the application production path regardless of exporter reachability."""
    formatter = StructuredJsonFormatter()

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name.startswith("opentelemetry"):
                return
            captured.append(formatter.format(record))

    handler = _CaptureHandler()
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)
    return handler


# ── Production-path traceId contract ─────────────────────────────────────────


def test_emitted_record_carries_real_nonzero_traceid_via_production_path(
    _production_logging: list[logging.Handler],
) -> None:
    """With a real SDK TracerProvider and a recording span active, the
    **production path** — `configure_root_logger()` (via the
    `_production_logging` fixture) then a log call inside
    `observability_scope(...)` — emits a `StructuredJsonFormatter` record whose
    `traceId` matches `^[0-9a-f]{32}$` and is not all-zeros.

    `configure_root_logger()` calls `instrument(set_logging_format=False)` with
    the `_otel_log_hook`, which attaches `record.otelTraceID` from the active
    span so the formatter surfaces the real trace ID rather than `traceId: null`
    (the 0.63b1 no-op record factory never attaches it on its own under
    `set_logging_format=False`).
    """
    captured: list[str] = []
    _attach_capturing_handler(captured)

    with observability_scope({}) as ctx:
        active_span_ctx = trace.get_current_span().get_span_context()
        assert active_span_ctx.is_valid, (
            "expected a valid recording span under the real TracerProvider"
        )
        assert ctx.trace_id is not None, "scope should resolve a real trace_id from the span"
        logging.getLogger("cairn.pipeline").info("instrumented invocation")

    assert captured, "production log path emitted no record"
    out = json.loads(captured[-1])
    trace_id = out[ObservabilityKeys.TRACE_ID]

    assert trace_id is not None, (
        "production path must emit a non-null traceId when a recording span is "
        "active — the _otel_log_hook attaches it under instrument(set_logging_format=False)"
    )
    assert isinstance(trace_id, str)
    assert _HEX32.match(trace_id), f"traceId {trace_id!r} is not 32 lowercase hex chars"
    assert trace_id != _ALL_ZEROS, "traceId is the all-zeros sentinel — span was non-recording"
    # The emitted trace ID must match the active recording span's trace ID.
    assert trace_id == format(active_span_ctx.trace_id, "032x")
    # A recording span surfaces a real, non-null span id too.
    assert out[ObservabilityKeys.SPAN_ID] is not None


# ── Single emission with the guaranteed base fields ──────────────────────────


def test_single_log_call_emits_exactly_one_record_with_base_fields(
    _production_logging: list[logging.Handler],
) -> None:
    """After `configure_root_logger()`, a single log call emits exactly ONE
    `CairnLogLayout` record carrying the four guaranteed base fields (`level`,
    `message`, `traceId`, `sessionId`) — with no duplicate handler and no
    `basicConfig`-installed stream handler.

    Guards against the `set_logging_format=True` failure mode, which calls
    `logging.basicConfig()` and would install a second stream handler that
    double-logs.
    """
    production_handlers = _production_logging
    # The fixture ran the production `configure_root_logger()` against a clean
    # root (cold-start equivalent) and captured exactly the handlers it
    # installed. That path installs exactly ONE handler — and it carries the
    # StructuredJsonFormatter. The `set_logging_format=True` failure mode would
    # additionally call `logging.basicConfig()`, installing a SECOND bare
    # StreamHandler that double-logs; a count of exactly 1 guards against it.
    assert len(production_handlers) == 1, (
        f"expected exactly one production-installed root handler, got "
        f"{len(production_handlers)} (duplicate handler / basicConfig regression?)"
    )
    assert isinstance(production_handlers[0].formatter, StructuredJsonFormatter), (
        "the production handler must carry StructuredJsonFormatter"
    )
    # No basicConfig-installed bare StreamHandler among the production handlers.
    basicconfig_handlers = [
        h
        for h in production_handlers
        if type(h) is logging.StreamHandler and not isinstance(h.formatter, StructuredJsonFormatter)
    ]
    assert not basicconfig_handlers, (
        "production path must not install a basicConfig stream handler "
        f"(found {len(basicconfig_handlers)})"
    )

    captured: list[str] = []
    _attach_capturing_handler(captured)

    logging.getLogger("cairn.pipeline").warning("single emission")

    assert len(captured) == 1, (
        f"expected exactly one emitted record, got {len(captured)} "
        "(duplicate handler / basicConfig regression?)"
    )
    out = json.loads(captured[0])
    # Four guaranteed base fields present.
    assert out[ObservabilityKeys.LEVEL] == "WARNING"
    assert out[ObservabilityKeys.MESSAGE] == "single emission"
    assert ObservabilityKeys.TRACE_ID in out
    assert ObservabilityKeys.SESSION_ID in out


# ── Null traceId when no recording span is active ────────────────────────────


def test_production_path_emits_null_traceid_without_recording_span(
    _production_logging: list[logging.Handler],
) -> None:
    """With the default no-op provider (no recording span active), the
    production log path emits `traceId: null` without raising.

    We force a no-op `NonRecordingSpan` to be the active span (the deployed
    runtime's state when no ADOT layer/SDK provider is present), then drive the
    production log path. The formatter must surface `traceId: null` and not
    raise — proving the log path is robust when uninstrumented (the hook only
    attaches IDs when the span context is valid; a no-op span has an invalid
    context).
    """
    captured: list[str] = []
    _attach_capturing_handler(captured)

    # Make the active span an explicit NonRecordingSpan with an invalid context
    # — exactly what OTEL's default no-op provider yields in an uninstrumented
    # deployment. `INVALID_SPAN` carries the all-zeros / invalid SpanContext.
    token = context.attach(
        trace.set_span_in_context(trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT))
    )
    try:
        logging.getLogger("cairn.pipeline").info("uninstrumented invocation")
    finally:
        context.detach(token)

    assert captured, "production log path emitted no record"
    out = json.loads(captured[-1])
    assert out[ObservabilityKeys.TRACE_ID] is None, (
        "no recording span → traceId must normalize to null"
    )
    assert out[ObservabilityKeys.SPAN_ID] is None


# ── Edge-case coverage: _otel_log_hook unit behavior ─────────────────────────
#
# The existing 1.4 test drives the hook end-to-end through the SDK
# TracerProvider, but never exercises the hook function in isolation across the
# span-context cases that determine whether IDs are attached. These call
# `_otel_log_hook(span, record)` directly with hand-built SpanContexts — no
# global state, no TracerProvider, no instrumentor — so they are fully
# parallel-safe (pure function under test).


def _bare_record() -> logging.LogRecord:
    """A fresh LogRecord with NO otel* attributes attached (the hook is the
    only thing that should attach them)."""
    return logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )


def test_log_hook_attaches_both_trace_and_span_id_for_valid_context() -> None:
    """Gap: 1.4 asserts traceId end-to-end but the hook must attach BOTH
    otelTraceID AND otelSpanID. A regression that dropped only the span id
    would leave 1.4 green (it asserts spanId is not None only after the SDK
    path). Drive the hook directly with a known valid SpanContext and assert
    both attributes equal the exact zero-padded hex of the context."""
    trace_id = 0x0AF7651916CD43DD8448EB211C80319C
    span_id = 0xB7AD6B7169203331
    sc = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    rec = _bare_record()
    _otel_log_hook(NonRecordingSpan(sc), rec)
    assert rec.otelTraceID == "0af7651916cd43dd8448eb211c80319c"
    assert rec.otelSpanID == "b7ad6b7169203331"
    assert _HEX32.match(rec.otelTraceID)
    assert _HEX16.match(rec.otelSpanID)


def test_log_hook_zero_pads_short_ids_to_full_width() -> None:
    """Gap: the rendered traceId must be exactly 32 hex chars, zero-padded.
    A small-integer trace id (1) must render as 31 leading zeros + '1', not
    a short string — otherwise the formatter's all-zeros normalization and any
    downstream 32-hex consumer would mishandle it."""
    sc = SpanContext(
        trace_id=0x1,
        span_id=0x2,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    rec = _bare_record()
    _otel_log_hook(NonRecordingSpan(sc), rec)
    assert rec.otelTraceID == "00000000000000000000000000000001"
    assert rec.otelSpanID == "0000000000000002"
    assert len(rec.otelTraceID) == 32
    assert len(rec.otelSpanID) == 16


def test_log_hook_attaches_ids_for_valid_but_sampled_out_remote_context() -> None:
    """Gap: a valid SpanContext that is remote and NOT sampled (sampled-out)
    still has `is_valid == True`, so the hook MUST attach the IDs — log-trace
    correlation does not depend on the sampling decision. This guards against a
    mistaken `is_recording()`/sampled-flag gate replacing the `is_valid` check,
    which would silently drop traceId on legitimately-correlated lines whenever
    the trace was sampled out upstream."""
    sc = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),  # sampled-OUT
    )
    assert sc.is_valid, "precondition: a non-zero remote context is still valid"
    rec = _bare_record()
    _otel_log_hook(NonRecordingSpan(sc), rec)
    assert rec.otelTraceID == "0af7651916cd43dd8448eb211c80319c"
    assert rec.otelSpanID == "b7ad6b7169203331"


def test_log_hook_attaches_nothing_for_invalid_context() -> None:
    """Gap: the no-op / uninstrumented case at the hook level. INVALID_SPAN
    carries the all-zeros (invalid) SpanContext that OTEL's default provider
    yields. The hook must leave BOTH otel attributes absent (not set them to
    zeros) so the formatter's getattr(...) default → None path produces
    `traceId: null` rather than the literal all-zeros string."""
    rec = _bare_record()
    _otel_log_hook(INVALID_SPAN, rec)
    assert not hasattr(rec, "otelTraceID")
    assert not hasattr(rec, "otelSpanID")


# ── Edge-case coverage: configure_root_logger idempotency (reused Lambda env) ─
#
# The docstring of `configure_root_logger` promises idempotency across reused
# Lambda execution environments. The existing
# test_log.py only asserts handler count after two calls; it does NOT assert
# the instrumentor is not re-instrumented (which would re-wrap the LogRecord
# factory) and does NOT assert the hook still fires after the second call.


def test_configure_root_logger_does_not_reinstrument_or_rewrap_factory(
    _production_logging: list[logging.Handler],
) -> None:
    """Gap: a second `configure_root_logger()` in the same process (warm Lambda
    reuse) must NOT re-run `_INSTRUMENTOR.instrument(...)` — doing so would
    re-wrap the LogRecord factory around the already-wrapped one, compounding
    on every warm invocation. Assert the instrumentor stays instrumented and
    the LogRecord factory object is byte-identical (not re-wrapped) after the
    second call.

    The `_production_logging` fixture has already driven one production
    `configure_root_logger()`; we capture the post-first-call state, call again,
    and assert nothing changed."""
    inst = LoggingInstrumentor()
    assert inst.is_instrumented_by_opentelemetry, (
        "precondition: fixture drove the production configure_root_logger()"
    )
    factory_before = logging.getLogRecordFactory()
    handlers_before = list(logging.getLogger().handlers)

    configure_root_logger()

    assert inst.is_instrumented_by_opentelemetry
    assert logging.getLogRecordFactory() is factory_before, (
        "second configure_root_logger() re-wrapped the LogRecord factory — "
        "the instrumentor idempotency guard regressed"
    )
    # No duplicate handler installed on the second call.
    assert logging.getLogger().handlers == handlers_before


# ── Layer pre-instrumentation with no hook ───────────────────────────────────
#
# The ADOT layer's auto-instrumentation may call `LoggingInstrumentor().instrument()`
# ITSELF during bootstrap — with no log_hook and set_logging_format=False (the
# 0.63b1 no-op record factory) — before `configure_root_logger()` runs. A guard
# that instrumented only when `not is_instrumented_by_opentelemetry` would SKIP
# our instrument call, so `_otel_log_hook` would never register and `traceId`
# would stay null even with a recording span. `configure_root_logger()` instead
# detects that the active hook is not ours, uninstruments, and re-instruments
# with our hook. This test simulates that layer-first ordering and asserts our
# hook wins.


def test_configure_root_logger_wins_over_layer_preinstrumentation_without_hook() -> None:
    """When the layer pre-instruments `LoggingInstrumentor` with NO log_hook
    (set_logging_format=False) before our code runs, then after
    `configure_root_logger()` a record emitted with a recording span active MUST
    carry a real non-zero 32-hex traceId — proving our `_otel_log_hook` replaced
    the layer's hook-less no-op instrumentation.

    Snapshots and restores all instrumentor / LogRecord-factory / root-handler
    global state on teardown so nothing leaks across xdist workers.
    """
    inst = LoggingInstrumentor()
    was_instrumented = inst.is_instrumented_by_opentelemetry
    saved_hook = LoggingInstrumentor._log_hook
    saved_factory = logging.getLogRecordFactory()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level

    if was_instrumented:
        inst.uninstrument()
    root.handlers[:] = []

    # Simulate the ADOT layer's bootstrap: instrument with NO hook. This is the
    # state our code finds itself in — already instrumented, but with the 0.63b1
    # no-op record factory that never attaches otelTraceID.
    inst.instrument(set_logging_format=False)
    assert LoggingInstrumentor._log_hook is not _otel_log_hook, (
        "precondition: layer pre-instrumented WITHOUT our hook"
    )

    try:
        # Our code runs after the layer. It must swap in our hook despite the
        # instrumentor already reporting as instrumented.
        configure_root_logger()
        assert LoggingInstrumentor._log_hook is _otel_log_hook, (
            "configure_root_logger() failed to install _otel_log_hook over the "
            "layer's hook-less pre-instrumentation"
        )

        captured: list[str] = []
        _attach_capturing_handler(captured)

        with observability_scope({}):
            active_span_ctx = trace.get_current_span().get_span_context()
            assert active_span_ctx.is_valid
            logging.getLogger("cairn.pipeline").info("post-layer instrumentation")

        assert captured, "production log path emitted no record"
        out = json.loads(captured[-1])
        trace_id = out[ObservabilityKeys.TRACE_ID]
        assert trace_id is not None, "layer pre-instrumentation shadowed our hook — traceId null"
        assert _HEX32.match(trace_id), f"traceId {trace_id!r} is not 32 lowercase hex chars"
        assert trace_id != _ALL_ZEROS
        assert trace_id == format(active_span_ctx.trace_id, "032x")
        assert out[ObservabilityKeys.SPAN_ID] is not None
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        if was_instrumented:
            inst.instrument(set_logging_format=False, log_hook=saved_hook)
        else:
            LoggingInstrumentor._log_hook = saved_hook
        logging.setLogRecordFactory(saved_factory)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_hook_still_fires_after_second_configure_root_logger(
    _production_logging: list[logging.Handler],
) -> None:
    """Gap: idempotency must not break correlation. After a SECOND
    `configure_root_logger()` (warm-reuse), a log emitted inside an
    instrumented scope must STILL carry a real non-zero 32-hex traceId. Guards
    against a second call silently uninstalling/short-circuiting the hook."""
    configure_root_logger()  # second call (fixture already ran the first)

    captured: list[str] = []
    _attach_capturing_handler(captured)

    with observability_scope({}):
        active_span_ctx = trace.get_current_span().get_span_context()
        assert active_span_ctx.is_valid
        logging.getLogger("cairn.pipeline").info("after second configure")

    assert captured, "production log path emitted no record"
    out = json.loads(captured[-1])
    trace_id = out[ObservabilityKeys.TRACE_ID]
    assert trace_id is not None, "hook stopped firing after a second configure_root_logger()"
    assert _HEX32.match(trace_id)
    assert trace_id != _ALL_ZEROS
    assert trace_id == format(active_span_ctx.trace_id, "032x")
    assert out[ObservabilityKeys.SPAN_ID] is not None


# ── Edge-case coverage: the REAL configure_tracing() X-Ray provider end-to-end ─
#
# The session-autouse `_otel_tracer_provider` fixture above installs a PLAIN
# `TracerProvider()` (default random 128-bit id generator) so the existing 1.4
# end-to-end test proves only "non-zero 32-hex traceId reaches the log". It never
# drives the actual production `configure_tracing()` provider, so it cannot prove
# the emitted traceId is X-Ray-FORMATTED (epoch-prefixed) — the property X-Ray
# requires. These tests snapshot/clear OTel's once-set provider globals, install
# the real `configure_tracing()` provider, and restore on teardown (xdist-safe).


def _span_processors(provider: TracerProvider) -> list[object]:
    """Enumerate the provider's active span processors (no public accessor)."""
    multi = provider._active_span_processor
    if isinstance(multi, SynchronousMultiSpanProcessor):
        return list(multi._span_processors)
    return list(getattr(multi, "_span_processors", []))


@pytest.fixture
def _real_xray_provider() -> Iterator[TracerProvider]:
    """Clear OTel's once-set provider globals, install the genuine
    `configure_tracing()` provider (AwsXRayIdGenerator + OTLP BatchSpanProcessor),
    yield it, then restore the prior global provider.

    Distinct from the session-autouse plain `TracerProvider()` fixture: this is
    the *production* provider, so a span started under it carries an
    X-Ray-formatted (time-prefixed) trace id.

    `scope._tracer` is a module-level `ProxyTracer` that LAZILY caches the real
    tracer (`_real_tracer`) on its first `start_span`. Sibling suites start spans
    while the plain session `TracerProvider()` is installed, so that proxy may
    already cache the plain provider's tracer. In production the ordering is the
    reverse — `configure_tracing()` runs at cold-start module-init, before any
    span — so the proxy resolves against the X-Ray provider. We reproduce that
    cold-start ordering by clearing the proxy's cached tracer after installing the
    X-Ray provider, and restore it on teardown (xdist-safe)."""
    saved_provider = trace_api._TRACER_PROVIDER
    saved_once = trace_api._TRACER_PROVIDER_SET_ONCE
    saved_real_tracer = _scope_mod._tracer._real_tracer
    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = trace_api.Once()
    try:
        configure_tracing()
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
        assert isinstance(provider.id_generator, AwsXRayIdGenerator)
        # Force the module ProxyTracer to re-resolve against the X-Ray provider
        # (production cold-start ordering), not a plain tracer a sibling cached.
        _scope_mod._tracer._real_tracer = None
        yield provider
    finally:
        _scope_mod._tracer._real_tracer = saved_real_tracer
        trace_api._TRACER_PROVIDER = saved_provider
        trace_api._TRACER_PROVIDER_SET_ONCE = saved_once


def test_emitted_record_traceid_is_xray_formatted_via_real_configure_tracing(
    _production_logging: list[logging.Handler],
    _real_xray_provider: TracerProvider,
) -> None:
    """Gap: the existing 1.4 end-to-end test runs under a PLAIN `TracerProvider()`
    so it proves only that *a* non-zero traceId reaches the log — not that the
    deployed-runtime traceId is X-Ray-FORMATTED. A regression dropping
    `id_generator=AwsXRayIdGenerator()` from `configure_tracing()` would leave 1.4
    green (random ids are still 32-hex non-zero) while X-Ray silently rejected
    every trace in production.

    Drive the full production path under the REAL `configure_tracing()` provider
    and assert the emitted log record's traceId (a) equals the active span's
    trace id and (b) has X-Ray's epoch-second prefix in its high 32 bits."""
    captured: list[str] = []
    _attach_capturing_handler(captured)

    before = int(time.time())
    with observability_scope({}):
        active_span_ctx = trace.get_current_span().get_span_context()
        assert active_span_ctx.is_valid, "expected a recording span under the X-Ray provider"
        logging.getLogger("cairn.pipeline").info("xray-formatted invocation")
    after = int(time.time())

    assert captured, "production log path emitted no record"
    out = json.loads(captured[-1])
    trace_id = out[ObservabilityKeys.TRACE_ID]

    assert trace_id is not None
    assert _HEX32.match(trace_id), f"traceId {trace_id!r} is not 32 lowercase hex chars"
    assert trace_id != _ALL_ZEROS
    assert trace_id == format(active_span_ctx.trace_id, "032x")

    # X-Ray format: high 32 bits == creation epoch seconds. Window-bound to
    # tolerate the second tick between span start and the post-scope read.
    high_32 = int(trace_id[:8], 16)
    assert before <= high_32 <= after, (
        f"emitted traceId high-32 bits {high_32} not a plausible creation epoch "
        f"in [{before}, {after}] — configure_tracing()'s AwsXRayIdGenerator not in effect"
    )


def test_second_configure_root_logger_adds_no_duplicate_span_processor(
    _real_xray_provider: TracerProvider,
) -> None:
    """Gap: `configure_root_logger()` now also calls `configure_tracing()`. The
    existing idempotency test
    (`test_configure_root_logger_does_not_reinstrument_or_rewrap_factory`) guards
    only the instrumentor/handler/factory state — it never asserts that a second
    `configure_root_logger()` on a warm Lambda does NOT register a duplicate
    `BatchSpanProcessor` on the already-installed provider. A duplicate processor
    would double-export every span on every warm invocation.

    The `_real_xray_provider` fixture has installed the production provider with
    exactly one BatchSpanProcessor. Call `configure_root_logger()` twice and
    assert the provider object is unchanged and the BatchSpanProcessor count
    stays at exactly one.

    This test deliberately does NOT use `_production_logging`: it asserts on the
    span-processor side of the bootstrap, and re-instrumenting the logger is
    irrelevant here. It restores the LoggingInstrumentor/factory/handler state
    itself so it leaves no global pollution under xdist."""
    inst = LoggingInstrumentor()
    was_instrumented = inst.is_instrumented_by_opentelemetry
    saved_hook = LoggingInstrumentor._log_hook
    saved_factory = logging.getLogRecordFactory()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level

    provider_before = trace.get_tracer_provider()
    assert provider_before is _real_xray_provider
    batch_before = [
        p for p in _span_processors(provider_before) if isinstance(p, BatchSpanProcessor)
    ]
    assert len(batch_before) == 1, "precondition: exactly one BatchSpanProcessor installed"

    try:
        configure_root_logger()
        configure_root_logger()  # warm-reuse second call

        provider_after = trace.get_tracer_provider()
        assert provider_after is provider_before, (
            "configure_root_logger() replaced the tracer provider on a warm call"
        )
        batch_after = [
            p for p in _span_processors(provider_after) if isinstance(p, BatchSpanProcessor)
        ]
        assert len(batch_after) == 1, (
            f"warm configure_root_logger() added a duplicate BatchSpanProcessor "
            f"(now {len(batch_after)}) — configure_tracing() idempotency regressed"
        )
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        if was_instrumented:
            inst.instrument(set_logging_format=False, log_hook=saved_hook)
        else:
            LoggingInstrumentor._log_hook = saved_hook
        logging.setLogRecordFactory(saved_factory)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
