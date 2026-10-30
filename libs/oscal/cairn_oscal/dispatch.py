"""Content-derived schema version → SDK dispatch ([OSCAL-001]).

OSCAL is backward-compatible within the 1.x major version, so a single
binding set (`oscal-bindings`, currently tracking 1.2.x) covers
every 1.x producer. The dispatcher parses any OSCAL layer with the single
shipped binding via `OscalDoc.from_json` (the union RootModel
self-discriminates the root model with no version hint), then derives the
schema version from the parsed document's own `metadata.oscal-version`
and gates it to a supported 1.x string. The version is NOT
producer-declared — the Config_Blob `oscalSchemaVersion` field is not
read. When OSCAL 2.0 ships, the same content read is the seam that
grows a version registry; see the "Major-version namespace" decision in
oscal-bindings' DEVELOPING.md (bindings live in `oscal_bindings.v1`; 2.0
gets its own version subpackage, `oscal_bindings.v2`).

`parse_oscal_layer` enforces a hard size cap on producer-supplied bytes
before invoking the JSON parser. Evidence-ingestion permits per-layer
artifacts up to 10 GB, but OSCAL documents in practice are KB to low-MB;
multi-GB OSCAL is pathological and an unbounded parse risks OOM-killing
the validate-bundle Lambda. The cap is a defense-in-depth guard at
this layer in addition to any cap the calling Lambda may enforce.
"""

from __future__ import annotations

import re
from typing import Final

from oscal_bindings import OscalDoc, OscalParseError

from cairn_oscal.errors import OscalExtractionError

_OSCAL_1X_RE = re.compile(r"^1\.\d+(\.\d+)?$")

MAX_OSCAL_LAYER_BYTES: Final[int] = 64 * 1024 * 1024
"""Per-layer OSCAL parse cap (64 MiB).

OSCAL documents are typically KB to low-MB. The artifact-ingestion limit
of 10 GB per artifact applies to arbitrary content layers (PDFs, scan
reports, etc.), not OSCAL JSON. Beyond ~64 MiB an OSCAL layer is
adversarial: the parse-time memory cost would OOM the Lambda before
any signal of a hostile producer escapes."""


def is_supported_version(version: str | None) -> bool:
    """Return True iff `version` is an OSCAL 1.x version string."""
    if version is None:
        return False
    return bool(_OSCAL_1X_RE.match(version))


def parse_oscal_layer(
    blob: bytes,
    *,
    layer_digest: str | None = None,
) -> tuple[OscalDoc, str]:
    """Parse one OSCAL-typed layer and derive its schema version from content.

    The single shipped 1.x binding parses any OSCAL document via the
    `OscalDoc.from_json` union RootModel (model-agnostic, self-discriminating
    by root key). The schema version is then read from the parsed document's
    own `metadata.oscal-version` and gated to a supported 1.x string — it is
    NOT producer-declared.

    Args:
        blob: The raw layer bytes (OSCAL JSON document).
        layer_digest: Optional layer Manifest_Digest used for error reporting
            so the resulting `artifact.validation-failed` event can identify
            which layer failed ([OSCAL-005]).

    Returns:
        A 2-tuple `(doc, version)` of the parsed `OscalDoc` and its validated
        content-derived OSCAL 1.x schema version string.

    Raises:
        OscalExtractionError: On an oversized blob or any SDK parse failure
            (`parse_failure`), a document with no readable embedded version
            (`missing_schema_version`), or an embedded version that is not a
            supported 1.x string (`unsupported_version`). All variants are
            permanent ([OSCAL-005]).
    """
    if len(blob) > MAX_OSCAL_LAYER_BYTES:
        raise OscalExtractionError(
            f"OSCAL layer exceeds the {MAX_OSCAL_LAYER_BYTES}-byte parse cap "
            f"(received {len(blob)} bytes)",
            error_kind="parse_failure",
            layer_digest=layer_digest,
        )

    try:
        # bytes-direct path: pydantic v2 model_validate_json accepts bytes,
        # halving peak memory vs. an intermediate .decode("utf-8"). Invalid
        # UTF-8 is folded into OscalParseError by the upstream parser.
        doc = OscalDoc.from_json(blob)
    except OscalParseError as e:
        raise OscalExtractionError(
            f"OSCAL SDK parse failed: {e}",
            error_kind="parse_failure",
            layer_digest=layer_digest,
        ) from e

    version = doc.oscal_version
    if version is None or not version.strip():
        raise OscalExtractionError(
            "OSCAL layer carries no readable embedded metadata.oscal-version",
            error_kind="missing_schema_version",
            layer_digest=layer_digest,
        )

    if not is_supported_version(version):
        raise OscalExtractionError(
            f"Unsupported OSCAL version {version!r}; supported: 1.x",
            error_kind="unsupported_version",
            layer_digest=layer_digest,
            document_version=version,
        )

    return (doc, version)
