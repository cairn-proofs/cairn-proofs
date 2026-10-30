"""Regression guards for `Stage` (and `DetailType`) on the update-metadata EMF.

Two invariants:

- `test_stage_default_dimension_on_emf_after_handler`: `@metrics.log_metrics`
  attaches `Stage=<STAGE env var>` to the serialized EMF line.
- `test_stage_and_detail_type_both_on_emf`: the per-invocation
  `metrics.add_dimension(name="DetailType", ...)` call inside `lambda_handler`
  results in BOTH `Stage` and `DetailType` on the EMF line — the default
  `Stage` dimension is not disturbed by the per-invocation one.

Modelled on `test_log_metrics_decorator_flushes_emf_to_stdout` in
`tests/unit/cairn_observability/test_metrics.py`, driving the real handler.
"""

from __future__ import annotations

import json
from typing import Any

from tests.unit.cairn_lambda_update_metadata.conftest import STAGE, validated_event


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


def test_stage_default_dimension_on_emf_after_handler(
    reload_handler, lambda_context, capsys
) -> None:
    """The serialized EMF emitted after the handler runs carries
    `Stage=<env value>`."""
    handler = reload_handler

    handler.lambda_handler(validated_event(), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert docs, "expected at least one EMF metric document"
    assert any(d.get("Stage") == STAGE for d in docs), (
        f"expected Stage={STAGE!r} on the EMF line; "
        f"saw Stage values {[d.get('Stage') for d in docs]!r}"
    )


def test_stage_and_detail_type_both_on_emf(reload_handler, lambda_context, capsys) -> None:
    """The per-invocation `add_dimension(DetailType=...)` and the default
    `Stage` dimension BOTH land on the same EMF line — the per-invocation
    dimension does not displace the default one."""
    handler = reload_handler

    event = validated_event()
    detail_type = event["detail-type"]  # "artifact.validated"
    handler.lambda_handler(event, lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    both = [d for d in docs if d.get("Stage") == STAGE and d.get("DetailType") == detail_type]
    assert both, (
        f"expected an EMF line carrying BOTH Stage={STAGE!r} and "
        f"DetailType={detail_type!r}; saw "
        f"{[(d.get('Stage'), d.get('DetailType')) for d in docs]!r}"
    )
