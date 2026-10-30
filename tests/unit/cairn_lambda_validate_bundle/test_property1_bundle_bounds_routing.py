"""End-to-end routing tests for the Property-1 bundle-bounds criteria.

``validate_bundle`` enforces three which-constraint criteria
(``libs/validation/cairn_validation/bundle.py``):

  - ``i1`` — content-layer count < 1 (too few)
  - ``i2`` — content-layer count > ``MAX_CONTENT_LAYERS`` (300) (too many)
  - ``j``  — ADVISORY: a layer's declared descriptor ``size`` exceeds
             ``MAX_ARTIFACT_SIZE_BYTES`` (10 GiB), named by index + digest.

These tests prove those criteria reach the in-band DLQ rejection as PERMANENT
failures, carrying the which-constraint detail in ``failedCriteria``.

WIRING NOTE (no handler change needed): the handler already collects
``validate_bundle(...).errors`` into ``failed_criteria`` and routes the
non-empty list through ``_emit_failure_event`` + ``raise
PermanentValidationFailure`` (handler.py ~L1092/L1152), which the upstream
``lambda_handler`` converts to the ``_REJECTED`` in-band DLQ rejection. So i1
and j flow through with no added wiring.

i2 EXCEPTION (Pydantic cap intercepts first): the handler resolves the content
manifest via ``OciManifest.model_validate(manifest_dict)`` BEFORE calling
``validate_bundle``. ``OciManifest.layers`` carries a pydantic
``max_length=MAX_CONTENT_LAYERS`` field cap, so a 301-layer manifest raises a
``ValidationError`` at ``model_validate`` and routes via the
``malformed_manifest`` permanent-failure path — it never reaches
``validate_bundle``'s i2. We assert THAT permanent-failure routing here. The
i2 criterion in ``validate_bundle`` is defense-in-depth (a second wall behind
the field cap) and is exercised directly via ``model_construct`` in
``tests/unit/cairn_validation/test_bundle.py``.
"""

from __future__ import annotations

import json
from typing import Any

from cairn_oci import CONFIG_BLOB_MEDIA_TYPE, compute_sha256_digest
from cairn_oci.annotations import CairnAnnotations
from cairn_oci.manifest import MAX_ARTIFACT_SIZE_BYTES, MAX_CONTENT_LAYERS

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    BundleFixture,
    config_blob_body,
    submit,
)


def _bundle_with_layers(layers: list[dict[str, Any]]) -> BundleFixture:
    """Build a content BundleFixture whose manifest carries an arbitrary layer
    list, recomputing the manifest digest so the seeded ECR + the push event
    agree. Mirrors conftest.build_bundle but lets the test own the layer list
    (count and declared sizes) to exercise the handler's real
    ``OciManifest.model_validate`` → ``validate_bundle`` path."""
    config_bytes = config_blob_body()
    config_digest = compute_sha256_digest(config_bytes)

    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": CONFIG_BLOB_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": layers,
        "annotations": {
            CairnAnnotations.PROJECT_ID: "alpha-bank",
        },
    }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    manifest_digest = compute_sha256_digest(manifest_raw.encode("utf-8"))

    # A representative first layer (for the >0-layer cases) so its blob is
    # fetchable; the layer list itself is whatever the caller supplied.
    first_layer_digest = layers[0]["digest"] if layers else ("sha256:" + "0" * 64)
    first_layer_bytes = b"{}"

    return BundleFixture(
        manifest_digest=manifest_digest,
        manifest_dict=manifest,
        manifest_raw=manifest_raw,
        config_digest=config_digest,
        config_bytes=config_bytes,
        layer_digest=first_layer_digest,
        layer_bytes=first_layer_bytes,
        repo_name="artifact-cmh-alpha/alpha-bank",
        registry_id="123456789012",
        region="us-east-2",
        image_tag="",
    )


def _layer_descriptor(*, digest_seed: str, size: int, title: str) -> dict[str, Any]:
    return {
        "mediaType": "application/oscal.catalog+json",
        "digest": "sha256:" + (digest_seed * 64)[:64],
        "size": size,
        "annotations": {"org.opencontainers.image.title": title},
    }


def test_zero_layer_bundle_routes_to_dlq_with_criterion_i1(reload_handler, lambda_context) -> None:
    """A 0-layer content bundle is rejected PERMANENTLY: the handler returns the
    ``_REJECTED`` sentinel, sends one in-band DLQ rejection, and the
    validation-failed event carries criterion ``i1``."""
    handler = reload_handler
    bundle = _bundle_with_layers([])
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    # In-band DLQ rejection (not a raise/retry), naming the bundle digest.
    sqs_bodies = handler._test_fake_sqs.bodies()
    assert len(sqs_bodies) == 1
    assert sqs_bodies[0]["manifestDigest"] == bundle.manifest_digest

    # The validation-failed event carries the which-constraint criterion.
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    criteria = failed[0]["failedCriteria"]
    assert any(c["criterion"] == "i1" for c in criteria), criteria
    # No promotion.
    assert handler._test_recorder.details("artifact.validated") == []


