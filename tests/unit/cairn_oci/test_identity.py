"""Tests for the derived-identity primitive (metadata-index Req: deterministic
derived identity over the canonical prefixed Manifest_Digest).

Pins three invariants:
  * the namespace is the frozen ``uuid5(NAMESPACE_DNS, "cairnproofs.dev")``;
  * ``resource_uuid_for`` derives over the canonical PREFIXED ``sha256:<hex>`` form;
  * the bare 64-hex form (no prefix) is rejected — the prefixed form is required.
"""

from __future__ import annotations

import uuid

import pytest
from cairn_oci import CAIRN_NAMESPACE, resource_uuid_for

# Concrete known digest used as the golden vector throughout.
_GOLDEN_HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_GOLDEN_DIGEST = f"sha256:{_GOLDEN_HEX}"


def test_namespace_is_frozen_dns_derivation() -> None:
    """CAIRN_NAMESPACE is the frozen uuid5 over the load-bearing namespace string."""
    assert CAIRN_NAMESPACE == uuid.uuid5(uuid.NAMESPACE_DNS, "cairnproofs.dev")


def test_resource_uuid_for_derives_over_prefixed_digest() -> None:
    """Derived identity is uuid5 over the canonical PREFIXED digest (golden vector)."""
    expected = str(uuid.uuid5(CAIRN_NAMESPACE, _GOLDEN_DIGEST))
    assert resource_uuid_for(_GOLDEN_DIGEST) == expected


def test_resource_uuid_for_is_deterministic_across_calls() -> None:
    """Same prefixed digest yields the same identity on every call (pure function)."""
    assert resource_uuid_for(_GOLDEN_DIGEST) == resource_uuid_for(_GOLDEN_DIGEST)


def test_bare_hex_is_rejected() -> None:
    """The bare 64-hex form (no sha256: prefix) is rejected; prefixed form required."""
    with pytest.raises(ValueError, match="prefixed"):
        resource_uuid_for(_GOLDEN_HEX)


def test_non_sha256_prefix_is_rejected() -> None:
    """A non-sha256-prefixed value is rejected (consistent with keys.py)."""
    with pytest.raises(ValueError):
        resource_uuid_for(f"sha512:{_GOLDEN_HEX}")
