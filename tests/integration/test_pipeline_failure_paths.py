"""Failure-path integration tests against a real cmh-alpha deployment.

One test per failure mode. Each test pushes a deliberately broken bundle
directly via ``oras`` (bypassing the CLI's client-side validation) and
asserts the expected downstream signal — validate-DLQ message,
``artifact.validation-failed`` audit event, or integrity-mismatch alarm
transitioning to ALARM.

validate-bundle deposits the
rejection on the validate-DLQ **in-band** at classification time ([PIPELINE-006] —
no Lambda async-invoke retry). The DLQ message body is therefore the JSON
rejection ``detail`` the handler builds — ``manifestDigest`` +
``repositoryName`` + bounded ``failedCriteria`` (each a
``{criterion, field, message}`` entry) + an optional ``errorKind`` — NOT the
raw ECR ``PutImage`` envelope a Lambda async dead-letter destination would
deposit. Delivery is synchronous with the permanent classification, so the
existing 240 s assertion window is comfortably met (no async-retry backoff).

The DLQ assertions inspect that structured message body — matching the
``manifestDigest`` field and reading the ``failedCriteria`` reason text —
not just queue depth, so a regression that routes a bundle to the wrong DLQ
(or strips the reason) surfaces as a content mismatch rather than a silent
pass.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

import pytest
from cairn_oci import CairnAnnotations
from cairn_oci.config_blob import CONFIG_BLOB_MEDIA_TYPE

from tests.integration._bundle import (
    build_config_blob_bytes,
    ecr_login,
    oras_push_raw,
)
from tests.integration._waiters import (
    drain_dlq_messages,
    fetch_metadata_row,
    wait_for,
)
from tests.integration.conftest import BundleCleanup, StackOutputs

# validate-bundle sends the rejection in-band to the validate-DLQ at
# classification time ([PIPELINE-006]), so all three permanent-failure classes
# (missing-required-annotation, malformed-OSCAL, project-id-mismatch) surface a
# reason-bearing message well within the 240 s window — delivered synchronously,
# not via Lambda async-invoke retries. The cross-check exemplar is a project-id
# mismatch: [PIPELINE-013] requires the claim's org, tenant, and project to each
# exact-match the corresponding ECR repository path segment, uniformly for every
# bundle, so a mismatched project-id routes to the DLQ. The DLQ is the canonical
# rejection store.
pytestmark = [
    pytest.mark.integration,
    # DISABLED pending failure-path rework against the attestation-referrer trigger
    # model. validate-bundle's single positive trigger is a
    # Producer_Attestation_Referrer push; a bare content
    # manifest is skipped as an incomplete submission and never validated. These
    # tests push only a broken content manifest via `oras_push_raw` (no referrer),
    # so the broken bundle is never promoted → never rejected → no DLQ message
    # (240 s timeout). Reworking them also requires re-targeting the project-id
    # mismatch onto the *referrer claim* (projectId moved out of the content
    # manifest into the claim). Tracked with the validate-stage claim-rule work
    # in the backlog (same enforcement point: validate-stage Producer_Attestation_Referrer
    # claim checks).
    pytest.mark.skip(
        reason=(
            "failure-path tests push a bare content manifest (no Producer_Attestation_Referrer); "
            "validate only triggers on a referrer push, "
            "so these are never validated/rejected. Rework tracked in the "
            "backlog with the validate-stage claim-rule work (validate-stage "
            "Producer_Attestation_Referrer claim checks)."
        )
    ),
]

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_AR_PATH = _FIXTURES_DIR / "assessment_results.json"
_LOG_PATH = _FIXTURES_DIR / "secondary_log.txt"
_MALFORMED_AR_PATH = _FIXTURES_DIR / "malformed_assessment_results.json"

_CONFIG_BLOB_MEDIA_TYPE = CONFIG_BLOB_MEDIA_TYPE
_OSCAL_AR_MEDIA_TYPE = "application/oscal+json"
_LOG_MEDIA_TYPE = "text/plain"
_CATALOG_UUID = "22222222-2222-4222-8222-222222222222"
_ASSESSMENT_PERIOD = "2025-q1"
_BUNDLE_TYPE = "assessment-results"

_DLQ_TIMEOUT_SECONDS = 240.0
_ALARM_TIMEOUT_SECONDS = 240.0


def _unique_project_id(prefix: str) -> str:
    return f"int-{prefix}-{secrets.token_hex(4)}"


def _ecr_reference(stack: StackOutputs, repo_path: str) -> str:
    """Compose the TAGLESS push reference for the shared pre-provisioned repo.

    Mirrors ``cairn push`` (``cli/cairn_cli/push.py``): the client pushes BY
    DIGEST to a tagless ``{registry}/{repo}`` reference — the pipeline mints
    the canonical tag server-side, so the client
    never composes one. The evidence category (artifact-kind) is sourced from
    the Producer_Attestation_Referrer ``artifactKind`` claim, not from the
    content manifest or a client-supplied tag. The whole suite reuses the
    stack's pre-provisioned per-project repo; per-test content distinctness comes from the
    bundle's claim fields, not from a per-test repo.
    """
    return f"{stack.ecr_registry_host}/{repo_path}"


def _baseline_annotations(project_id: str) -> dict[str, str]:
    """An otherwise-valid manifest annotation set in the CURRENT wire format.

    Mirrors what ``cairn push`` emits (``cli/cairn_cli/push.py``): the
    ``dev.cairnproofs.artifact.*`` namespace, the identity floor
    (project-id / created) plus optional control + period linkage. There is
    intentionally NO ``artifact-kind`` annotation (kind is carried
    only by the Producer_Attestation_Referrer ``artifactKind`` claim), NO control-family
    annotation (families are a derived projection of the control ids), and NO
    catalog annotation (the catalog-or-profile UUID lives only in the
    Config_Blob). ``controls`` is the comma-joined string form.
    """
    return {
        CairnAnnotations.PROJECT_ID: project_id,
        CairnAnnotations.CREATED: "2026-05-26T12:00:00Z",
        CairnAnnotations.ASSESSMENT_PERIOD: _ASSESSMENT_PERIOD,
        CairnAnnotations.CONTROLS: "ac-1",
    }


def _baseline_config_blob(project_id: str) -> dict[str, Any]:
    """An otherwise-valid Config_Blob in the CURRENT schema (``ConfigBlob`` aliases).

    Field names match what ``cairn push`` serializes via
    ``ConfigBlob.model_dump_json(by_alias=True, exclude_none=True)``: no
    ``bundleType`` (kind is claim-only) and no ``controlFamily`` (not a schema field).
    """
    return {
        "projectId": project_id,
        "controls": ["ac-1"],
        "catalogOrProfileUuid": _CATALOG_UUID,
        "assessmentPeriod": _ASSESSMENT_PERIOD,
    }


def _push_broken_bundle(
    *,
    aws_session: Any,
    stack: StackOutputs,
    repo_path: str,
    config_blob: dict[str, Any],
    annotations: dict[str, str],
    primary_layer: tuple[Path, str],
    secondary_layer: tuple[Path, str] | None = None,
) -> tuple[str, str]:
    """Push a hand-crafted bundle to the shared pre-provisioned repo via oras.

    Returns ``(repo_path, manifest_digest)``. Describe-not-create: the repo
    must already exist (verified by the ``shared_repo_name`` fixture); this
    helper never creates it.
    """
    ecr = aws_session.client("ecr")
    reference = _ecr_reference(stack, repo_path)
    ecr_login(ecr, stack.ecr_registry_host)

    config_bytes = build_config_blob_bytes(config_blob)
    with tempfile.TemporaryDirectory(prefix="cairn-integration-") as raw_tmpdir:
        tmpdir = Path(raw_tmpdir)
        config_path = tmpdir / "config.json"
        config_path.write_bytes(config_bytes)
        manifest_export = tmpdir / "manifest.json"
        layers: list[tuple[Path, str]] = [primary_layer]
        if secondary_layer is not None:
            layers.append(secondary_layer)
        manifest_digest = oras_push_raw(
            reference=reference,
            config_path=config_path,
            config_media_type=_CONFIG_BLOB_MEDIA_TYPE,
            layers=layers,
            annotations=annotations,
            manifest_export_path=manifest_export,
        )
    return repo_path, manifest_digest


def _wait_for_dlq_message_matching(
    sqs_client: Any,
    queue_url: str,
    *,
    manifest_digest: str,
    timeout_seconds: float,
    description: str,
) -> dict[str, Any]:
    def _probe() -> dict[str, Any] | None:
        for message in drain_dlq_messages(sqs_client, queue_url):
            if _message_references_digest(message, manifest_digest):
                return message
        return None

    return wait_for(_probe, timeout_seconds=timeout_seconds, description=description)


def _message_references_digest(message: dict[str, Any], manifest_digest: str) -> bool:
    """True if the in-band rejection body identifies ``manifest_digest``.

    The DLQ body is now the handler's rejection ``detail`` (design
    ``permanent-failure-dlq-no-retry``), so the digest lives in the structured
    ``manifestDigest`` field. Prefer that exact field match; fall back to a
    serialized-substring scan so a transitional message (or a body whose digest
    rides a nested field) still matches rather than silently timing out.
    """
    if message.get("manifestDigest") == manifest_digest:
        return True
    serialized = json.dumps(message, default=str)
    return manifest_digest in serialized


def _rejection_reason_text(message: dict[str, Any]) -> str:
    """Lower-cased reason text of a rejection body for keyword assertions.

    Reads the structured rejection fields the handler emits — ``errorKind`` plus
    every ``failedCriteria`` entry's ``criterion``/``field``/``message`` — so the
    content assertions key on the failure reason rather than on incidental
    serialization. Falls back to the whole serialized body if those structured
    fields are absent (defensive against an unexpected envelope shape).
    """
    parts: list[str] = []
    error_kind = message.get("errorKind")
    if isinstance(error_kind, str):
        parts.append(error_kind)
    failed_criteria = message.get("failedCriteria")
    if isinstance(failed_criteria, list):
        for entry in failed_criteria:
            if isinstance(entry, dict):
                for key in ("criterion", "field", "message"):
                    value = entry.get(key)
                    if isinstance(value, str):
                        parts.append(value)
    if not parts:
        parts.append(json.dumps(message, default=str))
    return " ".join(parts).lower()


def test_missing_required_annotation_routes_to_validate_dlq(
    aws_session: Any,
    stack_outputs: StackOutputs,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_project_id: str,
) -> None:
    """A bundle missing the ``project-id`` manifest annotation routes to validate-DLQ.

    Given an Evidence_Bundle pushed without the required
    ``dev.cairnproofs.artifact.project-id`` annotation,
    When the ECR PUSH event reaches validate-bundle,
    Then validate-bundle emits ``artifact.validation-failed`` and deposits
    an in-band rejection on the validate-bundle DLQ at classification time
    ([PIPELINE-006] — no async-invoke retry) carrying the offending manifest digest
    and the failing-criteria reason, sign-bundle is never invoked, and no
    Metadata_Index row reaches ``signingStatus=signed``.
    """
    # Push into the shared project's repo with a matching Config_Blob
    # ``projectId`` so the repo-path cross-check passes — the ONLY failure is
    # the missing ``project-id`` manifest annotation.
    project_id = target_project_id
    annotations = _baseline_annotations(project_id)
    annotations.pop(CairnAnnotations.PROJECT_ID)
    config_blob = _baseline_config_blob(project_id)

    repo_path, manifest_digest = _push_broken_bundle(
        aws_session=aws_session,
        stack=stack_outputs,
        repo_path=shared_repo_name,
        config_blob=config_blob,
        annotations=annotations,
        primary_layer=(_AR_PATH, _OSCAL_AR_MEDIA_TYPE),
        secondary_layer=(_LOG_PATH, _LOG_MEDIA_TYPE),
    )
    bundle_cleanup.track_ecr_manifest(repo_path, manifest_digest)

    sqs = aws_session.client("sqs")
    dlq_message = _wait_for_dlq_message_matching(
        sqs,
        stack_outputs.validate_bundle_dlq_url,
        manifest_digest=manifest_digest,
        timeout_seconds=_DLQ_TIMEOUT_SECONDS,
        description=(
            f"validate-bundle DLQ to surface a message referencing "
            f"{manifest_digest} (missing project-id annotation)"
        ),
    )
    reason = _rejection_reason_text(dlq_message)
    assert "annotation" in reason or "project-id" in reason or "validation" in reason, (
        f"validate-DLQ rejection did not mention annotation/project-id/validation: {dlq_message!r}"
    )

    ddb = aws_session.client("dynamodb")
    item = fetch_metadata_row(ddb, stack_outputs.metadata_table, project_id, manifest_digest)
    assert item is None or item.get("signingStatus", {}).get("S") != "signed", (
        "Metadata_Index row reached signingStatus=signed for a bundle that "
        "should have failed validation"
    )


def test_malformed_oscal_routes_to_validate_dlq(
    aws_session: Any,
    stack_outputs: StackOutputs,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_project_id: str,
) -> None:
    """A bundle whose OSCAL AR layer is structurally invalid routes to validate-DLQ.

    Given an Evidence_Bundle whose ``application/oscal+json`` assessment-results
    layer fails OSCAL JSON Schema validation,
    When validate-bundle parses the bundle,
    Then the OSCAL parse error is reported via ``artifact.validation-failed``
    and an in-band rejection lands on the validate-DLQ at classification time
    ([PIPELINE-006]) carrying the offending manifest digest and the OSCAL failure
    reason.
    """
    # Matching project so the repo-path cross-check passes — the ONLY failure
    # is the malformed OSCAL layer.
    project_id = target_project_id
    annotations = _baseline_annotations(project_id)
    config_blob = _baseline_config_blob(project_id)

    repo_path, manifest_digest = _push_broken_bundle(
        aws_session=aws_session,
        stack=stack_outputs,
        repo_path=shared_repo_name,
        config_blob=config_blob,
        annotations=annotations,
        primary_layer=(_MALFORMED_AR_PATH, _OSCAL_AR_MEDIA_TYPE),
    )
    bundle_cleanup.track_ecr_manifest(repo_path, manifest_digest)

    sqs = aws_session.client("sqs")
    dlq_message = _wait_for_dlq_message_matching(
        sqs,
        stack_outputs.validate_bundle_dlq_url,
        manifest_digest=manifest_digest,
        timeout_seconds=_DLQ_TIMEOUT_SECONDS,
        description=(
            f"validate-bundle DLQ to surface malformed-OSCAL failure for {manifest_digest}"
        ),
    )
    reason = _rejection_reason_text(dlq_message)
    assert "oscal" in reason or "schema" in reason or "validation" in reason, (
        f"validate-DLQ rejection did not mention OSCAL/schema/validation: {dlq_message!r}"
    )


def test_project_id_mismatch_routes_to_validate_dlq(
    aws_session: Any,
    stack_outputs: StackOutputs,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_project_id: str,
) -> None:
    """A Config_Blob ``projectId`` disagreeing with the ECR repo path routes to validate-DLQ.

    Given a bundle pushed into the shared pre-provisioned repository (whose
    path segment is the target project) but whose signed claim asserts a
    DIFFERENT, bogus project-id ([PIPELINE-013]),
    When validate-bundle cross-checks the claim against the ECR
    repository path segment,
    Then validation fails and an in-band rejection lands on the validate-DLQ
    at classification time ([PIPELINE-006]) carrying the offending manifest digest
    and the project-id-mismatch reason.

    [PIPELINE-013] requires the claim's org, tenant, and project to each
    exact-match the corresponding ECR repository path segment, uniformly for
    every bundle, so a mismatched project-id exercises the
    "claim/path cross-check failure → in-band DLQ" path against this stack.

    Under the shared-repo harness the mismatch is expressed
    *without a second repo*: push into the shared project's repo, but stamp a
    distinct bogus ``projectId`` in BOTH the annotation and the Config_Blob
    claim, so the repo-path-vs-claim cross-check still fails exactly as before.
    """
    # The bundle claims `claimed_project_id` in BOTH the annotation and the
    # Config_Blob (so they agree with each other), but it is pushed into the
    # SHARED repo whose path segment is `target_project_id` — so only the
    # ECR-path-vs-claim cross-check fails (no second repo).
    claimed_project_id = _unique_project_id("projid-mismatch-claimed")
    assert claimed_project_id != target_project_id
    annotations = _baseline_annotations(claimed_project_id)
    config_blob = _baseline_config_blob(claimed_project_id)

    repo_path, manifest_digest = _push_broken_bundle(
        aws_session=aws_session,
        stack=stack_outputs,
        repo_path=shared_repo_name,
        config_blob=config_blob,
        annotations=annotations,
        primary_layer=(_AR_PATH, _OSCAL_AR_MEDIA_TYPE),
        secondary_layer=(_LOG_PATH, _LOG_MEDIA_TYPE),
    )
    bundle_cleanup.track_ecr_manifest(repo_path, manifest_digest)

    sqs = aws_session.client("sqs")
    dlq_message = _wait_for_dlq_message_matching(
        sqs,
        stack_outputs.validate_bundle_dlq_url,
        manifest_digest=manifest_digest,
        timeout_seconds=_DLQ_TIMEOUT_SECONDS,
        description=(f"validate-bundle DLQ to surface project-id mismatch for {manifest_digest}"),
    )
    reason = _rejection_reason_text(dlq_message)
    assert "project" in reason or "mismatch" in reason or "validation" in reason, (
        f"validate-DLQ rejection did not mention project/mismatch/validation: {dlq_message!r}"
    )


# Note on "digest tampering → integrity-mismatch alarm fires":
#
# The IntegrityMismatch metric is emitted from archive_to_s3/handler.py only
# inside `except IntegrityError`, which is raised when ECR-fetched manifest,
# config-blob, or layer bytes hash to a value that disagrees with the digest
# declared in the manifest. ECR is content-addressed: blobs and manifests
# are stored keyed by their own SHA-256, so a fetch-by-digest can only
# return bytes whose SHA-256 matches the requested digest. There is no
# code path by which a black-box integration test can inject the mismatch
# this defense-in-depth check is guarding against — the failure mode would
# require an ECR-side corruption or MITM that AWS's content-addressing +
# SigV4 + TLS prevents.
#
# Coverage of the IntegrityError → IntegrityMismatch path lives at the
# unit-test layer where the ECR client can be stubbed to return tampered
# bytes against an unchanged manifest digest:
#   - tests/unit/cairn_lambda_archive_to_s3/test_handler.py
#       ::test_digest_mismatch_aborts_with_archive_failed
#       ::test_manifest_digest_mismatch_aborts
#       ::test_failure_and_integrity_mismatch_metric_on_digest_mismatch
#
# The CDK test suite separately asserts the integrity-mismatch CloudWatch
# alarm is provisioned and wired to the SNS topic.
#
# If a post-archive tamper-detection capability (an S3-event-driven
# re-verification listener that detects out-of-band overwrites of archived
# objects) is desired, that's a backlog item — the surface does not exist
# today. The earlier draft of this test attempted to simulate that
# capability and would always either time out or produce a false positive.
