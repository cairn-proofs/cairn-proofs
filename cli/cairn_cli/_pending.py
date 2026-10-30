"""Shared "tag pending" vocabulary for the read consumers.

`cairn push` admits an artifact by its **Manifest_Digest** synchronously; the
canonical ECR **tag** is minted by the pipeline only after validation +
archive (see artifact-ingestion → "Address is eventually consistent"). For the
window between push and mint, the artifact exists by digest but has no tag yet.

Read paths (`pull` / `verify` / `query` and the read-only TUI) MUST treat a
by-tag miss during that window as a *transient* "tag pending" state, NOT a hard
error or a security rejection — the digest (identity, [INGEST-004]) remains the
resolvable anchor. This module is the single home
for that wording so every consumer reads identically.
"""

from __future__ import annotations

from typing import Any

from cairn_cli._refs import ResolvedReference

# Exit code for the pushed-but-not-yet-tagged window: a by-tag lookup that
# misses while the pipeline has not minted the canonical tag yet.
# Distinct from the hard-error code (4) so callers/scripts can retry rather
# than treat it as a permanent failure. Shared by ``pull`` and ``verify`` so
# scripts read both the same way.
TAG_PENDING_EXIT_CODE = 3

# The canonical operator-facing phrase. `cairn push`'s post-push
# ``TAG_PENDING_MESSAGE`` derives its leading label from this constant, so the
# producer and every consumer speak the same language from one source.
TAG_PENDING_LABEL = "tag pending"

# One-line guidance shown when a by-tag lookup misses during the pending
# window. Points the operator at the always-resolvable digest anchor.
TAG_PENDING_HINT = (
    "tag pending: the artifact was pushed but the pipeline has not minted its "
    "canonical tag yet (the address is eventually consistent). Resolve it by "
    "its Manifest_Digest (`<project-id>/<evidence-category>@sha256:...`) until "
    "the tag appears."
)

# Substrings an OCI registry uses to signal a missing tag/manifest. A by-tag
# miss matching one of these — rather than a transport/credential failure — is
# the eventual-consistency window, not a permanent error.
_TAG_MISS_MARKERS: tuple[str, ...] = (
    "MANIFEST_UNKNOWN",
    "manifest unknown",
    "not found",
    "NAME_UNKNOWN",
    "MANIFEST_UNVERIFIED",
)


def is_tag_miss(stderr: str) -> bool:
    """True if a registry stderr indicates a missing tag/manifest (not a fault).

    Used by ``pull`` / ``verify`` to discriminate the transient pending-tag
    window (a tag that the pipeline has not minted yet) from a genuine
    transport, credential, or signature failure. Matching is case-insensitive
    on the well-known OCI distribution error codes.
    """
    haystack = stderr.lower()
    return any(marker.lower() in haystack for marker in _TAG_MISS_MARKERS)


def is_by_tag(ref: ResolvedReference) -> bool:
    """A reference resolved by ``:tag`` (vs ``@digest``) can hit the pending window."""
    return ref.selector.startswith(":")


def surface_tag_pending(ref: ResolvedReference) -> int:
    """Print the eventual-consistency "tag pending" notice; return the exit code.

    Shared by ``pull`` and ``verify`` so a by-tag miss during the pre-mint
    window surfaces identically and resolves to the same
    :data:`TAG_PENDING_EXIT_CODE`.
    """
    print(f"{TAG_PENDING_LABEL}: {ref.full}")
    print(TAG_PENDING_HINT)
    return TAG_PENDING_EXIT_CODE


def is_tag_pending(record: dict[str, Any]) -> bool:
    """True if a metadata/ECR record is in the pushed-but-not-yet-tagged window.

    The pending window is identified structurally: the artifact is resolvable
    by digest (``manifestDigest`` present) but carries no canonical ECR tag
    (``imageTag`` absent/empty). A validated bundle (one with a Metadata_Index
    row whose status is not ``"unvalidated"``) always has a tag by the time it
    is indexed, so this only ever fires for the transient pre-mint state.
    """
    digest = record.get("manifestDigest")
    if not isinstance(digest, str) or not digest:
        return False
    tag = record.get("imageTag")
    return not (isinstance(tag, str) and tag)


__all__ = [
    "TAG_PENDING_EXIT_CODE",
    "TAG_PENDING_HINT",
    "TAG_PENDING_LABEL",
    "is_by_tag",
    "is_tag_miss",
    "is_tag_pending",
    "surface_tag_pending",
]
