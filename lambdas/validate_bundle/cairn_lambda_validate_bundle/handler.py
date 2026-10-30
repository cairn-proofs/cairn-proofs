"""validate-bundle Lambda entry point.

Pipeline stage 1. Triggered by EventBridge for ECR ``PutImage`` events
on repos matching ``artifact-{regionCode}-{stage}/*``. The trigger is INVERTED:
the single positive validation trigger is the push of an
**Producer_Attestation_Referrer** (a manifest whose ``artifactType`` is
``application/vnd.cairnproofs.producer-attestation.v1+json`` over a content-manifest
``subject``). EVERY other push — the content manifest's own push, Notation
signatures, OCI empty/referrer configs, image indexes, anything unrecognized —
is skipped with a benign ``ValidationSkipped`` disposition. Responsibilities on
the trigger:

1. Read the Producer_Attestation_Referrer manifest; parse its claim layer as an in-toto
   Statement and unwrap the producer claim via
   :func:`cairn_oci.read_claim_from_statement` (asserting the Cairn
   ``predicateType`` and a single-element ``subject``); read its envelope
   ``subject`` = the content manifest digest, and verify the Statement's own
   subject agrees with it.
2. Subject-scope binding: confirm the content manifest resides in
   the SAME IAM-enforced ECR repository as the referrer BEFORE any
   resolve/promote. A foreign-scope subject is a permanent validation failure
   routed to the DLQ.
3. Fetch the content manifest + Config_Blob via the subject digest.
4. Run [INGEST-017.07] content-structure validation (``validate_bundle``) and the
   claim-derived checks (``validate_claim``) over the (content manifest + claim)
   pair.
5. Run the [PIPELINE-013] cross-check — the producer-asserted org / tenant /
   project triple, sourced from the Producer_Attestation_Referrer claim, each
   exact-matched against the corresponding {org-id}/{tenant-id}/{project-id}
   segment of the IAM-enforced ECR repository path. Disagreement on any
   coordinate is a permanent failure.
6. Per-OSCAL-layer temporal consistency: reject if any layer's
   ``metadata.last-modified`` is strictly after ``ecrPushedAt`` ([PIPELINE-009]); set
   ``temporalConsistencyWarning`` when drift exceeds the staleness window. There
   is no synthetic ``created`` annotation.
7. For every ``application/oscal.*`` layer, parse, derive + gate the OSCAL
   schema version from the layer's own ``metadata.oscal-version``, and extract
   metadata.
8. Emit ``artifact.validated`` on success or ``artifact.validation-failed`` on
   permanent failure; permanent failures deliver an in-band rejection record
   directly to the validate DLQ ([PIPELINE-006] — no async-invoke retry) and the handler
   returns the ``_REJECTED`` sentinel so Lambda does not re-invoke.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cairn_metadata import MetadataIndex
from cairn_observability import (
    Metrics,
    MetricUnit,
    ObservabilityKeys,
    configure_root_logger,
    metric_namespace,
    observability_scope,
    set_stage_default_dimension,
)
from cairn_observability.keys import EVENT_TYPES_FAILURE, EVENT_TYPES_SUCCESS, STATUSES
from cairn_oci import (
    CONFIG_BLOB_MEDIA_TYPE,
    LAYER_REQUIRED_TITLE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    InvalidStatementError,
    OciManifest,
    classify_pipeline_internal,
    read_claim_from_statement,
    subject_digest_matches,
)
from cairn_oci.categories import (
    InvalidArtifactKindError,
    normalize_artifact_kind,
)
from cairn_oscal import (
    OscalExtractionError,
    extract_oscal_metadata,
    make_artifact_reference,
    parse_oscal_layer,
)
from cairn_validation import (
    crosscheck_identity,
    extract_authenticated_push_principal,
    validate_bundle,
    validate_claim,
)
from cairn_validation.principal import MissingUserIdentityError
from opentelemetry import baggage, context, trace
from pydantic import ValidationError

from cairn_lambda_validate_bundle import dlq
from cairn_lambda_validate_bundle.ecr_client import (
    EcrFetchError,
    fetch_blob,
    fetch_manifest,
    image_resides_in_repo,
)
from cairn_lambda_validate_bundle.ecr_event import (
    EcrPushEvent,
    MalformedEcrEvent,
    ecr_image_uri,
    parse_ecr_push_event,
)
from cairn_lambda_validate_bundle.events import emit_validated, emit_validation_failed

LOG = logging.getLogger(__name__)
configure_root_logger()

HANDLER_NAME = "validate-bundle"
STAGE = os.environ.get("STAGE") or os.environ.get("CAIRN_STAGE", "")
REGION_CODE = os.environ.get("REGION_CODE") or os.environ.get("CAIRN_REGION_CODE", "")
ARCHIVE_BUCKET = os.environ.get("ARCHIVE_BUCKET", "")
STALENESS_WINDOW_DAYS = int(os.environ.get("STALENESS_WINDOW_DAYS", "30"))

# Per-invocation monotonic start anchor for the failure-log durationMs field
# (OBS-008). lambda_handler sets it at entry; _emit_failure_event reads it to
# compute the validate-stage wall-clock. A ContextVar (never a module global)
# keeps the value per-invocation and parallel-test-safe — it is cleared to None
# for any invocation that does not set it, so a direct unit call to
# _emit_failure_event omits durationMs rather than reading a stale value.
_INVOCATION_START: ContextVar[float | None] = ContextVar("_INVOCATION_START", default=None)

# The fixed failure-path eventType / status for this stage's permanent-failure
# log record (OBS-008). Membership in the closed vocabularies is required at
# import, so the emitted values stay bound to cairn_observability.keys rather
# than drifting as hand-typed literals.
_FAILURE_EVENT_TYPE: Final[str] = "validation-failed"
_FAILURE_STATUS: Final[str] = "failure"
_SUCCESS_EVENT_TYPE: Final[str] = "validated"
_SUCCESS_STATUS: Final[str] = "success"
if _FAILURE_EVENT_TYPE not in EVENT_TYPES_FAILURE:
    raise ValueError(f"{_FAILURE_EVENT_TYPE!r} is not a member of EVENT_TYPES_FAILURE")
if _SUCCESS_EVENT_TYPE not in EVENT_TYPES_SUCCESS:
    raise ValueError(f"{_SUCCESS_EVENT_TYPE!r} is not a member of EVENT_TYPES_SUCCESS")
if _FAILURE_STATUS not in STATUSES:
    raise ValueError(f"{_FAILURE_STATUS!r} is not a member of STATUSES")
if _SUCCESS_STATUS not in STATUSES:
    raise ValueError(f"{_SUCCESS_STATUS!r} is not a member of STATUSES")

# Code-point cap on the producer-asserted projectId when it lands on a per-stage
# LOG line (OBS-008 projectId parity), applied in the shared _emit_stage_log
# helper. On the identity-cross-check FAILURE path the value is logged BEFORE the
# cross-check has confirmed it matches the IAM-enforced ECR path segment, so it
# is still raw, unvalidated producer input (the claim model bounds neither its
# length nor charset); an oversized / high-cardinality projectId would otherwise
# amplify CloudWatch log volume and cost. StructuredJsonFormatter's json.dumps
# already escapes control/newline characters (no record forging), so a code-point
# truncation is the needed defense; it mirrors MAX_CRITERION_STR_LEN /
# MAX_LAYER_TITLE_LEN. On the SUCCESS path (and the post-cross-check failure
# paths) the value is already bound to an ECR path segment, so the cap is a no-op.
MAX_PROJECT_ID_LOG_LEN = 256

# Bound the artifact.validation-failed event payload against EventBridge's
# 256 KB per-entry limit. Producer-controlled manifests can drive
# failedCriteria to tens of thousands of entries (~1.8 MB); cap the count and
# the per-string length so the mandated failure notification always fits. The
# cap applies ONLY to the EventBridge detail — the log line keeps the full,
# unbounded list (the forensic record the failed-bundle surfacing relies on).
MAX_FAILED_CRITERIA = 50
MAX_CRITERION_STR_LEN = 1024

# Per-title BYTE budget bounding each layer title's contribution to the downstream
# Metadata_Index DDB item. The 400 KB DDB item limit is measured in BYTES, and
# org.opencontainers.image.title is producer free text that may be non-ASCII (up to
# 4 UTF-8 bytes per code-point), so the cap is enforced against the UTF-8 byte length
# (not the code-point count, which would undercount a multibyte title ~4x). Fail closed
# (explainable validation error) rather than silently truncating, keeping the stored value
# a faithful projection (metadata-index "Per-layer descriptor projection" spec).
# This is defense-in-depth at the per-title granularity: the aggregate layer-count
# bound is the 1..300 spec Property (artifact-ingestion [INGEST-009], [INGEST-010]), now enforced in
# THIS validate path via validate_bundle (criteria i1/i2, routed to the DLQ through the
# existing failed_criteria chokepoint). The per-title byte cap (plus the fail-closed
# downstream DDB write if any aggregate ceiling is ever exceeded) is what additionally
# keeps a single title from pushing the item past the byte limit.
MAX_LAYER_TITLE_LEN = int(os.environ.get("MAX_LAYER_TITLE_LEN", "1024"))

# Hard byte ceiling for the serialized EventBridge detail. EventBridge's
# per-entry limit is 256 KB; default to 200 KB for margin against JSON
# escaping (ensure_ascii expands non-ASCII to \uXXXX / surrogate pairs).
# The count/length caps above bound code-points, but json.dumps' default
# ensure_ascii=True can still inflate attacker-controlled non-ASCII content
# past the byte limit after bounding, so this is the byte-true backstop.
# Env-overridable so the ceiling can be raised per-deployment if needed.
MAX_EVENT_DETAIL_BYTES = int(os.environ.get("MAX_EVENT_DETAIL_BYTES", str(200 * 1024)))

if STAGE:
    os.environ.setdefault("CAIRN_STAGE", STAGE)
if REGION_CODE:
    os.environ.setdefault("CAIRN_REGION_CODE", REGION_CODE)

_OSCAL_MEDIA_PREFIX = "application/oscal."

# Metadata_Index table that holds the durable digest-keyed record. Validate
# reads it to short-circuit a re-seen digest (the re-entrant PutImage the
# canonical-tag-minting stage emits) and writes the canonical-tag coordinates
# (``artifactKind``) onto it for the mint stage to consume.
METADATA_TABLE_NAME = os.environ.get("METADATA_TABLE_NAME", "")

# Durable-record marker: validate stamps ``validateStatus=validated`` on the
# digest-keyed Metadata_Index row once it has recorded coordinates for a digest.
# The re-tag short-circuit keys its prior-completion check on this marker, so it
# fires only for a digest validate ITSELF completed — not for any incidental row
# a later stage may have upserted first.
_REC_VALIDATE_STATUS_KEY = "validateStatus"
_VALIDATE_STATUS_DONE = "validated"

# Terminal-success gate for the re-seen short-circuit. The re-seen skip must
# fire ONLY for a true duplicate of an already-completed bundle, never for a
# bundle parked non-terminal (a retry must be allowed to progress it). These
# mirror the canonical mutable-status strings update-metadata writes onto the
# same digest-keyed row (update_metadata handler signed/archived transitions);
# the validate stage only READS them here.
_REC_SIGNING_STATUS_KEY = "signingStatus"
_SIGNING_STATUS_SIGNED = "signed"
_REC_ARCHIVE_STATUS_KEY = "archiveStatus"
_ARCHIVE_STATUS_ARCHIVED = "archived"

# Pair-wise dedup discriminator. The set of Producer_Attestation_Referrer digests validate
# has already accepted for this CONTENT subject, accumulated on the durable row
# via an idempotent DynamoDB String-Set ADD (see
# MetadataIndex.add_validated_attestation). Membership makes the dedup unit the
# (content Manifest_Digest, Producer_Attestation_Referrer digest) pair: a re-pushed same
# attestation skips, a genuinely NEW attestation over the same content processes
# ("one content, many attestations" — artifact-ingestion).
_REC_VALIDATED_ATTESTATIONS_KEY = "validatedAttestationDigests"

# SkipReason dimension value for the re-tag short-circuit. Kept handler-local
# (not in the cairn_oci SkipReason enum, whose members classify manifest-shape
# skips from the manifest dict) because this skip is discriminated by the
# durable digest record, not by manifest media types. The metric series is
# shared with the manifest-shape skips.
_SKIP_REASON_RE_SEEN = "re-seen-digest"

# SkipReason dimension values for the inverted-trigger skips. The
# single positive trigger is a Producer_Attestation_Referrer push; everything else is a
# benign skip. ``content_manifest`` covers a content manifest pushed on its own
# (an incomplete submission — never promoted until its attestation arrives);
# ``not_attestation`` covers any manifest that is neither a positively-identified
# Producer_Attestation_Referrer nor a recognized pipeline-internal shape. The
# signature/empty-config/image-index shapes keep their cairn_oci SkipReason
# values via classify_pipeline_internal.
_SKIP_REASON_CONTENT_MANIFEST = "content_manifest"
_SKIP_REASON_NOT_ATTESTATION = "not_attestation"
_SKIP_REASON_ATTESTATION_NO_SUBJECT = "attestation_missing_subject"


def _supplied_resolved_kind(supplied: str | None) -> dict[str, str]:
    """Producer-supplied artifact-kind token and its resolved canonical.

    Aliases silently remap producer intent (``policies`` → ``governance-doc``,
    ``scan-reports`` → ``tool-output``, …); recording ONLY the resolved canonical
    loses what the producer literally sent. This surfaces both on the success-path
    structured log / payload AND on the failure event so the audit trail can answer
    "what did the producer actually send".

    The producer-supplied
    artifact-kind is sourced from the Producer_Attestation_Referrer ``artifactKind`` claim
    (``claim.artifact_kind``) — never a content-manifest annotation or a
    Config_Blob field. Fail-soft by design: an absent/empty value or an
    unresolvable token is ALREADY handled by the criterion="d" vocabulary check
    (``validate_claim``) that runs over the same claim, so we never raise — we
    return only the keys we can populate (``suppliedArtifactKind`` when the token
    is present, ``resolvedArtifactKind`` when it additionally resolves). An empty
    dict means the claim carried no artifact-kind at all.
    """
    if not supplied:
        return {}
    out: dict[str, str] = {"suppliedArtifactKind": supplied}
    try:
        out["resolvedArtifactKind"] = normalize_artifact_kind(supplied)
    except InvalidArtifactKindError:
        # The claim vocabulary check (validate_claim criterion="d") gates this
        # same token through the same resolver, so an unresolvable value is
        # already a criterion="d" failure; omit the resolved key rather than
        # crash the already-failing path.
        pass
    return out


# CloudWatch Metrics via EMF (Powertools). The namespace is composed by the
# shared cairn_observability.metric_namespace helper ([OBS-009]:
# ``CairnProofs/{regionCode}-{stage}``) so the namespace token lives in exactly
# one place. Stage is set as a default dimension so it attaches to every metric
# without per-emission boilerplate; the per-bundle TenantId dimension is added
# inside _handle once the claim's tenant coordinate is cross-check-verified.
metrics = Metrics(
    namespace=metric_namespace(REGION_CODE, STAGE),
    service=HANDLER_NAME,
)
set_stage_default_dimension(metrics, STAGE)


def _metadata_index() -> MetadataIndex | None:
    """Facade over the durable digest-keyed Metadata_Index row, or ``None``.

    Returns ``None`` when ``METADATA_TABLE_NAME`` is unset (the table is not
    wired into this deployment), so the re-tag short-circuit and coordinate
    recording degrade to no-ops rather than erroring. Monkeypatched in tests to
    inject an in-memory fake.
    """
    if not METADATA_TABLE_NAME:
        return None
    return MetadataIndex(METADATA_TABLE_NAME)


def _re_seen_terminal_duplicate(
    *, content_digest: str, tenant_id: str, project_id: str, attestation_referrer_digest: str
) -> bool:
    """True iff this push is a true duplicate of an already-COMPLETED bundle.

    The dedup unit is the **(content Manifest_Digest, Producer_Attestation_Referrer
    digest) pair, gated on terminal-success state**. Return ``True`` (skip as
    re-seen) if and ONLY if ALL hold on the durable digest-keyed row:

    1. the row exists AND carries validate's ``validateStatus=validated`` marker,
    2. the row is terminal-success — ``signingStatus=signed`` AND
       ``archiveStatus=archived`` (the mutable transitions update-metadata writes
       once the bundle has been signed and archived), AND
    3. the triggering ``attestation_referrer_digest`` is already a member of the
       row's ``validatedAttestationDigests`` String Set.

    Return ``False`` otherwise — no index, missing row, non-terminal status, OR a
    referrer digest not yet recorded — so the bundle is PROCESSED. Gating on
    terminal-success state (not ``validateStatus`` alone) ensures a
    non-terminal-retry re-push and a genuinely new independently-signed
    attestation over the same content are both PROCESSED: (a) a
    re-push to retry a bundle parked non-terminal after a transient downstream
    failure and (b) a genuinely NEW, independently-signed
    Producer_Attestation_Referrer over the same content subject (the
    artifact-ingestion "one content, many attestations" Requirement).

    Keying on the CONTENT digest (NOT the tag, NOT the attestation digest) is the
    load-bearing addressing control: the content digest is
    stable across the untagged→tagged transition, so the mint stage's
    re-tag ``PutImage`` resolves to the same row. Critically, that re-tag re-tags
    the **content** manifest, which the ``artifactType`` classifier catches BEFORE
    this predicate is ever reached — so the mint loop never depends on this skip,
    and tightening the predicate is safe.

    The row is addressed by ``(content_digest, tenant_id, project_id)`` — the
    tenant-PK Metadata_Index key schema ([META-001]); ``tenant_id``
    keys the partition and ``{project_id}#{content_digest}`` keys the sort. Both
    coordinates come from the producer's claim (the cross-checked identity
    triple) and must be known before this check runs, which is why the
    short-circuit lives after the cheap claim parse (the coordinate source) but
    before any signing/archive side effect.
    """
    index = _metadata_index()
    if index is None:
        return False
    record = index.get_record(
        manifest_digest=content_digest,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if record is None or record.get(_REC_VALIDATE_STATUS_KEY) != _VALIDATE_STATUS_DONE:
        return False
    if record.get(_REC_SIGNING_STATUS_KEY) != _SIGNING_STATUS_SIGNED:
        return False
    if record.get(_REC_ARCHIVE_STATUS_KEY) != _ARCHIVE_STATUS_ARCHIVED:
        return False
    seen = record.get(_REC_VALIDATED_ATTESTATIONS_KEY) or ()
    return attestation_referrer_digest in seen


def _record_coordinates(
    *,
    image_digest: str,
    tenant_id: str,
    project_id: str,
    artifact_kind: str | None,
    attestation_referrer_digest: str,
) -> None:
    """Persist the canonical-tag coordinates, keyed on the manifest digest.

    This durable row is the source of truth the downstream canonical-tag-minting
    stage reads — mint never re-derives identity from the event, and
    builds the single canonical form ``{artifact-kind}--_ref--{digest}`` from the
    recorded ``artifactKind`` plus the Manifest_Digest prefix (which mint already
    holds as its addressing anchor). The canonical tag carries no producer free
    text, so NO ``name`` is recorded here — human-friendly naming moves to the
    mutable ``_als`` alias form. The row
    records ``artifactKind`` + ``validateStatus`` (plus the
    attestation-referrer-digest accumulation below) only.

    The record is a flat ``dict`` written through the real ``MetadataIndex``
    facade, addressed by ``(manifest_digest, tenant_id, project_id)`` under the
    tenant-PK key schema ([META-001]) — ``tenant_id`` keys the
    partition, ``{project_id}#{manifest_digest}`` the sort. The write
    stamps ``validateStatus=validated``, one of the gates
    :func:`_re_seen_terminal_duplicate` keys on, making validate idempotent
    against its own re-tag re-trigger.

    Validate writes its marker fields via ``create_or_merge``:
    a row this stage's create-collides on — pre-created by update-metadata off
    the same ``artifact.validated`` event, or by a post-validated event
    (signing-failed, archived, …) under at-least-once / out-of-order delivery —
    has its still-absent marker attributes merged on via per-attribute
    ``if_not_exists``. The row converges to the union of all stages'
    complementary write-once fields regardless of create order, a re-pushed
    already-advanced digest is a clean no-op (no ``ConditionalUpdateFailed``
    escapes, no field regresses), and a peer's value is never clobbered.

    Also accumulates the triggering ``attestation_referrer_digest`` into the
    row's ``validatedAttestationDigests`` String Set via the idempotent
    ``add_validated_attestation`` facade — the set
    :func:`_re_seen_terminal_duplicate` reads back to make the dedup unit a
    (content digest, referrer digest) pair. This write is deliberately NOT on the
    write-once ``create_or_merge`` path (membership accumulates over distinct
    referrers); it shares the marker write's ``index is None`` early return.

    No-op when no Metadata_Index is configured (``METADATA_TABLE_NAME`` unset).
    """
    index = _metadata_index()
    if index is None:
        return
    coordinates: dict[str, Any] = {
        "artifactKind": artifact_kind,
        _REC_VALIDATE_STATUS_KEY: _VALIDATE_STATUS_DONE,
    }
    # Row-completeness invariant: validate and update-metadata
    # both write this digest-keyed row off the same artifact.validated event
    # with complementary write-once field sets. create_or_merge converges the
    # row to the UNION regardless of create order — our marker fields merge onto
    # a row a peer already created, and a re-pushed already-advanced digest is a
    # clean no-op (if_not_exists leaves the existing marker untouched). No
    # ConditionalUpdateFailed escapes, no field regresses. The mutable
    # signing/archive transitions stay on the unchanged timestamp-guarded path
    # (this write-once path never touches them).
    #
    # createdAt is NOT contributed here: it is single-owned by update-metadata
    # (the artifact.validated envelope time), keeping this stage's contribution
    # disjoint from the rich-payload writer's. A validate-first create yields a
    # marker-only row; update-metadata's merge later fills createdAt + updatedAt
    # via if_not_exists. Single-owner write-once
    # attributes preserve the disjointness invariant.
    index.create_or_merge(
        manifest_digest=image_digest,
        tenant_id=tenant_id,
        project_id=project_id,
        record=coordinates,
    )
    # Accumulate the triggering referrer digest into the content row's
    # validatedAttestationDigests set (idempotent ADD, keyed on the CONTENT digest
    # like the marker write above); see the docstring for the dedup rationale.
    index.add_validated_attestation(
        manifest_digest=image_digest,
        tenant_id=tenant_id,
        project_id=project_id,
        attestation_digest=attestation_referrer_digest,
    )


def _criterion_dict(error: Any) -> dict[str, Any]:
    """Serialize a ``BundleValidationError`` to the failedCriteria entry shape."""
    return {"criterion": error.criterion, "field": error.field, "message": error.message}


def _schema_failed_criteria(
    error: ValidationError, *, criterion: str, field_prefix: str = "", default_field: str
) -> list[dict[str, Any]]:
    """Map a pydantic ``ValidationError`` to the failedCriteria entry shape.

    Each pydantic sub-error becomes one entry; the dotted ``loc`` path is the
    field (falling back to ``default_field`` for a top-level error), optionally
    prefixed with ``field_prefix`` (e.g. ``"attestation."`` for the claim body).
    Shared by the attestation-manifest, attestation-claim, and content-manifest
    schema-failure branches so the three stay identical.
    """
    return [
        {
            "criterion": criterion,
            "field": field_prefix + (".".join(str(p) for p in err["loc"]) or default_field),
            "message": err["msg"],
        }
        for err in error.errors()
    ]


class LayerProjectionError(Exception):
    """Raised by ``_process_layers_and_build_reference`` for a per-layer projection
    failure that must fail the bundle closed.

    Currently the sole trigger is an ``org.opencontainers.image.title`` annotation
    that exceeds ``MAX_LAYER_TITLE_LEN``: projecting it verbatim could push the
    Metadata_Index item past DynamoDB's 400 KB limit, so the bundle is a permanent
    validation failure rather than a silently-truncated projection. ``_handle``
    catches it alongside ``OscalExtractionError`` and routes it through the
    ``_emit_failure_event`` chokepoint. Carries the offending layer's ``digest`` so
    the failure event can name the field precisely.
    """

    def __init__(self, message: str, *, layer_digest: str | None = None) -> None:
        super().__init__(message)
        self.layer_digest = layer_digest


class PermanentValidationFailure(Exception):
    """Raised by ``_handle`` after emitting artifact.validation-failed.

    Caught by ``lambda_handler``, which delivers the rejection in-band to the
    validate DLQ and returns the ``_REJECTED`` sentinel (a success, non-retryable
    return) — NOT re-raised. [PIPELINE-006] mandates permanent failures route directly
    to the DLQ without further retries, so we must not rely on Lambda's
    async-invoke retry-then-dead-letter path.
    """


# Sentinel returned by ``_handle`` when the pushed artifact was classified as
# pipeline-internal and skipped. It is distinct from both the
# happy-path payload (an ``artifact.validated`` dict) and the failure path
# (a raise), so ``lambda_handler`` can withhold the Success/Failure metric for
# a skip — a skip is neither success nor failure.
_SKIPPED: Final[dict[str, Any]] = {"_skipped": True}

# Sentinel returned by ``lambda_handler`` when a bundle was classified as a
# permanent failure ([PIPELINE-006]). The handler emits the artifact.validation-failed
# event, sends the in-band DLQ rejection, records the Failure(Reason=Permanent)
# metric, then returns THIS rather than raising — a successful, non-retryable
# completion so Lambda's async-invoke machinery does not re-run the handler
# against a structurally-invalid bundle. Distinct from both ``_SKIPPED`` and the
# happy-path payload so ``lambda_handler`` can withhold the Success metric.
_REJECTED: Final[dict[str, Any]] = {"_rejected": True}


def _bound_failed_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound a failed-criteria list for the EventBridge detail.

    Keeps at most ``MAX_FAILED_CRITERIA`` entries, truncates each entry's
    string values to ``MAX_CRITERION_STR_LEN``, and appends a single
    truncation marker (reusing the {criterion, field, message} shape) when
    any entries are dropped. No marker is appended when nothing is dropped.
    """
    bounded = [
        {k: (v[:MAX_CRITERION_STR_LEN] if isinstance(v, str) else v) for k, v in c.items()}
        for c in criteria[:MAX_FAILED_CRITERIA]
    ]
    omitted = len(criteria) - len(bounded)
    if omitted > 0:
        bounded.append(
            {
                "criterion": "truncated",
                "field": "failedCriteria",
                "message": (
                    f"{omitted} additional criteria omitted ({len(criteria)} total); "
                    "payload bounded to the EventBridge 256 KB per-entry limit"
                ),
            }
        )
    return bounded


