"""Re-export of `aws_lambda_powertools.metrics` for EMF-based metric emission.

Per the observability spec's *Metric Emission via EMF* Requirement,
CloudWatch metrics are emitted as EMF (Embedded Metric Format) JSON log
lines that CloudWatch Logs parses server-side into metrics — not via
synchronous `cloudwatch:PutMetricData` API calls. Lambda execution
roles do NOT need (and SHALL NOT be granted) `cloudwatch:PutMetricData`.

Recommended handler usage:

    from cairn_observability import Metrics, MetricUnit, metric_namespace

    metrics = Metrics(
        namespace=metric_namespace(REGION_CODE, STAGE),  # "CairnProofs/{regionCode}-{stage}"
        service="validate-bundle",
    )

    @metrics.log_metrics(capture_cold_start_metric=True)
    def lambda_handler(event, context):
        metrics.add_dimension(name="ProjectId", value=project_id)
        metrics.add_dimension(name="TenantId", value=tenant_id)
        metrics.add_metric(name="ValidationSuccess", unit=MetricUnit.Count, value=1)
        ...

The `@metrics.log_metrics` decorator handles the flush-at-handler-exit
lifecycle. Omitting the decorator silently drops emissions — handlers
that emit metrics MUST decorate `lambda_handler`.
"""

from aws_lambda_powertools.metrics import Metrics, MetricUnit

__all__ = ["MetricUnit", "Metrics", "metric_namespace", "set_stage_default_dimension"]

# Root token for every CloudWatch EMF metric namespace this system emits
# ([OBS-009]). The qualified namespace is `CairnProofs/{regionCode}-{stage}`
# (e.g. `CairnProofs/cmh-alpha`); the bare root is the fallback when the
# Deployment_Qualifier is not yet resolved at construction time.
_METRIC_NAMESPACE_ROOT: str = "CairnProofs"


def metric_namespace(region_code: str | None, stage: str | None) -> str:
    """Compose the CloudWatch EMF metric namespace for this deployment.

    Returns ``CairnProofs/{regionCode}-{stage}`` when both the region code
    and stage are known, falling back to the bare ``CairnProofs`` root when
    either is absent (e.g. a module-level construction before the
    Deployment_Qualifier is resolved).

    This is the single composition point for the metric namespace
    ([OBS-009]): emitters SHALL derive their ``Metrics(namespace=...)`` from
    here rather than hardcoding the literal, so the namespace token lives in
    exactly one place rather than scattered string literals.
    """
    if region_code and stage:
        return f"{_METRIC_NAMESPACE_ROOT}/{region_code}-{stage}"
    return _METRIC_NAMESPACE_ROOT


def set_stage_default_dimension(metrics: Metrics, stage: str) -> None:
    """Idempotent: set the `Stage` default dimension on the Powertools
    singleton. No-op if `Stage` is already set to `stage`. Emits the
    standard duplicate-dimension warning if `Stage` is set to a
    different value (signalling a real misconfiguration, not the
    benign duplicate-import case).

    Note on a residual upstream warning in Powertools: the *first* call here still emits one
    `PowertoolsUserWarning: Dimension 'Stage' has already been added`,
    because Powertools' own `Metrics.set_default_dimensions` double-adds —
    `provider.set_default_dimensions` (cloudwatch.py:549) runs
    `add_dimension('Stage', ...)`, then the `Metrics` wrapper
    (metrics.py:198) calls `add_dimension('Stage', ...)` a second time,
    which sees the dimension already present and warns. This is upstream
    redundancy, not a defect here, and is functionally inert: the
    emitted EMF carries `Stage` exactly once with the correct value
    (the "overwrite" replaces the value with the identical value). This
    helper's job is to eliminate the *additional* warnings that would otherwise
    accrue when each of the four handler modules loaded into one process
    and re-set the shared singleton — see
    `tests/unit/cairn_observability/test_handler_import_no_stage_warning.py`."""
    if not stage:
        return
    if metrics.default_dimensions.get("Stage") == stage:
        return
    metrics.set_default_dimensions(Stage=stage)
