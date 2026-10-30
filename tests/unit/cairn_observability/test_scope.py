"""Tests for cairn_observability.scope — OTEL-baggage-backed observability_scope."""

from __future__ import annotations

import pytest
from cairn_observability.keys import ObservabilityKeys
from cairn_observability.scope import (
    TraceContext,
    extract_trace_context,
    inject_trace_context,
    observability_scope,
)
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider

VALID_TP = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
VALID_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """Install an SDK TracerProvider so tracer.start_as_current_span() creates
    spans with valid SpanContexts. Without this, OTEL's no-op default provider
    yields invalid SpanContexts and the scope's "headers" / "span" / "none"
    source resolution can't distinguish them."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(autouse=True)
def _isolate_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot OTEL context before each test, restore after. Mirrors the
    autouse-reset pattern from the `tests-parallel-safe` toolchain Requirement
    — baggage is ContextVar-backed but pytest runs sync tests as sequential
    function calls in the worker's main thread, so within-worker leakage of
    baggage entries between tests is possible without an explicit reset.

    Also clears `_X_AMZN_TRACE_ID` so the W3C/no-span paths are deterministic
    regardless of any X-Ray trace header in the runner's environment; tests that
    exercise the native-X-Ray adoption set it explicitly."""
    monkeypatch.delenv("_X_AMZN_TRACE_ID", raising=False)
    token = context.attach(context.Context())
    yield
    context.detach(token)


# ── extract_trace_context ──────────────────────────────────────────────────


def test_extract_from_root_level_traceparent() -> None:
    ctx = extract_trace_context({"traceparent": VALID_TP})
    assert ctx.trace_id == VALID_TRACE_ID
    assert ctx.source == "headers"


def test_extract_from_detail_traceparent() -> None:
    ctx = extract_trace_context({"detail": {"traceparent": VALID_TP}})
    assert ctx.trace_id == VALID_TRACE_ID
    assert ctx.source == "headers"


def test_extract_from_detail_headers_traceparent() -> None:
    ctx = extract_trace_context({"detail": {"headers": {"traceparent": VALID_TP}}})
    assert ctx.trace_id == VALID_TRACE_ID
    assert ctx.source == "headers"


def test_extract_includes_session_id_from_baggage_header() -> None:
    ctx = extract_trace_context(
        {"traceparent": VALID_TP, "baggage": "sessionId=sess-42,other=ignored"}
    )
    assert ctx.trace_id == VALID_TRACE_ID
    assert ctx.session_id == "sess-42"


def test_extract_returns_empty_on_no_traceparent_anywhere() -> None:
    ctx = extract_trace_context({"detail": {"other": "field"}})
    assert ctx == TraceContext(trace_id=None, session_id=None, source="none")


def test_extract_returns_empty_on_malformed_traceparent() -> None:
    ctx = extract_trace_context({"traceparent": "not-a-traceparent"})
    assert ctx.trace_id is None
    assert ctx.source == "none"


# ── observability_scope ────────────────────────────────────────────────────


def test_scope_hydrates_traceparent_and_yields_TraceContext() -> None:
    with observability_scope({"traceparent": VALID_TP}) as ctx:
        assert ctx.trace_id is not None
        # The scope STARTS a new span (child of the extracted one), so the
        # active span's trace_id matches the hydrated traceparent.
        assert ctx.trace_id == VALID_TRACE_ID


def test_scope_sets_baggage_for_caller_supplied_fields() -> None:
    with observability_scope(
        {},
        request_id="req-123",
        project_id="alpha-bank",
        manifest_digest="sha256:abc",
        event_type="validate",
    ):
        assert baggage.get_baggage(ObservabilityKeys.REQUEST_ID) == "req-123"
        assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) == "alpha-bank"
        assert baggage.get_baggage(ObservabilityKeys.MANIFEST_DIGEST) == "sha256:abc"
        assert baggage.get_baggage(ObservabilityKeys.EVENT_TYPE) == "validate"


