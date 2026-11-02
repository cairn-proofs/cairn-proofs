"""archive-to-s3 Lambda entry point.

Pipeline stage 3. Triggered by ``artifact.signed``. Streams every blob
from ECR (manifest, Config_Blob, every content layer, signature
artifact(s)), verifies SHA-256 against the manifest-declared digests
([WORM-003.02]), writes them to the S3 archive bucket under the
canonical key prefix ([WORM-002]), generates a WORM_Receipt ([WORM-005]), and
attaches the receipt as an OCI Referrer to the source manifest.

WORM_Enabled is read from the ``WORM_ENABLED`` env var (default
``false``). The receipt schema is identical either way; the
WORM_Enabled=true branch adds Object Lock headers to the S3 PutObjects.

Retention ([RETAIN-003], [RETAIN-007]): the retention period in days is read
at runtime from the stack-written SSM parameter named by
``RETENTION_PERIOD_PARAM`` (by name, no label or version selector). A missing,
non-integer, or out-of-bounds value fails the archive with
``artifact.archive-failed`` before any S3 write. Retain_Until is the archive
timestamp plus that many days, from ONE UTC clock reading, in both
``WORM_Enabled`` modes; it is recorded in the receipt body as ``retainUntil``,
as the receipt referrer annotation ``dev.cairnproofs.artifact.retain-until``,
and (WORM only) as every object's ``ObjectLockRetainUntilDate``.

Idempotency ([PIPELINE-007]): if the S3 manifest object already exists AND a
WORM_Receipt referrer is attached in ECR, short-circuit and re-emit
``artifact.archived``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from cairn_observability import (
    Metrics,
    MetricUnit,
    configure_root_logger,
    metric_namespace,
    observability_scope,
    set_stage_default_dimension,
)
from cairn_oci import (
    MAX_ARTIFACT_SIZE_BYTES,
    RESOURCE_ARTIFACT_TYPE,
    OciManifest,
    compute_sha256_digest,
    identity_triple,
    verify_digest,
)
from cairn_oci.digest import IntegrityError, SizeIntegrityError

from cairn_lambda_archive_to_s3.ecr_client import (
    fetch_blob,
    fetch_manifest,
    put_blob,
    put_referrer_artifact,
)
from cairn_lambda_archive_to_s3.events import emit_archive_failed, emit_archived
from cairn_lambda_archive_to_s3.receipt import (
    RECEIPT_ARTIFACT_TYPE,
    build_receipt,
    build_receipt_manifest,
)
from cairn_lambda_archive_to_s3.referrers import (
    existing_receipt_referrer,
    existing_resource_referrer,
)
from cairn_lambda_archive_to_s3.resource_referrer import build_resource_referrer_manifest

LOG = logging.getLogger(__name__)
configure_root_logger()

HANDLER_NAME = "archive-to-s3"
STAGE = os.environ.get("STAGE") or os.environ.get("CAIRN_STAGE", "")
REGION_CODE = os.environ.get("REGION_CODE") or os.environ.get("CAIRN_REGION_CODE", "")
ARCHIVE_BUCKET = os.environ.get("ARCHIVE_BUCKET", "")
WORM_ENABLED = os.environ.get("WORM_ENABLED", "false").lower() == "true"
RETENTION_PERIOD_PARAM = os.environ.get("RETENTION_PERIOD_PARAM", "")

# [RETAIN-007] retention-period bounds, in days (1 day to 99 x 365).
RETENTION_PERIOD_MIN_DAYS = 1
RETENTION_PERIOD_MAX_DAYS = 36135
_WHOLE_NUMBER = re.compile(r"[0-9]+")

if STAGE:
    os.environ.setdefault("CAIRN_STAGE", STAGE)
if REGION_CODE:
    os.environ.setdefault("CAIRN_REGION_CODE", REGION_CODE)

_S3: Any = None
_SSM: Any = None


def _s3() -> Any:
    global _S3
    if _S3 is None:
        _S3 = boto3.client("s3")
    return _S3


def _ssm() -> Any:
    global _SSM
    if _SSM is None:
        _SSM = boto3.client("ssm")
    return _SSM


def _now() -> datetime:
    """The archive stage's single UTC clock reading (patched by tests)."""
    return datetime.now(UTC)


