"""update-metadata Lambda entry point.

Subscribes to every ``evidence.*`` event on the custom bus and reflects
it into the Metadata_Index:

  - ``artifact.validated``        → create_or_merge ([META-007]; row-completeness
                                    invariant — merges the rich payload onto a
                                    marker-only row validate may have pre-created
                                    off the same event rather than discarding it;
                                    populates authenticatedPushPrincipal and
                                    ecrPushedAt — [PIPELINE-009], [PIPELINE-010])
  - ``artifact.signed``           → update_on_event(signingStatus="signed",
                                    signatureDigest, attestationSignatureDigest,
                                    signedAt, signingProfileArn)
  - ``artifact.signing-failed``   → update_on_event(signingStatus="unsigned",
                                    signingError)
  - ``artifact.archived``         → update_on_event(archiveStatus="archived",
                                    s3Bucket, s3KeyPrefix, wormReceiptDigest,
                                    archivedAt)
  - ``artifact.archive-failed``   → update_on_event(archiveStatus="failed",
                                    archiveError)
  - ``artifact.validation-failed``→ log + metric; no DDB record (the bundle
                                    never got a Metadata_Index entry per design)

All updates use ``MetadataIndex.update_on_event`` which guards on the
event timestamp ([META-007.02]) so retried/out-of-order events can't
clobber newer state.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from cairn_metadata import ControlClaims, MetadataIndex
from cairn_metadata.conditional import ConditionalUpdateFailed
from cairn_observability import (
    Metrics,
    MetricUnit,
    configure_root_logger,
    metric_namespace,
    observability_scope,
    set_stage_default_dimension,
)
from cairn_oci import identity_triple

LOG = logging.getLogger(__name__)
configure_root_logger()

HANDLER_NAME = "update-metadata"
STAGE = os.environ.get("STAGE") or os.environ.get("CAIRN_STAGE", "")
REGION_CODE = os.environ.get("REGION_CODE") or os.environ.get("CAIRN_REGION_CODE", "")
METADATA_TABLE_NAME = os.environ.get("METADATA_TABLE_NAME", "")
# The Lambda env var key is CONTROLS_TABLE_NAME; the facade is the
# ControlClaims class.
CONTROLS_TABLE_NAME = os.environ.get("CONTROLS_TABLE_NAME", "")

if STAGE:
    os.environ.setdefault("CAIRN_STAGE", STAGE)
if REGION_CODE:
    os.environ.setdefault("CAIRN_REGION_CODE", REGION_CODE)

_INDEX: MetadataIndex | None = None
_CONTROLS: ControlClaims | None = None


def _index() -> MetadataIndex:
    global _INDEX
    if _INDEX is None:
        if not METADATA_TABLE_NAME:
            raise RuntimeError("METADATA_TABLE_NAME env var is required")
        _INDEX = MetadataIndex(METADATA_TABLE_NAME)
    return _INDEX


def _controls() -> ControlClaims:
    global _CONTROLS
    if _CONTROLS is None:
        if not CONTROLS_TABLE_NAME:
            raise RuntimeError("CONTROLS_TABLE_NAME env var is required")
        _CONTROLS = ControlClaims(CONTROLS_TABLE_NAME)
    return _CONTROLS


metrics = Metrics(
    namespace=metric_namespace(REGION_CODE, STAGE),
    service=HANDLER_NAME,
)
set_stage_default_dimension(metrics, STAGE)


def _event_time(event: dict[str, Any]) -> str:
    """EventBridge envelope's ``time`` field is ISO-8601 RFC 3339."""
    return str(event.get("time") or "")


