"""Authenticated push principal extraction ([PIPELINE-010]).

Producers are untrusted for content claims — the only producer-side identity
we trust is the AWS-verified IAM principal that performed the ECR push,
sourced from the EventBridge event's `userIdentity` block. The Config_Blob's
own producer fields are informational only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthenticatedPushPrincipal:
    """The AWS-verified principal that performed the ECR push ([PIPELINE-010])."""

    arn: str
    type: str  # "IAMUser" | "AssumedRole" | "Root" | "FederatedUser" | etc.
    account_id: str | None  # AWS_Account_ID (12-digit) — distinct from logical Account_ID
    user_name: str | None = None
    # CloudTrail surfaces MFA as a string "true"/"false" on
    # sessionContext.attributes.mfaAuthenticated; we normalize to bool|None
    # so downstream consumers don't have to re-parse the string.
    mfa_authenticated: bool | None = None
    principal_id: str | None = None


class MissingUserIdentityError(ValueError):
    """Raised when the EventBridge event has no usable userIdentity block.

    Per [PIPELINE-010.01] / the validate-bundle Lambda contract, this is a hard
    failure — we don't accept producer-asserted identity as a fallback.
    """


def extract_authenticated_push_principal(event: dict[str, Any]) -> AuthenticatedPushPrincipal:
    """Extract userIdentity from an ECR-push EventBridge event.

    Accepts both raw CloudTrail-shaped events (detail.userIdentity directly)
    and EventBridge-wrapped CloudTrail events. Raises MissingUserIdentityError
    if no userIdentity is present — never falls back to producer-asserted
    fields.
    """
    detail = event.get("detail") if isinstance(event, dict) else None
    if not isinstance(detail, dict):
        raise MissingUserIdentityError("event.detail is missing or not a dict")

    user_identity = detail.get("userIdentity")
    if not isinstance(user_identity, dict):
        raise MissingUserIdentityError("event.detail.userIdentity is missing or not a dict")

    arn = user_identity.get("arn")
    user_type = user_identity.get("type")
    if not isinstance(arn, str) or not isinstance(user_type, str):
        raise MissingUserIdentityError("userIdentity is missing required 'arn' or 'type' fields")

    session_context = user_identity.get("sessionContext") or {}
    session_issuer = (
        session_context.get("sessionIssuer", {}) if isinstance(session_context, dict) else {}
    )
    attributes = session_context.get("attributes", {}) if isinstance(session_context, dict) else {}
    mfa_raw = attributes.get("mfaAuthenticated") if isinstance(attributes, dict) else None
    mfa_authenticated: bool | None
    if isinstance(mfa_raw, bool):
        mfa_authenticated = mfa_raw
    elif isinstance(mfa_raw, str):
        mfa_authenticated = mfa_raw.lower() == "true"
    else:
        mfa_authenticated = None

    return AuthenticatedPushPrincipal(
        arn=arn,
        type=user_type,
        account_id=user_identity.get("accountId"),
        user_name=user_identity.get("userName") or session_issuer.get("userName"),
        mfa_authenticated=mfa_authenticated,
        principal_id=user_identity.get("principalId"),
    )
