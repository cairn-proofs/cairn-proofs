"""mint-tag Lambda entry point — post-archive canonical tag minting.

Pipeline tail. Triggered by ``artifact.archived``. The artifact is already
durable in S3/WORM and digest-addressable; this stage gives it its friendly
ECR tag and nothing more, so a mint failure leaves an *archived-but-untagged*
(recoverable) artifact rather than a tagged-but-unarchived one.

The canonical tag has a SINGLE form ``{artifact-kind}--_ref--{digest}`` derived
purely from the validated artifact — it carries NO producer
free text. The leading lowercase-hex prefix of the Manifest_Digest is the
terminal collision-breaker, so the tag is a deterministic function of the
validated artifact — there is nothing to allocate, just build and apply.

Flow:

1. From the ``artifact.archived`` event take ONLY the addressing anchors
   needed to LOAD the validate record and address ECR — manifest digest,
   the opaque tenant id + project id (from the claim projection), repository,
   registry, region. NEVER the identity itself.
2. Read the durable validate-recorded artifact-kind via
   ``MetadataIndex.get_record(manifest_digest, tenant_id, project_id)``
   (tenant-PK key schema). Mint sources the ``artifact-kind`` segment
   SOLELY from this record's ``artifactKind``, never the event and
   never a re-parse of the OSCAL; the ``digest`` segment is the prefix of the
   Manifest_Digest addressing anchor it already holds. Every record carrying an
   ``artifactKind`` mints — there is no untagged outcome.
3. Build the tag through the grammar authority (``ArtifactLocator.build_ref_tag``)
   — never hand-format a tag.
4. Idempotent ECR ``PutImage``-with-tag on the existing digest: if the digest
   already carries the canonical tag, short-circuit; a concurrent peer that
   wins the ``put_image`` race for the SAME digest is rejected by ECR tag
   immutability and treated as an idempotent no-op. The idempotency anchor
   is the digest, never the tag.
5. Tag collision: if ``put_image`` is rejected by tag immutability
   but the tag already maps to a *different* digest, that is a legible
   fail-closed condition ("tag already mapped to a different digest") — two
   distinct digests sharing a ``(kind, digest-prefix)`` is a negligible prefix
   collision (or forgery) worth surfacing rather than silently re-disambiguating.
   The failure names the colliding DIGEST (content hash) and never echoes the
   producer free text.

On mint failure: emit a ``TagMintFailure`` metric + ``artifact.tag-failed`` and
re-raise so the failure destination / DLQ sees it; the step is safely
re-drivable on the digest.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from botocore.exceptions import ClientError
from cairn_metadata import MetadataIndex
from cairn_observability import (
    Metrics,
    MetricUnit,
    configure_root_logger,
    metric_namespace,
    observability_scope,
    set_stage_default_dimension,
)
from cairn_oci import (
    ArtifactLocator,
    InvalidTagError,
    digest_prefix,
    identity_triple,
    is_alias_tag,
)

from cairn_lambda_mint_tag.ecr_client import (
    TAG_ALREADY_EXISTS_ERRORS,
    digest_for_tag,
    fetch_manifest,
    put_image_tag,
    tags_for_digest,
)
from cairn_lambda_mint_tag.events import emit_tag_failed, emit_tagged

LOG = logging.getLogger(__name__)
configure_root_logger()

HANDLER_NAME = "mint-tag"
STAGE = os.environ.get("STAGE") or os.environ.get("CAIRN_STAGE", "")
REGION_CODE = os.environ.get("REGION_CODE") or os.environ.get("CAIRN_REGION_CODE", "")
METADATA_TABLE_NAME = os.environ.get("METADATA_TABLE_NAME", "")

if STAGE:
    os.environ.setdefault("CAIRN_STAGE", STAGE)
if REGION_CODE:
    os.environ.setdefault("CAIRN_REGION_CODE", REGION_CODE)

# Record keys validate stamps (kept in sync with validate_bundle's writer).
# The canonical tag carries NO producer free text; the artifact-kind is the only
# record-sourced tag input and the digest segment is derived from the
# Manifest_Digest anchor. Every validated record has an ``artifactKind``, so
# every artifact is tagged — there is no untagged outcome.
_REC_ARTIFACT_KIND = "artifactKind"
_REC_VALIDATE_STATUS = "validateStatus"
_VALIDATE_STATUS_DONE = "validated"

_INDEX: MetadataIndex | None = None
_LOCATOR = ArtifactLocator()


def _index() -> MetadataIndex:
    global _INDEX
    if _INDEX is None:
        if not METADATA_TABLE_NAME:
            raise RuntimeError("METADATA_TABLE_NAME env var is required")
        _INDEX = MetadataIndex(METADATA_TABLE_NAME)
    return _INDEX


metrics = Metrics(
    namespace=metric_namespace(REGION_CODE, STAGE),
    service=HANDLER_NAME,
)
set_stage_default_dimension(metrics, STAGE)


class MintError(RuntimeError):
    """Raised when minting cannot proceed (missing record, missing anchors)."""


class TagCollisionError(RuntimeError):
    """Raised when the canonical tag already maps to a DIFFERENT digest (D4).

    The Manifest_Digest prefix is the collision-breaker. The collision tuple is
    ``(artifact-kind, digest-prefix)``; the artifact kind and digest prefix are
    the only entropy sources — so if two distinct manifests (different digests)
    derive the same
    canonical tag (a negligible prefix collision or a forgery), the second mint's
    ``put_image`` is rejected by ECR tag immutability AND the tag points at a
    digest other than this artifact's. That is a legible fail-closed condition,
    not a silent re-disambiguation. The message names the colliding DIGEST (a
    content hash) and MUST NOT echo any producer free text (log hygiene).
    """


def _extract_anchors(event: dict[str, Any]) -> dict[str, str]:
    """Pull ONLY the addressing anchors from the artifact.archived event.

    Identity (artifact-kind) is deliberately NOT read here — it comes
    solely from the durable validate record. The manifest digest
    IS an addressing anchor and doubles as the tag's collision-breaker segment.

    The Metadata_Index row is addressed by ``(tenantId, projectId, digest)`` under
    the tenant-PK key schema ([META-001]); the opaque ``orgId`` /
    ``tenantId`` / ``projectId`` triple is sourced from the claim projection
    (``attestation``) threaded onto the event — never
    ``configBlob.projectId``. ``orgId`` is carried forward for provenance only.
    """
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise MintError("event.detail missing or not a dict on artifact.archived event")
    org_id, tenant_id, project_id = identity_triple(detail)
    manifest_digest = detail.get("manifestDigest")
    repository_name = detail.get("repositoryName")
    registry_id = detail.get("registryId")
    if not (tenant_id and project_id and manifest_digest and repository_name and registry_id):
        raise MintError(
            "artifact.archived detail missing one of "
            "attestation.tenantId / attestation.projectId / manifestDigest / "
            "repositoryName / registryId"
        )
    return {
        "org_id": str(org_id) if org_id else "",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "manifest_digest": str(manifest_digest),
        "repository_name": str(repository_name),
        "registry_id": str(registry_id),
        "region": str(detail.get("region") or ""),
    }


def _load_record(*, manifest_digest: str, tenant_id: str, project_id: str) -> dict[str, Any]:
    record = _index().get_record(
        manifest_digest=manifest_digest,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if record is None:
        raise MintError(
            f"no validate record for digest={manifest_digest!r} "
            f"tenant={tenant_id!r} project={project_id!r}; "
            "cannot mint without recorded identity"
        )
    if record.get(_REC_VALIDATE_STATUS) != _VALIDATE_STATUS_DONE:
        raise MintError(
            f"validate record for digest={manifest_digest!r} is not marked validated; "
            "refusing to mint"
        )
    return record


def _artifact_kind_from_record(record: dict[str, Any]) -> str:
    """Extract the validated artifact-kind from the record.

    The artifact-kind is sourced SOLELY from ``record`` — never the event, never
    a re-parse of the OSCAL. It is the only record-derived tag segment (the
    ``digest`` segment comes from the Manifest_Digest addressing anchor); the
    canonical tag carries no producer free text. A record missing ``artifactKind``
    cannot mint and is an error.
    """
    artifact_kind = record.get(_REC_ARTIFACT_KIND)
    if not artifact_kind:
        raise MintError("validate record missing artifactKind; cannot mint")
    return str(artifact_kind)


def _existing_canonical_tag(
    *, registry_id: str, repository_name: str, manifest_digest: str
) -> str | None:
    """Return an already-minted canonical tag on the digest, else None.

    Digest-anchored idempotency: a digest receives exactly one
    canonical mint. We inspect the digest's *current* tags and return the first
    that parses (via the grammar authority — never hand-matched) as a canonical
    tag. This is the re-drive short-circuit: a re-seen digest never double-mints.
    """
    for tag in tags_for_digest(
        registry_id=registry_id,
        repository_name=repository_name,
        image_digest=manifest_digest,
    ):
        try:
            parsed = _LOCATOR.parse_tag(tag)
        except InvalidTagError:
            continue  # a non-canonical tag (shouldn't happen) — ignore it
        if is_alias_tag(parsed):
            continue  # a mutable alias (_als/_chn) is never the canonical mint
        return tag
    return None


def _apply_tag(
    *,
    registry_id: str,
    repository_name: str,
    manifest_digest: str,
    image_tag: str,
) -> bool:
    """ECR PutImage-with-tag. Returns True if a tag was newly applied.

    If ``put_image`` is rejected by tag immutability, disambiguate:

    - the tag already maps to THIS digest (a concurrent peer / re-drive won the
      race) → idempotent no-op, return False;
    - the tag maps to a DIFFERENT digest → tag collision, raise
      :class:`TagCollisionError` naming the colliding digest. The digest prefix
      was supposed to disambiguate; surface a clash fail-closed rather than
      silently disambiguate. The message names the colliding DIGEST only —
      never any producer free text.
    """
    manifest_raw, media_type = fetch_manifest(
        registry_id=registry_id,
        repository_name=repository_name,
        image_digest=manifest_digest,
    )
    try:
        put_image_tag(
            registry_id=registry_id,
            repository_name=repository_name,
            manifest_raw=manifest_raw,
            media_type=media_type,
            image_tag=image_tag,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in TAG_ALREADY_EXISTS_ERRORS:
            colliding_digest = digest_for_tag(
                registry_id=registry_id,
                repository_name=repository_name,
                image_tag=image_tag,
            )
            if colliding_digest == manifest_digest:
                # Same digest (re-drive / concurrent peer) — durably present
                # either way. Idempotent no-op. Never log the tag's
                # free-text segments.
                LOG.info(
                    "put_image rejected by immutability; tag already on this digest (idempotent)",
                    extra={"manifestDigest": manifest_digest, "code": code},
                )
                return False
            if colliding_digest is not None:
                # The tag already maps to a DIFFERENT digest. Fail closed and
                # name the colliding digest (content hash). Do NOT echo any
                # producer free text.
                raise TagCollisionError(
                    "tag already mapped to a different digest "
                    f"(this artifact={manifest_digest}, "
                    f"existing tag points at digest={colliding_digest})"
                ) from e
            # Rejected as already-existing, yet the tag maps to no digest now — a
            # transient race between the reject and the lookup. Do NOT claim a
            # live tag (would emit artifact.tagged for a tag ECR isn't serving);
            # re-raise so the step re-drives, idempotent on the digest.
            raise
        raise
    return True


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    anchors = _extract_anchors(event)
    record = _load_record(
        manifest_digest=anchors["manifest_digest"],
        tenant_id=anchors["tenant_id"],
        project_id=anchors["project_id"],
    )
    artifact_kind = _artifact_kind_from_record(record)

    # Digest-anchored idempotency: if the digest already carries a
    # canonical tag, the mint already ran. Short-circuit so a re-drive never
    # double-mints. The anchor is the manifest digest, never the tag string.
    existing = _existing_canonical_tag(
        registry_id=anchors["registry_id"],
        repository_name=anchors["repository_name"],
        manifest_digest=anchors["manifest_digest"],
    )
    if existing is not None:
        LOG.info(
            "digest already carries a canonical tag; mint is a no-op",
            extra={"manifestDigest": anchors["manifest_digest"]},
        )
        return _emit_minted(anchors, existing, newly_applied=False)

    image_tag = _LOCATOR.build_ref_tag(
        artifact_kind=artifact_kind,
        digest=digest_prefix(anchors["manifest_digest"]),
    )
    newly_applied = _apply_tag(
        registry_id=anchors["registry_id"],
        repository_name=anchors["repository_name"],
        manifest_digest=anchors["manifest_digest"],
        image_tag=image_tag,
    )
    return _emit_minted(anchors, image_tag, newly_applied=newly_applied)


def _emit_minted(
    anchors: dict[str, str],
    image_tag: str,
    *,
    newly_applied: bool,
) -> dict[str, Any]:
    payload = {
        "manifestDigest": anchors["manifest_digest"],
        "repositoryName": anchors["repository_name"],
        "registryId": anchors["registry_id"],
        "region": anchors["region"],
        "tenantId": anchors["tenant_id"],
        "projectId": anchors["project_id"],
        "imageTag": image_tag,
        "newlyApplied": newly_applied,
    }
    if anchors["org_id"]:
        payload["orgId"] = anchors["org_id"]
    emit_tagged(payload)
    metrics.add_metric(name="TagMinted", unit=MetricUnit.Count, value=1)
    return payload


@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", None)
    start = time.perf_counter()
    # Per-bundle TenantId metric dimension under the uniform tenancy model;
    # sourced from the claim projection, set only when present.
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    tenant_id = identity_triple(detail)[1]
    with observability_scope(event, request_id=request_id, event_type="mint-tag"):
        if tenant_id:
            metrics.add_dimension(name="TenantId", value=tenant_id)
        try:
            payload = _handle(event)
            metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
            return payload
        except Exception as exc:
            # Archived-but-untagged failure mode: the artifact stays
            # durable + digest-addressable. Surface the failure as a metric so
            # it is not silent, emit artifact.tag-failed for the alarm/DLQ
            # path, and re-raise so the Lambda failure destination routes it.
            # Re-driving the mint on the same digest is safe (idempotent).
            metrics.add_metric(name="TagMintFailure", unit=MetricUnit.Count, value=1)
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            LOG.exception("tag mint failed; artifact remains archived + digest-addressable")
            _emit_tag_failed_safe(event, exc)
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=elapsed_ms)


def _emit_tag_failed_safe(event: dict[str, Any], exc: Exception) -> None:
    """Best-effort artifact.tag-failed emit; never masks the original error."""
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    org_id, tenant_id, project_id = identity_triple(detail)
    try:
        emit_tag_failed(
            {
                "manifestDigest": detail.get("manifestDigest"),
                "repositoryName": detail.get("repositoryName"),
                "registryId": detail.get("registryId"),
                "orgId": org_id,
                "tenantId": tenant_id,
                "projectId": project_id,
                "mintError": str(exc),
            }
        )
    except Exception:
        LOG.exception("failed to emit artifact.tag-failed (original mint error still raised)")
