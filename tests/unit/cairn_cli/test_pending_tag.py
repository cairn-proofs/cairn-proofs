"""Tests for the pushed-but-not-yet-tagged ("tag pending") read tolerance.

`pull` / `verify` / `query` fall back to digest resolution and present a
consistent "tag pending" state during the eventual-consistency window, rather
than erroring or rejecting.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cairn_cli import pull as pull_mod
from cairn_cli import query as query_mod
from cairn_cli import verify as verify_mod
from cairn_cli._pending import (
    TAG_PENDING_LABEL,
    is_tag_miss,
    is_tag_pending,
)
from cairn_cli._refs import ResolvedReference

# Canonical 3-segment ECR tag ({artifact-kind}--_ref--{digest}). The CLI treats a
# tag as an opaque convenience label and never parses its segments, so this is a
# representative literal — its only role here is "a present, non-empty tag".
CANONICAL_TAG = "assessment-results--_ref--0123456789ab"

# --- shared helpers (_pending) ------------------------------------------------


def test_is_tag_miss_recognizes_registry_not_found_codes():
    assert is_tag_miss("MANIFEST_UNKNOWN: manifest unknown")
    assert is_tag_miss("Error: NAME_UNKNOWN")
    assert is_tag_miss("repository:tag: not found")
    # A genuine transport / credential fault is NOT a tag miss.
    assert not is_tag_miss("AccessDenied: not authorized")
    assert not is_tag_miss("connection refused")


def test_is_tag_pending_digest_without_tag():
    digest = "sha256:" + "a" * 64
    assert is_tag_pending({"manifestDigest": digest})
    assert is_tag_pending({"manifestDigest": digest, "imageTag": ""})
    assert is_tag_pending({"manifestDigest": digest, "imageTag": None})
    # Tagged → not pending; no digest → not resolvable, not pending.
    assert not is_tag_pending({"manifestDigest": digest, "imageTag": CANONICAL_TAG})
    assert not is_tag_pending({"imageTag": CANONICAL_TAG})


def test_pending_fallback_keys_on_digest_not_tag_form():
    """The tag-pending fallback is purely digest-driven and form-agnostic.

    A record bearing the canonical tag is "tagged" (not pending); the same record
    with the tag absent is pending — proving the fallback keys on digest presence
    + tag absence, never on the tag's internal shape (tags remain a non-load-
    bearing convenience).
    """
    digest = "sha256:" + "d" * 64
    assert not is_tag_pending({"manifestDigest": digest, "imageTag": CANONICAL_TAG})
    assert is_tag_pending({"manifestDigest": digest})
    assert is_tag_pending({"manifestDigest": digest, "imageTag": ""})


# --- pull -------------------------------------------------------------------


class _FakeOras:
    """A login-capable oras stand-in with a REAL exception class.

    Using a bare ``MagicMock`` as ``oras_module`` is a trap: ``oras_module.OrasCommandError``
    would be an auto-generated mock, not a ``BaseException`` subclass, so the read
    paths' ``except oras_module.OrasCommandError`` clause raises ``TypeError`` if it
    is ever evaluated. A no-op ``login`` plus a real exception class keeps the
    tag-pending tests honest.
    """

    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def login(self, *, registry: str, username: str, password: str) -> None:
        pass


def _token_ecr() -> MagicMock:
    """A MagicMock ECR client returning a usable basic-auth token.

    These tests run without moto, so the read paths' ECR login (added for
    docker-login parity with push) needs a client whose
    ``get_authorization_token`` decodes to ``AWS:<password>``.
    """
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {
        "authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:pw").decode()}]
    }
    return ecr


def _by_tag_ref() -> ResolvedReference:
    return ResolvedReference(
        registry="123.dkr.ecr.us-east-2.amazonaws.com",
        repository="artifact-cmh-alpha/acme/assessment-results",
        selector=":v1",
    )


def _by_digest_ref() -> ResolvedReference:
    return ResolvedReference(
        registry="123.dkr.ecr.us-east-2.amazonaws.com",
        repository="artifact-cmh-alpha/acme/assessment-results",
        selector="@sha256:" + "a" * 64,
    )


@pytest.fixture
def pull_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        reference="acme/assessment-results:v1",
        output_dir=tmp_path,
        stage="alpha",
        region_code="cmh",
        # Explicit appName so resolve_app_name short-circuits without an SSM read
        # (these tests inject a bare MagicMock SSM client).
        app_name="cairnproofs4af",
    )


def test_pull_by_tag_miss_in_verify_is_tag_pending(monkeypatch, pull_args, capsys):
    """A by-tag verify miss during the pre-mint window → tag pending, not error."""
    from cairn_signing import NotationCommandError

    monkeypatch.setattr(pull_mod, "resolve_reference", lambda *a, **k: _by_tag_ref())

    def boom(ref: str) -> None:
        raise NotationCommandError(
            returncode=1, stderr="MANIFEST_UNKNOWN: manifest unknown", command=["notation"]
        )

    rc = pull_mod._run(
        pull_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_FakeOras(),
        verify_fn=boom,
    )
    assert rc == pull_mod.TAG_PENDING_EXIT_CODE
    out = capsys.readouterr().out
    assert TAG_PENDING_LABEL in out
    # The digest anchor is surfaced as the fallback resolution path.
    assert "@sha256:" in out


def test_pull_by_tag_miss_in_manifest_fetch_is_tag_pending(monkeypatch, pull_args, capsys):
    monkeypatch.setattr(pull_mod, "resolve_reference", lambda *a, **k: _by_tag_ref())
    monkeypatch.setattr(pull_mod, "verify_artifact", lambda ref: None)

    class _Oras:
        OrasCommandError = pull_mod._oras.OrasCommandError

        def login(self, *, registry: str, username: str, password: str) -> None:
            pass

        def fetch_manifest(self, *, reference: str) -> bytes:
            raise self.OrasCommandError(returncode=1, stderr="manifest unknown", command=["oras"])

    rc = pull_mod._run(
        pull_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_Oras(),
        verify_fn=lambda ref: None,
    )
    assert rc == pull_mod.TAG_PENDING_EXIT_CODE
    assert TAG_PENDING_LABEL in capsys.readouterr().out


def test_pull_by_digest_miss_stays_hard_error(monkeypatch, pull_args):
    """A by-DIGEST miss is NOT the pending window — it stays a hard error."""
    from cairn_signing import NotationCommandError

    monkeypatch.setattr(pull_mod, "resolve_reference", lambda *a, **k: _by_digest_ref())

    def boom(ref: str) -> None:
        raise NotationCommandError(returncode=1, stderr="MANIFEST_UNKNOWN", command=["notation"])

    rc = pull_mod._run(
        pull_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_FakeOras(),
        verify_fn=boom,
    )
    assert rc == 4


def test_pull_real_signature_failure_is_not_pending(monkeypatch, pull_args):
    """A genuine signature failure by tag must remain a hard error, not pending."""
    from cairn_signing import NotationCommandError

    monkeypatch.setattr(pull_mod, "resolve_reference", lambda *a, **k: _by_tag_ref())

    def boom(ref: str) -> None:
        raise NotationCommandError(
            returncode=1, stderr="signature verification failed", command=["notation"]
        )

    rc = pull_mod._run(
        pull_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_FakeOras(),
        verify_fn=boom,
    )
    assert rc == 4


# --- verify -----------------------------------------------------------------


@pytest.fixture
def verify_args() -> argparse.Namespace:
    return argparse.Namespace(
        reference="acme/assessment-results:v1",
        stage="alpha",
        region_code="cmh",
        app_name="cairnproofs4af",
    )


def _policy(tmp_path: Path) -> Path:
    p = tmp_path / "trustpolicy.json"
    p.write_text("{}")
    return p


def test_verify_by_tag_miss_is_tag_pending(monkeypatch, verify_args, tmp_path, capsys):
    from cairn_signing import NotationCommandError

    monkeypatch.setattr(verify_mod, "resolve_reference", lambda *a, **k: _by_tag_ref())

    def boom(ref: str) -> None:
        raise NotationCommandError(returncode=1, stderr="MANIFEST_UNKNOWN", command=["notation"])

    rc = verify_mod._run(
        verify_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_FakeOras(),
        verify_fn=boom,
        trust_policy_path=_policy(tmp_path),
    )
    assert rc == verify_mod.TAG_PENDING_EXIT_CODE
    out = capsys.readouterr().out
    assert TAG_PENDING_LABEL in out
    assert "@sha256:" in out


def test_verify_real_failure_stays_error(monkeypatch, verify_args, tmp_path):
    from cairn_signing import NotationCommandError

    monkeypatch.setattr(verify_mod, "resolve_reference", lambda *a, **k: _by_tag_ref())

    def boom(ref: str) -> None:
        raise NotationCommandError(returncode=1, stderr="bad signature", command=["notation"])

    rc = verify_mod._run(
        verify_args,
        ssm_client=MagicMock(),
        ecr_client=_token_ecr(),
        oras_module=_FakeOras(),
        verify_fn=boom,
        trust_policy_path=_policy(tmp_path),
    )
    assert rc == 4


# --- query ------------------------------------------------------------------


class _Page:
    def __init__(self, items):
        self.items = items
        self.next_token = None


def test_query_marks_pending_record(monkeypatch, capsys):
    """A by-tag lookup landing on an untagged-but-digest-resolvable record
    surfaces the pending indicator in the emitted JSONL — not an error."""
    digest = "sha256:" + "b" * 64
    pending = {"projectId": "acme", "manifestDigest": digest}  # no imageTag yet
    tagged = {
        "projectId": "acme",
        "manifestDigest": "sha256:" + "c" * 64,
        "imageTag": CANONICAL_TAG,
    }

    monkeypatch.setattr(query_mod, "_query_by_project", lambda *a, **k: _Page([pending, tagged]))

    args = argparse.Namespace(
        project="acme",
        tenant_id="acme",
        control=None,
        catalog_or_profile_uuid=None,
        limit=100,
        page_token=None,
        stage="alpha",
        region_code="cmh",
        app_name="cairnproofs4af",
        date_range=None,
        date_from=None,
        date_to=None,
        assessment_id=None,
        text=None,
        digest=None,
    )
    rc = query_mod._run(args, dynamodb_resource=MagicMock())
    assert rc == 0

    out_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    by_digest = {rec["manifestDigest"]: rec for rec in out_lines}
    # The pending record is resolved by its digest and flagged tag-pending.
    assert by_digest[digest]["tagPending"] is True
    assert by_digest[digest]["tagState"] == TAG_PENDING_LABEL
    # The already-tagged record is untouched.
    assert "tagPending" not in by_digest["sha256:" + "c" * 64]
