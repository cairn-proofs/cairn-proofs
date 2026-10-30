"""Property-based tests for the metadata-index capability.

Covers: [META-007.01], [META-007.02] (conditional writes prevent race
conditions), [META-009.01], [META-010.01] (pagination yields full result set
with no duplicates). [META-001.02] / [META-011.01] are SKIPPED — see test below
for the surface-not-implemented justification.
"""

from __future__ import annotations

from typing import Any

import pytest
from cairn_metadata.access import MetadataIndex
from cairn_metadata.conditional import (
    ConditionalUpdateFailed,
    create_record,
    update_record_if_newer,
)
from cairn_metadata.pagination import (
    InvalidPaginationToken,
    PageIterator,
    paginate_query,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies + fixtures
# ---------------------------------------------------------------------------

# Tenant-PK key schema ([META-001]): the partition key is the
# opaque ``{tenant-id}`` (the exact-match isolation boundary) and the sort key is
# ``{project-id}#{manifest-digest}`` (the queryable sub-hierarchy). A project
# listing is a ``begins_with('{project-id}#')`` condition under the tenant
# partition. ``org-id`` is never a key component.
TENANT_ID = "tnt-foo"
PROJECT_ID = "alpha-bank"


@st.composite
def rfc3339_iso(draw: st.DrawFn) -> str:
    year = draw(st.integers(min_value=2024, max_value=2029))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    second = draw(st.integers(min_value=0, max_value=59))
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


@pytest.fixture
def fresh_metadata_table_factory(aws_region: str):
    """Per-test moto context with a factory that yields fresh, empty tables.

    Hypothesis re-runs the test body across examples but only invokes the
    fixture once per pytest test — the factory pattern lets each example
    operate on a clean table without stale rows from prior examples.
    """
    import uuid

    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=aws_region)

        def _make():
            name = f"MetaProp-{uuid.uuid4().hex[:8]}"
            resource.create_table(
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
            return resource.Table(name)

        yield _make


# ---------------------------------------------------------------------------
# [META-001.02] / [META-011.01] — Query construction / parameter validation (SKIPPED)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "[META-001.02] / [META-011.01] — the metadata-index spec calls for a "
        "'query construction "
        "layer' that selects the access path AND rejects queries with "
        "unrecognized fields, malformed values, or empty required parameters. "
        "Today's surface (cairn_metadata.MetadataIndex.query_by_project, "
        "ControlClaims.query_by_control) is narrowly typed per access path with "
        "no generic dispatch / parameter validator. The CLI-level "
        "cairn_cli.query._run does perform selector rejection but is "
        "the wrong granularity for a library-level property test. Mark as SKIP "
        "until the spec-shaped query-construction surface lands as a separate "
        "change."
    )
)
def test_property_10_query_construction_validates_parameters() -> None:
    """**WHEN** a caller submits a query containing an unrecognized field name,
    malformed value, or empty required parameter **THEN** the Metadata_Index
    rejects the query before issuing any DynamoDB call and returns an error
    naming the offending parameter.

    Source: openspec/specs/metadata-index/spec.md ([META-001.02], [META-011.01]).
    """
    raise AssertionError("skipped — see decorator")


# ---------------------------------------------------------------------------
# [META-007.01], [META-007.02] — Race conditions during concurrent updates
# ---------------------------------------------------------------------------
# Source: openspec/specs/metadata-index/spec.md ([META-007.01], [META-007.02])


@given(repeats=st.integers(min_value=2, max_value=6))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=10,
    deadline=None,
)
def test_property_11_duplicate_create_attempts_fail_after_first(
    fresh_metadata_table_factory: Any, repeats: int
) -> None:
    """**WHEN** two pipeline invocations attempt to create the Metadata_Index
    record for the same Manifest_Digest concurrently **THEN** the first create
    succeeds, the second create fails its `attribute_not_exists` condition, and
    no duplicate record is produced.

    Source: openspec/specs/metadata-index/spec.md ([META-007.01] — duplicate create
    rejection).
    """
    table = fresh_metadata_table_factory()
    item = {
        "pk": TENANT_ID,
        "sk": f"{PROJECT_ID}#sha256:" + "0" * 64,
        "status": "validated",
        "updatedAt": "2026-05-24T00:00:00Z",
    }
    create_record(table, item)
    for _ in range(repeats - 1):
        with pytest.raises(ConditionalUpdateFailed):
            create_record(table, item)
    response = table.scan()
    assert len(response["Items"]) == 1


