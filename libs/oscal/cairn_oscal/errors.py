"""Permanent-failure exception type for the OSCAL extraction pipeline.

OSCAL extraction failures are never retried ([OSCAL-005]); the validate-bundle
Lambda translates any `OscalExtractionError` into an `artifact.validation-failed`
event and routes the bundle straight to the DLQ.
"""

from __future__ import annotations

from typing import Literal

OscalErrorKind = Literal[
    "missing_schema_version",
    "unsupported_version",
    "parse_failure",
    "unsupported_doc_type",
]


class OscalExtractionError(Exception):
    """Permanent OSCAL processing failure.

    `error_kind` lets the caller branch on the failure category when
    composing the `artifact.validation-failed` event payload ([OSCAL-005]).
    `layer_digest` is optional because some failures (e.g., an oversized
    or unparseable blob reported before the layer digest is known) can be
    bundle-level rather than layer-level. `document_version` carries the
    content-derived `metadata.oscal-version` on an `unsupported_version`
    failure so the event can name the offending version.
    """

    def __init__(
        self,
        message: str,
        *,
        error_kind: OscalErrorKind,
        layer_digest: str | None = None,
        document_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.layer_digest = layer_digest
        self.document_version = document_version
