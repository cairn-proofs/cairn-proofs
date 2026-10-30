"""Cross-stage pipeline trace-correlation integration tests.

This module is the integration proof that the producer half
(`inject_trace_context`) and the consumer half (`extract_trace_context` /
`observability_scope`) compose end-to-end across simulated pipeline stage
hops. It drives the *real* helpers — no mocks — so it pins the two
correlate-pipeline-traces spec scenarios:

  - "Full pipeline push shares one root trace" — validate → sign → archive →
    update-metadata → mint-tag all resolve ONE root trace identity.
  - "Re-injection preserves span parentage across hops" — the
    `traceparent` a stage injects reflects ITS OWN current span id, not a
    verbatim echo of the upstream header, so the parent chain advances per hop
    while the trace id is preserved.

The TracerProvider install + per-test context-isolation fixtures mirror
`test_scope.py` exactly so the module is deterministic under `-n 0`.
"""

from __future__ import annotations

import pytest
from cairn_observability.keys import ObservabilityKeys
from cairn_observability.scope import (
    extract_trace_context,
    inject_trace_context,
    observability_scope,
)
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """Install an SDK TracerProvider so started spans carry valid
    SpanContexts (mirrors test_scope.py)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(autouse=True)
def _isolate_context() -> None:
    """Snapshot/restore OTEL context per test so baggage and active spans do
    not leak between tests (mirrors test_scope.py — needed for determinism
    under -n 0)."""
    token = context.attach(context.Context())
    yield
    context.detach(token)


_tracer = trace.get_tracer("test-pipeline-correlation")


def _parse_traceparent(traceparent: str) -> tuple[str, str]:
    """Return (trace_id, parent_span_id) from a W3C traceparent string.

    Format: ``00-<trace_id:32hex>-<parent_span_id:16hex>-<flags:2hex>``.
    """
    _version, trace_id, span_id, _flags = traceparent.split("-")
    return trace_id, span_id


# ── Re-injection preserves span parentage across hops ───────────────────────


def test_cross_stage_handoff_preserves_trace_and_baggage_over_three_hops() -> None:
    """A stage opens a span, sets allow-listed baggage, and injects a carrier.
    A second stage extracts it (source="headers", same trace id, baggage
    survives), re-injects, and a third stage extracts — the trace id is
    preserved across all hops."""
    # ── Stage 1 (validate): active span + allow-listed baggage → inject ──
    with _tracer.start_as_current_span("validate"):
        ctx = baggage.set_baggage(ObservabilityKeys.MANIFEST_DIGEST, "sha256:abc")
        ctx = baggage.set_baggage(ObservabilityKeys.SESSION_ID, "sess-handoff", ctx)
        token = context.attach(ctx)
        try:
            detail_1 = inject_trace_context({})
        finally:
            context.detach(token)
        stage1_trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")

    # ── Stage 2 (sign): extract upstream Detail ──
    resolved_2 = extract_trace_context({"detail": detail_1})
    assert resolved_2.source == "headers"
    assert resolved_2.trace_id == stage1_trace_id
    # Baggage (sessionId) survived the hop.
    assert resolved_2.session_id == "sess-handoff"

    # Stage 2 continues the trace under its own span, then re-injects.
    with observability_scope({"detail": detail_1}, event_type="sign"):
        detail_2 = inject_trace_context({})

    # ── Stage 3 (archive): extract stage-2 Detail — trace id preserved ──
    resolved_3 = extract_trace_context({"detail": detail_2})
    assert resolved_3.source == "headers"
    assert resolved_3.trace_id == stage1_trace_id


def test_reinjection_advances_parent_span_under_one_trace_id() -> None:
    """Design D3: a stage that continued an upstream trace re-injects from its
    OWN current span — the re-injected traceparent shares the trace id but
    carries a DIFFERENT (advanced) parent span id pointing at the emitting
    stage, not a verbatim echo of the upstream header."""
    # Upstream stage injects its carrier.
    with _tracer.start_as_current_span("validate"):
        upstream_detail = inject_trace_context({})
    upstream_trace_id, upstream_span_id = _parse_traceparent(upstream_detail["traceparent"])

    # Downstream stage continues that context under its own span, then
    # re-injects from the live (now-advanced) context.
    with observability_scope({"detail": upstream_detail}, event_type="sign") as ctx:
        downstream_detail = inject_trace_context({})
        active_span_id = format(trace.get_current_span().get_span_context().span_id, "016x")
        assert ctx.source == "headers"

    reinjected_trace_id, reinjected_span_id = _parse_traceparent(downstream_detail["traceparent"])

    # Trace id preserved across the hop …
    assert reinjected_trace_id == upstream_trace_id
    # … but the re-injected parent span id is the emitting stage's OWN span,
    # advanced past the upstream header (NOT a verbatim echo).
    assert reinjected_span_id != upstream_span_id
    assert reinjected_span_id == active_span_id


# ── Full pipeline push shares one root trace ────────────────────────────────


def test_full_pipeline_push_shares_one_root_trace() -> None:
    """Simulate validate → sign → archive → update-metadata → mint-tag. Each
    consumer extracts the upstream Detail, opens its own scope continuing that
    context, records its resolved trace id, and (for producer stages)
    re-injects to produce the next Detail. update_metadata is a terminal
    CONSUMER — it extracts but does not re-emit. All recorded trace ids must
    be identical: one root trace identity for the logical push."""
    recorded_trace_ids: list[str] = []

    # ── validate: root of the push (PutImage manifest). Active span, sets the
    # session baggage that rides the whole chain, then injects. ──
    with observability_scope({}, event_type="validate", manifest_digest="sha256:bundle") as ctx:
        ctx_with_session = baggage.set_baggage(ObservabilityKeys.SESSION_ID, "sess-push")
        token = context.attach(ctx_with_session)
        try:
            detail_validate = inject_trace_context({})
        finally:
            context.detach(token)
        recorded_trace_ids.append(ctx.trace_id)

    # ── sign: consumer of artifact.validated, producer of artifact.signed ──
    with observability_scope({"detail": detail_validate}, event_type="sign") as ctx:
        assert ctx.source == "headers"
        recorded_trace_ids.append(ctx.trace_id)
        detail_sign = inject_trace_context({})

    # ── archive: consumer of artifact.signed, producer of artifact.archived ──
    with observability_scope({"detail": detail_sign}, event_type="archive") as ctx:
        assert ctx.source == "headers"
        recorded_trace_ids.append(ctx.trace_id)
        detail_archive = inject_trace_context({})

    # ── update-metadata: TERMINAL consumer — extracts, does NOT re-emit ──
    with observability_scope({"detail": detail_archive}, event_type="update_metadata") as ctx:
        assert ctx.source == "headers"
        recorded_trace_ids.append(ctx.trace_id)

    # ── mint-tag: consumer of artifact.archived, producer of artifact.tagged ──
    with observability_scope({"detail": detail_archive}, event_type="mint_tag") as ctx:
        assert ctx.source == "headers"
        recorded_trace_ids.append(ctx.trace_id)
        inject_trace_context({})

    # One root trace identity across every hop of the logical push.
    assert len(recorded_trace_ids) == 5
    assert all(tid is not None for tid in recorded_trace_ids)
    assert len(set(recorded_trace_ids)) == 1, recorded_trace_ids


def test_full_pipeline_push_session_baggage_rides_the_whole_chain() -> None:
    """The allow-listed sessionId set at the validate root survives every hop,
    so the terminal mint-tag stage resolves the same session — confirming
    baggage (not just the trace id) propagates through the re-inject/extract
    chain across the full push."""
    with observability_scope({}, event_type="validate") as root:
        ctx_with_session = baggage.set_baggage(ObservabilityKeys.SESSION_ID, "sess-ride")
        token = context.attach(ctx_with_session)
        try:
            detail = inject_trace_context({})
        finally:
            context.detach(token)
        root_trace_id = root.trace_id

    # Walk the producer hops, re-injecting each time.
    for stage in ("sign", "archive", "mint_tag"):
        with observability_scope({"detail": detail}, event_type=stage):
            detail = inject_trace_context({})

    # Terminal extraction sees the same trace AND the same session baggage.
    final = extract_trace_context({"detail": detail})
    assert final.source == "headers"
    assert final.trace_id == root_trace_id
    assert final.session_id == "sess-ride"
