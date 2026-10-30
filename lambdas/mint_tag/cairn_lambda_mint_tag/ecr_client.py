"""ECR access helpers for mint-tag.

The mint applies a friendly ECR tag to an ALREADY-STORED manifest, addressed
by digest. ECR has no "tag an existing digest" verb, so the mint
re-supplies the existing manifest bytes to ``put_image`` together with an
``imageTag`` — ECR records the new tag against the same content (the digest is
unchanged). Idempotency anchor is the digest, never the tag.

Two idempotency surfaces matter:

- ``image_already_tagged`` — list the digest's existing tags; if the target
  tag is already present, the mint already ran. Re-drive is a no-op.
- ``put_image`` may still race a concurrent mint of the same tag; ECR tag
  immutability then rejects the second writer with
  ``ImageAlreadyExistsException`` / ``ImageTagAlreadyExistsException``. The
  caller treats that rejection as a successful idempotent no-op (the tag is
  durably present either way).
"""

from __future__ import annotations

import json
from typing import Any

import boto3

_ECR_CLIENT: Any = None

#: ECR PutImage rejections that mean "this exact tag is already present" — the
#: idempotent-re-drive signal under tag immutability.
TAG_ALREADY_EXISTS_ERRORS = frozenset(
    {"ImageAlreadyExistsException", "ImageTagAlreadyExistsException"}
)


class EcrMintError(RuntimeError):
    """Raised when an ECR fetch or tag-apply fails for a non-idempotent reason."""


def _ecr() -> Any:
    global _ECR_CLIENT
    if _ECR_CLIENT is None:
        _ECR_CLIENT = boto3.client("ecr")
    return _ECR_CLIENT


def fetch_manifest(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> tuple[str, str]:
    """Return ``(raw_manifest_text, media_type)`` for the stored digest."""
    resp = _ecr().batch_get_image(
        registryId=registry_id,
        repositoryName=repository_name,
        imageIds=[{"imageDigest": image_digest}],
        acceptedMediaTypes=[
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ],
    )
    images = resp.get("images") or []
    if not images:
        failures = resp.get("failures") or []
        raise EcrMintError(
            f"batch_get_image returned no images for digest={image_digest!r}; failures={failures}"
        )
    image = images[0]
    raw = image.get("imageManifest")
    media_type = image.get("imageManifestMediaType") or "application/vnd.oci.image.manifest.v1+json"
    if not raw:
        raise EcrMintError(f"batch_get_image entry has no imageManifest for {image_digest!r}")
    return raw, media_type


def tags_for_digest(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> list[str]:
    """Return the tags currently applied to ``image_digest`` (idempotency).

    Describes the image by digest and returns its ``imageTags``. A missing
    image (``ImageNotFoundException``) is treated as having no tags. This is the
    digest-anchored idempotency primitive: the mint checks whether the digest
    already carries a canonical tag before applying one, so a re-drive never
    double-mints — anchored on the digest, never the not-yet-known target tag.
    """
    try:
        resp = _ecr().describe_images(
            registryId=registry_id,
            repositoryName=repository_name,
            imageIds=[{"imageDigest": image_digest}],
        )
    except _ecr().exceptions.ImageNotFoundException:
        return []
    tags: list[str] = []
    for detail in resp.get("imageDetails") or []:
        tags.extend(detail.get("imageTags") or [])
    return tags


def digest_for_tag(
    *,
    registry_id: str,
    repository_name: str,
    image_tag: str,
) -> str | None:
    """Return the digest ``image_tag`` currently points at, or None if absent.

    Used on the tag-immutability reject path to distinguish a
    same-digest idempotent no-op from a cross-digest tag collision: the mint
    asks "which digest does this tag already address?" An unknown tag/image
    (``ImageNotFoundException``) returns None.
    """
    try:
        resp = _ecr().describe_images(
            registryId=registry_id,
            repositoryName=repository_name,
            imageIds=[{"imageTag": image_tag}],
        )
    except _ecr().exceptions.ImageNotFoundException:
        return None
    for detail in resp.get("imageDetails") or []:
        digest = detail.get("imageDigest")
        if digest:
            return str(digest)
    return None


def put_image_tag(
    *,
    registry_id: str,
    repository_name: str,
    manifest_raw: str,
    media_type: str,
    image_tag: str,
) -> str:
    """Apply ``image_tag`` to the manifest via ``put_image``; return the digest.

    Re-supplies the existing manifest bytes with the new ``imageTag``. Under
    ECR tag immutability a concurrent peer that already applied the same tag
    causes one of :data:`TAG_ALREADY_EXISTS_ERRORS`; the caller maps that to an
    idempotent no-op. The digest is unchanged by tagging.
    """
    resp = _ecr().put_image(
        registryId=registry_id,
        repositoryName=repository_name,
        imageManifest=manifest_raw,
        imageManifestMediaType=media_type,
        imageTag=image_tag,
    )
    image = resp.get("image") or {}
    image_id = image.get("imageId") or {}
    digest = image_id.get("imageDigest")
    if not digest:
        raise EcrMintError("put_image returned no imageDigest for tag apply")
    return str(digest)


def manifest_media_type(manifest_raw: str, fallback: str) -> str:
    """Best-effort media type from the manifest body, falling back as given."""
    try:
        parsed = json.loads(manifest_raw)
    except (ValueError, TypeError):
        return fallback
    if isinstance(parsed, dict):
        return str(parsed.get("mediaType") or fallback)
    return fallback
