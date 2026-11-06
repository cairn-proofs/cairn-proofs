"""Shared fixtures for validate-bundle Lambda tests.

Mocking strategy:
- ECR manifest/blob fetches are monkeypatched at the project's
  ``cairn_lambda_validate_bundle.ecr_client`` boundary because moto
  doesn't implement ``initiate_layer_upload`` / ``get_download_url_for_layer``.
- EventBridge ``put_events`` is captured by a recording stub that
  replaces the boto3 ``events`` client so tests can assert detail-type +
  payload contents on every emission.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from cairn_oci import (
    CONFIG_BLOB_MEDIA_TYPE,
    INTOTO_STATEMENT_MEDIA_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    ProducerAttestationReferrer,
    build_producer_statement,
    compute_sha256_digest,
)
from cairn_oci.annotations import CairnAnnotations
from moto import mock_aws

ACCOUNT_ID = "123456789012"
REGION = "us-east-2"
STAGE = "alpha"
REGION_CODE = "cmh"
# The uniform three-segment logical hierarchy {org-id}/{tenant-id}/{project-id}
# follows the {appName}-artifact-{regionCode}-{stage} namespace.
# Each segment is a stable, opaque id; the producer claim must carry all three
# and each is cross-checked exact-match against the corresponding path segment.
ORG_ID = "org-acme"
TENANT_ID = "tnt-foo"
PROJECT_ID = "alpha-bank"
REPO_NAME = f"artifact-{REGION_CODE}-{STAGE}/{ORG_ID}/{TENANT_ID}/{PROJECT_ID}"
EVENT_BUS_NAME = "cairnproofs4af-artifact-cmh-alpha"
ARCHIVE_BUCKET = "cairn-archive-cmh-alpha"
VALIDATE_DLQ_URL = (
    "https://sqs.us-east-2.amazonaws.com/123456789012/cairnproofs4af-validate-bundle-dlq-cmh-alpha"
)
CATALOG_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
CREATED_AT = "2026-05-26T12:00:00Z"
PUSHED_AT = "2026-05-26T12:05:00Z"
# The default in-band OSCAL last-modified (see catalog_blob_bytes) sits well
# before PUSHED_AT but inside the staleness window so the happy path neither
# rejects nor warns.
LAST_MODIFIED = "2026-05-26T00:00:00Z"
OSCAL_VERSION = "1.2.1"


def catalog_blob_bytes(
    uuid: str = CATALOG_UUID,
    oscal_version: str = OSCAL_VERSION,
    last_modified: str = LAST_MODIFIED,
) -> bytes:
    return json.dumps(
        {
            "catalog": {
                "uuid": uuid,
                "metadata": {
                    "title": "Test Catalog",
                    "last-modified": last_modified,
                    "version": "1.0",
                    "oscal-version": oscal_version,
                },
            }
        }
    ).encode("utf-8")


def config_blob_body(*, project_id: str = "alpha-bank") -> bytes:
    """The content Config_Blob.

    Carries only content-descriptive fields (projectId). artifact-kind is carried
    solely by the Producer_Attestation_Referrer ``artifactKind`` claim (see
    ``attestation_claim``), not the content manifest. The producer claim fields
    (accountId, controls, catalogOrProfileUuid, assessmentPeriod) likewise live in
    the referrer.
    """
    return json.dumps({"projectId": project_id}).encode("utf-8")


def attestation_claim(
    *,
    org_id: str = ORG_ID,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    artifact_kind: str = "assessment-results",
    catalog_or_profile_uuid: str | None = CATALOG_UUID,
    controls: list[str] | None = None,
    assessment_period: str | None = "2026-q2",
    assessor: str | None = None,
    assessment_id: str | None = None,
    supersedes: str | None = None,
) -> bytes:
    """The producer's compliance claim as FLAT claim JSON (the in-toto `predicate`).

    Returns the flat camelCase claim body (today's `ProducerAttestationReferrer` wire
    shape). The claim's identity tuple is the opaque ``orgId`` + ``tenantId`` +
    ``projectId`` triple — each cross-checked exact-match against
    the corresponding ECR repository path segment. The in-toto Statement envelope
    is added by ``build_attestation``, which binds the Statement `subject` to the
    content digest it already holds. Callers keep passing the flat body as
    ``claim_bytes``; ``build_attestation`` wraps it.
    """
    payload: dict[str, Any] = {
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "artifactKind": artifact_kind,
        "controls": controls if controls is not None else ["ac-1", "ac-2"],
    }
    if catalog_or_profile_uuid is not None:
        payload["catalogOrProfileUuid"] = catalog_or_profile_uuid
    if assessment_period is not None:
        payload["assessmentPeriod"] = assessment_period
    if assessor is not None:
        payload["assessor"] = assessor
    if assessment_id is not None:
        payload["assessmentId"] = assessment_id
    if supersedes is not None:
        payload["supersedes"] = supersedes
    return json.dumps(payload).encode("utf-8")


def producer_statement_bytes(
    *,
    subject_digest: str,
    claim_bytes: bytes | None = None,
) -> bytes:
    """Wrap a flat claim body into the on-the-wire in-toto Statement layer bytes.

    Mirrors the production write path: builds the Cairn producer Statement
    (single-element subject = ``subject_digest``, Cairn predicateType, predicate =
    the flat claim) via the libs/oci builder so the fixture and the validate read
    path agree on the wire shape.
    """
    if claim_bytes is None:
        claim_bytes = attestation_claim()
    claim = ProducerAttestationReferrer.model_validate_json(claim_bytes)
    statement = build_producer_statement(subject_digest, claim)
    return json.dumps(statement).encode("utf-8")


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
    # Push-by-digest: the production first-validation path is UNTAGGED.
    # The canonical tag is minted later by the pipeline FROM the validated
    # coordinates, so the validate stage never reads it. Defaults to "" (untagged);
    # the only legitimate tagged event is the post-mint re-tag, which tests inject
    # explicitly to exercise the digest-keyed short-circuit.
    image_tag: str = ""


def build_bundle(
    *,
    repo_name: str = REPO_NAME,
    config_bytes: bytes | None = None,
    layer_bytes: bytes | None = None,
    layer_media_type: str = "application/oscal.catalog+json",
    config_media_type: str = CONFIG_BLOB_MEDIA_TYPE,
    layer_title: str | None = "catalog.json",
    annotations_override: dict[str, str | None] | None = None,
    drop_annotations: tuple[str, ...] = (),
    image_tag: str | None = None,
) -> BundleFixture:
    """Build the CONTENT manifest (pure content identity — no producer claim,
    no synthetic `created`, no artifact-kind). The identity annotation floor is
    project-id only; artifact-kind is carried by the Producer_Attestation_Referrer
    claim, not the content manifest."""
    if config_bytes is None:
        config_bytes = config_blob_body()
    config_digest = compute_sha256_digest(config_bytes)
    if layer_bytes is None:
        layer_bytes = catalog_blob_bytes()
    layer_digest = compute_sha256_digest(layer_bytes)

    layer_annotations: dict[str, str] = {}
    if layer_title is not None:
        layer_annotations["org.opencontainers.image.title"] = layer_title

    manifest_annotations: dict[str, str | None] = {
        CairnAnnotations.PROJECT_ID: "alpha-bank",
    }
    if annotations_override:
        manifest_annotations.update(annotations_override)
    for key in drop_annotations:
        manifest_annotations.pop(key, None)
    manifest_annotations = {k: v for k, v in manifest_annotations.items() if v is not None}

    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": config_media_type,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": layer_media_type,
                "digest": layer_digest,
                "size": len(layer_bytes),
                "annotations": layer_annotations,
            }
        ],
        "annotations": manifest_annotations,
    }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    manifest_bytes = manifest_raw.encode("utf-8")
    manifest_digest = compute_sha256_digest(manifest_bytes)
    if image_tag is None:
        image_tag = ""
    return BundleFixture(
        manifest_digest=manifest_digest,
        manifest_dict=manifest,
        manifest_raw=manifest_raw,
        config_digest=config_digest,
        config_bytes=config_bytes,
        layer_digest=layer_digest,
        layer_bytes=layer_bytes,
        repo_name=repo_name,
        registry_id=ACCOUNT_ID,
        region=REGION,
        image_tag=image_tag,
    )


# OCI empty config descriptor used by the Producer_Attestation_Referrer manifest. The
# claim JSON lives in the config blob the descriptor points at; the referrer's
# discriminator is its top-level `artifactType`.
@dataclass(frozen=True)
class AttestationFixture:
    """A pushed Producer_Attestation_Referrer over a content bundle's subject digest."""

    attestation_digest: str
    manifest_dict: dict[str, Any]
    manifest_raw: str
    claim_digest: str
    claim_bytes: bytes
    subject_digest: str
    repo_name: str
    registry_id: str
    region: str


