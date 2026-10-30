"""`observability_scope` force-flushes the tracer provider on exit.

The deployment uses a `BatchSpanProcessor` and the collector-only ADOT layer
has no auto-instrument wrapper to flush on Lambda freeze. So `observability_scope`
calls `trace.get_tracer_provider().force_flush()` in its exit path (guarded:
only when the provider exposes `force_flush`). These tests assert the flush
fires on both normal and exceptional exit, via a stub provider whose
`force_flush` is a spy, and that a provider WITHOUT `force_flush` (the no-op
default) does not raise.

All tests snapshot/restore the global tracer provider and OTel context so they
are xdist-parallel-safe.
"""

from __future__ import annotations

from collections.abc import Iterator

import opentelemetry.trace as trace_api
import pytest
from cairn_observability import scope as _scope_mod
from cairn_observability.scope import observability_scope
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture(autouse=True)
def _isolate_context() -> Iterator[None]:
    token = context.attach(context.Context())
    yield
    context.detach(token)


@pytest.fixture
def _reset_tracer_provider() -> Iterator[None]:
    """Snapshot + clear OTel's once-set provider globals so a test can install
    its own stub; restore on teardown (parallel-safe).

    Also snapshots/clears `scope._tracer._real_tracer`: that module-level
    ProxyTracer LAZILY caches the real tracer on its first `start_span`, so a
    sibling test that started a span under a different provider may have cached
    that provider's tracer. Clearing it forces the scope to re-resolve against
    the provider THIS test installs (the production cold-start ordering, where
    the provider is set at module-init before any span). Mirrors the discipline
    in `test_trace_export_contract.py::_real_xray_provider`."""
    saved_provider = trace_api._TRACER_PROVIDER
    saved_once = trace_api._TRACER_PROVIDER_SET_ONCE
    saved_real_tracer = _scope_mod._tracer._real_tracer
    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = trace_api.Once()
    _scope_mod._tracer._real_tracer = None
    try:
        yield
    finally:
        _scope_mod._tracer._real_tracer = saved_real_tracer
        trace_api._TRACER_PROVIDER = saved_provider
        trace_api._TRACER_PROVIDER_SET_ONCE = saved_once


class _SpyProvider(TracerProvider):
    """A real SDK provider (so spans get valid contexts) with a force_flush spy."""

    def __init__(self) -> None:
        super().__init__()
        self.flush_calls = 0

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_calls += 1
        return super().force_flush(timeout_millis)


def test_scope_force_flushes_provider_on_normal_exit(_reset_tracer_provider: None) -> None:
    provider = _SpyProvider()
    trace.set_tracer_provider(provider)

    with observability_scope({}):
        pass

    assert provider.flush_calls == 1, "observability_scope did not force_flush on normal exit"


def test_scope_force_flushes_provider_on_exceptional_exit(_reset_tracer_provider: None) -> None:
    provider = _SpyProvider()
    trace.set_tracer_provider(provider)

    with pytest.raises(RuntimeError, match="boom"):
        with observability_scope({}):
            raise RuntimeError("boom")

    assert provider.flush_calls == 1, "observability_scope did not force_flush on exceptional exit"


def test_scope_does_not_raise_when_provider_has_no_force_flush(
    _reset_tracer_provider: None,
) -> None:
    """OTel's default no-op `ProxyTracerProvider` has no `force_flush`. The
    guarded call must skip it silently (no AttributeError)."""

    class _NoFlushProvider:
        """Minimal provider stub exposing only get_tracer (no force_flush)."""

        def get_tracer(self, *args: object, **kwargs: object) -> object:
            return trace.NoOpTracer()

    trace_api._TRACER_PROVIDER = _NoFlushProvider()  # type: ignore[assignment]

    # Must not raise despite the provider lacking force_flush.
    with observability_scope({}):
        pass


def test_scope_exit_flush_actually_exports_buffered_span(_reset_tracer_provider: None) -> None:
    """Gap: the spy tests above prove `force_flush` was *invoked*, but never that
    invoking it actually drains the buffered span to an exporter. The whole point
    of the scope-exit flush (BatchSpanProcessor + no auto-instrument
    wrapper to flush on Lambda freeze) is that the just-closed `cairn.handler`
    span reaches the exporter BEFORE the execution environment is frozen — a
    buffered-but-unexported span is lost.

    Drive the real production processor topology — `BatchSpanProcessor` (which
    buffers and would NOT export synchronously on span end) wrapping an
    `InMemorySpanExporter` (stands in for the OTLP exporter; never touches the
    network). Assert that nothing is exported *inside* the scope (proving the
    batch processor really is buffering, so the flush is load-bearing) and that
    exactly one `cairn.handler` span is exported once the scope exits and its
    force_flush drains the buffer.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    with observability_scope({}):
        # BatchSpanProcessor buffers; the span has not even ended yet, and even
        # after it ends the batch processor would not export until its schedule
        # delay or an explicit flush. So nothing should be exported here.
        assert exporter.get_finished_spans() == (), (
            "BatchSpanProcessor exported mid-scope — the scope-exit flush would not be load-bearing"
        )

    finished = exporter.get_finished_spans()
    assert len(finished) == 1, (
        f"expected exactly one span exported via the scope-exit force_flush, got {len(finished)} "
        "(the buffered span was lost, or the flush did not drain the batch processor)"
    )
    assert finished[0].name == "cairn.handler"


def test_scope_exit_flush_exports_span_on_exceptional_exit(_reset_tracer_provider: None) -> None:
    """Gap (exception-path companion to the export test): on an exceptional exit
    the `cairn.handler` span must STILL be drained to the exporter. A handler
    that raises is exactly when the trace is most diagnostically valuable, so a
    flush that fired only on the happy path would lose the span that matters most.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    with pytest.raises(RuntimeError, match="boom"):
        with observability_scope({}):
            raise RuntimeError("boom")

    finished = exporter.get_finished_spans()
    assert len(finished) == 1, (
        "the cairn.handler span was not exported on exceptional scope exit "
        f"(got {len(finished)} spans) — exit-time flush missed the error path"
    )
    assert finished[0].name == "cairn.handler"
