"""Tests for cairn_cli.query — supported access paths, unsupported-shape rejection, pagination."""

from __future__ import annotations

import argparse
import json

import pytest
from cairn_cli import main as main_module
from cairn_cli import query as query_module
from cairn_metadata import ControlClaims, MetadataIndex

from tests.unit.cairn_cli.conftest import CONTROL_CLAIMS_TABLE, METADATA_TABLE, subparser_help

CATALOG_UUID = "11111111-1111-4111-8111-111111111111"
TENANT = "acme"


def _make_args(
    *,
    project: str | None = None,
    tenant_id: str | None = TENANT,
    control: str | None = None,
    catalog_or_profile_uuid: str | None = None,
    limit: int = 100,
    page_token: str | None = None,
    **unsupported: object,
) -> argparse.Namespace:
    defaults = {
        "project": project,
        "tenant_id": tenant_id,
        "control": control,
        "catalog_or_profile_uuid": catalog_or_profile_uuid,
        "limit": limit,
        "page_token": page_token,
        "stage": "alpha",
        "region_code": "cmh",
        "app_name": None,
        "date_range": None,
        "date_from": None,
        "date_to": None,
        "assessment_id": None,
        "text": None,
        "digest": None,
    }
    defaults.update(unsupported)
    return argparse.Namespace(**defaults)


def test_query_help_has_usage_examples_and_concrete_example() -> None:
    help_text = subparser_help(main_module.build_parser(), "query")
    assert "usage" in help_text.lower()
    assert "Examples:" in help_text
    assert "cairn query --tenant-id acme --project alpha-bank" in help_text


def _seed_metadata(resource, project_id: str, count: int, *, tenant_id: str = TENANT) -> None:
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=resource)
    for i in range(count):
        idx.create_on_validated(
            manifest_digest=f"sha256:{i:064x}",
            tenant_id=tenant_id,
            project_id=project_id,
            record={"i": i, "manifestDigest": f"sha256:{i:064x}"},
        )


def _seed_controls(resource, *, control_id: str, count: int, tenant_id: str = TENANT) -> None:
    idx = ControlClaims(CONTROL_CLAIMS_TABLE, dynamodb_resource=resource)
    digests = [f"sha256:{i:064x}" for i in range(count)]
    for digest in digests:
        idx.record_controls(
            tenant_id=tenant_id,
            manifest_digest=digest,
            catalog_or_profile_uuid=CATALOG_UUID,
            controls=[control_id],
            denormalized={"projectId": "alpha-bank", "signingStatus": "signed"},
        )


