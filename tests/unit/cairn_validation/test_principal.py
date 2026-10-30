"""Tests for cairn_validation.principal ([PIPELINE-010.01] — authenticated push principal)."""

from __future__ import annotations

from typing import Any

import pytest
from cairn_validation.principal import (
    AuthenticatedPushPrincipal,
    MissingUserIdentityError,
    extract_authenticated_push_principal,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _wrap(user_identity: dict[str, Any]) -> dict[str, Any]:
    return {"detail": {"userIdentity": user_identity}}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_extract_basic_iam_user_identity() -> None:
    event = _wrap(
        {
            "type": "IAMUser",
            "arn": "arn:aws:iam::111122223333:user/alice",
            "accountId": "111122223333",
            "userName": "alice",
            "principalId": "AIDA...",
        }
    )
    principal = extract_authenticated_push_principal(event)
    assert principal == AuthenticatedPushPrincipal(
        arn="arn:aws:iam::111122223333:user/alice",
        type="IAMUser",
        account_id="111122223333",
        user_name="alice",
        mfa_authenticated=None,
        principal_id="AIDA...",
    )


def test_extract_assumed_role_picks_session_issuer_username() -> None:
    event = _wrap(
        {
            "type": "AssumedRole",
            "arn": "arn:aws:sts::111122223333:assumed-role/EcrPushRole/session",
            "accountId": "111122223333",
            "sessionContext": {
                "sessionIssuer": {"userName": "EcrPushRole"},
                "attributes": {"mfaAuthenticated": "true"},
            },
            "principalId": "AROA...",
        }
    )
    principal = extract_authenticated_push_principal(event)
    assert principal.user_name == "EcrPushRole"
    assert principal.mfa_authenticated is True
    assert principal.type == "AssumedRole"


def test_extract_accounts_for_missing_optional_fields() -> None:
    event = _wrap({"type": "Root", "arn": "arn:aws:iam::111122223333:root"})
    principal = extract_authenticated_push_principal(event)
    assert principal.user_name is None
    assert principal.mfa_authenticated is None
    assert principal.principal_id is None
    assert principal.account_id is None


# ---------------------------------------------------------------------------
# Failure paths ([PIPELINE-010.01] — must NEVER fall back to producer-asserted fields)
# ---------------------------------------------------------------------------


def test_missing_detail_block_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal({})


def test_non_dict_detail_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal({"detail": "string-not-dict"})


def test_missing_user_identity_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal({"detail": {}})


def test_non_dict_user_identity_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal({"detail": {"userIdentity": "string"}})


def test_missing_arn_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(_wrap({"type": "IAMUser"}))


def test_missing_type_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(_wrap({"arn": "arn:aws:iam::111:user/a"}))


def test_non_string_arn_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(_wrap({"arn": 123, "type": "IAMUser"}))


def test_non_string_type_raises() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(_wrap({"arn": "arn:...", "type": 42}))


def test_non_dict_event_raises_via_attribute_access() -> None:
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal("not-an-event")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# [PIPELINE-010.01] — invariant: extracts arn+type when present; raises when missing.
# ---------------------------------------------------------------------------


# Build arbitrary userIdentity blobs; we only care whether arn+type are present.
arn_strategy = st.text(min_size=1, max_size=64).map(lambda s: f"arn:aws:iam::111:user/{s}")
type_strategy = st.sampled_from(["IAMUser", "AssumedRole", "Root", "FederatedUser"])


@given(arn=arn_strategy, user_type=type_strategy, account=st.text(min_size=1, max_size=16))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property24_extracts_when_arn_and_type_present(
    arn: str, user_type: str, account: str
) -> None:
    event = _wrap({"arn": arn, "type": user_type, "accountId": account})
    principal = extract_authenticated_push_principal(event)
    assert principal.arn == arn
    assert principal.type == user_type
    assert principal.account_id == account


@given(
    arn=arn_strategy,
    user_type=type_strategy,
    drop=st.sampled_from(["arn", "type", "both"]),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property24_raises_when_arn_or_type_missing(arn: str, user_type: str, drop: str) -> None:
    payload: dict[str, Any] = {"arn": arn, "type": user_type, "accountId": "111"}
    if drop in ("arn", "both"):
        payload.pop("arn")
    if drop in ("type", "both"):
        payload.pop("type")
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(_wrap(payload))