# Sentinel so build_attestation can model a referrer with NO subject descriptor.
_NO_SUBJECT = object()


def build_attestation(
    *,
    subject_digest: str,
    claim_bytes: bytes | None = None,
    repo_name: str = REPO_NAME,
    artifact_type: str = PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    include_subject: bool = True,
    config_media_type: str = "application/vnd.oci.empty.v1+json",
    wrap_statement: bool = True,
    statement_subject_digest: str | None = None,
    statement_subjects: list[dict[str, Any]] | None = None,
    predicate_type: str | None = None,
    layer_bytes: bytes | None = None,
) -> AttestationFixture:
    """Build a Producer_Attestation_Referrer whose envelope `subject` is the content digest.

    The claim LAYER carries a standard in-toto Statement (mediaType
    ``application/vnd.in-toto+json``) wrapping the flat ``claim_bytes`` predicate
    Knobs for the negative paths:

    - ``wrap_statement=False`` — put ``layer_bytes`` (or the raw ``claim_bytes``)
      into the layer verbatim, NOT inside a Statement (models malformed JSON / a
      non-Statement payload).
    - ``statement_subject_digest`` — override ONLY the Statement's inner subject
      digest while the OCI envelope subject stays ``subject_digest`` (models D4
      Statement/envelope divergence).
    - ``statement_subjects`` — replace the Statement's ``subject`` array wholesale
      (models the zero-/multi-subject rejection).
    - ``predicate_type`` — override the Statement ``predicateType`` (models the D5
      predicateType mismatch).

    Set ``include_subject=False`` to model a malformed referrer with no envelope
    subject; set ``artifact_type`` to a non-attestation value for a non-trigger push.
    """
    if claim_bytes is None:
        claim_bytes = attestation_claim()
    if not wrap_statement:
        # Verbatim layer payload (malformed JSON / non-Statement): bypass the
        # Statement envelope entirely.
        layer_payload = layer_bytes if layer_bytes is not None else claim_bytes
    else:
        claim = ProducerAttestationReferrer.model_validate_json(claim_bytes)
        statement = build_producer_statement(subject_digest, claim)
        if statement_subjects is not None:
            statement["subject"] = statement_subjects
        elif statement_subject_digest is not None:
            # Re-key only the Statement's inner subject so it diverges from the
            # OCI envelope subject (the digest is the bare hex in a DigestSet).
            statement["subject"] = [
                {"digest": {"sha256": statement_subject_digest.split(":", 1)[-1]}}
            ]
        if predicate_type is not None:
            statement["predicateType"] = predicate_type
        layer_payload = json.dumps(statement).encode("utf-8")
    claim_bytes = layer_payload
    claim_digest = compute_sha256_digest(claim_bytes)

    # Faithful `oras attach` wire shape: the claim JSON is the referrer's single
    # LAYER (mediaType == the attestation type), over an empty OCI scratch config
    # (`{}`). The referrer's top-level `artifactType` is the trigger
    # discriminator; the config is NOT the claim. (The earlier fixture put the
    # claim in the config descriptor, which `oras attach` never does — that drift
    # masked a producer↔validate wire mismatch until a live push surfaced it.)
    empty_config = b"{}"
    empty_config_digest = compute_sha256_digest(empty_config)
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": artifact_type,
        "config": {
            "mediaType": config_media_type,
            "digest": empty_config_digest,
            "size": len(empty_config),
        },
        "layers": [
            {
                # The claim layer announces the in-toto Statement format to
                # external tooling once wrapped; the OCI envelope artifactType
                # (above) stays the Cairn role label / trigger discriminator.
                "mediaType": INTOTO_STATEMENT_MEDIA_TYPE
                if wrap_statement
                else PRODUCER_ATTESTATION_ARTIFACT_TYPE,
                "digest": claim_digest,
                "size": len(claim_bytes),
            }
        ],
    }
    if include_subject:
        manifest["subject"] = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": subject_digest,
            "size": 100,
        }
    manifest_raw = json.dumps(manifest, separators=(",", ":"))
    attestation_digest = compute_sha256_digest(manifest_raw.encode("utf-8"))
    return AttestationFixture(
        attestation_digest=attestation_digest,
        manifest_dict=manifest,
        manifest_raw=manifest_raw,
        claim_digest=claim_digest,
        claim_bytes=claim_bytes,
        subject_digest=subject_digest,
        repo_name=repo_name,
        registry_id=ACCOUNT_ID,
        region=REGION,
    )


