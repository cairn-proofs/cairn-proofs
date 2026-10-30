"""Regression guards for `Stage` (and `Reason`) on the validate-bundle EMF.

Two invariants:

- `test_stage_default_dimension_on_emf_after_handler`: `@metrics.log_metrics`
  attaches `Stage=<STAGE env var>` to the serialized EMF line.
- `test_stage_and_reason_both_on_emf`: the permanent-failure branch's
  `metrics.add_dimension(name="Reason", value="Permanent")` call inside
  `lambda_handler` results in BOTH `Stage` and `Reason` on the EMF line — the
  default `Stage` dimension is not disturbed by the per-invocation one.

Modelled on `test_log_metrics_decorator_flushes_emf_to_stdout` in
`tests/unit/cairn_observability/test_metrics.py`, driving the real handler.
"""

from __future__ import annotations

import json
from typing import Any

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    STAGE,
    BundleFixture,
    build_bundle,
    ecr_push_event,
)


def _parse_emf(out: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "_aws" not in line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "_aws" in doc:
            docs.append(doc)
    return docs


def _seed(handler: Any, bundle: BundleFixture) -> None:
    handler._test_fake_ecr.add_bundle(bundle)


def test_stage_default_dimension_on_emf_after_handler(
    reload_handler, lambda_context, capsys
) -> None:
    """The serialized EMF emitted after the happy-path handler runs carries
    `Stage=<env value>`."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    handler.lambda_handler(ecr_push_event(bundle=bundle), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert docs, "expected at least one EMF metric document"
    assert any(d.get("Stage") == STAGE for d in docs), (
        f"expected Stage={STAGE!r} on the EMF line; "
        f"saw Stage values {[d.get('Stage') for d in docs]!r}"
    )


def test_stage_and_reason_both_on_emf(reload_handler, lambda_context, capsys) -> None:
    """On the permanent-failure branch the per-invocation
    `add_dimension(Reason="Permanent")` and the default `Stage` dimension
    BOTH land on the same EMF line — the per-invocation dimension does not
    displace the default one."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    # Missing userIdentity drives the permanent-failure branch that calls
    # metrics.add_dimension(name="Reason", value="Permanent").
    event = ecr_push_event(bundle=bundle, omit_user_identity=True)

    # [PIPELINE-006]: a permanent failure returns _REJECTED in-band (no raise); the
    # Reason="Permanent" + Stage dimensions still land on the EMF line.
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    docs = _parse_emf(capsys.readouterr().out)
    both = [d for d in docs if d.get("Stage") == STAGE and d.get("Reason") == "Permanent"]
    assert both, (
        f"expected an EMF line carrying BOTH Stage={STAGE!r} and "
        f"Reason='Permanent'; saw "
        f"{[(d.get('Stage'), d.get('Reason')) for d in docs]!r}"
    )