def _enforce_detail_byte_ceiling(detail: dict[str, Any], *, total_criteria: int) -> dict[str, Any]:
    """Guarantee the serialized detail fits EventBridge's per-entry limit.

    ``_bound_failed_criteria`` caps criteria COUNT and per-string code-point
    length, but ``emit()`` serializes via ``json.dumps(detail, default=str)``
    with the default ``ensure_ascii=True``, which escapes non-ASCII to
    ``\\uXXXX`` / surrogate pairs — so a crafted non-ASCII payload can still
    exceed the byte limit after bounding. Measure the true serialized size the
    SAME way ``emit()`` does; when it exceeds ``MAX_EVENT_DETAIL_BYTES``,
    collapse ``failedCriteria`` (the only attacker-amplifiable field) to a
    single short ASCII summary marker so the [PIPELINE-006]-mandated failure event
    always fits and delivers. Mutates and returns ``detail``.
    """
    if len(json.dumps(detail, default=str).encode("utf-8")) <= MAX_EVENT_DETAIL_BYTES:
        return detail
    detail["failedCriteria"] = [
        {
            "criterion": "truncated",
            "field": "failedCriteria",
            "message": (
                f"all {total_criteria} criteria omitted; bounded detail still "
                f"exceeded the {MAX_EVENT_DETAIL_BYTES}-byte EventBridge ceiling "
                "(non-ASCII JSON escaping)"
            ),
        }
    ]
    return detail


