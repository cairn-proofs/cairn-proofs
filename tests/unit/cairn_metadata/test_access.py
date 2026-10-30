"""Tests for cairn_metadata.access (MetadataIndex facade; tenant-PK schema)."""

from __future__ import annotations

import pytest
from cairn_metadata.access import MetadataIndex
from cairn_metadata.conditional import ConditionalUpdateFailed

from tests.unit.cairn_metadata.conftest import TABLE_NAME

DIGEST = "sha256:" + "0" * 64
TENANT = "tnt-01JABCD2EF3GH4JK5MN6PQ7RS8"
PROJECT = "prj-alpha-bank"


def _make_index(resource) -> MetadataIndex:
    return MetadataIndex(TABLE_NAME, dynamodb_resource=resource)


def test_create_on_validated_persists_record_with_tenant_pk(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={
            "status": "validated",
            "catalogOrProfileUuid": "22222222-2222-4222-8222-222222222222",
            "orgId": "org-01J",
        },
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored is not None
    assert stored["pk"] == TENANT
    assert stored["sk"] == f"{PROJECT}#{DIGEST}"
    assert stored["status"] == "validated"
    # org-id rides as a plain attribute, never a key component ([META-001] item 5).
    assert stored["orgId"] == "org-01J"


def test_create_or_merge_creates_then_merges_union(dynamodb_resource) -> None:
    """The facade converges the row to the union of two complementary writers."""
    idx = _make_index(dynamodb_resource)
    idx.create_or_merge(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"validateStatus": "validated", "name": "cat"},
    )
    idx.create_or_merge(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"oscalLayers": ["catalog"], "controls": ["ac-1"]},
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored is not None
    assert stored["pk"] == TENANT
    assert stored["sk"] == f"{PROJECT}#{DIGEST}"
    assert stored["validateStatus"] == "validated"
    assert stored["name"] == "cat"
    assert stored["oscalLayers"] == ["catalog"]
    assert stored["controls"] == ["ac-1"]


def test_create_or_merge_does_not_seed_updated_at(dynamodb_resource) -> None:
    """Unlike create_on_validated, the write-once merge path must not mirror
    createdAt into updatedAt (updatedAt is owned by the mutable guard)."""
    idx = _make_index(dynamodb_resource)
    idx.create_or_merge(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"validateStatus": "validated", "createdAt": "2026-05-26T12:05:00Z"},
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored is not None
    assert "updatedAt" not in stored


def test_create_on_validated_duplicate_raises_conditional_failure(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"status": "validated"},
    )
    with pytest.raises(ConditionalUpdateFailed):
        idx.create_on_validated(
            manifest_digest=DIGEST,
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"status": "validated"},
        )


_ATT_A = "sha256:" + "a" * 64
_ATT_B = "sha256:" + "b" * 64


