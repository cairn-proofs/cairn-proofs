"""Tests for cairn_lambda_validate_bundle.events emit helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from cairn_observability.scope import extract_trace_context
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider


@pytest.fixture(autouse=True)
def reset_client() -> None:
    import cairn_lambda_validate_bundle.events as mod

    mod._EVENTS_CLIENT = None
    yield
    mod._EVENTS_CLIENT = None


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


def test_emit_validated_uses_detail_type_and_env_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_validated({"manifestDigest": "sha256:" + "0" * 64})

    fake.put_events.assert_called_once()
    kwargs = fake.put_events.call_args.kwargs
    entries = kwargs["Entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["DetailType"] == "artifact.validated"
    assert entry["EventBusName"] == "cairnproofs4af-artifact-cmh-alpha"
    assert entry["Source"] == "dev.cairnproofs.artifact"
    parsed = json.loads(entry["Detail"])
    assert parsed["manifestDigest"] == "sha256:" + "0" * 64


def test_emit_validation_failed_uses_failed_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_validation_failed({"manifestDigest": "sha256:" + "1" * 64})

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.validation-failed"


def test_emit_raises_when_event_bus_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())

    with pytest.raises(RuntimeError, match="EVENT_BUS_NAME"):
        mod.emit_validated({})


def test_default_events_client_lazy_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_validate_bundle.events as mod

    sentinel = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel)

    assert mod._events() is sentinel
    assert mod._events() is sentinel


# ── producer-side trace-context injection ───


def test_emit_injects_trace_context_resolved_as_headers_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The serialized Detail carries the active trace context such that a
    consumer's extract_trace_context resolves source="headers" with the same
    trace id (the inject→extract round-trip the design pins)."""
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
    monkeypatch.setattr(mod, "_events", lambda: fake)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("origin"):
        expected_trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        mod.emit_validated({"manifestDigest": "sha256:" + "0" * 64})

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert parsed["manifestDigest"] == "sha256:" + "0" * 64
    assert "traceparent" in parsed
    resolved = extract_trace_context({"detail": parsed})
    assert resolved.source == "headers"
    assert resolved.trace_id == expected_trace_id


def test_emit_does_not_mutate_callers_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injection happens on a shallow copy — the caller's dict is untouched."""
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
    monkeypatch.setattr(mod, "_events", lambda: fake)

    detail = {"manifestDigest": "sha256:" + "0" * 64}
    with trace.get_tracer("test").start_as_current_span("origin"):
        mod.emit_validated(detail)

    assert detail == {"manifestDigest": "sha256:" + "0" * 64}
    assert "traceparent" not in detail


def test_emit_no_active_span_emits_no_traceparent(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no valid span active, the carrier carries no malformed traceparent
    and the entry is still emitted (downstream falls back unchanged).

    The `_isolate_context` fixture leaves no active span, so the carrier omits
    the traceparent (symmetric to extraction's source="none")."""
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_validated({"manifestDigest": "sha256:" + "2" * 64})

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert "traceparent" not in parsed
    assert parsed["manifestDigest"] == "sha256:" + "2" * 64


def test_emit_still_raises_on_failed_entry_count_after_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EventEmitError guard is preserved: FailedEntryCount>0 still raises,
    injection only changes what is serialized."""
    import cairn_lambda_validate_bundle.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "cairnproofs4af-artifact-cmh-alpha")
    fake = MagicMock()
    fake.put_events.return_value = {
        "FailedEntryCount": 1,
        "Entries": [{"ErrorCode": "ThrottlingException", "ErrorMessage": "slow down"}],
    }
    monkeypatch.setattr(mod, "_events", lambda: fake)

    with pytest.raises(mod.EventEmitError, match="ThrottlingException"):
        mod.emit_validated({"manifestDigest": "sha256:" + "3" * 64})
