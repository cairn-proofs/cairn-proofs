"""Tests for cairn_observability.tracing — manual SDK TracerProvider bootstrap.

The deployment uses a collector-only ADOT layer (no Python SDK), so the
application installs the SDK `TracerProvider` itself via `configure_tracing()`.
These tests assert the provider shape (X-Ray id generator + an OTLP
BatchSpanProcessor), idempotency, and parallel-safety. The global tracer
provider is process-global state, so every test snapshots and restores it.

`configure_tracing()`'s guard sets the provider only when the current one is
not already an SDK `TracerProvider`. OTel forbids re-`set_tracer_provider` after
the global is set (it logs and ignores the second call), so to test a fresh
install we reach into `opentelemetry.trace`'s private globals to clear them —
the documented test idiom for provider-install tests — and always restore.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from unittest import mock

import boto3
import opentelemetry.trace as trace_api
import pytest
from botocore.stub import Stubber
from cairn_observability.tracing import (
    _OTLP_EXPORT_TIMEOUT_SEC,
    _OTLP_TRACES_ENDPOINT,
    configure_tracing,
)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.trace import SynchronousMultiSpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


@pytest.fixture
def _reset_tracer_provider() -> Iterator[None]:
    """Snapshot the global tracer provider; clear OTel's once-set globals so a
    fresh `configure_tracing()` install runs; restore everything on teardown.

    OTel caches the global provider in `_TRACER_PROVIDER` and latches
    `_TRACER_PROVIDER_SET_ONCE`; without clearing the latch, a second
    `set_tracer_provider` is ignored. We restore both so no global state leaks
    across xdist workers / sibling suites (test_scope, test_trace_export_contract
    install their own session providers).

    `configure_tracing()` now ALSO instruments `BotocoreInstrumentor` (a
    process-wide singleton patch on botocore's client API). So this fixture also
    uninstruments botocore on teardown — any tracing test that ran
    `configure_tracing()` must leave that global patch reverted, or it leaks
    forward to sibling suites under `-n auto` (uninstrument is a no-op when not
    instrumented)."""
    saved_provider = trace_api._TRACER_PROVIDER
    saved_once = trace_api._TRACER_PROVIDER_SET_ONCE
    # Reset to a pristine "never set" state so configure_tracing installs fresh.
    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE = trace_api.Once()
    try:
        yield
    finally:
        BotocoreInstrumentor().uninstrument()
        trace_api._TRACER_PROVIDER = saved_provider
        trace_api._TRACER_PROVIDER_SET_ONCE = saved_once


def _span_processors(provider: TracerProvider) -> list[object]:
    """The SDK keeps active span processors under a multi-processor; reach in to
    enumerate them (no public accessor in the SDK)."""
    multi = provider._active_span_processor
    if isinstance(multi, SynchronousMultiSpanProcessor):
        return list(multi._span_processors)
    # Some SDK builds use the concurrent variant; fall back to its tuple/list.
    return list(getattr(multi, "_span_processors", []))


@pytest.fixture
def _uninstrument_botocore() -> Iterator[None]:
    """Guarantee a pristine `BotocoreInstrumentor` state around the test, on BOTH
    setup and teardown.

    `BotocoreInstrumentor` monkeypatches botocore's client API GLOBALLY (it is a
    process-wide singleton). Two leak directions must be closed:

    - *Inbound*: a sibling suite that calls `configure_tracing()` (e.g.
      `test_trace_export_contract`) leaves botocore instrumented and bound to
      THAT test's provider. If we don't uninstrument at setup, our own
      `configure_tracing()` call sees botocore already instrumented and its
      `instrument()` no-ops — leaving the patch bound to the stale provider, so
      our in-memory exporter never sees the AWS span. Uninstrumenting at setup
      forces our `configure_tracing()` to rebind botocore to OUR fresh provider.
    - *Outbound*: we uninstrument at teardown so our patch never leaks forward.

    Uninstrument on a never-instrumented instrumentor is a harmless no-op."""
    BotocoreInstrumentor().uninstrument()
    try:
        yield
    finally:
        BotocoreInstrumentor().uninstrument()


def _configure_with_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Run `configure_tracing()` and attach a synchronous in-memory exporter to
    the installed SDK provider so spans can be inspected in-test.
    `configure_tracing()` registers a `BatchSpanProcessor` (async export to the
    collector); we add a `SimpleSpanProcessor` alongside it so emitted spans are
    captured synchronously without standing up the OTLP endpoint."""
    configure_tracing()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _stubbed_dynamodb_client() -> tuple[object, Stubber]:
    """A boto3 DynamoDB client with a `Stubber` (no network, no real creds)."""
    client = boto3.client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    return client, Stubber(client)


