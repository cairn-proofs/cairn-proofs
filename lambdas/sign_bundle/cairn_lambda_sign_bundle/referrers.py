"""Notation signature referrer discovery via ECR ``list_image_referrers``.

Used for the [PIPELINE-007] idempotency check: if a signature already exists for
the (manifest, signing profile) pair, the sign-bundle handler skips
``notation sign`` and emits ``artifact.signed`` immediately using the
existing signature digest.

The signing-profile match heuristic relies on the OCI Referrer's
``artifactType`` being ``application/vnd.cncf.notary.signature`` and a
Cairn annotation we control (the signer ARN) on the referrer. Notation
itself does not stamp the signer ARN onto the referrer manifest in a
machine-readable place we can rely on across versions, so any
``notary.signature`` referrer is treated as "already signed by the
pipeline", which is safe because the trust policy has exactly one signing
profile.
"""

from __future__ import annotations

from typing import Any

import boto3

NOTARY_SIGNATURE_ARTIFACT_TYPE = "application/vnd.cncf.notary.signature"

_ECR_CLIENT: Any = None


def _ecr() -> Any:
    global _ECR_CLIENT
    if _ECR_CLIENT is None:
        _ECR_CLIENT = boto3.client("ecr")
    return _ECR_CLIENT


def existing_signature_digest(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> str | None:
    """Return the digest of an existing Notation signature referrer, or None.

    Uses the ECR OCI Referrers API (``list_image_referrers``). boto3 does
    not register a paginator for this op, so we walk ``nextToken`` manually
    and filter referrers by ``artifactType`` in-process.
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
            if ref.get("artifactType") == NOTARY_SIGNATURE_ARTIFACT_TYPE:
                digest = ref.get("imageDigest") or ref.get("digest")
                if digest:
                    return str(digest)
        next_token = resp.get("nextToken")
        if not next_token:
            return None
