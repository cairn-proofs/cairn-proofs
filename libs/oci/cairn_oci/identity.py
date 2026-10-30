"""Deterministic derived-identity primitive over the canonical Manifest_Digest.

Derives a stable UUIDv5 for any canonical artifact that carries no identifier of
its own (a Derived_Identity — e.g. the OSCAL back-matter ``Resource`` uuid that
cites an Artifact_Bundle). The derivation is a pure function of the bundle's
**canonical prefixed** ``sha256:<hex>`` Manifest_Digest, so re-deriving it in any
component, in any process, byte-matches the value minted onto the resource
referrer — keeping the Metadata_Index rebuildable from ECR and S3 alone
(metadata-index [META-012] *Deterministic derived identity*).

Home rationale: identity is derived *from* the Manifest_Digest (an OCI concept)
and both consumers (``libs/oscal`` and ``libs/metadata``) already depend on
``cairn-oci``, so this neutral home adds no dependency edge.

FROZEN-NAMESPACE CONSTRAINT: the namespace string
``"cairnproofs.dev"`` is load-bearing and PERMANENTLY FROZEN once any
non-disposable deployment exists. Changing it re-mints every derived resource
UUID — re-basing every Derived_Identity and dangling every existing citation —
so it MUST be treated as a frozen constant.
"""

from __future__ import annotations

import uuid

# Fixed project namespace, itself derived (not a magic literal) so it is
# self-documenting and reproducible. Anchors every digest->identity derivation.
# FROZEN (T-04): changing the namespace string re-mints every derived resource
# UUID; the seed is permanently frozen once any non-disposable deployment exists
# (see the module docstring).
CAIRN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cairnproofs.dev")


def resource_uuid_for(manifest_digest: str) -> str:
    """Deterministic UUIDv5 for a Derived_Identity, derived from the digest.

    Args:
        manifest_digest: The **canonical prefixed** ``sha256:<hex>`` Manifest_Digest.
            The bare 64-hex form is rejected — the system's canonical content
            address is the prefixed form (the form
            ``libs/metadata/keys.py:sort_key_for_digest`` enforces), and the
            derivation must run over that exact form so every independent
            re-derivation byte-matches.

    Returns:
        ``str(uuid5(CAIRN_NAMESPACE, manifest_digest))`` — stable across calls and
        processes so the Metadata_Index remains rebuildable from ECR + S3 alone.

    Raises:
        ValueError: If ``manifest_digest`` is not ``sha256:``-prefixed.
    """
    if not manifest_digest.startswith("sha256:"):
        raise ValueError(
            "manifest_digest must be the canonical prefixed 'sha256:<hex>' form "
            f"(got {manifest_digest!r}); the bare-hex form is not accepted."
        )
    return str(uuid.uuid5(CAIRN_NAMESPACE, manifest_digest))