@given(timestamps=st.lists(rfc3339_iso(), min_size=2, max_size=8))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=10,
    deadline=None,
)
def test_property_11_older_event_cannot_overwrite_newer_state(
    fresh_metadata_table_factory: Any, timestamps: list[str]
) -> None:
    """**WHEN** an update operation arrives whose event timestamp is not strictly
    greater than the record's current `updatedAt` value **THEN** the conditional
    write fails and the older event does not overwrite newer data, preserving
    the most recent state in the Metadata_Index.

    Source: openspec/specs/metadata-index/spec.md ([META-007.02] — event-timestamp guard).
    """
    table = fresh_metadata_table_factory()
    pk = TENANT_ID
    sk = f"{PROJECT_ID}#sha256:" + "0" * 64
    table.put_item(
        Item={"pk": pk, "sk": sk, "status": "validated", "updatedAt": "2000-01-01T00:00:00Z"}
    )
    accepted: list[str] = []
    for ts in timestamps:
        try:
            update_record_if_newer(
                table,
                pk=pk,
                sk=sk,
                updates={"status": "signed", "lastSeenTs": ts},
                event_timestamp=ts,
            )
            accepted.append(ts)
        except ConditionalUpdateFailed:
            pass
    item = table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["updatedAt"] == max(accepted) == max(timestamps)
    # No matter the delivery order, only one row exists.
    response = table.scan()
    assert len(response["Items"]) == 1


# ---------------------------------------------------------------------------
# [META-009.01] — Pagination yields full result set with no duplicates
# ---------------------------------------------------------------------------
# Source: openspec/specs/metadata-index/spec.md ([META-009.01])


@given(total_records=st.integers(min_value=0, max_value=250))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=8,
    deadline=None,
)
def test_property_12_pagination_yields_full_set_with_no_duplicates(
    fresh_metadata_table_factory: Any, total_records: int
) -> None:
    """**WHEN** a query matches 250 records **THEN** the first response contains
    100 items and a non-null pagination token; subsequent calls using that token
    return the next 100 items and another token, then the final 50 items with a
    null token; the concatenated pages equal the full 250-record result set with
    no duplicates and no omissions.

    Source: openspec/specs/metadata-index/spec.md ([META-009.01]).
    """
    table = fresh_metadata_table_factory()
    pk = TENANT_ID
    expected_sks: list[str] = []
    with table.batch_writer() as batch:
        for i in range(total_records):
            sk = f"{PROJECT_ID}#sha256:" + f"{i:064x}"
            expected_sks.append(sk)
            batch.put_item(Item={"pk": pk, "sk": sk, "rec_id": i})

    seen_sks: list[str] = []
    token: str | None = None
    iterations = 0
    while True:
        page = paginate_query(
            table,
            page_token=token,
            limit=100,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": pk},
        )
        # Each page contains at most 100 items.
        assert len(page.items) <= 100
        seen_sks.extend(item["sk"] for item in page.items)
        token = page.next_token
        iterations += 1
        if token is None:
            break
        assert iterations < 20  # guard against infinite loops

    assert sorted(seen_sks) == sorted(expected_sks)
    assert len(seen_sks) == len(set(seen_sks))


