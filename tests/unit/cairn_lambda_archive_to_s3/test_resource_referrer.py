"""Unit tests for the OSCAL resource-as-referrer mint (resource-as-referrer)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from cairn_oci import OCI_EMPTY_MEDIA_TYPE, RESOURCE_ARTIFACT_TYPE, RESOURCE_MEDIA_TYPE

from tests.unit.cairn_lambda_archive_to_s3.conftest import (
    BundleFixture,
    build_bundle,
    signed_event,
)

# A representative OSCAL resource reference. Keys are intentionally NOT in
# sorted order so the deterministic-serialization contract is exercised.
RESOURCE_REFERENCE: dict[str, Any] = {
    "uuid": "11111111-2222-3333-4444-555555555555",
    "title": "Assessment Evidence Bundle",
    "rlinks": [{"href": "oci://example/assessment-results"}],
    "props": [{"name": "type", "value": "evidence"}],
}


def _seed(handler: Any, bundle: BundleFixture) -> None:
    handler._test_fake_ecr.add_bundle(bundle)


def _patch_existing_resource_referrer(
    handler: Any, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """conftest patches existing_receipt_referrer but not the resource one."""
    monkeypatch.setattr(handler, "existing_resource_referrer", lambda **kw: value)


def _signed_with_resource(bundle: BundleFixture) -> dict[str, Any]:
    event = signed_event(bundle=bundle)
    event["detail"]["artifactResourceReference"] = RESOURCE_REFERENCE
    return event


def _resource_referrer_manifest(handler: Any) -> dict[str, Any] | None:
    for artifact in handler._test_fake_ecr.referrer_artifacts:
        if artifact.get("artifactType") == RESOURCE_ARTIFACT_TYPE:
            return artifact
    return None


def test_resource_reference_attaches_referrer_with_correct_shape(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) artifactType, empty-config mediaType, and subject digest are correct."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    _patch_existing_resource_referrer(handler, monkeypatch, None)

    payload = handler.lambda_handler(_signed_with_resource(bundle), lambda_context)

    manifest = _resource_referrer_manifest(handler)
    assert manifest is not None
    assert manifest["artifactType"] == RESOURCE_ARTIFACT_TYPE
    assert manifest["config"]["mediaType"] == OCI_EMPTY_MEDIA_TYPE
    assert manifest["config"]["size"] == 2
    assert manifest["subject"]["digest"] == bundle.manifest_digest
    assert manifest["layers"][0]["mediaType"] == RESOURCE_MEDIA_TYPE

    assert payload["oscalResourceReferrerDigest"] == handler._test_fake_ecr.next_referrer_digest
    assert payload["oscalResourceReferrerType"] == RESOURCE_ARTIFACT_TYPE


def test_minted_layer_body_is_input_serialized_byte_for_byte(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) finding-2 guard — the layer body is the input reference serialized
    deterministically, with no re-derivation."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    _patch_existing_resource_referrer(handler, monkeypatch, None)

    handler.lambda_handler(_signed_with_resource(bundle), lambda_context)

    expected_body = json.dumps(RESOURCE_REFERENCE, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    # The resource body blob is the only non-placeholder, non-receipt blob put.
    assert expected_body in handler._test_fake_ecr.put_blobs

    manifest = _resource_referrer_manifest(handler)
    assert manifest is not None
    from cairn_oci import compute_sha256_digest

    assert manifest["layers"][0]["digest"] == compute_sha256_digest(expected_body)
    assert manifest["layers"][0]["size"] == len(expected_body)


def test_idempotent_when_resource_referrer_already_exists(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) when existing_resource_referrer returns a digest, no new resource
    referrer is minted and archive still completes."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    prior = "sha256:" + "a" * 64
    _patch_existing_resource_referrer(handler, monkeypatch, prior)

    payload = handler.lambda_handler(_signed_with_resource(bundle), lambda_context)

    assert payload["oscalResourceReferrerDigest"] == prior
    # No resource-typed referrer artifact was put (only the WORM_Receipt).
    assert _resource_referrer_manifest(handler) is None
    resource_types = [a.get("artifactType") for a in handler._test_fake_ecr.referrer_artifacts]
    assert RESOURCE_ARTIFACT_TYPE not in resource_types
    # The deterministic resource body was never pushed as a blob.
    expected_body = json.dumps(RESOURCE_REFERENCE, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    assert expected_body not in handler._test_fake_ecr.put_blobs


def test_resource_referrer_not_reminted_on_discovered_existing(
    reload_handler, lambda_context
) -> None:
    """(e) end-to-end re-archive: when ``existing_resource_referrer`` (the real
    ECR-lookup edge, here driven via conftest state) reports a prior mint, the
    fresh-archive path reuses it and mints NO second resource referrer.

    This exercises the discover-existing branch through the conftest stub rather
    than a per-test monkeypatch, closing the seam where the resource idempotency
    lookup would otherwise reach real boto3 ECR.
    """
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    prior = "sha256:" + "b" * 64
    handler._test_existing_resource_state["value"] = prior

    payload = handler.lambda_handler(_signed_with_resource(bundle), lambda_context)

    assert payload["oscalResourceReferrerDigest"] == prior
    assert payload["oscalResourceReferrerType"] == RESOURCE_ARTIFACT_TYPE
    # No resource-typed referrer artifact minted (only the WORM_Receipt).
    assert _resource_referrer_manifest(handler) is None
    expected_body = json.dumps(RESOURCE_REFERENCE, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    assert expected_body not in handler._test_fake_ecr.put_blobs


def test_retried_archive_does_not_double_mint_resource_referrer(
    reload_handler, lambda_context
) -> None:
    """(g) Idempotency regression ([PIPELINE-007]): two deliveries of the same signed
    event mint the resource referrer exactly once.

    First invocation mints (no prior referrer); we then mirror real ECR by
    setting the discovery state to the just-minted digest and re-emit the
    identical event. The second run must NOT push a second resource body blob
    nor a second resource referrer artifact, and re-emits the same digest.
    """
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    handler._test_existing_resource_state["value"] = None
    event = _signed_with_resource(bundle)

    resource_body = json.dumps(RESOURCE_REFERENCE, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )

    first = handler.lambda_handler(event, lambda_context)
    minted_digest = first["oscalResourceReferrerDigest"]
    resource_body_puts_after_first = handler._test_fake_ecr.put_blobs.count(resource_body)
    resource_referrers_after_first = [
        a
        for a in handler._test_fake_ecr.referrer_artifacts
        if a.get("artifactType") == RESOURCE_ARTIFACT_TYPE
    ]
    assert resource_body_puts_after_first == 1
    assert len(resource_referrers_after_first) == 1

    # Second delivery: ECR now reports the existing referrer (real-world
    # discovery). The S3 manifest object also exists now, but the receipt
    # short-circuit only fires when the receipt referrer is discoverable too;
    # here we keep the receipt-discovery state unset to force the fresh path and
    # prove the RESOURCE branch is the thing that stays idempotent.
    handler._test_existing_resource_state["value"] = minted_digest
    second = handler.lambda_handler(event, lambda_context)

    assert second["oscalResourceReferrerDigest"] == minted_digest
    resource_referrers_after_second = [
        a
        for a in handler._test_fake_ecr.referrer_artifacts
        if a.get("artifactType") == RESOURCE_ARTIFACT_TYPE
    ]
    # Still exactly one resource referrer artifact across both invocations.
    assert len(resource_referrers_after_second) == 1
    # No additional resource body blob pushed on the retry.
    assert handler._test_fake_ecr.put_blobs.count(resource_body) == resource_body_puts_after_first


def test_resource_body_digest_is_independent_of_input_key_order(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(f) determinism contract: two references with identical content but
    different key-insertion order serialize to byte-identical bodies and the
    same layer digest — the load-bearing re-archive idempotency invariant.

    Without ``sort_keys=True`` the two orderings would yield different layer
    digests and a spurious second mint on re-archive.
    """
    from cairn_oci import compute_sha256_digest

    ordered_a: dict[str, Any] = {
        "uuid": "99999999-8888-7777-6666-555555555555",
        "title": "Bundle",
        "rlinks": [{"href": "oci://example/x"}],
    }
    # Same content, keys inserted in a different order (and nested dict too).
    ordered_b: dict[str, Any] = {
        "rlinks": [{"href": "oci://example/x"}],
        "title": "Bundle",
        "uuid": "99999999-8888-7777-6666-555555555555",
    }
    assert list(ordered_a.keys()) != list(ordered_b.keys())

    def _mint_and_get_layer(reference: dict[str, Any]) -> dict[str, Any]:
        handler = reload_handler
        # Fresh ECR state per mint so put_blobs/referrers don't bleed across runs.
        handler._test_fake_ecr.referrer_artifacts.clear()
        handler._test_fake_ecr.put_blobs.clear()
        bundle = build_bundle()
        _seed(handler, bundle)
        handler._test_existing_resource_state["value"] = None
        event = signed_event(bundle=bundle)
        event["detail"]["artifactResourceReference"] = reference
        handler.lambda_handler(event, lambda_context)
        manifest = _resource_referrer_manifest(handler)
        assert manifest is not None
        return manifest

    layer_a = _mint_and_get_layer(ordered_a)["layers"][0]
    body_a = json.dumps(ordered_a, separators=(",", ":"), sort_keys=True).encode("utf-8")
    layer_b = _mint_and_get_layer(ordered_b)["layers"][0]

    assert layer_a["digest"] == layer_b["digest"]
    assert layer_a["digest"] == compute_sha256_digest(body_a)
    assert layer_a["size"] == layer_b["size"]


def test_no_resource_reference_mints_only_receipt(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) a detail with no artifactResourceReference mints no resource referrer
    while still minting the WORM_Receipt."""
    handler = reload_handler
    bundle = build_bundle()
    _seed(handler, bundle)
    _patch_existing_resource_referrer(handler, monkeypatch, None)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    assert "oscalResourceReferrerDigest" not in payload
    assert _resource_referrer_manifest(handler) is None
    # The WORM_Receipt referrer is still attached.
    receipt_types = [a.get("artifactType") for a in handler._test_fake_ecr.referrer_artifacts]
    assert "application/vnd.cairnproofs.artifact.worm-receipt.v1" in receipt_types
    assert payload["wormReceiptDigest"] == handler._test_fake_ecr.next_referrer_digest
