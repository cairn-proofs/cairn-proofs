"""Tests for the Artifact_TUI data-source layer (``_sources.ArtifactSources``).

The whole point of this layer is the read-only invariant plus the
list/detail discipline and graceful DLQ-health degradation. The
tests use a *recording* stub that implements the ``ReadOnlyAwsClient`` public
surface and records every operation invoked, so we can assert structurally:

1. ``list_all_bundles`` issues only ``describe_repositories`` + ``query`` — no
   ``scan`` ever (the whole list rides on per-project key-condition queries).
2. Pagination is exhausted without truncation — ECR ``nextToken`` and the
   facade page ``next_token`` are both followed to the end; every item appears.
3. Detail reads are lazy — no ``batch_get_image`` / ``head_object`` before a
   bundle is selected; afterwards, only for the selected bundle.
4. DLQ health is counts-only — ``get_queue_attributes`` (+ maybe
   ``describe_repositories`` for the account id), never ``receive_message``;
   the four URLs are correct; graceful ``None`` when the account id is
   unresolved (zero repositories).
5. The control view queries the Controls table and returns the page items.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from typing import Any

import pytest
from cairn_cli._aws import DeploymentTarget
from cairn_cli.tui._sources import (
    NOTARY_SIGNATURE_ARTIFACT_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    RESOURCE_ARTIFACT_TYPE,
    WORM_RECEIPT_ARTIFACT_TYPE,
    ArtifactSources,
    BundleDetail,
    DlqDepth,
    ImageRef,
    Referrer,
    ReferrerListing,
    _normalize_pushed_at,
    classify_referrer,
    classify_unindexed_artifact,
)
from cairn_oci import (
    CONFIG_BLOB_MEDIA_TYPE,
    NOTARY_SIGNATURE_MEDIA_TYPE,
    OCI_EMPTY_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
)

REGION = "us-east-2"
ACCOUNT_ID = "123456789012"
# qualifier == cmh-alpha
TARGET = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofs4af")
QUALIFIER = "cmh-alpha"


# --------------------------------------------------------------------------
# Recording stub implementing the ReadOnlyAwsClient public surface.
# --------------------------------------------------------------------------


class _RecordingTable:
    """DynamoDB Table exposing ONLY query/get_item, recording each op.

    A ``scan`` (or any other) attribute is structurally absent — reaching for
    one raises ``AttributeError`` exactly as the real ``_ReadOnlyTable`` does,
    so a stray Scan in the source would fail loudly rather than be recorded.
    """

    def __init__(self, name: str, calls: list[str], pages_by_pk: dict[str, list[dict]]) -> None:
        self._name = name
        self._calls = calls
        # pages_by_pk: pk -> list of response dicts (each a DynamoDB Query page)
        self._pages_by_pk = pages_by_pk
        self._cursor: dict[str, int] = {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("query")
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        pages = self._pages_by_pk.get(pk, [{"Items": []}])
        # Advance through this pk's pages on each successive ExclusiveStartKey.
        idx = 0 if "ExclusiveStartKey" not in kwargs else self._cursor.get(pk, 0)
        page = pages[idx] if idx < len(pages) else {"Items": []}
        if "LastEvaluatedKey" in page:
            self._cursor[pk] = idx + 1
        return page

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._calls.append("get_item")
        return {}


class _RecordingResource:
    def __init__(
        self,
        calls: list[str],
        pages_by_pk: dict[str, list[dict]],
        registry_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._calls = calls
        self._pages_by_pk = pages_by_pk
        # id -> Identity_Registry item ({"id", "kind", "displayName", ...}); backs
        # the read-only batch_get_item the IdentityRegistry display-name join uses.
        self._registry_items = registry_items or {}

    def Table(self, name: str) -> _RecordingTable:  # boto3 API name
        return _RecordingTable(name, self._calls, self._pages_by_pk)

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Read-only BatchGetItem, mirroring the real _ReadOnlyResource surface.

        Resolves only the registered registry items; a write-side method
        (batch_write_item) is deliberately ABSENT, so the read-only invariant
        holds exactly as in production.
        """
        self._calls.append("batch_get_item")
        responses: dict[str, list[dict[str, Any]]] = {}
        for table_name, spec in kwargs.get("RequestItems", {}).items():
            rows = []
            for key in spec.get("Keys", []):
                item = self._registry_items.get(key.get("id"))
                if item is not None:
                    rows.append(item)
            responses[table_name] = rows
        return {"Responses": responses}


