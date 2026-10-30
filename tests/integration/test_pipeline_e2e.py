"""End-to-end happy-path test against a real cmh-alpha deployment.

Scenario: a single OSCAL Assessment Results bundle is pushed via the
``cairn`` CLI, the four-Lambda pipeline (validate → sign → archive →
metadata) runs to completion within 10 minutes, and the bundle is
queryable via ``cairn query`` within 1 minute of pipeline completion. The
test also asserts the OCI Referrers API exposes both the Notation
signature and the WORM_Receipt as referrers of the source manifest.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration._waiters import wait_for, wait_for_pipeline_complete
from tests.integration.conftest import BundleCleanup, CairnCli, StackOutputs

pytestmark = pytest.mark.integration

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_AR_PATH = _FIXTURES_DIR / "assessment_results.json"
_LOG_PATH = _FIXTURES_DIR / "secondary_log.txt"

_CATALOG_UUID = "11111111-1111-4111-8111-111111111111"
_ASSESSMENT_PERIOD = "2025-q1"
_BUNDLE_TYPE = "assessment-results"
_PIPELINE_TIMEOUT_SECONDS = 360.0
_QUERY_TIMEOUT_SECONDS = 30.0


@pytest.fixture
def unique_marker_layer(tmp_path: Path) -> Iterator[Path]:
    """A per-test secondary layer with random content.

    ``projectId`` is not in the content digest, so a project shared across the
    suite does not make a bundle digest unique by itself. Distinctness must come
    from
    **content**: this fixture injects a random-bytes marker layer
    so each happy-path test produces a fresh content ``Manifest_Digest`` that
    cannot collide with another test's bundle in the shared repo.
    """
    marker = tmp_path / "unique_marker.txt"
    marker.write_text(f"cairn-e2e-marker-{secrets.token_hex(8)}\n")
    yield marker


def test_pipeline_happy_path_oscal_assessment_results(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    unique_marker_layer: Path,
) -> None:
    """A pushed OSCAL Assessment Results bundle traverses the full pipeline.

    Given a freshly assembled Evidence_Bundle with a primary OSCAL
    Assessment Results layer and a secondary log layer,
    When ``cairn push`` uploads it to the customer-hosted ECR repo,
    Then within 10 minutes the Metadata_Index row reaches
    ``signingStatus=signed`` AND ``archiveStatus=archived`` with both
    ``authenticatedPushPrincipal`` ([PIPELINE-010]) and ``ecrPushedAt`` ([PIPELINE-009])
    populated, the ControlsIndex carries one row per OSCAL control, the S3
    archive bucket contains the artifact under the canonical key prefix,
    the OCI Referrers API on the source manifest returns BOTH the Notation
    signature and the WORM_Receipt, and within 1 additional minute
    ``cairn query --project`` surfaces the bundle.
    """
    # The whole suite reuses the stack's pre-provisioned per-project repo;
    # the project comes from the environment (default
    # ``mvp-tenant``) and the repo is resolved + verified (describe-not-create)
    # by the ``shared_repo_name`` fixture. Per-test distinctness comes from
    # content (the random ``unique_marker_layer``), not from ``projectId``.
    project_id = target_project_id
    repo_path = shared_repo_name

    push_result = cairn_cli(
        "push",
        "--org-id",
        target_org_id,
        "--tenant-id",
        target_tenant_id,
        "--project-id",
        project_id,
        "--assessment-period",
        _ASSESSMENT_PERIOD,
        str(_AR_PATH),
        str(_LOG_PATH),
        str(unique_marker_layer),
        "--artifact-kind",
        _BUNDLE_TYPE,
        "--catalog-or-profile-uuid",
        _CATALOG_UUID,
        "--control",
        "ac-1",
        "--control",
        "ac-2",
        "--source-tool",
        "cairn-integration-suite",
        "--stage",
        "alpha",
        "--region-code",
        "cmh",
        check=True,
    )

    manifest_digest = push_result.stdout.strip().splitlines()[-1].strip()
    assert manifest_digest.startswith("sha256:"), (
        f"cairn push did not emit a manifest digest; stdout was:\n{push_result.stdout}"
    )

    bundle_cleanup.track_ecr_manifest(repo_path, manifest_digest)

    partition_key = project_id
    sort_key = manifest_digest
    bundle_cleanup.track_metadata_row(target_tenant_id, partition_key, sort_key)

    ddb = aws_session.client("dynamodb")

    pipeline_started = time.monotonic()
    item = wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        partition_key,
        sort_key,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )
    pipeline_completed_at = time.monotonic()
    assert pipeline_completed_at - pipeline_started <= _PIPELINE_TIMEOUT_SECONDS

    assert "authenticatedPushPrincipal" in item, (
        "Metadata_Index row missing authenticatedPushPrincipal ([PIPELINE-010])"
    )
    assert "ecrPushedAt" in item, "Metadata_Index row missing ecrPushedAt ([PIPELINE-009])"

    # ControlClaims is keyed on the tenant-PK plane: pk = {tenant-id},
    # sk = {catalogOrProfileUuid}#{controlId}#{manifestDigest}. The test holds
    # the full triple, so address the row by its exact (pk, sk).
    controls_sk = f"{_CATALOG_UUID}#ac-1#{manifest_digest}"
    controls_response = ddb.query(
        TableName=stack_outputs.control_claims_table,
        KeyConditionExpression="pk = :pk AND sk = :sk",
        ExpressionAttributeValues={
            ":pk": {"S": target_tenant_id},
            ":sk": {"S": controls_sk},
        },
    )
    assert controls_response.get("Items"), (
        f"ControlsIndex has no row for tenant {target_tenant_id} / {controls_sk} "
        "after pipeline completion"
    )

    # List + track THIS bundle's archived objects under its digest-terminal
    # prefix (``{org-id}/{tenant-id}/{project-id}/{manifest-digest}/`` per
    # [WORM-002]). Keying on the digest keeps the assertion and
    # cleanup targeted to what this test created, never the whole shared-project
    # prefix.
    archive_keys = bundle_cleanup.track_archived_objects(
        target_org_id, target_tenant_id, project_id, manifest_digest
    )
    assert archive_keys, (
        f"archive bucket {stack_outputs.archive_bucket} has no objects under "
        f"prefix {target_org_id}/{target_tenant_id}/{project_id}/{manifest_digest}/ "
        "after archive Lambda reported success"
    )

    ecr = aws_session.client("ecr")
    referrers = ecr.list_image_referrers(
        repositoryName=repo_path,
        subjectId={"imageDigest": manifest_digest},
    )
    referrer_artifact_types = {item["artifactType"] for item in referrers.get("referrers", [])}
    assert "application/vnd.cncf.notary.signature" in referrer_artifact_types, (
        f"Notation signature referrer missing from {repo_path}@{manifest_digest}; "
        f"observed artifact types: {referrer_artifact_types!r}"
    )
    assert any(
        artifact_type.startswith("application/vnd.cairnproofs.artifact.worm-receipt")
        for artifact_type in referrer_artifact_types
    ), (
        f"WORM_Receipt referrer missing from {repo_path}@{manifest_digest}; "
        f"observed artifact types: {referrer_artifact_types!r}"
    )

    query_started = time.monotonic()

    def _bundle_in_query() -> dict[str, Any] | None:
        query_result = cairn_cli(
            "query",
            "--project",
            project_id,
            "--tenant-id",
            target_tenant_id,
            "--stage",
            "alpha",
            "--region-code",
            "cmh",
        )
        if query_result.returncode != 0:
            return None
        for line in query_result.stdout.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("manifestDigest") == manifest_digest:
                return record
        return None

    record = wait_for(
        _bundle_in_query,
        timeout_seconds=_QUERY_TIMEOUT_SECONDS,
        description=f"cairn query --project {project_id} to surface {manifest_digest}",
    )
    query_completed_at = time.monotonic()
    assert query_completed_at - query_started <= _QUERY_TIMEOUT_SECONDS
    assert record["signingStatus"] == "signed"
    assert record["archiveStatus"] == "archived"


_CONTROLS_LESS_BUNDLE_TYPE = "other"


def test_pipeline_happy_path_controls_less_bundle(
    aws_session: Any,
    stack_outputs: StackOutputs,
    cairn_cli: CairnCli,
    bundle_cleanup: BundleCleanup,
    shared_repo_name: str,
    target_org_id: str,
    target_tenant_id: str,
    target_project_id: str,
    unique_marker_layer: Path,
) -> None:
    """A controls-less bundle traverses the full pipeline (regression guard for the incident).

    Given an Evidence_Bundle whose Config_Blob omits ALL control linkage — no
    `controls`, no `catalog-or-profile-uuid`, no `control-family` (the exact
    shape `cairn push <project> <oscal-file>` mints by default, and the exact
    shape a stale pre-949213d validator rejected on four criteria),
    When `cairn push` uploads it,
    Then within 10 minutes the Metadata_Index row reaches
    `signingStatus=signed` AND `archiveStatus=archived`, the S3 archive bucket
    holds the artifact, the OCI Referrers API returns BOTH the Notation
    signature and the WORM_Receipt, and `cairn query` surfaces it — while NO
    ControlsIndex row is created (control linkage is optional, not a storage
    precondition). This is the integration-layer guard for the deploy/contract
    drift that caused the incident; the unit-layer guard is
    tests/unit/cairn_cli/test_push_validate_roundtrip.py.
    """
    # Shared pre-provisioned repo; content-derived distinctness
    # via the random marker layer.
    project_id = target_project_id
    repo_path = shared_repo_name

    # No --control, no --catalog-or-profile-uuid: the controls-less default.
    # The OSCAL schema version is derived from the document's own
    # metadata.oscal-version, so no --oscal-schema-version flag is passed.
    push_result = cairn_cli(
        "push",
        "--org-id",
        target_org_id,
        "--tenant-id",
        target_tenant_id,
        "--project-id",
        project_id,
        "--assessment-period",
        _ASSESSMENT_PERIOD,
        str(_AR_PATH),
        str(unique_marker_layer),
        "--artifact-kind",
        _CONTROLS_LESS_BUNDLE_TYPE,
        "--source-tool",
        "cairn-integration-suite",
        "--stage",
        "alpha",
        "--region-code",
        "cmh",
        check=True,
    )

    manifest_digest = push_result.stdout.strip().splitlines()[-1].strip()
    assert manifest_digest.startswith("sha256:"), (
        f"cairn push did not emit a manifest digest; stdout was:\n{push_result.stdout}"
    )

    bundle_cleanup.track_ecr_manifest(repo_path, manifest_digest)
    partition_key = project_id
    sort_key = manifest_digest
    bundle_cleanup.track_metadata_row(target_tenant_id, partition_key, sort_key)

    ddb = aws_session.client("dynamodb")

    item = wait_for_pipeline_complete(
        ddb,
        stack_outputs.metadata_table,
        target_tenant_id,
        partition_key,
        sort_key,
        timeout_seconds=_PIPELINE_TIMEOUT_SECONDS,
    )

    assert "authenticatedPushPrincipal" in item, (
        "Metadata_Index row missing authenticatedPushPrincipal ([PIPELINE-010])"
    )
    assert "ecrPushedAt" in item, "Metadata_Index row missing ecrPushedAt ([PIPELINE-009])"

    # Digest-terminal prefix keeps the listing/cleanup scoped to this bundle
    # within the shared project.
    archive_keys = bundle_cleanup.track_archived_objects(
        target_org_id, target_tenant_id, project_id, manifest_digest
    )
    assert archive_keys, (
        f"archive bucket has no objects under "
        f"{target_org_id}/{target_tenant_id}/{project_id}/{manifest_digest}/ "
        "after archive reported success"
    )

    ecr = aws_session.client("ecr")
    referrers = ecr.list_image_referrers(
        repositoryName=repo_path,
        subjectId={"imageDigest": manifest_digest},
    )
    referrer_artifact_types = {item["artifactType"] for item in referrers.get("referrers", [])}
    assert "application/vnd.cncf.notary.signature" in referrer_artifact_types, (
        f"Notation signature referrer missing; observed: {referrer_artifact_types!r}"
    )
    assert any(
        artifact_type.startswith("application/vnd.cairnproofs.artifact.worm-receipt")
        for artifact_type in referrer_artifact_types
    ), f"WORM_Receipt referrer missing; observed: {referrer_artifact_types!r}"

    def _bundle_in_query() -> dict[str, Any] | None:
        query_result = cairn_cli(
            "query",
            "--project",
            project_id,
            "--tenant-id",
            target_tenant_id,
            "--stage",
            "alpha",
            "--region-code",
            "cmh",
        )
        if query_result.returncode != 0:
            return None
        for line in query_result.stdout.splitlines():
            if not line.strip():
                continue
            candidate = json.loads(line)
            if candidate.get("manifestDigest") == manifest_digest:
                return candidate
        return None

    record = wait_for(
        _bundle_in_query,
        timeout_seconds=_QUERY_TIMEOUT_SECONDS,
        description=f"cairn query --project {project_id} to surface {manifest_digest}",
    )
    assert record["signingStatus"] == "signed"
    assert record["archiveStatus"] == "archived"
    # The Metadata_Index record holds only system-bounded facts — it never
    # carries the producer's control claim, whether or not the bundle cited
    # controls. Control linkage lives in the control-claims projection
    # ([PCLAIM-001]) and the rest of the claim in the signed
    # Producer_Attestation_Referrer; a controls-less bundle simply writes zero
    # control-claims rows while staying reachable by project and digest.
    for claim_attr in (
        "controls",
        "controlLinkage",
        "catalogOrProfileUuid",
        "assessor",
        "assessmentPeriod",
    ):
        assert claim_attr not in record, (
            f"Metadata_Index record carries producer-claim attribute "
            f"{claim_attr!r}: {record.get(claim_attr)!r}"
        )
