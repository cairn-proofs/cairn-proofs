"""Tests for cairn_cli.sign — reviewer sign-off over stock Notation tooling."""

from __future__ import annotations

import argparse

import pytest
from cairn_cli import main as main_module
from cairn_cli import sign as sign_module
from cairn_signing import REVIEWER_PROFILE_NAME, NotationCommandError, SigningProfile

from tests.unit.cairn_cli.conftest import APP_NAME, ECR_HOST, subparser_help

# The CLI reads the appName-prefixed reviewer-profile SSM path.
SSM_REVIEWER_PARAM = f"/{APP_NAME}/alpha/signing/reviewer-profile-version-arn"
REVIEWER_ARN = (
    "arn:aws:signer:us-east-2:123456789012:/signing-profiles/"
    f"{APP_NAME}_artifact_reviewer_signoff/abc"
)


def _make_args(reference: str) -> argparse.Namespace:
    return argparse.Namespace(reference=reference, stage="alpha", region_code="cmh", app_name=None)


def _put_reviewer_arn(ssm) -> None:
    ssm.put_parameter(Name=SSM_REVIEWER_PARAM, Value=REVIEWER_ARN, Type="String", Overwrite=True)


class _FakeOras:
    """Minimal oras stand-in so sign's ECR login does not shell out."""

    class OrasCommandError(RuntimeError):
        def __init__(self, *, returncode: int, stderr: str, command: list[str]) -> None:
            super().__init__(stderr)
            self.returncode = returncode
            self.stderr = stderr
            self.command = command

    def __init__(self, *, login_should_fail: str | None = None) -> None:
        self._login_should_fail = login_should_fail
        self.login_calls: list[dict[str, str]] = []

    def login(self, *, registry: str, username: str, password: str) -> None:
        if self._login_should_fail is not None:
            raise self.OrasCommandError(returncode=1, stderr=self._login_should_fail, command=[])
        self.login_calls.append({"registry": registry, "username": username})


def test_sign_registered_and_help_has_example() -> None:
    parser = main_module.build_parser()
    help_text = subparser_help(parser, "sign")
    assert "usage" in help_text.lower()
    assert "Examples:" in help_text
    assert "cairn sign alpha-bank/assessment-results:2026-q1" in help_text


def test_sign_success_targets_reviewer_profile(
    aws_stack, capsys: pytest.CaptureFixture[str]
) -> None:
    _put_reviewer_arn(aws_stack["ssm"])
    captured: dict[str, object] = {}

    def fake_sign_off(reference: str, profile: SigningProfile, **_kwargs):
        captured["reference"] = reference
        captured["profile"] = profile
        return None

    args = _make_args("alpha-bank/assessment-results:2026-q1")
    fake_oras = _FakeOras()
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=fake_sign_off,
        oras_module=fake_oras,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "signed-off:" in out
    # It signed the resolved long-form reference against the reviewer profile.
    profile = captured["profile"]
    assert isinstance(profile, SigningProfile)
    assert profile.name == REVIEWER_PROFILE_NAME
    assert profile.arn == REVIEWER_ARN
    assert ECR_HOST in str(captured["reference"])
    # It authenticated the credential store itself (no ambient docker login).
    assert len(fake_oras.login_calls) == 1


def test_sign_off_targets_manifest_digest_referrer(
    aws_stack, capsys: pytest.CaptureFixture[str]
) -> None:
    # A by-digest reference must reach sign_off with its @sha256:... selector
    # intact — the sign-off referrer is attached to the bundle's Manifest_Digest
    # (signing [SIGN-002], [SIGN-003]), not a mutable tag, so the produced referrer keys on
    # the immutable digest.
    _put_reviewer_arn(aws_stack["ssm"])
    digest = "sha256:" + "a" * 64
    captured: dict[str, object] = {}

    def fake_sign_off(reference: str, profile: SigningProfile, **_kwargs):
        captured["reference"] = reference
        return None

    args = _make_args(f"alpha-bank/assessment-results@{digest}")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=fake_sign_off,
        oras_module=_FakeOras(),
    )
    assert rc == 0
    ref = str(captured["reference"])
    assert ref.endswith(f"@{digest}")
    assert ECR_HOST in ref
    assert f"signed-off: {ECR_HOST}" in capsys.readouterr().out


def test_sign_invalid_reference_returns_2(aws_stack) -> None:
    _put_reviewer_arn(aws_stack["ssm"])
    args = _make_args("totally-bogus")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=lambda *a, **k: None,
        oras_module=_FakeOras(),
    )
    assert rc == 2


def test_sign_missing_reviewer_arn_returns_4(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    # No reviewer-profile-arn parameter written → resolution fails.
    args = _make_args("alpha-bank/assessment-results:2026-q1")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=lambda *a, **k: None,
        oras_module=_FakeOras(),
    )
    assert rc == 4
    assert "reviewer Signing_Profile" in capsys.readouterr().err


def test_sign_rejects_non_reviewer_profile_returns_2(
    aws_stack, capsys: pytest.CaptureFixture[str]
) -> None:
    _put_reviewer_arn(aws_stack["ssm"])

    def fake_sign_off(reference: str, profile: SigningProfile, **_kwargs):
        raise ValueError("sign_off requires the 'artifact-reviewer-signoff' profile")

    args = _make_args("alpha-bank/assessment-results:2026-q1")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=fake_sign_off,
        oras_module=_FakeOras(),
    )
    assert rc == 2
    assert "artifact-reviewer-signoff" in capsys.readouterr().err


def test_sign_notation_failure_returns_4(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _put_reviewer_arn(aws_stack["ssm"])

    def fake_sign_off(reference: str, profile: SigningProfile, **_kwargs):
        raise NotationCommandError(returncode=1, stderr="signing failed: access denied", command=[])

    args = _make_args("alpha-bank/assessment-results:2026-q1")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=fake_sign_off,
        oras_module=_FakeOras(),
    )
    assert rc == 4
    assert "signing failed: access denied" in capsys.readouterr().err


def test_sign_login_failure_returns_4(aws_stack, capsys: pytest.CaptureFixture[str]) -> None:
    _put_reviewer_arn(aws_stack["ssm"])
    args = _make_args("alpha-bank/assessment-results:2026-q1")
    rc = sign_module._run(
        args,
        ssm_client=aws_stack["ssm"],
        ecr_client=aws_stack["ecr"],
        sign_off_fn=lambda *a, **k: None,
        oras_module=_FakeOras(login_should_fail="denied: not authorized"),
    )
    assert rc == 4
    assert "ECR login failed" in capsys.readouterr().err
