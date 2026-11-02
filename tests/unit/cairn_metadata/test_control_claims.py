"""Tests for cairn_metadata.control_claims (ControlClaims fan-out + query).

The control-claims projection is a tenant-PK plane ([PCLAIM-001], [META-001]):
``pk = {tenant-id}`` / ``sk = {catalogOrProfileUuid}#{controlId}#{manifestDigest}``.
These tests assert the application-level key composition that the CDK template
structurally cannot see.
"""

from __future__ import annotations

from cairn_metadata.control_claims import ControlClaims

from tests.unit.cairn_metadata.conftest import CONTROL_CLAIMS_TABLE_NAME

DIGEST = "sha256:" + "a" * 64
CATALOG_UUID = "11111111-1111-4111-8111-111111111111"
TENANT = "tnt-01JABCD2EF3GH4JK5MN6PQ7RS8"


def _make_index(resource) -> ControlClaims:
    return ControlClaims(CONTROL_CLAIMS_TABLE_NAME, dynamodb_resource=resource)


def test_record_controls_fans_out_one_row_per_control(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    count = idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2", "ac-3", "au-4"],
        denormalized={"projectId": "alpha-bank", "signingStatus": "pending"},
    )
    assert count == 3
    table = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME)
    rows = table.scan()["Items"]
    assert len(rows) == 3
    # Tenant-PK plane: every row partitions on the exact tenant id.
    assert {r["pk"] for r in rows} == {TENANT}
    sks = sorted(r["sk"] for r in rows)
    assert sks == [
        f"{CATALOG_UUID}#ac-2#{DIGEST}",
        f"{CATALOG_UUID}#ac-3#{DIGEST}",
        f"{CATALOG_UUID}#au-4#{DIGEST}",
    ]
    for row in rows:
        assert row["tenantId"] == TENANT
        assert row["catalogOrProfileUuid"] == CATALOG_UUID
        assert row["manifestDigest"] == DIGEST
        assert row["projectId"] == "alpha-bank"
        assert row["signingStatus"] == "pending"


def test_record_controls_deduplicates_input(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    count = idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2", "ac-2", "au-4"],
        denormalized={},
    )
    assert count == 2


def test_record_controls_empty_is_noop(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    count = idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=[],
        denormalized={"projectId": "alpha-bank"},
    )
    assert count == 0
    table = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME)
    assert table.scan()["Items"] == []


def test_record_controls_reserved_keys_are_stripped_from_denormalized(
    dynamodb_resource,
) -> None:
    idx = _make_index(dynamodb_resource)
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={
            "pk": "attacker-pk",
            "sk": "attacker-sk",
            "controlId": "attacker-control",
            "manifestDigest": "attacker-digest",
            "projectId": "alpha-bank",
        },
    )
    table = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME)
    row = table.scan()["Items"][0]
    assert row["pk"] == TENANT
    assert row["sk"] == f"{CATALOG_UUID}#ac-2#{DIGEST}"
    assert row["manifestDigest"] == DIGEST


def test_record_controls_denormalized_cannot_override_key_tenant(
    dynamodb_resource,
) -> None:
    """A ``denormalized`` dict carrying ``tenantId`` must NOT override the
    key-derived ``tenant_id`` — tenant is a key component, not pass-through."""
    idx = _make_index(dynamodb_resource)
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"tenantId": "attacker-tenant", "projectId": "alpha-bank"},
    )
    table = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME)
    row = table.scan()["Items"][0]
    assert row["pk"] == TENANT
    assert row["tenantId"] == TENANT