def user_identity_block(
    *,
    account_id: str = ACCOUNT_ID,
    arn: str | None = None,
    user_type: str = "AssumedRole",
    mfa: str = "true",
) -> dict[str, Any]:
    return {
        "type": user_type,
        "arn": arn or f"arn:aws:sts::{account_id}:assumed-role/EvidencePushRole/session-1",
        "principalId": "AROAEXAMPLE:session-1",
        "accountId": account_id,
        "sessionContext": {
            "sessionIssuer": {
                "type": "Role",
                "principalId": "AROAEXAMPLE",
                "arn": f"arn:aws:iam::{account_id}:role/EvidencePushRole",
                "accountId": account_id,
                "userName": "EvidencePushRole",
            },
            "attributes": {"mfaAuthenticated": mfa, "creationDate": PUSHED_AT},
        },
    }


def ecr_push_event(
    *,
    bundle: BundleFixture,
    omit_user_identity: bool = False,
    pushed_at: str = PUSHED_AT,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "eventTime": pushed_at,
        "eventSource": "ecr.amazonaws.com",
        "eventName": "PutImage",
        "awsRegion": bundle.region,
        "recipientAccountId": bundle.registry_id,
        "requestParameters": {
            "registryId": bundle.registry_id,
            "repositoryName": bundle.repo_name,
            "imageTag": bundle.image_tag,
            "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
        },
        "responseElements": {
            "image": {
                "registryId": bundle.registry_id,
                "repositoryName": bundle.repo_name,
                "imageId": {"imageDigest": bundle.manifest_digest},
            }
        },
    }
    if not omit_user_identity:
        detail["userIdentity"] = user_identity_block()
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.ecr",
        "account": bundle.registry_id,
        "time": pushed_at,
        "region": bundle.region,
        "detail": detail,
    }


