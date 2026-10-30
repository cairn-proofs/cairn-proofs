"""Notation + AWS Signer integration for the Cairn Provenance_System.

Thin wrapper around the Notation CLI binary (provided by the Notation Lambda
layer). Reads the TimestampProvider strategy from SSM Parameter
Store (`system-clock` is built; `rfc3161` is a recognized but unsupported
value) and exposes a single `sign_artifact` entrypoint that invokes Notation
with the AWS Signer plugin against the `artifact-pipeline-signing` profile.
"""

from cairn_signing.notation import (
    NotationCommandError,
    SignArtifactResult,
    sign_artifact,
    sign_off,
    verify_artifact,
)
from cairn_signing.profile import (
    PIPELINE_PROFILE_NAME,
    REVIEWER_PROFILE_NAME,
    SigningProfile,
)
from cairn_signing.status import (
    KNOWN_SIGNING_PROFILE_NAMES,
    AggregatedSignatureStatus,
    BundleTrust,
    BundleTrustVerdict,
    SignatureDetail,
    SignatureResult,
    SignatureRole,
    SignatureStatus,
    SigningStatus,
    aggregate_status,
    bundle_trust_from_projection,
    classify_bundle_trust,
)
from cairn_signing.timestamp_provider import (
    TimestampProvider,
    read_timestamp_provider_from_ssm,
)

__all__ = [
    "KNOWN_SIGNING_PROFILE_NAMES",
    "PIPELINE_PROFILE_NAME",
    "REVIEWER_PROFILE_NAME",
    "AggregatedSignatureStatus",
    "BundleTrust",
    "BundleTrustVerdict",
    "NotationCommandError",
    "SignArtifactResult",
    "SignatureDetail",
    "SignatureResult",
    "SignatureRole",
    "SignatureStatus",
    "SigningProfile",
    "SigningStatus",
    "TimestampProvider",
    "aggregate_status",
    "bundle_trust_from_projection",
    "classify_bundle_trust",
    "read_timestamp_provider_from_ssm",
    "sign_artifact",
    "sign_off",
    "verify_artifact",
]
