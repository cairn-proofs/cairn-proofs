"""Signing_Profile reference ([SIGN-001], [SIGN-003])."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Logical Signing_Profile labels (genus = artifact; the signer of a sign-off is
# the Attesting Reviewer, NOT a read-only auditor — see the audit-lexicon
# scope-down decision). These are internal discriminator labels used to
# distinguish the two profile kinds; the deployed AWS Signer physical names
# (``{appName}_artifact_{pipeline_signing,reviewer_signoff}_{qualifier}``) are
# bound at the ARN level via SSM, not by string-matching these constants.
PIPELINE_PROFILE_NAME: Final[str] = "artifact-pipeline-signing"
REVIEWER_PROFILE_NAME: Final[str] = "artifact-reviewer-signoff"


@dataclass(frozen=True)
class SigningProfile:
    """An AWS Signer Signing_Profile reference (name + ARN + signing platform)."""

    name: str
    arn: str
    platform_id: str = "Notation-OCI-SHA384-ECDSA"

    @classmethod
    def pipeline(cls, arn: str) -> SigningProfile:
        return cls(name=PIPELINE_PROFILE_NAME, arn=arn)

    @classmethod
    def reviewer(cls, arn: str) -> SigningProfile:
        return cls(name=REVIEWER_PROFILE_NAME, arn=arn)
