"""Two-object archive tests.

The archive stage writes BOTH the content manifest and the triggering
Producer_Attestation_Referrer to durable storage under the SAME canonical
``{project-id}/{manifest-digest}/`` prefix (digest = the CONTENT manifest
digest — the key shape is unchanged; no period/scope/attestation-digest
segment is added). The digest-keyed ``head_object`` idempotency short-circuit
keys on the content digest and is preserved. The emitted ``artifact.archived``
event carries ``attestationDigest`` and ``attestation`` forward for the
metadata stage.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    ARCHIVE_BUCKET,
    BundleFixture,
    build_bundle,
    signed_event,
)


def _seed(handler: Any, bundle: BundleFixture) -> None:
    handler._test_fake_ecr.add_bundle(bundle)


def test_both_content_and_attestation_archived_under_content_prefix(
    reload_handler, lambda_context
) -> None:
    """Both the content manifest (manifest.json) and the Producer_Attestation_Referrer
    (attestation.json) land under the SAME ``{project}/{content-digest}/``
    prefix. The attestation digest does not appear in the key shape."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    assert payload["s3KeyPrefix"] == expected_prefix
    # The key shape is the CONTENT digest only — the attestation's own digest
    # is never a key segment.
    assert bundle.attestation_digest.split(":", 1)[1] not in payload["s3KeyPrefix"]

    # Content manifest archived as today.
    handler._test_s3.head_object(Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}manifest.json")
    # Producer_Attestation_Referrer archived as a distinct object under the same prefix.
    att_obj = handler._test_s3.get_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}attestation.json"
    )
    att_body = att_obj["Body"].read()
    assert att_body == bundle.attestation_raw.encode("utf-8")
    # Attestation claim layer + placeholder config land under the shared blobs/
    # namespace so the bundle is self-contained without ECR.
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.attestation_layer_digest.replace(':', '/')}",
    )
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.attestation_config_digest.replace(':', '/')}",
    )


def test_archived_event_carries_attestation_fields(reload_handler, lambda_context) -> None:
    """The emitted artifact.archived event forwards attestationDigest +
    attestation (the projected claim) for the metadata stage, and keeps
    configBlob.projectId + manifestDigest present for the mint_tag stage."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    archived = handler._test_recorder.details("artifact.archived")
    assert len(archived) == 1
    evt = archived[0]
    assert evt["attestationDigest"] == bundle.attestation_digest
    assert evt["attestation"] == bundle.attestation
    # mint_tag inputs preserved.
    assert evt["manifestDigest"] == bundle.manifest_digest
    assert evt["configBlob"]["projectId"] == "alpha-bank"


def test_idempotency_short_circuit_still_holds_on_content_digest(
    reload_handler, lambda_context
) -> None:
    """The head_object short-circuit keys on the CONTENT digest. When the
    content manifest.json + receipt referrer are already present, archiving is
    a no-op: no objects re-fetched, no blobs pushed, and the re-emitted event
    still carries the attestation fields."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    key_prefix = f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    handler._test_s3.put_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{key_prefix}manifest.json", Body=b"prior"
    )
    handler._test_existing_receipt_state["value"] = "sha256:" + "e" * 64

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["idempotentShortCircuit"] is True
    # No second object written by the short-circuit path.
    assert handler._test_fake_ecr.referrer_artifacts == []
    assert handler._test_fake_ecr.put_blobs == []
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        handler._test_s3.head_object(Bucket=ARCHIVE_BUCKET, Key=f"{key_prefix}attestation.json")
    # The re-emitted event still carries the attestation fields.
    assert payload["attestationDigest"] == bundle.attestation_digest
    assert payload["attestation"] == bundle.attestation


def test_attestation_digest_mismatch_routes_to_archive_failed(
    reload_handler, lambda_context
) -> None:
    """A claim-object whose raw bytes don't match its announced digest is a
    permanent failure routed through the same archive-failed/DLQ path as a
    content digest mismatch."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    # Tamper the stored attestation manifest raw so its computed digest drifts.
    handler._test_fake_ecr.manifests[bundle.attestation_digest] = (
        bundle.attestation_dict,
        bundle.attestation_raw + " ",
        "application/vnd.oci.image.manifest.v1+json",
    )
    from cairn_oci.digest import IntegrityError

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["artifact"] == "attestation"
    assert failed[0]["attestationDigest"] == bundle.attestation_digest


def test_event_without_attestation_digest_archives_content_only(
    reload_handler, lambda_context
) -> None:
    """Backward/forward-compat: a signed event with no attestationDigest (a
    pre-clean-break event) archives the content as today and emits null
    attestation fields — no attestation.json is written."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    event = signed_event(bundle=bundle)
    # The triple's explicit event-contract carrier is the top-level fields; with
    # no attestation claim present they are the sole identity source for the WORM
    # key. The content-only archive path is otherwise unchanged.
    event["detail"]["orgId"] = bundle.org_id
    event["detail"]["tenantId"] = bundle.tenant_id
    event["detail"]["projectId"] = bundle.project_id
    del event["detail"]["attestationDigest"]
    del event["detail"]["attestation"]

    payload = handler.lambda_handler(event, lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    handler._test_s3.head_object(Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}manifest.json")
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        handler._test_s3.head_object(
            Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}attestation.json"
        )
    assert payload["attestationDigest"] is None
    assert payload["attestation"] is None


def test_worm_headers_applied_to_attestation_objects(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When WORM_Enabled, the attestation object + its blobs get the same
    Object Lock retention headers as the content objects."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    monkeypatch.setattr(handler, "WORM_ENABLED", True)

    from unittest.mock import MagicMock

    captured: list[dict[str, Any]] = []
    fake_s3 = MagicMock()

    def _put_object(**kwargs):
        captured.append(kwargs)
        return {}

    fake_s3.put_object.side_effect = _put_object
    fake_s3.head_object.side_effect = handler._test_s3.head_object
    monkeypatch.setattr(handler, "_s3", lambda: fake_s3)

    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    att_calls = [c for c in captured if c["Key"] == f"{expected_prefix}attestation.json"]
    assert len(att_calls) == 1
    assert att_calls[0]["ObjectLockMode"] == "COMPLIANCE"
    assert "ObjectLockRetainUntilDate" in att_calls[0]


def test_no_period_attestation_archives_cleanly(reload_handler, lambda_context) -> None:
    """A reference-form attestation (no assessmentPeriod) archives without a
    fabricated segment — same digest-terminal prefix."""
    handler = reload_handler
    bundle = build_bundle(assessment_period=None)
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    assert payload["s3KeyPrefix"] == expected_prefix
    assert "/None/" not in payload["s3KeyPrefix"]
    att = handler._test_s3.get_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}attestation.json"
    )
    # attestation.json is the referrer manifest verbatim; its claim layer (with
    # no assessmentPeriod) is archived under blobs/.
    att_manifest = json.loads(att["Body"].read())
    assert att_manifest["subject"]["digest"] == bundle.manifest_digest
    assert "assessmentPeriod" not in bundle.attestation
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.attestation_layer_digest.replace(':', '/')}",
    )
