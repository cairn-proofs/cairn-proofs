"""Regression guards for ``_emit_failure_event``.

These pin the invariants ``_emit_failure_event`` must hold:

- Unchanged #1: a Config_Blob that cites controls + a valid
  ``catalogOrProfileUuid`` still validates cleanly; a malformed UUID
  (criteria a2/c5) or a malformed control id (criterion h) still fails.
- Unchanged #2: a genuinely-invalid bundle still emits the
  ``artifact.validation-failed`` EventBridge event carrying the FULL
  ``failedCriteria`` list AND the handler still raises
  ``PermanentValidationFailure``.
- Unchanged #3: the failure log record still carries the observability
  base/pipeline fields it carried before, ALONGSIDE the new
  ``failedCriteria`` AND the OBS-008 ``eventType`` / ``status`` /
  ``durationMs`` / ``projectId`` fields that fix-validate-failure-log-field-parity
  now attaches. See the field-parity note on the test below for how the fix
  overrides the baggage ``eventType`` so the rendered line carries the failure
  value.

All criteria/UUID/control-id behavior is exercised through the handler's
real ``lambda_handler`` → ``validate_bundle`` path, so these double as
integration guards on the chokepoint caller.
"""

from __future__ import annotations

import json
import logging

from .conftest import (
    CATALOG_UUID,
    attestation_claim,
    build_bundle,
    submit,
)

# --------------------------------------------------------------------------
# Unchanged #1 — control-linkage validation is preserved (criteria c5/h),
# sourced from the Producer_Attestation_Referrer claim.
# --------------------------------------------------------------------------


def test_claim_with_valid_controls_and_uuid_still_validates(reload_handler, lambda_context) -> None:
    """A claim that DOES cite controls + a valid catalogOrProfileUuid validates
    cleanly and reaches artifact.validated (no failure event)."""
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(
        handler,
        bundle=bundle,
        claim_bytes=attestation_claim(
            controls=["ac-1", "ac-2"], catalog_or_profile_uuid=CATALOG_UUID
        ),
    )

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["manifestDigest"] == bundle.manifest_digest


def test_malformed_catalog_or_profile_uuid_still_fails(reload_handler, lambda_context) -> None:
    """A present-but-malformed claim catalogOrProfileUuid still fails the UUID-shape
    check (criterion c5)."""
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(catalog_or_profile_uuid="not-a-uuid"),
    )

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    criteria = {c["criterion"] for c in failed[0]["failedCriteria"]}
    assert "c5" in criteria, failed[0]["failedCriteria"]


def test_malformed_control_id_still_fails(reload_handler, lambda_context) -> None:
    """A malformed claim control identifier still fails the grammar check (criterion h)."""
    handler = reload_handler
    _attestation, event = submit(
        handler,
        bundle=build_bundle(),
        claim_bytes=attestation_claim(
            controls=["AC_1; DROP TABLE"], catalog_or_profile_uuid=CATALOG_UUID
        ),
    )

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    criteria = {c["criterion"] for c in failed[0]["failedCriteria"]}
    assert "h" in criteria, failed[0]["failedCriteria"]


# --------------------------------------------------------------------------
# Unchanged #2 — invalid bundle still emits the full event AND still raises.
# --------------------------------------------------------------------------


def test_invalid_bundle_still_emits_full_failed_criteria_and_raises(
    reload_handler, lambda_context
) -> None:
    """The EventBridge contract is preserved: a genuinely-invalid bundle still
    emits artifact.validation-failed carrying the FULL failedCriteria list,
    and the handler still raises PermanentValidationFailure.

    Drives the same chokepoint (_emit_failure_event) the new log line lives
    in — confirms the additive log line did not displace emit_validation_failed
    or swallow the raise."""
    handler = reload_handler
    # Drop a required manifest annotation -> criterion "a" permanent failure.
    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    # [PIPELINE-006]: a permanent failure returns _REJECTED in-band (no raise).
    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    detail = failed[0]
    # Full per-criterion list is present on the EVENT (the audit contract),
    # each entry carrying the criterion/field/message triple.
    assert detail["failedCriteria"], detail
    for entry in detail["failedCriteria"]:
        assert set(entry) >= {"criterion", "field", "message"}


def test_emit_failure_event_event_and_log_carry_identical_criteria(reload_handler, caplog) -> None:
    """Calling _emit_failure_event directly: the SAME failedCriteria list lands
    on BOTH the EventBridge detail and the new CloudWatch log record — the log
    line is additive, not a replacement, and does not mutate the detail."""
    handler = reload_handler
    failed = [
        {"criterion": "a", "field": "manifest.annotations[x]", "message": "missing"},
        {
            "criterion": "c4",
            "field": "config_blob.catalogOrProfileUuid",
            "message": "Field required",
        },
    ]
    with caplog.at_level(logging.WARNING):
        handler._emit_failure_event(
            manifest_digest="sha256:dead",
            repository_name="artifact-cmh-alpha/p",
            failed_criteria=failed,
            error_kind="invalid_config_blob",
        )

    emitted = handler._test_recorder.details("artifact.validation-failed")
    assert len(emitted) == 1
    assert emitted[0]["failedCriteria"] == failed

    log_records = [r for r in caplog.records if hasattr(r, "failedCriteria")]
    assert log_records, "no failedCriteria log record emitted"
    assert log_records[-1].failedCriteria == failed


# --------------------------------------------------------------------------
# Unchanged #3 — failure log record still carries the fields it carried, plus
# the new failedCriteria. GAP documented inline.
# --------------------------------------------------------------------------


