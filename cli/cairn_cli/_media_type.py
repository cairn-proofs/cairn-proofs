"""Layer media-type sniffing based on filename extension + OSCAL detection.

Producers MAY override per-file via ``--media-type filename=mediatype``;
absent that, we default by extension:

  *.json + OSCAL root key   → application/oscal.<doctype>+json ([INGEST-011])
  *.json (other)            → application/json
  *.pdf                     → application/pdf
  *.{png,jpg,jpeg,gif,webp} → image/<type>
  *                         → application/octet-stream

The OSCAL detection looks for the top-level key — `catalog`, `profile`,
`system-security-plan`, `assessment-plan`, `assessment-results`, `poam`,
`component-definition` — and produces the matching `application/oscal.*`
media type. We DO NOT parse the full document; only the top-level key is
inspected (cheap, JSON-safe, won't OOM on adversarial input because we cap
the read at 8 KiB before giving up and falling back to application/json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

OSCAL_ROOT_TO_MEDIA: Final[dict[str, str]] = {
    "catalog": "application/oscal.catalog+json",
    "profile": "application/oscal.profile+json",
    "system-security-plan": "application/oscal.ssp+json",
    "assessment-plan": "application/oscal.assessment-plan+json",
    "assessment-results": "application/oscal.assessment-results+json",
    "plan-of-action-and-milestones": "application/oscal.poam+json",
    "component-definition": "application/oscal.component-definition+json",
}

_IMAGE_EXTS: Final[dict[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Cap how much we peek at when sniffing JSON for OSCAL — defends against
# producer-supplied OOM via huge JSON files (we only need the root key).
_JSON_SNIFF_BYTES: Final[int] = 8 * 1024


def sniff_media_type(path: Path) -> str:
    """Best-effort layer media-type detection."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        oscal = _sniff_oscal_top_level(path)
        if oscal is not None:
            return oscal
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in _IMAGE_EXTS:
        return _IMAGE_EXTS[suffix]
    if suffix in {".yaml", ".yml"}:
        return "application/yaml"
    if suffix in {".txt", ".md"}:
        return "text/plain"
    return "application/octet-stream"


def _sniff_oscal_top_level(path: Path) -> str | None:
    """Return the OSCAL media type if `path` is an OSCAL JSON document.

    Read only the first 8 KiB and look for one of the recognized root keys
    via a fast json.loads on a bounded substring. If the file isn't valid
    JSON OR its root isn't a known OSCAL document type, returns None.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_JSON_SNIFF_BYTES)
    except OSError:
        return None
    # Try to parse the head as JSON; if truncated, json will likely raise
    # and we fall back to application/json. To detect the root key cheaply
    # without a full parse, we instead look for the literal `"<key>"` near
    # the start; if found, return the matching media type.
    decoded = head.decode("utf-8", errors="replace")
    # Skip leading whitespace AND the opening `{`.
    stripped = decoded.lstrip()
    if not stripped.startswith("{"):
        return None
    # Look at the first 256 chars for a recognized OSCAL root key — far
    # cheaper than a full json.loads and avoids choking on truncated input.
    needle_zone = stripped[1:256].lstrip()
    for key, media in OSCAL_ROOT_TO_MEDIA.items():
        if needle_zone.startswith(f'"{key}"'):
            return media
    # Defensive fallback: if the document parses cleanly and the top-level
    # is an object with exactly one OSCAL root key, accept it.
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or len(parsed) != 1:
        return None
    root_key = next(iter(parsed))
    return OSCAL_ROOT_TO_MEDIA.get(root_key)
