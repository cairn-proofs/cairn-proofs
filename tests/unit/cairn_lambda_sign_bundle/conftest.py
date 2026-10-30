"""Shared fixtures for sign-bundle Lambda tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from moto import mock_aws

ACCOUNT_ID = "123456789012"
REGION = "us-east-2"
STAGE = "alpha"
REGION_CODE = "cmh"
APP_NAME = "cairnproofs4af"
# Bare libs-level namespace (appName is prepended at the CLI/CDK layer, never in
# the appName-agnostic libs core; the crosscheck only inspects the trailing
# segment, so the leading prefix is immaterial here).
REPO_NAME = f"artifact-{REGION_CODE}-{STAGE}/alpha-bank/assessment-results"
EVENT_BUS_NAME = "cairnproofs4af-artifact-cmh-alpha"
METADATA_TABLE_NAME = "cairnproofs4af-ArtifactMetadata-cmh-alpha"
# Physical AWS Signer profile name: {appName}_artifact_pipeline_signing_{qualifier}.
SIGNER_PROFILE_ARN = (
    f"arn:aws:signer:{REGION}:{ACCOUNT_ID}:/signing-profiles/"
    f"{APP_NAME}_artifact_pipeline_signing_{REGION_CODE}_{STAGE}"
)
# Full SSM timestamp-provider parameter name (/{appName}/{stage}/...), passed to
# the handler via env exactly as the CDK passes it — the handler reads the name,
# it does not reconstruct the path.
TIMESTAMP_PROVIDER_PARAM = f"/{APP_NAME}/{STAGE}/signing/timestamp-provider"


def manifest_digest(filler: str = "a") -> str:
    return "sha256:" + (filler * 64)[:64]


def signature_digest(filler: str = "b") -> str:
    return "sha256:" + (filler * 64)[:64]


def attestation_digest(filler: str = "d") -> str:
    return "sha256:" + (filler * 64)[:64]


def validated_event(
    *,
    md: str | None = None,
    project_id: str = "alpha-bank",
    account_id: str | None = None,
    repo_name: str = REPO_NAME,
    event_time: str = "2026-05-26T12:05:00Z",
    att_digest: str | None = None,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    md_value = md or manifest_digest("a")
    detail: dict[str, Any] = {
        "manifestDigest": md_value,
        "repositoryName": repo_name,
        "registryId": ACCOUNT_ID,
        "region": REGION,
        "ecrPushedAt": event_time,
        # The configBlob carries no bundleType; kind rides only on the
        # Attestation claim.
        "configBlob": {
            "projectId": project_id,
            "accountId": account_id,
        },
        "authenticatedPushPrincipal": {
            "arn": f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/EvidencePushRole/s",
            "type": "AssumedRole",
            "accountId": ACCOUNT_ID,
            "userName": "EvidencePushRole",
            "mfaAuthenticated": True,
            "principalId": "AROAEXAMPLE:s",
        },
    }
    # The two-signature contract: the
    # validate stage carries the Attestation referrer's own OCI digest
    # (sign target #2) and the projected claim dict on the event.
    if att_digest is not None:
        detail["attestationDigest"] = att_digest
    if attestation is not None:
        detail["attestation"] = attestation
    return {
        "version": "0",
        "id": "evt-validated-1",
        "detail-type": "artifact.validated",
        "source": "dev.cairnproofs.artifact",
        "account": ACCOUNT_ID,
        "time": event_time,
        "region": REGION,
        "detail": detail,
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
        import json

        return [json.loads(e["Detail"]) for e in self.entries if e["DetailType"] == detail_type]


@dataclass
class LambdaContext:
    aws_request_id: str = "req-1"
    function_name: str = "sign-bundle"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:sign-bundle"


@pytest.fixture
def lambda_context() -> LambdaContext:
    return LambdaContext()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("SIGNER_PROFILE_ARN", SIGNER_PROFILE_ARN)
    monkeypatch.setenv("METADATA_TABLE_NAME", METADATA_TABLE_NAME)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")
    monkeypatch.setenv("SIGN_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("TIMESTAMP_PROVIDER_PARAM", TIMESTAMP_PROVIDER_PARAM)


@pytest.fixture
def ssm_with_system_clock(env: None, aws_region: str) -> Iterator[Any]:
    """Boot moto SSM with the timestamp-provider param = system-clock at the
    deployed /{appName}/{stage}/signing/timestamp-provider name."""
    with mock_aws():
        client = boto3.client("ssm", region_name=aws_region)
        client.put_parameter(
            Name=TIMESTAMP_PROVIDER_PARAM,
            Value="system-clock",
            Type="String",
            Overwrite=True,
        )
        yield client


@pytest.fixture
def metadata_table(env: None, aws_region: str) -> Iterator[Any]:
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
        ssm = boto3.client("ssm", region_name=aws_region)
        ssm.put_parameter(
            Name=TIMESTAMP_PROVIDER_PARAM,
            Value="system-clock",
            Type="String",
            Overwrite=True,
        )
        yield ddb.Table(METADATA_TABLE_NAME)


@pytest.fixture
def reload_handler(metadata_table: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Reload the sign-bundle handler under a moto-active context.

    Resets module-level boto3 caches; replaces ``events._events`` with a
    recording stub; replaces ``existing_signature_digest`` and
    ``sign_artifact`` with controllable callables that tests can program
    via the returned handler's ``_test_*`` attributes.
    """
    import importlib

    import cairn_lambda_sign_bundle.events as events_module
    import cairn_lambda_sign_bundle.handler as handler_module
    import cairn_lambda_sign_bundle.referrers as referrers_module

    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
    handler = importlib.reload(handler_module)

    recorder = EventRecorder()
    monkeypatch.setattr(events_module, "_events", lambda: recorder)

    # Per-subject signature state, keyed by the SUBJECT's OCI digest (a
    # subject is either the content manifest or the Attestation referrer).
    # ``existing_signature_state["value"]`` is a legacy single-subject knob
    # the pre-two-signature tests set to pre-seed an existing signature; it
    # is interpreted as the content manifest's existing signature.
    existing_signature_state: dict[str, str | None] = {"value": None}
    per_subject_existing: dict[str, str | None] = {}
    sign_calls: list[dict[str, Any]] = []

    def _ref_to_digest(artifact_reference: str) -> str:
        # ``...amazonaws.com/{repo}@{digest}`` — the subject digest is the
        # segment after the final ``@``.
        return artifact_reference.rsplit("@", 1)[-1]

    def _existing(*, registry_id: str, repository_name: str, image_digest: str) -> str | None:
        if image_digest in per_subject_existing:
            return per_subject_existing[image_digest]
        # Legacy single-knob behavior: applies to the content manifest ONLY,
        # never to the Attestation referrer's subject digest — so pre-seeding
        # a content signature doesn't accidentally mark the attestation signed.
        if image_digest == manifest_digest("a"):
            return existing_signature_state["value"]
        return None

    def _sig_for_subject(subject_digest: str) -> str:
        # The content manifest's fresh signature stays ``signature_digest("b")``
        # for backward compatibility with the single-subject tests. Any other
        # subject (the Attestation referrer) gets a distinct signature digest
        # derived deterministically from its own subject digest so a test can
        # assert the two signatures differ.
        if subject_digest == manifest_digest("a"):
            return signature_digest("b")
        # Derive a stable, distinct signature digest from the subject digest's
        # bare hex so attestation signatures never collide with content's.
        bare = subject_digest.split(":", 1)[-1]
        return "sha256:sig" + bare[:61]

    def _sign(artifact_reference, profile, timestamp_provider, *, timeout_seconds):
        sign_calls.append(
            {
                "artifact_reference": artifact_reference,
                "profile_arn": profile.arn,
                "timestamp_provider": timestamp_provider,
                "timeout_seconds": timeout_seconds,
            }
        )
        # Production sign attaches a referrer to ECR; we simulate by
        # populating the existing-signature state so the handler's
        # post-sign Referrers-API lookup returns the new digest. Each
        # subject digest gets its OWN signature digest so tests can assert
        # the two signatures are distinct and independently produced.
        subject_digest = _ref_to_digest(artifact_reference)
        sig = _sig_for_subject(subject_digest)
        # Record the freshly-attached signature under THIS subject's digest
        # only — never globally — so signing one subject can't make another
        # subject's post-sign lookup spuriously short-circuit.
        per_subject_existing[subject_digest] = sig
        if subject_digest == manifest_digest("a"):
            existing_signature_state["value"] = sig
        from cairn_signing.notation import SignArtifactResult

        return SignArtifactResult(
            artifact_reference=artifact_reference,
            timestamp_provider=timestamp_provider,
        )

    monkeypatch.setattr(handler, "existing_signature_digest", _existing)
    monkeypatch.setattr(handler, "sign_artifact", _sign)

    handler._test_recorder = recorder
    handler._test_existing_signature_state = existing_signature_state
    handler._test_per_subject_existing = per_subject_existing
    handler._test_sign_calls = sign_calls
    handler._test_metadata_table = metadata_table

    yield handler

    events_module._EVENTS_CLIENT = None
    referrers_module._ECR_CLIENT = None
