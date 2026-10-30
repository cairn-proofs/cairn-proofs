"""Tests for the idempotent `set_stage_default_dimension` helper.

These pin the four behaviours of the helper:

1. Idempotent same-value: a second call with the same `Stage` value emits
   no `PowertoolsUserWarning` and preserves `default_dimensions["Stage"]`.
2. Empty/unset STAGE is a no-op: `""` leaves `default_dimensions` untouched
   and emits no warning.
3. Same-value idempotence via a prior `set_default_dimensions` call: the
   helper recognises an already-set matching value and stays silent.
4. Real conflict (alpha->beta) still warns: a differing value is a genuine
   misconfiguration and SHALL keep emitting the Powertools warning.
"""

from __future__ import annotations

import warnings

import pytest
from aws_lambda_powertools.warnings import PowertoolsUserWarning
from cairn_observability import Metrics, set_stage_default_dimension

_DUPLICATE_MSG = "Dimension 'Stage' has already been added"


@pytest.fixture
def metrics() -> Metrics:
    """Powertools `Metrics` shares its `metric_set` / `default_dimensions`
    across all instances via the provider singleton. Each test instantiates
    a fresh `Metrics`, then `clear_metrics()` + `clear_default_dimensions()`
    wipe the shared state at entry and exit so neighbouring tests in the
    same xdist worker don't see each other's metrics — keeping the file
    safe under `pytest-xdist -n auto` per the `tests-parallel-safe`
    Requirement. Mirrors the fixture in `test_metrics.py`."""
    m = Metrics(namespace="Artifact/cmh-alpha", service="validate-bundle")
    m.clear_metrics()
    m.clear_default_dimensions()
    yield m
    m.clear_metrics()
    m.clear_default_dimensions()


def _duplicate_stage_warnings(
    records: list[warnings.WarningMessage],
) -> list[warnings.WarningMessage]:
    return [
        w
        for w in records
        if issubclass(w.category, PowertoolsUserWarning) and _DUPLICATE_MSG in str(w.message)
    ]


def test_idempotent_same_value_second_call_no_warning(metrics: Metrics) -> None:
    """Clause #1: two helper calls with the same value -> no warning on the
    second, and `Stage` stays `"alpha"`."""
    set_stage_default_dimension(metrics, "alpha")

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        set_stage_default_dimension(metrics, "alpha")

    assert _duplicate_stage_warnings(records) == []
    assert metrics.default_dimensions["Stage"] == "alpha"


def test_empty_stage_is_noop_no_warning(metrics: Metrics) -> None:
    """Clause #2: empty STAGE leaves `default_dimensions` unchanged (no
    `"Stage"` key) and emits no warning."""
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        set_stage_default_dimension(metrics, "")

    assert "Stage" not in metrics.default_dimensions
    assert _duplicate_stage_warnings(records) == []


def test_same_value_after_direct_set_default_dimensions_no_warning(metrics: Metrics) -> None:
    """Clause #1 (core same-value idempotence, distinct from #1 above):
    when `Stage="alpha"` was set directly via `set_default_dimensions`, the
    helper recognises the matching value and stays silent."""
    metrics.set_default_dimensions(Stage="alpha")

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        set_stage_default_dimension(metrics, "alpha")

    assert _duplicate_stage_warnings(records) == []
    assert metrics.default_dimensions["Stage"] == "alpha"


def test_real_conflict_alpha_to_beta_still_warns(metrics: Metrics) -> None:
    """Clause #3: a differing value is a real misconfiguration — the helper
    SHALL still emit the Powertools duplicate-dimension warning, and `Stage`
    ends up `"beta"`."""
    set_stage_default_dimension(metrics, "alpha")

    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        set_stage_default_dimension(metrics, "beta")

    assert _duplicate_stage_warnings(records), (
        f"expected a PowertoolsUserWarning matching {_DUPLICATE_MSG!r} on the alpha->beta conflict"
    )
    assert metrics.default_dimensions["Stage"] == "beta"
