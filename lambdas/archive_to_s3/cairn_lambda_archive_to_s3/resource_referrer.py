"""OSCAL resource OCI artifact-manifest envelope (resource-as-referrer).

The archive stage attaches the OSCAL resource reference as a second OCI
Referrer alongside the WORM_Receipt. The resource body is the
already-built resource JSON (carried on the ``artifact.signed`` event as
``artifactResourceReference``); this builder wraps that body verbatim —
it MUST NOT rebuild or re-derive the resource (the body's bytes are the
identity anchor for the layer digest).

Mirrors ``receipt.build_receipt_manifest``: same OCI artifact-manifest
shape, ``subject`` pointing at the source bundle, and the deliberate
2-byte ``{}`` placeholder config (ECR rejects zero-byte layer parts).
"""

from __future__ import annotations

from typing import Any

from cairn_oci import (
    OCI_EMPTY_MEDIA_TYPE,
    RESOURCE_ARTIFACT_TYPE,
    RESOURCE_MEDIA_TYPE,
    compute_sha256_digest,
)


def build_resource_referrer_manifest(
    *,
    resource_body: bytes,
    subject_digest: str,
    subject_size: int,
    subject_media_type: str,
) -> dict[str, Any]:
    """Build the OCI artifact manifest wrapping the OSCAL resource reference.

    ``resource_body`` is the already-serialized resource JSON; it is wrapped
    verbatim as the single layer (no re-derivation). ``subject`` ties the
    artifact to the source bundle for Referrer discovery.
    """
    resource_digest = compute_sha256_digest(resource_body)
    # 2-byte `{}` placeholder config — see _attach_receipt_referrer comment
    # in handler.py for why we don't use the OCI empty-descriptor pattern.
    placeholder_config_digest = compute_sha256_digest(b"{}")
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": RESOURCE_ARTIFACT_TYPE,
        "config": {
            "mediaType": OCI_EMPTY_MEDIA_TYPE,
            "digest": placeholder_config_digest,
            "size": 2,
        },
        "layers": [
            {
                "mediaType": RESOURCE_MEDIA_TYPE,
                "digest": resource_digest,
                "size": len(resource_body),
            }
        ],
        "subject": {
            "mediaType": subject_media_type,
            "digest": subject_digest,
            "size": subject_size,
        },
        "annotations": {
            "dev.cairnproofs.artifact.resource-type": RESOURCE_ARTIFACT_TYPE,
        },
    }