def _handle_validated(event: dict[str, Any], detail: dict[str, Any]) -> None:
    """Write the rich validated-event payload via create-or-merge.

    Both validated-event writers (this stage and validate's marker write) share
    the same digest-keyed row and converge it to the union of their disjoint,
    write-once field sets via ``create_or_merge``'s per-attribute
    ``if_not_exists`` — whichever stage loses the create race merges its still-
    absent fields onto the peer's row, in either order, with no collision and no
    field regression. This stage single-owns ``createdAt`` — now the bundle's
    FIRST-SEEN time, derived from ``ecrPushedAt`` (the AWS-set EventBridge
    eventTime), NOT a producer/content timestamp — and seeds the mutable-guard
    baseline ``updatedAt`` from the envelope ``time``.
    """
    config_blob = detail.get("configBlob") or {}
    # The producer's compliance claim travels in a separately-signed Producer
    # Attestation referrer, projected onto the event under `attestation`
    # (ProducerAttestationReferrer.model_dump(by_alias=True, exclude_none=True)).
    # configBlob carries content-descriptive fields only (oscalReferences). The
    # Metadata_Index record holds only system-bounded facts; the claim's control
    # linkage is projected into the producer-claims control-claims projection
    # ([PCLAIM-001]) and the rest stays in the signed referrer (its system of
    # record), read on demand.
    attestation = detail.get("attestation") or {}
    # The bundle's identity is the opaque orgId + tenantId + projectId triple
    # ([INGEST-005]), sourced from the claim projection — never an
    # AWS account number, never a display name. tenantId is the Metadata_Index
    # partition-key isolation boundary; projectId is the sort-key sub-hierarchy
    # ([META-001]); orgId rides as a plain record attribute.
    org_id, tenant_id, project_id = identity_triple(detail)
    if not (tenant_id and project_id):
        raise ValueError(
            "artifact.validated detail missing the attestation identity triple "
            "(tenantId / projectId)"
        )

    # createdAt is the bundle's FIRST-SEEN time, derived from ecrPushedAt (the
    # AWS-set EventBridge eventTime of the triggering PutImage) — NEVER from a
    # producer/content timestamp. It is stable under re-delivery of the same
    # triggering event (create_or_merge's per-attribute if_not_exists is
    # write-once). The envelope `time` seeds the mutable-guard baseline
    # updatedAt / validatedAt.
    event_time = _event_time(event)
    created_at = detail.get("ecrPushedAt") or event_time
    # Control linkage drives the producer-claims control-claims fan-out ([PCLAIM-001])
    # and the ControlLinkageNone observability signal — it is not a record attribute.
    controls_list = attestation.get("controls") or []
    catalog_or_profile_uuid = attestation.get("catalogOrProfileUuid")
    layers = detail.get("layers") or []
    record: dict[str, Any] = {
        "manifestDigest": detail["manifestDigest"],
        "attestationDigest": detail.get("attestationDigest"),
        "repositoryName": detail.get("repositoryName"),
        "registryId": detail.get("registryId"),
        "region": detail.get("region"),
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "oscalReferences": config_blob.get("oscalReferences"),
        "manifestAnnotations": detail.get("manifestAnnotations") or {},
        # [PIPELINE-009] — authenticated push timestamp, set only on create.
        "ecrPushedAt": detail.get("ecrPushedAt"),
        "temporalConsistencyWarning": detail.get("temporalConsistencyWarning", False),
        # [PIPELINE-010] — full userIdentity block, set only on create.
        "authenticatedPushPrincipal": detail.get("authenticatedPushPrincipal"),
        "layers": layers,
        "artifactResourceReference": detail.get("artifactResourceReference"),
        "signingStatus": "pending",
        "archiveStatus": "pending",
        "validatedAt": event_time,
        # First-seen time, from the AWS-set ecrPushedAt (NOT the producer claim).
        "createdAt": created_at,
        # Seed the mutable-transition guard's baseline. create_or_merge's
        # per-attribute if_not_exists makes this write-once: it lands only when
        # absent (the originating artifact.validated timestamp), and a row whose
        # signing/archive transition already advanced updatedAt keeps its newer
        # value. The guarded upsert path (signed/archived) still ADVANCES
        # updatedAt via its own unconditional SET — that path is untouched.
        "updatedAt": event_time,
    }
    if not controls_list:
        # Auditor-trust signal: surface "N artifacts stored without
        # control linkage" as a reviewable figure so absence from a
        # control-scoped query is explainable, not an invisible gap.
        metrics.add_metric(name="ControlLinkageNone", unit=MetricUnit.Count, value=1)
        LOG.info(
            "metadata record written with no control linkage",
            extra={
                "manifestDigest": detail["manifestDigest"],
                "controlLinkage": "none",
                "projectId": project_id,
            },
        )
    # Row-completeness invariant: validate pre-creates the row
    # with its marker fields (validateStatus/artifactKind/name) off the SAME
    # artifact.validated event. create_or_merge converges the row to the union
    # — our system-bounded payload merges onto a marker-only row instead of
    # being discarded as a no-op, and a true duplicate is a clean no-op. The
    # write-once merge never clobbers a peer's field or a later mutable
    # transition (if_not_exists per attribute), so the initial pending
    # signing/archive status here cannot regress an already-advanced row.
    _index().create_or_merge(
        manifest_digest=detail["manifestDigest"],
        tenant_id=tenant_id,
        project_id=project_id,
        record=record,
    )
    LOG.info(
        "validated payload merged onto Metadata_Index row",
        extra={"manifestDigest": detail["manifestDigest"]},
    )

    # Control-claims projection fan-out ([PCLAIM-001], producer-claims). The
    # projection is a tenant-PK plane ([META-001]): one row per control keyed
    # pk = {tenant-id}, sk = {catalogOrProfileUuid}#{controlId}#{manifestDigest}.
    # The tenant_id partition key comes ONLY from identity_triple(detail) (the
    # AWS-set identity triple resolved above), never from producer-asserted
    # bundle content. The catalog/profile uuid and
    # controls list are sourced from the Producer Attestation referrer
    # projection. Idempotent: re-delivering the same validated event re-writes
    # identical rows. Skipped (zero rows) when the referrer supplies no controls
    # or no catalogOrProfileUuid — record_controls also returns 0 on an empty
    # list.
    if catalog_or_profile_uuid and controls_list:
        # Every attribute on the row traces to a defined source ([PCLAIM-001.04]):
        # projectId/artifactKind from the attestation projection; the three
        # read-state fields signingStatus/archiveStatus/wormStatus are the
        # pipeline-set status snapshot at fan-out time (all pending pre-sign).
        denormalized = {
            "orgId": org_id,
            "tenantId": tenant_id,
            "projectId": project_id,
            "artifactKind": attestation.get("artifactKind"),
            "ecrRepoPath": record.get("repositoryName"),
            "signingStatus": "pending",
            "archiveStatus": "pending",
            "wormStatus": "pending",
            "createdAt": created_at,
        }
        # Drop None-valued attributes so DynamoDB doesn't store nulls.
        denormalized = {k: v for k, v in denormalized.items() if v is not None}
        try:
            written = _controls().record_controls(
                tenant_id=tenant_id,
                manifest_digest=detail["manifestDigest"],
                catalog_or_profile_uuid=catalog_or_profile_uuid,
                controls=controls_list,
                denormalized=denormalized,
            )
            LOG.info(
                "controls relationship rows written",
                extra={
                    "manifestDigest": detail["manifestDigest"],
                    "controlsCount": written,
                    "catalogOrProfileUuid": catalog_or_profile_uuid,
                },
            )
        except Exception:
            LOG.exception(
                "controls fan-out failed (Metadata_Index already committed)",
                extra={"manifestDigest": detail["manifestDigest"]},
            )
            raise


