"""Unit tests for the archive-to-s3 Lambda handler."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    ACCOUNT_ID,
    ARCHIVE_BUCKET,
    BundleFixture,
    build_bundle,
    signed_event,
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


def _metric_value(doc: dict[str, Any], name: str) -> float | None:
    value = doc.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _seed(handler: Any, bundle: BundleFixture) -> None:
    handler._test_fake_ecr.add_bundle(bundle)


def test_happy_path_archives_writes_objects_attaches_receipt(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    event = signed_event(bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    archived = handler._test_recorder.details("artifact.archived")
    assert len(archived) == 1
    assert archived[0]["manifestDigest"] == bundle.manifest_digest
    assert archived[0]["wormEnabled"] is False
    assert archived[0]["idempotentShortCircuit"] is False
    assert archived[0]["blobCount"] == 3
    assert archived[0]["s3Bucket"] == ARCHIVE_BUCKET

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    assert payload["s3KeyPrefix"] == expected_prefix
    assert payload["wormReceiptDigest"] == handler._test_fake_ecr.next_referrer_digest
    assert payload["authenticatedPushPrincipal"]["accountId"] == ACCOUNT_ID
    assert payload["ecrPushedAt"] == "2026-05-26T12:05:00Z"

    head_manifest = handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}manifest.json"
    )
    assert head_manifest["ContentLength"] == len(bundle.manifest_raw.encode("utf-8"))

    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.config_digest.replace(':', '/')}",
    )
    handler._test_s3.head_object(
        Bucket=ARCHIVE_BUCKET,
        Key=f"{expected_prefix}blobs/{bundle.layer_digest.replace(':', '/')}",
    )
    receipt = handler._test_s3.get_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}worm-receipt.json"
    )
    receipt_body = json.loads(receipt["Body"].read())
    assert receipt_body["manifestDigest"] == bundle.manifest_digest
    assert receipt_body["wormEnabled"] is False
    assert receipt_body["integrityVerified"] is True
    assert receipt_body["blobCount"] == 3

    assert len(handler._test_fake_ecr.referrer_artifacts) == 1
    artifact = handler._test_fake_ecr.referrer_artifacts[0]
    assert artifact["subject"]["digest"] == bundle.manifest_digest
    assert artifact["layers"][0]["mediaType"] == (
        "application/vnd.cairnproofs.artifact.worm-receipt.v1+json"
    )


def test_all_derived_object_keys_under_digest_terminal_prefix(
    reload_handler, lambda_context
) -> None:
    """Every derived object key (manifest.json, blobs/..., worm-receipt.json)
    sits under the canonical ``{project-id}/{manifest-digest}/`` prefix — the
    digest is the sole identity anchor; no period/bundle-type segment appears."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    assert payload["s3KeyPrefix"] == expected_prefix
    assert "/2026-Q2/" not in payload["s3KeyPrefix"]
    assert "/assessment-results/" not in payload["s3KeyPrefix"]

    derived_keys = [
        f"{expected_prefix}manifest.json",
        f"{expected_prefix}blobs/{bundle.config_digest.replace(':', '/')}",
        f"{expected_prefix}blobs/{bundle.layer_digest.replace(':', '/')}",
        f"{expected_prefix}worm-receipt.json",
    ]
    for key in derived_keys:
        assert key.startswith(expected_prefix)
        handler._test_s3.head_object(Bucket=ARCHIVE_BUCKET, Key=key)


