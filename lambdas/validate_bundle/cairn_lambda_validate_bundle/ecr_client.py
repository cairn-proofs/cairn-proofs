"""ECR access helpers for the validate-bundle Lambda.

We use the ``ecr`` boto3 client for the manifest (returned inline by
``batch_get_image``) and the ``ecr`` ``get_download_url_for_layer`` API
(plus a plain HTTP GET) for blob bodies. This is the path AWS documents
for "I have an image digest, give me the raw manifest + each layer";
the ECR service does the SigV4 work for ``batch_get_image`` and the
returned download URL is presigned for the layer GET.

Why not ORAS-CLI? The Lambda would have to ship a binary and we'd lose
boto3's transparent SigV4 + IAM-role credential chain. boto3 is the
simpler dependency (no shipped binary; transparent SigV4 + IAM-role
credential chain).
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import urllib3
from cairn_oci.manifest import MAX_ARTIFACT_SIZE_BYTES

_ECR_CLIENT: Any = None
_HTTP: urllib3.PoolManager | None = None

# Per-blob ceiling for layer downloads (10 GiB per artifact-ingestion [INGEST-003]).
# Single-sourced from cairn_oci so this fetch cap cannot drift from the canonical
# per-artifact size bound. We read into memory because this Lambda fetches
# only small blobs (the Config_Blob, the producer attestation statement) and
# OSCAL layers. For an OSCAL layer the handler passes the deployment's
# per-layer OSCAL limit (the MAX_OSCAL_LAYER_BYTES environment variable,
# default cairn_oscal.DEFAULT_MAX_OSCAL_LAYER_BYTES) as ``max_bytes``. Other
# content layers are not fetched by this Lambda, just digest-checked downstream.
# (Over-ceiling without ``max_bytes`` stays a transient EcrFetchError; the
# authoritative permanent byte-true rejection is at archive-to-s3.)
_MAX_BLOB_BYTES = MAX_ARTIFACT_SIZE_BYTES
_READ_CHUNK_BYTES = 64 * 1024


class EcrFetchError(RuntimeError):
    """Raised when a manifest or blob fetch from ECR fails unexpectedly.

    This is the TRANSIENT classification: the handler lets Lambda retry on it.
    """


class BlobSizeLimitExceeded(RuntimeError):
    """The blob bytes are longer than the caller's ``max_bytes`` limit.

    This is a PERMANENT classification: the same bytes give the same result
    on a retry. ``digest`` names the blob and ``limit`` is the limit that
    applied. The read stopped after ``limit + 1`` bytes, so the full length
    is not known.
    """

    def __init__(self, digest: str, *, limit: int) -> None:
        super().__init__(f"blob {digest} has bytes longer than the limit of {limit:,} bytes")
        self.digest = digest
        self.limit = limit


class ImageNotFoundError(RuntimeError):
    """The requested image digest does not resolve in the given repository.

    Distinct from :class:`EcrFetchError` (a transient/unexpected fetch failure):
    ECR positively reported the digest absent from the repository
    (``ImageNotFound`` / ``ImageNotFoundException`` failure code on
    ``batch_get_image``). The subject-scope check treats this as a
    PERMANENT validation failure — the Producer_Attestation_Referrer's ``subject`` does
    not reside in the triggering repository — never a transient retry.
    """


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
) -> tuple[dict[str, Any], str]:
    """Fetch the OCI manifest JSON via ``ecr:BatchGetImage``.

    Returns ``(parsed_manifest, raw_manifest_text)``. The raw text is
    useful for digest verification by callers; this function does NOT
    re-verify the digest because ``batch_get_image`` is fetched by digest
    and ECR has already content-addressed the response.
    """
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
    raw = images[0].get("imageManifest")
    if not raw:
        raise EcrFetchError(
            f"batch_get_image image entry has no imageManifest for {image_digest!r}"
        )
    return json.loads(raw), raw


# ECR ``batch_get_image`` failure codes that mean the digest is positively
# absent from the repository (a permanent, not transient, condition). The exact
# spelling has varied across the API surface, so match defensively.
_IMAGE_NOT_FOUND_CODES = frozenset({"ImageNotFound", "ImageNotFoundException"})


def image_resides_in_repo(
    *,
    registry_id: str,
    repository_name: str,
    image_digest: str,
) -> bool:
    """True iff ``image_digest`` resolves in ``repository_name`` (subject-scope check).

    Confirms the Producer_Attestation_Referrer's resolved ``subject`` content manifest
    actually resides in the SAME IAM-enforced ECR repository as the triggering
    referrer (anti-hijack). Uses ``batch_get_image`` HEAD-style: a
    present digest returns an ``images`` entry; a positively-absent digest comes
    back in ``failures`` with an ``ImageNotFound`` code, for which we return
    ``False`` (a permanent foreign-scope condition). Any other failure shape is
    a transient/unexpected fault and raises :class:`EcrFetchError` so the handler
    retries rather than mislabeling an outage as a cross-scope hijack.
    """
    resp = _ecr().batch_get_image(
        registryId=registry_id,
        repositoryName=repository_name,
        imageIds=[{"imageDigest": image_digest}],
        acceptedMediaTypes=[
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ],
    )
    if resp.get("images"):
        return True
    failures = resp.get("failures") or []
    for failure in failures:
        if failure.get("failureCode") in _IMAGE_NOT_FOUND_CODES:
            return False
    # No image AND no recognized not-found failure: treat as transient/unexpected.
    raise EcrFetchError(
        f"batch_get_image for subject digest={image_digest!r} in "
        f"repo={repository_name!r} returned neither an image nor a recognized "
        f"not-found failure; failures={failures}"
    )


def fetch_blob(
    *,
    registry_id: str,
    repository_name: str,
    blob_digest: str,
    max_bytes: int | None = None,
) -> bytes:
    """Fetch a layer/config blob body by digest.

    Uses ECR's ``get_download_url_for_layer`` to obtain a presigned URL
    and an HTTP GET to pull the bytes. ECR treats Config_Blob and layer
    blobs identically at this endpoint.

    With ``max_bytes`` set, the read stops after ``max_bytes + 1`` bytes and
    raises the permanent :class:`BlobSizeLimitExceeded`. Without it, a blob
    over the per-artifact ceiling raises the transient :class:`EcrFetchError`.
    """
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
        # Read with a ceiling to guard against pathological producers. Each
        # read asks for no more than the bytes left before the ceiling plus
        # one, so the loop never reads more than ``ceiling + 1`` bytes.
        ceiling = _MAX_BLOB_BYTES if max_bytes is None else max_bytes
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = response.read(min(_READ_CHUNK_BYTES, ceiling + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > ceiling:
                if max_bytes is not None:
                    raise BlobSizeLimitExceeded(blob_digest, limit=max_bytes)
                raise EcrFetchError(
                    f"blob {blob_digest!r} exceeds per-artifact size cap ({_MAX_BLOB_BYTES} bytes)"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.release_conn()