def attestation_push_event(
    *,
    attestation: AttestationFixture,
    omit_user_identity: bool = False,
    pushed_at: str = PUSHED_AT,
) -> dict[str, Any]:
    """The inverted-trigger push: a PutImage whose imageDigest is the ATTESTATION
    referrer's digest. This is the single positive validation trigger."""
    detail: dict[str, Any] = {
        "eventTime": pushed_at,
        "eventSource": "ecr.amazonaws.com",
        "eventName": "PutImage",
        "awsRegion": attestation.region,
        "recipientAccountId": attestation.registry_id,
        "requestParameters": {
            "registryId": attestation.registry_id,
            "repositoryName": attestation.repo_name,
            "imageTag": "",
            "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
        },
        "responseElements": {
            "image": {
                "registryId": attestation.registry_id,
                "repositoryName": attestation.repo_name,
                "imageId": {"imageDigest": attestation.attestation_digest},
            }
        },
    }
    if not omit_user_identity:
        detail["userIdentity"] = user_identity_block()
    return {
        "version": "0",
        "id": "evt-attest-1",
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.ecr",
        "account": attestation.registry_id,
        "time": pushed_at,
        "region": attestation.region,
        "detail": detail,
    }


def submit(
    handler: Any,
    *,
    bundle: BundleFixture,
    claim_bytes: bytes | None = None,
    seed_bundle: bool = True,
    pushed_at: str = PUSHED_AT,
    omit_user_identity: bool = False,
) -> tuple[AttestationFixture, dict[str, Any]]:
    """Seed a content bundle + its Producer_Attestation_Referrer and return (attestation, event).

    The single positive trigger is the attestation push; callers invoke
    ``handler.lambda_handler(event, ctx)`` with the returned event. Returns the
    built :class:`AttestationFixture` so callers can assert on its digest.
    """
    if seed_bundle:
        handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        claim_bytes=claim_bytes,
        repo_name=bundle.repo_name,
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(
        attestation=attestation, pushed_at=pushed_at, omit_user_identity=omit_user_identity
    )
    return attestation, event


