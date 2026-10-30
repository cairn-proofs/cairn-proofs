"""Tests for failure-event structured logging.

Property: for any non-empty ``failed_criteria`` passed to the validate
Lambda's ``_emit_failure_event``, a structured CloudWatch log record SHALL
be emitted carrying EVERY ``{criterion, field, message}`` triple — not merely
a count or the generic ``Config_Blob invalid; see failedCriteria`` summary.

``_emit_failure_event`` emits a structured log record carrying the
per-criterion detail in addition to the ``emit_validation_failed(detail)``
EventBridge event, so the detail reaches the log group and not only the event.

Log capture: ``caplog`` attaches a handler to the root logger and records
every ``LogRecord``; the handler's ``LOG.warning(..., extra={...})`` fields
land as attributes on that record (the same mechanism ``StructuredJsonFormatter``
serializes), so we assert against ``record.failedCriteria`` and the rendered
message text.
"""

from __future__ import annotations

import logging
from typing import Any


def _failed_criteria() -> list[dict[str, Any]]:
    # The exact shape the four stale-validator criteria take:
    # three missing-annotation criteria plus the config-blob required-field one.
    return [
        {
            "criterion": "a",
            "field": "manifest.annotations[dev.cairnproofs.artifact.catalog-or-profile-uuid]",
            "message": "required manifest annotation missing",
        },
        {
            "criterion": "a",
            "field": "manifest.annotations[dev.cairnproofs.artifact.control-family]",
            "message": "required manifest annotation missing",
        },
        {
            "criterion": "a",
            "field": "manifest.annotations[dev.cairnproofs.artifact.controls]",
            "message": "required manifest annotation missing",
        },
        {
            "criterion": "c4",
            "field": "config_blob.catalogOrProfileUuid",
            "message": "Field required",
        },
    ]


def test_emit_failure_event_logs_every_criterion_field_message(reload_handler, caplog) -> None:
    handler = reload_handler
    failed = _failed_criteria()

    with caplog.at_level(logging.WARNING):
        handler._emit_failure_event(
            manifest_digest="sha256:0e957095",
            repository_name="artifact-cmh-alpha/mvp-tenant",
            failed_criteria=failed,
            error_kind="config_blob_invalid",
        )

    # Find a record that carries the structured failing-criteria detail.
    # _emit_failure_event routes the per-criterion list through
    # extra={"failedCriteria": ...}.
    detail_records = [r for r in caplog.records if hasattr(r, "failedCriteria")]
    assert detail_records, (
        "no log record carried a 'failedCriteria' attribute — "
        "_emit_failure_event logged nothing structured about why validation failed"
    )

    logged_criteria = detail_records[-1].failedCriteria
    # Every triple must be present, not a count or a generic summary string.
    for expected in failed:
        assert expected in logged_criteria, (
            f"criterion {expected!r} missing from logged failedCriteria {logged_criteria!r}"
        )

    # And the values must actually be discoverable from the log payload, not
    # collapsed to a count. Assert each field/message string surfaces somewhere
    # in the rendered record (message + structured attrs).
    rendered = " ".join(
        [r.getMessage() for r in caplog.records]
        + [str(getattr(r, "failedCriteria", "")) for r in caplog.records]
    )
    for expected in failed:
        assert expected["field"] in rendered
        assert expected["message"] in rendered
        assert expected["criterion"] in str(logged_criteria)
