"""Tests for cairn_oscal.dispatch (content-derived schema version, [OSCAL-001]).

Covers:
- `is_supported_version` — 1.x acceptance & all rejection branches
- `parse_oscal_layer` — happy path (version read from content), the
  per-layer size limit (`oscal_layer_too_large`), one leading byte-order
  mark, `parse_failure` for invalid UTF-8 / malformed JSON / non-OSCAL
  JSON / an undeclared field (strict models) / a key spelled with a Python
  field name instead of its OSCAL name, `unsupported_version`
  for a parseable non-1.x document,
  `missing_schema_version` for a parseable document with no embedded
  version, and `layer_digest` propagation onto the raised exception.

The schema version is derived from the parsed document's own
`metadata.oscal-version`; `parse_oscal_layer` takes no declared version.
"""

from __future__ import annotations

import json

import pytest
from cairn_oscal.dispatch import (
    DEFAULT_MAX_OSCAL_LAYER_BYTES,
    is_supported_version,
    oscal_layer_too_large_error,
    parse_oscal_layer,
)
from cairn_oscal.errors import OscalExtractionError
from oscal_bindings import OscalDoc, OscalParseError

from .conftest import TEST_UUID, minimal_catalog_json

SUPPORTED_VERSIONS = ["1.0", "1.0.0", "1.1", "1.1.2", "1.2.1", "1.99", "1.10.0"]
UNSUPPORTED_VERSIONS = ["2.0.0", "0.9.0", "1", "abc", "1.x", "1.0.0-rc1", " 1.2.1 "]


def _catalog_json_with_version(oscal_version: str) -> bytes:
    """A minimal catalog whose embedded `metadata.oscal-version` is `oscal_version`."""
    return minimal_catalog_json(oscal_version).encode("utf-8")


