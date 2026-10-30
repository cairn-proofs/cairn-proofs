"""Per-classification coverage for the permanent-failure DLQ contract.

[PIPELINE-006] routes EVERY permanent classification to the DLQ through the
single ``_emit_failure_event`` chokepoint, so each Property-4 / cross-check
permanent failure reaches the ``_REJECTED``-return + in-band-DLQ-send contract.

This file covers:

  - projectId mismatch ([PIPELINE-013], criterion "f");
  - future-dated ``created`` / temporal inconsistency ([PIPELINE-009], criterion "temporal").

It also covers the latent DEFENSIVE branch (handler ~910-933, config media type
"slipped past validate_bundle"): it routes through the chokepoint, emitting a
failure event AND sending a DLQ rejection AND returning ``_REJECTED``.

All assert the same invariant set: ``lambda_handler`` RETURNS ``handler._REJECTED``
(no raise), exactly ONE DLQ rejection lands on ``VALIDATE_DLQ_URL`` carrying the
manifest digest, and the rejection's failedCriteria name the failure.
"""

from __future__ import annotations

from typing import Any

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    CONFIG_BLOB_MEDIA_TYPE,
    PUSHED_AT,
    VALIDATE_DLQ_URL,
    attestation_claim,
    build_bundle,
    catalog_blob_bytes,
    submit,
)


def _assert_rejected_with_single_dlq(
    handler: Any,
    result: Any,
    bundle: Any,
    *,
    expected_criterion: str,
) -> None:
    """Shared invariant: _REJECTED return + exactly one reason-bearing DLQ send."""
    assert result is handler._REJECTED
    assert len(handler._test_fake_sqs.messages) == 1
    msg = handler._test_fake_sqs.messages[0]
    assert msg["QueueUrl"] == VALIDATE_DLQ_URL
    body = handler._test_fake_sqs.bodies()[0]
    assert body["manifestDigest"] == bundle.manifest_digest
    criteria = body.get("failedCriteria") or []
    assert any(c.get("criterion") == expected_criterion for c in criteria), criteria
    # Exactly one matching artifact.validation-failed event accompanies it.
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1


def test_project_id_mismatch_returns_rejected_and_sends_one_dlq(
    reload_handler, lambda_context
) -> None:
    """[PIPELINE-013] (criterion "f"): a Config_Blob projectId disagreeing with the ECR
    repository path's project-id segment returns ``_REJECTED`` and sends one
    in-band DLQ rejection naming the mismatch."""
    handler = reload_handler
    # Default repo path project-id segment is ``alpha-bank``; the CLAIM declares a
    # different projectId so crosscheck_project_id flags a criterion-"f" failure.
    bundle = build_bundle()
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(project_id="wrong-bank")
    )

    result = handler.lambda_handler(event, lambda_context)

    _assert_rejected_with_single_dlq(handler, result, bundle, expected_criterion="f")


def test_future_dated_oscal_last_modified_returns_rejected_and_sends_one_dlq(
    reload_handler, lambda_context
) -> None:
    """[PIPELINE-009] (criterion "temporal"): an OSCAL layer ``metadata.last-modified``
    strictly after ``ecrPushedAt`` is a permanent temporal failure that returns
    ``_REJECTED`` and sends one in-band DLQ rejection."""
    handler = reload_handler
    future_modified = "2030-01-01T00:00:00Z"
    assert future_modified > PUSHED_AT
    bundle = build_bundle(layer_bytes=catalog_blob_bytes(last_modified=future_modified))
    _attestation, event = submit(handler, bundle=bundle)

    result = handler.lambda_handler(event, lambda_context)

    _assert_rejected_with_single_dlq(handler, result, bundle, expected_criterion="temporal")


def test_defensive_config_media_type_branch_emits_event_and_sends_dlq(
    reload_handler, lambda_context, monkeypatch
) -> None:
    """Covers the DEFENSIVE branch (handler ~910-933).

    The config media-type re-check after OSCAL extraction is defensive — it fires
    only if a wrong ``manifest.config.mediaType`` "slipped past validate_bundle".
    It routes through the chokepoint, emitting ONE artifact.validation-failed
    event, sending ONE in-band DLQ rejection naming criterion
    ``config_media_type``, and returning ``_REJECTED`` (not raising silently).

    The branch is unreachable via real input (``validate_bundle`` enforces the
    config media type as a Property-4 criterion), so we force it: build a bundle
    whose config descriptor carries a wrong media type, then stub ``validate_bundle``
    to return a clean result (passing config_blob, no errors) so control reaches the
    post-OSCAL defensive re-check with the wrong media type still on the manifest.
    """
    from cairn_oci import ConfigBlob
    from cairn_validation.bundle import ValidationResult

    handler = reload_handler

    wrong_media_type = "application/vnd.oci.image.config.v1+json"
    assert wrong_media_type != CONFIG_BLOB_MEDIA_TYPE
    bundle = build_bundle(config_media_type=wrong_media_type)
    _attestation, event = submit(handler, bundle=bundle)

    # Force the defensive branch: make validate_bundle pass (clean result) despite
    # the wrong config media type, so the post-OSCAL re-check is the gate that fires.
    real_parse = ConfigBlob

    def _passing_validate_bundle(manifest, config_blob_bytes):
        return ValidationResult(
            manifest=manifest,
            config_blob=real_parse.model_validate_json(config_blob_bytes),
            errors=[],
        )

    monkeypatch.setattr(handler, "validate_bundle", _passing_validate_bundle)

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._REJECTED
    # Exactly one failure event + one DLQ rejection, both naming the defensive criterion.
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    assert any(c.get("criterion") == "config_media_type" for c in failed[0]["failedCriteria"])
    assert len(handler._test_fake_sqs.messages) == 1
    assert handler._test_fake_sqs.messages[0]["QueueUrl"] == VALIDATE_DLQ_URL
    body = handler._test_fake_sqs.bodies()[0]
    assert body["errorKind"] == "config_media_type_mismatch"
    assert body["manifestDigest"] == bundle.manifest_digest