class _RecordingClient:
    """Stub matching the ReadOnlyAwsClient public surface; records every op.

    Anything outside the allowlisted methods is simply not defined on this
    class — calling it raises ``AttributeError`` (e.g. ``receive_message``),
    mirroring the structural absence in the real client.
    """

    def __init__(
        self,
        *,
        ecr_pages: list[dict],
        pages_by_pk: dict[str, list[dict]] | None = None,
        sqs_attributes: dict[str, Any] | None = None,
        images_by_repo: dict[str, list[dict]] | None = None,
        referrer_pages: list[dict] | None = None,
        manifests_by_digest: dict[str, str] | None = None,
        registry_items: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.queue_urls: list[str] = []
        self.referrer_subjects: list[dict[str, Any]] = []
        self._ecr_pages = ecr_pages
        self._ecr_cursor = 0
        self._resource = _RecordingResource(self.calls, pages_by_pk or {}, registry_items)
        self._sqs_attributes = sqs_attributes if sqs_attributes is not None else {}
        self._images_by_repo = images_by_repo or {}
        self._referrer_pages = referrer_pages or []
        # digest -> imageManifest JSON string, returned by batch_get_image so
        # the unindexed-classification path can be driven per image.
        self._manifests_by_digest = manifests_by_digest or {}

    @property
    def dynamodb_resource(self) -> _RecordingResource:
        return self._resource

    def describe_repositories(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_repositories")
        if "nextToken" in kwargs:
            # Resume from the page identified by the token (its index).
            idx = int(kwargs["nextToken"])
        else:
            idx = 0
        return self._ecr_pages[idx] if idx < len(self._ecr_pages) else {"repositories": []}

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_images")
        repo = kwargs.get("repositoryName")
        return {"imageDetails": list(self._images_by_repo.get(repo, []))}

    def batch_get_image(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("batch_get_image")
        digest = kwargs["imageIds"][0].get("imageDigest")
        image: dict[str, Any] = {"imageId": kwargs["imageIds"][0]}
        if digest in self._manifests_by_digest:
            image["imageManifest"] = self._manifests_by_digest[digest]
        return {"images": [image], "_kwargs": kwargs}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("head_object")
        return {"ContentLength": 123, "_kwargs": kwargs}

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("get_queue_attributes")
        self.queue_urls.append(kwargs["QueueUrl"])
        return {"Attributes": dict(self._sqs_attributes)}

    def list_image_referrers(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("list_image_referrers")
        self.referrer_subjects.append(kwargs.get("subjectId"))
        # Resume from the page identified by the token (its index); the page
        # carries its own nextToken when more pages follow.
        idx = int(kwargs["nextToken"]) if "nextToken" in kwargs else 0
        return self._referrer_pages[idx] if idx < len(self._referrer_pages) else {"referrers": []}


def _repo(name: str, *, registry_id: str = ACCOUNT_ID) -> dict[str, Any]:
    return {"repositoryName": name, "registryId": registry_id}


def _record(project_id: str, digest: str, **extra: Any) -> dict[str, Any]:
    return {"projectId": project_id, "manifestDigest": digest, **extra}


def _sources(client: _RecordingClient, **kwargs: Any) -> ArtifactSources:
    return ArtifactSources(target=TARGET, client=client, region=REGION, **kwargs)


# --------------------------------------------------------------------------
# 1. No Scan: list_all_bundles issues only describe_repositories + query.
# --------------------------------------------------------------------------


def test_list_all_bundles_issues_only_describe_and_query_never_scan() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": [_record("proj-a", "sha256:1")]}]},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert [b["manifestDigest"] for b in bundles] == ["sha256:1"]
    assert "scan" not in client.calls
    # The unvalidated-surfacing path adds per-repo DescribeImages image reads.
    assert set(client.calls) <= {"describe_repositories", "query", "describe_images"}


def test_list_all_bundles_with_no_matching_projects_issues_no_query() -> None:
    # A repo that does not match the grammar yields no project -> no query.
    client = _RecordingClient(ecr_pages=[{"repositories": [_repo("not-evidence/x/y")]}])
    sources = _sources(client)

    assert sources.list_all_bundles() == []
    assert "query" not in client.calls
    assert "scan" not in client.calls


# --------------------------------------------------------------------------
# 2. Pagination exhausted without truncation (ECR nextToken + facade tokens).
# --------------------------------------------------------------------------


def test_ecr_describe_repositories_pagination_is_exhausted() -> None:
    # Two ECR pages: first carries nextToken "1", second has no token. Each
    # page names a distinct project so both must be enumerated.
    client = _RecordingClient(
        ecr_pages=[
            {
                "repositories": [_repo(f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a")],
                "nextToken": "1",
            },
            {"repositories": [_repo(f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-b/proj-b")]},
        ],
        pages_by_pk={
            "proj-a": [{"Items": [_record("proj-a", "sha256:a")]}],
            "proj-b": [{"Items": [_record("proj-b", "sha256:b")]}],
        },
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()
    digests = {b["manifestDigest"] for b in bundles}

    # Both projects (one per ECR page) reached -> ECR token followed to the end.
    assert digests == {"sha256:a", "sha256:b"}
    assert client.calls.count("describe_repositories") == 2


def test_metadata_query_pagination_is_exhausted_without_truncation() -> None:
    # One project, two metadata pages: first has LastEvaluatedKey, second none.
    # ALL items across both pages must appear; none dropped.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={
            "proj-a": [
                {
                    "Items": [_record("proj-a", "sha256:1"), _record("proj-a", "sha256:2")],
                    "LastEvaluatedKey": {"pk": "proj-a", "sk": "sha256:2"},
                },
                {"Items": [_record("proj-a", "sha256:3")]},
            ]
        },
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()
    digests = sorted(b["manifestDigest"] for b in bundles)

    assert digests == ["sha256:1", "sha256:2", "sha256:3"]
    # Two query calls = two pages fetched (token followed to exhaustion).
    assert client.calls.count("query") == 2


# --------------------------------------------------------------------------
# 3. Lazy detail: no batch_get_image / head_object until get_bundle_detail.
# --------------------------------------------------------------------------


def test_detail_reads_are_lazy_and_scoped_to_selected_bundle() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    record = _record(
        "proj-a",
        "sha256:sel",
        registryId=ACCOUNT_ID,
        repositoryName=repo,
        archiveStatus="archived",
        s3Bucket="cairn-worm-bucket",
        s3KeyPrefix="proj-a/sha256/sel/",
    )
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": [record]}]},
    )
    sources = _sources(client)

    sources.list_all_bundles()
    # No detail reads issued during listing.
    assert "batch_get_image" not in client.calls
    assert "head_object" not in client.calls

    detail = sources.get_bundle_detail(record)

    assert isinstance(detail, BundleDetail)
    assert client.calls.count("batch_get_image") == 1
    assert client.calls.count("head_object") == 1
    # The manifest fetch targeted exactly the selected bundle's digest.
    assert detail.manifest is not None
    assert detail.manifest["_kwargs"]["imageIds"] == [{"imageDigest": "sha256:sel"}]
    # head_object derived {prefix}manifest.json from the trailing-slash prefix.
    assert detail.s3_head is not None
    assert detail.s3_head["_kwargs"]["Key"] == "proj-a/sha256/sel/manifest.json"


def test_detail_skips_s3_head_when_not_archived() -> None:
    record = _record(
        "proj-a",
        "sha256:x",
        registryId=ACCOUNT_ID,
        repositoryName="r",
        archiveStatus="pending",
        s3Bucket="b",
        s3KeyPrefix="p/",
    )
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    detail = sources.get_bundle_detail(record)

    assert detail.s3_head is None
    assert "head_object" not in client.calls
    # Manifest still fetched (the bundle exists in ECR).
    assert client.calls.count("batch_get_image") == 1


def test_detail_manifest_none_when_identifiers_missing() -> None:
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    detail = sources.get_bundle_detail({"projectId": "p"})  # no registryId/repo/digest

    assert detail.manifest is None
    assert detail.s3_head is None
    assert "batch_get_image" not in client.calls
    assert "head_object" not in client.calls


def test_detail_head_object_clienterror_degrades_to_none() -> None:
    from botocore.exceptions import ClientError

    record = _record(
        "proj-a",
        "sha256:x",
        registryId=ACCOUNT_ID,
        repositoryName="r",
        archiveStatus="archived",
        s3Bucket="b",
        s3KeyPrefix="p/",
    )
    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("head_object")
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    client.head_object = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    detail = sources.get_bundle_detail(record)

    assert detail.s3_head is None  # 404 tolerated, not raised


def test_detail_degrades_both_manifest_and_s3_head_to_none_on_clienterror() -> None:
    # Regression guard — the lazy detail path degrades a failing
    # ECR manifest fetch AND a failing S3 head fetch to None without raising,
    # exactly as today. Guards _sources.py:236-265. The existing head_object
    # test covers only s3_head; this
    # asserts BOTH manifest and s3_head degrade together when both calls raise.
    from botocore.exceptions import ClientError

    record = _record(
        "proj-a",
        "sha256:x",
        registryId=ACCOUNT_ID,
        repositoryName="r",
        archiveStatus="archived",
        s3Bucket="b",
        s3KeyPrefix="p/",
    )
    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom_image(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "BatchGetImage")

    def _boom_head(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("head_object")
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    client.batch_get_image = _boom_image  # type: ignore[method-assign]
    client.head_object = _boom_head  # type: ignore[method-assign]
    sources = _sources(client)

    detail = sources.get_bundle_detail(record)

    assert isinstance(detail, BundleDetail)
    assert detail.manifest is None  # ECR error tolerated, not raised
    assert detail.s3_head is None  # S3 error tolerated, not raised


# --------------------------------------------------------------------------
# 4. DLQ health: counts-only, correct URLs, graceful None.
# --------------------------------------------------------------------------


def test_dlq_health_issues_only_get_queue_attributes_never_receive_message() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        sqs_attributes={"ApproximateNumberOfMessages": "7"},
    )
    sources = _sources(client)
    # Prime the account-id cache via the normal list path.
    sources.list_all_bundles()

    depths = sources.dlq_health()

    assert "receive_message" not in client.calls
    assert client.calls.count("get_queue_attributes") == 4
    assert all(isinstance(d, DlqDepth) for d in depths)
    assert [d.stage for d in depths] == ["validate", "sign", "archive", "metadata"]
    assert all(d.approximate_messages == 7 for d in depths)

    expected_names = [
        f"cairnproofs4af-validate-bundle-dlq-{QUALIFIER}",
        f"cairnproofs4af-sign-bundle-dlq-{QUALIFIER}",
        f"cairnproofs4af-archive-to-s3-dlq-{QUALIFIER}",
        f"cairnproofs4af-update-metadata-dlq-{QUALIFIER}",
    ]
    assert [d.queue_name for d in depths] == expected_names
    assert client.queue_urls == [
        f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT_ID}/{name}" for name in expected_names
    ]


def test_dlq_health_resolves_account_id_via_describe_when_not_cached() -> None:
    # dlq_health called WITHOUT a prior list_all_bundles -> it resolves the
    # account id by issuing describe_repositories itself.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        sqs_attributes={"ApproximateNumberOfMessages": "0"},
    )
    sources = _sources(client)

    depths = sources.dlq_health()

    assert "describe_repositories" in client.calls
    assert "receive_message" not in client.calls
    assert all(d.approximate_messages == 0 for d in depths)


def test_dlq_health_degrades_to_none_when_account_id_unresolved() -> None:
    # Zero repositories -> no registryId -> no account id -> no queue URL built,
    # no get_queue_attributes issued, every depth None. Never raises.
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    depths = sources.dlq_health()

    assert len(depths) == 4
    assert all(d.approximate_messages is None for d in depths)
    assert "get_queue_attributes" not in client.calls
    assert "receive_message" not in client.calls


def test_dlq_health_names_are_derived_from_deployment_target_not_stale_literals() -> None:
    # T-13 regression: the TUI's DLQ names MUST be derived through
    # DeploymentTarget.dlq_name(resource) (the CDK nameFor() mirror), NOT
    # hardcoded literals that silently rot when appName/qualifier change. We
    # prove derivation two ways: (1) every resolved queue_name equals the
    # DeploymentTarget composition for the same resource, and (2) a target with a
    # DIFFERENT appName produces names that track it (a stale literal would not).
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        sqs_attributes={"ApproximateNumberOfMessages": "0"},
    )
    sources = _sources(client)
    sources.list_all_bundles()  # prime account-id cache

    depths = sources.dlq_health()
    resources = [
        "validate-bundle-dlq",
        "sign-bundle-dlq",
        "archive-to-s3-dlq",
        "update-metadata-dlq",
    ]
    assert [d.queue_name for d in depths] == [TARGET.dlq_name(r) for r in resources]
    # Each derived name carries the deployed appName + qualifier and the new
    # `-bundle-dlq` / `-to-s3-dlq` scheme (not the pre-rename `evidence` form).
    for name in (d.queue_name for d in depths):
        assert name.startswith("cairnproofs4af-")
        assert name.endswith(f"-{QUALIFIER}")
        assert "evidence" not in name

    # Derivation proof: a deployment under a different appName yields names that
    # track it — a stale literal in _sources would fail this.
    other = DeploymentTarget(stage="alpha", region_code="cmh", app_name="cairnproofszzz")
    other_client = _RecordingClient(
        ecr_pages=[
            {"repositories": [_repo(f"cairnproofszzz-artifact-{QUALIFIER}/org-a/proj-a/proj-a")]}
        ],
        sqs_attributes={"ApproximateNumberOfMessages": "0"},
    )
    other_sources = ArtifactSources(target=other, client=other_client, region=REGION)
    other_sources.list_all_bundles()
    other_depths = other_sources.dlq_health()
    assert [d.queue_name for d in other_depths] == [other.dlq_name(r) for r in resources]
    assert all(d.queue_name.startswith("cairnproofszzz-") for d in other_depths)


# --------------------------------------------------------------------------
# 5. Control view: query the Controls table, return the page items.
# --------------------------------------------------------------------------


def test_list_control_bundles_queries_controls_table_and_returns_items() -> None:
    uuid = "11111111-1111-1111-1111-111111111111"
    tenant_id = "tnt-a"
    # The control-claims projection is a tenant-PK plane: the partition key is
    # the exact-match {tenant-id}, NOT the {uuid}#{controlId} composite (that is
    # now the sort-key prefix).
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        pages_by_pk={
            tenant_id: [{"Items": [_record("proj-a", "sha256:c1"), _record("proj-b", "sha256:c2")]}]
        },
    )
    sources = _sources(client)

    page = sources.list_control_bundles(
        tenant_id=tenant_id, catalog_or_profile_uuid=uuid, control_id="ac-2"
    )

    assert client.calls.count("query") == 1
    assert "scan" not in client.calls
    assert [item["manifestDigest"] for item in page.items] == ["sha256:c1", "sha256:c2"]
    assert page.next_token is None


def test_list_project_bundles_returns_single_page_for_caller_paging() -> None:
    repo_pk = "proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        pages_by_pk={
            repo_pk: [
                {
                    "Items": [_record("proj-a", "sha256:p1")],
                    "LastEvaluatedKey": {"pk": repo_pk, "sk": "sha256:p1"},
                },
                {"Items": [_record("proj-a", "sha256:p2")]},
            ]
        },
    )
    sources = _sources(client)

    page = sources.list_project_bundles(tenant_id="proj-a", project_id="proj-a")

    # Single page only — caller drives the next page via the token.
    assert client.calls.count("query") == 1
    assert [item["manifestDigest"] for item in page.items] == ["sha256:p1"]
    assert page.next_token is not None


# --------------------------------------------------------------------------
# 5b. Identity_Registry display-name join (read-time, [IDREG-001]/[TUI-002.04]).
# --------------------------------------------------------------------------


def _registry_item(opaque_id: str, kind: str, display_name: str, **extra: Any) -> dict[str, Any]:
    return {"id": opaque_id, "kind": kind, "displayName": display_name, **extra}


def test_resolve_identity_names_joins_opaque_ids_to_display_names() -> None:
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        registry_items={
            "org-a": _registry_item("org-a", "org", "Acme Federal"),
            "tnt-a": _registry_item("tnt-a", "tenant", "Defense BU", orgId="org-a"),
            "proj-a": _registry_item("proj-a", "project", "Payments API"),
        },
    )
    sources = _sources(client)

    resolved = sources.resolve_identity_names(["org-a", "tnt-a", "proj-a", "unknown-id"])

    # Known ids resolve; an unknown id is simply absent (caller renders the id).
    assert resolved == {
        "org-a": "Acme Federal",
        "tnt-a": "Defense BU",
        "proj-a": "Payments API",
    }
    # The join rides the read-only BatchGetItem — never a Scan or a write.
    assert "batch_get_item" in client.calls
    assert "scan" not in client.calls


def test_resolve_identity_names_degrades_to_empty_on_read_error() -> None:
    from botocore.exceptions import ClientError

    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "BatchGetItem")

    client._resource.batch_get_item = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    # A registry read failure costs only display names — never raises, so the
    # list still renders the opaque ids ([IDREG-001.03]).
    assert sources.resolve_identity_names(["org-a"]) == {}


