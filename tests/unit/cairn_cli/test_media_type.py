"""Tests for cairn_cli._media_type — extension-based sniffing + OSCAL detection."""

from __future__ import annotations

import json
from pathlib import Path

from cairn_cli._media_type import sniff_media_type


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


def test_json_without_oscal_root(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"something": "else"}))
    assert sniff_media_type(p) == "application/json"


def test_json_with_oscal_catalog_root(tmp_path: Path) -> None:
    p = tmp_path / "cat.json"
    p.write_text(json.dumps({"catalog": {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/oscal.catalog+json"


def test_json_with_oscal_assessment_results_root(tmp_path: Path) -> None:
    p = tmp_path / "ar.json"
    p.write_text(json.dumps({"assessment-results": {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/oscal.assessment-results+json"


def test_json_with_oscal_ssp_root(tmp_path: Path) -> None:
    p = tmp_path / "ssp.json"
    p.write_text(json.dumps({"system-security-plan": {"uuid": "x"}}))
    assert sniff_media_type(p) == "application/oscal.ssp+json"


def test_json_truncated_large_oscal_file_still_detected(tmp_path: Path) -> None:
    """Sniffer caps at 8 KiB but still finds the root key near the start."""
    p = tmp_path / "huge.json"
    padding = "x" * 200_000
    p.write_text('{"catalog": {"uuid": "x", "filler": "' + padding + '"}}')
    assert sniff_media_type(p) == "application/oscal.catalog+json"


def test_yaml_extension(tmp_path: Path) -> None:
    p = tmp_path / "policy.yaml"
    p.write_bytes(b"key: value")
    assert sniff_media_type(p) == "application/yaml"
