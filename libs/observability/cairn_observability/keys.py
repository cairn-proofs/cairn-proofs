"""Canonical key names for fields that recur across pipeline stages (Obs 4).

Use these constants when attaching structured fields to log context so
searches and dashboards operate on a stable vocabulary. Pipeline-specific
fields not enumerated here follow camelCase by convention.
"""

from typing import Final


class ObservabilityKeys:
    """Canonical structured-log field names (Obs 4, Obs 7)."""

    # Base fields guaranteed on every log line (Obs 6)
    LEVEL: Final[str] = "level"
    MESSAGE: Final[str] = "message"
    TRACE_ID: Final[str] = "traceId"
    SESSION_ID: Final[str] = "sessionId"
    SPAN_ID: Final[str] = "spanId"

    # Per-stage required fields (Obs 7)
    REQUEST_ID: Final[str] = "requestId"
    EVENT_TYPE: Final[str] = "eventType"
    PROJECT_ID: Final[str] = "projectId"
    MANIFEST_DIGEST: Final[str] = "manifestDigest"
    DURATION_MS: Final[str] = "durationMs"
    STATUS: Final[str] = "status"

    # Frequently attached pipeline fields (Obs 4)
    ARTIFACT_KIND: Final[str] = "artifactKind"
    TENANT_ID: Final[str] = "tenantId"
    ECR_REPOSITORY: Final[str] = "ecrRepository"
    SIGNING_PROFILE_ARN: Final[str] = "signingProfileArn"


# Allow-list of baggage keys that are permitted to flow from W3C baggage
# (extracted from producer events) or `observability_scope(**extra_fields)`
# into structured log output as top-level fields. Producer-supplied baggage
# entries with keys NOT in this set are dropped at formatter time, and
# `extra_fields` kwargs with disallowed keys are silently skipped at scope
# entry. The intent is to keep operator-meaningful field names a closed
# vocabulary and prevent an untrusted producer from injecting arbitrary
# keys (potentially shadowing operator fields or accidentally capturing
# upstream secrets) into CloudWatch Logs.
ALLOWED_BAGGAGE_KEYS: Final[frozenset[str]] = frozenset(
    {
        ObservabilityKeys.SESSION_ID,
        ObservabilityKeys.REQUEST_ID,
        ObservabilityKeys.EVENT_TYPE,
        ObservabilityKeys.PROJECT_ID,
        ObservabilityKeys.MANIFEST_DIGEST,
        ObservabilityKeys.ARTIFACT_KIND,
        ObservabilityKeys.TENANT_ID,
        ObservabilityKeys.ECR_REPOSITORY,
        ObservabilityKeys.SIGNING_PROFILE_ARN,
    }
)


# Closed vocabularies for the eventType and status fields (Obs 7).
EVENT_TYPES_SUCCESS: Final[frozenset[str]] = frozenset(
    {"validated", "signed", "archived", "metadata-updated"}
)
EVENT_TYPES_FAILURE: Final[frozenset[str]] = frozenset(
    {"validation-failed", "signing-failed", "archive-failed"}
)
EVENT_TYPES: Final[frozenset[str]] = EVENT_TYPES_SUCCESS | EVENT_TYPES_FAILURE

STATUSES: Final[frozenset[str]] = frozenset({"success", "failure", "error"})
