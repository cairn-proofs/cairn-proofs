"""Layer media-type sniffing based on filename extension + OSCAL detection.

Producers MAY override per-file via ``--media-type filename=mediatype``;
absent that, we default by extension:

  *.json + OSCAL root key   → application/oscal+json ([OSCAL-002])
  *.json (other)            → application/json
  *.pdf                     → application/pdf
  *.{png,jpg,jpeg,gif,webp} → image/<type>
  *                         → application/octet-stream

The OSCAL detection looks for one of the 8 GA OSCAL root keys —
`catalog`, `profile`, `mapping-collection`, `component-definition`,
`system-security-plan`, `assessment-plan`, `assessment-results`,
`plan-of-action-and-milestones` — and produces `application/oscal+json` for
every model. The pipeline reads the model from the document root key, so the
media type does not name it. One leading UTF-8 byte-order mark and one leading
`"$schema"` member are skipped before the root-key test. We DO NOT parse the
full document; only the start is inspected (cheap, JSON-safe, won't OOM on
adversarial input because we cap the read at 8 KiB before giving up and
falling back to application/json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

# The CLI keeps its own literals so it does not depend on cairn-oscal. A unit
# test pins both to cairn_oscal.OSCAL_JSON_MEDIA_TYPE and OscalDocType.
OSCAL_JSON_MEDIA_TYPE: Final[str] = "application/oscal+json"

OSCAL_ROOT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "catalog",
        "profile",
        "mapping-collection",
        "component-definition",
        "system-security-plan",
        "assessment-plan",
        "assessment-results",
        "plan-of-action-and-milestones",
    }
)

_SCHEMA_KEY: Final[str] = "$schema"

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

_DECODER: Final[json.JSONDecoder] = json.JSONDecoder()


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


def _skip_schema_member(members: str) -> str:
    """Return `members` after one leading `"$schema": "<string>",` member.

    `members` is the object text after the opening `{`, with leading
    whitespace removed. When it does not start with a complete `$schema`
    string member followed by a comma, it is returned unchanged.
    """
    prefix = f'"{_SCHEMA_KEY}"'
    if not members.startswith(prefix):
        return members
    rest = members[len(prefix) :].lstrip()
    if not rest.startswith(":"):
        return members
    rest = rest[1:].lstrip()
    try:
        value, end = _DECODER.raw_decode(rest)
    except json.JSONDecodeError:
        return members
    if not isinstance(value, str):
        return members
    rest = rest[end:].lstrip()
    if not rest.startswith(","):
        return members
    return rest[1:].lstrip()


def _sniff_oscal_top_level(path: Path) -> str | None:
    """Return `application/oscal+json` if `path` is an OSCAL JSON document.

    Read only the first 8 KiB and look for one of the recognized root keys
    at the start of the top-level object. If that fails, fall back to a full
    parse of the head, which accepts an object whose keys are one OSCAL root
    key and an optional `$schema`. Otherwise return None.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_JSON_SNIFF_BYTES)
    except OSError:
        return None
    # `utf-8-sig` removes one leading byte-order mark.
    decoded = head.decode("utf-8-sig", errors="replace")
    stripped = decoded.lstrip()
    if not stripped.startswith("{"):
        return None
    members = _skip_schema_member(stripped[1:].lstrip())
    for key in OSCAL_ROOT_KEYS:
        if members.startswith(f'"{key}"'):
            return OSCAL_JSON_MEDIA_TYPE
    # Defensive fallback: a document that parses cleanly and whose top-level
    # object holds one OSCAL root key plus an optional `$schema`.
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    root_keys = set(parsed) - {_SCHEMA_KEY}
    if len(root_keys) != 1 or not root_keys <= OSCAL_ROOT_KEYS:
        return None
    return OSCAL_JSON_MEDIA_TYPE
