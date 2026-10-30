"""Tests for cairn_signing.notation (subprocess-wrapped Notation CLI)."""

from __future__ import annotations

import subprocess

import pytest
from cairn_signing.notation import (
    NotationCommandError,
    sign_artifact,
    sign_off,
    verify_artifact,
)
from cairn_signing.profile import REVIEWER_PROFILE_NAME, SigningProfile
from cairn_signing.timestamp_provider import TimestampProvider, UnsupportedTimestampProvider

ARTIFACT_REF = "111122223333.dkr.ecr.us-east-2.amazonaws.com/r1@sha256:" + "0" * 64
PROFILE_ARN = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-pipeline-signing"
SIGNATURE_DIGEST = "sha256:" + "f" * 64


def _profile() -> SigningProfile:
    return SigningProfile.pipeline(PROFILE_ARN)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_sign_artifact_invokes_sign_and_returns_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    invocations: list[list[str]] = []

    def _fake_run(cmd, capture_output, text, timeout, check):
        invocations.append(cmd)
        return _completed()

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = sign_artifact(
        ARTIFACT_REF,
        _profile(),
        TimestampProvider.SYSTEM_CLOCK,
        timeout_seconds=15.0,
    )

    assert result.artifact_reference == ARTIFACT_REF
    assert result.timestamp_provider is TimestampProvider.SYSTEM_CLOCK
    # sign_artifact no longer resolves the signature digest itself —
    # callers query ECR's Referrers API after this returns.
    assert len(invocations) == 1
    sign_call = invocations[0]
    assert "sign" in sign_call
    assert sign_call[-1] == ARTIFACT_REF
    assert "--id" in sign_call and PROFILE_ARN in sign_call
    assert "--plugin" in sign_call


def test_sign_artifact_raises_when_notation_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: None)
    with pytest.raises(RuntimeError, match="notation"):
        sign_artifact(ARTIFACT_REF, _profile(), TimestampProvider.SYSTEM_CLOCK, timeout_seconds=1.0)


def test_sign_artifact_rejects_rfc3161(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    with pytest.raises(UnsupportedTimestampProvider):
        sign_artifact(ARTIFACT_REF, _profile(), TimestampProvider.RFC3161, timeout_seconds=1.0)


def test_sign_artifact_raises_notation_command_error_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")

    def _fake_run(cmd, capture_output, text, timeout, check):
        return _completed(returncode=2, stderr="AccessDeniedException: missing kms:Sign\n")

    monkeypatch.setattr("subprocess.run", _fake_run)

    with pytest.raises(NotationCommandError) as exc:
        sign_artifact(ARTIFACT_REF, _profile(), TimestampProvider.SYSTEM_CLOCK, timeout_seconds=1.0)
    assert exc.value.returncode == 2
    assert "AccessDeniedException" in exc.value.stderr


# Note: the "no signatures found post-sign" failure mode now lives in the
# sign-bundle Lambda handler (which queries ECR's Referrers API after
# sign_artifact returns), not in sign_artifact itself.


def test_verify_artifact_succeeds_on_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _completed(stdout="ok"))
    verify_artifact(ARTIFACT_REF, timeout_seconds=1.0)


def test_verify_artifact_raises_on_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _completed(returncode=1, stderr="signature not trusted"),
    )
    with pytest.raises(NotationCommandError) as exc:
        verify_artifact(ARTIFACT_REF, timeout_seconds=1.0)
    assert "signature not trusted" in exc.value.stderr


def test_verify_artifact_raises_when_notation_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda b: None)
    with pytest.raises(RuntimeError, match="notation"):
        verify_artifact(ARTIFACT_REF, timeout_seconds=1.0)


REVIEWER_ARN = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-reviewer-signoff"


def test_sign_off_targets_reviewer_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    invocations: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **k: invocations.append(cmd) or _completed(),
    )

    result = sign_off(ARTIFACT_REF, SigningProfile.reviewer(REVIEWER_ARN), timeout_seconds=5.0)

    assert result.artifact_reference == ARTIFACT_REF
    assert result.timestamp_provider is TimestampProvider.SYSTEM_CLOCK
    sign_call = invocations[0]
    assert "sign" in sign_call
    assert REVIEWER_ARN in sign_call  # reviewer profile ARN is the --id


def test_sign_off_rejects_non_reviewer_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda b: "/opt/bin/notation")
    with pytest.raises(ValueError, match=REVIEWER_PROFILE_NAME):
        sign_off(ARTIFACT_REF, _profile(), timeout_seconds=1.0)


def test_notation_command_error_string_representation_includes_returncode_and_stderr() -> None:
    err = NotationCommandError(returncode=3, stderr="oops happened", command=["notation", "sign"])
    assert "exited 3" in str(err)
    assert "oops happened" in str(err)
