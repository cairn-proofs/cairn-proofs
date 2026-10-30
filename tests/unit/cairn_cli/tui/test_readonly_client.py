"""No-other-method stub test for ``ReadOnlyAwsClient``.

This test is the structural enforcement of the *Read-only AWS access surface*
Requirement. It does two things:

1. Drives every public method of ``ReadOnlyAwsClient`` against stub boto3
   clients/resource that RECORD each method name called and RAISE on any
   non-allowlisted call, then asserts that ONLY allowlisted underlying
   operations were invoked.
2. Proves — via a ``getattr`` sweep over a forbidden-method corpus — that
   there is NO reachable path through the read-only surfaces to any mutating
   or message-consuming operation. ``PutItem``, ``DeleteObject``, and
   ``ReceiveMessage`` are named explicitly as the task requires; the wrappers
   raise ``AttributeError`` for each because the mutator is structurally
   absent, not merely "not called."
"""

from __future__ import annotations

from typing import Any

import pytest
from cairn_cli.tui._readonly_client import (
    ReadOnlyAwsClient,
    _ReadOnlyResource,
    _ReadOnlyTable,
)

# The complete allowlist, keyed by service, exactly as the access layer
# documents it. Any underlying boto3 op outside these sets is a violation.
ALLOWLISTED_DYNAMODB = {"query", "get_item"}
ALLOWLISTED_ECR = {"describe_repositories", "batch_get_image", "list_image_referrers"}
ALLOWLISTED_S3 = {"get_object", "head_object"}
ALLOWLISTED_SQS = {"get_queue_attributes"}

# Forbidden boto3 method names per service — mutations and message consumers.
# These MUST be unreachable through the read-only surfaces. PutItem,
# DeleteObject, and ReceiveMessage are called out by the task and appear here.
FORBIDDEN_DYNAMODB_TABLE_METHODS = [
    "put_item",
    "update_item",
    "delete_item",
    "batch_writer",
    "scan",
]
FORBIDDEN_ECR_METHODS = [
    "put_image",
    "batch_delete_image",
    "delete_repository",
    "tag_resource",
]
FORBIDDEN_S3_METHODS = [
    "put_object",
    "delete_object",
    "delete_objects",
    "copy_object",
]
FORBIDDEN_SQS_METHODS = [
    "receive_message",
    "delete_message",
    "send_message",
    "purge_queue",
    "start_message_move_task",  # the DLQ "redrive" op
]


class _RecordingClient:
    """Stub boto3 client that records allowed calls and raises on anything else.

    Allowed method names forward to a recorder and return a canned dict;
    any other attribute access raises ``AssertionError`` so an accidental
    non-allowlisted call fails loudly at the boto3 boundary.
    """

    def __init__(self, allowed: set[str], calls: list[str]) -> None:
        self._allowed = allowed
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        if name in self._allowed:

            def _op(**kwargs: Any) -> dict[str, Any]:
                self._calls.append(name)
                return {"_stub_op": name, "_kwargs": kwargs}

            return _op
        raise AssertionError(f"stub client received a non-allowlisted call: {name!r}")


class _RecordingTable:
    """Stub boto3 DynamoDB Table that records allowed calls, raises otherwise."""

    def __init__(self, name: str, allowed: set[str], calls: list[str]) -> None:
        self.name = name
        self._allowed = allowed
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        if name in self._allowed:

            def _op(**kwargs: Any) -> dict[str, Any]:
                self._calls.append(name)
                return {"_stub_op": name, "_kwargs": kwargs}

            return _op
        raise AssertionError(f"stub table received a non-allowlisted call: {name!r}")


class _RecordingResource:
    """Stub boto3 DynamoDB resource whose ``Table`` yields a recording table."""

    def __init__(self, allowed: set[str], calls: list[str]) -> None:
        self._allowed = allowed
        self._calls = calls
        self.tables_requested: list[str] = []

    def Table(self, name: str) -> _RecordingTable:  # boto3 API name
        self.tables_requested.append(name)
        return _RecordingTable(name, self._allowed, self._calls)


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def stubs(calls: list[str]) -> dict[str, Any]:
    return {
        "dynamodb_resource": _RecordingResource(ALLOWLISTED_DYNAMODB, calls),
        "ecr_client": _RecordingClient(ALLOWLISTED_ECR, calls),
        "s3_client": _RecordingClient(ALLOWLISTED_S3, calls),
        "sqs_client": _RecordingClient(ALLOWLISTED_SQS, calls),
    }


@pytest.fixture
def client(stubs: dict[str, Any]) -> ReadOnlyAwsClient:
    return ReadOnlyAwsClient(**stubs)


# --------------------------------------------------------------------------
# 1. Every public method invokes ONLY its allowlisted underlying operation.
# --------------------------------------------------------------------------


def test_dynamodb_resource_query_and_get_item_only(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    table.query(KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "p"})
    table.get_item(Key={"pk": "p", "sk": "s"})
    assert calls == ["query", "get_item"]
    assert set(calls) <= ALLOWLISTED_DYNAMODB