def test_resolve_identity_names_empty_input_issues_no_read() -> None:
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    assert sources.resolve_identity_names([]) == {}
    assert "batch_get_item" not in client.calls


def test_recording_table_has_no_scan_attribute() -> None:
    # Sanity: the stub mirrors the real read-only table's structural absence of
    # scan, so a stray Scan in _sources would fail loudly rather than pass.
    table = _RecordingTable("t", [], {})
    with pytest.raises(AttributeError):
        table.scan  # noqa: B018 — attribute access is the assertion


# --------------------------------------------------------------------------
# 6. Referrer fetch + classification.
# --------------------------------------------------------------------------


def _referrer_record(**extra: Any) -> dict[str, Any]:
    return _record(
        "proj-a",
        "sha256:subj",
        registryId=ACCOUNT_ID,
        repositoryName=f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a",
        **extra,
    )


def test_classify_referrer_signature_receipt_resource_and_counterfeit_other() -> None:
    # Classification is an exact string match against the three pinned constants;
    # everything else — including a counterfeit string that merely looks like a
    # type — is "other". (A string match, not a verification.) All
    # THREE pipeline-minted referrer types classify (resource-ref included).
    assert classify_referrer(NOTARY_SIGNATURE_ARTIFACT_TYPE) == "signature"
    assert classify_referrer(WORM_RECEIPT_ARTIFACT_TYPE) == "worm-receipt"
    assert classify_referrer(RESOURCE_ARTIFACT_TYPE) == "resource-ref"
    assert classify_referrer("application/vnd.cncf.notary.signature.FAKE") == "other"
    assert classify_referrer("application/vnd.cairnproofs.artifact.resource.v1.FAKE") == "other"
    assert classify_referrer("application/vnd.example.thing") == "other"
    assert classify_referrer("") == "other"


def test_get_bundle_referrers_exhausts_nexttoken_surfacing_every_referrer() -> None:
    # Two referrer pages: first carries nextToken "1", second none. Every
    # referrer across both pages must surface (no truncation), classified.
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        referrer_pages=[
            {
                "referrers": [
                    {
                        "artifactType": NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:sig",
                        "imageTags": ["sig-tag"],
                        "annotations": {"k": "v"},
                    }
                ],
                "nextToken": "1",
            },
            {
                "referrers": [
                    {
                        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
                        "digest": "sha256:rcpt",
                    }
                ]
            },
        ],
    )
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    assert isinstance(listing, ReferrerListing)
    assert listing.available is True
    assert client.calls.count("list_image_referrers") == 2
    # Subject is the bundle's manifest digest on every page.
    assert client.referrer_subjects == [{"imageDigest": "sha256:subj"}] * 2
    assert [r.digest for r in listing.referrers] == ["sha256:sig", "sha256:rcpt"]
    assert [classify_referrer(r.artifact_type) for r in listing.referrers] == [
        "signature",
        "worm-receipt",
    ]
    # imageDigest preferred, imageTags[0] surfaced, digest fallback honored.
    sig = listing.referrers[0]
    assert sig.tag == "sig-tag"
    assert sig.annotations == {"k": "v"}
    rcpt = listing.referrers[1]
    assert rcpt.tag is None
    assert rcpt.annotations == {}