# Body content that MUST NOT appear in any subsegment attribute under the
# default (metadata-only) instrumentor config.
_SENSITIVE_ITEM = {"secret": {"S": "do-not-capture-this-plaintext"}}


def test_configure_tracing_installs_sdk_provider_with_xray_idgen(
    _reset_tracer_provider: None,
) -> None:
    """`configure_tracing()` installs an SDK `TracerProvider` whose
    `id_generator` is an `AwsXRayIdGenerator` (X-Ray rejects non-time-prefixed
    trace IDs)."""
    configure_tracing()

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert isinstance(provider.id_generator, AwsXRayIdGenerator)


def test_configure_tracing_registers_otlp_batch_processor(
    _reset_tracer_provider: None,
) -> None:
    """A `BatchSpanProcessor` wrapping an `OTLPSpanExporter` pointed at the
    collector's localhost OTLP/HTTP receiver is registered."""
    configure_tracing()

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    processors = _span_processors(provider)
    batch_processors = [p for p in processors if isinstance(p, BatchSpanProcessor)]
    assert len(batch_processors) == 1, (
        f"expected exactly one BatchSpanProcessor, got {processors!r}"
    )
    exporter = batch_processors[0].span_exporter
    assert isinstance(exporter, OTLPSpanExporter)
    assert _OTLP_TRACES_ENDPOINT == "http://localhost:4318/v1/traces"


def test_configure_tracing_is_idempotent_no_duplicate_processor(
    _reset_tracer_provider: None,
) -> None:
    """A second call must NOT re-install the provider or add a duplicate
    `BatchSpanProcessor` (warm-Lambda reuse). The provider object stays
    identical and the processor count does not grow."""
    configure_tracing()
    provider_first = trace.get_tracer_provider()
    processors_first = _span_processors(provider_first)  # type: ignore[arg-type]

    configure_tracing()  # second call — should be a no-op

    provider_second = trace.get_tracer_provider()
    assert provider_second is provider_first, "second configure_tracing() replaced the provider"
    processors_second = _span_processors(provider_second)  # type: ignore[arg-type]
    assert len(processors_second) == len(processors_first) == 1, (
        "second call added a duplicate span processor"
    )


def test_configure_tracing_no_op_when_sdk_provider_already_installed(
    _reset_tracer_provider: None,
) -> None:
    """If some other code already installed an SDK `TracerProvider` (e.g.
    test_scope's session fixture), `configure_tracing()` leaves it untouched
    rather than clobbering it — the guard keys on the SDK-provider type."""
    preexisting = TracerProvider()
    trace.set_tracer_provider(preexisting)

    configure_tracing()

    assert trace.get_tracer_provider() is preexisting
    # The pre-existing plain provider had no X-Ray generator; configure_tracing
    # did not swap it.
    assert not isinstance(preexisting.id_generator, AwsXRayIdGenerator)