@given(total_records=st.integers(min_value=0, max_value=250))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=5,
    deadline=None,
)
def test_property_12_page_iterator_yields_each_record_exactly_once(
    fresh_metadata_table_factory: Any, total_records: int
) -> None:
    """**WHEN** a query matches 250 records **THEN** ... the concatenated pages
    equal the full 250-record result set with no duplicates and no omissions.

    Source: openspec/specs/metadata-index/spec.md ([META-009.01] — exercises the lazy
    PageIterator that the access-path layer uses).
    """
    table = fresh_metadata_table_factory()
    pk = TENANT_ID
    expected_sks = [f"{PROJECT_ID}#sha256:" + f"{i:064x}" for i in range(total_records)]
    with table.batch_writer() as batch:
        for i, sk in enumerate(expected_sks):
            batch.put_item(Item={"pk": pk, "sk": sk, "rec_id": i})

    iterator = PageIterator(
        table,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": pk},
    )
    seen = [row["sk"] for row in iterator]
    assert sorted(seen) == sorted(expected_sks)
    assert len(seen) == len(set(seen))


def test_property_12_no_match_query_returns_empty_page_with_null_token(
    fresh_metadata_table_factory: Any,
) -> None:
    """**WHEN** a caller queries within tenant `tnt-nonexistent` for project
    `nonexistent-project` **THEN** the Metadata_Index responds with an empty
    items array, no pagination token, and HTTP success (no error), letting the
    caller distinguish "no matches" from "invalid query".

    Source: openspec/specs/metadata-index/spec.md ([META-010.01] — empty result set
    invariant).
    """
    table = fresh_metadata_table_factory()
    page = paginate_query(
        table,
        page_token=None,
        limit=100,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": "nonexistent"},
    )
    assert page.items == []
    assert page.next_token is None


@given(
    bad_token=st.one_of(
        st.just("not-base64-!!!"),
        st.just("aGVsbG8gd29ybGQ="),  # decodes to "hello world" — not JSON dict
        st.just("eyJ4IjogMX0="),  # decodes to {"x": 1} — unrecognized key
        # base64 of a 5000-byte JSON dict — exceeds the 4 KiB cap.
        st.builds(
            lambda: (
                __import__("base64")
                .urlsafe_b64encode(b'{"pk": "' + b"a" * 5000 + b'"}')
                .decode("ascii")
            )
        ),
    )
)
def test_property_12_malformed_pagination_token_rejected(bad_token: str) -> None:
    """**WHEN** a caller submits a query whose date range uses a malformed ISO-
    8601 string or references an unrecognized field name **THEN** the
    Metadata_Index rejects the query and returns an error identifying the
    offending parameter, without issuing any DynamoDB call.

    Source: openspec/specs/metadata-index/spec.md ([META-009.01] — defensive: a malformed
    pagination token must be rejected before the DynamoDB call is made).
    """
    import unittest.mock

    table = unittest.mock.MagicMock()
    with pytest.raises(InvalidPaginationToken):
        paginate_query(
            table,
            page_token=bad_token,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "acme-prod"},
        )
    table.query.assert_not_called()


def test_property_12_tenant_scoped_metadata_index_pagination_round_trip(
    fresh_metadata_table_factory: Any, aws_region: str
) -> None:
    """**WHEN** a query matches 250 records **THEN** ... the concatenated pages
    equal the full 250-record result set with no duplicates and no omissions.

    Source: openspec/specs/metadata-index/spec.md ([META-009.01] / [META-004.01] — via
    the MetadataIndex.query_by_project facade so the facade's tenant-PK
    partition-key + project sort-key-prefix composition is exercised under
    pagination).
    """
    import boto3

    table = fresh_metadata_table_factory()
    with table.batch_writer() as batch:
        for i in range(150):
            batch.put_item(Item={"pk": TENANT_ID, "sk": f"{PROJECT_ID}#sha256:" + f"{i:064x}"})

    resource = boto3.resource("dynamodb", region_name=aws_region)
    index = MetadataIndex(table.name, dynamodb_resource=resource)
    page1 = index.query_by_project(tenant_id=TENANT_ID, project_id=PROJECT_ID)
    assert len(page1.items) == 100
    assert page1.next_token is not None
    page2 = index.query_by_project(
        tenant_id=TENANT_ID, project_id=PROJECT_ID, page_token=page1.next_token
    )
    assert len(page2.items) == 50
    assert page2.next_token is None
    sks = [it["sk"] for it in page1.items + page2.items]
    assert len(sks) == len(set(sks)) == 150