def test_project_query_emits_jsonl(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_metadata(aws_stack["dynamodb"], "alpha-bank", count=3)
    args = _make_args(project="alpha-bank")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line valid JSON


def test_control_query_requires_tenant_id(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    # The control-claims projection is a tenant-PK plane, so a control query must
    # name its tenant — a --control without --tenant-id is rejected before any
    # DynamoDB call.
    args = _make_args(control="ac-2", tenant_id=None, catalog_or_profile_uuid=CATALOG_UUID)
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2
    assert "--tenant-id" in capsys.readouterr().err


def test_control_query_requires_catalog_uuid(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    args = _make_args(control="ac-2")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2
    assert "catalog-or-profile-uuid" in capsys.readouterr().err


def test_control_query_rejects_invalid_uuid(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    args = _make_args(control="ac-2", catalog_or_profile_uuid="not-a-uuid")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2
    assert "UUID" in capsys.readouterr().err


def test_control_query_emits_jsonl(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_controls(aws_stack["dynamodb"], control_id="ac-2", count=2)
    args = _make_args(control="ac-2", catalog_or_profile_uuid=CATALOG_UUID)
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert record["controlId"] == "ac-2"


def test_mutual_exclusion_enforced(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    args = _make_args(project="alpha-bank", control="ac-2", catalog_or_profile_uuid=CATALOG_UUID)
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2
    assert "exactly one" in capsys.readouterr().err


def test_neither_selector_supplied_rejected(aws_stack) -> None:
    args = _make_args()
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2


def test_unsupported_date_range_rejected(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    args = _make_args(project="alpha-bank", date_range="2026-01-01/2026-12-31")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2
    assert "not a supported query shape" in capsys.readouterr().err


def test_unsupported_assessment_id_rejected(aws_stack) -> None:
    args = _make_args(project="alpha-bank", assessment_id="ar-1")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2


def test_unsupported_text_search_rejected(aws_stack) -> None:
    args = _make_args(project="alpha-bank", text="pci-dss")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2


def test_limit_bounds_enforced(aws_stack) -> None:
    args = _make_args(project="alpha-bank", limit=101)
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2


def test_pagination_token_plumbing(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    """A small limit forces a second page; next_token surfaces on stderr."""
    _seed_metadata(aws_stack["dynamodb"], "alpha-bank", count=5)
    args = _make_args(project="alpha-bank", limit=2)
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    captured = capsys.readouterr()
    stdout_lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(stdout_lines) == 2
    # __next_token__ goes to stderr — stdout stays pure JSONL.
    assert "__next_token__" in captured.err
    # Extract the token and use it to fetch page 2.
    token_line = next(ln for ln in captured.err.splitlines() if "__next_token__" in ln)
    token = token_line.split(":", 1)[1].strip()
    args_page2 = _make_args(project="alpha-bank", limit=2, page_token=token)
    rc = query_module._run(args_page2, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    captured2 = capsys.readouterr()
    page2_lines = [ln for ln in captured2.out.splitlines() if ln.strip()]
    assert len(page2_lines) == 2


def test_invalid_page_token_rejected(aws_stack) -> None:
    args = _make_args(project="alpha-bank", page_token="!!!not-base64!!!")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 2


# --- Two-signature trust annotation on the query projection ------------------


def test_annotate_trust_maps_signed_to_fully_trusted() -> None:
    out = query_module._annotate_trust({"manifestDigest": "sha256:x", "signingStatus": "signed"})
    assert out["trust"] == "fully-trusted"
    assert out["trustSource"] == "projection"


def test_annotate_trust_maps_pending_to_partial() -> None:
    # A not-yet-fully-signed projection is never reported as fully trusted.
    out = query_module._annotate_trust({"signingStatus": "pending"})
    assert out["trust"] == "partial"


def test_annotate_trust_maps_unsigned_to_unsigned() -> None:
    out = query_module._annotate_trust({"signingStatus": "unsigned"})
    assert out["trust"] == "unsigned"


def test_annotate_trust_passes_through_record_without_signing_status() -> None:
    # A synthetic entry with no signingStatus is left untouched (no false trust).
    record = {"manifestDigest": "sha256:x"}
    out = query_module._annotate_trust(record)
    assert "trust" not in out
    assert out is record


def test_annotate_trust_does_not_mutate_input() -> None:
    record = {"signingStatus": "signed"}
    snapshot = dict(record)
    query_module._annotate_trust(record)
    assert record == snapshot


# --- Render-safety: producer-controlled free-text reaches `cairn query` --------


def test_project_query_emits_no_raw_control_bytes(
    aws_stack, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crafted `layers[].title`/`mediaType` must not reach stdout as raw bytes.

    The Metadata_Index projection includes the producer-controlled,
    free-text ``org.opencontainers.image.title`` annotation, which reaches `cairn
    query` output. The output path emits each record via
    ``json.dump(..., ensure_ascii=True)`` (the default), which JSON-escapes
    every control byte to a ``\\uXXXX`` literal, so no raw C0/DEL/C1 byte is
    ever written to the auditor's terminal. The escaping is lossless, so the
    projection round-trips faithfully — the bytes are merely inert on the wire.
    """
    # Mix CSI color, OSC clipboard (OSC 52) + window title, and stray
    # C0/C1 control bytes — the full terminal-manipulation payload class.
    malicious_title = (
        "\x1b[31mPWNED\x1b[0m"  # CSI / SGR color
        "\x1b]52;c;cHduZWQ=\x07"  # OSC 52 clipboard write, BEL-terminated
        "\x1b]0;pwned\x07"  # OSC 0 window-title rewrite
        "\x07\x00\x9b"  # stray BEL, NUL, C1 CSI introducer
    )
    malicious_media_type = "application/vnd.evil\x1b[2J\x9d52;c;x\x9c"
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=aws_stack["dynamodb"])
    digest = "sha256:" + "a" * 64
    idx.create_on_validated(
        manifest_digest=digest,
        tenant_id=TENANT,
        project_id="alpha-bank",
        record={
            "manifestDigest": digest,
            "layers": [{"title": malicious_title, "mediaType": malicious_media_type}],
        },
    )
    args = _make_args(project="alpha-bank")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1
    line = lines[0]

    # No raw control byte (C0, DEL, or C1) appears anywhere in the raw output.
    control_bytes = [c for c in line if c <= "\x1f" or c == "\x7f" or ("\x80" <= c <= "\x9f")]
    assert control_bytes == [], f"raw control byte(s) leaked to stdout: {control_bytes!r}"

    # Escaping is lossless — the record round-trips and the title/mediaType
    # equal the original malicious strings (the projection stays faithful).
    record = json.loads(line)
    assert record["layers"][0]["title"] == malicious_title
    assert record["layers"][0]["mediaType"] == malicious_media_type


def test_project_query_emits_trust_field(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    # End-to-end: the JSONL records carry the derived trust verdict + source.
    idx = MetadataIndex(METADATA_TABLE, dynamodb_resource=aws_stack["dynamodb"])
    idx.create_on_validated(
        manifest_digest="sha256:" + "1" * 64,
        tenant_id=TENANT,
        project_id="alpha-bank",
        record={"manifestDigest": "sha256:" + "1" * 64, "signingStatus": "signed"},
    )
    args = _make_args(project="alpha-bank")
    rc = query_module._run(args, dynamodb_resource=aws_stack["dynamodb"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["trust"] == "fully-trusted"
    assert record["trustSource"] == "projection"