def _emit_stage_log(
    message: str,
    *,
    level: int,
    event_type: str,
    status: str,
    project_id: str | None,
    extra: dict[str, Any],
) -> None:
    """Emit one OBS-008-compliant per-stage structured log record.

    The single place that attaches the four OBS-008 per-stage fields for BOTH
    validate-bundle outcomes (the failure chokepoint and the success emission),
    so the baggage-vs-``extra`` routing lives once:

    - ``eventType`` (``event_type``) rides OTEL **baggage**, not ``extra``:
      ``StructuredJsonFormatter`` populates the payload from baggage BEFORE the
      per-record ``extra`` loop and skips any key already present (``log.py``
      103-111), so the scope's entry-verb ``eventType="validate"`` would shadow
      an ``extra`` value on the rendered CloudWatch line. We override the baggage
      entry itself. The attach mutates the active (scope-live) context;
      ``observability_scope`` detaches its own token on exit and the invocation is
      terminal by the time a per-stage line is emitted, so the override never
      leaks across invocations.
    - ``status`` rides ``extra`` (not in baggage on any path).
    - ``durationMs`` rides ``extra``, derived from the per-invocation
      ``_INVOCATION_START`` anchor set in ``lambda_handler`` — OMITTED when unset
      (a direct unit call outside ``lambda_handler``).
    - ``projectId`` rides ``extra`` when supplied, truncated to
      ``MAX_PROJECT_ID_LOG_LEN`` (on the failure cross-check path it is still raw,
      unvalidated producer input).

    ``extra`` is the caller's path-specific fields (failure: ``failedCriteria`` /
    ``errorKind`` / …; success: ``attestationDigest`` / …); the four fields are
    merged onto a copy so the caller's dict is untouched.
    """
    log_extra: dict[str, Any] = {**extra, ObservabilityKeys.STATUS: status}
    invocation_start = _INVOCATION_START.get()
    if invocation_start is not None:
        log_extra[ObservabilityKeys.DURATION_MS] = int(
            (time.perf_counter() - invocation_start) * 1000
        )
    if project_id is not None:
        log_extra[ObservabilityKeys.PROJECT_ID] = project_id[:MAX_PROJECT_ID_LOG_LEN]
    context.attach(baggage.set_baggage(ObservabilityKeys.EVENT_TYPE, event_type))
    LOG.log(level, message, extra=log_extra)


