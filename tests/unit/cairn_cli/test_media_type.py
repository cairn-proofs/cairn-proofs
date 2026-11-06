"""Tests for cairn_cli._media_type — extension-based sniffing + OSCAL detection."""

from __future__ import annotations

import json
import typing
from pathlib import Path

import cairn_oscal
import pytest
from cairn_cli._media_type import OSCAL_JSON_MEDIA_TYPE, OSCAL_ROOT_KEYS, sniff_media_type

_BOM = b"\xef\xbb\xbf"
_SCHEMA_URL = (
    "https://raw.githubusercontent.com/usnistgov/OSCAL/main/json/schema/oscal_complete_schema.json"
)


def test_pdf_extension(tmp_path: Path) -> None:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.7")
    assert sniff_media_type(p) == "application/pdf"


def test_image_extensions(tmp_path: Path) -> None:
    for ext, expected in {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.items():
        p = tmp_path / f"img{ext}"
        p.write_bytes(b"")
        assert sniff_media_type(p) == expected


def test_unknown_extension(tmp_path: Path) -> None:
    p = tmp_path / "file.bin"
    p.write_bytes(b"")
    assert sniff_media_type(p) == "application/octet-stream"


def test_yaml_extension(tmp_path: Path) -> None:
    p = tmp_path / "policy.yaml"
    p.write_bytes(b"key: value")
    assert sniff_media_type(p) == "application/yaml"


# ---------------------------------------------------------------------------
# OSCAL detection ([OSCAL-002.06], [OSCAL-002.07])
# ---------------------------------------------------------------------------


def test_cli_literal_matches_library_constant() -> None:
    assert OSCAL_JSON_MEDIA_TYPE == cairn_oscal.OSCAL_JSON_MEDIA_TYPE


def test_root_keys_match_library_doc_types() -> None:
    assert set(typing.get_args(cairn_oscal.OscalDocType)) == OSCAL_ROOT_KEYS
    assert len(OSCAL_ROOT_KEYS) == 8


@pytest.mark.parametrize("root_key", sorted(OSCAL_ROOT_KEYS))
def test_every_ga_root_key_gives_oscal_json(tmp_path: Path, root_key: str) -> None:
    p = tmp_path / "doc.json"
    p.write_text(json.dumps({root_key: {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/oscal+json"


def test_byte_order_mark_is_skipped(tmp_path: Path) -> None:
    p = tmp_path / "bom.json"
    p.write_bytes(_BOM + json.dumps({"catalog": {"uuid": "x"}}).encode())
    assert sniff_media_type(p) == "application/oscal+json"


def test_leading_schema_member_is_skipped(tmp_path: Path) -> None:
    p = tmp_path / "schema.json"
    p.write_text(json.dumps({"$schema": _SCHEMA_URL, "system-security-plan": {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/oscal+json"


def test_bom_and_schema_and_large_file(tmp_path: Path) -> None:
    """A file larger than the 8 KiB head, with a BOM and a leading `$schema`."""
    p = tmp_path / "huge.json"
    body = (
        '{\n  "$schema" : "'
        + _SCHEMA_URL
        + '",\n  "catalog": {"uuid": "x", "filler": "'
        + "x" * 200_000
        + '"}}'
    )
    p.write_bytes(_BOM + body.encode())
    assert p.stat().st_size > 8 * 1024
    assert sniff_media_type(p) == "application/oscal+json"


def test_large_oscal_file_without_schema_detected(tmp_path: Path) -> None:
    """Sniffer caps at 8 KiB but still finds the root key near the start."""
    p = tmp_path / "huge.json"
    p.write_text('{"catalog": {"uuid": "x", "filler": "' + "x" * 200_000 + '"}}')
    assert sniff_media_type(p) == "application/oscal+json"


def test_parse_fallback_accepts_schema_after_root_key(tmp_path: Path) -> None:
    """The parse fallback accepts one root key plus `$schema` in any order."""
    p = tmp_path / "late-schema.json"
    p.write_text(json.dumps({"profile": {"uuid": "x"}, "$schema": _SCHEMA_URL}))
    assert sniff_media_type(p) == "application/oscal+json"


@pytest.mark.parametrize(
    "doc",
    [
        {"something": "else"},
        {"$schema": _SCHEMA_URL, "something": "else"},
        {"$schema": _SCHEMA_URL},
        {"poam": {"uuid": "x"}},
        ["catalog"],
    ],
)
def test_non_oscal_json_gives_application_json(tmp_path: Path, doc: object) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(doc))
    assert sniff_media_type(p) == "application/json"


def test_non_string_schema_value_is_not_skipped(tmp_path: Path) -> None:
    """A `$schema` whose value is not a string is not skipped; the parse
    fallback still finds the single root key."""
    p = tmp_path / "odd.json"
    p.write_text('{"$schema": 7, "catalog": {"uuid": "x"}}')
    assert sniff_media_type(p) == "application/oscal+json"


def test_truncated_non_oscal_json_gives_application_json(tmp_path: Path) -> None:
    p = tmp_path / "big.json"
    p.write_text('{"$schema": "' + _SCHEMA_URL + '", "data": "' + "y" * 20_000 + '"}')
    assert sniff_media_type(p) == "application/json"


# ---------------------------------------------------------------------------
# `$schema` skip edge cases and malformed heads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # `$schema` with no colon: not a member; the parse fallback fails too.
        '{"$schema" "x", "catalog": {"uuid": "x"}}',
        # `$schema` with an unterminated string value.
        '{"$schema": "https://example, "catalog": {"uuid": "x"}}',
        # `$schema` followed by a second `$schema`: the second is not skipped,
        # and the parse fallback sees no OSCAL root key.
        '{"$schema": "a", "$schema": "b", "data": 1}',
    ],
)
def test_malformed_schema_member_is_not_oscal(tmp_path: Path, text: str) -> None:
    p = tmp_path / "bad.json"
    p.write_text(text)
    assert sniff_media_type(p) == "application/json"


def test_schema_member_with_whitespace_variants(tmp_path: Path) -> None:
    p = tmp_path / "ws.json"
    p.write_text('\n\t{\n\t"$schema"\t:\n"' + _SCHEMA_URL + '"\n,\n\t"catalog": {"uuid": "x"}}')
    assert sniff_media_type(p) == "application/oscal+json"


def test_two_byte_order_marks_are_not_oscal(tmp_path: Path) -> None:
    """Only one byte-order mark is skipped, matching `parse_oscal_layer`,
    which rejects a second one."""
    p = tmp_path / "bom2.json"
    p.write_bytes(_BOM + _BOM + json.dumps({"catalog": {"uuid": "x"}}).encode())
    assert sniff_media_type(p) == "application/json"


def test_root_key_prefix_is_not_a_root_key(tmp_path: Path) -> None:
    """`"catalogue"` is not `"catalog"`: the closing quote is part of the match."""
    p = tmp_path / "near.json"
    p.write_text(json.dumps({"catalogue": {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/json"


def test_invalid_utf8_head_does_not_raise(tmp_path: Path) -> None:
    p = tmp_path / "bin.json"
    p.write_bytes(b"\xff\xfe{\x00\x80" * 100)
    assert sniff_media_type(p) == "application/json"
