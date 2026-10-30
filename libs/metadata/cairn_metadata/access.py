"""MetadataIndex facade — high-level entry points for the four pipeline stages.

Wraps the lower-level conditional-write and pagination helpers in a thin
class that owns the boto3 DynamoDB Table resource and exposes the
operations each Lambda needs.

Tenant-PK key schema ([META-001]):

- Partition key ``pk`` = the opaque ``{tenant-id}`` — the isolation
  boundary, scoped exact-match via ``dynamodb:LeadingKeys``.
- Sort key ``sk`` = ``{project-id}#{manifest-digest}`` — a project listing
  is a ``begins_with('{project-id}#')`` condition under the tenant partition.

Every read supplies an exact-match ``{tenant-id}`` partition key; there is
no unscoped ``Scan`` path on this facade ([META-001.04]). Alternate access
paths, when added, are Global Secondary Indexes that themselves lead with
``{tenant-id}`` — never a Local Secondary Index.
"""

from __future__ import annotations

from typing import Any

import boto3

from cairn_metadata.conditional import (
    ConditionalUpdateFailed,
    add_to_string_set,
    create_or_merge,
    create_record,
    update_record_if_newer,
    upsert_record_if_newer,
)
from cairn_metadata.keys import partition_key, sort_key, sort_key_prefix
from cairn_metadata.pagination import Page, PageIterator, paginate_query

# The only attributes a project query may filter on in-partition: the
# pipeline-set status fields. These are system-bounded (the pipeline writes
# them, never the producer), so filtering on them keeps the Metadata_Index
# record queryable only by system-bounded dimensions ([META-001]).
_SYSTEM_STATUS_FILTER_ATTRS = frozenset({"signingStatus", "archiveStatus", "wormStatus"})