def test_query_by_control_returns_only_matching_rows(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    other_digest = "sha256:" + "b" * 64
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2", "ac-3"],
        denormalized={"projectId": "alpha-bank"},
    )
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=other_digest,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"projectId": "beta-corp"},
    )
    other_uuid = "22222222-2222-4222-8222-222222222222"
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=other_uuid,
        controls=["ac-2"],
        denormalized={"projectId": "alpha-bank"},
    )

    page = idx.query_by_control(TENANT, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-2")
    assert len(page.items) == 2
    project_ids = sorted(item["projectId"] for item in page.items)
    assert project_ids == ["alpha-bank", "beta-corp"]


def test_query_by_control_does_not_cross_tenant_boundary(dynamodb_resource) -> None:
    """A query for one tenant never returns another tenant's rows citing the
    same control — cross-tenant isolation at the key-composition level
    ([PCLAIM-001.05])."""
    idx = _make_index(dynamodb_resource)
    other_tenant = "tnt-01JZZZZZZZZZZZZZZZZZZZZZZZ"
    other_digest = "sha256:" + "c" * 64
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"projectId": "alpha-bank"},
    )
    idx.record_controls(
        tenant_id=other_tenant,
        manifest_digest=other_digest,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"projectId": "beta-corp"},
    )

    page = idx.query_by_control(TENANT, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-2")
    assert len(page.items) == 1
    assert page.items[0]["projectId"] == "alpha-bank"
    assert page.items[0]["pk"] == TENANT

    other_page = idx.query_by_control(
        other_tenant, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-2"
    )
    assert len(other_page.items) == 1
    assert other_page.items[0]["projectId"] == "beta-corp"


def test_query_by_control_prefix_boundary_excludes_longer_control_id(
    dynamodb_resource,
) -> None:
    """A query for ``ac-2`` must NOT return ``ac-20`` rows under the same
    tenant + catalog.

    The sort-key prefix is ``{uuid}#{controlId}#`` — the trailing ``#`` is
    load-bearing. Without it, ``begins_with(sk, '{uuid}#ac-2')`` would also
    match ``{uuid}#ac-20#...`` (OSCAL token IDs are arbitrary strings, so
    ``ac-2`` is a character prefix of ``ac-20``). A regression dropping that
    delimiter would silently over-return sibling controls, so assert the
    boundary directly."""
    idx = _make_index(dynamodb_resource)
    digest_ac2 = "sha256:" + "d" * 64
    digest_ac20 = "sha256:" + "e" * 64
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=digest_ac2,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"projectId": "alpha-bank"},
    )
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=digest_ac20,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-20"],
        denormalized={"projectId": "alpha-bank"},
    )

    page = idx.query_by_control(TENANT, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-2")
    assert len(page.items) == 1
    assert {item["controlId"] for item in page.items} == {"ac-2"}
    assert page.items[0]["manifestDigest"] == digest_ac2

    # The reciprocal query returns only the longer control, never the shorter.
    page_ac20 = idx.query_by_control(
        TENANT, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-20"
    )
    assert len(page_ac20.items) == 1
    assert page_ac20.items[0]["controlId"] == "ac-20"


def test_query_by_control_paginates_within_one_tenant(dynamodb_resource) -> None:
    """A multi-page control result under one tenant round-trips through the
    opaque page_token with the ``begins_with`` key condition — no row is
    dropped or duplicated across the page boundary, and the final page clears
    ``next_token``."""
    idx = _make_index(dynamodb_resource)
    digests = [f"sha256:{i:064x}" for i in range(5)]
    for digest in digests:
        idx.record_controls(
            tenant_id=TENANT,
            manifest_digest=digest,
            catalog_or_profile_uuid=CATALOG_UUID,
            controls=["ac-2"],
            denormalized={"projectId": "alpha-bank"},
        )

    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        page = idx.query_by_control(
            TENANT,
            catalog_or_profile_uuid=CATALOG_UUID,
            control_id="ac-2",
            page_token=token,
            limit=2,
        )
        pages += 1
        seen.extend(item["manifestDigest"] for item in page.items)
        # Every returned row matches the queried control under this tenant.
        assert all(item["controlId"] == "ac-2" for item in page.items)
        assert all(item["pk"] == TENANT for item in page.items)
        token = page.next_token
        if token is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert sorted(seen) == sorted(digests)
    assert len(seen) == len(set(seen)), "a row was returned on more than one page"


def test_record_controls_same_control_two_catalogs_writes_distinct_rows(
    dynamodb_resource,
) -> None:
    """One bundle citing the same control under two different catalog/profile
    UUIDs writes two distinct rows (distinct sort key) under the single tenant
    partition — the leading ``catalogOrProfileUuid`` SK segment keeps them
    from colliding ([PCLAIM-001])."""
    idx = _make_index(dynamodb_resource)
    other_uuid = "22222222-2222-4222-8222-222222222222"
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["ac-2"],
        denormalized={"projectId": "alpha-bank"},
    )
    idx.record_controls(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=other_uuid,
        controls=["ac-2"],
        denormalized={"projectId": "alpha-bank"},
    )

    table = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME)
    rows = table.scan()["Items"]
    assert len(rows) == 2
    assert {r["pk"] for r in rows} == {TENANT}
    assert sorted(r["sk"] for r in rows) == sorted(
        [
            f"{CATALOG_UUID}#ac-2#{DIGEST}",
            f"{other_uuid}#ac-2#{DIGEST}",
        ]
    )
    # Each catalog is independently queryable and returns only its own row.
    page_a = idx.query_by_control(TENANT, catalog_or_profile_uuid=CATALOG_UUID, control_id="ac-2")
    page_b = idx.query_by_control(TENANT, catalog_or_profile_uuid=other_uuid, control_id="ac-2")
    assert len(page_a.items) == 1
    assert len(page_b.items) == 1
    assert page_a.items[0]["catalogOrProfileUuid"] == CATALOG_UUID
    assert page_b.items[0]["catalogOrProfileUuid"] == other_uuid