def test_scope_extra_fields_restricted_to_allow_list() -> None:
    """`**extra_fields` is filtered against `ALLOWED_BAGGAGE_KEYS` — known
    keys land in baggage, unknown keys are silently dropped. Defense
    against future callers accidentally leaking secret material into
    baggage (which downstream propagates and is logged on every record)."""
    with observability_scope(
        {},
        artifactKind="assessment-results",
        custom_field="value",
        secretToken="leaked",
    ):
        # `artifactKind` is in the allow-list → present
        assert baggage.get_baggage(ObservabilityKeys.ARTIFACT_KIND) == "assessment-results"
        # `custom_field`, `secretToken` are NOT in the allow-list → dropped
        assert baggage.get_baggage("custom_field") is None
        assert baggage.get_baggage("secretToken") is None


def test_scope_clears_baggage_after_normal_exit() -> None:
    with observability_scope({}, project_id="alpha-bank"):
        assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) == "alpha-bank"
    assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) is None


def test_scope_clears_baggage_after_exceptional_exit() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with observability_scope({}, project_id="alpha-bank"):
            assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) == "alpha-bank"
            raise RuntimeError("boom")
    assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) is None


def test_scope_extracts_session_id_from_baggage_header() -> None:
    with observability_scope({"traceparent": VALID_TP, "baggage": "sessionId=sess-7"}) as ctx:
        assert ctx.session_id == "sess-7"


def test_scope_with_no_headers_yields_span_source() -> None:
    """With an SDK TracerProvider installed, the started span has a valid
    SpanContext, so the source resolves to "span" (not "none")."""
    with observability_scope({}) as ctx:
        # No traceparent in event → not "headers"; but the started span is
        # valid → "span" via the trace_id check.
        assert ctx.source == "span"
        assert ctx.trace_id is not None


def test_scope_stringifies_non_string_extra_fields() -> None:
    """Baggage entries are W3C strings; the scope coerces non-string values."""
    with observability_scope({}, request_id=42, project_id=None):
        assert baggage.get_baggage(ObservabilityKeys.REQUEST_ID) == "42"
        # project_id=None is filtered out (not added to baggage).
        assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID) is None


def test_scope_default_links_is_backward_compatible() -> None:
    """The optional `links` param defaults to None: the started span carries no
    links and behavior is identical to the prior (link-free) signature."""
    with observability_scope({}) as ctx:
        span = trace.get_current_span()
        assert ctx.source == "span"
        assert len(getattr(span, "links", [])) == 0


def test_scope_records_provided_span_link() -> None:
    """When links are passed (the fan-out skip-path correlation), the
    started span records them — the OTEL-idiomatic 'causally related, separately
    rooted' relationship to the originating bundle trace."""
    origin_ctx = trace.SpanContext(
        trace_id=int(VALID_TRACE_ID, 16),
        span_id=0xB7AD6B7169203331,
        is_remote=True,
        trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
    )
    link = trace.Link(origin_ctx)
    with observability_scope({}, links=[link]):
        span = trace.get_current_span()
        recorded = list(getattr(span, "links", []))
        assert len(recorded) == 1
        assert recorded[0].context.trace_id == int(VALID_TRACE_ID, 16)


# ── inject_trace_context (carrier-shape contract) ───────────────────────────