def test_configure_tracing_started_span_has_xray_formatted_trace_id(
    _reset_tracer_provider: None,
) -> None:
    """Gap: the existing tests assert the provider's ``id_generator`` IS an
    ``AwsXRayIdGenerator`` (a type check on a stored attribute) but never prove
    the generator is actually WIRED into span creation. A regression that
    constructed the ``TracerProvider`` WITHOUT passing ``id_generator=`` (or
    passed it to the wrong kwarg) would leave the type assertion green — the
    attribute would just be unused — while spans silently got random,
    non-time-prefixed IDs that X-Ray rejects.

    Start a real span through the installed provider and assert its trace ID is
    X-Ray-formatted: the high 32 bits are the trace's creation epoch seconds
    (``AwsXRayIdGenerator`` does ``(int(time.time()) << 96) + getrandbits(96)``).
    We bound the high word to a window around "now" rather than an exact match
    to stay robust across the second tick between span start and the assertion.
    """
    before = int(time.time())
    configure_tracing()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)

    tracer = provider.get_tracer("gap-test")
    with tracer.start_as_current_span("gap-span") as span:
        ctx = span.get_span_context()
    after = int(time.time())

    assert ctx.is_valid, "span context must be valid under the SDK provider"
    assert ctx.trace_id != 0, "trace id must be non-zero (recording span)"

    high_32 = ctx.trace_id >> 96
    # The X-Ray epoch prefix is the creation time captured during start_span;
    # it must fall within [before, after] (inclusive) — proving the X-Ray id
    # generator, not the default random 128-bit generator, produced this id.
    assert before <= high_32 <= after, (
        f"trace id high-32 bits {high_32} not a plausible creation epoch "
        f"in [{before}, {after}] — AwsXRayIdGenerator is not wired into span creation"
    )


def test_configure_tracing_otlp_exporter_uses_bounded_timeout(
    _reset_tracer_provider: None,
) -> None:
    """Gap: the existing processor test asserts the exporter IS an
    ``OTLPSpanExporter`` pointed at the localhost endpoint, but never asserts the
    bounded per-export ``timeout`` is actually passed through. The module docstring
    and ``_OTLP_EXPORT_TIMEOUT_SEC = 1`` are load-bearing: they cap how long a
    ``observability_scope`` exit-time ``force_flush`` can block when the collector
    is briefly unreachable (the OTLP HTTP retry loop is deadline-based, bounded by
    this ``timeout``). A regression that dropped ``timeout=`` from the
    ``OTLPSpanExporter(...)`` construction would revert to the multi-second default
    backoff budget — adding several seconds to every invocation on a collector
    hiccup — while every existing assertion (endpoint, type, X-Ray id) stayed green.

    Assert the installed exporter's effective timeout equals the bounded module
    constant (1s), and that the constant itself is the short bound the docstring
    promises (<= 1s)."""
    assert _OTLP_EXPORT_TIMEOUT_SEC == 1, (
        "the OTLP export timeout must stay the short ~1s bound documented in "
        "tracing.py — a longer value lets a collector hiccup block scope-exit flush"
    )

    configure_tracing()

    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    processors = _span_processors(provider)
    batch_processors = [p for p in processors if isinstance(p, BatchSpanProcessor)]
    assert len(batch_processors) == 1
    exporter = batch_processors[0].span_exporter
    assert isinstance(exporter, OTLPSpanExporter)
    # The OTLP HTTP exporter keeps the per-export deadline on its HTTP client.
    timeout = exporter._client._timeout
    assert timeout == _OTLP_EXPORT_TIMEOUT_SEC, (
        f"OTLPSpanExporter timeout {timeout!r} != bounded constant "
        f"{_OTLP_EXPORT_TIMEOUT_SEC!r} — the `timeout=` kwarg was dropped, "
        "reverting to the multi-second default backoff that scope-exit flush would block on"
    )


def _aws_child_spans(exporter: InMemorySpanExporter) -> list[object]:
    """Finished spans the botocore instrumentor emitted (rpc.system == aws-api),
    i.e. excluding any handler span we opened ourselves."""
    return [s for s in exporter.get_finished_spans() if s.attributes.get("rpc.system") == "aws-api"]


