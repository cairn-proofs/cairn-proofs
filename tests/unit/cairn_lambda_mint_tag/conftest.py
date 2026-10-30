"""Fixtures for the mint-tag Lambda tests.

Boots a moto DynamoDB Metadata_Index table, seeds a validate record
(artifactKind — the only record-sourced tag input; the digest segment comes from
the Manifest_Digest, and the canonical tag carries no producer free text),
reloads the handler with env set, and monkeypatches the ECR edge functions with
an in-memory fake so no real AWS calls happen.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

STAGE = "alpha"
REGION_CODE = "cmh"
METADATA_TABLE = "MetadataIndexMintTest"
EVENT_BUS_NAME = "cairn-pipeline-bus"

ORG_ID = "org-01JABCDORG00000000000000"
TENANT_ID = "tnt-01JABCD2EF3GH4JK5MN6PQ7RS8"
PROJECT_ID = "alpha-bank"
REGISTRY_ID = "123456789012"
REPOSITORY = f"artifact-cmh-alpha/{ORG_ID}/{TENANT_ID}/{PROJECT_ID}"
REGION = "us-east-2"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
MANIFEST_RAW = '{"mediaType":"application/vnd.oci.image.manifest.v1+json","layers":[]}'
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"


def row_key(digest: str = DIGEST, *, project_id: str = PROJECT_ID) -> dict[str, str]:
    """The tenant-PK Metadata_Index key ([META-001]):
    ``pk = {tenant-id}``, ``sk = {project-id}#{manifest-digest}``."""
    return {"pk": TENANT_ID, "sk": f"{project_id}#{digest}"}


# Canonical defaults for the single tag form {artifact-kind}--_ref--{digest}.
# DIGEST = "sha256:" + "a"*64, so its 12-char hex prefix is "aaaaaaaaaaaa".
# NAME is retained only as a free-text decoy: the lying-event T-02 test asserts a
# producer-asserted name is ignored, and the collision tests assert it never
# leaks into a failure message. It is NOT a tag segment anymore.
NAME = "nist-sp-800-53-rev-5"
DIGEST_PREFIX = "a" * 12


class LambdaContext:
    aws_request_id = "test-request-id"
    function_name = "mint-tag"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-2:123456789012:function:mint-tag"


