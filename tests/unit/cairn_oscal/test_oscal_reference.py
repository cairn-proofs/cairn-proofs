"""Tests for cairn_oscal.reference ([OSCAL-007.01]).

`make_artifact_reference` builds an OSCAL `BackMatterResource` for inclusion in
`back-matter.resources[]` with two rlinks (ECR + S3), both carrying a
SHA-256 hash equal to the supplied `manifest_digest`. The two rlinks are
generated **regardless of `WORM_Enabled`** — the function signature
takes no WORM flag, since the S3 URI is always meaningful (the archive
workflow always writes to S3; WORM only adds Object Lock headers).
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from cairn_oscal.reference import (
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    make_artifact_reference,
)
from oscal_bindings.models import BackMatterResource, HashAlgorithm

DIGEST_HEX = "a" * 64  # bare 64-hex; the OSCAL rlink Hash carries this form
DIGEST = f"sha256:{DIGEST_HEX}"  # canonical prefixed form fed to the builder
ECR_URI = f"123456789012.dkr.ecr.us-east-2.amazonaws.com/cairn/alpha-bank@{DIGEST}"
S3_URI = f"s3://cairn-alpha-archive-us-east-2/alpha-bank/abc/{DIGEST_HEX}.tar"


def test_returns_resource_with_two_rlinks() -> None:
    """[OSCAL-007.01]: exactly two rlinks (ECR + S3) on the produced Resource."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    assert isinstance(resource, BackMatterResource)
    assert resource.rlinks is not None
    assert len(resource.rlinks) == 2


def test_rlinks_order_is_ecr_then_s3() -> None:
    """[OSCAL-007.01]: ECR rlink comes first, S3 rlink second (stable ordering)."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    assert resource.rlinks[0].href == ECR_URI
    assert resource.rlinks[1].href == S3_URI


def test_both_rlinks_carry_sha256_hash_matching_manifest_digest() -> None:
    """[OSCAL-007.01]: both rlinks have a single SHA-256 hash equal to the supplied digest."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    for rlink in resource.rlinks:
        assert rlink.hashes is not None
        assert len(rlink.hashes) == 1
        h = rlink.hashes[0]
        assert h.algorithm == HashAlgorithm.SHA_256
        assert h.value == DIGEST_HEX


def test_ecr_rlink_uses_oci_image_manifest_media_type() -> None:
    """[OSCAL-007.01]: the ECR rlink declares the OCI image manifest media type."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    assert resource.rlinks[0].media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE
    assert OCI_IMAGE_MANIFEST_MEDIA_TYPE == "application/vnd.oci.image.manifest.v1+json"


def test_s3_rlink_carries_oci_manifest_media_type() -> None:
    """[OSCAL-007.01]: the S3 rlink declares the OCI image manifest media type,
    matching the ECR rlink. Both addresses point at the same OCI manifest
    bytes, so consumers should be able to dereference either rlink with the
    same media-type expectation."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    assert resource.rlinks[1].media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE


def test_default_uuid_is_deterministic_when_not_supplied() -> None:
    """deterministic-resource-uuid: when `resource_uuid` is omitted, the
    resource UUID is the deterministic ``uuid5(CAIRN_NAMESPACE, digest)``
    derivation — byte-identical across independent calls (NOT a per-process
    random UUID4). This is what makes the Metadata_Index rebuildable from ECR +
    S3 alone (metadata-index [META-012]). Determinism in depth is covered by the
    property suite in test_deterministic_resource_uuid.py."""
    from cairn_oscal.reference import CAIRN_NAMESPACE

    r1 = make_artifact_reference(manifest_digest=DIGEST, ecr_uri=ECR_URI, s3_uri=S3_URI)
    r2 = make_artifact_reference(manifest_digest=DIGEST, ecr_uri=ECR_URI, s3_uri=S3_URI)
    assert r1.uuid == r2.uuid
    # Derived over the canonical PREFIXED form (sha256:<hex>), not bare hex.
    assert r1.uuid == str(uuid.uuid5(CAIRN_NAMESPACE, DIGEST))


def test_supplied_uuid_is_honored() -> None:
    """[OSCAL-007.01]: explicit `resource_uuid` is propagated onto the Resource."""
    explicit = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
        resource_uuid=explicit,
    )
    assert resource.uuid == explicit


