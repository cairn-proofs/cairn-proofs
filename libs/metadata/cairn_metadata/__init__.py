"""DynamoDB access library for the Cairn Provenance_System Metadata_Index.

Scope:
- Metadata_Index table — one row per bundle, keyed ``pk = {tenant-id}`` /
  ``sk = {project-id}#{manifest-digest}`` (tenant-PK isolation,
  [META-001]). Project listings use a ``begins_with`` sort-key condition;
  there is no unscoped ``Scan`` path.
- Control Claims table (``ControlClaims``) — one row per (catalog/profile,
  control, bundle) tuple, queried by the composite control-UUID PK
  ([PCLAIM-001]).
- Identity_Registry table (``IdentityRegistry``) — control-plane opaque-id →
  display-name resolution plus tenant→org parentage ([IDREG-001]). Canonical
  mutable state, distinct from the rebuildable Metadata_Index.

Alternate access paths, when added, are Global Secondary Indexes that
themselves lead with ``{tenant-id}`` — never a Local Secondary Index
([META-001.04]).

Implements: conditional-write semantics ([META-007.01], [META-007.02]),
pagination with 100-item page size ([META-009.01], [META-010.01]), and
rebuildability helpers ([META-012]).
"""

from cairn_metadata.access import MetadataIndex
from cairn_metadata.conditional import (
    ConditionalUpdateFailed,
    create_record,
    update_record_if_newer,
    upsert_record_if_newer,
)
from cairn_metadata.control_claims import ControlClaims
from cairn_metadata.keys import (
    partition_key,
    sort_key,
    sort_key_prefix,
)
from cairn_metadata.pagination import PageIterator, paginate_query
from cairn_metadata.registry import IdentityRegistry

__all__ = [
    "ConditionalUpdateFailed",
    "ControlClaims",
    "IdentityRegistry",
    "MetadataIndex",
    "PageIterator",
    "create_record",
    "paginate_query",
    "partition_key",
    "sort_key",
    "sort_key_prefix",
    "update_record_if_newer",
    "upsert_record_if_newer",
]