class EventRecorder:
    """Stand-in for the boto3 events client; records put_events Entries."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def put_events(self, *, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class FakeEcr:
    """In-memory ECR: tracks tag -> digest and PutImage calls.

    Tag immutability is modelled: a put_image of a tag already mapped to a
    digest raises ImageTagAlreadyExistsException. ``digest_for_tag`` lets the
    handler distinguish a same-digest re-drive (idempotent no-op) from a
    cross-digest tag collision (fail-closed).
    """

    def __init__(self) -> None:
        # tag -> digest it currently points at
        self.tag_to_digest: dict[str, str] = {}
        self.put_image_calls: list[dict[str, Any]] = []
        # When set, the next put_image_tag raises this ClientError unconditionally.
        self.put_raises: Exception | None = None
        # When True, put_image_tag honours tag immutability (raises on an
        # existing tag) instead of overwriting.
        self.enforce_immutability = True

    def _pre_tag(self, tag: str, digest: str) -> None:
        """Seed a tag already pointing at ``digest`` (no PutImage recorded)."""
        self.tag_to_digest[tag] = digest

    def fetch_manifest(
        self, *, registry_id: str, repository_name: str, image_digest: str
    ) -> tuple[str, str]:
        return MANIFEST_RAW, MANIFEST_MEDIA_TYPE

    def tags_for_digest(
        self,
        *,
        registry_id: str,
        repository_name: str,
        image_digest: str,
    ) -> list[str]:
        return [tag for tag, dig in self.tag_to_digest.items() if dig == image_digest]

    def digest_for_tag(
        self,
        *,
        registry_id: str,
        repository_name: str,
        image_tag: str,
    ) -> str | None:
        return self.tag_to_digest.get(image_tag)

    def put_image_tag(
        self,
        *,
        registry_id: str,
        repository_name: str,
        manifest_raw: str,
        media_type: str,
        image_tag: str,
    ) -> str:
        # The handler addresses the digest it is minting for; the fixture mints
        # against DIGEST unless a test pre-points the tag elsewhere.
        self.put_image_calls.append({"imageTag": image_tag})
        if self.put_raises is not None:
            raise self.put_raises
        if self.enforce_immutability and image_tag in self.tag_to_digest:
            raise ClientError(
                error_response={
                    "Error": {
                        "Code": "ImageTagAlreadyExistsException",
                        "Message": "tag exists",
                    }
                },
                operation_name="PutImage",
            )
        self.tag_to_digest[image_tag] = DIGEST
        return DIGEST


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("METADATA_TABLE_NAME", METADATA_TABLE)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")


@pytest.fixture
def ddb_table(env: None, aws_region: str) -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=aws_region)
        resource.create_table(
            TableName=METADATA_TABLE,
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
        yield resource.Table(METADATA_TABLE)


def seed_validate_record(
    table: Any,
    *,
    artifact_kind: str = "catalog",
    validate_status: str = "validated",
) -> None:
    """Write a validate record keyed by (project pk, digest sk).

    The row carries NO ``name`` attribute — validate does not derive one and
    mint does not read one. The artifact-kind is the only record-sourced tag
    input; every record carrying an ``artifactKind`` mints (there is no untagged
    outcome).
    """
    item: dict[str, Any] = {
        **row_key(DIGEST),  # tenant-PK: pk={tenant-id}, sk={project-id}#{digest}
        "bundleTag": "pending",
        "artifactKind": artifact_kind,
        "validateStatus": validate_status,
        "createdAt": "2026-06-01T00:00:00Z",
    }
    table.put_item(Item=item)


def _attestation(
    *,
    org_id: str = ORG_ID,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> dict[str, Any]:
    """The Producer_Attestation_Referrer claim projection threaded onto the
    archived event — the source of the opaque identity triple."""
    return {
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "artifactKind": "catalog",
    }


def archived_event(**overrides: Any) -> dict[str, Any]:
    detail = {
        "manifestDigest": DIGEST,
        "repositoryName": REPOSITORY,
        "registryId": REGISTRY_ID,
        "region": REGION,
        "configBlob": {},
        "attestation": _attestation(),
    }
    detail.update(overrides.pop("detail", {}))
    event = {
        "detail-type": "artifact.archived",
        "source": "dev.cairnproofs.artifact",
        "time": "2026-06-01T00:01:00Z",
        "detail": detail,
    }
    event.update(overrides)
    return event


@pytest.fixture
def reload_handler(ddb_table: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Reload the mint-tag handler with patched ECR edges + recording bus."""
    import cairn_lambda_mint_tag.ecr_client as ecr_module
    import cairn_lambda_mint_tag.events as events_module
    import cairn_lambda_mint_tag.handler as handler_module

    ecr_module._ECR_CLIENT = None
    events_module._EVENTS_CLIENT = None
    handler = importlib.reload(handler_module)
    handler._INDEX = None

    fake_ecr = FakeEcr()
    recorder = EventRecorder()

    monkeypatch.setattr(handler, "fetch_manifest", fake_ecr.fetch_manifest)
    monkeypatch.setattr(handler, "tags_for_digest", fake_ecr.tags_for_digest)
    monkeypatch.setattr(handler, "digest_for_tag", fake_ecr.digest_for_tag)
    monkeypatch.setattr(handler, "put_image_tag", fake_ecr.put_image_tag)
    monkeypatch.setattr(events_module, "_events", lambda: recorder)

    handler._test_fake_ecr = fake_ecr
    handler._test_recorder = recorder
    handler._test_table = ddb_table

    yield handler

    ecr_module._ECR_CLIENT = None
    events_module._EVENTS_CLIENT = None
    handler._INDEX = None