def seed_metadata_index(
    handler: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_digest: str,
    record: dict[str, Any],
) -> None:
    """Monkeypatch ``_metadata_index`` to a read-only stub returning ``record``
    for ``manifest_digest`` (else ``None``). ``create_or_merge`` /
    ``add_validated_attestation`` are no-ops so a processing path does not error —
    use the stateful ``FakeMetadataIndex`` instead when a test needs to read back
    what was written."""

    class _Index:
        def get_record(self, *, manifest_digest: str, **_kw: Any) -> dict[str, Any] | None:
            return dict(record) if manifest_digest == _digest else None

        def create_or_merge(self, **_kw: Any) -> None:
            pass

        def add_validated_attestation(self, **_kw: Any) -> None:
            pass

    _digest = manifest_digest
    monkeypatch.setattr(handler, "_metadata_index", lambda: _Index())


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
class FakeSqs:
    """Records every send_message so tests can assert the in-band DLQ rejection.

    Mirrors the boto3 SQS client surface ``dlq.send_rejection`` calls
    (``send_message(QueueUrl=..., MessageBody=...)``). Injected by
    monkeypatching the dlq module's lazy ``_sqs()`` accessor — the same shape as
    the events recorder injection.
    """

    messages: list[dict[str, Any]] = field(default_factory=list)

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict[str, Any]:
        self.messages.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})
        return {"MessageId": f"msg-{len(self.messages)}"}

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(m["MessageBody"]) for m in self.messages]