def test_inject_extract_round_trip_preserves_trace_and_baggage() -> None:
    """inject→extract round-trip: a carrier written by the producer side
    resolves to the SAME trace_id with source="headers" on the consumer side,
    and allow-listed baggage survives the hop. Pins the T3 carrier-shape
    contract that lets the producer waves wire emit() safely."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer") as span:
        # Allow-listed baggage entries set on the active context.
        ctx = baggage.set_baggage(ObservabilityKeys.MANIFEST_DIGEST, "sha256:abc")
        ctx = baggage.set_baggage(ObservabilityKeys.SESSION_ID, "sess-99", ctx)
        token = context.attach(ctx)
        try:
            carrier = inject_trace_context()
        finally:
            context.detach(token)

        expected_trace_id = format(span.get_span_context().trace_id, "032x")

    # Consumer probes `detail` as a carrier (mirrors _candidate_carriers).
    resolved = extract_trace_context({"detail": carrier})
    assert resolved.trace_id == expected_trace_id
    assert resolved.source == "headers"
    assert resolved.session_id == "sess-99"
    # manifestDigest baggage round-trips intact (`:` is percent-encoded per the
    # W3C baggage value grammar — sha256:abc → sha256%3Aabc on the wire).
    assert "manifestDigest=sha256%3Aabc" in carrier["baggage"]


def test_inject_filters_baggage_to_allow_list() -> None:
    """A baggage key outside ALLOWED_BAGGAGE_KEYS is NOT serialized into the
    carrier (prevents leaking secret material downstream); an allowed key
    alongside it survives."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        ctx = baggage.set_baggage(ObservabilityKeys.PROJECT_ID, "alpha-bank")
        ctx = baggage.set_baggage("secret", "hunter2", ctx)
        ctx = baggage.set_baggage("password", "letmein", ctx)
        token = context.attach(ctx)
        try:
            carrier = inject_trace_context()
        finally:
            context.detach(token)

    baggage_header = carrier["baggage"]
    assert "projectId=alpha-bank" in baggage_header
    assert "secret" not in baggage_header
    assert "password" not in baggage_header
    assert "hunter2" not in baggage_header
    assert "letmein" not in baggage_header


def test_inject_with_no_active_span_omits_traceparent() -> None:
    """With no valid current span, the carrier carries no (malformed)
    traceparent — symmetric to extraction's source="none" — and feeding it
    back through extraction does not yield source="headers"."""
    # _isolate_context fixture leaves us with no active span here.
    carrier = inject_trace_context()
    assert "traceparent" not in carrier

    resolved = extract_trace_context({"detail": carrier})
    assert resolved.source != "headers"


def test_inject_into_existing_dict_preserves_other_keys() -> None:
    """Passing a pre-populated dict (an event Detail) preserves its existing
    keys and adds the trace keys."""
    detail = {"bundleDigest": "sha256:deadbeef", "stage": "validate"}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        returned = inject_trace_context(detail)

    # Same dict, mutated in place, original keys intact.
    assert returned is detail
    assert detail["bundleDigest"] == "sha256:deadbeef"
    assert detail["stage"] == "validate"
    assert "traceparent" in detail


def test_inject_overwrites_stale_traceparent_in_carrier() -> None:
    """A carrier already carrying a (stale, upstream) traceparent is OVERWRITTEN
    with the emitting stage's live context — never left echoing an upstream id.
    Pins the carrier-key behavior: re-emitting a Detail that already
    holds a traceparent must reflect THIS stage's span, so the downstream parent
    chain advances rather than re-pointing at a stale upstream span."""
    stale = "00-11111111111111111111111111111111-2222222222222222-01"
    carrier = {"traceparent": stale, "stage": "sign"}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer") as span:
        live_trace_id = format(span.get_span_context().trace_id, "032x")
        inject_trace_context(carrier)

    # The stale upstream traceparent is gone; the live context took its place.
    assert carrier["traceparent"] != stale
    resolved = extract_trace_context({"detail": carrier})
    assert resolved.source == "headers"
    assert resolved.trace_id == live_trace_id
    # Unrelated keys are preserved through the overwrite.
    assert carrier["stage"] == "sign"