class RetentionSettingError(RuntimeError):
    """The runtime retention-period setting is missing or invalid ([RETAIN-007.03])."""


def _read_retention_period_days() -> int:
    """Read and validate the retention period from SSM ([RETAIN-007.03]).

    Reads the parameter by exact name — no ``:label`` or ``:version``
    selector — so the value is always the parameter's current version.
    Raises :class:`RetentionSettingError` when the parameter is unconfigured,
    absent, not a whole number, or outside 1..36135. Any other SSM error
    (throttling, access denied) propagates unchanged so the invocation fails
    and is retried, never defaulted.
    """
    name = RETENTION_PERIOD_PARAM
    if not name:
        raise RetentionSettingError("RETENTION_PERIOD_PARAM env var is not set")
    try:
        response = _ssm().get_parameter(Name=name)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise RetentionSettingError(f"retention-period parameter {name!r} is missing") from e
        raise
    raw = ((response.get("Parameter") or {}).get("Value") or "").strip()
    if not _WHOLE_NUMBER.fullmatch(raw):
        raise RetentionSettingError(
            f"retention-period parameter {name!r} is not a whole number of days: {raw!r}"
        )
    days = int(raw)
    if not RETENTION_PERIOD_MIN_DAYS <= days <= RETENTION_PERIOD_MAX_DAYS:
        raise RetentionSettingError(
            f"retention-period parameter {name!r} is {days} days, outside "
            f"{RETENTION_PERIOD_MIN_DAYS} to {RETENTION_PERIOD_MAX_DAYS}"
        )
    return days


