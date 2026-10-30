"""Regression guard for the deployment's `OTEL_PROPAGATORS` set being installable.

`opentelemetry.propagate` reads `OTEL_PROPAGATORS` **at module import** and
resolves each comma-separated name to a propagator via the
`opentelemetry_propagator` entry-point group. A configured name with no
registered entry point makes that import raise

    ValueError: Propagator xray not found. It is either misspelled or not installed.

which crashes the Lambda at init (it imports `opentelemetry.propagate`
transitively via the OTel SDK). The deployment sets
`OTEL_PROPAGATORS=tracecontext,baggage,xray`
(cdk/lib/constructs/lambda-functions.ts). `tracecontext` + `baggage` ship with
`opentelemetry-api`; `xray` is registered ONLY by
`opentelemetry-propagator-aws-xray`. This test exercises the
propagator-entry-point resolution locally.

Parallel-safety / global-pollution discipline: `opentelemetry.propagate`
installs a process-global composite textmap propagator at import. We force a
fresh resolution under the deployment's env via `importlib.reload`, then in
`finally` restore the global by reloading the module back under the ambient
(default) env so concurrent xdist workers and later tests see the unpolluted
default. No network — entry-point resolution is purely local metadata.
"""

from __future__ import annotations

import importlib
import os
from unittest import mock

import opentelemetry.propagate as otel_propagate

# Exactly what cdk/lib/constructs/lambda-functions.ts baseEnv sets.
_DEPLOYMENT_PROPAGATORS = "tracecontext,baggage,xray"


def test_aws_xray_propagator_importable() -> None:
    """The package providing the `xray` entry point must also be importable.

    Fails with ImportError when `opentelemetry-propagator-aws-xray` is absent.
    """
    from opentelemetry.propagators.aws import AwsXRayPropagator

    assert AwsXRayPropagator is not None


def test_deployment_propagators_all_resolvable() -> None:
    """`OTEL_PROPAGATORS=tracecontext,baggage,xray` must resolve without raising.

    This re-runs the exact import-time resolution that crashes the Lambda when
    `xray` has no registered entry point. Without
    `opentelemetry-propagator-aws-xray` installed, `importlib.reload` raises
    ``ValueError: Propagator xray not found...``; the dependency must be present
    for the configured propagators to resolve.
    """
    from opentelemetry.propagators.aws import AwsXRayPropagator
    from opentelemetry.propagators.composite import CompositePropagator

    try:
        with mock.patch.dict(
            os.environ, {"OTEL_PROPAGATORS": _DEPLOYMENT_PROPAGATORS}, clear=False
        ):
            # Forces a fresh read of OTEL_PROPAGATORS and entry-point lookup.
            importlib.reload(otel_propagate)
            composite = otel_propagate.get_global_textmap()

        assert isinstance(composite, CompositePropagator)
        members = composite._propagators  # only way to inspect membership
        assert any(isinstance(m, AwsXRayPropagator) for m in members), (
            "global composite propagator is missing the AWS X-Ray propagator; "
            "OTEL_PROPAGATORS=...,xray was configured but did not resolve to "
            "AwsXRayPropagator"
        )
    finally:
        # Restore the process-global propagator to the ambient-env default so
        # parallel xdist workers and later tests are not polluted.
        importlib.reload(otel_propagate)
