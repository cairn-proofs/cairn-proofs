"""Shared fixtures for archive-to-s3 Lambda tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from cairn_oci import (
    CONFIG_BLOB_MEDIA_TYPE,
    OCI_EMPTY_MEDIA_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    compute_sha256_digest,
)
from moto import mock_aws

ACCOUNT_ID = "123456789012"
REGION = "us-east-2"
STAGE = "alpha"
REGION_CODE = "cmh"
ORG_ID = "acme-org"
TENANT_ID = "alpha-bank-tenant"
PROJECT_ID = "alpha-bank"
REPO_NAME = f"artifact-{REGION_CODE}-{STAGE}/{ORG_ID}/{TENANT_ID}/{PROJECT_ID}"
EVENT_BUS_NAME = "cairnproofs4af-artifact-cmh-alpha"
ARCHIVE_BUCKET = "cairn-archive-cmh-alpha"


@dataclass(frozen=True)
class BundleFixture:
    manifest_digest: str
    manifest_dict: dict[str, Any]
    manifest_raw: str
    config_digest: str
    config_bytes: bytes
    layer_digest: str
    layer_bytes: bytes
    repo_name: str
    registry_id: str
    region: str
    # Opaque identity triple. The WORM key prefix is
    # {org-id}/{tenant-id}/{project-id}/{manifest-digest}/; these are carried on
    # the signed claim, never on the path as display names.
    org_id: str
    tenant_id: str
    project_id: str
    # Producer_Attestation_Referrer (subject = content manifest digest). Its own OCI
    # digest is the second object the archive stage carries; its claim layer +
    # placeholder config are archived under the shared content prefix.
    attestation: dict[str, Any]
    attestation_digest: str
    attestation_dict: dict[str, Any]
    attestation_raw: str
    attestation_config_digest: str
    attestation_config_bytes: bytes
    attestation_layer_digest: str
    attestation_layer_bytes: bytes


# Sentinel so callers can distinguish "default period" from "omit it entirely"
# (the latter exercises the D7 period-resolution fallback in the archive stage,
# whose event carries no ECR tag).
_OMIT = object()


def build_bundle(
    *,
    org_id: str = ORG_ID,
    tenant_id: str = TENANT_ID,
    project_id: str = "alpha-bank",
    bundle_type: str = "assessment-results",
    assessment_period: Any = _OMIT,
) -> BundleFixture:
    # The Config_Blob carries no bundleType; kind rides only on the Attestation
    # claim's `artifactKind` (built from `bundle_type` below).
    config: dict[str, Any] = {
        "projectId": project_id,
        "controls": ["ac-1"],
        "controlFamily": "AC",
        "catalogOrProfileUuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    }
    if assessment_period is _OMIT:
        config["assessmentPeriod"] = "2026-Q2"
    elif assessment_period is not None:
        config["assessmentPeriod"] = assessment_period
    config_bytes = json.dumps(config).encode("utf-8")
    config_digest = compute_sha256_digest(config_bytes)
    layer_bytes = b"some-evidence-payload-bytes"
    layer_digest = compute_sha256_digest(layer_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": CONFIG_BLOB_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/pdf",
                "digest": layer_digest,
                "size": len(layer_bytes),
                "annotations": {"org.opencontainers.image.title": "evidence.pdf"},
            }
        ],
        "annotations": {},
    }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    manifest_bytes = manifest_raw.encode("utf-8")
    manifest_digest = compute_sha256_digest(manifest_bytes)

    # Producer_Attestation_Referrer: the producer's compliance claim, subject = content
    # manifest digest. Its claim payload is the single layer; a 2-byte `{}`
    # placeholder config matches the pipeline's referrer idiom.
    attestation_claim: dict[str, Any] = {
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "artifactKind": bundle_type,
        "controls": ["ac-1"],
        "catalogOrProfileUuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    }
    if assessment_period is _OMIT:
        attestation_claim["assessmentPeriod"] = "2026-Q2"
    elif assessment_period is not None:
        attestation_claim["assessmentPeriod"] = assessment_period
    att_layer_bytes = json.dumps(attestation_claim, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    att_layer_digest = compute_sha256_digest(att_layer_bytes)
    att_config_bytes = b"{}"
    att_config_digest = compute_sha256_digest(att_config_bytes)
    attestation_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
        "config": {
            "mediaType": OCI_EMPTY_MEDIA_TYPE,
            "digest": att_config_digest,
            "size": len(att_config_bytes),
        },
        "layers": [
            {
                "mediaType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                "digest": att_layer_digest,
                "size": len(att_layer_bytes),
            }
        ],
        "subject": {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": manifest_digest,
            "size": len(manifest_bytes),
        },
        "annotations": {},
    }
    attestation_raw = json.dumps(attestation_manifest, separators=(",", ":"))
    attestation_digest = compute_sha256_digest(attestation_raw.encode("utf-8"))

    return BundleFixture(
        manifest_digest=manifest_digest,
        manifest_dict=manifest,
        manifest_raw=manifest_raw,
        config_digest=config_digest,
        config_bytes=config_bytes,
        layer_digest=layer_digest,
        layer_bytes=layer_bytes,
        repo_name=REPO_NAME,
        registry_id=ACCOUNT_ID,
        region=REGION,
        org_id=org_id,
        tenant_id=tenant_id,
        project_id=project_id,
        attestation=attestation_claim,
        attestation_digest=attestation_digest,
        attestation_dict=attestation_manifest,
        attestation_raw=attestation_raw,
        attestation_config_digest=att_config_digest,
        attestation_config_bytes=att_config_bytes,
        attestation_layer_digest=att_layer_digest,
        attestation_layer_bytes=att_layer_bytes,
    )


def signed_event(*, bundle: BundleFixture) -> dict[str, Any]:
    return {
        "version": "0",
        "id": "evt-signed-1",
        "detail-type": "artifact.signed",
        "source": "dev.cairnproofs.artifact",
        "account": ACCOUNT_ID,
        "time": "2026-05-26T12:10:00Z",
        "region": REGION,
        "detail": {
            "manifestDigest": bundle.manifest_digest,
            "repositoryName": bundle.repo_name,
            "registryId": bundle.registry_id,
            "region": bundle.region,
            "signatureDigest": "sha256:" + "5" * 64,
            "signingProfileArn": (f"arn:aws:signer:{REGION}:{ACCOUNT_ID}:/signing-profiles/p"),
            "timestampProvider": "system-clock",
            "signedAt": "2026-05-26T12:08:00Z",
            "configBlob": json.loads(bundle.config_bytes),
            "attestationDigest": bundle.attestation_digest,
            "attestation": bundle.attestation,
            "authenticatedPushPrincipal": {
                "arn": f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/EvidencePushRole/s",
                "type": "AssumedRole",
                "accountId": ACCOUNT_ID,
                "userName": "EvidencePushRole",
                "mfaAuthenticated": True,
                "principalId": "AROAEXAMPLE:s",
            },
            "ecrPushedAt": "2026-05-26T12:05:00Z",
        },
    }


@dataclass
class EventRecorder:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def put_events(self, *, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        self.entries.extend(Entries)
        return {
            "FailedEntryCount": 0,
            "Entries": [{"EventId": f"evt-{i}"} for i, _ in enumerate(Entries)],
        }

    def detail_types(self) -> list[str]:
        return [e["DetailType"] for e in self.entries]

    def details(self, detail_type: str) -> list[dict[str, Any]]:
        return [json.loads(e["Detail"]) for e in self.entries if e["DetailType"] == detail_type]


@dataclass
class FakeEcr:
    manifests: dict[str, tuple[dict[str, Any], str, str]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    put_blobs: list[bytes] = field(default_factory=list)
    referrer_artifacts: list[dict[str, Any]] = field(default_factory=list)
    next_referrer_digest: str = "sha256:" + "9" * 64

    def add_bundle(self, bundle: BundleFixture) -> None:
        self.manifests[bundle.manifest_digest] = (
            bundle.manifest_dict,
            bundle.manifest_raw,
            "application/vnd.oci.image.manifest.v1+json",
        )
        self.blobs[bundle.config_digest] = bundle.config_bytes
        self.blobs[bundle.layer_digest] = bundle.layer_bytes
        # Producer_Attestation_Referrer manifest + its blobs, fetchable by digest.
        self.manifests[bundle.attestation_digest] = (
            bundle.attestation_dict,
            bundle.attestation_raw,
            "application/vnd.oci.image.manifest.v1+json",
        )
        self.blobs[bundle.attestation_config_digest] = bundle.attestation_config_bytes
        self.blobs[bundle.attestation_layer_digest] = bundle.attestation_layer_bytes

    def fetch_manifest(
        self, *, registry_id: str, repository_name: str, image_digest: str
    ) -> tuple[dict[str, Any], str, str]:
        return self.manifests[image_digest]

    def fetch_blob(self, *, registry_id: str, repository_name: str, blob_digest: str) -> bytes:
        return self.blobs[blob_digest]

    def put_blob(self, *, registry_id: str, repository_name: str, blob: bytes) -> None:
        self.put_blobs.append(blob)

    def put_referrer_artifact(
        self,
        *,
        registry_id: str,
        repository_name: str,
        artifact_manifest: dict[str, Any],
    ) -> str:
        self.referrer_artifacts.append(artifact_manifest)
        return self.next_referrer_digest


@dataclass
class LambdaContext:
    aws_request_id: str = "req-1"
    function_name: str = "archive-to-s3"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:archive-to-s3"


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("ARCHIVE_BUCKET", ARCHIVE_BUCKET)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("WORM_ENABLED", "false")
    monkeypatch.setenv("WORM_RETENTION_YEARS", "7")


@pytest.fixture
def s3_bucket(env: None, aws_region: str) -> Iterator[Any]:
    with mock_aws():
        s3 = boto3.client("s3", region_name=aws_region)
        s3.create_bucket(
            Bucket=ARCHIVE_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": aws_region},
        )
        yield s3


@pytest.fixture
def reload_handler(s3_bucket: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Reload archive-to-s3 handler with monkeypatched ECR edges and recording bus."""
    import importlib

    import cairn_lambda_archive_to_s3.ecr_client as ecr_module
    import cairn_lambda_archive_to_s3.events as events_module
    import cairn_lambda_archive_to_s3.handler as handler_module
    import cairn_lambda_archive_to_s3.referrers as referrers_module

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
    handler_module._S3 = None
    handler = importlib.reload(handler_module)
    handler._S3 = None

    fake_ecr = FakeEcr()
    recorder = EventRecorder()
    existing_receipt_state: dict[str, str | None] = {"value": None}
    existing_resource_state: dict[str, str | None] = {"value": None}

    monkeypatch.setattr(handler, "fetch_manifest", fake_ecr.fetch_manifest)
    monkeypatch.setattr(handler, "fetch_blob", fake_ecr.fetch_blob)
    monkeypatch.setattr(handler, "put_blob", fake_ecr.put_blob)
    monkeypatch.setattr(handler, "put_referrer_artifact", fake_ecr.put_referrer_artifact)
    monkeypatch.setattr(
        handler,
        "existing_receipt_referrer",
        lambda **kw: existing_receipt_state["value"],
    )
    # The resource-referrer idempotency lookup also hits boto3 ECR in prod; stub
    # it (default None) so any fresh-archive test carrying an
    # ``artifactResourceReference`` exercises the mint path without reaching real
    # AWS. Tests that want to drive the discover-existing branch set
    # ``handler._test_existing_resource_state["value"]``.
    monkeypatch.setattr(
        handler,
        "existing_resource_referrer",
        lambda **kw: existing_resource_state["value"],
    )
    monkeypatch.setattr(events_module, "_events", lambda: recorder)

    handler._test_fake_ecr = fake_ecr
    handler._test_recorder = recorder
    handler._test_existing_receipt_state = existing_receipt_state
    handler._test_existing_resource_state = existing_resource_state
    handler._test_s3 = s3_bucket

    yield handler

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
    handler_module._S3 = None
