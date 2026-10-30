"""Per-invocation scope and W3C trace context extraction (Obs 2, Obs 3).

Built directly on OpenTelemetry primitives — the ADOT Lambda layer loads
the OTEL SDK at cold start, so this module is glue between EventBridge
event shape and OTEL's standard context model:

- W3C `traceparent` / `baggage` extraction uses
  `TraceContextTextMapPropagator` + `W3CBaggagePropagator` composed
  together. EventBridge events don't carry HTTP headers natively, so we
  probe a small set of candidate carrier locations (event root, `detail`,
  `detail.headers`) and let the propagator find the headers wherever the
  producer chose to put them.
- Per-invocation ambient state lives in `opentelemetry.baggage`
  (ContextVar-backed via `opentelemetry.context._RUNTIME_CONTEXT`). The
  `observability_scope` context manager attaches the populated context
  for the handler's lifetime and detaches on exit — both normal and
  exceptional. The active OTEL span is created via
  `tracer.start_as_current_span(...)` and closes deterministically with
  the `with` block.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import baggage, context, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from cairn_observability.keys import ALLOWED_BAGGAGE_KEYS, ObservabilityKeys

_PROPAGATOR = CompositePropagator(
    [
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ]
)

# The Lambda runtime publishes the active X-Ray trace context (the same one the
# native AWS::Lambda segment uses, propagated across EventBridge when the
# functions run `Tracing.ACTIVE`) in the `_X_AMZN_TRACE_ID` env var. We adopt it
# as the span parent so our OTEL span joins that ONE X-Ray trace rather than
# minting a parallel OTEL trace id (which would double every push into two console
# traces). `X-Amzn-Trace-Id` is the carrier key `AwsXRayPropagator` reads.
_XRAY_PROPAGATOR = AwsXRayPropagator()
_XRAY_HEADER = "X-Amzn-Trace-Id"
_XRAY_ENV_VAR = "_X_AMZN_TRACE_ID"

_tracer = trace.get_tracer("cairn-observability")


@dataclass(frozen=True)
class TraceContext:
    """Caller-facing view of the resolved trace context.

    Kept as a dataclass (rather than exposing OTEL's `Context` directly)
    so callers that want a "did we get headers or fall back to the active
    span" signal can read `source` without learning OTEL internals.
    """

    trace_id: str | None
    session_id: str | None
    source: str  # "headers" | "span" | "none"


def _candidate_carriers(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Carrier dicts to try, in priority order."""
    out: list[dict[str, Any]] = []
    if isinstance(event, dict):
        out.append(event)
        detail = event.get("detail")
        if isinstance(detail, dict):
            out.append(detail)
            headers = detail.get("headers")
            if isinstance(headers, dict):
                out.append(headers)
    return out


def _hex_trace_id(span_ctx: trace.SpanContext) -> str | None:
    if not span_ctx.is_valid or span_ctx.trace_id == 0:
        return None
    return format(span_ctx.trace_id, "032x")


def extract_trace_context(event: dict[str, Any]) -> TraceContext:
    """Resolve W3C traceparent/baggage with span fallback (Obs 2).

    Resolution order:
      1. EventBridge event payload (`traceparent` / `baggage` at the event
         root, then under `detail`, then under `detail.headers`) via the
         composite OTEL propagator.
      2. Active OpenTelemetry span via `trace.get_current_span()`.
      3. Empty context (both trace_id and session_id are None).
    """
    for carrier in _candidate_carriers(event):
        extracted = _PROPAGATOR.extract(carrier=carrier)
        span_ctx = trace.get_current_span(extracted).get_span_context()
        trace_id = _hex_trace_id(span_ctx)
        if trace_id is not None:
            session_id = baggage.get_baggage(ObservabilityKeys.SESSION_ID, extracted)
            return TraceContext(
                trace_id=trace_id,
                session_id=session_id if isinstance(session_id, str) else None,
                source="headers",
            )

    span_ctx = trace.get_current_span().get_span_context()
    trace_id = _hex_trace_id(span_ctx)
    if trace_id is not None:
        return TraceContext(trace_id=trace_id, session_id=None, source="span")

    return TraceContext(trace_id=None, session_id=None, source="none")


def inject_trace_context(carrier: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize the active W3C context into a carrier (producer-side mirror of
    extraction).

    The symmetric counterpart to `extract_trace_context`: where extraction
    *reads* `traceparent` / `baggage` off an incoming EventBridge carrier,
    this *writes* the active invocation's context into one — used to seed an
    outgoing EventBridge `Detail` so the downstream stage's
    `extract_trace_context()` resolves `source="headers"` and continues the
    same trace.

    Reuses the module's single `_PROPAGATOR` instance (Decision D1) so the
    producer-written and consumer-probed carrier shapes cannot drift.

    Before injecting, the active baggage is filtered down to
    `ALLOWED_BAGGAGE_KEYS` (Decision D5) so non-allow-listed baggage entries
    (potentially upstream secret material) are never serialized into the
    carrier — only allow-listed keys appear in the injected `baggage` header.

    If `carrier` is None a fresh dict is used; a caller may pass an existing
    dict (e.g. an event `Detail`) to inject the trace keys *into*, preserving
    its other entries.

    When no valid span is active (mirror of extraction's `source="none"`), the
    W3C `TraceContextTextMapPropagator` declines to inject, so the carrier is
    returned with no `traceparent` rather than a malformed one — no crash.

    Injection is authoritative: any `traceparent`/`baggage`/`tracestate` key
    already present on the passed carrier is cleared first, so a caller that
    re-injects into an extracted upstream `Detail` cannot leave a stale carrier
    behind when the propagator declines to write (no active span, or no
    allow-listed baggage). Non-carrier keys on the dict are preserved.
    """
    if carrier is None:
        carrier = {}

    # Clear any pre-existing carrier keys so injection is authoritative even
    # when the propagator declines to write (stale value would otherwise
    # survive). `_PROPAGATOR.fields` is the same propagator's own list of the
    # keys it manages, so this cannot drift from what `inject` writes.
    for field in _PROPAGATOR.fields:
        carrier.pop(field, None)

    # Build a context that carries only allow-listed baggage entries, then
    # inject from it (Decision D5). Starting from the current context (rather
    # than a bare one) preserves the active span so `traceparent` injects.
    filtered_ctx = context.get_current()
    for key in baggage.get_all():
        if key not in ALLOWED_BAGGAGE_KEYS:
            filtered_ctx = baggage.remove_baggage(key, filtered_ctx)

    _PROPAGATOR.inject(carrier=carrier, context=filtered_ctx)
    return carrier


@contextmanager
def observability_scope(
    event: dict[str, Any],
    *,
    request_id: str | None = None,
    project_id: str | None = None,
    manifest_digest: str | None = None,
    event_type: str | None = None,
    links: Sequence[trace.Link] | None = None,
    **extra_fields: Any,
) -> Iterator[TraceContext]:
    """Open per-invocation scope: hydrate W3C context, start span, set baggage.

    Use as the outermost context manager in every Lambda handler. The
    yielded `TraceContext` is informational; structured logging happens
    via standard `logging.*` calls and `StructuredJsonFormatter` reads
    trace/span IDs (via `LoggingInstrumentor`-attached record attributes)
    plus baggage entries automatically.

    `links` are optional OpenTelemetry span links recorded on the started
    span (Decision D4): a causally-related-but-separately-rooted invocation
    (e.g. an ECR fan-out `ValidationSkipped` linking back to the originating
    bundle's trace) passes the recovered originating `SpanContext` wrapped in
    a `trace.Link`. The default `None` is the common case (link-free) —
    linking is strictly additive and never gates the invocation.

    On exit (normal or exceptional), the attached OTEL context is
    detached and the started span closes — no leakage across reused
    Lambda execution environments.

    Span flush on Lambda freeze: the deployment installs an SDK
    `TracerProvider` with a `BatchSpanProcessor` (see `tracing.py`) and
    the collector-only ADOT layer provides no auto-instrument wrapper to
    flush on freeze. So on exit we `force_flush()` the global provider
    (guarded — only when it exposes `force_flush`, i.e. an SDK provider is
    installed; OTel's default no-op provider does not). This guarantees the
    just-closed span exports to the collector before the Lambda execution
    environment is frozen, otherwise the buffered span would be lost.
    """
    # Hydrate W3C context from the EventBridge event payload onto the
    # current context. The composite propagator handles both traceparent
    # and baggage in one pass.
    current_ctx = context.get_current()
    extracted_ctx = current_ctx
    for carrier in _candidate_carriers(event):
        candidate = _PROPAGATOR.extract(carrier=carrier, context=extracted_ctx)
        span_ctx = trace.get_current_span(candidate).get_span_context()
        if span_ctx.is_valid and span_ctx.trace_id != 0:
            extracted_ctx = candidate
            break

    # Layer caller-supplied fields into baggage. We deliberately stringify
    # values — baggage entries are W3C strings; callers passing ints/UUIDs
    # get an implicit `str()`.
    fields: dict[str, str] = {}
    if request_id is not None:
        fields[ObservabilityKeys.REQUEST_ID] = str(request_id)
    if project_id is not None:
        fields[ObservabilityKeys.PROJECT_ID] = str(project_id)
    if manifest_digest is not None:
        fields[ObservabilityKeys.MANIFEST_DIGEST] = str(manifest_digest)
    if event_type is not None:
        fields[ObservabilityKeys.EVENT_TYPE] = str(event_type)
    # Extra fields are restricted to the `ALLOWED_BAGGAGE_KEYS` allow-list
    # so a future caller can't accidentally leak secret material into
    # baggage (which propagates downstream and lands in every log line of
    # the invocation). Unknown keys are silently dropped — same trust
    # model as the formatter's baggage filter in `log.py`.
    for key, value in extra_fields.items():
        if key in ALLOWED_BAGGAGE_KEYS:
            fields[key] = str(value)

    for key, value in fields.items():
        extracted_ctx = baggage.set_baggage(key, value, extracted_ctx)

    # Adopt the Lambda runtime's native X-Ray trace context as the span parent
    # when present, so this span shares ONE X-Ray trace with the native
    # AWS::Lambda segment and with EventBridge's native AWSTraceHeader
    # propagation across stages — instead of minting a parallel OTEL trace id,
    # which would double every logical push into two separate traces in the X-Ray
    # console. This replicates the single behavior the ADOT auto-instrument
    # exec-wrapper would provide; we run the collector only (no
    # AWS_LAMBDA_EXEC_WRAPPER), so we hydrate it here. Baggage already layered
    # onto `extracted_ctx` is preserved — the X-Ray propagator sets only the
    # span context. When the header is absent (local runs, tests) the W3C parent
    # resolved above is kept.
    xray_header = os.environ.get(_XRAY_ENV_VAR)
    if xray_header:
        xray_ctx = _XRAY_PROPAGATOR.extract(
            carrier={_XRAY_HEADER: xray_header}, context=extracted_ctx
        )
        if trace.get_current_span(xray_ctx).get_span_context().is_valid:
            extracted_ctx = xray_ctx

    token = context.attach(extracted_ctx)
    try:
        with _tracer.start_as_current_span("cairn.handler", links=links):
            span_ctx = trace.get_current_span().get_span_context()
            trace_id = _hex_trace_id(span_ctx)
            session_id_value = baggage.get_baggage(ObservabilityKeys.SESSION_ID)
            session_id = session_id_value if isinstance(session_id_value, str) else None
            source = (
                "headers"
                if trace_id is not None and extracted_ctx is not current_ctx
                else ("span" if trace_id is not None else "none")
            )
            yield TraceContext(trace_id=trace_id, session_id=session_id, source=source)
    finally:
        # Flush buffered spans before the Lambda freezes (BatchSpanProcessor +
        # no auto-instrument flush wrapper). Guarded: the no-op default provider
        # has no force_flush; only an installed SDK provider does.
        provider = trace.get_tracer_provider()
        force_flush = getattr(provider, "force_flush", None)
        if callable(force_flush):
            force_flush()
        context.detach(token)
