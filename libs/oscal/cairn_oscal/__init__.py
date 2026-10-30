"""OSCAL extraction adapter for the Cairn Provenance_System.

Implements the `oscal-interop` capability spec surfaces consumed by
the validate-bundle Lambda: content-derived schema-version dispatch
([OSCAL-001]), per-layer metadata extraction ([OSCAL-003]), OSCAL artifact
reference generation ([OSCAL-007.01]), and a single
permanent-failure exception type for routing to
`artifact.validation-failed` ([OSCAL-005]).

The package is a thin layer over `oscal_bindings` (the `oscal-bindings`
PyPI distribution). Schema parsing, model construction, and back-matter
element factories live upstream; this package owns the Cairn-specific
composition: how the parsed document drives version gating, what the
Metadata_Index expects, and how the artifact reference's rlinks are
shaped from ECR + S3 URIs.
"""

from cairn_oscal.dispatch import (
    MAX_OSCAL_LAYER_BYTES,
    is_supported_version,
    parse_oscal_layer,
)
from cairn_oscal.errors import OscalErrorKind, OscalExtractionError
from cairn_oscal.extraction import (
    OscalDocType,
    OscalLayerMetadata,
    extract_oscal_metadata,
)
from cairn_oscal.reference import make_artifact_reference

__all__ = [
    "MAX_OSCAL_LAYER_BYTES",
    "OscalDocType",
    "OscalErrorKind",
    "OscalExtractionError",
    "OscalLayerMetadata",
    "extract_oscal_metadata",
    "is_supported_version",
    "make_artifact_reference",
    "parse_oscal_layer",
]