def test_failure_log_record_carries_base_fields_alongside_failed_criteria(
    reload_handler, lambda_context, caplog
) -> None:
    """Through the real lambda_handler path (inside observability_scope), the
    failure warning record carries the new failedCriteria AND the per-stage
    observability fields the spec requires, in the EMITTED JSON line.

    Two capture surfaces are used deliberately:

    - ``caplog`` inspects raw ``LogRecord`` ATTRIBUTES. ``_emit_failure_event``
      attaches ``failedCriteria`` + ``manifestDigest`` directly via ``extra=``,
      so they are visible as record attributes.
    - The baggage-driven fields (``requestId``, ``eventType``, ``traceId``,
      ``sessionId``, ``level``, ``message``) are merged into the JSON by
      ``StructuredJsonFormatter`` *at emission time* from active OTEL baggage
      (``observability_scope``). They are NOT raw record attributes and they
      are GONE once the scope tears down, so re-formatting the captured record
      after the call would drop them. We therefore attach a dedicated handler
      using the project's ``StructuredJsonFormatter`` that renders each record
      to JSON DURING emission (while baggage is live), and assert against that
      rendered line.

    OBS-008 field parity (fix-validate-failure-log-field-parity): the failure
    warning record now carries the OBS-008-mandated ``status`` / ``durationMs``
    / ``projectId`` fields as per-record ``extra`` attributes. ``_emit_failure_event``
    attaches ``status=failure``, a ``durationMs`` derived from the per-invocation
    ``_INVOCATION_START`` anchor, and ``projectId`` (this trigger drops the
    content-manifest project-id annotation, which fails at ``validate_bundle``
    AFTER the producer claim is parsed, so the post-claim call site passes
    ``project_id=claim.project_id``). These three assertions are below on the raw
    ``record``.

    ``eventType`` is the exception: it does NOT ride ``extra``.
    ``StructuredJsonFormatter`` emits baggage keys before per-record ``extra``
    keys and skips an ``extra`` key already set by baggage, and
    ``observability_scope`` seeds baggage with the entry verb
    ``eventType=validate``. So ``_emit_failure_event`` overrides the baggage
    entry itself with ``validation-failed`` before the ``LOG.warning`` (OBS-008
    and this change's Fix 1), and the formatter emits that overridden value. The
    raw ``record`` therefore carries no ``eventType`` attribute (asserted absent
    below); ``eventType=validation-failed`` surfaces only on the rendered line.
    """
    from cairn_observability.log import StructuredJsonFormatter

    handler = reload_handler
    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    # Render each emitted record to JSON via the project formatter AT emission
    # time, so baggage (set by observability_scope) is still active and the
    # merged per-stage fields appear — exactly as CloudWatch would see them.
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
        with caplog.at_level(logging.WARNING):
            # [PIPELINE-006]: a permanent failure returns _REJECTED in-band (no raise).
            assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    finally:
        root.removeHandler(rendering_handler)

    # --- raw LogRecord attributes (caplog) ---
    detail_records = [r for r in caplog.records if hasattr(r, "failedCriteria")]
    assert detail_records, "fix did not emit a failedCriteria log record"
    record = detail_records[-1]
    assert record.levelname == "WARNING"
    assert record.getMessage() == "validate-bundle validation failed"
    assert record.manifestDigest == bundle.manifest_digest
    assert record.failedCriteria
    for entry in record.failedCriteria:
        assert set(entry) >= {"criterion", "field", "message"}

    # OBS-008 fields attached to the record via extra= (the surface the fix
    # changed): status / durationMs / projectId. eventType is NOT among them —
    # _emit_failure_event routes eventType through a baggage override, not extra
    # because the formatter gives baggage precedence over
    # extra on the rendered line. So the raw record carries no eventType
    # attribute; the overridden value surfaces only on the rendered line below.
    assert not hasattr(record, "eventType")
    assert record.status == "failure"
    assert isinstance(record.durationMs, int) and record.durationMs >= 0
    # projectId IS emitted: this trigger drops the content-manifest project-id
    # annotation, which fails at validate_bundle AFTER the claim is parsed, so
    # the post-claim call site passes project_id=claim.project_id.
    assert record.projectId == "alpha-bank"

    # --- emitted JSON line carries base + baggage-merged fields ---
    lines = [ln for ln in rendered_lines if '"validate-bundle validation failed"' in ln]
    assert lines, "structured failure JSON was not emitted to the log stream"
    emitted = json.loads(lines[-1])

    # Base fields (Obs 6) — always present, alongside the new failedCriteria.
    assert emitted["level"] == "WARNING"
    assert emitted["message"] == "validate-bundle validation failed"
    assert "traceId" in emitted  # null under test (no trace context), key present
    assert "sessionId" in emitted
    assert emitted["manifestDigest"] == bundle.manifest_digest
    assert emitted["failedCriteria"] == record.failedCriteria
    # Per-stage pipeline fields merged from observability_scope baggage.
    assert emitted["requestId"] == lambda_context.aws_request_id
    # eventType on the RENDERED line is "validation-failed": _emit_failure_event
    # overrides the observability_scope baggage entry (seeded as the entry verb
    # "validate") with the failure value before LOG.warning,
    # and StructuredJsonFormatter emits the overridden baggage value. The record
    # attribute (asserted above) carries the same value.
    assert emitted["eventType"] == "validation-failed"
    # The OBS-008 fields the fix adds via extra= DO surface on the rendered line
    # (no baggage key shadows them): status / durationMs / projectId.
    assert emitted["status"] == "failure"
    assert isinstance(emitted["durationMs"], int) and emitted["durationMs"] >= 0
    assert emitted["projectId"] == "alpha-bank"
