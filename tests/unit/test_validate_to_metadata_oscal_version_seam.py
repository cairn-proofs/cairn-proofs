"""Cross-Lambda seam regression: validate → update_metadata OSCAL-version contract.

The content-derived OSCAL schema version travels across a Lambda boundary as a
*string-keyed* field: validate's ``_process_layers_and_build_reference`` stamps it onto each
OSCAL layer's ``oscal`` enrichment sub-object under ``oscalSchemaVersion`` (within
the unified ``layers`` list on the EventBridge ``artifact.validated`` detail), and
update_metadata projects that ``layers`` list verbatim onto the Metadata_Index
record, so the version lands at ``item["layers"][].oscal.oscalSchemaVersion``
(there is no record-level ``oscalSchemaVersion`` scalar — the version is a
per-layer property).

The per-Lambda unit suites each assert the ``oscalSchemaVersion`` key name on
their own side independently. A rename on ONE side (e.g. ``oscalVersion``)
would pass both suites yet silently break the pipeline — the indexed per-layer
version would go ``None`` and the audit trail would lose the schema version with
no loud failure. These tests close that gap by driving the *real* validate handler
to produce a *real* emitted detail, then feeding it verbatim into the *real*
update_metadata handler and asserting the content-derived version is what lands
in the Metadata_Index. No hand-built intermediate ``layers`` shape is
involved, so a key-name drift on either side fails here.

Both handlers are set up inline (not via their package-local ``reload_handler``
fixtures, which collide by name across the two suites). Setup mirrors those
fixtures exactly; every edge is monkeypatched or moto-mocked, so the test is
parallel-safe and makes no real AWS call.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from tests.unit.cairn_lambda_update_metadata.conftest import (
    CONTROLS_TABLE_NAME,
    METADATA_TABLE_NAME,
)
from tests.unit.cairn_lambda_validate_bundle.conftest import (
    ARCHIVE_BUCKET,
    EVENT_BUS_NAME,
    OSCAL_VERSION,
    REGION_CODE,
    STAGE,
    EventRecorder,
    FakeEcr,
    attestation_push_event,
    build_attestation,
    build_bundle,
)


class _LambdaContext:
    aws_request_id = "seam-req-1"
    function_name = "seam"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-2:123456789012:function:seam"


@pytest.fixture
def validate_handler(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Real validate-bundle handler with ECR + EventBridge edges stubbed.

    Mirrors the validate suite's ``env`` + ``reload_handler`` fixtures.
    """
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("ARCHIVE_BUCKET", ARCHIVE_BUCKET)
    monkeypatch.setenv("EVENT_BUS_NAME", EVENT_BUS_NAME)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")
    monkeypatch.setenv("STALENESS_WINDOW_DAYS", "30")

    import cairn_lambda_validate_bundle.ecr_client as ecr_module
    import cairn_lambda_validate_bundle.events as events_module
    import cairn_lambda_validate_bundle.handler as handler_module

    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None

    handler = importlib.reload(handler_module)
    fake_ecr = FakeEcr()
    recorder = EventRecorder()
    monkeypatch.setattr(handler, "fetch_manifest", fake_ecr.fetch_manifest)
    monkeypatch.setattr(handler, "fetch_blob", fake_ecr.fetch_blob)
    monkeypatch.setattr(handler, "image_resides_in_repo", fake_ecr.image_resides_in_repo)
    monkeypatch.setattr(events_module, "_events", lambda: recorder)
    handler._test_fake_ecr = fake_ecr
    handler._test_recorder = recorder
    yield handler
    ecr_module._ECR_CLIENT = None
    ecr_module._HTTP = None
    events_module._EVENTS_CLIENT = None


@pytest.fixture
def metadata_handler(monkeypatch: pytest.MonkeyPatch, aws_region: str) -> Iterator[tuple[Any, Any]]:
    """Real update-metadata handler over moto-mocked DynamoDB.

    Mirrors the update_metadata suite's ``env`` + ``tables`` + ``reload_handler``
    fixtures. Yields ``(handler, metadata_table)``.
    """
    monkeypatch.setenv("STAGE", STAGE)
    monkeypatch.setenv("REGION_CODE", REGION_CODE)
    monkeypatch.setenv("CAIRN_STAGE", STAGE)
    monkeypatch.setenv("CAIRN_REGION_CODE", REGION_CODE)
    monkeypatch.setenv("METADATA_TABLE_NAME", METADATA_TABLE_NAME)
    monkeypatch.setenv("CONTROLS_TABLE_NAME", CONTROLS_TABLE_NAME)
    monkeypatch.setenv("DEPLOYMENT_MODE", "Customer_Hosted")

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=aws_region)
        for name in (METADATA_TABLE_NAME, CONTROLS_TABLE_NAME):
            ddb.create_table(
                TableName=name,
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

        import cairn_lambda_update_metadata.handler as handler_module

        handler_module._INDEX = None
        handler_module._CONTROLS = None
        handler = importlib.reload(handler_module)
        handler._INDEX = None
        handler._CONTROLS = None
        yield handler, ddb.Table(METADATA_TABLE_NAME)
        handler_module._INDEX = None
        handler_module._CONTROLS = None


def _emitted_validated_detail(validate_handler: Any, bundle: Any) -> dict[str, Any]:
    """Run validate on ``bundle`` (via its Producer_Attestation_Referrer trigger) and return
    its real emitted detail payload."""
    validate_handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(subject_digest=bundle.manifest_digest)
    validate_handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)
    validate_handler.lambda_handler(event, _LambdaContext())
    emitted = validate_handler._test_recorder.details("artifact.validated")
    assert len(emitted) == 1
    return emitted[0]


def _index(metadata_handler: tuple[Any, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Feed an ``artifact.validated`` detail to update_metadata; return the row."""
    handler, table = metadata_handler
    event = {
        "version": "0",
        "id": "seam-evt-1",
        "detail-type": "artifact.validated",
        "source": "dev.cairnproofs.artifact",
        "account": "123456789012",
        "time": "2026-05-26T12:05:00Z",
        "region": "us-east-2",
        "detail": detail,
    }
    handler.lambda_handler(event, _LambdaContext())
    # Tenant-PK key schema ([META-001]): the row is addressed by the
    # opaque identity triple carried on the claim projection — pk = {tenant-id},
    # sk = {project-id}#{manifest-digest} — never by configBlob.
    tenant_id = detail["attestation"]["tenantId"]
    project_id = detail["attestation"]["projectId"]
    return table.get_item(Key={"pk": tenant_id, "sk": f"{project_id}#{detail['manifestDigest']}"})[
        "Item"
    ]


def test_content_version_survives_validate_to_metadata_seam(
    validate_handler: Any, metadata_handler: tuple[Any, Any]
) -> None:
    """The content-derived version validate emits is the version metadata indexes.

    End-to-end across the Lambda boundary with no hand-built intermediate: a
    key-name drift on either side of the ``oscalSchemaVersion`` contract makes
    the indexed value go ``None`` and fails this assertion loudly.
    """
    bundle = build_bundle()
    detail = _emitted_validated_detail(validate_handler, bundle)

    # Sanity: validate stamped the content-derived version under the contract key.
    assert detail["layers"][0]["oscal"]["oscalSchemaVersion"] == OSCAL_VERSION

    item = _index(metadata_handler, detail)
    assert item["layers"][0]["oscal"]["oscalSchemaVersion"] == OSCAL_VERSION
