"""ControlClaims — the Control Claims table for (control → bundle) queries.

The Metadata_Index holds one row per bundle. The **Control Claims table**
holds the (catalog/profile, control) → bundle relationship — one row per
(control, bundle) pair, with the denormalized attributes auditors need to
satisfy the typical query ("show me every bundle citing AC-2 against
profile X with its signing/archive status") in a single Query, without
dereferencing back to Metadata_Index per hit.

Primary key shape of the control-claims projection (`producer-claims`
[PCLAIM-001]). The projection is a tenant-PK plane and carries the uniform
physical key schema metadata-index [META-001] mandates for every such plane:
  PK: `{tenant-id}` — the isolation boundary, scoped exact-match via
      ``dynamodb:LeadingKeys`` (NOT a prefix).
  SK: `{catalogOrProfileUuid}#{controlId}#{manifestDigest}` — a
      (catalog/profile, control) listing is a
      ``begins_with('{catalogOrProfileUuid}#{controlId}#')`` condition under
      the tenant partition.

The `catalogOrProfileUuid` leading SK segment guarantees that the same
`controlId` token under different OSCAL catalogs / profiles (e.g., "ac-2"
in NIST 800-53 rev5 vs in a FedRAMP profile) never collides — OSCAL token
IDs are only unique within their containing document per the OSCAL spec.

Producer chooses Catalog vs Profile UUID based on the compliance framing
they're claiming (see artifact-ingestion [INGEST-016]). Cairn trusts the
producer's choice and does not dereference catalog↔profile import chains.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import boto3

from cairn_metadata.pagination import Page, paginate_query


class ControlClaims:
    """Facade for the Control Claims table.

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

    def record_controls(
        self,
        *,
        tenant_id: str,
        manifest_digest: str,
        catalog_or_profile_uuid: str,
        controls: Iterable[str],
        denormalized: dict[str, Any],
    ) -> int:
        """Fan out one row per control on the bundle.

        Uses boto3's ``Table.batch_writer`` which automatically chunks
        into 25-item BatchWriteItem requests AND retries UnprocessedItems
        — so a single call cleanly handles bundles citing more controls
        than fit in one DynamoDB request.

        Args:
            tenant_id: The caller's opaque ``{tenant-id}`` — the partition
                key and exact-match isolation boundary on every row
                ([PCLAIM-001], [META-001]). Derived from the AWS-set
                identity triple, never from producer content.
            manifest_digest: Bundle's canonical identifier ([INGEST-026]);
                the trailing SK segment on every row.
            catalog_or_profile_uuid: Producer-declared OSCAL Catalog or
                Profile UUID; leading SK segment on every row.
            controls: Control IDs (OSCAL token IDs). Duplicates are
                de-duplicated; empty input is a no-op (zero rows written).
            denormalized: Attributes to copy onto every row so that
                auditor queries don't need a follow-up GetItem on the
                Metadata_Index — typically: ``projectId``,
                ``assessmentPeriod``, ``artifactKind``, ``signingStatus``,
                ``archiveStatus``, ``wormStatus``, ``createdAt``,
                ``ecrRepoPath``. The keys ``pk``, ``sk``, ``tenantId``,
                ``catalogOrProfileUuid``, ``controlId``, and
                ``manifestDigest`` are reserved and overwritten by this
                method if supplied.

        Returns:
            Count of rows written (after de-duplication of ``controls``).
        """
        unique_controls = sorted(set(controls))
        if not unique_controls:
            return 0
        # Strip reserved keys so a caller can pass through a record dict
        # without manual filtering. The key-shape is enforced here — a
        # caller cannot override the key-derived tenant via ``denormalized``.
        reserved = {
            "pk",
            "sk",
            "tenantId",
            "catalogOrProfileUuid",
            "controlId",
            "manifestDigest",
        }
        base = {k: v for k, v in denormalized.items() if k not in reserved}
        with self._table.batch_writer() as batch:
            for control_id in unique_controls:
                item = {
                    **base,
                    "pk": tenant_id,
                    "sk": f"{catalog_or_profile_uuid}#{control_id}#{manifest_digest}",
                    "tenantId": tenant_id,
                    "catalogOrProfileUuid": catalog_or_profile_uuid,
                    "controlId": control_id,
                    "manifestDigest": manifest_digest,
                }
                batch.put_item(Item=item)
        return len(unique_controls)

    def query_by_control(
        self,
        tenant_id: str,
        *,
        catalog_or_profile_uuid: str,
        control_id: str,
        page_token: str | None = None,
        limit: int = 100,
    ) -> Page:
        """[PCLAIM-001]: list every bundle in a tenant citing a control under a catalog/profile.

        Issues a tenant-scoped ``Query``: the partition key is the exact-match
        ``{tenant-id}`` and the sort-key condition is
        ``begins_with('{catalogOrProfileUuid}#{controlId}#')``. A query for one
        tenant never returns another tenant's rows, and the same ``controlId``
        under a different catalog/profile is excluded ([PCLAIM-001.05]).
        """
        return paginate_query(
            self._table,
            page_token=page_token,
            limit=limit,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :skprefix)",
            ExpressionAttributeValues={
                ":pk": tenant_id,
                ":skprefix": f"{catalog_or_profile_uuid}#{control_id}#",
            },
        )


__all__ = ["ControlClaims"]