# --- per-item PutItem (no BatchWriteItem; access-control [ACCESS-007.02]) ---


class _RecordingTable:
    """Stand-in Table that records put_item calls and can fail on the Nth one."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.puts: list[dict] = []
        self.fail_on = fail_on

    def put_item(self, *, Item: dict) -> dict:
        from botocore.exceptions import ClientError

        if self.fail_on is not None and len(self.puts) == self.fail_on:
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
                "PutItem",
            )
        self.puts.append(Item)
        return {}

    def batch_writer(self, *a, **kw):  # pragma: no cover — must never be called
        raise AssertionError("record_controls must not use BatchWriteItem")


class _Resource:
    def __init__(self, table: _RecordingTable) -> None:
        self._table = table

    def Table(self, name: str) -> _RecordingTable:
        return self._table


def test_record_controls_writes_one_put_item_per_control_and_is_idempotent(
    dynamodb_resource,
) -> None:
    table = _RecordingTable()
    idx = ControlClaims(CONTROL_CLAIMS_TABLE_NAME, dynamodb_resource=_Resource(table))
    args = dict(
        tenant_id=TENANT,
        manifest_digest=DIGEST,
        catalog_or_profile_uuid=CATALOG_UUID,
        controls=["au-4", "ac-2"],
        denormalized={},
    )
    assert idx.record_controls(**args) == 2
    assert [p["sk"] for p in table.puts] == [
        f"{CATALOG_UUID}#ac-2#{DIGEST}",
        f"{CATALOG_UUID}#au-4#{DIGEST}",
    ]

    # Re-delivery against the real (moto) table converges to the same rows.
    real = _make_index(dynamodb_resource)
    real.record_controls(**args)
    real.record_controls(**args)
    rows = dynamodb_resource.Table(CONTROL_CLAIMS_TABLE_NAME).scan()["Items"]
    assert len(rows) == 2


def test_record_controls_surfaces_a_failed_put() -> None:
    import pytest
    from botocore.exceptions import ClientError

    table = _RecordingTable(fail_on=1)
    idx = ControlClaims(CONTROL_CLAIMS_TABLE_NAME, dynamodb_resource=_Resource(table))
    with pytest.raises(ClientError) as excinfo:
        idx.record_controls(
            tenant_id=TENANT,
            manifest_digest=DIGEST,
            catalog_or_profile_uuid=CATALOG_UUID,
            controls=["ac-2", "ac-3", "au-4"],
            denormalized={},
        )
    assert excinfo.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"
    # The first row landed; the failure stopped the fan-out rather than being skipped.
    assert len(table.puts) == 1
