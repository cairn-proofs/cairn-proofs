"""Discover an existing WORM_Receipt OCI Referrer for a manifest.

Used by the archive-to-s3 idempotency check: if both the S3 manifest
object is present AND a receipt referrer is attached in ECR, the prior
archive run completed and we just re-emit ``artifact.archived``.
"""

from __future__ import annotations

from typing import Any

import boto3
from cairn_oci import RESOURCE_ARTIFACT_TYPE

from cairn_lambda_archive_to_s3.receipt import RECEIPT_ARTIFACT_TYPE

_ECR_CLIENT: Any = None


def _ecr() -> Any:
    global _ECR_CLIENT
    if _ECR_CLIENT is None:
        _ECR_CLIENT = boto3.client("ecr")
    return _ECR_CLIENT


def existing_receipt_referrer(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> str | None:
    """Return the digest of an existing WORM_Receipt referrer, or None.

    boto3's ``list_image_referrers`` op has no registered paginator, so we
    walk ``nextToken`` manually and filter referrers by ``artifactType``.
    """
    ecr = _ecr()
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "registryId": registry_id,
            "repositoryName": repository_name,
            "subjectId": {"imageDigest": image_digest},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = ecr.list_image_referrers(**kwargs)
        for ref in resp.get("referrers", []):
            if ref.get("artifactType") == RECEIPT_ARTIFACT_TYPE:
                digest = ref.get("imageDigest") or ref.get("digest")
                if digest:
                    return str(digest)
        next_token = resp.get("nextToken")
        if not next_token:
            return None


def existing_resource_referrer(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> str | None:
    """Return the digest of an existing OSCAL-resource referrer, or None.

    Mirrors ``existing_receipt_referrer`` exactly, filtering by the
    resource ``artifactType`` instead. boto3's ``list_image_referrers`` op
    has no registered paginator, so we walk ``nextToken`` manually.
    """
    ecr = _ecr()
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "registryId": registry_id,
            "repositoryName": repository_name,
            "subjectId": {"imageDigest": image_digest},
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = ecr.list_image_referrers(**kwargs)
        for ref in resp.get("referrers", []):
            if ref.get("artifactType") == RESOURCE_ARTIFACT_TYPE:
                digest = ref.get("imageDigest") or ref.get("digest")
                if digest:
                    return str(digest)
        next_token = resp.get("nextToken")
        if not next_token:
            return None