@dataclass
class FakeEcr:
    """Records all manifest/blob fetches and returns scripted responses.

    Mirrors only the project methods that ``ecr_client.fetch_manifest`` and
    ``ecr_client.fetch_blob`` call into; tests inject this via
    monkeypatching so we never touch real urllib3 / presigned URLs.
    """

    manifests: dict[str, tuple[dict[str, Any], str]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifest_fetch_log: list[tuple[str, str, str]] = field(default_factory=list)
    blob_fetch_log: list[tuple[str, str, str]] = field(default_factory=list)
    fetch_blob_side_effects: dict[str, Exception] = field(default_factory=dict)
    fetch_manifest_side_effects: dict[str, Exception] = field(default_factory=dict)
    # (repository_name, image_digest) pairs that exist — backs the subject-scope
    # check (image_resides_in_repo). add_bundle/add_attestation populate it.
    residency: set[tuple[str, str]] = field(default_factory=set)
    # image_resides_in_repo side effects keyed by image_digest (e.g. a transient
    # EcrFetchError); takes precedence over the residency set when present.
    residency_side_effects: dict[str, Exception] = field(default_factory=dict)

    def add_bundle(self, bundle: BundleFixture) -> None:
        self.manifests[bundle.manifest_digest] = (bundle.manifest_dict, bundle.manifest_raw)
        self.blobs[bundle.config_digest] = bundle.config_bytes
        self.blobs[bundle.layer_digest] = bundle.layer_bytes
        self.residency.add((bundle.repo_name, bundle.manifest_digest))

    def add_attestation(self, attestation: AttestationFixture) -> None:
        self.manifests[attestation.attestation_digest] = (
            attestation.manifest_dict,
            attestation.manifest_raw,
        )
        self.blobs[attestation.claim_digest] = attestation.claim_bytes
        self.residency.add((attestation.repo_name, attestation.attestation_digest))

    def fetch_manifest(
        self, *, registry_id: str, repository_name: str, image_digest: str
    ) -> tuple[dict[str, Any], str]:
        self.manifest_fetch_log.append((registry_id, repository_name, image_digest))
        if image_digest in self.fetch_manifest_side_effects:
            raise self.fetch_manifest_side_effects[image_digest]
        return self.manifests[image_digest]

    def fetch_blob(self, *, registry_id: str, repository_name: str, blob_digest: str) -> bytes:
        self.blob_fetch_log.append((registry_id, repository_name, blob_digest))
        if blob_digest in self.fetch_blob_side_effects:
            raise self.fetch_blob_side_effects[blob_digest]
        return self.blobs[blob_digest]

    def image_resides_in_repo(
        self, *, registry_id: str, repository_name: str, image_digest: str
    ) -> bool:
        if image_digest in self.residency_side_effects:
            raise self.residency_side_effects[image_digest]
        return (repository_name, image_digest) in self.residency


@dataclass
class LambdaContext:
    aws_request_id: str = "req-1"
    function_name: str = "validate-bundle"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:validate-bundle"


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
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")
    monkeypatch.setenv("STALENESS_WINDOW_DAYS", "30")
    monkeypatch.setenv("VALIDATE_DLQ_URL", VALIDATE_DLQ_URL)


@pytest.fixture
def reload_handler(env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Re-import the validate-bundle handler with monkeypatched edges.

    Returns the freshly reimported handler module. Also returns a tuple-like
    interface via the ``fake_ecr`` and ``event_recorder`` attributes on the
    yielded namespace for asserting against project-side state.
    """
    import importlib

    import cairn_lambda_validate_bundle.dlq as dlq_module
    import cairn_lambda_validate_bundle.ecr_client as ecr_module
    import cairn_lambda_validate_bundle.events as events_module
    import cairn_lambda_validate_bundle.handler as handler_module

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    dlq_module._SQS_CLIENT = None

    handler = importlib.reload(handler_module)

    fake_ecr = FakeEcr()
    recorder = EventRecorder()
    fake_sqs = FakeSqs()

    monkeypatch.setattr(handler, "fetch_manifest", fake_ecr.fetch_manifest)
    monkeypatch.setattr(handler, "fetch_blob", fake_ecr.fetch_blob)
    monkeypatch.setattr(handler, "image_resides_in_repo", fake_ecr.image_resides_in_repo)
    monkeypatch.setattr(events_module, "_events", lambda: recorder)
    monkeypatch.setattr(dlq_module, "_sqs", lambda: fake_sqs)

    handler._test_fake_ecr = fake_ecr
    handler._test_recorder = recorder
    handler._test_fake_sqs = fake_sqs

    yield handler

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None
    dlq_module._SQS_CLIENT = None


@pytest.fixture
def aws_stack(env: None, aws_region: str) -> Iterator[dict[str, Any]]:
    """Boot moto for ancillary AWS calls (none today; placeholder for parity)."""
    with mock_aws():
        events = boto3.client("events", region_name=aws_region)
        events.create_event_bus(Name=EVENT_BUS_NAME)
        yield {"events": events}
