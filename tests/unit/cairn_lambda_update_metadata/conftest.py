"""Shared fixtures for update-metadata Lambda tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from moto import mock_aws

ACCOUNT_ID = "123456789012"
REGION = "us-east-2"
STAGE = "alpha"
REGION_CODE = "cmh"
ORG_ID = "org-01JABCDORG00000000000000"
TENANT_ID = "tnt-01JABCD2EF3GH4JK5MN6PQ7RS8"
PROJECT_ID = "alpha-bank"
REPO_NAME = f"artifact-{REGION_CODE}-{STAGE}/{ORG_ID}/{TENANT_ID}/{PROJECT_ID}"
METADATA_TABLE_NAME = "cairnproofs4af-ArtifactMetadata-cmh-alpha"
CONTROLS_TABLE_NAME = "Cairn-Controls-cmh-alpha"
CATALOG_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def manifest_digest(filler: str = "a") -> str:
    return "sha256:" + (filler * 64)[:64]


def row_key(md: str, *, project_id: str = PROJECT_ID, tenant_id: str = TENANT_ID) -> dict[str, str]:
    """The tenant-PK Metadata_Index key for a bundle ([META-001]):
    ``pk = {tenant-id}``, ``sk = {project-id}#{manifest-digest}``."""
    return {"pk": tenant_id, "sk": f"{project_id}#{md}"}


def control_row_key(
    md: str,
    control_id: str,
    *,
    tenant_id: str = TENANT_ID,
    catalog_or_profile_uuid: str = CATALOG_UUID,
) -> dict[str, str]:
    """The tenant-PK ControlClaims key for one (control, bundle) row
    ([PCLAIM-001] / [META-001]): ``pk = {tenant-id}``,
    ``sk = {catalogOrProfileUuid}#{controlId}#{manifest-digest}``."""
    return {"pk": tenant_id, "sk": f"{catalog_or_profile_uuid}#{control_id}#{md}"}


def _attestation(
    *,
    project_id: str = PROJECT_ID,
    tenant_id: str = TENANT_ID,
    org_id: str = ORG_ID,
    artifact_kind: str = "assessment-results",
    controls: list[str] | None = None,
    catalog_or_profile_uuid: str | None = CATALOG_UUID,
    assessment_period: str | None = "2026-Q2",
) -> dict[str, Any]:
    """The Producer_Attestation_Referrer claim projection carried on the event.

    Carries the opaque identity triple ``orgId`` + ``tenantId`` + ``projectId``
    ([INGEST-005]); control linkage feeds the control-claims fan-out
    ([PCLAIM-001]). ``exclude_none``-style: omit ``controls`` for an explicitly
    empty list to model "no controls"."""
    attestation: dict[str, Any] = {
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "artifactKind": artifact_kind,
    }
    if assessment_period is not None:
        attestation["assessmentPeriod"] = assessment_period
    _controls = controls if controls is not None else ["ac-1", "ac-2"]
    if _controls:
        attestation["controls"] = _controls
    if catalog_or_profile_uuid is not None:
        attestation["catalogOrProfileUuid"] = catalog_or_profile_uuid
    return attestation


