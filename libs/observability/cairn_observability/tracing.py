"""Manual OpenTelemetry TracerProvider bootstrap for the collector-only ADOT layer.

The deployment uses a **collector-only** ADOT Lambda layer (a Go binary that
runs the OTel Collector as a `localhost` OTLP receiver — it ships no Python SDK
and does no auto-instrumentation). Our application therefore installs the SDK
`TracerProvider` itself:

- ``id_generator=AwsXRayIdGenerator()`` — REQUIRED. X-Ray rejects trace IDs that
  are not time-prefixed; the default OTel random ID generator produces IDs X-Ray
  will not accept. Spans flow OTLP → the in-process collector → its ``awsxray``
  exporter → X-Ray.
- ``BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces"))``
  — exports over the collector's OTLP/HTTP receiver.

`configure_tracing()` is idempotent and parallel-safe: it installs the provider
ONCE per process (guarded by the global provider type — mirroring the
instrumentor guard in `log.py`'s `configure_root_logger`). Because we use a
`BatchSpanProcessor` and there is no auto-instrument wrapper to flush on Lambda
freeze, `observability_scope` force-flushes the provider on exit so spans export
before the execution environment is frozen — see `scope.py`.

The fresh install also wires up `BotocoreInstrumentor` so every boto3 / botocore
AWS-SDK call becomes a child span (X-Ray subsegment) under the active handler
span — per-dependency visibility without per-handler wiring. See
`configure_tracing` for the metadata-only capture contract.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# The collector-only ADOT layer exposes the OTel Collector's OTLP/HTTP receiver
# on localhost. Read-only module constant (immutable after import) — safe as a
# module global per the project's concurrency rules.
_OTLP_TRACES_ENDPOINT = "http://localhost:4318/v1/traces"

# Per-export timeout (seconds) for the OTLP HTTP exporter. The collector runs
# in-process on localhost, so a healthy export is sub-millisecond. This bounds
# the WORST case: `observability_scope` force-flushes on exit (to export spans
# before Lambda freeze), and when the collector is briefly unreachable the OTLP
# HTTP exporter retries with backoff. In the installed exporter version that
# retry loop is deadline-based — bounded by this `timeout` — so the exporter's
# `timeout` (not the `BatchSpanProcessor.force_flush` timeout, which only waits
# on the queue) is the effective ceiling on how long a scope-exit flush can
# block. Kept short so a collector hiccup adds at most ~1s to an invocation
# rather than the several seconds the default backoff budget would.
_OTLP_EXPORT_TIMEOUT_SEC = 1


def configure_tracing() -> None:
    """Install the SDK ``TracerProvider`` once per process (idempotent, parallel-safe).

    Sets the global provider only when the current one is not already an SDK
    ``TracerProvider`` (OTel's default is a no-op ``ProxyTracerProvider``). The
    guard makes the call idempotent across reused Lambda execution environments
    — a second call is a no-op and does not register a duplicate
    ``BatchSpanProcessor`` — and parallel-safe: it mutates only the OTel global
    provider, which tests snapshot/restore.

    The provider uses ``AwsXRayIdGenerator`` (X-Ray rejects non-time-prefixed
    trace IDs) and a ``BatchSpanProcessor`` exporting over OTLP/HTTP to the
    collector-only layer's ``localhost`` receiver.

    The fresh install also installs ``BotocoreInstrumentor`` bound to the
    constructed provider (see the inline comment for the metadata-only capture
    contract). It sits past the early-return guard, so it runs once on the fresh
    install and is idempotent across reused execution environments.
    """
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return

    provider = TracerProvider(id_generator=AwsXRayIdGenerator())
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=_OTLP_TRACES_ENDPOINT,
                timeout=_OTLP_EXPORT_TIMEOUT_SEC,
            )
        )
    )
    trace.set_tracer_provider(provider)
    # Wire boto3/botocore AWS-SDK calls as child spans under the handler span,
    # bound explicitly to the provider we just constructed (not global
    # resolution). DEFAULT config only — no request/response hooks — so
    # subsegments carry service/operation metadata, never payload bodies.
    BotocoreInstrumentor().instrument(tracer_provider=provider)