def test_get_bundle_referrers_empty_read_is_available_true_empty_list() -> None:
    # A successful read of zero referrers: available=True, empty list — the
    # "no referrers" case, distinct from "unavailable".
    client = _RecordingClient(ecr_pages=[{"repositories": []}], referrer_pages=[{"referrers": []}])
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    assert listing.available is True
    assert listing.referrers == []


def test_get_bundle_referrers_access_denied_is_unavailable_no_exception() -> None:
    # An AccessDeniedException (principal lacks ecr:ListImageReferrers) yields
    # available=False with NO exception escaping — distinct from empty.
    from botocore.exceptions import ClientError

    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("list_image_referrers")
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListImageReferrers")

    client.list_image_referrers = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    assert listing.available is False
    assert listing.referrers == []


def test_get_bundle_referrers_transport_error_is_unavailable_no_exception() -> None:
    # A BotoCoreError (transport-level failure) is also caught -> unavailable.
    from botocore.exceptions import EndpointConnectionError

    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("list_image_referrers")
        raise EndpointConnectionError(endpoint_url="https://ecr")

    client.list_image_referrers = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    assert listing.available is False


def test_get_bundle_referrers_missing_identifiers_is_unavailable_no_aws_call() -> None:
    # A record missing registryId/repositoryName/manifestDigest is treated as
    # unavailable WITHOUT calling AWS with None.
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    listing = sources.get_bundle_referrers({"projectId": "proj-a"})

    assert listing.available is False
    assert listing.referrers == []
    assert "list_image_referrers" not in client.calls


def test_get_bundle_referrers_issues_only_list_image_referrers_no_scan_or_mutator() -> None:
    # Allowlist assertion: building the referrer listing issues ONLY
    # list_image_referrers — no scan, no mutator, no message-consuming call.
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        referrer_pages=[
            {"referrers": [{"artifactType": WORM_RECEIPT_ARTIFACT_TYPE, "imageDigest": "sha256:r"}]}
        ],
    )
    sources = _sources(client)

    sources.get_bundle_referrers(_referrer_record())

    assert set(client.calls) == {"list_image_referrers"}
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls


def test_tui_worm_receipt_constant_matches_archive_lambda_literal() -> None:
    # Drift guard: the TUI-local WORM-receipt artifactType
    # constant MUST equal the archive Lambda's canonical literal. Tests may
    # cross the libs/lambdas boundary; the SOURCE must not.
    from cairn_lambda_archive_to_s3.receipt import RECEIPT_ARTIFACT_TYPE

    assert WORM_RECEIPT_ARTIFACT_TYPE == RECEIPT_ARTIFACT_TYPE


def test_referrer_dataclass_shape() -> None:
    # Sanity on the public Referrer surface the view (wave 3) will import.
    ref = Referrer(
        artifact_type=NOTARY_SIGNATURE_ARTIFACT_TYPE,
        digest="sha256:x",
        tag="t",
        annotations={"a": "b"},
    )
    assert ref.artifact_type == NOTARY_SIGNATURE_ARTIFACT_TYPE
    assert ref.digest == "sha256:x"
    assert ref.tag == "t"
    assert ref.annotations == {"a": "b"}


def test_get_bundle_referrers_malformed_entry_does_not_raise() -> None:
    # Edge case: a Referrers API entry that omits BOTH digest keys
    # (imageDigest/digest) AND artifactType, and supplies no imageTags or
    # annotations, must NOT raise — _build_referrer fills render-safe empties so
    # the fetch keeps surfacing the read instead of crashing the session. A
    # well-formed sibling entry on the same page must still surface intact.
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        referrer_pages=[
            {
                "referrers": [
                    {},  # wholly empty entry
                    {"artifactType": None, "imageDigest": None, "imageTags": None},
                    {
                        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
                        "imageDigest": "sha256:good",
                    },
                ]
            }
        ],
    )
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    assert listing.available is True
    assert len(listing.referrers) == 3
    # The malformed entries degrade to empty strings / None, never raising.
    empty, nulled, good = listing.referrers
    assert empty.artifact_type == ""
    assert empty.digest == ""
    assert empty.tag is None
    assert empty.annotations == {}
    assert nulled.artifact_type == ""
    assert nulled.digest == ""
    assert nulled.tag is None
    # The well-formed sibling on the same page is unaffected.
    assert good.digest == "sha256:good"
    assert classify_referrer(good.artifact_type) == "worm-receipt"


def test_get_bundle_referrers_non_dict_entry_is_skipped_not_raised() -> None:
    # Edge case: a malformed/hostile response whose ``referrers`` list contains a
    # NON-dict element (None, a string, a list) must be skipped, NOT crash —
    # ``_build_referrer``'s ``.get`` would raise AttributeError on a non-dict and
    # escape the degradation boundary (no exception ever escapes). A
    # non-mapping ``annotations`` must likewise not make ``dict(...)`` raise.
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        referrer_pages=[
            {
                "referrers": [
                    None,  # non-dict entries — must be skipped
                    "not-a-dict",
                    ["also", "not", "a", "dict"],
                    {
                        "artifactType": NOTARY_SIGNATURE_ARTIFACT_TYPE,
                        "imageDigest": "sha256:sig",
                        "annotations": ["not", "a", "mapping"],  # non-dict annotations
                    },
                ]
            }
        ],
    )
    sources = _sources(client)

    listing = sources.get_bundle_referrers(_referrer_record())

    # Read succeeded (available) and only the one well-formed entry surfaced;
    # the three non-dict entries were skipped and nothing raised.
    assert listing.available is True
    assert len(listing.referrers) == 1
    only = listing.referrers[0]
    assert only.digest == "sha256:sig"
    assert classify_referrer(only.artifact_type) == "signature"
    assert only.annotations == {}  # non-mapping annotations degraded to empty


def test_get_bundle_referrers_handles_referrers_key_absent_or_null() -> None:
    # A success response whose body omits the ``referrers`` key entirely, or
    # carries it as ``None``, is treated as zero referrers (available=True,
    # empty) — the ``response.get("referrers") or []`` guard must not raise.
    for body in ({"nextToken": None}, {"referrers": None}):
        client = _RecordingClient(ecr_pages=[{"repositories": []}], referrer_pages=[dict(body)])
        sources = _sources(client)

        listing = sources.get_bundle_referrers(_referrer_record())

        assert listing.available is True
        assert listing.referrers == []


# --------------------------------------------------------------------------
# 7. Unindexed-artifact classification — the data layer labels each
#    ECR-present-but-unindexed image by reading its manifest.
# --------------------------------------------------------------------------


def _signature_manifest() -> dict[str, Any]:
    # A Notation signature: config.mediaType is the notary signature type.
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": NOTARY_SIGNATURE_MEDIA_TYPE},
    }


def _worm_receipt_manifest() -> dict[str, Any]:
    # A WORM_Receipt: top-level artifactType is the receipt const AND the config
    # is oci-empty — so order matters (receipt must win over the oci-empty rule).
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }


def _oscal_resource_manifest() -> dict[str, Any]:
    # An OSCAL-resource referrer: top-level artifactType is the resource const
    # AND the config is oci-empty — so order matters (the artifactType check must
    # win over the oci-empty rule, or it mis-buckets as "index").
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": RESOURCE_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }


def _attestation_referrer_manifest() -> dict[str, Any]:
    # The producer-claim Producer_Attestation_Referrer: pushed via `oras attach`, so its
    # top-level artifactType is the attestation const AND its config is the
    # oci-empty scratch config — order matters (the artifactType check must win
    # over the oci-empty rule, or it mis-buckets as "index" and leaves its own
    # signature dangling as "unattached").
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": PRODUCER_ATTESTATION_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }


def _image_index_manifest() -> dict[str, Any]:
    return {"mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE, "manifests": []}


def _oci_empty_manifest() -> dict[str, Any]:
    # An oci-empty config that is NOT a receipt -> generic index-like placeholder.
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }


def _producer_bundle_manifest() -> dict[str, Any]:
    # A genuine producer Artifact_Bundle: config.mediaType is the Cairn artifact
    # Config_Blob type -> classify_pipeline_internal returns None -> did-not-land.
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": CONFIG_BLOB_MEDIA_TYPE},
    }


def test_classify_unindexed_signature_from_config_media_type() -> None:
    assert classify_unindexed_artifact(_signature_manifest()) == "signature"


def test_classify_unindexed_worm_receipt_wins_over_oci_empty_order() -> None:
    # The receipt has an oci-empty config; the top-level artifactType check MUST
    # run first or it would be mis-bucketed as "index" (order proof).
    assert classify_unindexed_artifact(_worm_receipt_manifest()) == "worm-receipt"


def test_classify_unindexed_oscal_resource_wins_over_oci_empty_not_index() -> None:
    # The OSCAL-resource referrer has an oci-empty config; the top-level
    # artifactType check MUST run first or it would be mis-bucketed as "index"
    # (guards against the index mislabel).
    assert classify_unindexed_artifact(_oscal_resource_manifest()) == "resource-ref"


def test_classify_unindexed_attestation_wins_over_oci_empty_not_index() -> None:
    # The Producer_Attestation_Referrer (oras attach → oci-empty config) MUST be matched by
    # its top-level artifactType BEFORE the oci-empty fallthrough, or it
    # mis-buckets as "index" (hidden) and its Notation signature is stranded as
    # "unattached".
    assert classify_unindexed_artifact(_attestation_referrer_manifest()) == "attestation"


def test_classify_referrer_recognizes_attestation_type() -> None:
    assert classify_referrer(PRODUCER_ATTESTATION_ARTIFACT_TYPE) == "attestation"


def test_classify_unindexed_image_index_from_top_level_media_type() -> None:
    assert classify_unindexed_artifact(_image_index_manifest()) == "index"


def test_classify_unindexed_generic_oci_empty_is_index() -> None:
    assert classify_unindexed_artifact(_oci_empty_manifest()) == "index"


def test_classify_unindexed_producer_config_blob_is_unvalidated_bundle() -> None:
    # No pipeline-internal media type matched and no receipt artifactType ->
    # a producer bundle that did not complete the pipeline.
    assert classify_unindexed_artifact(_producer_bundle_manifest()) == "unvalidated bundle"


def test_unvalidated_entry_attaches_classification_from_manifest_read() -> None:
    # Integration: an ECR image with no Metadata_Index row gets a synthetic
    # entry whose `classification` comes from a batch_get_image manifest read,
    # while imageTag/manifestDigest/status remain on the entry.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        # No metadata rows for proj-a -> the ECR image is unindexed.
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:sig", "imageTags": ["sig-tag"]}]},
        manifests_by_digest={"sha256:sig": json.dumps(_signature_manifest())},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert len(bundles) == 1
    entry = bundles[0]
    assert entry["status"] == "unvalidated"
    assert entry["classification"] == "signature"
    assert entry["imageTag"] == "sig-tag"
    assert entry["manifestDigest"] == "sha256:sig"
    # The manifest read targeted exactly the unindexed digest.
    assert client.calls.count("batch_get_image") == 1


def test_unvalidated_entry_classification_degrades_to_unclassified_on_error() -> None:
    # A batch_get_image raising ClientError yields classification "unclassified"
    # WITHOUT raising and WITHOUT dropping a sibling unindexed entry whose own
    # manifest read succeeds.
    from botocore.exceptions import ClientError

    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={
            repo: [
                {"imageDigest": "sha256:boom", "imageTags": ["boom-tag"]},
                {"imageDigest": "sha256:ok", "imageTags": ["ok-tag"]},
            ]
        },
        manifests_by_digest={"sha256:ok": json.dumps(_signature_manifest())},
    )

    real_batch = client.batch_get_image

    def _maybe_boom(**kwargs: Any) -> dict[str, Any]:
        if kwargs["imageIds"][0].get("imageDigest") == "sha256:boom":
            client.calls.append("batch_get_image")
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "BatchGetImage")
        return real_batch(**kwargs)

    client.batch_get_image = _maybe_boom  # type: ignore[method-assign]
    sources = _sources(client)

    bundles = sources.list_all_bundles()
    by_digest = {b["manifestDigest"]: b for b in bundles}

    # The failing image degrades to "unclassified"; the sibling still surfaces.
    assert by_digest["sha256:boom"]["classification"] == "unclassified"
    assert by_digest["sha256:ok"]["classification"] == "signature"
    assert set(by_digest) == {"sha256:boom", "sha256:ok"}


def test_unvalidated_entry_classification_unclassified_on_unparseable_manifest() -> None:
    # A manifest that is not valid JSON degrades that one entry to
    # "unclassified" without raising.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:bad", "imageTags": ["bad-tag"]}]},
        manifests_by_digest={"sha256:bad": "{not valid json"},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert len(bundles) == 1
    assert bundles[0]["classification"] == "unclassified"


def test_all_bundles_classification_issues_only_allowlisted_reads_no_scan() -> None:
    # Allowlist assertion: building the all-bundles list WITH classification
    # issues only describe_repositories / describe_images / batch_get_image /
    # query — no scan, no mutator, no message-consuming call.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:u", "imageTags": ["u-tag"]}]},
        manifests_by_digest={"sha256:u": json.dumps(_producer_bundle_manifest())},
    )
    sources = _sources(client)

    sources.list_all_bundles()

    assert set(client.calls) <= {
        "describe_repositories",
        "describe_images",
        "batch_get_image",
        "query",
    }
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls


# --------------------------------------------------------------------------
# 7b. Classification precedence edge cases — pin the exact ordering between
#     image-index, oci-empty, signature, and the receipt artifactType so the
#     label cannot drift from classify_pipeline_internal.
# --------------------------------------------------------------------------


def test_classify_unindexed_image_index_wins_over_config_signature() -> None:
    # A top-level image-index mediaType is classified IMAGE_INDEX by
    # classify_pipeline_internal REGARDLESS of any config it carries (its own
    # documented precedence). So even a signature config.mediaType under an
    # image-index manifest must label "index", never "signature".
    manifest = {
        "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
        "config": {"mediaType": NOTARY_SIGNATURE_MEDIA_TYPE},
        "manifests": [],
    }
    assert classify_unindexed_artifact(manifest) == "index"


def test_classify_unindexed_receipt_wins_over_image_index_media_type() -> None:
    # Receipt-first ordering: classify_unindexed_artifact checks the top-level
    # artifactType BEFORE delegating to classify_pipeline_internal. So a manifest
    # that is BOTH a receipt (artifactType) AND an image-index (mediaType) must
    # label "worm-receipt" — the receipt identity wins per design.
    manifest = {
        "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
        "manifests": [],
    }
    assert classify_unindexed_artifact(manifest) == "worm-receipt"


# --------------------------------------------------------------------------
# 7c. _classify_unindexed boundary degradation — odd batch_get_image response
#     shapes must degrade to "unclassified" without IndexError/KeyError, never
#     drop sibling entries, and never raise (spec: remain interactive).
# --------------------------------------------------------------------------


_MISSING = object()


