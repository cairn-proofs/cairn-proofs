"""OSCAL layer media-type classification ([OSCAL-002]).

The oscal-interop module owns the OSCAL media-type family: every media
type whose type and subtype begin with `application/oscal.` or
`application/oscal+`. The only accepted member is `application/oscal+json`,
the type that the NIST OSCAL metadata metaschema recommends for OSCAL JSON.
The OSCAL model comes from the document root key, not from the media type.

`is_oscal_layer` compares the type and subtype case-insensitively
(RFC 6838) and ignores media-type parameters. A family member other than
`application/oscal+json` is a permanent validation failure ([OSCAL-005]).
"""

from __future__ import annotations

from typing import Final

from cairn_oscal.errors import OscalExtractionError

OSCAL_JSON_MEDIA_TYPE: Final[str] = "application/oscal+json"
"""The one OSCAL layer media type for every OSCAL JSON model."""

_OSCAL_FAMILY_PREFIXES: Final[tuple[str, ...]] = ("application/oscal.", "application/oscal+")

_MAX_DECLARED_CHARS: Final[int] = 256
"""Longest prefix of a producer-declared media type that an error message shows."""


def _essence(media_type: str) -> str:
    """Return the lowercased type and subtype, without parameters."""
    return media_type.split(";", 1)[0].strip().lower()


def _bounded_declared(media_type: str) -> str:
    """Render a producer-declared value as bounded, escaped text.

    `ascii()` shows control, format, and non-ASCII characters as escapes.
    A value longer than the bound is truncated, and the text states the
    original length.
    """
    shown = ascii(media_type[:_MAX_DECLARED_CHARS])
    if len(media_type) > _MAX_DECLARED_CHARS:
        return f"{shown} (truncated to {_MAX_DECLARED_CHARS} of {len(media_type)} characters)"
    return shown


def is_oscal_layer(media_type: str, *, layer_digest: str) -> bool:
    """Classify one layer by its media type ([OSCAL-002]).

    Args:
        media_type: The layer media type from the image manifest descriptor.
        layer_digest: The layer digest. A rejection error names it.

    Returns:
        `True` for `application/oscal+json`. `False` for a media type
        outside the OSCAL media-type family.

    Raises:
        OscalExtractionError(error_kind="unsupported_media_type"): For a
            member of the OSCAL media-type family other than
            `application/oscal+json`, for example
            `application/oscal.catalog+json` or `application/oscal+xml`.
    """
    essence = _essence(media_type)
    if essence == OSCAL_JSON_MEDIA_TYPE:
        return True
    if not essence.startswith(_OSCAL_FAMILY_PREFIXES):
        return False
    raise OscalExtractionError(
        f"layer {layer_digest} declares OSCAL media type (producer-declared) "
        f"{_bounded_declared(media_type)}; the required OSCAL media type is "
        f"{OSCAL_JSON_MEDIA_TYPE}",
        error_kind="unsupported_media_type",
        layer_digest=layer_digest,
    )
