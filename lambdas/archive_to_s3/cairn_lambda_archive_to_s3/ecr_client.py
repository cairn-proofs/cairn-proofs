"""ECR access helpers for archive-to-s3.

Same shape as the validate-bundle Lambda's ECR client but lives here
so each Lambda is self-contained for packaging. Adds a helper to push
an OCI artifact (the WORM_Receipt) via ``put_image`` with a ``subject``
descriptor pointing at the source manifest — that's what makes it a
discoverable Referrer.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import urllib3
from cairn_oci.digest import SizeIntegrityError
from cairn_oci.manifest import MAX_ARTIFACT_SIZE_BYTES

_ECR_CLIENT: Any = None
_HTTP: urllib3.PoolManager | None = None

# Single-sourced from cairn_oci so the streaming fetch cap, the validate-time
# advisory descriptor check, and the post-fetch byte-true check in
# ``handler._archive_blob`` cannot drift (10 GiB; see cairn_oci.manifest).
_MAX_BLOB_BYTES = MAX_ARTIFACT_SIZE_BYTES


class EcrFetchError(RuntimeError):
    """Raised when an ECR fetch or referrer attach fails."""


def _ecr() -> Any:
    global _ECR_CLIENT
    if _ECR_CLIENT is None:
        _ECR_CLIENT = boto3.client("ecr")
    return _ECR_CLIENT


def _http() -> urllib3.PoolManager:
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib3.PoolManager()
    return _HTTP


def fetch_manifest(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> tuple[dict[str, Any], str, str]:
    """Return ``(parsed_manifest, raw_manifest_text, media_type)``."""
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
        raise EcrFetchError(
            f"batch_get_image returned no images for digest={image_digest!r}; failures={failures}"
        )
    image = images[0]
    raw = image.get("imageManifest")
    media_type = image.get("imageManifestMediaType") or "application/vnd.oci.image.manifest.v1+json"
    if not raw:
        raise EcrFetchError(f"batch_get_image entry has no imageManifest for {image_digest!r}")
    return json.loads(raw), raw, media_type


def fetch_blob(
    *,
    registry_id: str,
    repository_name: str,
    blob_digest: str,
) -> bytes:
    url_resp = _ecr().get_download_url_for_layer(
        registryId=registry_id,
        repositoryName=repository_name,
        layerDigest=blob_digest,
    )
    download_url = url_resp.get("downloadUrl")
    if not download_url:
        raise EcrFetchError(
            f"get_download_url_for_layer returned no downloadUrl for {blob_digest!r}"
        )
    response = _http().request("GET", download_url, preload_content=False)
    try:
        if response.status != 200:
            raise EcrFetchError(f"blob GET for {blob_digest!r} returned HTTP {response.status}")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.stream(64 * 1024):
            size += len(chunk)
            if size > _MAX_BLOB_BYTES:
                # AUTHORITATIVE over-size gate: the fetch
                # aborts as soon as the true streamed bytes exceed the 10 GiB
                # cap — before the whole blob is buffered and before any S3
                # write. Raised as a SizeIntegrityError (an IntegrityError) so
                # it routes through the handler's archive-failed / DLQ chokepoint
                # as a PERMANENT, named integrity failure rather than a transient
                # EcrFetchError. The byte-true checks in handler._archive_blob
                # are then defense-in-depth for any (in-cap) blob that still
                # disagrees with its declared descriptor size.
                raise SizeIntegrityError(
                    message=(
                        f"blob exceeds the {_MAX_BLOB_BYTES}-byte (10 GiB) "
                        f"per-artifact size cap (streamed > {size} bytes)"
                    ),
                    computed=str(size),
                    expected=str(_MAX_BLOB_BYTES),
                    artifact=f"blob[{blob_digest}]",
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.release_conn()


def put_referrer_artifact(
    *,
    registry_id: str,
    repository_name: str,
    artifact_manifest: dict[str, Any],
) -> str:
    """``put_image`` a manifest whose ``subject`` field references the source.

    Returns the new artifact's image digest. The ``subject`` field is the
    OCI Referrers API plumbing — ECR exposes the artifact under
    ``list_image_referrers`` of the subject digest.
    """
    raw = json.dumps(artifact_manifest, separators=(",", ":"))
    resp = _ecr().put_image(
        registryId=registry_id,
        repositoryName=repository_name,
        imageManifest=raw,
        imageManifestMediaType=artifact_manifest.get(
            "mediaType", "application/vnd.oci.image.manifest.v1+json"
        ),
    )
    image = resp.get("image") or {}
    image_id = image.get("imageId") or {}
    digest = image_id.get("imageDigest")
    if not digest:
        raise EcrFetchError("put_image returned no imageDigest for referrer artifact")
    return str(digest)


def put_blob(
    *,
    registry_id: str,
    repository_name: str,
    blob: bytes,
) -> None:
    """Upload a blob via the ECR layer upload sequence — idempotent.

    The OCI Referrer flow for the WORM_Receipt requires both an artifact
    manifest (via put_image) AND the manifest's referenced blob bodies to
    exist in the registry. For the receipt we have one such blob (the
    receipt JSON, sized like the Config_Blob).

    Idempotency: the placeholder config blob has a fixed digest across
    every receipt artifact, so the second bundle archived to the same
    repo hits ``LayerAlreadyExistsException`` on ``complete_layer_upload``.
    We pre-check via ``batch_check_layer_availability`` and short-circuit
    when the digest is already present.
    """
    import hashlib

    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    avail = _ecr().batch_check_layer_availability(
        registryId=registry_id,
        repositoryName=repository_name,
        layerDigests=[digest],
    )
    for layer in avail.get("layers", []):
        if layer.get("layerDigest") == digest and layer.get("layerAvailability") == "AVAILABLE":
            return
    init = _ecr().initiate_layer_upload(
        registryId=registry_id,
        repositoryName=repository_name,
    )
    upload_id = init["uploadId"]
    _ecr().upload_layer_part(
        registryId=registry_id,
        repositoryName=repository_name,
        uploadId=upload_id,
        partFirstByte=0,
        partLastByte=len(blob) - 1,
        layerPartBlob=blob,
    )
    try:
        _ecr().complete_layer_upload(
            registryId=registry_id,
            repositoryName=repository_name,
            uploadId=upload_id,
            layerDigests=[digest],
        )
    except _ecr().exceptions.LayerAlreadyExistsException:
        # Concurrent invocation completed the same digest first; the
        # blob is durably present either way.
        pass
