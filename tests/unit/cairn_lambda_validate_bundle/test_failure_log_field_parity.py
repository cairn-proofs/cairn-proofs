"""RED reproduction: validate-bundle failure log omits the OBS-008-mandated
``eventType``, ``status``, ``durationMs``, and ``projectId`` fields on the
RENDERED CloudWatch line.

The failure is driven through the
LIVE ``lambda_handler`` -> ``observability_scope`` path (not a bare
``_emit_failure_event`` call) because the fix derives ``durationMs`` from a
per-invocation anchor set in ``lambda_handler``, and because ``eventType`` is
sourced from OTEL baggage that only exists inside the scope.

Trigger: a producer-claim identity cross-check failure. The claim carries a
valid ``projectId`` (equal to the ECR path's ``{project-id}`` segment) but a
foreign ``orgId``, so the org leg of the [PIPELINE-013] exact-match cross-check
fails. This is a PERMANENT failure raised AFTER the producer claim is parsed, so
``claim.project_id`` is in scope at the ``_emit_failure_event`` call site — the
precondition for ``projectId`` parity.

Assertion surface — the RENDERED JSON line, NOT the raw record attribute.
OBS-008 governs the serialized ``CairnLogLayout`` output an operator reads, not
the in-memory ``LogRecord``. ``StructuredJsonFormatter`` populates the payload
from active OTEL baggage FIRST and then copies per-record ``extra`` keys, SKIPPING
any key baggage already set (``libs/observability/cairn_observability/log.py``
103-111). ``observability_scope`` seeds ``eventType="validate"`` into baggage at
handler entry, so an ``extra={"eventType": "validation-failed"}`` is SHADOWED:
the raw record attribute would read ``validation-failed`` while the serialized
line reads ``validate``. A record-attribute assertion would miss that gap, so
every assertion below is against the rendered line. The line is captured at
emission time (while baggage is live) by a dedicated ``logging.Handler`` using
the project ``StructuredJsonFormatter`` — the same capture pattern as
``test_failure_logging_guards.py``.
"""

from __future__ import annotations

import json
import logging

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    PROJECT_ID,
    REPO_NAME,
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
)

_FAILURE_LOG_MESSAGE = "validate-bundle validation failed"


def _drive_post_claim_permanent_failure(handler, lambda_context):
    """Drive a post-claim-parse permanent failure through the live
    ``lambda_handler``.

    Uses the identity cross-check failure path: the claim's ``orgId`` disagrees
    with the ECR path's ``{org-id}`` segment while ``tenantId`` / ``projectId``
    still match, so the claim is fully parsed (``claim.project_id == PROJECT_ID``)
    before the cross-check raises ``PermanentValidationFailure``. Returns the
    ``lambda_handler`` result so the caller can confirm the permanent-failure
    (``_REJECTED``) branch was taken.
    """
    bundle = build_bundle(repo_name=REPO_NAME)
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        repo_name=REPO_NAME,
        claim_bytes=attestation_claim(org_id="org-foreign"),
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)
    return handler.lambda_handler(event, lambda_context)


def _rendered_failure_line(handler, lambda_context):
    """Drive the failure and return the RENDERED failure-log JSON line as a dict.

    Attaches a dedicated handler using the project ``StructuredJsonFormatter``
    that serializes each ``LogRecord`` to JSON AT emission time — while the
    ``observability_scope`` baggage is still live — so the captured line is
    exactly what ``CairnLogLayout`` writes to CloudWatch (baggage merged over
    per-record ``extra`` with baggage precedence). Returns a 2-tuple of the
    ``lambda_handler`` result and the parsed JSON dict of the last emitted
    failure line.
    """
    from cairn_observability.log import StructuredJsonFormatter

    rendered_lines: list[str] = []

    class _RenderingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self._fmt = StructuredJsonFormatter()

        def emit(self, record: logging.LogRecord) -> None:
            rendered_lines.append(self._fmt.format(record))

    rendering_handler = _RenderingHandler()
    root = logging.getLogger()
    root.addHandler(rendering_handler)
    try:
        result = _drive_post_claim_permanent_failure(handler, lambda_context)
    finally:
        root.removeHandler(rendering_handler)

    lines = [ln for ln in rendered_lines if f'"{_FAILURE_LOG_MESSAGE}"' in ln]
    assert lines, (
        "no rendered 'validate-bundle validation failed' JSON line was emitted — "
        "the permanent-failure chokepoint did not fire"
    )
    return result, json.loads(lines[-1])


def test_failure_log_carries_event_type_validation_failed(reload_handler, lambda_context) -> None:
    """The RENDERED failure log line SHALL carry
    ``eventType == "validation-failed"``."""
    handler = reload_handler
    result, emitted = _rendered_failure_line(handler, lambda_context)

    assert result is handler._REJECTED
    assert emitted["eventType"] == "validation-failed", (
        "rendered failure line is missing eventType=='validation-failed'; "
        f"got {emitted.get('eventType', '<absent>')!r} (the baggage entry verb "
        "'validate' shadows the per-record extra under formatter precedence)"
    )


def test_failure_log_carries_status_failure_or_error(reload_handler, lambda_context) -> None:
    """The RENDERED failure log line SHALL carry
    ``status`` set to ``failure`` or ``error``."""
    handler = reload_handler
    result, emitted = _rendered_failure_line(handler, lambda_context)

    assert result is handler._REJECTED
    status = emitted.get("status")
    assert status in {"failure", "error"}, (
        f"rendered failure line is missing a status in {{failure, error}}; got {status!r}"
    )


def test_failure_log_carries_non_negative_integer_duration_ms(
    reload_handler, lambda_context
) -> None:
    """The RENDERED failure log line SHALL carry
    ``durationMs`` holding the validate-stage wall-clock as a non-negative
    integer."""
    handler = reload_handler
    result, emitted = _rendered_failure_line(handler, lambda_context)

    assert result is handler._REJECTED
    duration_ms = emitted.get("durationMs")
    assert duration_ms is not None, "rendered failure line is missing durationMs"
    assert isinstance(duration_ms, int) and not isinstance(duration_ms, bool), (
        f"durationMs must be an int; got {type(duration_ms).__name__} ({duration_ms!r})"
    )
    assert duration_ms >= 0, f"durationMs must be non-negative; got {duration_ms!r}"


def test_failure_log_carries_project_id_from_parsed_claim(reload_handler, lambda_context) -> None:
    """For a failure raised AFTER the producer claim
    is parsed (the identity cross-check), the RENDERED failure log line SHALL
    carry ``projectId`` equal to the claim's ``projectId``."""
    handler = reload_handler
    result, emitted = _rendered_failure_line(handler, lambda_context)

    assert result is handler._REJECTED
    assert emitted.get("projectId") == PROJECT_ID, (
        "rendered failure line is missing projectId equal to the claim's projectId; "
        f"expected {PROJECT_ID!r}, got {emitted.get('projectId', '<absent>')!r}"
    )