def _handle_signed(event: dict[str, Any], detail: dict[str, Any]) -> None:
    # A submission carries TWO independently-signed referrers: the content
    # manifest and the Producer
    # Attestation referrer. The Metadata_Index is a rebuildable PROJECTION of
    # BOTH signed referrers (metadata-index *Rebuildability*), so we project
    # each signature's own digest: ``signatureDigest`` is the content
    # signature's identity (the top-level key consumers read) and
    # ``attestationSignatureDigest`` is the attestation
    # signature's. Both flow through the None-pruning path below, so a
    # single-signature event (no attestation) simply omits the attestation
    # digest rather than writing a null.
    updates = {
        "signingStatus": "signed",
        "signatureDigest": detail.get("signatureDigest"),
        "attestationSignatureDigest": detail.get("attestationSignatureDigest"),
        "signedAt": detail.get("signedAt"),
        "signingProfileArn": detail.get("signingProfileArn"),
        "timestampProvider": detail.get("timestampProvider"),
    }
    _conditional_update(event, detail, updates)


def _handle_signing_failed(event: dict[str, Any], detail: dict[str, Any]) -> None:
    updates = {
        "signingStatus": "unsigned",
        "signingError": detail.get("signingError"),
    }
    _conditional_update(event, detail, updates)


def _handle_archived(event: dict[str, Any], detail: dict[str, Any]) -> None:
    updates = {
        "archiveStatus": "archived",
        "s3Bucket": detail.get("s3Bucket"),
        "s3KeyPrefix": detail.get("s3KeyPrefix"),
        "wormReceiptDigest": detail.get("wormReceiptDigest"),
        "wormEnabled": detail.get("wormEnabled"),
        "archivedAt": detail.get("archivedAt"),
    }
    _conditional_update(event, detail, updates)


