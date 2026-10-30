"""IdentityRegistry — control-plane opaque-id → display-name resolution ([IDREG-001]).

The **Identity_Registry** is a DynamoDB table, SEPARATE from the
Metadata_Index, that resolves the opaque identity coordinates the system
carries throughout the ECR/S3 path, the signed claim, and the index. It:

1. Maps each opaque ``{org-id}`` / ``{tenant-id}`` / ``{project-id}`` to its
   human-readable display name, keyed by the opaque id.
2. Records each tenant's parentage — the ``{org-id}`` a ``{tenant-id}``
   belongs to — so every tenant resolves to exactly one org.

It is canonical, mutable control-plane state — encrypted at rest and
IAM-gated like every other DynamoDB table — and DISTINCT from the
rebuildable Metadata_Index projection. Losing it costs only display-name
resolution, never artifact identity, discoverability, or verifiability.

Display names live ONLY here — never in the ECR/S3 path, the signed
producer claim, or an ECR resource tag (those are operator/console-visible).
Per-bundle Metadata_Index records carry only the opaque coordinates;
consumers resolve a display name by JOINING against this registry at read
time (no per-row denormalization), so a rename is a single-item write rather
than an N-row rewrite ([IDREG-001.01]).

Table key shape (one item per opaque id):
  PK: ``id``    — the opaque ``{org-id}`` / ``{tenant-id}`` / ``{project-id}``

Item attributes:
  ``id``           — the opaque identifier (partition key)
  ``kind``         — ``"org"`` | ``"tenant"`` | ``"project"``
  ``displayName``  — the human-readable name (the only place it lives)
  ``orgId``        — present on a ``tenant`` item: its parent ``{org-id}``
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import boto3

ORG = "org"
TENANT = "tenant"
PROJECT = "project"
_KINDS = frozenset({ORG, TENANT, PROJECT})


class IdentityRegistry:
    """Facade for the control-plane Identity_Registry table.

    Holds one boto3 DynamoDB Table resource per instance; safe to share
    across handler invocations within a Lambda execution environment.
    """

    def __init__(
        self,
        table_name: str,
        *,
        dynamodb_resource: Any | None = None,
    ) -> None:
        self.table_name = table_name
        self._resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._resource.Table(table_name)

    # -- writes (control-plane mutation) ----------------------------------

    def put_org(self, *, org_id: str, display_name: str) -> None:
        """Register an org's opaque id → display name."""
        self._put(kind=ORG, opaque_id=org_id, display_name=display_name)

    def put_tenant(self, *, tenant_id: str, org_id: str, display_name: str) -> None:
        """Register a tenant's opaque id → display name AND its org parentage.

        Every tenant belongs to exactly one org ([IDREG-001] item 2 / design
        D.9), so ``org_id`` is required and recorded on the tenant item.
        """
        if not org_id:
            raise ValueError("org_id is required: every tenant belongs to exactly one org")
        self._put(kind=TENANT, opaque_id=tenant_id, display_name=display_name, org_id=org_id)

    def put_project(self, *, project_id: str, display_name: str) -> None:
        """Register a project's opaque id → display name."""
        self._put(kind=PROJECT, opaque_id=project_id, display_name=display_name)

    def _put(
        self,
        *,
        kind: str,
        opaque_id: str,
        display_name: str,
        org_id: str | None = None,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}; got {kind!r}")
        if not opaque_id:
            raise ValueError("opaque_id is required")
        if not display_name:
            raise ValueError("display_name is required")
        item: dict[str, Any] = {"id": opaque_id, "kind": kind, "displayName": display_name}
        if org_id is not None:
            item["orgId"] = org_id
        self._table.put_item(Item=item)

    def set_display_name(self, *, opaque_id: str, display_name: str) -> None:
        """Rename an org/tenant/project — a SINGLE-item write ([IDREG-001.01]).

        Updates only the ``displayName`` attribute on the item keyed by the
        opaque id, requiring the item to already exist. No Metadata_Index
        record is touched: those carry the unchanged opaque id and resolve the
        new name on the next read-time join.
        """
        if not display_name:
            raise ValueError("display_name is required")
        from botocore.exceptions import ClientError

        try:
            self._table.update_item(
                Key={"id": opaque_id},
                UpdateExpression="SET displayName = :dn",
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeValues={":dn": display_name},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise KeyError(f"no identity-registry item for id={opaque_id!r}") from e
            raise

    # -- reads (read-time join) -------------------------------------------

    def get_display_name(self, opaque_id: str) -> str | None:
        """Resolve a single opaque id to its display name, or None if unknown."""
        if not opaque_id:
            raise ValueError("opaque_id is required")
        item = self._table.get_item(Key={"id": opaque_id}).get("Item")
        return item.get("displayName") if item else None

    def resolve_display_names(self, opaque_ids: Iterable[str]) -> dict[str, str]:
        """Batch-resolve opaque ids → display names (the read-time join).

        Unknown ids are simply absent from the returned mapping; callers
        render the opaque id itself when a name is missing. Empty input
        returns an empty mapping without any DynamoDB call.
        """
        unique_ids = {i for i in opaque_ids if i}
        if not unique_ids:
            return {}
        resolved: dict[str, str] = {}
        pending = list(unique_ids)
        # batch_get_item caps at 100 keys per request; chunk defensively.
        for start in range(0, len(pending), 100):
            chunk = pending[start : start + 100]
            request = {self.table_name: {"Keys": [{"id": i} for i in chunk]}}
            while request:
                response = self._resource.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    name = item.get("displayName")
                    if name is not None:
                        resolved[item["id"]] = name
                # Re-issue any unprocessed keys until DynamoDB drains them.
                request = response.get("UnprocessedKeys") or None
        return resolved

    def get_tenant_org(self, tenant_id: str) -> str | None:
        """Return the ``{org-id}`` a tenant belongs to, or None if unknown ([IDREG-001] item 2)."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        item = self._table.get_item(Key={"id": tenant_id}).get("Item")
        return item.get("orgId") if item else None


__all__ = ["ORG", "PROJECT", "TENANT", "IdentityRegistry"]