def test_inject_clears_stale_carrier_keys_when_propagator_declines() -> None:
    """Injection is authoritative even when the propagator declines to write.
    A carrier re-used from an extracted upstream Detail may already hold a
    `traceparent`/`baggage`; with no active span (traceparent) and no
    allow-listed baggage, the propagator writes nothing, so the helper MUST
    clear the stale keys rather than leave them to propagate downstream."""
    stale_tp = "00-11111111111111111111111111111111-2222222222222222-01"
    stale_bag = "projectId=ghost-project"
    carrier = {"traceparent": stale_tp, "baggage": stale_bag, "stage": "replay"}

    # _isolate_context leaves no active span and no baggage in scope.
    inject_trace_context(carrier)

    assert "traceparent" not in carrier
    assert "baggage" not in carrier
    # Non-carrier keys survive the clear.
    assert carrier["stage"] == "replay"
    # And it does not falsely resolve as headers downstream.
    assert extract_trace_context({"detail": carrier}).source != "headers"


def test_inject_baggage_with_comma_value_round_trips_cleanly() -> None:
    """A baggage value containing the W3C list delimiter (`,`) is percent-encoded
    on inject and decoded on extract — the comma must NOT split the value into a
    spurious second baggage entry. Guards the carrier-shape contract for values
    that collide with baggage-header grammar."""
    tricky = "alpha,bank,inc"
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        ctx = baggage.set_baggage(ObservabilityKeys.PROJECT_ID, tricky)
        token = context.attach(ctx)
        try:
            carrier = inject_trace_context()
        finally:
            context.detach(token)

    # The raw delimiter is escaped on the wire (not a bare comma in the value).
    assert tricky not in carrier["baggage"]
    # …and it decodes back to the exact original on the consumer side.
    resolved = extract_trace_context({"detail": carrier})
    # projectId is not surfaced on TraceContext, so probe the decoded baggage
    # directly via the propagator's extraction into a fresh context.
    from opentelemetry.baggage.propagation import W3CBaggagePropagator

    extracted = W3CBaggagePropagator().extract(carrier=carrier)
    assert baggage.get_baggage(ObservabilityKeys.PROJECT_ID, extracted) == tricky
    assert resolved.source == "headers"


# ── native X-Ray trace adoption (one trace per push) ─────────────────────────


def test_scope_adopts_native_xray_trace_over_w3c(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the Lambda runtime's `_X_AMZN_TRACE_ID` is present, the span parents
    off the native X-Ray trace so our OTEL span JOINS the one X-Ray trace the
    native AWS::Lambda segment uses — overriding any W3C traceparent carried in
    the event. This collapses the dual trace planes (native + OTEL) into a
    single console trace per push. Baggage still flows."""
    monkeypatch.setenv(
        "_X_AMZN_TRACE_ID",
        "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1",
    )
    # Event carries a DIFFERENT W3C trace + baggage — X-Ray must win for the
    # span identity, but baggage must still be hydrated.
    event = {
        "detail": {
            "traceparent": VALID_TP,
            "baggage": "manifestDigest=sha256%3Aabc",
        }
    }
    with observability_scope(event, request_id="r1") as tc:
        assert tc.trace_id == "5759e988bd862e3fe1be46a994272793"
        assert tc.trace_id != VALID_TRACE_ID  # the W3C carrier did NOT win
        assert baggage.get_baggage(ObservabilityKeys.MANIFEST_DIGEST) == "sha256:abc"
        assert baggage.get_baggage(ObservabilityKeys.REQUEST_ID) == "r1"


def test_scope_falls_back_to_w3c_when_no_xray_header() -> None:
    """With no `_X_AMZN_TRACE_ID` (cleared by the autouse fixture), the span
    parents off the W3C traceparent in the event — unchanged non-Lambda
    behavior, so the cross-stage W3C correlation path is preserved."""
    event = {"detail": {"traceparent": VALID_TP}}
    with observability_scope(event) as tc:
        assert tc.trace_id == VALID_TRACE_ID
        assert tc.source == "headers"


def test_scope_ignores_malformed_xray_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed/invalid `_X_AMZN_TRACE_ID` does not override the W3C parent —
    the invalid X-Ray span context is rejected and the W3C trace is kept."""
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "not-a-valid-xray-header")
    event = {"detail": {"traceparent": VALID_TP}}
    with observability_scope(event) as tc:
        assert tc.trace_id == VALID_TRACE_ID
