"""Tests for cairn_metadata.registry (IdentityRegistry facade; [IDREG-001])."""

from __future__ import annotations

import pytest
from cairn_metadata.registry import IdentityRegistry

from tests.unit.cairn_metadata.conftest import IDENTITY_REGISTRY_TABLE_NAME

ORG_ID = "org-01JABCD2EF3GH4JK5MN6PQ7RS8"
TENANT_ID = "tnt-01JXYZW2EF3GH4JK5MN6PQ7RS8"
PROJECT_ID = "prj-01JYBV8X1MZ3K7QF9R2T6WYN4D"


def _make_registry(resource) -> IdentityRegistry:
    return IdentityRegistry(IDENTITY_REGISTRY_TABLE_NAME, dynamodb_resource=resource)


def test_put_and_get_display_name_for_each_kind(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    reg.put_org(org_id=ORG_ID, display_name="Acme Holdings")
    reg.put_tenant(tenant_id=TENANT_ID, org_id=ORG_ID, display_name="Acme Bank")
    reg.put_project(project_id=PROJECT_ID, display_name="Quarterly SOC2")

    assert reg.get_display_name(ORG_ID) == "Acme Holdings"
    assert reg.get_display_name(TENANT_ID) == "Acme Bank"
    assert reg.get_display_name(PROJECT_ID) == "Quarterly SOC2"


def test_get_display_name_unknown_id_returns_none(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    assert reg.get_display_name("org-unknown") is None


def test_put_tenant_records_org_parentage(dynamodb_resource) -> None:
    """[IDREG-001] item 2: every tenant resolves to exactly one org."""
    reg = _make_registry(dynamodb_resource)
    reg.put_tenant(tenant_id=TENANT_ID, org_id=ORG_ID, display_name="Acme Bank")
    assert reg.get_tenant_org(TENANT_ID) == ORG_ID


def test_put_tenant_requires_org_id(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    with pytest.raises(ValueError, match="org_id"):
        reg.put_tenant(tenant_id=TENANT_ID, org_id="", display_name="Acme Bank")


def test_get_tenant_org_unknown_returns_none(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    assert reg.get_tenant_org("tnt-unknown") is None


def test_resolve_display_names_batch_join(dynamodb_resource) -> None:
    """The read-time join resolves many opaque ids in one call ([IDREG-001])."""
    reg = _make_registry(dynamodb_resource)
    reg.put_org(org_id=ORG_ID, display_name="Acme Holdings")
    reg.put_tenant(tenant_id=TENANT_ID, org_id=ORG_ID, display_name="Acme Bank")
    reg.put_project(project_id=PROJECT_ID, display_name="Quarterly SOC2")

    resolved = reg.resolve_display_names([ORG_ID, TENANT_ID, PROJECT_ID, "prj-missing"])
    assert resolved == {
        ORG_ID: "Acme Holdings",
        TENANT_ID: "Acme Bank",
        PROJECT_ID: "Quarterly SOC2",
    }
    # An unknown id is simply absent (caller renders the opaque id itself).
    assert "prj-missing" not in resolved


def test_resolve_display_names_empty_input_no_dynamodb(dynamodb_resource) -> None:
    """Empty input short-circuits without any DynamoDB call."""

    class _ExplodingResource:
        def Table(self, _name: str):
            return object()

        def batch_get_item(self, **_kwargs):
            raise AssertionError("DynamoDB must not be touched for empty input")

    reg = IdentityRegistry(IDENTITY_REGISTRY_TABLE_NAME, dynamodb_resource=_ExplodingResource())
    assert reg.resolve_display_names([]) == {}
    assert reg.resolve_display_names(["", None]) == {}  # type: ignore[list-item]


def test_rename_is_single_item_write_no_index_rewrite(dynamodb_resource) -> None:
    """[IDREG-001.01]: a rename updates only the registry item; the opaque id is
    unchanged, so no Metadata_Index record would be rewritten."""
    reg = _make_registry(dynamodb_resource)
    reg.put_project(project_id=PROJECT_ID, display_name="Old Name")
    reg.set_display_name(opaque_id=PROJECT_ID, display_name="New Name")

    assert reg.get_display_name(PROJECT_ID) == "New Name"
    # The opaque id (the only thing the index carries) is untouched by a rename.
    table = dynamodb_resource.Table(IDENTITY_REGISTRY_TABLE_NAME)
    item = table.get_item(Key={"id": PROJECT_ID})["Item"]
    assert item["id"] == PROJECT_ID
    assert item["kind"] == "project"


def test_rename_unknown_id_raises_keyerror(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    with pytest.raises(KeyError):
        reg.set_display_name(opaque_id="prj-nope", display_name="X")


def test_display_name_lives_only_in_registry_item(dynamodb_resource) -> None:
    """[IDREG-001.02]: display name appears in the registry item; the registry is
    keyed by the opaque id alone (the path/claim/index carry only that id)."""
    reg = _make_registry(dynamodb_resource)
    reg.put_tenant(tenant_id=TENANT_ID, org_id=ORG_ID, display_name="Acme Bank")
    table = dynamodb_resource.Table(IDENTITY_REGISTRY_TABLE_NAME)
    item = table.get_item(Key={"id": TENANT_ID})["Item"]
    assert item["displayName"] == "Acme Bank"
    assert item["id"] == TENANT_ID
    assert item["orgId"] == ORG_ID


def test_facade_reads_deploy_seed_shaped_item_by_id(dynamodb_resource) -> None:
    """The facade resolves a CDK-deploy-seed-shaped item by opaque id.

    Regression for the Identity_Registry key-schema mismatch the security
    review caught: the CDK construct (``cdk/lib/constructs/identity-registry.ts``)
    seeds items keyed on the ``id`` attribute with a ``kind`` discriminator,
    ``displayName``, and (for a tenant) ``orgId`` parentage. This test writes
    that exact seed shape DIRECTLY to the table (bypassing the facade's own
    ``put_*`` writers) and reads it back THROUGH the facade. A schema
    disagreement (e.g. the facade keying on ``pk`` while the seed uses ``id``)
    would raise a key-schema ``ClientError`` here — proving agreement, not a
    silent degrade-to-empty.
    """
    table = dynamodb_resource.Table(IDENTITY_REGISTRY_TABLE_NAME)
    # Exactly the attribute names/shape the deploy-time AwsCustomResource seeds.
    table.put_item(Item={"id": ORG_ID, "kind": "org", "displayName": "Acme Holdings"})
    table.put_item(
        Item={
            "id": TENANT_ID,
            "kind": "tenant",
            "displayName": "Acme Bank",
            "orgId": ORG_ID,
        }
    )
    table.put_item(
        Item={
            "id": PROJECT_ID,
            "kind": "project",
            "displayName": "Quarterly SOC2",
            "tenantId": TENANT_ID,
        }
    )

    reg = _make_registry(dynamodb_resource)
    # Single GetItem read-back by opaque id (would ClientError on key mismatch).
    assert reg.get_display_name(ORG_ID) == "Acme Holdings"
    assert reg.get_display_name(TENANT_ID) == "Acme Bank"
    assert reg.get_display_name(PROJECT_ID) == "Quarterly SOC2"
    # Parentage attribute name (orgId) agrees with the seed too.
    assert reg.get_tenant_org(TENANT_ID) == ORG_ID
    # BatchGetItem join read-back over the same seed-shaped items.
    assert reg.resolve_display_names([ORG_ID, TENANT_ID, PROJECT_ID]) == {
        ORG_ID: "Acme Holdings",
        TENANT_ID: "Acme Bank",
        PROJECT_ID: "Quarterly SOC2",
    }


def test_put_rejects_empty_fields(dynamodb_resource) -> None:
    reg = _make_registry(dynamodb_resource)
    with pytest.raises(ValueError):
        reg.put_org(org_id="", display_name="X")
    with pytest.raises(ValueError):
        reg.put_org(org_id=ORG_ID, display_name="")
