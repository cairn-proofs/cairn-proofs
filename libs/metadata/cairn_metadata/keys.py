"""Partition + sort key construction for Metadata_Index records
([META-001], [META-004.01], [META-012]).

Per the tenancy key schema ([META-001]):

- Partition key is the opaque ``{tenant-id}`` — the isolation boundary,
  scoped exact-match via ``dynamodb:LeadingKeys`` (NOT a prefix).
- Sort key is ``{project-id}#{manifest-digest}`` — the queryable
  sub-hierarchy. A project listing is a ``begins_with('{project-id}#')``
  sort-key condition under the tenant partition; the random
  ``{manifest-digest}`` suffix spreads writes across the sort-key range so
  DynamoDB can split a tenant's item collection across partitions.

``{org-id}`` is NOT a key component on any tenant-PK plane: it rides as a
DynamoDB attribute on the record and as the ECR/S3 path prefix ([META-001]
item 5).
"""

from __future__ import annotations

_SK_DELIMITER = "#"


def partition_key(tenant_id: str) -> str:
    """``{tenant-id}`` — the partition key and exact-match isolation boundary.

    ``dynamodb:LeadingKeys`` scopes a caller to this exact value, so the
    tenant id is the whole partition key (never a prefix of a wider key).
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for the Metadata_Index partition key")
    if _SK_DELIMITER in tenant_id:
        raise ValueError(f"tenant_id MUST NOT contain {_SK_DELIMITER!r}")
    return tenant_id


def sort_key(project_id: str, manifest_digest: str) -> str:
    """``{project-id}#{manifest-digest}`` — the queryable sub-hierarchy sort key.

    The ``{project-id}`` prefix lets a project listing use ``begins_with``
    (see :func:`sort_key_prefix`); the ``{manifest-digest}`` suffix makes the
    key unique per bundle within a (tenant, project) namespace and spreads
    writes across the sort-key range.
    """
    if not project_id:
        raise ValueError("project_id is required for the Metadata_Index sort key")
    if _SK_DELIMITER in project_id:
        raise ValueError(f"project_id MUST NOT contain {_SK_DELIMITER!r}")
    if not manifest_digest.startswith("sha256:"):
        raise ValueError(f"manifest_digest must be a sha256 string; got {manifest_digest!r}")
    return f"{project_id}{_SK_DELIMITER}{manifest_digest}"


def sort_key_prefix(project_id: str) -> str:
    """``{project-id}#`` — the ``begins_with`` prefix for a project listing ([META-004.01])."""
    if not project_id:
        raise ValueError("project_id is required for the project sort-key prefix")
    if _SK_DELIMITER in project_id:
        raise ValueError(f"project_id MUST NOT contain {_SK_DELIMITER!r}")
    return f"{project_id}{_SK_DELIMITER}"