def validated_event(
    *,
    md: str | None = None,
    project_id: str = PROJECT_ID,
    tenant_id: str = TENANT_ID,
    org_id: str = ORG_ID,
    controls: list[str] | None = None,
    catalog_or_profile_uuid: str | None = CATALOG_UUID,
    event_time: str = "2026-05-26T12:05:00Z",
    repo_name: str = REPO_NAME,
    layers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    # configBlob carries content-descriptive fields only; the producer's
    # compliance claim travels in the signed Attestation referrer, projected
    # onto the event under `attestation` (camelCase, exclude_none). The
    # Metadata_Index record holds only system-bounded facts; the claim's control
    # linkage feeds the producer-claims control-claims projection ([PCLAIM-001]).
    # Attestation referrer claim projection — the source for the identity triple
    # AND the control-claims fan-out ([PCLAIM-001]).
    attestation = _attestation(
        project_id=project_id,
        tenant_id=tenant_id,
        org_id=org_id,
        controls=controls,
        catalog_or_profile_uuid=catalog_or_profile_uuid,
    )
    return {
        "version": "0",
        "id": "evt-validated-1",
        "detail-type": "artifact.validated",
        "source": "dev.cairnproofs.artifact",
        "account": ACCOUNT_ID,
        "time": event_time,
        "region": REGION,
        "detail": {
            "manifestDigest": md_value,
            "attestationDigest": "sha256:" + "d" * 64,
            "repositoryName": repo_name,
            "registryId": ACCOUNT_ID,
            "region": REGION,
            "ecrPushedAt": event_time,
            "temporalConsistencyWarning": False,
            "configBlob": {},
            "attestation": attestation,
            "manifestAnnotations": {"dev.cairnproofs.artifact.project-id": project_id},
            "authenticatedPushPrincipal": {
                "arn": f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/EvidencePushRole/s",
                "type": "AssumedRole",
                "accountId": ACCOUNT_ID,
                "userName": "EvidencePushRole",
                "mfaAuthenticated": True,
                "principalId": "AROAEXAMPLE:s",
            },
            "layers": layers if layers is not None else [],
        },
    }


def oscal_layer(
    *,
    schema_version: str = "1.2.1",
    media_type: str = "application/oscal.assessment-results+json",
    layer_digest: str | None = None,
) -> dict[str, Any]:
    """A unified ``layers`` event entry for an OSCAL layer as emitted by validate's
    ``_process_layers_and_build_reference`` — a universal OCI descriptor carrying an
    ``oscal`` enrichment sub-object with the content-extracted schema version."""
    return {
        "digest": layer_digest or ("sha256:" + "e" * 64),
        "size": 123,
        "mediaType": media_type,
        "title": "doc.json",
        "oscal": {
            "oscalSchemaVersion": schema_version,
            "metadata": {},
        },
    }


def non_oscal_layer(
    *,
    media_type: str = "text/plain",
    layer_digest: str | None = None,
    title: str = "evidence.log",
) -> dict[str, Any]:
    """A unified ``layers`` event entry for a non-OSCAL layer — descriptor only, no
    ``oscal`` enrichment sub-object."""
    return {
        "digest": layer_digest or ("sha256:" + "c" * 64),
        "size": 42,
        "mediaType": media_type,
        "title": title,
    }


def signed_event(
    *,
    md: str | None = None,
    project_id: str = PROJECT_ID,
    event_time: str = "2026-05-26T12:08:00Z",
    attestation_signature_digest: str | None = None,
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    detail: dict[str, Any] = {
        "manifestDigest": md_value,
        "signatureDigest": "sha256:" + "5" * 64,
        "signedAt": event_time,
        "signingProfileArn": (f"arn:aws:signer:{REGION}:{ACCOUNT_ID}:/signing-profiles/p"),
        "timestampProvider": "system-clock",
        "configBlob": {},
        # The claim projection (carrying the identity triple) is threaded through
        # every post-validated event so the tenant-PK row stays addressable.
        "attestation": _attestation(project_id=project_id),
    }
    # The second (Attestation referrer) signature's own digest — projected
    # alongside the content signature so the index reflects BOTH signed referrers.
    if attestation_signature_digest is not None:
        detail["attestationSignatureDigest"] = attestation_signature_digest
    return {
        "detail-type": "artifact.signed",
        "source": "dev.cairnproofs.artifact",
        "time": event_time,
        "account": ACCOUNT_ID,
        "region": REGION,
        "detail": detail,
    }


def signing_failed_event(
    *,
    md: str | None = None,
    project_id: str = PROJECT_ID,
    event_time: str = "2026-05-26T12:07:00Z",
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    return {
        "detail-type": "artifact.signing-failed",
        "source": "dev.cairnproofs.artifact",
        "time": event_time,
        "account": ACCOUNT_ID,
        "region": REGION,
        "detail": {
            "manifestDigest": md_value,
            "signingError": "notation exited 2: access denied",
            "configBlob": {},
            "attestation": _attestation(project_id=project_id),
        },
    }


def archived_event(
    *,
    md: str | None = None,
    project_id: str = PROJECT_ID,
    event_time: str = "2026-05-26T12:12:00Z",
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    return {
        "detail-type": "artifact.archived",
        "source": "dev.cairnproofs.artifact",
        "time": event_time,
        "account": ACCOUNT_ID,
        "region": REGION,
        "detail": {
            "manifestDigest": md_value,
            "s3Bucket": "cairn-archive-cmh-alpha",
            "s3KeyPrefix": f"{ORG_ID}/{TENANT_ID}/{project_id}/{md_value}/",
            "wormReceiptDigest": "sha256:" + "9" * 64,
            "wormEnabled": False,
            "archivedAt": event_time,
            "configBlob": {},
            "attestation": _attestation(project_id=project_id),
        },
    }


def archive_failed_event(
    *,
    md: str | None = None,
    project_id: str = PROJECT_ID,
    event_time: str = "2026-05-26T12:11:00Z",
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    return {
        "detail-type": "artifact.archive-failed",
        "source": "dev.cairnproofs.artifact",
        "time": event_time,
        "account": ACCOUNT_ID,
        "region": REGION,
        "detail": {
            "manifestDigest": md_value,
            "computedDigest": "sha256:" + "b" * 64,
            "expectedDigest": "sha256:" + "a" * 64,
            "artifact": "config",
            "configBlob": {},
            "attestation": _attestation(project_id=project_id),
        },
    }


def validation_failed_event(
    *, md: str | None = None, event_time: str = "2026-05-26T12:04:00Z"
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    return {
        "detail-type": "artifact.validation-failed",
        "source": "dev.cairnproofs.artifact",
        "time": event_time,
        "account": ACCOUNT_ID,
        "region": REGION,
        "detail": {
            "manifestDigest": md_value,
            "errorKind": "parse_failure",
            "failedCriteria": [{"criterion": "oscal", "field": "layer", "message": "x"}],
            "repositoryName": REPO_NAME,
        },
    }


@dataclass
class LambdaContext:
    aws_request_id: str = "req-1"
    function_name: str = "update-metadata"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:update-metadata"


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("METADATA_TABLE_NAME", METADATA_TABLE_NAME)
    monkeypatch.setenv("CONTROLS_TABLE_NAME", CONTROLS_TABLE_NAME)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")


@pytest.fixture
def tables(env: None, aws_region: str) -> Iterator[dict[str, Any]]:
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=aws_region)
        ddb.create_table(
            TableName=METADATA_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName=CONTROLS_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {
            "metadata": ddb.Table(METADATA_TABLE_NAME),
            "controls": ddb.Table(CONTROLS_TABLE_NAME),
        }


@pytest.fixture
def reload_handler(tables: dict[str, Any]) -> Iterator[Any]:
    """Reload the update-metadata handler under an active moto-mocked DDB."""
    import importlib

    import cairn_lambda_update_metadata.handler as handler_module

    handler_module._INDEX = None
    handler_module._CONTROLS = None
    handler = importlib.reload(handler_module)
    handler._INDEX = None
    handler._CONTROLS = None
    handler._test_tables = tables
    yield handler
    handler_module._INDEX = None
    handler_module._CONTROLS = None
