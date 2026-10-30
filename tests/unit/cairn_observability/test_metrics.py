"""Tests for cairn_observability.metrics — Powertools `Metrics` re-export + EMF emission."""

from __future__ import annotations

import json

import pytest
from cairn_observability import Metrics, MetricUnit, metric_namespace


@pytest.fixture
def metrics() -> Metrics:
    """Powertools `Metrics` shares its `metric_set` / `default_dimensions`
    across all instances via the provider singleton. Each test instantiates
    a fresh `Metrics`, then `clear_metrics()` + `clear_default_dimensions()`
    wipe the shared state at entry and exit so neighbouring tests in the
    same xdist worker don't see each other's metrics. Same shape of in-
    worker test leakage the `tests-parallel-safe` Requirement calls out."""
    m = Metrics(namespace="CairnProofs/cmh-alpha", service="validate-bundle")
    m.clear_metrics()
    m.clear_default_dimensions()
    yield m
    m.clear_metrics()
    m.clear_default_dimensions()


def test_metrics_class_re_exported_from_cairn_observability() -> None:
    """`from cairn_observability import Metrics` is the canonical import path."""
    import aws_lambda_powertools.metrics as pt

    assert Metrics is pt.Metrics


def test_metric_unit_re_exported() -> None:
    """`MetricUnit.Count` and `MetricUnit.Milliseconds` are the units we use most."""
    assert MetricUnit.Count.value == "Count"
    assert MetricUnit.Milliseconds.value == "Milliseconds"


# ── EMF serialization ──────────────────────────────────────────────────


def test_add_metric_appears_in_serialized_emf(metrics: Metrics) -> None:
    metrics.add_dimension(name="ProjectId", value="alpha-bank")
    metrics.add_metric(name="ValidationSuccess", unit=MetricUnit.Count, value=1)
    out = metrics.serialize_metric_set()
    assert out["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "CairnProofs/cmh-alpha"
    assert out["ValidationSuccess"] == [1]
    assert out["ProjectId"] == "alpha-bank"


def test_metric_namespace_carries_through(metrics: Metrics) -> None:
    metrics.add_metric(name="Anything", unit=MetricUnit.Count, value=1)
    out = metrics.serialize_metric_set()
    assert out["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "CairnProofs/cmh-alpha"


def test_service_dimension_attached_automatically(metrics: Metrics) -> None:
    """Powertools attaches `service` as a dimension on every emission when
    the Metrics constructor sets it."""
    metrics.add_metric(name="X", unit=MetricUnit.Count, value=1)
    out = metrics.serialize_metric_set()
    assert out["service"] == "validate-bundle"


def test_default_dimensions_set_once_attach_to_every_metric(metrics: Metrics) -> None:
    metrics.set_default_dimensions(Stage="alpha")
    metrics.add_metric(name="A", unit=MetricUnit.Count, value=1)
    out = metrics.serialize_metric_set()
    assert out["Stage"] == "alpha"


def test_milliseconds_unit_round_trips(metrics: Metrics) -> None:
    metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=42)
    out = metrics.serialize_metric_set()
    metric_def = out["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]
    assert metric_def["Name"] == "DurationMs"
    assert metric_def["Unit"] == "Milliseconds"
    assert out["DurationMs"] == [42]


def test_multiple_metrics_in_one_emission(metrics: Metrics) -> None:
    metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
    metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=120)
    out = metrics.serialize_metric_set()
    metric_names = {d["Name"] for d in out["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert metric_names == {"Success", "DurationMs"}


def test_log_metrics_decorator_flushes_emf_to_stdout(capsys, metrics: Metrics) -> None:
    """End-to-end: the @metrics.log_metrics decorator wraps a handler, flushes
    the EMF JSON line to stdout at handler exit, and the line is parseable."""
    metrics.set_default_dimensions(Stage="alpha")

    @metrics.log_metrics
    def handler(_event, _context):
        metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
        return "ok"

    result = handler({}, None)
    assert result == "ok"

    out = capsys.readouterr().out.strip().splitlines()
    emf_lines = [json.loads(line) for line in out if line.startswith("{") and "_aws" in line]
    assert len(emf_lines) >= 1
    emf = emf_lines[-1]
    assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "CairnProofs/cmh-alpha"
    assert emf["Success"] == [1]
    assert emf["Stage"] == "alpha"


# ── metric_namespace composition point ([OBS-009]) ─────────────────────


def test_metric_namespace_qualified_when_region_and_stage_present() -> None:
    """The single composition point yields `CairnProofs/{regionCode}-{stage}`."""
    assert metric_namespace("cmh", "alpha") == "CairnProofs/cmh-alpha"
    assert metric_namespace("pdx", "prod") == "CairnProofs/pdx-prod"


def test_metric_namespace_uses_cairnproofs_root_not_legacy_artifact() -> None:
    """The metric namespace root is `CairnProofs`, never `Artifact`."""
    ns = metric_namespace("cmh", "alpha")
    assert ns.startswith("CairnProofs/")
    assert not ns.startswith("Artifact/")


@pytest.mark.parametrize(
    ("region_code", "stage"),
    [(None, "alpha"), ("cmh", None), (None, None), ("", ""), ("cmh", "")],
)
def test_metric_namespace_falls_back_to_bare_root(region_code, stage) -> None:
    """When the Deployment_Qualifier is not fully resolved, the namespace is the
    bare `CairnProofs` root (mirrors the handler `... else "CairnProofs"` path)."""
    assert metric_namespace(region_code, stage) == "CairnProofs"
