"""Tests for cairn_observability.keys vocabulary constants."""

from __future__ import annotations

from cairn_observability.keys import (
    EVENT_TYPES,
    EVENT_TYPES_FAILURE,
    EVENT_TYPES_SUCCESS,
    STATUSES,
    ObservabilityKeys,
)


def test_base_field_names_match_obs6() -> None:
    assert ObservabilityKeys.LEVEL == "level"
    assert ObservabilityKeys.MESSAGE == "message"
    assert ObservabilityKeys.TRACE_ID == "traceId"
    assert ObservabilityKeys.SESSION_ID == "sessionId"


def test_per_stage_required_fields_match_obs7() -> None:
    assert ObservabilityKeys.REQUEST_ID == "requestId"
    assert ObservabilityKeys.EVENT_TYPE == "eventType"
    assert ObservabilityKeys.PROJECT_ID == "projectId"
    assert ObservabilityKeys.MANIFEST_DIGEST == "manifestDigest"
    assert ObservabilityKeys.DURATION_MS == "durationMs"
    assert ObservabilityKeys.STATUS == "status"


def test_pipeline_field_names() -> None:
    assert ObservabilityKeys.ARTIFACT_KIND == "artifactKind"
    assert ObservabilityKeys.TENANT_ID == "tenantId"
    assert ObservabilityKeys.ECR_REPOSITORY == "ecrRepository"
    assert ObservabilityKeys.SIGNING_PROFILE_ARN == "signingProfileArn"


def test_allowed_baggage_keys_carry_tenant_not_account() -> None:
    """The closed allow-list stays in lockstep with the metric `TenantId`
    dimension ([OBS-006.04]): `tenantId` is allowed, `accountId` is not."""
    from cairn_observability.keys import ALLOWED_BAGGAGE_KEYS

    assert ObservabilityKeys.TENANT_ID in ALLOWED_BAGGAGE_KEYS
    assert "tenantId" in ALLOWED_BAGGAGE_KEYS
    assert "accountId" not in ALLOWED_BAGGAGE_KEYS


def test_event_types_success_vocabulary() -> None:
    assert EVENT_TYPES_SUCCESS == frozenset({"validated", "signed", "archived", "metadata-updated"})


def test_event_types_failure_vocabulary() -> None:
    assert EVENT_TYPES_FAILURE == frozenset(
        {"validation-failed", "signing-failed", "archive-failed"}
    )


def test_event_types_union_matches() -> None:
    assert EVENT_TYPES == EVENT_TYPES_SUCCESS | EVENT_TYPES_FAILURE


def test_event_types_success_failure_disjoint() -> None:
    assert EVENT_TYPES_SUCCESS.isdisjoint(EVENT_TYPES_FAILURE)


def test_statuses_vocabulary() -> None:
    assert STATUSES == frozenset({"success", "failure", "error"})
