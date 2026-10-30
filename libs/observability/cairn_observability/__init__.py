"""Shared observability utilities for Cairn Provenance_System Lambdas.

Built directly on OpenTelemetry primitives (loaded by the ADOT Lambda
layer) and `aws_lambda_powertools.Metrics` for EMF-based CloudWatch
metric emission. This module is the project-side seam that satisfies
the `observability` capability spec — W3C trace context extraction with
span fallback, per-invocation scope via `observability_scope`, the
`StructuredJsonFormatter` JSON log output with `LoggingInstrumentor`-
driven trace correlation, `ObservabilityKeys` constants for the canonical
field-name vocabulary, and `Metrics` + `MetricUnit` re-exported from
Powertools for EMF emission.
"""

from cairn_observability.keys import ObservabilityKeys
from cairn_observability.log import StructuredJsonFormatter, configure_root_logger
from cairn_observability.metrics import (
    Metrics,
    MetricUnit,
    metric_namespace,
    set_stage_default_dimension,
)
from cairn_observability.scope import (
    TraceContext,
    extract_trace_context,
    inject_trace_context,
    observability_scope,
)
from cairn_observability.tracing import configure_tracing

__all__ = [
    "MetricUnit",
    "Metrics",
    "ObservabilityKeys",
    "StructuredJsonFormatter",
    "TraceContext",
    "configure_root_logger",
    "configure_tracing",
    "extract_trace_context",
    "inject_trace_context",
    "metric_namespace",
    "observability_scope",
    "set_stage_default_dimension",
]