def test_too_many_layers_routes_to_dlq_as_permanent_failure(reload_handler, lambda_context) -> None:
    """A 301-layer manifest is rejected PERMANENTLY.

    In the handler's real path the ``OciManifest.layers`` pydantic
    ``max_length=300`` field cap intercepts at ``model_validate`` BEFORE
    ``validate_bundle`` runs, so this routes via the ``malformed_manifest``
    schema-failure path rather than ``validate_bundle``'s i2. Either way it is a
    permanent, in-band DLQ rejection — which is the property under test. (i2
    itself is covered as defense-in-depth in the cairn_validation lib tests.)"""
    handler = reload_handler
    over_count = MAX_CONTENT_LAYERS + 1
    layers = [
        _layer_descriptor(digest_seed=format(i, "x"), size=10, title=f"layer-{i}.json")
        for i in range(over_count)
    ]
    # Distinct digests so the descriptor list is well-formed (count is what matters).
    for i, layer in enumerate(layers):
        layer["digest"] = "sha256:" + format(i, "064x")
    bundle = _bundle_with_layers(layers)
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    sqs_bodies = handler._test_fake_sqs.bodies()
    assert len(sqs_bodies) == 1
    assert sqs_bodies[0]["manifestDigest"] == bundle.manifest_digest

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    # The field cap surfaces as the malformed-manifest schema-failure path; the
    # rejection still names a failure (errorKind and/or bounded failedCriteria).
    assert failed[0].get("errorKind") == "malformed_manifest"
    criteria = failed[0]["failedCriteria"]
    # The schema failure points at the layers field (the over-length list).
    assert any("layers" in c.get("field", "") for c in criteria), criteria
    assert handler._test_recorder.details("artifact.validated") == []


def test_oversized_layer_routes_to_dlq_with_criterion_j_naming_layer(
    reload_handler, lambda_context
) -> None:
    """A bundle whose layer declares a descriptor ``size`` over 10 GiB is
    rejected PERMANENTLY with criterion ``j`` naming the offending layer
    (index + digest) in the validation-failed event."""
    handler = reload_handler
    ok_layer = _layer_descriptor(digest_seed="a", size=10, title="ok.json")
    ok_layer["digest"] = "sha256:" + "a" * 64
    over_layer = _layer_descriptor(
        digest_seed="b", size=MAX_ARTIFACT_SIZE_BYTES + 1, title="huge.json"
    )
    over_layer["digest"] = "sha256:" + "b" * 64
    bundle = _bundle_with_layers([ok_layer, over_layer])
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    sqs_bodies = handler._test_fake_sqs.bodies()
    assert len(sqs_bodies) == 1
    assert sqs_bodies[0]["manifestDigest"] == bundle.manifest_digest

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    criteria = failed[0]["failedCriteria"]
    j_errors = [c for c in criteria if c["criterion"] == "j"]
    assert j_errors, criteria
    # Names the offending layer index (1) and its digest.
    assert j_errors[0]["field"] == "manifest.layers[1].size"
    assert over_layer["digest"] in j_errors[0]["message"]
    assert handler._test_recorder.details("artifact.validated") == []


def test_multiple_oversized_layers_all_reported_in_criterion_j(
    reload_handler, lambda_context
) -> None:
    """GAP: collect-all at the handler boundary. When two layers are oversized,
    the validation-failed event carries a criterion-``j`` entry for EACH,
    naming each offending index+digest — not just the first."""
    handler = reload_handler
    over0 = _layer_descriptor(digest_seed="c", size=MAX_ARTIFACT_SIZE_BYTES + 1, title="h0.json")
    over0["digest"] = "sha256:" + "c" * 64
    ok = _layer_descriptor(digest_seed="a", size=10, title="ok.json")
    ok["digest"] = "sha256:" + "a" * 64
    over2 = _layer_descriptor(digest_seed="d", size=MAX_ARTIFACT_SIZE_BYTES + 50, title="h2.json")
    over2["digest"] = "sha256:" + "d" * 64
    bundle = _bundle_with_layers([over0, ok, over2])
    _attestation, event = submit(handler, bundle=bundle)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    j_errors = [c for c in failed[0]["failedCriteria"] if c["criterion"] == "j"]
    assert len(j_errors) == 2, failed[0]["failedCriteria"]
    fields = {c["field"] for c in j_errors}
    assert fields == {"manifest.layers[0].size", "manifest.layers[2].size"}
    assert handler._test_recorder.details("artifact.validated") == []
