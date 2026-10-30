"""Tests for permanent-failure DLQ routing.

Permanent validation failures are delivered to the DLQ *in-band* with a
non-retryable return, rather than raised out of the handler (which would route
to the DLQ only after Lambda's async-invoke retries are exhausted).

These tests assert the contract:

  - `lambda_handler` RETURNS the `handler._REJECTED` sentinel and does NOT
    raise for a permanently-invalid bundle (and for a malformed ECR event).
  - exactly one SQS rejection is sent to `VALIDATE_DLQ_URL`, carrying the
    manifest digest + failure reason (`failedCriteria` / `errorKind`).

The shared `reload_handler` fixture (conftest) sets `VALIDATE_DLQ_URL` and
injects a recording `FakeSqs` on `handler._test_fake_sqs`.
"""

from __future__ import annotations

import json

import pytest

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    VALIDATE_DLQ_URL,
    BundleFixture,
    attestation_claim,
    build_bundle,
    submit,
)


def _missing_annotation_bundle() -> BundleFixture:
    # Drop the required io.cairn project-id annotation — a Property-4 permanent
    # failure (criterion routed through the missing-annotation path).
    return build_bundle(drop_annotations=("dev.cairnproofs.artifact.project-id",))


def _malformed_oscal_bundle() -> BundleFixture:
    # An OSCAL layer whose bytes are not parseable OSCAL — OscalExtractionError,
    # which the handler upgrades to a permanent failure with errorKind from the
    # extraction error.
    return build_bundle(layer_bytes=b"{this-is-not-valid-oscal-json")


PERMANENT_BUNDLE_FACTORIES = {
    "missing_required_annotation": _missing_annotation_bundle,
    "malformed_oscal_layer": _malformed_oscal_bundle,
}


@pytest.mark.parametrize("case", sorted(PERMANENT_BUNDLE_FACTORIES))
def test_permanent_failure_returns_rejected_sentinel_without_raising(
    reload_handler, lambda_context, case: str
) -> None:
    """A permanently-invalid bundle RETURNS `_REJECTED` and does NOT raise."""
    handler = reload_handler
    bundle = PERMANENT_BUNDLE_FACTORIES[case]()
    _attestation, event = submit(handler, bundle=bundle)

    # Contract: a non-retryable in-band rejection, returned not raised.
    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._REJECTED
    # Exactly one in-band DLQ rejection, carrying the digest + a failure reason.
    fake_sqs = handler._test_fake_sqs
    assert len(fake_sqs.messages) == 1
    msg = fake_sqs.messages[0]
    assert msg["QueueUrl"] == VALIDATE_DLQ_URL
    body = json.loads(msg["MessageBody"])
    assert body["manifestDigest"] == bundle.manifest_digest
    # The rejection names the failure: bounded failedCriteria and/or an errorKind.
    assert body.get("failedCriteria") or body.get("errorKind")


def test_identity_mismatch_returns_rejected_sentinel_without_raising(
    reload_handler, lambda_context
) -> None:
    """A claimed identity coordinate that disagrees with the ECR path RETURNS
    `_REJECTED` and sends one in-band DLQ rejection naming the mismatch.

    The org+tenant+project cross-check is uniform (no
    deployment-mode gate); here the claim asserts a foreign tenantId while
    org-id and project-id match the path.
    """
    handler = reload_handler
    bundle = build_bundle()
    # The cross-check operand is the CLAIM's tenantId.
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(tenant_id="tnt-foreign")
    )

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._REJECTED
    assert len(handler._test_fake_sqs.messages) == 1
    body = json.loads(handler._test_fake_sqs.messages[0]["MessageBody"])
    assert body["manifestDigest"] == bundle.manifest_digest
    criteria = body.get("failedCriteria") or []
    assert any(c.get("criterion") == "f" for c in criteria)


def test_malformed_ecr_event_returns_rejected_sentinel_without_raising(
    reload_handler, lambda_context
) -> None:
    """A malformed (unparseable) ECR event RETURNS `_REJECTED` and sends one
    in-band DLQ rejection with `errorKind="malformed_event"` — no raise.
    """
    handler = reload_handler
    event = {"detail-type": "AWS API Call via CloudTrail", "detail": {}}

    result = handler.lambda_handler(event, lambda_context)

    assert result is handler._REJECTED
    assert len(handler._test_fake_sqs.messages) == 1
    body = json.loads(handler._test_fake_sqs.messages[0]["MessageBody"])
    assert body.get("errorKind") == "malformed_event"