def _emit_failure_event(
    *,
    manifest_digest: str | None,
    failed_criteria: list[dict[str, Any]],
    repository_name: str | None,
    layer_digest: str | None = None,
    error_kind: str | None = None,
    supplied_kind: str | None = None,
    project_id: str | None = None,
) -> None:
    # Surface the producer-SUPPLIED artifact-kind alongside its resolved
    # canonical so the audit trail records what the producer literally sent, even on
    # the failure path. ``supplied_kind`` is the producer-declared artifact-kind
    # (the Producer_Attestation_Referrer ``artifactKind`` claim) when it is available —
    # ``None`` on early failures (malformed event/manifest/claim) where no kind has
    # been parsed yet. Short scalars, so they cannot blow the EventBridge ceiling.
    kind = _supplied_resolved_kind(supplied_kind)
    detail: dict[str, Any] = {
        "manifestDigest": manifest_digest,
        "repositoryName": repository_name,
        "failedCriteria": _bound_failed_criteria(failed_criteria),
        **kind,
    }
    if layer_digest:
        detail["layerDigest"] = layer_digest
    if error_kind:
        detail["errorKind"] = error_kind
    # Single chokepoint for every permanent-failure path: log the full
    # failing-criteria detail to CloudWatch so the failure is diagnosable from
    # logs alone, without reconstructing the EventBridge event. The full,
    # unbounded failedCriteria list goes to the LOG record (the bounded copy
    # above is only for the EventBridge detail). _emit_stage_log attaches the
    # four OBS-008 per-stage fields (eventType via baggage, status/durationMs/
    # projectId via extra) — see its docstring. projectId is emitted only when a
    # post-claim call site supplied it (claim identity triple parsed).
    _emit_stage_log(
        "validate-bundle validation failed",
        level=logging.WARNING,
        event_type=_FAILURE_EVENT_TYPE,
        status=_FAILURE_STATUS,
        project_id=project_id,
        extra={
            "failedCriteria": failed_criteria,
            "errorKind": error_kind,
            "manifestDigest": manifest_digest,
            "repositoryName": repository_name,
            **kind,
        },
    )
    # Byte-true backstop (H1): _bound_failed_criteria caps code-point COUNT and
    # length, but json.dumps' ensure_ascii expansion of attacker-controlled
    # non-ASCII can still exceed EventBridge's per-entry limit. Collapse
    # failedCriteria to a single marker if the fully-built detail is oversized,
    # so the mandated failure event always delivers. Applied only here, on the
    # failure path — the artifact.validated happy path's payload is load-bearing
    # and is handled by raising on FailedEntryCount>0, not by truncation.
    detail = _enforce_detail_byte_ceiling(detail, total_criteria=len(failed_criteria))
    # [PIPELINE-006]: deliver the rejection DIRECTLY to the validate DLQ in-band, so a
    # permanent failure reaches the queue at classification time (with the
    # failure reason in the body) instead of after Lambda's async-invoke retries
    # are exhausted. This is the single chokepoint, so every permanent-failure
    # path deposits exactly one reason-bearing DLQ message. The same byte ceiling
    # enforced above (EventBridge's 256 KB) also keeps the SQS body in range.
    #
    # Order matters: write the DURABLE operator store (DLQ) BEFORE emitting the
    # EventBridge event. If the SQS send raises (throttle/transient), the
    # exception propagates to lambda_handler's transient branch and the whole
    # invocation is async-retried — and because the event has NOT been emitted
    # yet, the retry emits it exactly once, preserving the "exactly one
    # artifact.validation-failed event" invariant. (The reverse order would
    # re-emit the event on every send retry.) emit_validation_failed itself
    # raises on a non-zero FailedEntryCount, so an emit failure also retries; the
    # only residue is a possible duplicate DLQ record, which is benign for an
    # operator-triage queue.
    dlq.send_rejection(detail)
    emit_validation_failed(detail)


def _parse_pushed_at(ecr_pushed_at_iso: str) -> datetime:
    """Parse the AWS-set ``ecrPushedAt`` (EventBridge event time) to an aware datetime."""
    pushed = datetime.fromisoformat(ecr_pushed_at_iso.replace("Z", "+00:00"))
    if pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=UTC)
    return pushed


