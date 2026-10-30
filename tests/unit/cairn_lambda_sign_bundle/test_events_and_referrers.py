"""Tests for sign-bundle events emit helpers + referrer discovery."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from cairn_observability.scope import extract_trace_context
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(scope="session", autouse=True)
def _otel_tracer_provider() -> None:
    """Install an SDK TracerProvider so start_as_current_span yields valid
    SpanContexts (mirrors tests/unit/cairn_observability/test_scope.py)."""
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture(autouse=True)
def _isolate_context() -> None:
    """Snapshot/restore OTEL context per test so an active span/baggage cannot
    leak across tests within a worker."""
    token = context.attach(context.Context())
    yield
    context.detach(token)


@pytest.fixture(autouse=True)
def reset_module_clients() -> None:
    import cairn_lambda_sign_bundle.events as events_module
    import cairn_lambda_sign_bundle.referrers as referrers_module

    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
    yield
    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None


def test_emit_signed_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_signed({"manifestDigest": "sha256:" + "1" * 64})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.signed"
    assert entry["EventBusName"] == "B"
    assert json.loads(entry["Detail"])["manifestDigest"] == "sha256:" + "1" * 64


def test_emit_signing_failed_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_signing_failed({"reason": "x"})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.signing-failed"


def test_emit_raises_when_event_bus_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())
    with pytest.raises(RuntimeError, match="EVENT_BUS_NAME"):
        mod.emit_signed({})


def test_emit_injects_trace_context_resolvable_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit() seeds the serialized Detail with traceparent from the active span,
    so a downstream extract_trace_context resolves source="headers" / same id."""
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    detail = {"manifestDigest": "sha256:" + "1" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer") as span:
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        mod.emit_signed(detail)

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert "traceparent" in parsed
    assert parsed["manifestDigest"] == "sha256:" + "1" * 64
    resolved = extract_trace_context({"detail": parsed})
    assert resolved.source == "headers"
    assert resolved.trace_id == expected_trace_id


def test_emit_does_not_mutate_caller_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's detail dict is left untouched — trace keys live only in the
    serialized payload (shallow copy)."""
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())

    detail = {"manifestDigest": "sha256:" + "1" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        mod.emit_signed(detail)

    assert "traceparent" not in detail
    assert detail == {"manifestDigest": "sha256:" + "1" * 64}


def test_emit_filters_non_allow_listed_baggage_at_producer_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 'Injected baggage is restricted to the allow-list' at the PRODUCER
    EMIT boundary (not just the scope unit): with both an allow-listed key and a
    non-allow-listed (secret-shaped) key live in baggage when emit() runs, the
    serialized Detail's baggage header carries ONLY the allow-listed key — the
    non-allow-listed key and its value never reach the wire."""
    import cairn_lambda_sign_bundle.events as mod
    from cairn_observability.keys import ObservabilityKeys
    from opentelemetry import baggage

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        ctx = baggage.set_baggage(ObservabilityKeys.PROJECT_ID, "alpha-bank")
        ctx = baggage.set_baggage("secretToken", "hunter2", ctx)
        token = context.attach(ctx)
        try:
            mod.emit_signed({"manifestDigest": "sha256:" + "1" * 64})
        finally:
            context.detach(token)

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    baggage_header = parsed["baggage"]
    assert "projectId=alpha-bank" in baggage_header
    assert "secretToken" not in baggage_header
    assert "hunter2" not in baggage_header


def test_emit_no_active_span_downstream_falls_back_not_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 'No active span — carrier absent, downstream falls back', proven
    THROUGH a producer emit(): with no active span the serialized Detail carries
    no traceparent, AND a downstream consumer extracting that Detail does NOT
    resolve source="headers" — it falls through to its existing resolution order.
    Complements the scope-unit test by asserting the downstream fall-back, not
    just the absent carrier key."""
    import cairn_lambda_sign_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    # _isolate_context leaves no active span → emit from a span-less context.
    mod.emit_signed({"manifestDigest": "sha256:" + "1" * 64})

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert "traceparent" not in parsed
    resolved = extract_trace_context({"detail": parsed})
    assert resolved.source != "headers"
    assert resolved.trace_id is None


_NOTARY = "application/vnd.cncf.notary.signature"


def test_existing_signature_digest_returns_first_referrer(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {
        "referrers": [{"artifactType": _NOTARY, "imageDigest": "sha256:" + "f" * 64}],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    result = mod.existing_signature_digest(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/p/other",
        image_digest="sha256:" + "0" * 64,
    )
    assert result == "sha256:" + "f" * 64
    fake.list_image_referrers.assert_called_once()
    kwargs = fake.list_image_referrers.call_args.kwargs
    assert kwargs["subjectId"] == {"imageDigest": "sha256:" + "0" * 64}
    assert kwargs["registryId"] == "111122223333"


def test_existing_signature_digest_returns_none_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {"referrers": []}
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    result = mod.existing_signature_digest(
        registry_id="111122223333",
        repository_name="r",
        image_digest="sha256:" + "0" * 64,
    )
    assert result is None


def test_existing_signature_digest_filters_non_notary_artifact_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_lambda_sign_bundle.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.return_value = {
        "referrers": [
            {"artifactType": "application/vnd.example.other", "imageDigest": "sha256:" + "b" * 64},
            {"artifactType": _NOTARY, "imageDigest": "sha256:" + "a" * 64},
        ],
    }
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    assert (
        mod.existing_signature_digest(
            registry_id="x", repository_name="r", image_digest="sha256:" + "0" * 64
        )
        == "sha256:" + "a" * 64
    )


def test_existing_signature_digest_pages_via_next_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.referrers as mod

    fake = MagicMock()
    fake.list_image_referrers.side_effect = [
        {"referrers": [{"artifactType": "x", "imageDigest": "skip"}], "nextToken": "t1"},
        {"referrers": [{"artifactType": _NOTARY, "imageDigest": "sha256:" + "c" * 64}]},
    ]
    monkeypatch.setattr(mod, "_ecr", lambda: fake)

    assert (
        mod.existing_signature_digest(
            registry_id="x", repository_name="r", image_digest="sha256:" + "0" * 64
        )
        == "sha256:" + "c" * 64
    )
    assert fake.list_image_referrers.call_count == 2
    second_call_kwargs = fake.list_image_referrers.call_args_list[1].kwargs
    assert second_call_kwargs["nextToken"] == "t1"


def test_ecr_client_lazy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_sign_bundle.referrers as mod

    sentinel = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel)
    assert mod._ecr() is sentinel
    assert mod._ecr() is sentinel
