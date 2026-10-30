"""Tests for cairn_signing.profile (SigningProfile factory methods)."""

from __future__ import annotations

from cairn_signing.profile import (
    PIPELINE_PROFILE_NAME,
    REVIEWER_PROFILE_NAME,
    SigningProfile,
)


def test_pipeline_profile_name_matches_spec() -> None:
    # Hard-coded name is the logical-label contract (the two profile kinds'
    # discriminator); the deployed physical name is bound by ARN via SSM.
    assert PIPELINE_PROFILE_NAME == "artifact-pipeline-signing"


def test_reviewer_profile_name_matches_spec() -> None:
    assert REVIEWER_PROFILE_NAME == "artifact-reviewer-signoff"


def test_pipeline_factory_uses_pipeline_name() -> None:
    arn = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-pipeline-signing"
    p = SigningProfile.pipeline(arn)
    assert p.name == PIPELINE_PROFILE_NAME
    assert p.arn == arn
    assert p.platform_id == "Notation-OCI-SHA384-ECDSA"


def test_reviewer_factory_uses_reviewer_name() -> None:
    arn = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-reviewer-signoff"
    p = SigningProfile.reviewer(arn)
    assert p.name == REVIEWER_PROFILE_NAME
    assert p.arn == arn


def test_signing_profile_default_platform_id() -> None:
    p = SigningProfile(name="x", arn="arn:y")
    assert p.platform_id == "Notation-OCI-SHA384-ECDSA"


def test_signing_profile_platform_id_override() -> None:
    p = SigningProfile(name="x", arn="arn:y", platform_id="Custom-Platform")
    assert p.platform_id == "Custom-Platform"


def test_signing_profile_is_frozen() -> None:
    import dataclasses

    import pytest

    p = SigningProfile.pipeline("arn:aws:signer:...:profile/x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.name = "mutated"  # type: ignore[misc]
