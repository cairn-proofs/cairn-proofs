"""Content-derived schema version → SDK dispatch ([OSCAL-001]).

OSCAL is backward-compatible within the 1.x major version, so a single
binding set (`oscal-bindings`, currently tracking 1.2.x) covers
every 1.x producer. The dispatcher parses any OSCAL layer with the single
shipped binding via `OscalDoc.from_json` (the union RootModel
self-discriminates the root model with no version hint), then derives the
schema version from the parsed document's own `metadata.oscal-version`
and gates it to a supported 1.x string. When OSCAL 2.0 ships, the same
content read is the seam that grows a version registry; see the
"Major-version namespace" decision in oscal-bindings' DEVELOPING.md
(bindings live in `oscal_bindings.v1`; 2.0 gets its own version
subpackage, `oscal_bindings.v2`).

`parse_oscal_layer` enforces the per-layer OSCAL size limit (`max_bytes`)
on producer-supplied bytes before it invokes the JSON parser. Evidence
ingestion permits artifacts up to 10 GiB, but parse memory grows with the
document size, and an unbounded parse can stop the validate-bundle Lambda
with an out-of-memory error. The calling Lambda checks the same limit
before and during the fetch; this check is defense in depth.
"""

from __future__ import annotations

import re
from typing import Final

from oscal_bindings import OscalDoc, OscalParseError

from cairn_oscal.errors import OscalExtractionError

_OSCAL_1X_RE = re.compile(r"^1\.\d+(\.\d+)?$")

DEFAULT_MAX_OSCAL_LAYER_BYTES: Final[int] = 16 * 1024 * 1024
"""Default per-layer OSCAL size limit (16 MiB).

This value is the default for a deployment setting. A deployment sets its
own limit with `cdk --context maxOscalLayerBytes=N`, which reaches the
validate-bundle Lambda as the `MAX_OSCAL_LAYER_BYTES` environment
variable. `parse_oscal_layer` takes the limit as its `max_bytes`
argument and reads no environment variable.

Parse memory measured with `parse_oscal_layer` on macOS: the NIST SP
800-53 rev5 catalog (10.0 MiB, the largest known OSCAL document) peaked
at 171 MiB RSS. A synthetic catalog of minimal controls peaked at 479 MiB
at 8 MiB, 872 MiB at 16 MiB, and 3,030 MiB at 60 MiB. The validate Lambda
has 1,769 MB, sized in the CDK stack for this peak plus the runtime and the
ADOT collector. The 16 MiB default gives 60% headroom over the largest known
document and keeps a dense document of that size below the Lambda memory.
A deployment that raises the limit must also give the Lambda more memory."""

_MIB: Final[int] = 1024 * 1024
_UTF8_BOM: Final[bytes] = b"\xef\xbb\xbf"


def oscal_layer_too_large_error(
    layer_digest: str | None, *, limit: int, declared_size: int | None
) -> OscalExtractionError:
    """Build the permanent `oscal_layer_too_large` error ([OSCAL-001]).

    The message names the layer digest, the declared size (or, when
    `declared_size` is `None`, that the bytes are longer than the limit),
    the limit in bytes and in MiB, and that the limit applies per OSCAL layer.
    """
    if declared_size is None:
        size_text = "has bytes longer than"
    else:
        size_text = f"declares {declared_size:,} bytes ({declared_size / _MIB:.1f} MiB), above"
    return OscalExtractionError(
        f"OSCAL layer {layer_digest} {size_text} the per-layer OSCAL limit of "
        f"{limit:,} bytes ({limit / _MIB:.1f} MiB) for this deployment; "
        "the limit applies per OSCAL layer",
        error_kind="oscal_layer_too_large",
        layer_digest=layer_digest,
    )


def is_supported_version(version: str | None) -> bool:
    """Return True iff `version` is an OSCAL 1.x version string."""
    if version is None:
        return False
    return bool(_OSCAL_1X_RE.match(version))


def parse_oscal_layer(
    blob: bytes,
    *,
    layer_digest: str | None = None,
    max_bytes: int = DEFAULT_MAX_OSCAL_LAYER_BYTES,
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
        max_bytes: The per-layer OSCAL size limit that applies. The caller
            passes the deployment limit.

    Returns:
        A 2-tuple `(doc, version)` of the parsed `OscalDoc` and its validated
        content-derived OSCAL 1.x schema version string.

    Raises:
        OscalExtractionError: On a blob longer than `max_bytes`
            (`oscal_layer_too_large`), any SDK parse failure
            (`parse_failure`), a document with no readable embedded version
            (`missing_schema_version`), or an embedded version that is not a
            supported 1.x string (`unsupported_version`). All variants are
            permanent ([OSCAL-005]).
    """
    if len(blob) > max_bytes:
        raise oscal_layer_too_large_error(layer_digest, limit=max_bytes, declared_size=None)

    # RFC 8259 section 8.1 permits a parser to ignore one leading byte-order
    # mark. Only the parse input changes; the stored bytes keep it.
    if blob.startswith(_UTF8_BOM):
        blob = blob[len(_UTF8_BOM) :]

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
