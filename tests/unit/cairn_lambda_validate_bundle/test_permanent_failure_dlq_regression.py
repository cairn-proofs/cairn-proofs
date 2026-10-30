"""Regression guards for permanent-failure DLQ routing.

These guard the metric-accounting invariants and the in-band DLQ-send
invariants of the ``dlq.send_rejection`` seam:

  - transient errors raise ([PIPELINE-005]) with NO failure event and NO DLQ send;
  - a valid bundle records Success and issues NO DLQ send;
  - a permanent failure emits exactly ONE bounded validation-failed event AND
    exactly ONE DLQ rejection, and records Failure(Reason=Permanent) exactly once
    with NO Success — the handler returns ``_REJECTED`` (not by raising); the
    return must not change metric accounting;
  - a pipeline-internal PutImage / re-seen digest skips with NO DLQ send and
    NO Success/Failure metric;
  - a non-zero put_events FailedEntryCount surfaces as an error.

The shared ``reload_handler`` fixture (conftest) injects a recording ``FakeSqs``
on ``handler._test_fake_sqs`` and sets ``VALIDATE_DLQ_URL``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    TENANT_ID,
    VALIDATE_DLQ_URL,
    build_attestation,
    build_bundle,
    ecr_push_event,
    submit,
)


def _emf_docs(out: str) -> list[dict[str, Any]]:
    docs = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "_aws" in doc:
            docs.append(doc)
    return docs


def _metric_value(doc: dict[str, Any], name: str) -> float | None:
    for block in doc["_aws"]["CloudWatchMetrics"]:
        for m in block["Metrics"]:
            if m["Name"] == name:
                v = doc.get(name)
                return v[0] if isinstance(v, list) else v
    return None


# --- 3.1 transient still raises (no event, no DLQ); valid bundle no DLQ -------


def test_transient_ecr_fetch_error_raises_with_no_event_and_no_dlq_send(
    reload_handler, lambda_context
) -> None:
    """Unchanged #1 ([PIPELINE-005]): a transient EcrFetchError STILL raises so Lambda's
    async retry applies, emits NO artifact.validation-failed, and issues NO
    in-band DLQ send (the in-band path is permanent-only)."""
    from cairn_lambda_validate_bundle.ecr_client import EcrFetchError

    handler = reload_handler
    bundle = build_bundle()
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    handler._test_fake_ecr.add_bundle(bundle)
    handler._test_fake_ecr.add_attestation(attestation)
    # The TRIGGER fetch (the attestation manifest) fails transiently.
    handler._test_fake_ecr.fetch_manifest_side_effects[attestation.attestation_digest] = (
        EcrFetchError("transient")
    )
    from tests.unit.cairn_lambda_validate_bundle.conftest import attestation_push_event

    event = attestation_push_event(attestation=attestation)

    with pytest.raises(EcrFetchError):
        handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.details("artifact.validation-failed") == []
    assert handler._test_fake_sqs.messages == []


def test_valid_bundle_emits_validated_records_success_and_no_dlq_send(
    reload_handler, lambda_context, capsys
) -> None:
    """Unchanged #5: a valid bundle emits artifact.validated, records the Success
    metric, and issues NO DLQ send (the DLQ is the permanent-failure sink)."""
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle())

    payload = handler.lambda_handler(event, lambda_context)

    assert payload is not handler._SKIPPED
    assert payload is not handler._REJECTED
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert handler._test_fake_sqs.messages == []

    docs = _emf_docs(capsys.readouterr().out)
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert not any(_metric_value(d, "Failure") == 1.0 for d in docs)


# --- 3.2 one bounded event; pipeline-internal + re-seen skip, no DLQ ----------


def test_permanent_failure_emits_exactly_one_bounded_validation_failed_event(
    reload_handler, lambda_context
) -> None:
    """Unchanged #2: a permanent failure emits EXACTLY ONE artifact.validation-failed
    event carrying a bounded failedCriteria list — and exactly one matching DLQ
    rejection (the same chokepoint detail)."""
    handler = reload_handler
    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._REJECTED
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    assert isinstance(failed[0]["failedCriteria"], list)
    assert failed[0]["failedCriteria"]

    # The single DLQ rejection mirrors the emitted event's bounded detail.
    assert len(handler._test_fake_sqs.messages) == 1
    body = handler._test_fake_sqs.bodies()[0]
    assert body["manifestDigest"] == bundle.manifest_digest
    assert body["failedCriteria"] == failed[0]["failedCriteria"]


def test_pipeline_internal_putimage_skips_with_no_failure_event_and_no_dlq_send(
    reload_handler, lambda_context, capsys
) -> None:
    """Unchanged #3: a pipeline-internal PutImage (OCI image index) classifies as
    ValidationSkipped, returns _SKIPPED, emits no failure event, issues NO DLQ
    send, and records NEITHER Success NOR Failure."""
    from cairn_oci import compute_sha256_digest

    handler = reload_handler
    # An OCI image index manifest (no config descriptor) is pipeline-internal.
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [],
    }
    raw = json.dumps(manifest, separators=(",", ":"))
    digest = compute_sha256_digest(raw.encode("utf-8"))
    handler._test_fake_ecr.manifests[digest] = (manifest, raw)
    import dataclasses

    event = ecr_push_event(bundle=dataclasses.replace(build_bundle(), manifest_digest=digest))

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._SKIPPED
    assert handler._test_recorder.detail_types() == []
    assert handler._test_fake_sqs.messages == []

    docs = _emf_docs(capsys.readouterr().out)
    assert not any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert not any(_metric_value(d, "Failure") == 1.0 for d in docs)


def test_reseen_digest_short_circuits_skipped_with_no_reemit_and_no_dlq_send(
    reload_handler, lambda_context
) -> None:
    """Unchanged #4/#5: a re-push of the SAME Producer_Attestation_Referrer against a
    terminal-success content row (whose validatedAttestationDigests already holds
    the referrer) short-circuits as skipped, with NO re-emitted event and NO DLQ
    send on the second pass. The dedup unit is the (content digest, referrer)
    pair gated on terminal-success — a non-terminal row would instead PROCESS
    (covered by test_repush_of_same_attestation_on_non_terminal_row_is_processed
    in test_coordinate_recording.py)."""
    handler = reload_handler

    class _FakeIndex:
        """Minimal MetadataIndex stand-in: (tenant_id, project_id, digest)-keyed,
        if_not_exists + idempotent String-Set ADD mirroring add_validated_attestation."""

        def __init__(self) -> None:
            self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}

        def get_record(self, *, manifest_digest, tenant_id, project_id):
            return self.rows.get((tenant_id, project_id, manifest_digest))

        def create_or_merge(self, *, manifest_digest, tenant_id, project_id, record):
            row = self.rows.setdefault((tenant_id, project_id, manifest_digest), {})
            for attr, value in record.items():
                row.setdefault(attr, value)

        def add_validated_attestation(
            self, *, manifest_digest, tenant_id, project_id, attestation_digest
        ):
            row = self.rows.setdefault((tenant_id, project_id, manifest_digest), {})
            row.setdefault("validatedAttestationDigests", set()).add(attestation_digest)

    index = _FakeIndex()
    handler._metadata_index = lambda: index

    bundle = build_bundle()

    # First attestation push validates the content + records the durable row
    # (keyed on the CONTENT digest) and accumulates the referrer into the set.
    a1, event1 = submit(handler, bundle=bundle)
    handler.lambda_handler(event1, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert handler._test_fake_sqs.messages == []

    # Advance the content row to terminal-success (signed + archived), as
    # update-metadata would once the bundle completes downstream.
    row = index.rows[(TENANT_ID, "alpha-bank", bundle.manifest_digest)]
    row["signingStatus"] = "signed"
    row["archiveStatus"] = "archived"
    assert a1.attestation_digest in row["validatedAttestationDigests"]

    # A re-push of the SAME Producer_Attestation_Referrer over the terminal-success row
    # short-circuits as skipped on the (content digest, referrer) pair.
    a2, event2 = submit(handler, bundle=bundle, seed_bundle=False)
    assert a2.attestation_digest == a1.attestation_digest
    result = handler.lambda_handler(event2, lambda_context)

    assert result is handler._SKIPPED
    # No re-emit, no DLQ send.
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert handler._test_fake_sqs.messages == []


# --- 3.3 metric-once on return; FailedEntryCount still surfaces ---------------


def test_permanent_failure_records_failure_permanent_once_and_no_success(
    reload_handler, lambda_context, capsys
) -> None:
    """A permanent failure records Failure(Reason=Permanent) EXACTLY once and
    records NO Success. The handler returns _REJECTED (not by raising); the
    return must not change metric accounting."""
    handler = reload_handler
    _attestation, event = submit(handler, bundle=build_bundle(), omit_user_identity=True)

    result = handler.lambda_handler(event, lambda_context)
    assert result is handler._REJECTED

    docs = _emf_docs(capsys.readouterr().out)
    failures = [d for d in docs if _metric_value(d, "Failure") == 1.0]
    assert len(failures) == 1
    assert failures[0].get("Reason") == "Permanent"
    assert not any(_metric_value(d, "Success") == 1.0 for d in docs)
    # Exactly one in-band DLQ rejection for the single permanent classification.
    assert len(handler._test_fake_sqs.messages) == 1
    assert handler._test_fake_sqs.messages[0]["QueueUrl"] == VALIDATE_DLQ_URL


def test_nonzero_put_events_failed_entry_count_still_surfaces_as_error(
    reload_handler, lambda_context, monkeypatch
) -> None:
    """Unchanged #6: a non-zero put_events FailedEntryCount on the failure path is
    surfaced as an EventEmitError (the emit chokepoint never silently drops), so
    the invocation fails and is retried rather than silently accepting the bundle.

    The chokepoint writes the durable DLQ record BEFORE emitting the EventBridge
    event (so an SQS-send failure cannot re-drive the event), which means a
    failed emit here happens AFTER the DLQ rejection is deposited: the operator
    store still receives the reason-bearing message, and the surfaced error
    triggers a retry whose only residue is a benign duplicate DLQ record."""
    import cairn_lambda_validate_bundle.events as events_module

    handler = reload_handler

    class _DroppingEvents:
        def put_events(self, *, Entries):
            return {
                "FailedEntryCount": 1,
                "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "dropped"}],
            }

    monkeypatch.setattr(events_module, "_events", lambda: _DroppingEvents())

    bundle = build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))
    _attestation, event = submit(handler, bundle=bundle)

    # The dropped failure event surfaces as a transient Exception (EventEmitError),
    # which the handler re-raises — the emission is never silently dropped.
    with pytest.raises(events_module.EventEmitError):
        handler.lambda_handler(event, lambda_context)

    # dlq.send_rejection runs before emit_validation_failed, so the durable
    # rejection IS deposited (exactly once) even though the subsequent emit
    # raised — the error still surfaces via the EventEmitError above.
    assert len(handler._test_fake_sqs.messages) == 1