def _temporal_check_layers(
    *,
    layers: list[dict[str, Any]],
    ecr_pushed_at_iso: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Per-OSCAL-layer temporal consistency against ``ecrPushedAt`` ([PIPELINE-009]).

    There is no synthetic bundle-level ``created`` annotation. Temporal
    consistency is enforced PER OSCAL layer against the layer's in-band
    ``metadata.last-modified`` (captured by OSCAL extraction):

    - a layer whose ``last-modified`` is strictly AFTER ``ecrPushedAt`` is
      temporally impossible (authored after it was submitted) → a permanent
      failure criterion;
    - a layer whose ``ecrPushedAt - last-modified`` exceeds the staleness window
      (default 30 days) sets ``temporalConsistencyWarning=true`` (still accepted);
    - a layer with NO in-band ``metadata.last-modified`` contributes no assertion
      (skipped gracefully — neither rejected nor warned).

    ``ecrPushedAt`` ALWAYS comes from the EventBridge event time, never a producer
    field. Returns ``(failed_criteria, warn_stale)``. The argument is the unified
    ``layers`` projection; only entries carrying an ``oscal`` sub-object are
    considered, with the OSCAL metadata read from ``entry["oscal"]["metadata"]``
    (key ``last_modified`` or its alias ``last-modified``; both read defensively)
    and the layer digest from ``entry["digest"]``.
    """
    pushed = _parse_pushed_at(ecr_pushed_at_iso)
    staleness = timedelta(days=STALENESS_WINDOW_DAYS)
    failed: list[dict[str, Any]] = []
    warn_stale = False
    for layer in layers:
        oscal = layer.get("oscal")
        if not isinstance(oscal, dict):
            # Non-OSCAL layer — no in-band OSCAL metadata, contributes no assertion.
            continue
        metadata = oscal.get("metadata") or {}
        last_modified_iso = metadata.get("last_modified") or metadata.get("last-modified")
        if not last_modified_iso:
            # Layer carries no in-band timestamp — contributes no assertion.
            continue
        last_modified = datetime.fromisoformat(str(last_modified_iso).replace("Z", "+00:00"))
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=UTC)
        layer_digest = layer.get("digest")
        if last_modified > pushed:
            failed.append(
                {
                    "criterion": "temporal",
                    "field": f"layer[{layer_digest}].metadata.last-modified",
                    "message": (
                        f"metadata.last-modified={last_modified_iso!r} is strictly after "
                        f"ecrPushedAt={ecr_pushed_at_iso!r}; "
                        "evidence cannot be authored after it was submitted"
                    ),
                }
            )
            continue
        if pushed - last_modified > staleness:
            warn_stale = True
    return failed, warn_stale


def _predicted_s3_key_prefix(
    org_id: str,
    tenant_id: str,
    project_id: str,
    manifest_digest: str,
) -> str:
    """Predicted canonical S3 key prefix for the archived bundle (digest-terminal).

    Returns ``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` — the single
    uniform three-segment WORM key shape ([WORM-002]). This MUST stay
    character-for-character identical to the archive Lambda's ``_s3_key_prefix``
    so the predicted OSCAL rlink points at the key archive actually writes
    (worm-archive "Predicted S3 URI matches the archived key shape" [WORM-002.04]).
    The three identity segments come from the producer's
    cross-checked claim triple; the manifest digest is the sole content-addressed
    identity anchor and no assessment period, bundle type, or control scope enters
    the key.
    """
    return f"{org_id}/{tenant_id}/{project_id}/{manifest_digest}/"


def _record_skip(
    *, image_digest: str | None, repository_name: str | None, skip_reason: str
) -> dict[str, Any]:
    """Emit the benign ``ValidationSkipped`` metric and return the ``_SKIPPED`` sentinel.

    Single chokepoint for every non-trigger artifact: the content
    manifest's own push, Notation signatures, OCI empty/referrer configs, image
    indexes, an attestation whose subject is missing, and anything unrecognized.
    A skip is neither a success nor a failure — no ``artifact.validation-failed``
    event, no promotion, no error. Logs only digest/repo/skipReason
    (no producer free-text), dimensions the metric on ``SkipReason``.
    """
    LOG.debug(
        "validate-bundle skipped non-trigger artifact",
        extra={
            "manifestDigest": image_digest,
            "repositoryName": repository_name,
            "skipReason": skip_reason,
        },
    )
    metrics.add_dimension(name="SkipReason", value=skip_reason)
    metrics.add_metric(name="ValidationSkipped", unit=MetricUnit.Count, value=1)
    return _SKIPPED


def _process_layers_and_build_reference(
    *,
    manifest: OciManifest,
    push: EcrPushEvent,
    manifest_digest: str,
    org_id: str,
    tenant_id: str,
    project_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Project EVERY content-manifest layer; build the evidence reference.

    Walks every layer in ``manifest.layers`` (no OSCAL-only skip) and emits a
    universal OCI descriptor entry — ``digest``, ``size``, ``mediaType``, and the
    ``org.opencontainers.image.title`` annotation (omitted when absent) — into a
    single unified ``layers`` list. OSCAL layers additionally carry an ``oscal``
    sub-object ``{"oscalSchemaVersion", "metadata"}`` re-derived from the parsed
    OSCAL blob; non-OSCAL layers contribute descriptor detail only, with no blob
    fetch (metadata-index *Per-layer descriptor projection* Requirement).

    The artifact reference is built for EVERY archived-eligible bundle (any
    bundle, OSCAL-bearing or not, once an archive bucket is configured); the
    OSCAL root uuid it carries is optional provenance (``None`` for a non-OSCAL
    bundle — see the ``make_artifact_reference`` call below).

    No canonical-tag name is derived here:
    the canonical tag is the nameless ``{artifact-kind}--_ref--{digest}``
    form, so no producer free text (OSCAL ``metadata.title`` / pushed filename)
    enters the tag path. Returns ``(layers, artifact_reference)``.

    Raises ``OscalExtractionError`` on any permanent OSCAL failure, and
    ``LayerProjectionError`` when a layer's title exceeds ``MAX_LAYER_TITLE_LEN``
    — the handler upgrades either to a ``PermanentValidationFailure``
    after emitting validation-failed.
    """
    layers: list[dict[str, Any]] = []
    oscal_root_uuid: str | None = None
    has_oscal = False
    for layer in manifest.layers:
        # Universal descriptor entry for EVERY layer, OSCAL or not (OCI field
        # names). The title is omitted when absent (None-pruning discipline).
        entry: dict[str, Any] = {
            "digest": layer.digest,
            "size": layer.size,
            "mediaType": layer.media_type,
        }
        title = layer.annotations.get(LAYER_REQUIRED_TITLE)
        if title:
            # Fail closed on an oversized title rather than truncate:
            # a faithful projection that would blow the DDB item limit is a
            # permanent validation failure, not a silent partial copy. The cap is
            # BYTE-true — the DDB 400 KB item limit is measured in bytes, and a
            # non-ASCII title can be ~4x its code-point count in UTF-8 — so measure
            # the encoded length, never len(title).
            title_bytes = len(title.encode("utf-8"))
            if title_bytes > MAX_LAYER_TITLE_LEN:
                raise LayerProjectionError(
                    f"layer org.opencontainers.image.title UTF-8 byte length {title_bytes} "
                    f"exceeds the {MAX_LAYER_TITLE_LEN}-byte cap; projecting it verbatim "
                    "could push the Metadata_Index item past DynamoDB's 400 KB limit",
                    layer_digest=layer.digest,
                )
            entry["title"] = title

        if layer.media_type.startswith(_OSCAL_MEDIA_PREFIX):
            is_first_oscal_layer = not has_oscal
            has_oscal = True
            blob = fetch_blob(
                registry_id=push.registry_id,
                repository_name=push.repository_name,
                blob_digest=layer.digest,
            )
            doc, version = parse_oscal_layer(blob, layer_digest=layer.digest)
            meta = extract_oscal_metadata(doc)
            # The OSCAL root identity comes from the FIRST OSCAL layer; it is
            # recorded as a distinct document identity on the evidence reference.
            if is_first_oscal_layer:
                oscal_root_uuid = meta.doc_uuid
            # OSCAL enrichment is a per-entry sub-object on the unified list, NOT
            # a separate list. oscalSchemaVersion is derived from THIS layer's own
            # metadata.oscal-version (content-authoritative, per-layer);
            # update_metadata indexes it from here, NOT from the declared
            # Config_Blob field.
            entry["oscal"] = {
                "oscalSchemaVersion": version,
                "metadata": meta.model_dump(mode="json"),
            }

        layers.append(entry)

    artifact_reference: dict[str, Any] | None = None
    if ARCHIVE_BUCKET:
        # Built for EVERY archived-eligible bundle, OSCAL-bearing or not: the
        # reference is a pure function of the bundle's digest + locators, and the
        # OSCAL root uuid is optional provenance (None for a non-OSCAL bundle, in
        # which case make_artifact_reference derives the digest-derived uuid for
        # both the resource uuid and document-ids[0]).
        # The S3 rlink is generated regardless of WORM_Enabled ([PIPELINE-015]).
        # Key prefix is the canonical worm-archive shape ([WORM-002]) — the same
        # digest-terminal builder the archive Lambda uses,
        # so the reference stays rebuildable from the canonical digest alone.
        s3_key_prefix = _predicted_s3_key_prefix(org_id, tenant_id, project_id, manifest_digest)
        s3_uri = f"s3://{ARCHIVE_BUCKET}/{s3_key_prefix}manifest.json"
        ecr_uri = ecr_image_uri(
            registry_id=push.registry_id,
            region=push.region,
            repository_name=push.repository_name,
            digest=manifest_digest,
        )
        # Pass the single canonical PREFIXED ``sha256:<hex>`` Manifest_Digest:
        # the builder derives the resource identity over this exact prefixed form
        # (so the index stays rebuildable from the canonical value) and reduces it
        # to the bare 64-hex form OSCAL requires for the rlink hashes.
        resource = make_artifact_reference(
            manifest_digest=manifest_digest,
            ecr_uri=ecr_uri,
            s3_uri=s3_uri,
            title=f"Artifact bundle {manifest_digest}",
            oscal_root_uuid=oscal_root_uuid,
        )
        artifact_reference = resource.model_dump(mode="json", by_alias=True, exclude_none=True)

    return layers, artifact_reference


def _subject_digest(manifest_dict: dict[str, Any]) -> str | None:
    """The ``subject`` descriptor digest of a manifest, or ``None``.

    A Producer_Attestation_Referrer's ``subject`` points at the content Manifest_Digest.
    The OciManifest model does not surface ``subject`` (it lives under
    ``extra="allow"``), so read it from the raw dict where the trigger logic
    needs it before/independent of model parsing.
    """
    subject = manifest_dict.get("subject")
    if not isinstance(subject, dict):
        return None
    digest = subject.get("digest")
    return str(digest) if digest else None


def _attestation_claim_layer_digest(manifest: OciManifest) -> str | None:
    """Digest of the Producer_Attestation_Referrer's claim LAYER, or ``None``.

    ``cairn push`` attaches the claim via ``oras attach``, which carries the
    claim JSON as the referrer's single LAYER (mediaType ==
    ``PRODUCER_ATTESTATION_ARTIFACT_TYPE``) over an empty OCI scratch config — the same
    payload-in-layer + empty-config shape every other Cairn referrer
    (the evidence-reference resource referrer, the WORM_Receipt) uses. The claim
    is therefore read from the claim layer, NEVER from the (empty) config blob:
    the referrer's top-level ``artifactType`` is the discriminator, not its
    config mediaType. Returns the first layer whose mediaType is the attestation
    type, else — defensively — the sole layer's digest when the referrer carries
    exactly one layer, else ``None`` (no claim layer present).
    """
    for layer in manifest.layers:
        if layer.media_type == PRODUCER_ATTESTATION_ARTIFACT_TYPE:
            return layer.digest
    if len(manifest.layers) == 1:
        return manifest.layers[0].digest
    return None


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    """Core handler body; separated so observability_scope can wrap it cleanly.

    Inverted trigger: the single positive trigger is an
    Producer_Attestation_Referrer push. The triggering ``push.image_digest`` is the
    ATTESTATION digest; the content manifest digest is the attestation's
    ``subject``. Everything else is a benign skip.
    """
    push = parse_ecr_push_event(event)

    # [PIPELINE-010]: authenticated push principal MUST come from
    # EventBridge userIdentity, never producer-asserted fields.
    try:
        principal = extract_authenticated_push_principal(event)
    except MissingUserIdentityError as e:
        _emit_failure_event(
            manifest_digest=push.image_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "userIdentity",
                    "field": "event.detail.userIdentity",
                    "message": str(e),
                }
            ],
            error_kind="missing_user_identity",
        )
        raise PermanentValidationFailure(str(e)) from e

    # Fetch the TRIGGERING manifest (the pushed object).
    try:
        triggering_manifest_dict, _raw = fetch_manifest(
            registry_id=push.registry_id,
            repository_name=push.repository_name,
            image_digest=push.image_digest,
        )
    except EcrFetchError:
        # Transient; let Lambda retry. Don't emit a validation-failed
        # event because this isn't a permanent classification.
        raise

    # === Inverted trigger / artifact filter ===
    # The single POSITIVE trigger is a manifest whose `artifactType` is the
    # Producer_Attestation_Referrer media type over a content-manifest `subject`. EVERY
    # other push is skipped with a benign, non-failure disposition:
    #   - a known pipeline-internal shape (Notation signature / OCI empty config /
    #     image index) — classified by classify_pipeline_internal;
    #   - a content manifest pushed on its own (config.mediaType == the Cairn
    #     content Config_Blob type) — an incomplete submission, never promoted
    #     until its attestation arrives;
    #   - anything else unrecognized.
    # The trigger discrimination is done HERE in the validate handler (the spec
    # forbids editing classify_pipeline_internal in libs/oci for this).
    artifact_type = triggering_manifest_dict.get("artifactType")
    if artifact_type != PRODUCER_ATTESTATION_ARTIFACT_TYPE:
        # Reuse the libs/oci classifier for the known pipeline-internal shapes so
        # the signature/empty-config/image-index SkipReason values stay canonical;
        # fall back to handler-local reasons for the content-manifest and
        # unrecognized cases.
        internal = classify_pipeline_internal(triggering_manifest_dict)
        if internal is not None:
            skip_reason = internal.value
        else:
            config = triggering_manifest_dict.get("config")
            config_media_type = config.get("mediaType") if isinstance(config, dict) else None
            if config_media_type == CONFIG_BLOB_MEDIA_TYPE:
                skip_reason = _SKIP_REASON_CONTENT_MANIFEST
            else:
                skip_reason = _SKIP_REASON_NOT_ATTESTATION
        return _record_skip(
            image_digest=push.image_digest,
            repository_name=push.repository_name,
            skip_reason=skip_reason,
        )

    # Positively-identified Producer_Attestation_Referrer. Its `subject` is the content
    # manifest digest. An attestation with no subject is malformed as a TRIGGER —
    # treat it as a benign non-trigger skip (it cannot select content to promote),
    # never a permanent content-bundle failure.
    content_digest = _subject_digest(triggering_manifest_dict)
    if content_digest is None:
        return _record_skip(
            image_digest=push.image_digest,
            repository_name=push.repository_name,
            skip_reason=_SKIP_REASON_ATTESTATION_NO_SUBJECT,
        )

    # Parse the producer's claim from the Producer_Attestation_Referrer claim LAYER — the
    # claim is read from the referrer, NEVER reconstructed from the content
    # manifest/Config_Blob. `cairn push` attaches the claim via `oras attach`,
    # which carries the claim JSON as the referrer's LAYER (mediaType ==
    # PRODUCER_ATTESTATION_ARTIFACT_TYPE) over an empty OCI scratch config — the same
    # payload-in-layer shape every other Cairn referrer uses. The referrer's
    # top-level `artifactType` is the trigger discriminator, not its config.
    try:
        attestation_manifest = OciManifest.model_validate(triggering_manifest_dict)
    except ValidationError as e:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=_schema_failed_criteria(
                e, criterion="manifest_schema", default_field="manifest"
            ),
            error_kind="malformed_attestation",
        )
        raise PermanentValidationFailure(
            "Producer_Attestation_Referrer manifest failed schema validation; see failedCriteria"
        ) from e

    claim_layer_digest = _attestation_claim_layer_digest(attestation_manifest)
    if claim_layer_digest is None:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "attestation_schema",
                    "field": "attestation.layers",
                    "message": (
                        "Producer_Attestation_Referrer carries no claim layer "
                        f"(mediaType {PRODUCER_ATTESTATION_ARTIFACT_TYPE!r}); "
                        "the producer claim is absent"
                    ),
                }
            ],
            error_kind="malformed_attestation_claim",
        )
        raise PermanentValidationFailure(
            "Producer_Attestation_Referrer carries no claim layer; see failedCriteria"
        )

    # The claim layer is a standard in-toto Statement:
    # the producer's flat claim rides in the
    # Statement's `predicate`, and `read_claim_from_statement` unwraps it back to
    # the SAME flat ProducerAttestationReferrer the downstream Property-4 cross-checks and
    # Metadata_Index projection already consume (so nothing downstream changes).
    # Two failure shapes route through the EXISTING malformed-claim → DLQ branch
    # (no new unbounded payload surface; the byte/count caps in
    # _emit_failure_event still bound the failure event):
    #   1. The bytes aren't a valid in-toto Statement object (bad JSON / wrong
    #      type / predicate fails ProducerAttestationReferrer schema) → caught below as a
    #      ValueError (json.JSONDecodeError) or pydantic ValidationError.
    #   2. The Statement is structurally a Statement but carries the wrong payload
    #      — predicateType is not the Cairn URI (D5), or the subject array is not
    #      exactly one element (D4) — which read_claim_from_statement raises as
    #      InvalidStatementError (a ValueError subclass).
    try:
        claim_bytes = fetch_blob(
            registry_id=push.registry_id,
            repository_name=push.repository_name,
            blob_digest=claim_layer_digest,
        )
        statement = json.loads(claim_bytes)
        if not isinstance(statement, dict):
            raise InvalidStatementError(
                f"in-toto Statement must be a JSON object, got {type(statement).__name__}"
            )
        claim = read_claim_from_statement(statement)
    except (ValidationError, InvalidStatementError, json.JSONDecodeError) as e:
        if isinstance(e, ValidationError):
            failed_criteria = _schema_failed_criteria(
                e,
                criterion="attestation_schema",
                field_prefix="attestation.",
                default_field="claim",
            )
        else:
            # InvalidStatementError (predicateType / subject-count) or a JSON
            # decode failure — a single descriptive criterion (no pydantic loc).
            failed_criteria = [
                {
                    "criterion": "attestation_schema",
                    "field": "attestation.statement",
                    "message": str(e),
                }
            ]
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=failed_criteria,
            error_kind="malformed_attestation_claim",
        )
        raise PermanentValidationFailure(
            "Producer_Attestation_Referrer claim failed in-toto Statement validation; "
            "see failedCriteria"
        ) from e

    # === D4 dual-subject consistency check ====================================
    # The in-toto Statement carries its OWN subject (an algorithm-keyed DigestSet)
    # in addition to the OCI envelope subject. The OCI envelope subject
    # (content_digest, via _subject_digest above) REMAINS the sole authority for
    # the anti-hijack repository-path binding below; here we additionally verify
    # the Statement's subject AGREES with it. read_claim_from_statement
    # already asserted the subject array is exactly one element, so statement
    # ["subject"][0]["digest"] is the single DigestSet to compare. Divergence is a
    # producer-reachable inconsistency → permanent failure routed to the DLQ; the
    # Statement subject is NEVER used independently of the envelope subject.
    # subject_digest_matches owns the non-dict / missing-algorithm guard (it
    # returns False, never raises), so no separate isinstance check is needed.
    statement_digest_set = statement["subject"][0].get("digest")
    if not subject_digest_matches(content_digest, statement_digest_set):
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "statement_subject",
                    "field": "attestation.statement.subject",
                    "message": (
                        "in-toto Statement subject digest "
                        f"{statement_digest_set!r} does not agree with the OCI envelope "
                        f"subject digest {content_digest!r}; the envelope subject is the "
                        "sole anti-hijack authority and the Statement subject MUST match it"
                    ),
                }
            ],
            error_kind="statement_subject_divergence",
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(
            "in-toto Statement subject diverges from the OCI envelope subject; see failedCriteria"
        )

    # === Subject-scope binding BEFORE any resolve/promote ===
    # The referrer's subject is a free digest pointer. Confirm the content
    # manifest actually resides in the SAME IAM-enforced ECR repository as the
    # triggering Producer_Attestation_Referrer. A foreign-scope subject is a cross-scope
    # attestation hijack: a PERMANENT validation failure routed to the DLQ, never
    # resolved/signed/archived/indexed. The triggering repo IS the IAM-enforced
    # scope (push authorization scopes account-id/project-id path segments), so
    # "does the subject digest resolve in THIS repo?" is the binding check.
    # A transient EcrFetchError on the scope HEAD propagates (Lambda retries);
    # only a positive not-found returns False below — never a misclassified outage.
    in_scope = image_resides_in_repo(
        registry_id=push.registry_id,
        repository_name=push.repository_name,
        image_digest=content_digest,
    )
    if not in_scope:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "subject_scope",
                    "field": "attestation.subject",
                    "message": (
                        f"Producer_Attestation_Referrer subject content manifest "
                        f"{content_digest!r} does not reside in the triggering "
                        f"repository {push.repository_name!r} (cross-scope "
                        "attestation hijack); attestation and subject MUST share "
                        "one IAM-enforced ECR repository path"
                    ),
                }
            ],
            error_kind="cross_scope_attestation_hijack",
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(
            f"cross-scope attestation hijack: subject {content_digest!r} is not in "
            f"the triggering repository {push.repository_name!r}"
        )

    # === Resolve the content manifest + Config_Blob via the subject digest =====
    # A transient EcrFetchError propagates (Lambda retries; the scope check above
    # already confirmed the subject is present in this repo).
    manifest_dict, _content_raw = fetch_manifest(
        registry_id=push.registry_id,
        repository_name=push.repository_name,
        image_digest=content_digest,
    )

    try:
        manifest = OciManifest.model_validate(manifest_dict)
    except ValidationError as e:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=_schema_failed_criteria(
                e, criterion="manifest_schema", default_field="manifest"
            ),
            error_kind="malformed_manifest",
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(
            "OCI content manifest failed schema validation; see failedCriteria"
        ) from e
    config_blob_bytes = fetch_blob(
        registry_id=push.registry_id,
        repository_name=push.repository_name,
        blob_digest=manifest.config.digest,
    )

    # The producer-DECLARED artifact-kind for the audit trail is now
    # sourced from the CLAIM (not a content-manifest annotation or tag).
    supplied_kind = claim.artifact_kind

    # [INGEST-017.07] / [INGEST-029.06] content-structure (a)-(c) + claim-derived (d),(c5),(h).
    result = validate_bundle(manifest, config_blob_bytes)
    failed_criteria: list[dict[str, Any]] = [_criterion_dict(e) for e in result.errors]
    failed_criteria.extend(_criterion_dict(e) for e in validate_claim(claim))

    # Config_Blob parse may have failed inside validate_bundle; in that
    # case the cross-checks below would NPE on the missing object. Emit
    # validation-failed and stop.
    config_blob = result.config_blob
    if config_blob is None:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=failed_criteria,
            error_kind="invalid_config_blob",
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure("Config_Blob invalid; see failedCriteria")

    # [PIPELINE-013]: the producer-asserted identity triple orgId / tenantId /
    # projectId (sourced from the Producer_Attestation_Referrer claim) must EACH
    # exact-match the corresponding {org-id}/{tenant-id}/{project-id} segment of
    # the IAM-enforced ECR repository path. The check is uniform across all three
    # segments — one path shape, nothing gated on a deployment mode (design
    # D.1/D.5). A disagreement on any coordinate is a permanent failure routed to
    # the DLQ (the triple composes the immutable WORM archive key, so each
    # coordinate must be honest-by-construction against the path).
    failed_criteria.extend(
        _criterion_dict(e) for e in crosscheck_identity(claim, push.repository_name)
    )

    if failed_criteria:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=failed_criteria,
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(f"{len(failed_criteria)} permanent validation failure(s)")

    # Re-seen short-circuit (HARD CORRECTNESS).
    # Suppress only a true duplicate of an already-completed bundle. MUST run
    # AFTER crosscheck_identity has verified the claim's org / tenant / project
    # against the IAM-enforced ECR path (the `if failed_criteria` gate above):
    # the dedup predicate keys on the tenant-PK schema (claim tenant-id +
    # project-id), so keying it on UNVERIFIED producer-asserted coordinates would
    # expose a cross-tenant existence oracle (a tenant-A producer probing
    # skip-vs-DLQ for a guessed tenant-B manifest digest). Past this point every
    # coordinate is cross-check-verified; the triggering referrer digest is
    # push.image_digest. Runs before any side effect.
    if _re_seen_terminal_duplicate(
        content_digest=content_digest,
        tenant_id=claim.tenant_id,
        project_id=claim.project_id,
        attestation_referrer_digest=push.image_digest,
    ):
        return _record_skip(
            image_digest=content_digest,
            repository_name=push.repository_name,
            skip_reason=_SKIP_REASON_RE_SEEN,
        )

    # Per-invocation TenantId metric dimension under the CairnProofs namespace
    # ([OBS-009]/observability). Added only here, AFTER the cross-check above has
    # verified claim.tenant_id exact-matches the IAM-enforced ECR path's
    # {tenant-id} segment, so the dimension value is the trusted path-bound id
    # and never producer free text (no unbounded-cardinality
    # producer input on the metric stream).
    metrics.add_dimension(name="TenantId", value=claim.tenant_id)

    # assessmentPeriod is producer-supplied in the claim and NEVER derived (D1).
    # The period rides verbatim from the Producer_Attestation_Referrer claim into the
    # validated payload — the period is never resolved from ``created``.

    # OSCAL extraction (Req 8.x) + universal per-layer projection. Any
    # OscalExtractionError is permanent; a LayerProjectionError (oversized title)
    # is likewise permanent.
    try:
        layers, artifact_reference = _process_layers_and_build_reference(
            manifest=manifest,
            push=push,
            manifest_digest=content_digest,
            org_id=claim.org_id,
            tenant_id=claim.tenant_id,
            project_id=claim.project_id,
        )
    except OscalExtractionError as e:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "oscal",
                    "field": (
                        f"layer[{e.layer_digest}].metadata.oscal-version"
                        if e.layer_digest
                        else "oscal layer metadata.oscal-version"
                    ),
                    "message": str(e),
                }
            ],
            layer_digest=e.layer_digest,
            error_kind=e.error_kind,
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(str(e)) from e
    except LayerProjectionError as e:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "layer_title",
                    "field": f"layer[{e.layer_digest}].annotations.{LAYER_REQUIRED_TITLE}",
                    "message": str(e),
                }
            ],
            layer_digest=e.layer_digest,
            error_kind="layer_title_too_large",
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(str(e)) from e

    # [PIPELINE-009] — per-OSCAL-layer temporal consistency against ecrPushedAt (the
    # EventBridge event time, never a producer field). A layer whose in-band
    # metadata.last-modified is strictly after ecrPushedAt is a permanent failure;
    # drift beyond the staleness window sets temporalConsistencyWarning; layers
    # with no in-band timestamp are skipped gracefully.
    temporal_failures, warn_stale = _temporal_check_layers(
        layers=layers,
        ecr_pushed_at_iso=push.ecr_pushed_at,
    )
    if temporal_failures:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=temporal_failures,
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(
            f"{len(temporal_failures)} temporal-consistency failure(s)"
        )

    # Confirm Config_Blob media type once more for the event payload
    # (validate_bundle already covered the failure path).
    if manifest.config.media_type != CONFIG_BLOB_MEDIA_TYPE:
        _emit_failure_event(
            manifest_digest=content_digest,
            repository_name=push.repository_name,
            failed_criteria=[
                {
                    "criterion": "config_media_type",
                    "field": "manifest.config.mediaType",
                    "message": (
                        f"Config_Blob media type {manifest.config.media_type!r} != "
                        f"{CONFIG_BLOB_MEDIA_TYPE!r}; slipped past validate_bundle"
                    ),
                }
            ],
            error_kind="config_media_type_mismatch",
            supplied_kind=supplied_kind,
            project_id=claim.project_id,
        )
        raise PermanentValidationFailure(
            "Config_Blob media type mismatch slipped past validate_bundle"
        )

    # Compose artifact.validated payload.
    # manifestDigest is the CONTENT digest (resolved from the attestation subject);
    # attestationDigest is the triggering Producer_Attestation_Referrer's own OCI digest;
    # attestation is the projected claim.
    validated_payload: dict[str, Any] = {
        "manifestDigest": content_digest,
        "attestationDigest": push.image_digest,
        "attestation": claim.model_dump(mode="json", by_alias=True, exclude_none=True),
        "repositoryName": push.repository_name,
        "registryId": push.registry_id,
        "region": push.region,
        "ecrPushedAt": push.ecr_pushed_at,
        "temporalConsistencyWarning": warn_stale,
        "authenticatedPushPrincipal": {
            "arn": principal.arn,
            "type": principal.type,
            "accountId": principal.account_id,
            "userName": principal.user_name,
            "mfaAuthenticated": principal.mfa_authenticated,
            "principalId": principal.principal_id,
        },
        "configBlob": config_blob.model_dump(mode="json", by_alias=True, exclude_none=True),
        "manifestAnnotations": dict(manifest.annotations),
        "layers": layers,
    }
    if artifact_reference is not None:
        validated_payload["artifactResourceReference"] = artifact_reference

    # Record the producer-SUPPLIED artifact-kind token next to its
    # resolved canonical (sourced from the CLAIM now) so the audit trail can
    # answer "what did the producer literally send" — aliases
    # (`policies` → `governance-doc`, …) otherwise leave only the canonical behind.
    kind = _supplied_resolved_kind(supplied_kind)
    validated_payload.update(kind)
    # OBS-008.01 success-record parity. Same four-field attachment as the failure
    # chokepoint, via the shared _emit_stage_log helper: eventType=validated (the
    # entry-verb baggage "validate" is overridden), status=success, durationMs
    # from the per-invocation anchor, projectId from the already-cross-checked
    # claim (so it equals the IAM-enforced ECR path segment — the truncation in
    # the helper is a no-op here). No failedCriteria on the success record.
    _emit_stage_log(
        "validate-bundle validated",
        level=logging.INFO,
        event_type=_SUCCESS_EVENT_TYPE,
        status=_SUCCESS_STATUS,
        project_id=claim.project_id,
        extra={
            "manifestDigest": content_digest,
            "attestationDigest": push.image_digest,
            "repositoryName": push.repository_name,
            **kind,
        },
    )

    emit_validated(validated_payload)

    # Record the canonical-tag coordinates on the durable digest-keyed CONTENT row
    # for the mint stage. artifactKind is sourced from the CLAIM now;
    # the canonical tag carries no name, so none
    # is recorded. Written AFTER emit_validated so a retry before a successful
    # emit re-runs fully.
    _record_coordinates(
        image_digest=content_digest,
        tenant_id=claim.tenant_id,
        project_id=claim.project_id,
        artifact_kind=kind.get("resolvedArtifactKind") or kind.get("suppliedArtifactKind"),
        attestation_referrer_digest=push.image_digest,
    )
    return validated_payload


def _recover_origin_links(event: dict[str, Any]) -> list[trace.Link] | None:
    """Best-effort span links to the originating bundle's trace.

    A pipeline-internal artifact ``PutImage`` (Notation signature referrer, OCI
    image index, OCI empty config, mint-tag re-tag) lands on validate-bundle as
    a ``ValidationSkipped`` invocation that — absent a link — would root its own
    disconnected trace. The OTEL-idiomatic representation of "causally related but
    separately rooted" is a span **link** to the originating bundle's
    ``SpanContext`` (NOT a forced shared trace id), recorded on this invocation's
    span at creation time via ``observability_scope(links=...)``.

    Recovery, not injection: ECR authors these ``PutImage`` events, so they carry
    no ``traceparent`` we could extract. The originating context must therefore be
    recovered from something already in scope. Nothing in
    scope at handler entry yields it:

      - The ECR event itself carries no W3C carrier (``extract_trace_context``
        would resolve ``source="none"`` from it).
      - The artifact's ``subject`` digest (the Notation referrer's link to the
        signed manifest) is only readable AFTER the manifest fetch inside
        ``_handle`` — and even once known, there is no store mapping
        subject-digest → originating trace context. The durable Metadata_Index
        row keyed on the manifest digest persists ``validateStatus`` /
        ``artifactKind`` ONLY; it does not persist the originating invocation's
        ``traceparent``.

    Persisting a digest→trace-context map is NEW out-of-scope infrastructure
    (Non-Goals: "no new infra"), so this helper returns ``None`` — the
    spec-compliant "Originating context unavailable — skip proceeds unlinked"
    path. The seam stays in place so that if a recovery source is added later
    (e.g. the row gains an originating-``traceparent`` column), this function can
    extract it and return ``[trace.Link(span_context)]`` with no change to the
    call site. Returning ``None`` (or an empty list) records no link and never
    gates the skip path.
    """
    return None


@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point. Returns the emitted ``artifact.validated`` payload."""
    request_id = getattr(context, "aws_request_id", None)
    start = time.perf_counter()
    # Publish the per-invocation start anchor so _emit_failure_event can derive
    # the OBS-008 durationMs from the SAME monotonic reading the finally block
    # below reads for the DurationMs metric (both read `start`). A ContextVar set
    # per invocation is inherently parallel-safe and never leaks across calls.
    _INVOCATION_START.set(start)
    # Best-effort span link to the originating bundle trace for the pipeline-internal
    # fan-out. Recovered (never injected — ECR authors the event);
    # `None` when no recovery source is in scope — the
    # spec-compliant unlinked path. Recording a link never gates the skip.
    origin_links = _recover_origin_links(event)
    with observability_scope(
        event, request_id=request_id, event_type="validate", links=origin_links
    ):
        try:
            try:
                payload = _handle(event)
            except (PermanentValidationFailure, MalformedEcrEvent) as e:
                # Permanent failure. The _handle path already emitted the
                # validation-failed event + in-band DLQ rejection via the
                # _emit_failure_event chokepoint; a MalformedEcrEvent escapes
                # event parsing BEFORE that chokepoint, so emit here (which also
                # sends its in-band DLQ rejection — [PIPELINE-006] for the malformed
                # branch too).
                LOG.warning("validate-bundle permanent failure", extra={"reason": str(e)})
                if isinstance(e, MalformedEcrEvent):
                    _emit_failure_event(
                        manifest_digest=None,
                        repository_name=None,
                        failed_criteria=[
                            {
                                "criterion": "event_envelope",
                                "field": "event.detail",
                                "message": str(e),
                            }
                        ],
                        error_kind="malformed_event",
                    )
                metrics.add_dimension(name="Reason", value="Permanent")
                metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
                # [PIPELINE-006]: complete the invocation as a non-retryable success so
                # Lambda's async-invoke machinery does NOT re-run the handler
                # against a structurally-invalid bundle. The rejection is already
                # on the DLQ in-band; returning (not raising) is what makes
                # "directly to the DLQ without further retries" hold.
                return _REJECTED
            # A skipped pipeline-internal artifact is neither Success nor Failure,
            # and a permanently-rejected bundle (_REJECTED) already recorded the
            # Failure metric above: _handle / the permanent branch returned a
            # sentinel. Withhold the Success metric for both so neither inflates
            # the ingestion-success series ([PIPELINE-006]).
            if payload is not _SKIPPED and payload is not _REJECTED:
                metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
            return payload
        except Exception:
            # Any other exception is transient — Lambda retries and
            # eventually DLQ-routes. No validation-failed event for
            # transient failures.
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=elapsed_ms)
