"""Tests for cairn_metadata.keys (tenant-PK / sub-hierarchy-SK construction; [META-001])."""

from __future__ import annotations

import pytest
from cairn_metadata.keys import partition_key, sort_key, sort_key_prefix
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

DIGEST = "sha256:" + "a" * 64

# ---------------------------------------------------------------------------
# partition_key — the exact-match {tenant-id} isolation boundary
# ---------------------------------------------------------------------------


def test_partition_key_is_tenant_id() -> None:
    assert partition_key("tnt-01JABCD2EF3GH4JK5MN6PQ7RS8") == "tnt-01JABCD2EF3GH4JK5MN6PQ7RS8"


def test_partition_key_empty_raises() -> None:
    with pytest.raises(ValueError):
        partition_key("")


def test_partition_key_rejects_hash() -> None:
    # '#' is the sort-key delimiter; an embedded '#' in the tenant id would be
    # ambiguous against the sort-key grammar. Defensive: reject.
    with pytest.raises(ValueError):
        partition_key("tnt#evil")


# ---------------------------------------------------------------------------
# sort_key — {project-id}#{manifest-digest}
# ---------------------------------------------------------------------------


def test_sort_key_concatenates_project_and_digest() -> None:
    assert sort_key("prj-alpha", DIGEST) == f"prj-alpha#{DIGEST}"


def test_sort_key_empty_project_raises() -> None:
    with pytest.raises(ValueError):
        sort_key("", DIGEST)


def test_sort_key_rejects_hash_in_project() -> None:
    with pytest.raises(ValueError):
        sort_key("prj#evil", DIGEST)


def test_sort_key_rejects_missing_digest_prefix() -> None:
    with pytest.raises(ValueError):
        sort_key("prj-alpha", "a" * 64)


def test_sort_key_rejects_wrong_algo() -> None:
    with pytest.raises(ValueError):
        sort_key("prj-alpha", "md5:abc")


# ---------------------------------------------------------------------------
# sort_key_prefix — the begins_with project listing prefix
# ---------------------------------------------------------------------------


def test_sort_key_prefix_is_project_then_delimiter() -> None:
    assert sort_key_prefix("prj-alpha") == "prj-alpha#"


def test_sort_key_prefix_empty_raises() -> None:
    with pytest.raises(ValueError):
        sort_key_prefix("")


def test_sort_key_prefix_rejects_hash() -> None:
    with pytest.raises(ValueError):
        sort_key_prefix("prj#evil")


# ---------------------------------------------------------------------------
# [META-001] / [META-004.01] — the project sort key begins with the prefix,
# and the random digest suffix spreads writes across the sort-key range.
# ---------------------------------------------------------------------------

_id_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
_id = st.text(alphabet=_id_alphabet, min_size=1, max_size=24)
_digest = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64).map(
    lambda h: f"sha256:{h}"
)


@given(project=_id, digest=_digest)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_sort_key_begins_with_project_prefix(project: str, digest: str) -> None:
    """[META-004.01] — for any valid (project, digest), the sort key starts with
    the project prefix, so a begins_with('{project}#') listing matches it."""
    sk = sort_key(project, digest)
    assert sk.startswith(sort_key_prefix(project))
    head, _, tail = sk.partition("#")
    assert head == project
    assert tail == digest


@given(tenant=_id)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_partition_key_is_whole_tenant_id_not_a_prefix(tenant: str) -> None:
    """[META-001] — the partition key is exactly the tenant id (LeadingKeys is
    exact-match, never a partition-key prefix)."""
    assert partition_key(tenant) == tenant
    assert "#" not in partition_key(tenant)