def format_retain_until(value: datetime) -> str:
    """RFC 3339 UTC, second precision: ``YYYY-MM-DDTHH:MM:SSZ``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


metrics = Metrics(
    namespace=metric_namespace(REGION_CODE, STAGE),
    service=HANDLER_NAME,
)
set_stage_default_dimension(metrics, STAGE)


def _s3_key_prefix(
    org_id: str | None,
    tenant_id: str | None,
    project_id: str | None,
    manifest_digest: str,
) -> str:
    """Canonical S3 archive key prefix per [WORM-002] (uniform three-segment form).

    Produces the single uniform shape
    ``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` — three mandatory
    logical-hierarchy segments followed by the content-addressed Manifest_Digest
    as the key's sole identity/uniqueness anchor. There is
    exactly one path shape: nothing branches per segment and no segment is
    optional, omitted, defaulted, or fabricated.

    Fail-closed ([WORM-002.02]): a missing or empty ``org_id`` / ``tenant_id`` /
    ``project_id`` — or any segment carrying a ``/`` that would forge an extra
    path segment — raises :class:`ValueError` rather than emitting a key with an
    omitted, defaulted, or fabricated segment. A missing ``manifest_digest``
    likewise fails closed. The validation layer turns the raised error into a
    permanent failure (archive-failed / DLQ), never a silent default.
    """
    segments = {"org-id": org_id, "tenant-id": tenant_id, "project-id": project_id}
    for name, value in segments.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"WORM key construction requires a non-empty {name}; got {value!r}")
        if "/" in value:
            raise ValueError(f"WORM key {name} segment MUST NOT contain '/'; got {value!r}")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        raise ValueError(
            f"WORM key construction requires a non-empty manifest-digest; got {manifest_digest!r}"
        )
    return f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"


def _put_object(*, key: str, body: bytes, content_type: str, retain_until: datetime) -> None:
    """PutObject with WORM headers when enabled.

    ``retain_until`` is the bundle's single Retain_Until ([RETAIN-003]); every
    object of a bundle carries the same value so they expire together and the
    Object Lock date equals the receipt's ``retainUntil`` ([RETAIN-003.02]).
    """
    kwargs: dict[str, Any] = {
        "Bucket": ARCHIVE_BUCKET,
        "Key": key,
        "Body": body,
        "ContentType": content_type,
    }
    if WORM_ENABLED:
        kwargs["ObjectLockMode"] = "COMPLIANCE"
        kwargs["ObjectLockRetainUntilDate"] = retain_until
    _s3().put_object(**kwargs)


def _existing_manifest_in_s3(*, key: str) -> bool:
    try:
        _s3().head_object(Bucket=ARCHIVE_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _archive_blob(
    *,
    registry_id: str,
    repository_name: str,
    blob_digest: str,
    media_type: str,
    key_prefix: str,
    artifact: str,
    retain_until: datetime,
    declared_size: int | None = None,
) -> None:
    """Fetch one OCI blob (config or layer) from ECR, verify its digest, write to S3.

    The single fetch → verify → enforce-size → ``blobs/{digest}`` PutObject step
    shared by both the content-bundle archival (:func:`_archive_blobs`) and the
    Producer_Attestation_Referrer archival (:func:`_archive_attestation`). ``artifact`` is
    the IntegrityError label naming which object failed verification.

    ``declared_size`` is the descriptor's announced byte length. AFTER the digest
    is verified (so ``len(blob)`` is the TRUE, content-addressed byte count), the
    blob is rejected if it exceeds the 10 GiB :data:`MAX_ARTIFACT_SIZE_BYTES` cap
    or if its true length disagrees with the declared descriptor size. Both
    raise :class:`SizeIntegrityError` (an :class:`IntegrityError`), so they route
    through the same archive-failed / DLQ chokepoint as a digest mismatch. This
    is the AUTHORITATIVE byte-true bound — the advisory
    descriptor check upstream at validate-bundle is spoofable.

    Note the over-cap case is *first* caught in :func:`ecr_client.fetch_blob`,
    which aborts the stream (raising the same :class:`SizeIntegrityError`) once
    the true bytes exceed the cap — so a >10 GiB blob is never fully buffered.
    The ``len(blob) > MAX_ARTIFACT_SIZE_BYTES`` branch below is therefore
    defense-in-depth; the uniquely load-bearing in-handler check is the
    declared-size-vs-true-bytes reconciliation (an in-cap blob whose descriptor
    ``size`` lied), which requires the fully-fetched length.
    """
    blob = fetch_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob_digest=blob_digest,
    )
    verify_digest(blob, blob_digest, artifact=artifact)
    true_bytes = len(blob)
    if true_bytes > MAX_ARTIFACT_SIZE_BYTES:
        raise SizeIntegrityError(
            message=(
                f"true byte length {true_bytes} exceeds the "
                f"{MAX_ARTIFACT_SIZE_BYTES}-byte (10 GiB) per-artifact limit"
            ),
            computed=str(true_bytes),
            expected=str(MAX_ARTIFACT_SIZE_BYTES),
            artifact=artifact,
        )
    if declared_size is not None and true_bytes != declared_size:
        raise SizeIntegrityError(
            message=(
                f"declared descriptor size {declared_size} disagrees with "
                f"true byte length {true_bytes}"
            ),
            computed=str(true_bytes),
            expected=str(declared_size),
            artifact=artifact,
        )
    _put_object(
        key=f"{key_prefix}blobs/{blob_digest.replace(':', '/')}",
        body=blob,
        content_type=media_type,
        retain_until=retain_until,
    )


def _archive_blobs(
    *,
    registry_id: str,
    repository_name: str,
    manifest: OciManifest,
    manifest_raw: str,
    manifest_media_type: str,
    image_digest: str,
    key_prefix: str,
    retain_until: datetime,
) -> tuple[int, list[str]]:
    """Pull manifest + every layer from ECR, verify digests, write to S3.

    Returns ``(blob_count, blob_digests)``. ``blob_count`` includes the
    manifest, config blob, and every layer.
    """
    # 1. Manifest — already content-addressed by ECR, but [WORM-003.02] says
    #    the verification function should always run. Compute the SHA-256
    #    of the raw text and compare to the announced digest.
    manifest_bytes = manifest_raw.encode("utf-8")
    computed_manifest_digest = compute_sha256_digest(manifest_bytes)
    if computed_manifest_digest != image_digest:
        raise IntegrityError(
            computed=computed_manifest_digest,
            expected=image_digest,
            artifact="manifest",
        )
    _put_object(
        key=f"{key_prefix}manifest.json",
        body=manifest_bytes,
        content_type=manifest_media_type,
        retain_until=retain_until,
    )

    digests: list[str] = [image_digest]

    # 2. Config blob.
    _archive_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob_digest=manifest.config.digest,
        media_type=manifest.config.media_type,
        key_prefix=key_prefix,
        artifact="config",
        retain_until=retain_until,
        declared_size=manifest.config.size,
    )
    digests.append(manifest.config.digest)

    # 3. Layers.
    for layer in manifest.layers:
        _archive_blob(
            registry_id=registry_id,
            repository_name=repository_name,
            blob_digest=layer.digest,
            media_type=layer.media_type,
            key_prefix=key_prefix,
            artifact=f"layer[{layer.digest}]",
            retain_until=retain_until,
            declared_size=layer.size,
        )
        digests.append(layer.digest)

    return len(digests), digests


def _archive_attestation(
    *,
    registry_id: str,
    repository_name: str,
    attestation_digest: str,
    key_prefix: str,
    retain_until: datetime,
) -> None:
    """Fetch the Producer_Attestation_Referrer by digest, verify, write under the prefix.

    The Producer_Attestation_Referrer is the producer's signed compliance claim whose
    ``subject`` is the content manifest digest. It is archived as a distinct
    object (``attestation.json``) alongside the content manifest under the SAME
    canonical ``{project}/{digest}/`` prefix (digest = the CONTENT manifest
    digest — the key shape is unchanged). Its layer body (the claim JSON) and
    config blob are archived under the shared ``blobs/`` namespace so the bundle
    stays self-contained without ECR, mirroring ``_archive_blobs``. Every
    PutObject gets the same WORM treatment as the content objects.
    """
    att_dict, att_raw, att_media_type = fetch_manifest(
        registry_id=registry_id,
        repository_name=repository_name,
        image_digest=attestation_digest,
    )
    att_bytes = att_raw.encode("utf-8")
    computed = compute_sha256_digest(att_bytes)
    if computed != attestation_digest:
        raise IntegrityError(
            computed=computed,
            expected=attestation_digest,
            artifact="attestation",
        )
    _put_object(
        key=f"{key_prefix}attestation.json",
        body=att_bytes,
        content_type=att_media_type,
        retain_until=retain_until,
    )

    att_manifest = OciManifest.model_validate(att_dict)
    _archive_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob_digest=att_manifest.config.digest,
        media_type=att_manifest.config.media_type,
        key_prefix=key_prefix,
        artifact="attestation-config",
        retain_until=retain_until,
    )
    for layer in att_manifest.layers:
        _archive_blob(
            registry_id=registry_id,
            repository_name=repository_name,
            blob_digest=layer.digest,
            media_type=layer.media_type,
            key_prefix=key_prefix,
            artifact=f"attestation-layer[{layer.digest}]",
            retain_until=retain_until,
        )


def _attach_receipt_referrer(
    *,
    registry_id: str,
    repository_name: str,
    receipt_body: bytes,
    subject_digest: str,
    subject_size: int,
    subject_media_type: str,
    retain_until: str,
) -> str:
    """Push receipt body + referrer artifact manifest to ECR.

    Returns the new referrer artifact's image digest. Both writes are
    idempotent at the ECR layer: an existing blob/manifest with the same
    digest is a no-op success.
    """
    # Layer (receipt body).
    put_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob=receipt_body,
    )
    # Config blob. We don't use the OCI "empty config" pattern
    # (`application/vnd.oci.empty.v1+json`, sha256:e3b0c44…855) because
    # ECR's PutImage requires every referenced blob to be present, and
    # ECR's upload_layer_part rejects zero-byte parts (partLastByte=-1
    # is invalid). Instead we use a 2-byte `{}` placeholder so the
    # initiate/upload/complete sequence has bytes to transfer.
    placeholder_config = b"{}"
    put_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob=placeholder_config,
    )
    artifact_manifest = build_receipt_manifest(
        receipt_body=receipt_body,
        subject_digest=subject_digest,
        subject_size=subject_size,
        subject_media_type=subject_media_type,
        retain_until=retain_until,
    )
    return put_referrer_artifact(
        registry_id=registry_id,
        repository_name=repository_name,
        artifact_manifest=artifact_manifest,
    )


def _serialize_resource_reference(resource_reference: Any) -> bytes:
    """Deterministic resource-body serialization (sorted keys, compact).

    ``sort_keys=True`` is required so a re-archive of the same reference
    yields a byte-identical body and therefore a reproducible layer digest.
    """
    return json.dumps(resource_reference, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _attach_resource_referrer(
    *,
    registry_id: str,
    repository_name: str,
    resource_body: bytes,
    subject_digest: str,
    subject_size: int,
    subject_media_type: str,
) -> str:
    """Push resource body + referrer artifact manifest to ECR.

    Mirrors ``_attach_receipt_referrer``: puts the resource JSON layer body
    and the 2-byte ``{}`` placeholder config (ECR rejects zero-byte layer
    parts), then the artifact manifest. Returns the new referrer digest.
    Both writes are idempotent at the ECR layer.
    """
    put_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob=resource_body,
    )
    put_blob(
        registry_id=registry_id,
        repository_name=repository_name,
        blob=b"{}",
    )
    artifact_manifest = build_resource_referrer_manifest(
        resource_body=resource_body,
        subject_digest=subject_digest,
        subject_size=subject_size,
        subject_media_type=subject_media_type,
    )
    return put_referrer_artifact(
        registry_id=registry_id,
        repository_name=repository_name,
        artifact_manifest=artifact_manifest,
    )


def _extract_signed_detail(event: dict[str, Any]) -> dict[str, Any]:
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise ValueError("event.detail missing or not a dict on artifact.signed event")
    return detail


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    if not ARCHIVE_BUCKET:
        raise RuntimeError("ARCHIVE_BUCKET env var is required")

    detail = _extract_signed_detail(event)
    manifest_digest = detail["manifestDigest"]
    repository_name = detail["repositoryName"]
    registry_id = detail["registryId"]
    region = detail["region"]
    config_blob = detail.get("configBlob", {})
    resource_reference = detail.get("artifactResourceReference")
    # The Producer_Attestation_Referrer is the producer's signed compliance claim
    # (subject = the content manifest digest). It is archived as a second
    # object alongside the content under the same content-digest key prefix,
    # and carried forward in artifact.archived for the metadata stage. May be
    # absent; archive only what is present.
    attestation_digest = detail.get("attestationDigest")
    attestation = detail.get("attestation")

    # Identity triple: the opaque org/tenant/project coordinates
    # that address the WORM key and are threaded forward on artifact.archived for
    # the update_metadata / mint_tag stages. Sourced from the signed claim (with a
    # top-level event-contract override); a missing segment fails the key build
    # closed ([WORM-002.02]).
    org_id, tenant_id, project_id = identity_triple(detail, include_top_level=True)
    key_prefix = _s3_key_prefix(org_id, tenant_id, project_id, manifest_digest)

    # Per-invocation TenantId metric dimension under the CairnProofs namespace
    # ([OBS-009]/observability). tenant_id is guaranteed non-empty here — the
    # key build above fails closed otherwise.
    metrics.add_dimension(name="TenantId", value=tenant_id)

    # Idempotency: short-circuit if both the S3 manifest object AND the
    # WORM_Receipt referrer are already in place.
    if _existing_manifest_in_s3(key=f"{key_prefix}manifest.json"):
        existing_referrer = existing_receipt_referrer(
            registry_id=registry_id,
            repository_name=repository_name,
            image_digest=manifest_digest,
        )
        if existing_referrer:
            LOG.info(
                "archive already complete; short-circuiting",
                extra={"manifestDigest": manifest_digest, "wormReceiptDigest": existing_referrer},
            )
            payload = {
                "manifestDigest": manifest_digest,
                "repositoryName": repository_name,
                "registryId": registry_id,
                "region": region,
                "orgId": org_id,
                "tenantId": tenant_id,
                "projectId": project_id,
                "s3Bucket": ARCHIVE_BUCKET,
                "s3KeyPrefix": key_prefix,
                "wormReceiptDigest": existing_referrer,
                "wormEnabled": WORM_ENABLED,
                "archivedAt": datetime.now(UTC).isoformat(),
                "idempotentShortCircuit": True,
                "configBlob": config_blob,
                "attestationDigest": attestation_digest,
                "attestation": attestation,
                "authenticatedPushPrincipal": detail.get("authenticatedPushPrincipal"),
                "ecrPushedAt": detail.get("ecrPushedAt"),
            }
            emit_archived(payload)
            return payload

    # Fresh archive. Resolve retention FIRST: an invalid setting must fail the
    # archive before any S3 object or receipt is written ([RETAIN-007.03]).
    try:
        retention_days = _read_retention_period_days()
    except RetentionSettingError as e:
        LOG.error("invalid retention-period setting", extra={"reason": str(e)})
        emit_archive_failed(
            {
                "manifestDigest": manifest_digest,
                "repositoryName": repository_name,
                "artifact": "retention-period-setting",
                "invalidSetting": RETENTION_PERIOD_PARAM or "RETENTION_PERIOD_PARAM",
                "reason": str(e),
                "configBlob": config_blob,
                "attestationDigest": attestation_digest,
                "attestation": attestation,
            }
        )
        raise
    # ONE clock reading for the whole bundle ([RETAIN-003]): the archive
    # timestamp, and Retain_Until = timestamp + period days. Second precision so
    # the receipt, the annotation, and every Object Lock date are equal.
    archived_at_dt = _now().astimezone(UTC).replace(microsecond=0)
    retain_until_dt = archived_at_dt + timedelta(days=retention_days)
    retain_until = format_retain_until(retain_until_dt)

    manifest_dict, manifest_raw, manifest_media_type = fetch_manifest(
        registry_id=registry_id,
        repository_name=repository_name,
        image_digest=manifest_digest,
    )
    manifest = OciManifest.model_validate(manifest_dict)

    try:
        blob_count, _digests = _archive_blobs(
            registry_id=registry_id,
            repository_name=repository_name,
            manifest=manifest,
            manifest_raw=manifest_raw,
            manifest_media_type=manifest_media_type,
            image_digest=manifest_digest,
            key_prefix=key_prefix,
            retain_until=retain_until_dt,
        )
        # Archive the Producer_Attestation_Referrer as a second object under the SAME
        # content-digest prefix. Inside the try so a claim-object digest
        # mismatch routes through the same archive-failed/DLQ path.
        if attestation_digest:
            _archive_attestation(
                registry_id=registry_id,
                repository_name=repository_name,
                attestation_digest=attestation_digest,
                key_prefix=key_prefix,
                retain_until=retain_until_dt,
            )
    except IntegrityError as e:
        LOG.error(
            "digest mismatch during archive",
            extra={"computed": e.computed, "expected": e.expected, "artifact": e.artifact},
        )
        emit_archive_failed(
            {
                "manifestDigest": manifest_digest,
                "repositoryName": repository_name,
                "computedDigest": e.computed,
                "expectedDigest": e.expected,
                "artifact": e.artifact,
                "configBlob": config_blob,
                "attestationDigest": attestation_digest,
                # Thread the signed claim forward so update_metadata can source
                # the orgId/tenantId/projectId triple to address the unsigned row
                # ([META-001]). Without it, the consumer's
                # identity_triple(detail) yields (None, None, None) and raises
                # ValueError, so archiveStatus never advances to "failed".
                "attestation": attestation,
            }
        )
        # Permanent: re-raise so destination-on-failure DLQ-routes it
        # (the archive-failed event is the canonical alarm signal).
        raise

    archived_at = archived_at_dt.isoformat()
    receipt_body = build_receipt(
        manifest_digest=manifest_digest,
        archived_at=archived_at,
        s3_bucket=ARCHIVE_BUCKET,
        s3_key_prefix=key_prefix,
        archive_operator=f"archive-to-s3:{STAGE}",
        worm_enabled=WORM_ENABLED,
        blob_count=blob_count,
        integrity_verified=True,
        retain_until=retain_until,
    )
    # Write a copy of the receipt to S3 too (the bundle stays "self-contained
    # without ECR" per [WORM-007] even though the canonical receipt is the ECR
    # referrer).
    _put_object(
        key=f"{key_prefix}worm-receipt.json",
        body=receipt_body,
        content_type="application/json",
        retain_until=retain_until_dt,
    )

    subject_size = len(manifest_raw.encode("utf-8"))
    worm_receipt_digest = _attach_receipt_referrer(
        registry_id=registry_id,
        repository_name=repository_name,
        receipt_body=receipt_body,
        subject_digest=manifest_digest,
        subject_size=subject_size,
        subject_media_type=manifest_media_type,
        retain_until=retain_until,
    )

    # Resource-as-referrer: when the signed event carries an OSCAL resource
    # reference, attach it as a second referrer. Absent (non-OSCAL /
    # no-bucket bundles) → mint nothing. Idempotent: reuse a prior mint.
    resource_referrer_digest: str | None = None
    if resource_reference is not None:
        resource_referrer_digest = existing_resource_referrer(
            registry_id=registry_id,
            repository_name=repository_name,
            image_digest=manifest_digest,
        )
        if resource_referrer_digest:
            LOG.info(
                "resource referrer already present; reusing",
                extra={
                    "manifestDigest": manifest_digest,
                    "oscalResourceReferrerDigest": resource_referrer_digest,
                },
            )
        else:
            resource_body = _serialize_resource_reference(resource_reference)
            resource_referrer_digest = _attach_resource_referrer(
                registry_id=registry_id,
                repository_name=repository_name,
                resource_body=resource_body,
                subject_digest=manifest_digest,
                subject_size=subject_size,
                subject_media_type=manifest_media_type,
            )

    payload = {
        "manifestDigest": manifest_digest,
        "repositoryName": repository_name,
        "registryId": registry_id,
        "region": region,
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "s3Bucket": ARCHIVE_BUCKET,
        "s3KeyPrefix": key_prefix,
        "wormReceiptDigest": worm_receipt_digest,
        "wormReceiptType": RECEIPT_ARTIFACT_TYPE,
        "wormEnabled": WORM_ENABLED,
        "archivedAt": archived_at,
        "retainUntil": retain_until,
        "blobCount": blob_count,
        "idempotentShortCircuit": False,
        "configBlob": config_blob,
        "attestationDigest": attestation_digest,
        "attestation": attestation,
        "authenticatedPushPrincipal": detail.get("authenticatedPushPrincipal"),
        "ecrPushedAt": detail.get("ecrPushedAt"),
    }
    if resource_referrer_digest:
        payload["oscalResourceReferrerDigest"] = resource_referrer_digest
        payload["oscalResourceReferrerType"] = RESOURCE_ARTIFACT_TYPE
    emit_archived(payload)
    return payload


@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", None)
    start = time.perf_counter()
    with observability_scope(event, request_id=request_id, event_type="archive"):
        try:
            payload = _handle(event)
            metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
            return payload
        except IntegrityError:
            metrics.add_metric(name="IntegrityMismatch", unit=MetricUnit.Count, value=1)
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            raise
        except Exception:
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=elapsed_ms)