def test_add_validated_attestation_round_trips_via_get_record(dynamodb_resource) -> None:
    """The facade keys the row identically to get_record (add then read shows the member)."""
    idx = _make_index(dynamodb_resource)
    idx.add_validated_attestation(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        attestation_digest=_ATT_A,
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored is not None
    assert stored["pk"] == TENANT
    assert stored["sk"] == f"{PROJECT}#{DIGEST}"
    assert stored["validatedAttestationDigests"] == {_ATT_A}


def test_add_validated_attestation_is_idempotent_on_re_delivery(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    for _ in range(3):
        idx.add_validated_attestation(
            manifest_digest=DIGEST,
            tenant_id=TENANT,
            project_id=PROJECT,
            attestation_digest=_ATT_A,
        )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored["validatedAttestationDigests"] == {_ATT_A}


def test_add_validated_attestation_accumulates_distinct_referrers(dynamodb_resource) -> None:
    """A second, distinct attestation over the same content subject accumulates."""
    idx = _make_index(dynamodb_resource)
    idx.add_validated_attestation(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        attestation_digest=_ATT_A,
    )
    idx.add_validated_attestation(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        attestation_digest=_ATT_B,
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored["validatedAttestationDigests"] == {_ATT_A, _ATT_B}


def test_add_validated_attestation_does_not_clobber_status_fields(dynamodb_resource) -> None:
    """The ADD never disturbs the row's mutable status / marker fields."""
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"signingStatus": "signed", "archiveStatus": "archived", "createdAt": "2026-05-24"},
    )
    idx.add_validated_attestation(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        attestation_digest=_ATT_A,
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored["validatedAttestationDigests"] == {_ATT_A}
    assert stored["signingStatus"] == "signed"
    assert stored["archiveStatus"] == "archived"


def test_get_record_returns_none_when_absent(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    assert idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id="missing") is None


def test_update_on_event_applies_when_newer(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"status": "validated"},
    )
    idx.update_on_event(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        updates={"status": "signed"},
        event_timestamp="2026-05-24T01:00:00Z",
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored["status"] == "signed"


def test_update_on_event_rejected_when_stale(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"status": "validated"},
    )
    idx.update_on_event(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        updates={"status": "signed"},
        event_timestamp="2026-05-24T05:00:00Z",
    )
    with pytest.raises(ConditionalUpdateFailed):
        idx.update_on_event(
            manifest_digest=DIGEST,
            tenant_id=TENANT,
            project_id=PROJECT,
            updates={"status": "archived"},
            event_timestamp="2026-05-24T01:00:00Z",  # older
        )


def test_upsert_on_event_creates_when_absent(dynamodb_resource) -> None:
    """[META-007] out-of-order delivery: a post-validated event lands cleanly
    even before the originating validated event creates the row."""
    idx = _make_index(dynamodb_resource)
    idx.upsert_on_event(
        manifest_digest=DIGEST,
        tenant_id=TENANT,
        project_id=PROJECT,
        updates={"signingStatus": "signed"},
        event_timestamp="2026-05-24T02:00:00Z",
    )
    stored = idx.get_record(manifest_digest=DIGEST, tenant_id=TENANT, project_id=PROJECT)
    assert stored is not None
    assert stored["pk"] == TENANT
    assert stored["sk"] == f"{PROJECT}#{DIGEST}"
    assert stored["signingStatus"] == "signed"


def test_query_by_project_returns_all_seeded_items(dynamodb_resource, dynamodb_table) -> None:
    idx = _make_index(dynamodb_resource)
    for i in range(5):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"status": "validated", "i": i},
        )
    page = idx.query_by_project(tenant_id=TENANT, project_id=PROJECT)
    assert len(page.items) == 5


def test_query_by_project_is_tenant_and_project_scoped(dynamodb_resource, dynamodb_table) -> None:
    """[META-001.04] / [META-004.01]: the begins_with project listing under one
    tenant partition never crosses the tenant boundary nor leaks a sibling
    project in the same tenant."""
    idx = _make_index(dynamodb_resource)
    # Same project id under a DIFFERENT tenant — must not appear.
    other_tenant = "tnt-OTHEROTHEROTHEROTHEROTHER0"
    idx.create_on_validated(
        manifest_digest="sha256:" + "e" * 64,
        tenant_id=other_tenant,
        project_id=PROJECT,
        record={"status": "validated"},
    )
    # A sibling project in the SAME tenant — must not appear in a PROJECT query.
    idx.create_on_validated(
        manifest_digest="sha256:" + "f" * 64,
        tenant_id=TENANT,
        project_id="prj-sibling",
        record={"status": "validated"},
    )
    # The target rows.
    for i in range(3):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"status": "validated"},
        )
    page = idx.query_by_project(tenant_id=TENANT, project_id=PROJECT)
    assert len(page.items) == 3
    assert all(item["pk"] == TENANT for item in page.items)
    assert all(item["sk"].startswith(f"{PROJECT}#") for item in page.items)

    # Cross-tenant query for the same project id sees only the other tenant's row.
    other_page = idx.query_by_project(tenant_id=other_tenant, project_id=PROJECT)
    assert len(other_page.items) == 1
    assert other_page.items[0]["pk"] == other_tenant