def _unindexed_repo_client(images_payload: dict[str, Any]) -> _RecordingClient:
    """A client with one unindexed image (digest ``sha256:x``) whose
    batch_get_image returns ``images_payload`` verbatim."""
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:x", "imageTags": ["x-tag"]}]},
    )

    def _override(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        return images_payload

    client.batch_get_image = _override  # type: ignore[method-assign]
    return client


def test_classify_unindexed_empty_images_degrades_to_unclassified() -> None:
    # batch_get_image returns no images (delete race between enumeration and read).
    client = _unindexed_repo_client({"images": []})
    bundles = _sources(client).list_all_bundles()
    assert len(bundles) == 1
    assert bundles[0]["classification"] == "unclassified"


def test_classify_unindexed_missing_image_manifest_key_degrades() -> None:
    # images[0] present but has no imageManifest key -> json.loads(None) -> TypeError.
    client = _unindexed_repo_client({"images": [{"imageId": {}}]})
    bundles = _sources(client).list_all_bundles()
    assert bundles[0]["classification"] == "unclassified"


def test_classify_unindexed_manifest_json_is_not_a_dict_degrades() -> None:
    # imageManifest parses to valid JSON that is a list, not a dict.
    client = _unindexed_repo_client({"images": [{"imageManifest": json.dumps([1, 2, 3])}]})
    bundles = _sources(client).list_all_bundles()
    assert bundles[0]["classification"] == "unclassified"


def test_classify_unindexed_manifest_json_is_scalar_degrades() -> None:
    # imageManifest parses to a bare scalar (number) -> isinstance(dict) guard.
    client = _unindexed_repo_client({"images": [{"imageManifest": json.dumps(5)}]})
    bundles = _sources(client).list_all_bundles()
    assert bundles[0]["classification"] == "unclassified"


def test_classify_unindexed_non_dict_images_element_degrades() -> None:
    # A hostile/malformed batch_get_image whose images[0] is a string, not a
    # dict, must NOT raise an uncaught AttributeError out of the list build:
    # _classify_unindexed guards `isinstance(images[0], dict)` and degrades to
    # "unclassified" (spec: manifest-read failure stays interactive, never
    # raises). Regression guard for that boundary.
    client = _unindexed_repo_client({"images": ["not-a-dict"]})
    bundles = _sources(client).list_all_bundles()
    assert bundles[0]["classification"] == "unclassified"


# --------------------------------------------------------------------------
# 7d. Mixed repo — a validated bundle (Metadata_Index row) coexisting with
#     several unindexed artifacts. Validated bundle keeps its stored data and
#     is NOT manifest-read; each unindexed digest is read exactly once and gets
#     its own classification (spec: validated bundles stay sourced from the
#     Metadata_Index; no re-read of validated bundles).
# --------------------------------------------------------------------------


def test_all_bundles_mixed_validated_and_unindexed_classifications() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    # sha256:v is the validated bundle (has a Metadata_Index row); the other
    # four digests are unindexed and each classifies differently.
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={
            "proj-a": [
                {"Items": [_record("proj-a", "sha256:v", artifactKind="OSCAL Assessment Results")]}
            ]
        },
        images_by_repo={
            repo: [
                {"imageDigest": "sha256:v", "imageTags": ["v-tag"]},
                {"imageDigest": "sha256:sig", "imageTags": ["sig-tag"]},
                {"imageDigest": "sha256:rcpt", "imageTags": ["rcpt-tag"]},
                {"imageDigest": "sha256:idx", "imageTags": ["idx-tag"]},
                {"imageDigest": "sha256:prod", "imageTags": ["prod-tag"]},
            ]
        },
        manifests_by_digest={
            "sha256:sig": json.dumps(_signature_manifest()),
            "sha256:rcpt": json.dumps(_worm_receipt_manifest()),
            "sha256:idx": json.dumps(_image_index_manifest()),
            "sha256:prod": json.dumps(_producer_bundle_manifest()),
        },
    )

    # Capture every digest passed to batch_get_image to prove the validated
    # bundle's digest is never manifest-read.
    read_digests: list[str] = []
    real_batch = client.batch_get_image

    def _capturing(**kwargs: Any) -> dict[str, Any]:
        read_digests.append(kwargs["imageIds"][0]["imageDigest"])
        return real_batch(**kwargs)

    client.batch_get_image = _capturing  # type: ignore[method-assign]
    sources = _sources(client)

    bundles = sources.list_all_bundles()
    by_digest = {b["manifestDigest"]: b for b in bundles}

    # All five surface; no duplication of the validated bundle.
    assert set(by_digest) == {"sha256:v", "sha256:sig", "sha256:rcpt", "sha256:idx", "sha256:prod"}

    # The validated bundle keeps its Metadata_Index data and carries NO
    # classification field (it is not re-classified by a manifest read).
    validated = by_digest["sha256:v"]
    assert validated.get("status") != "unvalidated"
    assert validated["artifactKind"] == "OSCAL Assessment Results"
    assert "classification" not in validated

    # Each unindexed entry got its own content-derived classification.
    assert by_digest["sha256:sig"]["classification"] == "signature"
    assert by_digest["sha256:rcpt"]["classification"] == "worm-receipt"
    assert by_digest["sha256:idx"]["classification"] == "index"
    assert by_digest["sha256:prod"]["classification"] == "unvalidated bundle"

    # Exactly the four unindexed digests were manifest-read — the validated
    # bundle's digest was NOT passed to batch_get_image.
    assert sorted(read_digests) == ["sha256:idx", "sha256:prod", "sha256:rcpt", "sha256:sig"]
    assert "sha256:v" not in read_digests
    assert client.calls.count("batch_get_image") == 4


# --------------------------------------------------------------------------
# 8. subjectDigest extraction onto unindexed entries.
#    The referrer's subject digest, read off the SAME manifest used to classify
#    it, drives grouping under the primary. A missing/malformed subject
#    degrades to None (the entry stays a top-level row) and NEVER raises.
# --------------------------------------------------------------------------


def _referrer_manifest_with_subject(subject_digest: str) -> dict[str, Any]:
    # A WORM_Receipt referrer manifest carrying an OCI ``subject`` pointer at its
    # primary — exactly the shape the grouping key is read from.
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
        "subject": {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": subject_digest,
        },
    }


def test_unvalidated_entry_extracts_subject_digest_from_manifest() -> None:
    # A referrer image's synthetic entry carries subjectDigest read off the SAME
    # batch_get_image manifest used to classify it (no extra AWS call).
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:rcpt", "imageTags": ["rcpt-tag"]}]},
        manifests_by_digest={
            "sha256:rcpt": json.dumps(_referrer_manifest_with_subject("sha256:primary"))
        },
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert len(bundles) == 1
    entry = bundles[0]
    assert entry["classification"] == "worm-receipt"
    assert entry["subjectDigest"] == "sha256:primary"
    # The subject came free off the classification read — one batch_get_image.
    assert client.calls.count("batch_get_image") == 1


def test_unvalidated_entry_subject_digest_none_when_no_subject() -> None:
    # A manifest with no ``subject`` (e.g. a producer bundle) -> subjectDigest
    # None, so the entry stays a top-level row in the grouping. No raise.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:prod", "imageTags": ["prod-tag"]}]},
        manifests_by_digest={"sha256:prod": json.dumps(_producer_bundle_manifest())},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert bundles[0]["classification"] == "unvalidated bundle"
    assert bundles[0]["subjectDigest"] is None


@pytest.mark.parametrize(
    "subject",
    [
        _MISSING,  # subject key absent
        None,  # subject is null
        "not-a-dict",  # subject is a bare string
        ["not", "a", "dict"],  # subject is a list
        {},  # subject dict without a digest
        {"digest": None},  # non-string digest
        {"digest": 123},  # non-string digest
        {"digest": ""},  # empty-string digest
    ],
)
def test_unvalidated_entry_malformed_subject_degrades_to_none_no_raise(subject: Any) -> None:
    # Every malformed/absent ``subject`` shape degrades subjectDigest to None
    # WITHOUT raising — the degrade-don't-crash boundary.
    manifest: dict[str, Any] = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": WORM_RECEIPT_ARTIFACT_TYPE,
        "config": {"mediaType": OCI_EMPTY_MEDIA_TYPE},
    }
    if subject is not _MISSING:
        manifest["subject"] = subject

    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:r", "imageTags": ["r-tag"]}]},
        manifests_by_digest={"sha256:r": json.dumps(manifest)},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()  # must not raise

    assert len(bundles) == 1
    assert bundles[0]["subjectDigest"] is None
    # Classification still works regardless of the subject shape.
    assert bundles[0]["classification"] == "worm-receipt"


