"""[INGEST-003.04] bundle-bounds invariant tests.

These tests make the Metadata_Index item-budget invariant *executable*: the
content-layer cap (``MAX_CONTENT_LAYERS``) must be small enough that the
worst-case per-layer projection still fits inside a single DynamoDB item
(< 400 KB). If a future change raises the cap or the per-layer projection grows,
one of these assertions fails — forcing the sizing model in
``docs/adr/0014-metadata-index-item-budget-governs-layer-cap.md`` to be revisited
(or the layers relocated to a dedicated table per the
``relocate-layers-to-dedicated-table`` backlog item).
"""

from __future__ import annotations

import pytest
from cairn_lambda_validate_bundle.handler import MAX_LAYER_TITLE_LEN
from cairn_oci import MAX_ARTIFACT_SIZE_BYTES, MAX_CONTENT_LAYERS, Descriptor, OciManifest
from pydantic import ValidationError

# --- Worst-case per-layer projection cost (bytes) ---------------------------
#
# Each content layer is projected into the single Metadata_Index row. The
# dominant terms, sized at their worst case:
#   - digest:    "sha256:" (7) + 64 hex chars                          = 71 B
#   - mediaType: an OSCAL/OCI media-type string, generously             ~ 64 B
#   - size:      a JSON integer (up to 10 GiB ≈ 11 digits)              ~ 11 B
#   - title:     the layer title annotation, capped at MAX_LAYER_TITLE_LEN
#                (default 1024 B) — the dominant, growth-sensitive term.
#   - structural JSON overhead: braces, quotes, field keys, commas      ~ 3 B
#     beyond the title's own key (the title key/quotes are folded into
#     the constant below).
#
# Tracking MAX_LAYER_TITLE_LEN from its canonical definition means a future
# bump of that cap automatically re-pressurizes this invariant.
_DIGEST_BYTES = 71
_MEDIA_TYPE_BYTES = 64
_SIZE_INT_BYTES = 11
_JSON_STRUCTURAL_BYTES = 3
WORST_CASE_PER_LAYER_BYTES = (
    _DIGEST_BYTES
    + _MEDIA_TYPE_BYTES
    + _SIZE_INT_BYTES
    + _JSON_STRUCTURAL_BYTES
    + MAX_LAYER_TITLE_LEN
)  # ≈ 1173 B at the 1024-byte title cap

# --- Safe item ceiling (bytes) ----------------------------------------------
#
# DynamoDB caps a single item at 400 KB. We reserve a row-remainder headroom for
# everything in the Metadata_Index row that is NOT the per-layer projection:
# pk/sk, the claim projection, the artifact reference, and validation/temporal
# statuses. 48 KB is a deliberately generous reservation.
_DYNAMODB_ITEM_LIMIT = 400 * 1024
_ROW_REMAINDER_HEADROOM = 48 * 1024
SAFE_ITEM_CEILING = _DYNAMODB_ITEM_LIMIT - _ROW_REMAINDER_HEADROOM


def _minimal_descriptor() -> Descriptor:
    return Descriptor(
        mediaType="application/vnd.oci.image.layer.v1.tar",
        digest="sha256:" + "0" * 64,
        size=1,
    )


def test_constant_values_are_pinned() -> None:
    assert MAX_CONTENT_LAYERS == 300
    assert MAX_ARTIFACT_SIZE_BYTES == 10 * 1024**3


def test_worst_case_layer_projection_fits_item_budget() -> None:
    """The binding invariant: cap x worst-case per-layer < safe item ceiling."""
    projected = MAX_CONTENT_LAYERS * WORST_CASE_PER_LAYER_BYTES
    assert projected < SAFE_ITEM_CEILING, (
        f"{MAX_CONTENT_LAYERS} layers x {WORST_CASE_PER_LAYER_BYTES} B "
        f"= {projected} B exceeds the safe item ceiling {SAFE_ITEM_CEILING} B "
        f"(400 KB - {_ROW_REMAINDER_HEADROOM} B headroom). Either lower the cap "
        f"or relocate layers to a dedicated table."
    )


def test_manifest_accepts_max_content_layers() -> None:
    manifest = OciManifest(
        config=_minimal_descriptor(),
        layers=[_minimal_descriptor() for _ in range(MAX_CONTENT_LAYERS)],
    )
    assert len(manifest.layers) == MAX_CONTENT_LAYERS


def test_manifest_rejects_over_max_content_layers() -> None:
    with pytest.raises(ValidationError):
        OciManifest(
            config=_minimal_descriptor(),
            layers=[_minimal_descriptor() for _ in range(MAX_CONTENT_LAYERS + 1)],
        )