def test_query_by_project_status_filter_narrows_in_partition(
    dynamodb_resource, dynamodb_table
) -> None:
    """[META-001.03]: a core project query may filter on a system-set status
    in-partition (key condition + FilterExpression), no Scan."""
    idx = _make_index(dynamodb_resource)
    for i in range(3):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"signingStatus": "signed"},
        )
    for i in range(3, 5):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"signingStatus": "pending"},
        )
    page = idx.query_by_project(
        tenant_id=TENANT,
        project_id=PROJECT,
        status_filter={"signingStatus": "signed"},
    )
    assert len(page.items) == 3
    assert all(item["signingStatus"] == "signed" for item in page.items)


def test_query_by_project_multi_status_filter_is_conjunctive(
    dynamodb_resource, dynamodb_table
) -> None:
    """[META-001.03]: multiple system-set status attributes AND together."""
    idx = _make_index(dynamodb_resource)
    idx.create_on_validated(
        manifest_digest="sha256:" + "1" * 64,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"signingStatus": "signed", "archiveStatus": "archived"},
    )
    idx.create_on_validated(
        manifest_digest="sha256:" + "2" * 64,
        tenant_id=TENANT,
        project_id=PROJECT,
        record={"signingStatus": "signed", "archiveStatus": "pending"},
    )
    page = idx.query_by_project(
        tenant_id=TENANT,
        project_id=PROJECT,
        status_filter={"signingStatus": "signed", "archiveStatus": "archived"},
    )
    assert len(page.items) == 1
    assert page.items[0]["archiveStatus"] == "archived"


def test_query_by_project_worm_status_filter_narrows_in_partition(
    dynamodb_resource, dynamodb_table
) -> None:
    """[META-001.03]: ``wormStatus`` is a system-bounded status dimension the
    pipeline sets, so a project query may filter on it in-partition — the third
    member of the {signingStatus, archiveStatus, wormStatus} filter set named by
    the spec, exercised here so the whole set (not just signing/archive) is
    pinned as an accepted, in-partition filter."""
    idx = _make_index(dynamodb_resource)
    for i in range(2):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"wormStatus": "archived"},
        )
    for i in range(2, 5):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"wormStatus": "pending"},
        )
    page = idx.query_by_project(
        tenant_id=TENANT,
        project_id=PROJECT,
        status_filter={"wormStatus": "archived"},
    )
    assert len(page.items) == 2
    assert all(item["wormStatus"] == "archived" for item in page.items)


def test_query_by_project_rejects_non_system_filter_before_dynamodb(dynamodb_resource) -> None:
    """[META-001.02]: a producer-claim filter dimension is rejected before any
    DynamoDB call — no table resource is touched."""

    class _ExplodingResource:
        def Table(self, _name: str):
            raise AssertionError("DynamoDB must not be touched for a rejected filter")

    idx = MetadataIndex(TABLE_NAME, dynamodb_resource=dynamodb_resource)
    # Swap in a table that fails loudly if queried, proving rejection precedes
    # any DynamoDB call.
    idx._table = _ExplodingResource()
    with pytest.raises(ValueError, match="assessor"):
        idx.query_by_project(
            tenant_id=TENANT,
            project_id=PROJECT,
            status_filter={"assessor": "auditor-x"},
        )


def test_iterate_by_project_streams_all_records(dynamodb_resource) -> None:
    idx = _make_index(dynamodb_resource)
    for i in range(7):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=TENANT,
            project_id=PROJECT,
            record={"i": i},
        )
    seen = list(idx.iterate_by_project(tenant_id=TENANT, project_id=PROJECT))
    assert len(seen) == 7


def test_no_unscoped_scan_or_lsi_on_metadata_plane(dynamodb_resource, dynamodb_table) -> None:
    """[META-001.04]: the Metadata_Index plane carries no Local Secondary Index,
    and the facade exposes no unscoped Scan entry point — every read is keyed by
    an exact-match tenant partition key."""
    description = dynamodb_table.meta.client.describe_table(TableName=TABLE_NAME)["Table"]
    assert not description.get("LocalSecondaryIndexes")
    # No facade method issues a Scan; the read surface is exact-match tenant-PK.
    public_read_methods = {name for name in dir(MetadataIndex) if not name.startswith("_")}
    assert "scan" not in {m.lower() for m in public_read_methods}