def test_unvalidated_entry_subject_digest_none_on_unreadable_manifest() -> None:
    # A manifest-read failure degrades BOTH classification and subjectDigest:
    # "unclassified" + None, never raising.
    from botocore.exceptions import ClientError

    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [{"imageDigest": "sha256:boom", "imageTags": ["boom-tag"]}]},
    )

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "BatchGetImage")

    client.batch_get_image = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert bundles[0]["classification"] == "unclassified"
    assert bundles[0]["subjectDigest"] is None


def test_validated_entries_have_no_subject_digest_field() -> None:
    # Only synthetic unindexed entries gain subjectDigest; a Metadata_Index row
    # is rendered verbatim and unaffected.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": [_record("proj-a", "sha256:v")]}]},
        images_by_repo={repo: [{"imageDigest": "sha256:v", "imageTags": ["v-tag"]}]},
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert len(bundles) == 1
    assert "subjectDigest" not in bundles[0]


# --------------------------------------------------------------------------
# 9. get_referrer_manifest — lazy focused-referrer raw-manifest read.
#    Backs the drill-down; uses ONLY the allowlisted ecr:BatchGetImage,
#    returns the parsed dict on success and None on any error/parse failure,
#    NEVER raising.
# --------------------------------------------------------------------------


def _referrer_args(**overrides: Any) -> dict[str, Any]:
    args = {
        "registry_id": ACCOUNT_ID,
        "repository_name": f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a",
        "manifest_digest": "sha256:ref",
    }
    args.update(overrides)
    return args


def test_get_referrer_manifest_returns_parsed_dict_on_success() -> None:
    manifest = _referrer_manifest_with_subject("sha256:primary")
    client = _RecordingClient(
        ecr_pages=[{"repositories": []}],
        manifests_by_digest={"sha256:ref": json.dumps(manifest)},
    )
    sources = _sources(client)

    result = sources.get_referrer_manifest(**_referrer_args())

    assert result == manifest
    # ONLY the allowlisted batch_get_image was issued — no ListImageReferrers,
    # no scan, no mutator.
    assert set(client.calls) == {"batch_get_image"}
    assert "list_image_referrers" not in client.calls
    assert "scan" not in client.calls
    assert "receive_message" not in client.calls


def test_get_referrer_manifest_none_when_identifiers_missing_no_aws_call() -> None:
    client = _RecordingClient(ecr_pages=[{"repositories": []}])
    sources = _sources(client)

    for missing in ("registry_id", "repository_name", "manifest_digest"):
        result = sources.get_referrer_manifest(**_referrer_args(**{missing: None}))
        assert result is None
    # AWS is never called with a None identifier.
    assert "batch_get_image" not in client.calls


def test_get_referrer_manifest_none_on_client_error_no_raise() -> None:
    from botocore.exceptions import ClientError

    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "BatchGetImage")

    client.batch_get_image = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    assert sources.get_referrer_manifest(**_referrer_args()) is None


def test_get_referrer_manifest_none_on_botocore_error_no_raise() -> None:
    from botocore.exceptions import EndpointConnectionError

    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _boom(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        raise EndpointConnectionError(endpoint_url="https://ecr")

    client.batch_get_image = _boom  # type: ignore[method-assign]
    sources = _sources(client)

    assert sources.get_referrer_manifest(**_referrer_args()) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"images": []},  # empty image set (delete race)
        {"images": ["not-a-dict"]},  # non-dict images element
        {"images": [{"imageId": {}}]},  # missing imageManifest key
        {"images": [{"imageManifest": "{not valid json"}]},  # malformed JSON
        {"images": [{"imageManifest": json.dumps([1, 2, 3])}]},  # parses to list, not dict
        {"images": [{"imageManifest": json.dumps(5)}]},  # parses to scalar, not dict
    ],
)
def test_get_referrer_manifest_none_on_parse_or_shape_failure(payload: dict[str, Any]) -> None:
    client = _RecordingClient(ecr_pages=[{"repositories": []}])

    def _override(**kwargs: Any) -> dict[str, Any]:
        client.calls.append("batch_get_image")
        return payload

    client.batch_get_image = _override  # type: ignore[method-assign]
    sources = _sources(client)

    assert sources.get_referrer_manifest(**_referrer_args()) is None  # never raises


# --------------------------------------------------------------------------
# 10. createdAt from the AWS-set imagePushedAt (tui-create-date-every-row).
#     Every synthetic unindexed/referrer entry carries createdAt mapped from the
#     DescribeImages imagePushedAt — the SAME timestamp source as the indexed
#     createdAt, normalized to the identical Z-suffixed UTC form — sourced ONLY
#     from imagePushedAt, never from a manifest ``created`` annotation, and
#     issuing NO additional AWS call.
# --------------------------------------------------------------------------


def _pushed_at(year: int = 2026, month: int = 5, day: int = 26, hour: int = 12) -> datetime:
    # A timezone-aware datetime exactly as boto3 returns imagePushedAt.
    return datetime(year, month, day, hour, 5, 0, tzinfo=UTC)


def _unindexed_client(
    *,
    digest: str,
    manifest: dict[str, Any],
    pushed_at: Any,
    extra_detail: dict[str, Any] | None = None,
) -> _RecordingClient:
    """A client with one unindexed image carrying ``pushed_at`` on its detail."""
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    detail: dict[str, Any] = {"imageDigest": digest, "imageTags": [f"{digest}-tag"]}
    if pushed_at is not _MISSING:
        detail["imagePushedAt"] = pushed_at
    if extra_detail:
        detail.update(extra_detail)
    return _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={repo: [detail]},
        manifests_by_digest={digest: json.dumps(manifest)},
    )


def test_list_image_refs_retains_pushed_at_alongside_digest_and_tag() -> None:
    # list_image_refs returns ImageRef triples carrying the AWS-set imagePushedAt
    # off the SAME DescribeImages detail — no extra AWS call.
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    pushed = _pushed_at()
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        images_by_repo={
            repo: [
                {"imageDigest": "sha256:a", "imageTags": ["a-tag"], "imagePushedAt": pushed},
                {"imageDigest": "sha256:b"},  # untagged, no imagePushedAt
            ]
        },
    )
    sources = _sources(client)

    refs = sources.list_image_refs(repo)

    assert refs == [
        ImageRef(digest="sha256:a", tag="a-tag", pushed_at=pushed),
        ImageRef(digest="sha256:b", tag=None, pushed_at=None),
    ]
    # Only the one DescribeImages call — imagePushedAt came free off it.
    assert client.calls.count("describe_images") == 1


