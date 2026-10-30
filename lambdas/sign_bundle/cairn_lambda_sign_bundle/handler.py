"""sign-bundle Lambda entry point.

Pipeline stage 2. Triggered by ``artifact.validated`` events. Signs the
submission via Notation + AWS Signer.

A submission is TWO OCI objects: the content manifest (pure content
identity) and the Attestation referrer (the producer's signed compliance
claim, ``subject`` = content manifest digest). The pipeline produces
**two independent signatures**: one whose ``subject`` is the content
manifest digest and one whose ``subject`` is the Attestation referrer's
own digest. Each is its own OCI Notation signature referrer (Notation
attaches it transparently as part of ``notation sign``). The two signings
are independent — signing one does not require re-signing the other —
which enables "same content, multiple attestations" and an independent
human co-sign over a claim.

A bundle is "signed" for the purpose of proceeding to archival ONLY when
BOTH subjects carry a valid signature; the promotion gate is both, never
half (see signing *Pipeline Signing via Notation and AWS Signer*).

Idempotency ([PIPELINE-007]): each subject is independently digest-keyed. If a
Notation signature referrer already exists for a given subject digest, we
short-circuit that subject's signing and reuse the existing signature
digest. The whole invocation short-circuits only when BOTH subjects are
already signed.

Failure classification ([SIGN-008]): ``NotationCommandError`` is mapped to
transient vs permanent based on returncode/stderr. Transient errors
re-raise (Lambda retries). Permanent errors emit
``artifact.signing-failed`` and mark ``signingStatus: unsigned`` in the
Metadata_Index, then re-raise so the DLQ captures the bundle. A permanent
failure on EITHER subject fails the whole invocation — a half-signed
submission is never promoted.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

import boto3
from cairn_metadata import MetadataIndex
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
from cairn_signing import (
    NotationCommandError,
    SigningProfile,
    read_timestamp_provider_from_ssm,
    sign_artifact,
)
from cairn_signing.notation import configure_ecr_docker_auth
from cairn_signing.timestamp_provider import TimestampProvider

from cairn_lambda_sign_bundle.events import emit_signed, emit_signing_failed
from cairn_lambda_sign_bundle.referrers import existing_signature_digest

LOG = logging.getLogger(__name__)
configure_root_logger()

HANDLER_NAME = "sign-bundle"
STAGE = os.environ.get("STAGE") or os.environ.get("CAIRN_STAGE", "")
REGION_CODE = os.environ.get("REGION_CODE") or os.environ.get("CAIRN_REGION_CODE", "")
SIGNER_PROFILE_ARN = os.environ.get("SIGNER_PROFILE_ARN", "")
METADATA_TABLE_NAME = os.environ.get("METADATA_TABLE_NAME", "")
SIGN_TIMEOUT_SECONDS = float(os.environ.get("SIGN_TIMEOUT_SECONDS", "60"))

# Post-sign Referrers-API propagation poll. `notation sign` durably attaches the
# signature referrer, but ECR's Referrers API is eventually consistent: a fast
# invocation can list referrers before the just-attached signature has
# propagated, yielding a spurious "no referrer found". We poll a bounded number
# of times before treating it as a hard failure (the design-anticipated "bounded
# retry on the Referrers lookup"). This only waits for the READ side to catch up
# — the signature is already written — so it converts a per-subject
# multi-invocation retry flap (which burns the async-retry budget and can race to
# the DLQ) into a single clean invocation. Bounded well within the sign Lambda's
# multi-minute timeout.
SIGNATURE_LOOKUP_MAX_ATTEMPTS = int(os.environ.get("SIGNATURE_LOOKUP_MAX_ATTEMPTS", "6"))
SIGNATURE_LOOKUP_DELAY_SECONDS = float(os.environ.get("SIGNATURE_LOOKUP_DELAY_SECONDS", "1.5"))

if STAGE:
    os.environ.setdefault("CAIRN_STAGE", STAGE)
if REGION_CODE:
    os.environ.setdefault("CAIRN_REGION_CODE", REGION_CODE)

metrics = Metrics(
    namespace=metric_namespace(REGION_CODE, STAGE),
    service=HANDLER_NAME,
)
set_stage_default_dimension(metrics, STAGE)

# stderr substrings that we treat as transient (worth a retry). Anything
# else from Notation we treat as permanent. The list is conservative —
# better to over-retry than to silently mark a fixable failure as permanent.
_TRANSIENT_STDERR_MARKERS = (
    "ThrottlingException",
    "throttling",
    "Throttling",
    "RequestTimeout",
    "TimeoutError",
    "i/o timeout",
    "connection reset",
    "connection refused",
    "ServiceUnavailable",
    "InternalServerError",
    "503",
    "500",
)


def _is_transient(err: NotationCommandError) -> bool:
    """Best-effort classify a Notation/Signer error as transient vs permanent."""
    if err.returncode == 0:
        # Anomaly path from sign_artifact's post-sign list lookup; safer to retry.
        return True
    stderr = err.stderr or ""
    return any(marker in stderr for marker in _TRANSIENT_STDERR_MARKERS)


def _extract_validated_detail(event: dict[str, Any]) -> dict[str, Any]:
    """The EventBridge wrapper puts our payload under ``detail``."""
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise ValueError("event.detail missing or not a dict on artifact.validated event")
    return detail


def _mark_unsigned_in_metadata(
    *,
    manifest_digest: str,
    tenant_id: str,
    project_id: str,
    error_message: str,
) -> None:
    """Best-effort Metadata_Index update to ``signingStatus: unsigned`` ([SIGN-008]).

    Uses ``update_record_if_newer`` so an out-of-order signed event from a
    retried success can still win. If the record doesn't yet exist (the
    validated event hasn't been processed by update-metadata yet), the
    conditional write fails with ``ConditionalUpdateFailed``; we log and
    drop because update-metadata will eventually create the record with
    the same unsigned state from the signing-failed event itself.

    The row is addressed by the opaque ``tenant_id`` partition key and the
    ``{project_id}#{manifest_digest}`` sort key ([META-001]); both coordinates
    are sourced from the signed claim's identity triple.
    """
    if not METADATA_TABLE_NAME:
        return
    try:
        index = MetadataIndex(METADATA_TABLE_NAME)
        index.update_on_event(
            manifest_digest=manifest_digest,
            tenant_id=tenant_id,
            project_id=project_id,
            updates={"signingStatus": "unsigned", "signingError": error_message},
            event_timestamp=datetime.now(UTC).isoformat(),
        )
    except ConditionalUpdateFailed:
        LOG.info("metadata record not yet present for unsigned mark; update-metadata will pick up")


def _artifact_ref(*, registry_id: str, region: str, repository_name: str, digest: str) -> str:
    return f"{registry_id}.dkr.ecr.{region}.amazonaws.com/{repository_name}@{digest}"


def _sign_subject(
    *,
    subject_label: str,
    subject_digest: str,
    registry_id: str,
    repository_name: str,
    region: str,
    profile: SigningProfile,
    provider: TimestampProvider,
) -> tuple[str, bool]:
    """Sign one digest-keyed subject as its own Notation signature referrer.

    Reuses the single-signature Notation invocation (``sign_artifact``) for
    BOTH the content manifest and the Attestation referrer — the only thing
    that differs between the two subjects is the ``subject`` digest baked
    into ``artifact_ref``. The two callers are fully independent: signing
    one subject neither reads nor mutates the other's signature.

    Returns ``(signature_digest, idempotent_short_circuit)``.

    Raises ``NotationCommandError`` on a Notation failure (caller maps to
    transient retry vs permanent DLQ) and ``RuntimeError`` if signing
    reports success but no referrer is found.
    """
    # [PIPELINE-007] idempotency: short-circuit this subject if its signature exists.
    existing = existing_signature_digest(
        registry_id=registry_id,
        repository_name=repository_name,
        image_digest=subject_digest,
    )
    if existing:
        LOG.info(
            "existing notation signature referrer found; short-circuiting subject",
            extra={
                "subject": subject_label,
                "subjectDigest": subject_digest,
                "signatureDigest": existing,
            },
        )
        return existing, True

    artifact_ref = _artifact_ref(
        registry_id=registry_id,
        region=region,
        repository_name=repository_name,
        digest=subject_digest,
    )
    sign_artifact(
        artifact_ref,
        profile,
        timestamp_provider=provider,
        timeout_seconds=SIGN_TIMEOUT_SECONDS,
    )
    # Resolve the just-attached signature's referrer digest via ECR's Referrers
    # API, polling for the eventual-consistency propagation window before
    # declaring failure (the signature is durably attached; only the read side
    # lags — see SIGNATURE_LOOKUP_* above). notation list's output flag isn't
    # stable across CLI versions, so we read from ECR directly.
    signature_digest = _poll_signature_digest(
        registry_id=registry_id,
        repository_name=repository_name,
        subject_digest=subject_digest,
    )
    if signature_digest is None:
        raise RuntimeError(
            f"notation sign succeeded but no signature referrer found on "
            f"{repository_name}@{subject_digest} (subject={subject_label}) "
            f"after {SIGNATURE_LOOKUP_MAX_ATTEMPTS} Referrers-API polls"
        )
    return signature_digest, False


def _poll_signature_digest(
    *, registry_id: str, repository_name: str, subject_digest: str
) -> str | None:
    """Poll the Referrers API for a just-attached signature referrer, bounded.

    Returns the signature referrer digest as soon as it propagates, or ``None``
    if it has not appeared within ``SIGNATURE_LOOKUP_MAX_ATTEMPTS`` polls. The
    first poll is immediate; subsequent polls wait ``SIGNATURE_LOOKUP_DELAY_SECONDS``
    to ride out ECR's Referrers-API eventual-consistency window so a fast
    invocation does not spuriously report a missing referrer for a signature it
    just durably attached.
    """
    for attempt in range(SIGNATURE_LOOKUP_MAX_ATTEMPTS):
        digest = existing_signature_digest(
            registry_id=registry_id,
            repository_name=repository_name,
            image_digest=subject_digest,
        )
        if digest is not None:
            return digest
        if attempt + 1 < SIGNATURE_LOOKUP_MAX_ATTEMPTS:
            time.sleep(SIGNATURE_LOOKUP_DELAY_SECONDS)
    return None


def _handle(event: dict[str, Any]) -> dict[str, Any]:
    detail = _extract_validated_detail(event)

    manifest_digest = detail["manifestDigest"]
    # The Attestation referrer's own OCI digest is the second signing
    # subject (D2). Carried by the validate
    # stage on the artifact.validated event.
    attestation_digest = detail.get("attestationDigest")
    repository_name = detail["repositoryName"]
    registry_id = detail["registryId"]
    region = detail["region"]
    config_blob = detail.get("configBlob", {})
    # The bundle's identity is the opaque orgId + tenantId + projectId triple
    # ([INGEST-005]), sourced from the signed Producer Attestation
    # referrer claim projected onto the event under `attestation` — never an AWS
    # account number, never a display name. tenantId is the Metadata_Index
    # partition-key isolation boundary; projectId is the sort-key sub-hierarchy
    # ([META-001]); orgId rides as a plain record attribute.
    attestation = detail.get("attestation")
    _org_id, tenant_id, project_id = identity_triple(detail)

    # Per-invocation TenantId metric dimension under the CairnProofs namespace
    # ([OBS-009]/observability). The value is the path-bound tenant id from the
    # claim (cross-check-verified upstream by validate before the
    # artifact.validated event was emitted), so it is never unbounded producer
    # free text. Added only when present so a malformed event missing the triple
    # does not crash the metric stream.
    if tenant_id:
        metrics.add_dimension(name="TenantId", value=tenant_id)
    # Optional OSCAL back-matter resource built by validate; forwarded
    # untouched so the archive stage can mint it as an OCI Referrer. Present
    # only for OSCAL-bearing bundles archived with a bucket configured.
    resource_reference = detail.get("artifactResourceReference")

    # Only system-clock is built. Anything else from SSM raises and the
    # Lambda goes to DLQ for operator intervention; this is intentional
    # (rfc3161 is recognized but unsupported).
    provider = read_timestamp_provider_from_ssm(STAGE)
    if provider != TimestampProvider.SYSTEM_CLOCK:
        raise RuntimeError(
            f"Unexpected TimestampProvider {provider.value!r} from SSM; only system-clock is built"
        )

    # Fail closed on a missing attestationDigest. The promotion gate is BOTH
    # signatures (signing *Pipeline Signing via Notation and AWS Signer*), so a
    # submission with no Attestation referrer to sign cannot legitimately
    # advance — validate ALWAYS sets attestationDigest on a well-formed
    # artifact.validated event. Degrading to one-subject signing here would
    # silently promote a half-signed (content-only) submission, violating the
    # both-signatures invariant. Treat the absence as a
    # PERMANENT failure: mark the bundle unsigned, emit artifact.signing-failed,
    # and raise so the DLQ captures it — the same triage path a permanent
    # Notation failure takes.
    if not attestation_digest:
        error_message = (
            "artifact.validated event missing attestationDigest; refusing to "
            "sign content-only (both-signatures promotion gate)"
        )
        LOG.error(
            "missing attestationDigest; failing closed",
            extra={"manifestDigest": manifest_digest},
        )
        if tenant_id and project_id:
            _mark_unsigned_in_metadata(
                manifest_digest=manifest_digest,
                tenant_id=tenant_id,
                project_id=project_id,
                error_message=error_message,
            )
        emit_signing_failed(
            {
                "manifestDigest": manifest_digest,
                "attestationDigest": attestation_digest,
                "repositoryName": repository_name,
                "signingError": error_message,
                # Thread the signed claim forward so update_metadata can source
                # the orgId/tenantId/projectId triple to address the unsigned row
                # ([META-001]). configBlob passthrough is preserved.
                "attestation": attestation,
                "configBlob": config_blob,
            }
        )
        raise RuntimeError(error_message)

    # Both subjects are signed against the same profile/plugin/timestamp
    # provider; the only difference is the subject digest. Resolve the
    # already-signed state of both up front so we can short-circuit the
    # whole invocation only when BOTH carry a signature (the promotion
    # gate = both signatures present), and otherwise authenticate to ECR
    # exactly once before signing whichever subject(s) still need it. The
    # attestation subject is ALWAYS present here — the fail-closed gate above
    # rejects a missing attestationDigest before we reach this point.
    subjects: list[tuple[str, str]] = [
        ("content", manifest_digest),
        ("attestation", attestation_digest),
    ]

    existing_by_subject: dict[str, str | None] = {
        label: existing_signature_digest(
            registry_id=registry_id,
            repository_name=repository_name,
            image_digest=digest,
        )
        for label, digest in subjects
    }
    all_already_signed = all(existing_by_subject[label] is not None for label, _ in subjects)

    if all_already_signed:
        LOG.info(
            "all subjects already signed; short-circuiting",
            extra={"manifestDigest": manifest_digest, "attestationDigest": attestation_digest},
        )
        signature_digests = {label: existing_by_subject[label] for label, _ in subjects}
        signed_payload = _build_signed_payload(
            manifest_digest=manifest_digest,
            attestation_digest=attestation_digest,
            attestation=attestation,
            repository_name=repository_name,
            registry_id=registry_id,
            region=region,
            provider=provider,
            signature_digests=signature_digests,
            idempotent_short_circuit=True,
            config_blob=config_blob,
            detail=detail,
            resource_reference=resource_reference,
        )
        emit_signed(signed_payload)
        return signed_payload

    if not SIGNER_PROFILE_ARN:
        raise RuntimeError("SIGNER_PROFILE_ARN env var is required")

    # Notation talks to ECR via the OCI distribution API and reads creds
    # from `$HOME/.docker/config.json`. Lambda has no prior `oras login`,
    # so we populate the file from the IAM role's short-lived ECR token
    # before the first invocation. Token is valid for 12h; we refresh per
    # invocation rather than caching since cold-start is rare anyway.
    registry_host = f"{registry_id}.dkr.ecr.{region}.amazonaws.com"
    configure_ecr_docker_auth(registry_host=registry_host, ecr_client=boto3.client("ecr"))

    profile = SigningProfile.pipeline(SIGNER_PROFILE_ARN)
    signature_digests: dict[str, str] = {}
    any_fresh_sign = False
    try:
        for label, digest in subjects:
            sig_digest, short_circuited = _sign_subject(
                subject_label=label,
                subject_digest=digest,
                registry_id=registry_id,
                repository_name=repository_name,
                region=region,
                profile=profile,
                provider=provider,
            )
            signature_digests[label] = sig_digest
            any_fresh_sign = any_fresh_sign or not short_circuited
    except NotationCommandError as e:
        # A permanent failure on EITHER subject fails the whole invocation:
        # a half-signed submission must never be promoted to archive.
        if _is_transient(e):
            LOG.warning(
                "transient notation failure; will retry",
                extra={"returncode": e.returncode, "stderr": e.stderr[:500]},
            )
            raise
        LOG.error(
            "permanent notation failure; marking bundle unsigned",
            extra={"returncode": e.returncode, "stderr": e.stderr[:500]},
        )
        if tenant_id and project_id:
            _mark_unsigned_in_metadata(
                manifest_digest=manifest_digest,
                tenant_id=tenant_id,
                project_id=project_id,
                error_message=f"notation exit {e.returncode}: {e.stderr.strip()[:500]}",
            )
        emit_signing_failed(
            {
                "manifestDigest": manifest_digest,
                "attestationDigest": attestation_digest,
                "repositoryName": repository_name,
                "signingError": str(e),
                # Thread the signed claim forward so update_metadata can source
                # the orgId/tenantId/projectId triple to address the unsigned row
                # ([META-001]). configBlob passthrough is preserved.
                "attestation": attestation,
                "configBlob": config_blob,
            }
        )
        # Re-raise so the DLQ captures the bundle for triage per [SIGN-008].
        # The artifact.signing-failed event is the alarm path (EventBridge
        # → SNS); the DLQ is the durable triage queue. Both fire by design.
        raise

    signed_payload = _build_signed_payload(
        manifest_digest=manifest_digest,
        attestation_digest=attestation_digest,
        attestation=attestation,
        repository_name=repository_name,
        registry_id=registry_id,
        region=region,
        provider=provider,
        signature_digests=signature_digests,
        # Whole-invocation short-circuit only when nothing fresh was signed.
        idempotent_short_circuit=not any_fresh_sign,
        config_blob=config_blob,
        detail=detail,
        resource_reference=resource_reference,
    )
    emit_signed(signed_payload)
    return signed_payload


def _build_signed_payload(
    *,
    manifest_digest: str,
    attestation_digest: str | None,
    attestation: Any,
    repository_name: str,
    registry_id: str,
    region: str,
    provider: TimestampProvider,
    signature_digests: dict[str, str | None],
    idempotent_short_circuit: bool,
    config_blob: dict[str, Any],
    detail: dict[str, Any],
    resource_reference: Any,
) -> dict[str, Any]:
    """Assemble the ``artifact.signed`` event payload.

    ``signatureDigest`` is the content-signature digest (the top-level key
    archive/metadata consumers read). The ``signatures`` map carries the
    per-subject signature digests so the archive stage (which archives both
    objects) and metadata stage can record both. ``attestationDigest`` and
    ``attestation`` are carried forward so downstream stages don't re-derive
    them.
    """
    signatures = {
        "content": {
            "subjectDigest": manifest_digest,
            "signatureDigest": signature_digests.get("content"),
        }
    }
    if attestation_digest:
        signatures["attestation"] = {
            "subjectDigest": attestation_digest,
            "signatureDigest": signature_digests.get("attestation"),
        }

    payload: dict[str, Any] = {
        "manifestDigest": manifest_digest,
        "repositoryName": repository_name,
        "registryId": registry_id,
        "region": region,
        # Content-manifest signature digest; the top-level key
        # archive/metadata consumers read.
        "signatureDigest": signature_digests.get("content"),
        "signatures": signatures,
        "signingProfileArn": SIGNER_PROFILE_ARN,
        "timestampProvider": provider.value,
        "signedAt": datetime.now(UTC).isoformat(),
        "idempotentShortCircuit": idempotent_short_circuit,
        "configBlob": config_blob,
        "authenticatedPushPrincipal": detail.get("authenticatedPushPrincipal"),
        "ecrPushedAt": detail.get("ecrPushedAt"),
    }
    if attestation_digest:
        payload["attestationDigest"] = attestation_digest
        payload["attestationSignatureDigest"] = signature_digests.get("attestation")
    if attestation is not None:
        payload["attestation"] = attestation
    if resource_reference is not None:
        payload["artifactResourceReference"] = resource_reference
    return payload


@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", None)
    start = time.perf_counter()
    with observability_scope(event, request_id=request_id, event_type="sign"):
        try:
            payload = _handle(event)
            metrics.add_metric(name="Success", unit=MetricUnit.Count, value=1)
            return payload
        except Exception:
            metrics.add_metric(name="Failure", unit=MetricUnit.Count, value=1)
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            metrics.add_metric(name="DurationMs", unit=MetricUnit.Milliseconds, value=elapsed_ms)