def test_reference_artifact_without_period_yields_no_fabricated_segment(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reference-artifact bundle whose Config_Blob carries NO assessmentPeriod
    produces a well-formed ``{project-id}/{manifest-digest}/`` key with no
    fabricated period segment."""
    handler = reload_handler
    bundle = build_bundle(assessment_period=None)
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    expected_prefix = (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )
    assert payload["s3KeyPrefix"] == expected_prefix
    assert "/None/" not in payload["s3KeyPrefix"]
    assert "/none/" not in payload["s3KeyPrefix"]
    # No fabricated date segment in the S3 key prefix.
    assert "/2026-05-26/" not in payload["s3KeyPrefix"]
    handler._test_s3.head_object(Bucket=ARCHIVE_BUCKET, Key=f"{expected_prefix}manifest.json")


def test_digest_mismatch_aborts_with_archive_failed(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    real_blobs = dict(handler._test_fake_ecr.blobs)
    tampered = b"tampered-config-payload"
    handler._test_fake_ecr.blobs[bundle.config_digest] = tampered

    from cairn_oci.digest import IntegrityError

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    failed = handler._test_recorder.details("artifact.archive-failed")
    assert len(failed) == 1
    assert failed[0]["expectedDigest"] == bundle.config_digest
    assert failed[0]["artifact"] == "config"

    handler._test_fake_ecr.blobs = real_blobs


def test_manifest_digest_mismatch_aborts(reload_handler, lambda_context) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    tampered_raw = bundle.manifest_raw + " "
    handler._test_fake_ecr.manifests[bundle.manifest_digest] = (
        bundle.manifest_dict,
        tampered_raw,
        "application/vnd.oci.image.manifest.v1+json",
    )
    from cairn_oci.digest import IntegrityError

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    failed = handler._test_recorder.details("artifact.archive-failed")
    assert failed[0]["artifact"] == "manifest"


def test_idempotency_short_circuit_when_s3_and_referrer_present(
    reload_handler, lambda_context
) -> None:
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
    assert payload["wormReceiptDigest"] == "sha256:" + "e" * 64
    assert handler._test_fake_ecr.referrer_artifacts == []
    assert handler._test_fake_ecr.put_blobs == []


def test_idempotency_skipped_when_s3_present_but_no_receipt_referrer(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    key_prefix = f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    handler._test_s3.put_object(
        Bucket=ARCHIVE_BUCKET, Key=f"{key_prefix}manifest.json", Body=b"prior"
    )
    handler._test_existing_receipt_state["value"] = None

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert payload["idempotentShortCircuit"] is False
    assert len(handler._test_fake_ecr.referrer_artifacts) == 1


def test_archive_bucket_env_required(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    monkeypatch.setattr(handler, "ARCHIVE_BUCKET", "")
    bundle = build_bundle()
    _seed(handler, bundle)
    with pytest.raises(RuntimeError, match="ARCHIVE_BUCKET"):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)


def test_event_without_detail_dict_raises_value_error(reload_handler, lambda_context) -> None:
    handler = reload_handler
    with pytest.raises(ValueError, match=r"event\.detail missing or not a dict"):
        handler.lambda_handler({"detail-type": "artifact.signed", "detail": None}, lambda_context)


def test_observability_scope_invoked_with_archive_event_type(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    captured: list[str | None] = []
    original = handler.observability_scope

    from contextlib import contextmanager

    @contextmanager
    def recording_scope(event, **kwargs):
        captured.append(kwargs.get("event_type"))
        with original(event, **kwargs) as tc:
            yield tc

    monkeypatch.setattr(handler, "observability_scope", recording_scope)
    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    assert captured == ["archive"]


def test_success_metric_on_archived_path(reload_handler, lambda_context, capsys) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)
    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert any(_metric_value(d, "DurationMs") is not None for d in docs)


def test_failure_and_integrity_mismatch_metric_on_digest_mismatch(
    reload_handler, lambda_context, capsys
) -> None:
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    handler._test_fake_ecr.blobs[bundle.config_digest] = b"tampered"
    from cairn_oci.digest import IntegrityError

    with pytest.raises(IntegrityError):
        handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Failure") == 1.0 for d in docs)
    assert any(_metric_value(d, "IntegrityMismatch") == 1.0 for d in docs)


def test_worm_enabled_branch_writes_object_lock_headers(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORM_Enabled=true adds Object Lock kwargs to every PutObject.

    Tests this without an Object-Lock bucket by stubbing the boto3 s3
    client at the handler boundary and asserting PutObject kwargs include
    ``ObjectLockMode`` + ``ObjectLockRetainUntilDate``.
    """
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

    assert captured
    for call in captured:
        assert call["ObjectLockMode"] == "COMPLIANCE"
        assert "ObjectLockRetainUntilDate" in call


# --- [WORM-002] uniform {org-id}/{tenant-id}/{project-id}/{manifest-digest}/ key ---


def test_archived_event_threads_identity_triple_topelevel(reload_handler, lambda_context) -> None:
    """The emitted artifact.archived event carries the opaque org/tenant/project
    triple as top-level fields so the downstream update_metadata / mint_tag
    stages compose on them rather than re-parsing configBlob.projectId
    ([WORM-002])."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert payload["orgId"] == bundle.org_id
    assert payload["tenantId"] == bundle.tenant_id
    assert payload["projectId"] == bundle.project_id

    archived = handler._test_recorder.details("artifact.archived")[0]
    assert archived["orgId"] == bundle.org_id
    assert archived["tenantId"] == bundle.tenant_id
    assert archived["projectId"] == bundle.project_id
    # The key prefix is exactly the uniform three-segment + digest shape.
    assert payload["s3KeyPrefix"] == (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )


def test_topelevel_triple_overrides_claim_on_re_emit(reload_handler, lambda_context) -> None:
    """[WORM-002] top-level event-contract fields take precedence over the claim
    (the idempotent-re-emit / cross-stage composition path)."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    event = signed_event(bundle=bundle)
    event["detail"]["orgId"] = "override-org"
    event["detail"]["tenantId"] = "override-tenant"
    event["detail"]["projectId"] = "override-project"

    payload = handler.lambda_handler(event, lambda_context)

    assert payload["s3KeyPrefix"] == (
        f"override-org/override-tenant/override-project/{bundle.manifest_digest}/"
    )


def test_emf_carries_tenant_id_dimension(reload_handler, lambda_context, capsys) -> None:
    """The archive-stage EMF metric carries the per-invocation TenantId dimension
    under the CairnProofs namespace ([OBS-009]/observability)."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)

    handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert any(d.get("TenantId") == bundle.tenant_id for d in docs), (
        f"expected TenantId={bundle.tenant_id!r} on an EMF line; "
        f"saw {[d.get('TenantId') for d in docs]!r}"
    )
    # The namespace root is CairnProofs, never the retired Artifact/ prefix.
    namespaces = [
        m.get("Namespace") for d in docs for m in d.get("_aws", {}).get("CloudWatchMetrics", [])
    ]
    assert namespaces, "expected at least one EMF CloudWatchMetrics namespace"
    assert all(ns and ns.startswith("CairnProofs") for ns in namespaces), namespaces


@pytest.mark.parametrize(
    ("org_id", "tenant_id", "project_id"),
    [
        ("acme-org", "tnt", "proj"),
        ("o", "t", "p"),
        ("org-01", "tenant-99", "project-xyz"),
    ],
)
def test_s3_key_prefix_uniform_shape(reload_handler, org_id, tenant_id, project_id) -> None:
    """[WORM-002.01]/[WORM-002.05] key construction yields
    {org-id}/{tenant-id}/{project-id}/{manifest-digest}/ char-for-char."""
    handler = reload_handler
    digest = "sha256:" + "a" * 64
    assert handler._s3_key_prefix(org_id, tenant_id, project_id, digest) == (
        f"{org_id}/{tenant_id}/{project_id}/{digest}/"
    )


@pytest.mark.parametrize(
    ("org_id", "tenant_id", "project_id", "digest"),
    [
        (None, "t", "p", "sha256:" + "a" * 64),
        ("", "t", "p", "sha256:" + "a" * 64),
        ("o", None, "p", "sha256:" + "a" * 64),
        ("o", "", "p", "sha256:" + "a" * 64),
        ("o", "t", None, "sha256:" + "a" * 64),
        ("o", "t", "", "sha256:" + "a" * 64),
        ("o", "t", "p", ""),
        ("o/x", "t", "p", "sha256:" + "a" * 64),  # forged extra path segment
        ("o", "t/x", "p", "sha256:" + "a" * 64),
        ("o", "t", "p/x", "sha256:" + "a" * 64),
    ],
)
def test_s3_key_prefix_fails_closed_on_missing_or_forged_segment(
    reload_handler, org_id, tenant_id, project_id, digest
) -> None:
    """[WORM-002.02] a missing/empty segment — or one carrying '/' that would
    forge an extra path segment — fails construction rather than emitting a key
    with an omitted, defaulted, or fabricated segment."""
    handler = reload_handler
    with pytest.raises(ValueError):
        handler._s3_key_prefix(org_id, tenant_id, project_id, digest)


def test_missing_tenant_in_claim_routes_to_permanent_failure(
    reload_handler, lambda_context
) -> None:
    """A signed event whose claim omits the tenant id fails closed before any S3
    write — a permanent failure (raise → destination-on-failure DLQ), never a
    key with an omitted segment ([WORM-002.02])."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    event = signed_event(bundle=bundle)
    del event["detail"]["attestation"]["tenantId"]

    with pytest.raises(ValueError, match="tenant-id"):
        handler.lambda_handler(event, lambda_context)

    # Nothing was archived under a fabricated key.
    assert handler._test_recorder.details("artifact.archived") == []