def test_unvalidated_entry_created_at_from_pushed_at_normalized() -> None:
    # (a) An unvalidated bundle entry carries createdAt equal to the AWS
    # imagePushedAt, normalized to the Z-suffixed UTC string form.
    client = _unindexed_client(
        digest="sha256:prod", manifest=_producer_bundle_manifest(), pushed_at=_pushed_at()
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    assert len(bundles) == 1
    entry = bundles[0]
    assert entry["classification"] == "unvalidated bundle"
    assert entry["createdAt"] == "2026-05-26T12:05:00Z"


def test_referrer_orphan_entry_created_at_from_pushed_at_normalized() -> None:
    # (a) A referrer/orphan entry (a WORM_Receipt referrer) likewise carries
    # createdAt mapped from its own imagePushedAt.
    client = _unindexed_client(
        digest="sha256:rcpt",
        manifest=_referrer_manifest_with_subject("sha256:primary"),
        pushed_at=_pushed_at(day=27, hour=9),
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()

    entry = bundles[0]
    assert entry["classification"] == "worm-receipt"
    assert entry["subjectDigest"] == "sha256:primary"
    assert entry["createdAt"] == "2026-05-27T09:05:00Z"


def test_created_at_normalizes_non_utc_offset_to_z_suffixed_utc() -> None:
    # A non-UTC tz-aware imagePushedAt is converted TO UTC and emitted with a
    # trailing Z (never a +00:00 / +hh:mm offset) so it matches the indexed form.
    from datetime import timedelta

    eastern = timezone(timedelta(hours=-4))
    pushed = datetime(2026, 5, 26, 8, 5, 0, tzinfo=eastern)  # == 12:05 UTC
    client = _unindexed_client(
        digest="sha256:prod", manifest=_producer_bundle_manifest(), pushed_at=pushed
    )
    sources = _sources(client)

    entry = sources.list_all_bundles()[0]

    assert entry["createdAt"] == "2026-05-26T12:05:00Z"
    assert "+" not in entry["createdAt"]


def test_created_at_sourced_from_pushed_at_never_manifest_created_annotation() -> None:
    # (b) Source lock-in: when the manifest carries a ``created``
    # annotation that DIFFERS from imagePushedAt, the entry's createdAt is the
    # imagePushedAt value — NEVER the producer-controlled annotation.
    manifest = _producer_bundle_manifest()
    manifest["annotations"] = {"org.opencontainers.image.created": "1999-01-01T00:00:00Z"}
    manifest["created"] = "1999-01-01T00:00:00Z"
    client = _unindexed_client(digest="sha256:prod", manifest=manifest, pushed_at=_pushed_at())
    sources = _sources(client)

    entry = sources.list_all_bundles()[0]

    assert entry["createdAt"] == "2026-05-26T12:05:00Z"  # the AWS push time
    assert entry["createdAt"] != "1999-01-01T00:00:00Z"  # never the annotation


def test_created_at_build_issues_no_extra_aws_call() -> None:
    # (c) Allowlist lock-in: attaching createdAt issues NO
    # additional ecr:BatchGetImage, no second DescribeImages, and no Scan beyond
    # what the unindexed path already makes — exactly one describe_images and one
    # batch_get_image per repo/image, all from the existing path.
    client = _unindexed_client(
        digest="sha256:prod", manifest=_producer_bundle_manifest(), pushed_at=_pushed_at()
    )
    sources = _sources(client)

    sources.list_all_bundles()

    assert client.calls.count("describe_images") == 1
    assert client.calls.count("batch_get_image") == 1
    assert "scan" not in client.calls
    assert set(client.calls) <= {
        "describe_repositories",
        "describe_images",
        "batch_get_image",
        "query",
    }


def test_created_at_absent_when_pushed_at_missing_no_raise() -> None:
    # (d) A genuinely-missing imagePushedAt leaves createdAt ABSENT (→ the table
    # renders its placeholder) without raising.
    client = _unindexed_client(
        digest="sha256:prod", manifest=_producer_bundle_manifest(), pushed_at=_MISSING
    )
    sources = _sources(client)

    bundles = sources.list_all_bundles()  # must not raise

    assert len(bundles) == 1
    assert "createdAt" not in bundles[0]


def test_created_at_absent_when_pushed_at_none() -> None:
    # An explicit None imagePushedAt is also treated as missing → key absent.
    client = _unindexed_client(
        digest="sha256:prod", manifest=_producer_bundle_manifest(), pushed_at=None
    )
    sources = _sources(client)

    assert "createdAt" not in sources.list_all_bundles()[0]


def test_created_at_passes_through_string_pushed_at_unchanged() -> None:
    # A fixture (or future AWS shape) supplying imagePushedAt already as the wire
    # string is passed through unchanged, not re-formatted.
    client = _unindexed_client(
        digest="sha256:prod",
        manifest=_producer_bundle_manifest(),
        pushed_at="2026-05-26T12:05:00Z",
    )
    sources = _sources(client)

    assert sources.list_all_bundles()[0]["createdAt"] == "2026-05-26T12:05:00Z"


# --------------------------------------------------------------------------
# 10b. _normalize_pushed_at unit-level edge cases (the PURE helper).
#      The entry-level tests above exercise the happy paths through
#      list_all_bundles; these pin the helper's contract directly so a
#      refactor that changes the format string or type handling is caught at
#      the source, not just indirectly.
# --------------------------------------------------------------------------


def test_normalize_pushed_at_drops_microseconds() -> None:
    # A sub-second / microsecond datetime is truncated cleanly to whole seconds
    # by the %Y-%m-%dT%H:%M:%SZ format (no fractional component, no rounding).
    pushed = datetime(2026, 5, 26, 12, 5, 0, 987654, tzinfo=UTC)

    assert _normalize_pushed_at(pushed) == "2026-05-26T12:05:00Z"


def test_normalize_pushed_at_converts_non_utc_offset_to_z() -> None:
    # A tz-aware datetime at a non-UTC offset is converted TO UTC and emitted
    # with a trailing Z (never a +hh:mm offset).
    from datetime import timedelta

    pushed = datetime(2026, 5, 26, 8, 5, 0, tzinfo=timezone(timedelta(hours=-4)))

    result = _normalize_pushed_at(pushed)

    assert result == "2026-05-26T12:05:00Z"
    assert result.endswith("Z")
    assert "+" not in result


def test_normalize_pushed_at_naive_datetime_interpreted_as_local_wall_clock() -> None:
    # A NAIVE datetime (no tzinfo) does NOT raise — astimezone() interprets it as
    # the host's local wall-clock time and converts to UTC. boto3 always returns
    # tz-aware imagePushedAt, so this is a robustness edge, not a live path; we
    # pin the behavior machine-independently by deriving the expected UTC value
    # from the SAME local interpretation rather than hardcoding an offset.
    naive = datetime(2026, 5, 26, 12, 5, 0)
    expected = naive.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = _normalize_pushed_at(naive)

    assert result == expected
    assert result.endswith("Z")  # still emits the Z form, never raises


@pytest.mark.parametrize(
    "junk",
    [
        None,
        12345,  # int (e.g. epoch seconds) is not accepted
        12345.6,  # float
        [datetime(2026, 5, 26, tzinfo=UTC)],  # list
        {"imagePushedAt": "2026-05-26T12:05:00Z"},  # dict
        object(),  # arbitrary object
        b"2026-05-26T12:05:00Z",  # bytes are NOT str -> rejected
    ],
)
def test_normalize_pushed_at_rejects_non_str_non_datetime_without_raise(junk: Any) -> None:
    # Anything that is neither str nor datetime returns None (key left absent),
    # and NEVER raises.
    assert _normalize_pushed_at(junk) is None


def test_normalize_pushed_at_passes_string_through_unchanged() -> None:
    # A str is returned verbatim — not re-parsed, not re-formatted (even a
    # non-canonical string is passed through, matching the docstring contract).
    assert _normalize_pushed_at("2026-05-26T12:05:00Z") == "2026-05-26T12:05:00Z"
    assert _normalize_pushed_at("not-a-date") == "not-a-date"


def test_normalize_pushed_at_bool_is_not_treated_as_datetime() -> None:
    # bool is an int subclass but neither str nor datetime -> None (no raise).
    # Guards against a surprising isinstance widening in a future refactor.
    assert _normalize_pushed_at(True) is None
    assert _normalize_pushed_at(False) is None


# --------------------------------------------------------------------------
# 10c. Mixed repo: some images carry imagePushedAt, some don't. Each synthetic
#      entry independently gets (or omits) its own createdAt — one missing push
#      time never strips createdAt from a sibling that has one.
# --------------------------------------------------------------------------


def test_mixed_pushed_at_each_entry_independently_correct() -> None:
    repo = f"cairnproofs4af-artifact-{QUALIFIER}/org-a/proj-a/proj-a"
    client = _RecordingClient(
        ecr_pages=[{"repositories": [_repo(repo)]}],
        pages_by_pk={"proj-a": [{"Items": []}]},
        images_by_repo={
            repo: [
                {  # has imagePushedAt
                    "imageDigest": "sha256:withdate",
                    "imageTags": ["withdate-tag"],
                    "imagePushedAt": _pushed_at(),
                },
                {  # no imagePushedAt key at all
                    "imageDigest": "sha256:nodate",
                    "imageTags": ["nodate-tag"],
                },
            ]
        },
        manifests_by_digest={
            "sha256:withdate": json.dumps(_producer_bundle_manifest()),
            "sha256:nodate": json.dumps(_producer_bundle_manifest()),
        },
    )
    sources = _sources(client)

    by_digest = {b["manifestDigest"]: b for b in sources.list_all_bundles()}

    assert by_digest["sha256:withdate"]["createdAt"] == "2026-05-26T12:05:00Z"
    assert "createdAt" not in by_digest["sha256:nodate"]
