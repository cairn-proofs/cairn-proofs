"""Property-based tests for the worm-archive capability.

Covers: [WORM-002] — the uniform three-segment S3 WORM key-prefix construction
(``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/``) and the object set the
archive stage writes under that prefix. There is exactly one path shape:
nothing branches per deployment mode.
"""

from __future__ import annotations

import json

from cairn_lambda_archive_to_s3.handler import _s3_key_prefix
from cairn_oci import compute_sha256_digest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies — lowercase-alphanumeric ids (matching the sibling pipeline
# property tests) and well-formed sha256 manifest digests.
# ---------------------------------------------------------------------------

_id_edge = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789")
_id_inner = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-")


@st.composite
def lc_id(draw: st.DrawFn) -> str:
    length = draw(st.integers(min_value=1, max_value=20))
    if length == 1:
        return draw(_id_edge)
    inner = draw(st.text(alphabet=_id_inner, min_size=length - 2, max_size=length - 2))
    return draw(_id_edge) + inner + draw(_id_edge)


_sha256_digest = st.from_regex(r"^sha256:[0-9a-f]{64}$", fullmatch=True)


# ---------------------------------------------------------------------------
# [WORM-002] — uniform S3 WORM key structure construction (for any valid inputs)
# ---------------------------------------------------------------------------
# Source: worm-archive/spec.md "Canonical S3 archive key structure" ([WORM-002]).
# Single uniform digest-terminal form:
#   {org-id}/{tenant-id}/{project-id}/{manifest-digest}/
# No deployment-mode branch, no account-id segment, and no assessment-period,
# bundle-type, or control-scope segment.


@given(
    org_id=lc_id(),
    tenant_id=lc_id(),
    project_id=lc_id(),
    manifest_digest=_sha256_digest,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=80)
def test_property_8_uniform_key_prefix_character_for_character(
    org_id: str, tenant_id: str, project_id: str, manifest_digest: str
) -> None:
    """**WHEN** the key construction function is invoked for any valid org-id,
    tenant-id, project-id, and manifest-digest **THEN** the produced key prefix is
    exactly ``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` — the single
    uniform three-segment shape, with no account-id segment and no
    assessment-period/bundle-type/control-scope segment.

    Source: worm-archive/spec.md ([WORM-002] — uniform three-segment form).
    """
    prefix = _s3_key_prefix(org_id, tenant_id, project_id, manifest_digest)
    assert prefix == f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"
    # No fabricated/extra segments: exactly four '/'-terminated parts.
    assert prefix.split("/") == [org_id, tenant_id, project_id, manifest_digest, ""]


@given(
    org_id=lc_id(),
    tenant_id=lc_id(),
    project_id=lc_id(),
    manifest_digest=_sha256_digest,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=60)
def test_property_8_no_period_resolution_for_reference_artifact(
    org_id: str, tenant_id: str, project_id: str, manifest_digest: str
) -> None:
    """**WHEN** the key construction function is invoked for a reference artifact
    (e.g. an OSCAL catalog/profile with no assessment period) **THEN** the produced
    key is the same uniform three-segment shape with no fabricated period segment.

    The uniform key carries no assessment-period/bundle-type segment at all, so a
    reference artifact can never acquire a fabricated or ``None`` segment.

    Source: worm-archive/spec.md ([WORM-002] — no fabricated segment).
    """
    prefix = _s3_key_prefix(org_id, tenant_id, project_id, manifest_digest)
    assert prefix == f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"
    assert "None" not in prefix


@given(
    org_id=lc_id(),
    tenant_id=lc_id(),
    project_id=lc_id(),
    manifest_digest=_sha256_digest,
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=60)
def test_property_8_object_keys_nested_under_uniform_prefix(
    org_id: str, tenant_id: str, project_id: str, manifest_digest: str
) -> None:
    """**WHEN** the uniform key prefix is used to address the bundle's full object
    set **THEN** every derived object key (manifest, Config_Blob blob, a
    content-layer blob, the WORM receipt) is nested under the
    ``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` prefix.

    The object-set coverage is asserted end-to-end through the moto-backed handler
    below; this composition-level check confirms nothing escapes the uniform
    tenant-scoped root (tenant key-isolation anchor).

    Source: worm-archive/spec.md ([WORM-002] — object-set coverage / tenant key
    isolation).
    """
    prefix = _s3_key_prefix(org_id, tenant_id, project_id, manifest_digest)
    layer_digest = "sha256:" + "b" * 64
    object_keys = [
        f"{prefix}manifest.json",
        f"{prefix}blobs/{manifest_digest.replace(':', '/')}",
        f"{prefix}blobs/{layer_digest.replace(':', '/')}",
        f"{prefix}worm-receipt.json",
    ]
    expected_root = f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"
    for key in object_keys:
        # Every object in the set is addressed under the uniform tenant-scoped
        # root — nothing escapes the prefix (cross-tenant isolation anchor).
        assert key.startswith(expected_root)


def test_property_8_object_set_covers_manifest_config_layers_and_signature(
    reload_archive_handler, lambda_context, signed_event, build_bundle
) -> None:
    """**WHEN** the archive stage writes a bundle under the canonical key prefix
    **THEN** the produced object set covers the OCI manifest, Config_Blob, all
    content layer blobs, and the signature artifact(s).

    The Config_Blob, layers, and manifest land in S3 under the uniform
    three-segment digest-terminal prefix; the signature artifact is the
    WORM_Receipt OCI Referrer attached back to the source manifest in ECR (the
    canonical signature/attestation surface).

    Source: worm-archive/spec.md ([WORM-002] — object-set coverage).
    """
    handler = reload_archive_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)

    payload = handler.lambda_handler(signed_event(bundle=bundle), lambda_context)

    prefix = payload["s3KeyPrefix"]
    assert prefix == (
        f"{bundle.org_id}/{bundle.tenant_id}/{bundle.project_id}/{bundle.manifest_digest}/"
    )

    # Manifest object.
    handler._test_s3.head_object(Bucket=handler.ARCHIVE_BUCKET, Key=f"{prefix}manifest.json")
    # Config_Blob object.
    config = handler._test_s3.get_object(
        Bucket=handler.ARCHIVE_BUCKET,
        Key=f"{prefix}blobs/{bundle.config_digest.replace(':', '/')}",
    )
    assert compute_sha256_digest(config["Body"].read()) == bundle.config_digest
    # Every content layer blob.
    layer = handler._test_s3.get_object(
        Bucket=handler.ARCHIVE_BUCKET,
        Key=f"{prefix}blobs/{bundle.layer_digest.replace(':', '/')}",
    )
    assert compute_sha256_digest(layer["Body"].read()) == bundle.layer_digest

    # Signature artifact: the WORM_Receipt referrer attached to the manifest.
    assert len(handler._test_fake_ecr.referrer_artifacts) == 1
    artifact = handler._test_fake_ecr.referrer_artifacts[0]
    assert artifact["subject"]["digest"] == bundle.manifest_digest
    assert artifact["layers"][0]["mediaType"] == (
        "application/vnd.cairnproofs.artifact.worm-receipt.v1+json"
    )
    # And the receipt copy is in S3 too, keeping the bundle self-contained.
    receipt = handler._test_s3.get_object(
        Bucket=handler.ARCHIVE_BUCKET, Key=f"{prefix}worm-receipt.json"
    )
    receipt_body = json.loads(receipt["Body"].read())
    assert receipt_body["manifestDigest"] == bundle.manifest_digest
