"""Tests for mint-tag events emit helpers + producer-side trace injection."""

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
    import cairn_lambda_mint_tag.events as events_module

    events_module._EVENTS_CLIENT = None
    yield
    events_module._EVENTS_CLIENT = None


def test_emit_tagged_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_mint_tag.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_tagged({"manifestDigest": "sha256:" + "a" * 64})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.tagged"
    assert entry["EventBusName"] == "B"
    assert json.loads(entry["Detail"])["manifestDigest"] == "sha256:" + "a" * 64


def test_emit_tag_failed_uses_correct_detail_type(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_mint_tag.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    mod.emit_tag_failed({"reason": "x"})
    entry = fake.put_events.call_args.kwargs["Entries"][0]
    assert entry["DetailType"] == "artifact.tag-failed"


def test_emit_raises_when_event_bus_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_mint_tag.events as mod

    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())
    with pytest.raises(RuntimeError, match="EVENT_BUS_NAME"):
        mod.emit_tagged({})


def test_emit_events_client_lazy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn_lambda_mint_tag.events as mod

    sentinel = object()
    monkeypatch.setattr("boto3.client", lambda *a, **k: sentinel)
    assert mod._events() is sentinel
    assert mod._events() is sentinel


def test_emit_injects_trace_context_resolvable_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit() seeds the serialized Detail with traceparent from the active span,
    so a downstream extract_trace_context resolves source="headers" / same id."""
    import cairn_lambda_mint_tag.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    fake = MagicMock()
    monkeypatch.setattr(mod, "_events", lambda: fake)

    detail = {"manifestDigest": "sha256:" + "a" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer") as span:
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        mod.emit_tagged(detail)

    entry = fake.put_events.call_args.kwargs["Entries"][0]
    parsed = json.loads(entry["Detail"])
    assert "traceparent" in parsed
    assert parsed["manifestDigest"] == "sha256:" + "a" * 64
    resolved = extract_trace_context({"detail": parsed})
    assert resolved.source == "headers"
    assert resolved.trace_id == expected_trace_id


def test_emit_does_not_mutate_caller_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's detail dict is left untouched — trace keys live only in the
    serialized payload (shallow copy)."""
    import cairn_lambda_mint_tag.events as mod

    monkeypatch.setenv("EVENT_BUS_NAME", "B")
    monkeypatch.setattr(mod, "_events", lambda: MagicMock())

    detail = {"manifestDigest": "sha256:" + "a" * 64}
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        mod.emit_tagged(detail)

    assert "traceparent" not in detail
    assert detail == {"manifestDigest": "sha256:" + "a" * 64}
