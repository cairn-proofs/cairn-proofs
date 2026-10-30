"""Tests for cairn_cli.pull — reference resolution, verify-before-write, digest verify."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cairn_cli import main as main_module
from cairn_cli import pull as pull_module
from cairn_oci import compute_sha256_digest

from tests.unit.cairn_cli.conftest import subparser_help


def _make_args(reference: str, output_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        reference=reference,
        output_dir=output_dir,
        stage="alpha",
        region_code="cmh",
        app_name=None,
    )


class _FakeOras:
    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(
        self,
        *,
        layers: list[tuple[str, bytes]],
        fetch_should_fail: str | None = None,
        pull_should_fail: str | None = None,
        login_should_fail: str | None = None,
        skip_writing_layer: str | None = None,
        corrupt_layer: str | None = None,
    ) -> None:
        self._layers = layers
        self._fetch_should_fail = fetch_should_fail
        self._pull_should_fail = pull_should_fail
        self._login_should_fail = login_should_fail
        self._skip = skip_writing_layer
        self._corrupt = corrupt_layer
        self.login_calls: list[dict[str, Any]] = []
        self.pull_calls: list[dict[str, Any]] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        if self._login_should_fail is not None:
            raise self.OrasCommandError(returncode=1, stderr=self._login_should_fail, command=[])
        self.login_calls.append({"registry": registry, "username": username})

    def fetch_manifest(self, *, reference: str) -> bytes:
        if self._fetch_should_fail is not None:
            raise self.OrasCommandError(returncode=1, stderr=self._fetch_should_fail, command=[])
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [
                {
                    "mediaType": "application/octet-stream",
                    "digest": compute_sha256_digest(body),
                    "size": len(body),
                    "annotations": {"org.opencontainers.image.title": title},
                }
                for title, body in self._layers
            ],
        }
        return json.dumps(manifest).encode("utf-8")

    def pull(self, *, reference: str, output_dir: Path) -> None:
        if self._pull_should_fail is not None:
            raise self.OrasCommandError(returncode=1, stderr=self._pull_should_fail, command=[])
        self.pull_calls.append({"reference": reference, "output_dir": output_dir})
        output_dir.mkdir(parents=True, exist_ok=True)
        for title, body in self._layers:
            if self._skip == title:
                continue
            payload = body if self._corrupt != title else body + b"X"
            (output_dir / title).write_bytes(payload)


def test_pull_help_has_usage_examples_and_concrete_example() -> None:
    help_text = subparser_help(main_module.build_parser(), "pull")
    assert "usage" in help_text.lower()
    assert "Examples:" in help_text
    assert "cairn pull alpha-bank/assessment-results:2026-q1 --output-dir ./out" in help_text


def _verify_ok(_reference: str) -> None:
    return None


def _verify_fail(_reference: str) -> None:
    from cairn_signing import NotationCommandError

    raise NotationCommandError(returncode=1, stderr="trust policy mismatch", command=[])


def test_pulls_and_verifies_layers(aws_stack, tmp_path: Path) -> None:
    fake_oras = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 0
    assert (tmp_path / "report.pdf").read_bytes() == b"hello-pdf"


def test_signature_failure_short_circuits_before_pull(aws_stack, tmp_path: Path) -> None:
    """Critical security invariant: signature verify failure MUST NOT write files."""
    fake_oras = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_fail,
    )
    assert rc == 4
    # Fake oras records calls on pull(); ensure NOTHING was pulled.
    assert fake_oras.pull_calls == []
    assert not (tmp_path / "report.pdf").exists()


def test_filename_collision_refused(aws_stack, tmp_path: Path) -> None:
    """If the output-dir already has a file with the layer's title, error out."""
    (tmp_path / "report.pdf").write_bytes(b"pre-existing")
    fake_oras = _FakeOras(layers=[("report.pdf", b"new")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4
    # Original file MUST NOT be overwritten.
    assert (tmp_path / "report.pdf").read_bytes() == b"pre-existing"


def test_duplicate_layer_title_in_manifest_rejected(aws_stack, tmp_path: Path) -> None:
    fake_oras = _FakeOras(layers=[("dup.txt", b"a"), ("dup.txt", b"b")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4


def test_corrupt_layer_after_pull_reports_clean_error(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per-layer SHA-256 mismatch surfaces as a clean error + non-zero exit ([INGEST-007.03])."""
    fake_oras = _FakeOras(layers=[("report.pdf", b"hello-pdf")], corrupt_layer="report.pdf")
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4
    assert "integrity check failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "hostile_title",
    [
        "../escape.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "subdir/inside.txt",
        "..\\windows.txt",
        "..",
        ".",
        "name\x00.txt",
    ],
)
def test_path_traversal_layer_title_rejected(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile_title: str
) -> None:
    """A malicious layer title must NOT cause writes outside the output directory.

    The threat model treats "signature verify passed" as the trust gate for
    filesystem writes — but a producer with a valid signing key could still
    embed `../` in a layer title. The CLI MUST reject before any pull happens.
    """
    fake_oras = _FakeOras(layers=[(hostile_title, b"hostile-bytes")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4
    assert "bare filename" in capsys.readouterr().err
    # Nothing was pulled — the rejection happens before oras pull is called.
    assert fake_oras.pull_calls == []


def test_layer_missing_title_annotation_rejected(aws_stack, tmp_path: Path) -> None:
    class _NoTitleOras(_FakeOras):
        def fetch_manifest(self, *, reference: str) -> bytes:
            return json.dumps({"layers": [{"digest": "sha256:" + "0" * 64}]}).encode()

    fake_oras = _NoTitleOras(layers=[])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4


def test_invalid_reference_rejected(aws_stack, tmp_path: Path) -> None:
    fake_oras = _FakeOras(layers=[])
    args = _make_args("totally-bogus", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 2
    # The reference is rejected before any registry round-trip, so no login.
    assert fake_oras.login_calls == []


def test_pull_logs_in_to_ecr_before_verifying(aws_stack, tmp_path: Path) -> None:
    """pull authenticates the credential store itself — no ambient docker login."""
    fake_oras = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 0
    assert len(fake_oras.login_calls) == 1
    assert fake_oras.login_calls[0]["registry"].endswith(".amazonaws.com")
    assert fake_oras.login_calls[0]["username"] == "AWS"


def test_pull_login_failure_short_circuits(aws_stack, tmp_path: Path) -> None:
    """An ECR login failure surfaces as exit 4 and writes nothing."""
    fake_oras = _FakeOras(
        layers=[("report.pdf", b"hello-pdf")],
        login_should_fail="denied: not authorized",
    )
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4
    assert fake_oras.pull_calls == []
    assert not (tmp_path / "report.pdf").exists()


def test_pull_malformed_ecr_token_surfaces_clean_error(
    aws_stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable ECR token (EcrAuthError) is surfaced as exit 4, not a traceback.

    This is the failure mode the auto-login change exists to eliminate — an
    opaque registry-auth error — so it must reach `fail()`, not escape.
    """
    fake_oras = _FakeOras(layers=[("report.pdf", b"hello-pdf")])
    bad_ecr = MagicMock()
    bad_ecr.get_authorization_token.return_value = {"authorizationData": []}
    args = _make_args("alpha-bank/assessment-results:2026-q1--ac-2--1", tmp_path)
    rc = pull_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=bad_ecr,
        oras_module=fake_oras,
        verify_fn=_verify_ok,
    )
    assert rc == 4
    assert "ECR login failed" in capsys.readouterr().err
    assert fake_oras.login_calls == []
    assert fake_oras.pull_calls == []
