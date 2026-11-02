"""WORM_Receipt JSON builder + OCI artifact manifest envelope ([WORM-005]).

Receipt schema: one JSON blob describing the archive operation.
Identical whether or not WORM_Enabled is set; the WORM_Enabled=true path
adds Object Lock per-object headers. ``retainUntil`` (RFC 3339 UTC) records
the bundle's Retain_Until in both modes ([WORM-005], [RETAIN-003]), and the
referrer manifest repeats it as the ``dev.cairnproofs.artifact.retain-until``
annotation so a Referrers API reader gets it without fetching the body.

The receipt is wrapped in an OCI artifact manifest with ``subject``
pointing at the source bundle, making it discoverable via the
Referrers API.
"""

from __future__ import annotations

import json
from typing import Any

from cairn_oci import RETAIN_UNTIL_ANNOTATION, WORM_RECEIPT_ARTIFACT_TYPE, compute_sha256_digest

RECEIPT_MEDIA_TYPE = "application/vnd.cairnproofs.artifact.worm-receipt.v1+json"
# Re-export the libs single source of truth as this Lambda's local mint-site
# constant (drift guard: tests pin RECEIPT_ARTIFACT_TYPE == WORM_RECEIPT_ARTIFACT_TYPE).
RECEIPT_ARTIFACT_TYPE = WORM_RECEIPT_ARTIFACT_TYPE
RECEIPT_TYPE_ANNOTATION = "dev.cairnproofs.artifact.receipt-type"


def build_receipt(
    *,
    manifest_digest: str,
    archived_at: str,
    s3_bucket: str,
    s3_key_prefix: str,
    archive_operator: str,
    worm_enabled: bool,
    blob_count: int,
    integrity_verified: bool,
    retain_until: str,
) -> bytes:
    """Render the WORM_Receipt JSON body as bytes (UTF-8)."""
    receipt = {
        "manifestDigest": manifest_digest,
        "archivedAt": archived_at,
        "s3Bucket": s3_bucket,
        "s3KeyPrefix": s3_key_prefix,
        "archiveOperator": archive_operator,
        "wormEnabled": worm_enabled,
        "blobCount": blob_count,
        "integrityVerified": integrity_verified,
        "retainUntil": retain_until,
        "schemaVersion": "v1",
    }
    return json.dumps(receipt, separators=(",", ":")).encode("utf-8")


def build_receipt_manifest(
    *,
    receipt_body: bytes,
    subject_digest: str,
    subject_size: int,
    subject_media_type: str,
    retain_until: str,
) -> dict[str, Any]:
    """Build the OCI artifact manifest wrapping the receipt ([WORM-005]).

    Uses the OCI artifact-manifest shape: ``artifactType`` carries our
    receipt type, ``layers`` holds the receipt JSON, ``subject`` ties
    the artifact to the source bundle for Referrer discovery.
    """
    receipt_digest = compute_sha256_digest(receipt_body)
    # 2-byte `{}` placeholder config — see _attach_receipt_referrer
    # comment for why we don't use the OCI empty-descriptor pattern.
    placeholder_config_digest = compute_sha256_digest(b"{}")
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": RECEIPT_ARTIFACT_TYPE,
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": placeholder_config_digest,
            "size": 2,
        },
        "layers": [
            {
                "mediaType": RECEIPT_MEDIA_TYPE,
                "digest": receipt_digest,
                "size": len(receipt_body),
            }
        ],
        "subject": {
            "mediaType": subject_media_type,
            "digest": subject_digest,
            "size": subject_size,
        },
        "annotations": {
            RECEIPT_TYPE_ANNOTATION: RECEIPT_ARTIFACT_TYPE,
            RETAIN_UNTIL_ANNOTATION: retain_until,
        },
    }