def test_title_and_description_propagate() -> None:
    """Optional title and description ride onto the Resource."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
        title="Bundle 2026Q1",
        description="Q1 controls evidence",
    )
    assert resource.title == "Bundle 2026Q1"
    assert resource.description == "Q1 controls evidence"


def test_signature_takes_no_worm_flag() -> None:
    """[OSCAL-007.01]: the function signature has no WORM-related parameter,
    confirming the contract that rlinks are produced uniformly regardless
    of `WORM_Enabled`."""
    sig = inspect.signature(make_artifact_reference)
    param_names = set(sig.parameters)
    # No worm/object-lock related parameter shall exist on the signature.
    assert not any(
        "worm" in name.lower() or "object_lock" in name.lower() for name in param_names
    ), f"unexpected WORM-related parameter on make_artifact_reference: {param_names}"
    # And the actual parameter set is the stable six (the no-WORM invariant is
    # preserved; oscal_root_uuid was added by deterministic-resource-uuid).
    assert param_names == {
        "manifest_digest",
        "ecr_uri",
        "s3_uri",
        "title",
        "description",
        "resource_uuid",
        "oscal_root_uuid",
    }


def test_same_digest_supplied_for_both_rlinks_by_construction() -> None:
    """[OSCAL-007.01]: caller passes one `manifest_digest`; both rlinks share it.
    This locks in the documented contract that both URIs point at the same
    bundle and therefore MUST carry identical SHA-256 hashes."""
    resource = make_artifact_reference(
        manifest_digest=DIGEST,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    h1 = resource.rlinks[0].hashes[0]
    h2 = resource.rlinks[1].hashes[0]
    assert h1.value == h2.value == DIGEST_HEX
    assert h1.algorithm == h2.algorithm == HashAlgorithm.SHA_256


# ---------------------------------------------------------------------------
# manifest_digest validation — caller-side misuse → ValueError (not
# OscalExtractionError, which is reserved for producer-input failures).
# ---------------------------------------------------------------------------


INVALID_DIGESTS = [
    pytest.param("a" * 64, id="bare-hex-no-prefix"),
    pytest.param(f"sha256:{'A' * 64}", id="uppercase-hex"),
    pytest.param(f"sha256:{'abc' * 22}", id="too-long-66-chars"),
    pytest.param(f"sha256:{'a' * 63}", id="too-short-63-chars"),
    pytest.param("", id="empty-string"),
    pytest.param(f"sha256:{'g' * 64}", id="non-hex-char"),
]


@pytest.mark.parametrize("bad_digest", INVALID_DIGESTS)
def test_invalid_manifest_digest_raises_value_error(bad_digest: str) -> None:
    """[OSCAL-007.01] (defensive): a digest that is not the canonical prefixed
    `sha256:<hex>` form is caller-side misuse, so the builder raises
    `ValueError` (NOT `OscalExtractionError`, which is reserved for
    producer-input failures routed to the DLQ)."""
    with pytest.raises(ValueError) as exc_info:
        make_artifact_reference(
            manifest_digest=bad_digest,
            ecr_uri=ECR_URI,
            s3_uri=S3_URI,
        )
    # Message must mention the sha256 form so the caller knows how to fix.
    assert "sha256" in str(exc_info.value)


def test_invalid_manifest_digest_raises_value_error_not_oscal_extraction_error() -> None:
    """The exception type is exactly `ValueError`, not a subclass like
    `OscalExtractionError`. Distinguishing the two error types matters
    because the validate-bundle Lambda routes them differently
    (caller-bug → 5xx; producer-input bug → validation-failed event)."""
    from cairn_oscal.errors import OscalExtractionError

    with pytest.raises(ValueError) as exc_info:
        make_artifact_reference(
            manifest_digest="not-a-digest",
            ecr_uri=ECR_URI,
            s3_uri=S3_URI,
        )
    assert not isinstance(exc_info.value, OscalExtractionError)


def test_valid_prefixed_digest_does_not_raise() -> None:
    """Positive control for the validation gate — a canonical prefixed
    `sha256:<64-hex>` digest passes and produces a Resource; the rlink Hash
    carries the bare-hex body (OSCAL Hash requires bare hex)."""
    valid_hex = "0123456789abcdef" * 4  # exactly 64 lowercase hex chars
    valid_digest = f"sha256:{valid_hex}"
    resource = make_artifact_reference(
        manifest_digest=valid_digest,
        ecr_uri=ECR_URI,
        s3_uri=S3_URI,
    )
    assert isinstance(resource, BackMatterResource)
    assert resource.rlinks[0].hashes[0].value == valid_hex