def test_ecr_methods_invoke_only_allowlisted_ops(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    client.describe_repositories(repositoryNames=["cairnproofs4af-artifact-cmh-alpha/proj/oscal"])
    client.batch_get_image(repositoryName="r", imageIds=[{"imageTag": "latest"}])
    assert calls == ["describe_repositories", "batch_get_image"]
    assert set(calls) <= ALLOWLISTED_ECR


def test_list_image_referrers_forwards_kwargs_and_returns_response(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    # The OCI Referrers read API: forwards its kwargs to the injected ECR
    # client stub and hands back that stub's response unchanged.
    response = client.list_image_referrers(
        repositoryName="cairnproofs4af-artifact-cmh-alpha/proj/oscal",
        imageId={"imageDigest": "sha256:abc123"},
    )
    assert calls == ["list_image_referrers"]
    assert set(calls) <= ALLOWLISTED_ECR
    assert response == {
        "_stub_op": "list_image_referrers",
        "_kwargs": {
            "repositoryName": "cairnproofs4af-artifact-cmh-alpha/proj/oscal",
            "imageId": {"imageDigest": "sha256:abc123"},
        },
    }


def test_readonly_invariant_holds_after_referrers_widening(
    client: ReadOnlyAwsClient,
) -> None:
    # Reaffirm the structural read-only invariant AFTER widening the allowlist
    # by the single OCI Referrers read op: no mutator or message-consuming op
    # is reachable on any surface, so reaching for one raises AttributeError.
    # The read-only DynamoDB Table exposes no mutator / message-consuming op.
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    for forbidden in ("put_item", "update_item", "delete_item", "batch_writer"):
        with pytest.raises(AttributeError):
            getattr(table, forbidden)
    # The client surface has no SQS consumer (receive_message / get_queue_url)
    # and no ECR tag-or-delete op — only the allowlisted reads are reachable.
    for forbidden in (
        "receive_message",
        "get_queue_url",
        "tag_resource",
        "batch_delete_image",
        "put_image",
    ):
        with pytest.raises(AttributeError):
            getattr(client, forbidden)


def test_s3_methods_invoke_only_allowlisted_ops(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    client.head_object(Bucket="b", Key="k")
    client.get_object(Bucket="b", Key="k")
    assert calls == ["head_object", "get_object"]
    assert set(calls) <= ALLOWLISTED_S3


def test_sqs_method_invokes_only_allowlisted_op(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    client.get_queue_attributes(QueueUrl="https://sqs/q", AttributeNames=["All"])
    assert calls == ["get_queue_attributes"]
    assert set(calls) <= ALLOWLISTED_SQS


def test_full_session_issues_only_allowlisted_operations(
    client: ReadOnlyAwsClient, calls: list[str]
) -> None:
    # Simulate a full TUI session: list, detail, control, DLQ health.
    client.describe_repositories()
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    table.query(KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "p"})
    table.get_item(Key={"pk": "p", "sk": "s"})
    client.batch_get_image(repositoryName="r", imageIds=[{"imageTag": "t"}])
    client.head_object(Bucket="b", Key="k")
    controls = client.dynamodb_resource.Table("cairnproofs4af-Controls-cmh-alpha")
    controls.query(KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": "c"})
    client.get_queue_attributes(
        QueueUrl="https://sqs/q", AttributeNames=["ApproximateNumberOfMessages"]
    )

    allowlist = ALLOWLISTED_DYNAMODB | ALLOWLISTED_ECR | ALLOWLISTED_S3 | ALLOWLISTED_SQS
    assert set(calls) <= allowlist
    # And nothing forbidden slipped through under any service.
    for forbidden in (
        FORBIDDEN_DYNAMODB_TABLE_METHODS
        + FORBIDDEN_ECR_METHODS
        + FORBIDDEN_S3_METHODS
        + FORBIDDEN_SQS_METHODS
    ):
        assert forbidden not in calls


# --------------------------------------------------------------------------
# 2. Structural absence: no reachable path to a non-allowlisted operation.
# --------------------------------------------------------------------------


def test_readonly_table_exposes_only_query_and_get_item(
    client: ReadOnlyAwsClient,
) -> None:
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    assert isinstance(table, _ReadOnlyTable)
    assert callable(table.query)
    assert callable(table.get_item)


@pytest.mark.parametrize("forbidden", FORBIDDEN_DYNAMODB_TABLE_METHODS)
def test_readonly_table_has_no_mutating_or_scan_method(
    client: ReadOnlyAwsClient, forbidden: str
) -> None:
    # PutItem in particular: structurally absent on the read-only table.
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    with pytest.raises(AttributeError):
        getattr(table, forbidden)


def test_readonly_table_put_item_is_unreachable(client: ReadOnlyAwsClient) -> None:
    # Explicit, named PutItem assertion (task requirement).
    table = client.dynamodb_resource.Table("cairnproofs4af-ArtifactMetadata-cmh-alpha")
    with pytest.raises(AttributeError):
        table.put_item  # noqa: B018 — attribute access is the assertion


def test_readonly_resource_exposes_only_table(client: ReadOnlyAwsClient) -> None:
    resource = client.dynamodb_resource
    assert isinstance(resource, _ReadOnlyResource)
    assert callable(resource.Table)
    # The resource cannot create/delete tables or batch-write through it.
    for forbidden in ("create_table", "batch_write_item", "delete_table"):
        with pytest.raises(AttributeError):
            getattr(resource, forbidden)


def test_client_has_no_generic_passthrough_or_dynamodb_client(
    client: ReadOnlyAwsClient,
) -> None:
    # No "issue op by name" dispatcher, no raw client/resource passthrough.
    for forbidden in ("client", "resource", "request", "make_api_call", "meta"):
        with pytest.raises(AttributeError):
            getattr(client, forbidden)


@pytest.mark.parametrize("forbidden", FORBIDDEN_ECR_METHODS)
def test_client_has_no_ecr_mutator(client: ReadOnlyAwsClient, forbidden: str) -> None:
    with pytest.raises(AttributeError):
        getattr(client, forbidden)


@pytest.mark.parametrize("forbidden", FORBIDDEN_S3_METHODS)
def test_client_has_no_s3_mutator(client: ReadOnlyAwsClient, forbidden: str) -> None:
    # DeleteObject in particular: structurally absent on the S3 surface.
    with pytest.raises(AttributeError):
        getattr(client, forbidden)


def test_client_delete_object_is_unreachable(client: ReadOnlyAwsClient) -> None:
    # Explicit, named DeleteObject assertion (task requirement).
    with pytest.raises(AttributeError):
        client.delete_object  # noqa: B018 — attribute access is the assertion


@pytest.mark.parametrize("forbidden", FORBIDDEN_SQS_METHODS)
def test_client_has_no_sqs_consumer_or_mutator(client: ReadOnlyAwsClient, forbidden: str) -> None:
    with pytest.raises(AttributeError):
        getattr(client, forbidden)


def test_client_receive_message_is_unreachable(client: ReadOnlyAwsClient) -> None:
    # Explicit, named ReceiveMessage assertion (task requirement) — the TUI
    # must never consume or alter a dead-letter-queue message.
    with pytest.raises(AttributeError):
        client.receive_message  # noqa: B018 — attribute access is the assertion


# --------------------------------------------------------------------------
# 3. Lazy real-client construction only when no stub is injected.
# --------------------------------------------------------------------------


def test_injected_clients_are_used_without_touching_boto3(
    monkeypatch: pytest.MonkeyPatch, stubs: dict[str, Any], calls: list[str]
) -> None:
    # If any boto3 constructor were called, this would blow up — proving the
    # injected stubs are used as-is and AWS is never contacted in unit tests.
    import cairn_cli.tui._readonly_client as mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("boto3 must not be constructed when stubs are injected")

    monkeypatch.setattr(mod.boto3, "client", _boom)
    monkeypatch.setattr(mod.boto3, "resource", _boom)

    ro = ReadOnlyAwsClient(**stubs)
    ro.describe_repositories()
    ro.get_object(Bucket="b", Key="k")
    ro.get_queue_attributes(QueueUrl="https://sqs/q")
    ro.dynamodb_resource.Table("t").query(KeyConditionExpression="pk = :pk")
    assert set(calls) <= (ALLOWLISTED_DYNAMODB | ALLOWLISTED_ECR | ALLOWLISTED_S3 | ALLOWLISTED_SQS)


def test_lazy_construction_uses_region_for_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn_cli.tui._readonly_client as mod

    constructed: list[tuple[str, str | None]] = []

    class _Dummy:
        def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
            return {}

        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            return {}

        def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
            return {}

        def Table(self, name: str) -> Any:  # boto3 API name
            return object()

    def _fake_client(service: str, region_name: str | None = None) -> _Dummy:
        constructed.append((service, region_name))
        return _Dummy()

    def _fake_resource(service: str, region_name: str | None = None) -> _Dummy:
        constructed.append((service, region_name))
        return _Dummy()

    monkeypatch.setattr(mod.boto3, "client", _fake_client)
    monkeypatch.setattr(mod.boto3, "resource", _fake_resource)

    ro = ReadOnlyAwsClient(region_name="us-east-2")
    ro.describe_repositories()
    ro.head_object(Bucket="b", Key="k")
    ro.get_queue_attributes(QueueUrl="https://sqs/q")
    _ = ro.dynamodb_resource

    assert ("ecr", "us-east-2") in constructed
    assert ("s3", "us-east-2") in constructed
    assert ("sqs", "us-east-2") in constructed
    assert ("dynamodb", "us-east-2") in constructed


def test_dynamodb_resource_property_is_memoized(client: ReadOnlyAwsClient) -> None:
    assert client.dynamodb_resource is client.dynamodb_resource