class MetadataIndex:
    """Facade for reading and writing Artifact_Bundle metadata records.

    Holds one boto3 DynamoDB Table resource per instance; safe to share
    across handler invocations within a Lambda execution environment.

    All write and read entry points take the opaque identity coordinates
    ``tenant_id`` + ``project_id`` + ``manifest_digest``: ``tenant_id`` keys
    the partition (the exact-match isolation boundary), and
    ``{project_id}#{manifest_digest}`` keys the sort. ``org_id``, when
    supplied, is carried as a plain attribute on the record — never a key
    component ([META-001] item 5).
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

    @staticmethod
    def _keys(*, tenant_id: str, project_id: str, manifest_digest: str) -> tuple[str, str]:
        return partition_key(tenant_id), sort_key(project_id, manifest_digest)

    def create_on_validated(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        record: dict[str, Any],
    ) -> None:
        """Create the initial record at validate-bundle time ([META-007]).

        Mirrors `createdAt` into `updatedAt` if the caller did not supply the
        latter, so every record has the timestamp attribute the subsequent
        `update_record_if_newer` guard compares against. Without this, the
        first post-create update would silently skip the `#ts < :event_ts`
        check via the `attribute_not_exists(#ts)` branch — correct for the
        first update but brittle for retried/out-of-order events.
        """
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        item = {**record, "pk": pk, "sk": sk}
        if "updatedAt" not in item and "createdAt" in item:
            item["updatedAt"] = item["createdAt"]
        create_record(self._table, item)

    def create_or_merge(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        record: dict[str, Any],
    ) -> None:
        """Create the row, or merge only-absent write-once fields onto it.

        Used by BOTH validated-event writers (validate's marker fields and
        update-metadata's rich OSCAL payload) for their WRITE-ONCE
        contributions. The row converges to the union of both stages'
        complementary field sets regardless of create order, and never
        overwrites a value already present (peer or later mutable transition).
        Repeated application is a true no-op. See
        :func:`cairn_metadata.conditional.create_or_merge`.

        Mirrors ``create_on_validated``'s signature and keying. Does NOT mirror
        ``createdAt`` into ``updatedAt``: ``updatedAt`` is owned by the mutable
        transition guard, and the write-once merge path must not seed or advance
        it (callers on this path do not contribute it).
        """
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        item = {**record, "pk": pk, "sk": sk}
        create_or_merge(self._table, item)

    def update_on_event(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        updates: dict[str, Any],
        event_timestamp: str,
    ) -> None:
        """Apply a signed/archived/signoff update with event-timestamp guard."""
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        update_record_if_newer(
            self._table,
            pk=pk,
            sk=sk,
            updates=updates,
            event_timestamp=event_timestamp,
        )

    def upsert_on_event(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        updates: dict[str, Any],
        event_timestamp: str,
    ) -> None:
        """Same as `update_on_event` but does NOT require the record to exist.

        Used by event handlers that may legitimately land before the
        originating `artifact.validated` event under at-least-once,
        out-of-order EventBridge delivery — specifically signing-failed,
        signed, archived, and archive-failed. See the conditional helper
        docstring for the race-mitigation discussion.
        """
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        upsert_record_if_newer(
            self._table,
            pk=pk,
            sk=sk,
            updates=updates,
            event_timestamp=event_timestamp,
        )

    def add_validated_attestation(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
        attestation_digest: str,
    ) -> None:
        """Record one validated Producer_Attestation_Referrer digest onto the content row.

        Keyed identically to ``create_or_merge``/``get_record`` — by the CONTENT
        Manifest_Digest, not the attestation digest — this accumulates
        ``attestation_digest`` into the row's ``validatedAttestationDigests``
        String Set via an idempotent ``ADD`` (see
        :func:`cairn_metadata.conditional.add_to_string_set`). Re-delivery of the
        same attestation is a no-op; a distinct attestation over the same content
        subject is added without disturbing prior members or peer/transition
        fields (``signingStatus``/``archiveStatus``/``updatedAt``).

        The validate stage's re-seen short-circuit reads this set back via
        ``get_record`` so the (content Manifest_Digest, Producer_Attestation_Referrer
        digest) pair — not the content digest alone — is the dedup unit: a
        re-pushed same attestation skips, a genuinely new attestation processes.
        """
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        add_to_string_set(
            self._table,
            pk=pk,
            sk=sk,
            attr="validatedAttestationDigests",
            values={attestation_digest},
        )

    def get_record(
        self,
        *,
        manifest_digest: str,
        tenant_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        """Read one record by (pk, sk). Returns None when absent."""
        pk, sk = self._keys(
            tenant_id=tenant_id, project_id=project_id, manifest_digest=manifest_digest
        )
        response = self._table.get_item(Key={"pk": pk, "sk": sk})
        return response.get("Item")

    def query_by_project(
        self,
        *,
        tenant_id: str,
        project_id: str,
        status_filter: dict[str, str] | None = None,
        page_token: str | None = None,
        limit: int = 100,
    ) -> Page:
        """[META-004]: list bundles for a project (one DynamoDB page).

        Issues a tenant-scoped ``Query``: the partition key is the exact-match
        ``{tenant-id}`` and the sort-key condition is
        ``begins_with('{project-id}#')``. This is never a ``Scan``
        ([META-001.04]).

        `status_filter`, when supplied, narrows the page to bundles matching
        a pipeline-set status in-partition ([META-001.03]). Its keys are
        restricted to the system-set status attributes — `signingStatus`,
        `archiveStatus`, `wormStatus` — which the pipeline writes, not the
        producer. A filter naming any other attribute (a producer-claim
        dimension such as `assessor`) is rejected before any DynamoDB call
        ([META-001.02]): the Metadata_Index record is queryable only by
        system-bounded dimensions. The filter rides the same key Query, so it
        is an in-partition filter, never a table Scan.
        """
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :skprefix)",
            "ExpressionAttributeValues": {
                ":pk": partition_key(tenant_id),
                ":skprefix": sort_key_prefix(project_id),
            },
        }
        if status_filter:
            disallowed = set(status_filter) - _SYSTEM_STATUS_FILTER_ATTRS
            if disallowed:
                raise ValueError(
                    "status_filter may only constrain system-set status "
                    f"attributes {sorted(_SYSTEM_STATUS_FILTER_ATTRS)}; got "
                    f"disallowed {sorted(disallowed)}"
                )
            names: dict[str, str] = {}
            clauses: list[str] = []
            for i, (attr, value) in enumerate(sorted(status_filter.items())):
                name_ph = f"#sf{i}"
                value_ph = f":sf{i}"
                names[name_ph] = attr
                query_kwargs["ExpressionAttributeValues"][value_ph] = value
                clauses.append(f"{name_ph} = {value_ph}")
            query_kwargs["FilterExpression"] = " AND ".join(clauses)
            query_kwargs["ExpressionAttributeNames"] = names
        return paginate_query(
            self._table,
            page_token=page_token,
            limit=limit,
            **query_kwargs,
        )

    def iterate_by_project(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> PageIterator:
        """Lazy iterator across all pages for a project (tenant-scoped)."""
        return PageIterator(
            self._table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :skprefix)",
            ExpressionAttributeValues={
                ":pk": partition_key(tenant_id),
                ":skprefix": sort_key_prefix(project_id),
            },
        )


__all__ = ["ConditionalUpdateFailed", "MetadataIndex", "Page", "PageIterator"]