def _handle_archive_failed(event: dict[str, Any], detail: dict[str, Any]) -> None:
    updates = {
        "archiveStatus": "failed",
        "archiveError": detail.get("artifact") or detail.get("computedDigest"),
        "archiveFailureDetail": {
            "computedDigest": detail.get("computedDigest"),
            "expectedDigest": detail.get("expectedDigest"),
            "artifact": detail.get("artifact"),
        },
    }
    _conditional_update(event, detail, updates)


def _handle_validation_failed(event: dict[str, Any], detail: dict[str, Any]) -> None:
    """No DDB write — the bundle never got a Metadata_Index entry by design.

    The CloudWatch alarm fires on the metric we emit here + the DLQ.
    """
    LOG.warning(
        "validation-failed observed; no metadata record by design",
        extra={
            "manifestDigest": detail.get("manifestDigest"),
            "errorKind": detail.get("errorKind"),
            "failedCriteria": detail.get("failedCriteria"),
        },
    )


def _conditional_update(
    event: dict[str, Any],
    detail: dict[str, Any],
    updates: dict[str, Any],
) -> None:
    # The identity triple is sourced from the claim projection; the
    # tenantId partition key and projectId sort-key sub-hierarchy address the row
    # ([META-001]). Every post-validated event that mutates a row threads the
    # claim, so the triple is present here too.
    org_id, tenant_id, project_id = identity_triple(detail)
    del org_id  # not a row-key component on the conditional-update path
    manifest_digest = detail.get("manifestDigest")
    if not (tenant_id and project_id and manifest_digest):
        raise ValueError("event detail missing tenantId / projectId / manifestDigest for update")
    event_time = _event_time(event)
    if not event_time:
        raise ValueError("event envelope missing 'time' (event timestamp guard requires it)")
    # Drop None-valued keys so we don't overwrite real data with nulls on
    # a sparse event.
    pruned = {k: v for k, v in updates.items() if v is not None}
    try:
        # Upsert (not update): post-validated events may legitimately
        # land before artifact.validated under at-least-once + out-of-
        # order EventBridge delivery. touch creates a stub record so
        # out-of-order events are not lost.
        _index().upsert_on_event(
            manifest_digest=manifest_digest,
            tenant_id=tenant_id,
            project_id=project_id,
            updates=pruned,
            event_timestamp=event_time,
        )
    except ConditionalUpdateFailed:
        # The existing record's updatedAt is at least as new as this
        # event — a stale out-of-order retry. Log and drop ([PIPELINE-007.01],
        # conditional-write facet [META-007.02]).
        LOG.info(
            "conditional upsert skipped (stale event)",
            extra={"manifestDigest": manifest_digest, "updates": list(pruned)},
        )


# detail-type → handler lookup keeps the dispatch readable + testable.
_DISPATCH: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
    "artifact.validated": _handle_validated,
    "artifact.signed": _handle_signed,
    "artifact.signing-failed": _handle_signing_failed,
    "artifact.archived": _handle_archived,
    "artifact.archive-failed": _handle_archive_failed,
    "artifact.validation-failed": _handle_validation_failed,
}


def _handle(event: dict[str, Any]) -> None:
    detail_type = event.get("detail-type")
    if detail_type not in _DISPATCH:
        raise ValueError(
            f"unhandled detail-type {detail_type!r}; expected one of {sorted(_DISPATCH)}"
        )
    detail = event.get("detail") or {}
    if not isinstance(detail, dict):
        raise ValueError("event.detail missing or not a dict")
    _DISPATCH[detail_type](event, detail)


@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", None)
    detail_type = str(event.get("detail-type", "unknown"))
    start = time.perf_counter()
    with observability_scope(
        event,
        request_id=request_id,
        event_type="metadata-update",
    ):
        # Per-invocation dimension (DetailType varies per event); set after
        # observability_scope so default dimensions (Stage) stay clean.
        metrics.add_dimension(name="DetailType", value=detail_type)
        # Per-bundle TenantId dimension under the uniform tenancy model ([OBS]
        # tenant-id dimension). Absent on events without a claim projection
        # (e.g. artifact.validation-failed), so set it only when known.
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        _, tenant_id, _ = identity_triple(detail)
        if tenant_id:
            metrics.add_dimension(name="TenantId", value=tenant_id)
        try:
            _handle(event)
            metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
            return {"detailType": detail_type, "status": "applied"}
        except Exception:
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=elapsed_ms)