def _catalog_json_without_version() -> bytes:
    """A catalog whose metadata block omits `oscal-version` entirely.

    The OSCAL 1.x schema makes `metadata.oscal-version` a REQUIRED non-blank
    field, so such bytes do not parse — `OscalDoc.from_json` raises and the
    layer is rejected as a permanent `parse_failure` (the spec's "a layer
    whose bytes are not parseable as an OSCAL document is rejected as a
    permanent parse failure" clause). The distinct `missing_schema_version`
    posture covers a parsed doc whose `oscal_version` is None/blank, which
    today is reachable only by bypassing schema validation — see
    `test_parse_oscal_layer_none_embedded_version_missing_schema_version`.
    """
    return json.dumps(
        {
            "catalog": {
                "uuid": TEST_UUID,
                "metadata": {
                    "title": "No-Version Catalog",
                    "last-modified": "2026-01-01T00:00:00Z",
                    "version": "1.0",
                },
            }
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# is_supported_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_is_supported_version_accepts_1x(version: str) -> None:
    """[OSCAL-001]: OSCAL 1.x versions pass the gate."""
    assert is_supported_version(version) is True


@pytest.mark.parametrize("version", UNSUPPORTED_VERSIONS)
def test_is_supported_version_rejects_non_1x(version: str) -> None:
    """[OSCAL-001]: non-1.x version strings are rejected."""
    assert is_supported_version(version) is False


def test_is_supported_version_rejects_none() -> None:
    """A missing version is rejected (returns False, not raises)."""
    assert is_supported_version(None) is False


def test_is_supported_version_rejects_empty_string() -> None:
    """Empty string is not a valid version."""
    assert is_supported_version("") is False


# ---------------------------------------------------------------------------
# parse_oscal_layer — happy path (version derived from content)
# ---------------------------------------------------------------------------


def test_parse_oscal_layer_happy_returns_doc_and_content_version() -> None:
    """Happy path: the version is read from the document's own content with
    no declared input, and `parse_oscal_layer` returns `(doc, version)`."""
    blob = _catalog_json_with_version("1.1.2")
    doc, version = parse_oscal_layer(blob)
    assert isinstance(doc, OscalDoc)
    assert version == "1.1.2"
    # The returned version is exactly the document-embedded value.
    assert doc.oscal_version == "1.1.2"


def test_parse_oscal_layer_happy_default_fixture_version() -> None:
    """The conftest default fixture (1.2.1) round-trips through the parse."""
    blob = minimal_catalog_json().encode("utf-8")
    doc, version = parse_oscal_layer(blob)
    assert isinstance(doc, OscalDoc)
    assert version == "1.2.1"


# ---------------------------------------------------------------------------
# parse_oscal_layer — missing_schema_version (no readable embedded version)
# ---------------------------------------------------------------------------


def test_parse_oscal_layer_absent_embedded_version_is_parse_failure() -> None:
    """A document omitting the required `metadata.oscal-version` does not parse
    (the OSCAL 1.x schema requires a non-blank value), so it is rejected as a
    permanent `parse_failure` per the spec's not-parseable clause — NOT as
    `missing_schema_version`, which is reserved for a parsed doc whose
    version is None/blank."""
    blob = _catalog_json_without_version()
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob)
    assert exc_info.value.error_kind == "parse_failure"


def test_parse_oscal_layer_none_embedded_version_missing_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parsed document whose `oscal_version` reads None/blank → missing_schema_version.

    The required-field schema makes this unreachable via the JSON parse path
    today, so we exercise the post-parse gate directly: a successful parse
    whose `oscal_version` accessor yields None must map to the distinct
    `missing_schema_version` posture, with `layer_digest`
    propagated for the validation-failed event ([OSCAL-005])."""
    blob = minimal_catalog_json().encode("utf-8")

    real_from_json = OscalDoc.from_json

    def _patched_from_json(data: bytes | str):  # type: ignore[no-untyped-def]
        doc = real_from_json(data)
        monkeypatch.setattr(type(doc), "oscal_version", property(lambda self: None))
        return doc

    monkeypatch.setattr(OscalDoc, "from_json", staticmethod(_patched_from_json))

    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:deadbeef")
    assert exc_info.value.error_kind == "missing_schema_version"
    assert exc_info.value.layer_digest == "sha256:deadbeef"


# ---------------------------------------------------------------------------
# parse_oscal_layer — unsupported_version (parseable but non-1.x content)
# ---------------------------------------------------------------------------


def test_parse_oscal_layer_parseable_2_0_rejects_unsupported_version() -> None:
    """A structurally-parseable 2.0 document is rejected by the 1.x gate on
    its own embedded version as unsupported_version ([OSCAL-001])."""
    blob = _catalog_json_with_version("2.0.0")
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:two-oh")
    assert exc_info.value.error_kind == "unsupported_version"
    assert exc_info.value.document_version == "2.0.0"
    assert exc_info.value.layer_digest == "sha256:two-oh"


# ---------------------------------------------------------------------------
# parse_oscal_layer — parse_failure (invalid UTF-8, malformed JSON, non-OSCAL)
# ---------------------------------------------------------------------------


def test_parse_oscal_layer_invalid_utf8_raises_parse_failure() -> None:
    """Non-UTF-8 bytes → parse_failure."""
    blob = b"\xff\xfe\x00\x00invalid"
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:bad")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:bad"


def test_parse_oscal_layer_malformed_json_raises_parse_failure() -> None:
    """Garbled JSON syntax → parse_failure."""
    blob = b"{ this is not json"
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:malformed")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:malformed"


def test_parse_oscal_layer_json_not_oscal_raises_parse_failure() -> None:
    """Well-formed JSON that doesn't match any OSCAL document shape → parse_failure."""
    blob = b'{"hello": "world"}'
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:notoscal")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:notoscal"


def test_parse_oscal_layer_unknown_field_raises_parse_failure() -> None:
    """A valid catalog plus one undeclared field → parse_failure.

    Guards the strict-model (`extra='forbid'`) contract Cairn relies on from
    the pinned `oscal-bindings` package: a pin bump that relaxed strictness
    would silently widen what the validate path accepts.
    """
    doc = json.loads(minimal_catalog_json())
    doc["catalog"]["metadata"]["not-an-oscal-field"] = "x"
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(json.dumps(doc).encode("utf-8"), layer_digest="sha256:extra")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:extra"


# ---------------------------------------------------------------------------
# parse_oscal_layer — Python-field-name keys are rejected, not dropped (#68)
# ---------------------------------------------------------------------------

_RESOURCE_UUID = "11111111-1111-4111-8111-111111111111"


def _catalog_with(extra: dict[str, object]) -> bytes:
    """A minimal valid catalog with `extra` merged into the `catalog` object."""
    doc = json.loads(minimal_catalog_json())
    doc["catalog"].update(extra)
    return json.dumps(doc).encode("utf-8")


def test_parse_oscal_layer_canonical_back_matter_is_kept() -> None:
    """Control case: the canonical hyphenated `back-matter` key parses and
    its content reaches the parsed document."""
    blob = _catalog_with({"back-matter": {"resources": [{"uuid": _RESOURCE_UUID}]}})
    doc, _ = parse_oscal_layer(blob)
    back_matter = doc.body.back_matter
    assert back_matter is not None
    assert [str(r.uuid) for r in back_matter.resources] == [_RESOURCE_UUID]


@pytest.mark.parametrize(
    ("extra", "offending_key"),
    [
        pytest.param(
            {"back_matter": {"resources": [{"uuid": _RESOURCE_UUID}]}},
            "back_matter",
            id="top-level-optional-field",
        ),
        pytest.param(
            {
                "back-matter": {
                    "resources": [
                        {
                            "uuid": _RESOURCE_UUID,
                            "rlinks": [{"href": "https://example.com/r.json", "media_type": "x/y"}],
                        }
                    ]
                }
            },
            "media_type",
            id="nested-optional-field",
        ),
        pytest.param(
            {
                "back-matter": {"resources": [{"uuid": _RESOURCE_UUID}]},
                "back_matter": {"resources": []},
            },
            "back_matter",
            id="both-spellings-present",
        ),
    ],
)
def test_parse_oscal_layer_python_field_name_key_raises_parse_failure(
    extra: dict[str, object], offending_key: str
) -> None:
    """A key spelled with a model's Python field name instead of its OSCAL
    name is a permanent `parse_failure` ([OSCAL-005]). It is never silently
    dropped from a document that then reports valid."""
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(_catalog_with(extra), layer_digest="sha256:pyname")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:pyname"
    cause = exc_info.value.__cause__
    assert isinstance(cause, OscalParseError)
    assert any(
        e["type"] == "extra_forbidden"
        and e["loc"][0] == "CatalogDocument"
        and e["loc"][-1] == offending_key
        for e in cause.errors
    )


# ---------------------------------------------------------------------------
# parse_oscal_layer — per-layer OSCAL size limit (max_bytes)
# ---------------------------------------------------------------------------

_SMALL_LIMIT = 1024


def test_default_max_oscal_layer_bytes_is_16_mib() -> None:
    assert DEFAULT_MAX_OSCAL_LAYER_BYTES == 16 * 1024 * 1024


def test_parse_oscal_layer_one_byte_over_limit_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[OSCAL-001.09]: a blob one byte over `max_bytes` gives
    `oscal_layer_too_large` before the parse runs. The message names the
    layer digest, that the bytes are longer than the limit, the limit in
    bytes and in MiB, and that the limit applies per OSCAL layer."""

    def _must_not_parse(data: bytes) -> OscalDoc:
        raise AssertionError("parser reached for an oversized blob")

    monkeypatch.setattr(OscalDoc, "from_json", staticmethod(_must_not_parse))
    oversized = b"x" * (DEFAULT_MAX_OSCAL_LAYER_BYTES + 1)
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(oversized, layer_digest="sha256:huge")
    err = exc_info.value
    assert err.error_kind == "oscal_layer_too_large"
    assert err.layer_digest == "sha256:huge"
    msg = str(err)
    assert "sha256:huge" in msg
    assert "bytes longer than" in msg
    assert "16,777,216 bytes (16.0 MiB)" in msg
    assert "per OSCAL layer" in msg


def test_parse_oscal_layer_at_exact_limit_reaches_parser() -> None:
    """[OSCAL-001.10]: a blob exactly `max_bytes` long passes the size check,
    so garbage content gives `parse_failure` from the parser."""
    at_limit = b"x" * DEFAULT_MAX_OSCAL_LAYER_BYTES
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(at_limit, layer_digest="sha256:atlimit")
    assert exc_info.value.error_kind == "parse_failure"
    assert isinstance(exc_info.value.__cause__, OscalParseError)


def test_parse_oscal_layer_non_default_limit_applies() -> None:
    """[OSCAL-001.11]: a non-default `max_bytes` is the limit that applies,
    and the message states it."""
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(
            b"x" * (_SMALL_LIMIT + 1), layer_digest="sha256:s", max_bytes=_SMALL_LIMIT
        )
    assert exc_info.value.error_kind == "oscal_layer_too_large"
    assert "1,024 bytes (0.0 MiB)" in str(exc_info.value)

    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(b"x" * _SMALL_LIMIT, layer_digest="sha256:s", max_bytes=_SMALL_LIMIT)
    assert exc_info.value.error_kind == "parse_failure"


def test_parse_oscal_layer_raised_limit_accepts_blob_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[OSCAL-001.11]: a limit above the default lets a larger blob reach the parser."""
    seen: list[int] = []

    def _fake_from_json(data: bytes) -> OscalDoc:
        seen.append(len(data))
        raise OscalParseError("stub")

    monkeypatch.setattr(OscalDoc, "from_json", staticmethod(_fake_from_json))
    big = b"x" * (DEFAULT_MAX_OSCAL_LAYER_BYTES + 1)
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(big, max_bytes=DEFAULT_MAX_OSCAL_LAYER_BYTES * 2)
    assert exc_info.value.error_kind == "parse_failure"
    assert seen == [len(big)]


def test_oscal_layer_too_large_error_declared_size() -> None:
    """The declared-size form names the declared size in bytes and in MiB."""
    err = oscal_layer_too_large_error(
        "sha256:d", limit=DEFAULT_MAX_OSCAL_LAYER_BYTES, declared_size=20 * 1024 * 1024
    )
    assert err.error_kind == "oscal_layer_too_large"
    assert err.layer_digest == "sha256:d"
    assert str(err) == (
        "OSCAL layer sha256:d declares 20,971,520 bytes (20.0 MiB), above the "
        "per-layer OSCAL limit of 16,777,216 bytes (16.0 MiB) for this deployment; "
        "the limit applies per OSCAL layer"
    )


# ---------------------------------------------------------------------------
# parse_oscal_layer — leading UTF-8 byte-order mark ([OSCAL-001.12])
# ---------------------------------------------------------------------------

_BOM = b"\xef\xbb\xbf"


def test_parse_oscal_layer_one_leading_bom_parses() -> None:
    blob = _BOM + minimal_catalog_json().encode("utf-8")
    doc, version = parse_oscal_layer(blob)
    assert isinstance(doc, OscalDoc)
    assert version == "1.2.1"


def test_parse_oscal_layer_two_leading_boms_is_parse_failure() -> None:
    blob = _BOM + _BOM + minimal_catalog_json().encode("utf-8")
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(blob, layer_digest="sha256:bom2")
    assert exc_info.value.error_kind == "parse_failure"
    assert exc_info.value.layer_digest == "sha256:bom2"


def test_parse_oscal_layer_bom_counts_toward_limit() -> None:
    """The size check runs on the stored bytes, byte-order mark included."""
    body = minimal_catalog_json().encode("utf-8")
    with pytest.raises(OscalExtractionError) as exc_info:
        parse_oscal_layer(_BOM + body, max_bytes=len(body))
    assert exc_info.value.error_kind == "oscal_layer_too_large"
