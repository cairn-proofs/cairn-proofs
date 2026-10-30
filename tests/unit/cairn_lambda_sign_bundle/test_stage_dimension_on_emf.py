"""Regression guard: the sign-bundle handler keeps `Stage` on its EMF.

`@metrics.log_metrics` attaches `Stage=<STAGE env var>` to the serialized EMF
line. Modelled on `test_log_metrics_decorator_flushes_emf_to_stdout` in
`tests/unit/cairn_observability/test_metrics.py`, lifted to drive the real
handler so the invariant is pinned end-to-end.
"""

from __future__ import annotations

import json
from typing import Any

from tests.unit.cairn_lambda_sign_bundle.conftest import (
    STAGE,
    attestation_digest,
    validated_event,
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


def test_stage_default_dimension_on_emf_after_handler(
    reload_handler, lambda_context, capsys
) -> None:
    """The serialized EMF emitted after the handler runs carries
    `Stage=<env value>`."""
    handler = reload_handler

    handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert docs, "expected at least one EMF metric document"
    assert any(d.get("Stage") == STAGE for d in docs), (
        f"expected Stage={STAGE!r} on the EMF line; "
        f"saw Stage values {[d.get('Stage') for d in docs]!r}"
    )