def test_botocore_call_emits_child_subsegment_under_handler_span(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """A boto3 call made under an active recording handler span emits a child
    span identifying the AWS service/operation, parented to the handler span.

    (Spec: "AWS SDK call emits a child subsegment under the handler span".)
    """
    provider, exporter = _configure_with_exporter()

    client, stubber = _stubbed_dynamodb_client()
    stubber.add_response("put_item", {}, {"TableName": "t", "Item": _SENSITIVE_ITEM})
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler") as handler:
            handler_span_id = handler.get_span_context().span_id
            client.put_item(TableName="t", Item=_SENSITIVE_ITEM)

    aws_spans = _aws_child_spans(exporter)
    assert len(aws_spans) == 1, f"expected exactly one AWS child span, got {aws_spans!r}"
    aws = aws_spans[0]
    # Service/operation are carried in the standard rpc.* attributes.
    assert aws.attributes["rpc.service"] == "DynamoDB"
    assert aws.attributes["rpc.method"] == "PutItem"
    # Nested under the handler span (X-Ray subsegment).
    assert aws.parent is not None and aws.parent.span_id == handler_span_id, (
        "AWS child span is not parented to the handler span"
    )


def test_botocore_faulting_call_marks_child_span_error(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """A faulting/throttled boto3 call marks its child span with error status.

    (Spec: "Faulting AWS call marks the subsegment with error/fault status".)
    """
    provider, exporter = _configure_with_exporter()

    client, stubber = _stubbed_dynamodb_client()
    stubber.add_client_error(
        "put_item",
        service_error_code="ProvisionedThroughputExceededException",
        service_message="throttled",
        http_status_code=400,
    )
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler"):
            with pytest.raises(Exception):  # noqa: B017 — botocore ClientError subclass
                client.put_item(TableName="t", Item={"pk": {"S": "x"}})

    aws_spans = _aws_child_spans(exporter)
    assert len(aws_spans) == 1
    assert aws_spans[0].status.status_code == StatusCode.ERROR, (
        "throttled/faulting AWS call must mark its child span StatusCode.ERROR"
    )


def test_botocore_faulting_call_records_error_detail_under_handler(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """Gap (Scenario 2 hardening): the existing fault test asserts ONLY
    ``status.status_code == StatusCode.ERROR``. The spec scenario requires the
    error span to *carry the error detail* and be *nested under the originating
    stage's handler span* ("attributable to that specific call and stage without
    log spelunking"). A regression that marked the span ERROR but dropped the
    error code/message (or recorded no exception, or detached the span from the
    handler) would leave the status-only assertion green while destroying the
    attributability the requirement exists to provide.

    Assert: (a) the failing operation's error code surfaces in the span's status
    description, (b) the instrumentor recorded an ``exception`` event carrying
    the botocore error type, and (c) the error span is parented to the handler
    span — not an orphan root.
    """
    provider, exporter = _configure_with_exporter()

    client, stubber = _stubbed_dynamodb_client()
    stubber.add_client_error(
        "put_item",
        service_error_code="ProvisionedThroughputExceededException",
        service_message="throttled",
        http_status_code=400,
    )
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler") as handler:
            handler_span_id = handler.get_span_context().span_id
            with pytest.raises(Exception):  # noqa: B017 — botocore ClientError subclass
                client.put_item(TableName="t", Item={"pk": {"S": "x"}})

    aws_spans = _aws_child_spans(exporter)
    assert len(aws_spans) == 1
    aws = aws_spans[0]

    # (a) The error code must be carried in the status detail, so the failure is
    # attributable to the exact service error rather than a bare ERROR flag.
    assert aws.status.status_code == StatusCode.ERROR
    assert "ProvisionedThroughputExceededException" in (aws.status.description or ""), (
        "error span must carry the failing operation's error code in its status "
        f"description; got {aws.status.description!r}"
    )

    # (b) The instrumentor must have recorded the exception itself on the span.
    exception_events = [e for e in aws.events if e.name == "exception"]
    assert len(exception_events) == 1, (
        f"expected exactly one recorded exception event, got {aws.events!r}"
    )
    assert "ProvisionedThroughputExceededException" in (
        exception_events[0].attributes.get("exception.type", "")
    ), "recorded exception event must identify the botocore error type"

    # (c) The error span is a subsegment of the handler span, not an orphan root.
    assert aws.parent is not None and aws.parent.span_id == handler_span_id, (
        "faulting AWS child span is not parented to the handler span — the "
        "failure would not be attributable to the originating stage"
    )


def test_configure_tracing_twice_instruments_botocore_once(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """`configure_tracing()` invoked twice installs `BotocoreInstrumentor`
    exactly once — no duplicate instrumentation, no duplicate subsegments.

    (Spec: "Instrumentation installed once per process, idempotent".)
    """
    configure_tracing()
    assert BotocoreInstrumentor().is_instrumented_by_opentelemetry is True

    configure_tracing()  # second call — past the early-return guard, a no-op
    # Singleton flag stays True (not toggled / re-applied).
    assert BotocoreInstrumentor().is_instrumented_by_opentelemetry is True

    # A third configure_tracing() here is likewise a guarded no-op; it just
    # narrows the provider type and attaches the in-memory exporter.
    provider, exporter = _configure_with_exporter()

    client, stubber = _stubbed_dynamodb_client()
    stubber.add_response("put_item", {}, {"TableName": "t", "Item": {"pk": {"S": "x"}}})
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler"):
            client.put_item(TableName="t", Item={"pk": {"S": "x"}})

    # Double-instrumentation would emit two child spans for the single call.
    assert len(_aws_child_spans(exporter)) == 1, (
        "a single boto3 call produced more than one AWS child span — botocore "
        "was double-instrumented"
    )


def test_configure_tracing_guard_blocks_second_botocore_instrument(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """`test_configure_tracing_twice_instruments_botocore_once`
    proves the *behavior* (one child span for one call) but cannot isolate WHICH
    guard produced it — the early-return guard in `configure_tracing()` and
    `BotocoreInstrumentor`'s own internal `is_instrumented_by_opentelemetry` latch
    both independently suppress a duplicate. If the early-return guard regressed,
    that behavior test would still pass on the instrumentor's latch alone.

    This test pins the guard the Requirement names: patch
    `BotocoreInstrumentor.instrument` and assert `configure_tracing()`'s own guard
    stops the SECOND call from ever reaching `.instrument()` — exactly once across
    two invocations, proving idempotency is enforced by our guard, not delegated
    to the instrumentor's internal latch.
    """
    with mock.patch.object(BotocoreInstrumentor, "instrument") as instrument:
        configure_tracing()
        configure_tracing()  # second call hits the early-return guard

    assert instrument.call_count == 1, (
        "configure_tracing()'s early-return guard did not block the second "
        f"BotocoreInstrumentor.instrument() — called {instrument.call_count}x"
    )
    # And the one call binds to the explicitly-constructed provider, not
    # global resolution.
    bound = instrument.call_args.kwargs.get("tracer_provider")
    assert isinstance(bound, TracerProvider), (
        "instrument() must bind to the constructed SDK provider via "
        f"tracer_provider=; got {bound!r}"
    )


def test_botocore_call_with_no_recording_span_does_not_raise_or_orphan(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """A boto3 call with no recording span active (default no-op provider)
    produces a non-recording child span without raising and emits no orphan
    root subsegment.

    (Spec: "No recording span active — no error, no orphan subsegment".)
    """
    # Do NOT call configure_tracing(): the global provider stays the default
    # no-op ProxyTracerProvider. Instrument under that no-op provider, mirroring
    # a module-init boto3 call before any scope opens.
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    BotocoreInstrumentor().instrument()

    assert trace.get_current_span().is_recording() is False
    client, stubber = _stubbed_dynamodb_client()
    stubber.add_response("put_item", {}, {"TableName": "t", "Item": {"pk": {"S": "x"}}})
    with stubber:
        # No handler span open; under the no-op provider this must not raise and
        # must leave call behavior unchanged.
        client.put_item(TableName="t", Item={"pk": {"S": "x"}})

    # The no-op provider records nothing — there is no recording root subsegment.
    assert trace.get_current_span().is_recording() is False


def test_botocore_isolation_fixture_leaves_clean_state(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """Gap (xdist isolation): `BotocoreInstrumentor` patches botocore's client
    API GLOBALLY (a process-wide singleton). The whole suite's correctness under
    `-n auto` rests on the load-bearing invariant that the isolation fixtures
    leave botocore UNINSTRUMENTED at the boundary, so the global patch never
    leaks into a sibling test (botocore or not) that the same xdist worker runs
    afterward. This test pins that invariant directly rather than leaving it
    implicit in fixture teardown.

    At entry (after `_uninstrument_botocore` setup runs) botocore must be in a
    clean, uninstrumented state. We then instrument + run a call (exercising the
    patch), and assert that the SAME fixture-mediated `uninstrument()` returns
    botocore to clean state — modelling what every botocore test relies on at
    teardown so the next worker-local test sees no stale patch.
    """
    # Entry invariant: the fixture setup must have produced a clean global.
    assert BotocoreInstrumentor().is_instrumented_by_opentelemetry is False, (
        "isolation fixture did not present a clean botocore state at test entry "
        "— a prior test's global patch leaked forward"
    )

    # Install + exercise the global patch, then revert exactly as a botocore
    # test's teardown does, and prove the revert is complete.
    provider, exporter = _configure_with_exporter()
    client, stubber = _stubbed_dynamodb_client()
    stubber.add_response("put_item", {}, {"TableName": "t", "Item": {"pk": {"S": "x"}}})
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler"):
            client.put_item(TableName="t", Item={"pk": {"S": "x"}})
    assert len(_aws_child_spans(exporter)) == 1, "patch was not actually active mid-test"

    BotocoreInstrumentor().uninstrument()
    assert BotocoreInstrumentor().is_instrumented_by_opentelemetry is False, (
        "uninstrument() left botocore instrumented — the global patch would leak "
        "to the next test on this xdist worker"
    )

    # A fresh client built post-uninstrument emits NO AWS subsegment: proof the
    # global API patch is genuinely gone, not merely flagged off.
    post_exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(post_exporter))
    client2, stubber2 = _stubbed_dynamodb_client()
    stubber2.add_response("put_item", {}, {"TableName": "t", "Item": {"pk": {"S": "x"}}})
    tracer2 = provider.get_tracer("test")
    with stubber2:
        with tracer2.start_as_current_span("handler2"):
            client2.put_item(TableName="t", Item={"pk": {"S": "x"}})
    assert _aws_child_spans(post_exporter) == [], (
        "an AWS subsegment was still emitted after uninstrument() — the global "
        "botocore patch leaked past teardown"
    )


def test_botocore_subsegment_carries_no_request_response_payload(
    _reset_tracer_provider: None,
    _uninstrument_botocore: None,
) -> None:
    """Regression: under the DEFAULT instrumentor
    config, no request/response payload (item content, S3 body, KMS plaintext)
    is captured on the child span — metadata-only capture is intentional.
    """
    provider, exporter = _configure_with_exporter()

    client, stubber = _stubbed_dynamodb_client()
    # The item carries a distinctive sensitive plaintext value; it must never
    # surface in any span attribute.
    stubber.add_response("put_item", {}, {"TableName": "t", "Item": _SENSITIVE_ITEM})
    tracer = provider.get_tracer("test")
    with stubber:
        with tracer.start_as_current_span("handler"):
            client.put_item(TableName="t", Item=_SENSITIVE_ITEM)

    aws_spans = _aws_child_spans(exporter)
    assert len(aws_spans) == 1
    serialized_attrs = repr(sorted(aws_spans[0].attributes.items()))
    assert "do-not-capture-this-plaintext" not in serialized_attrs, (
        "request payload body leaked into a subsegment attribute — the default "
        "metadata-only config must not capture request/response bodies"
    )
    # Belt-and-suspenders: no attribute key looks like a payload/body capture.
    for key in aws_spans[0].attributes:
        assert "body" not in key.lower(), (
            f"unexpected payload-body attribute {key!r} on the AWS subsegment"
        )
