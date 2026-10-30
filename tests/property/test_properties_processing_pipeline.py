"""Property-based tests for the processing-pipeline capability.

Covers: [PIPELINE-013.01] accountId cross-check, [PIPELINE-006.01] permanent
vs transient failure classification, [PIPELINE-007.01] idempotency,
[PIPELINE-009.01] temporal consistency, [PIPELINE-010.01] authenticated push
principal capture.
"""

from __future__ import annotations

from typing import Any

import pytest
from cairn_lambda_sign_bundle.handler import _is_transient
from cairn_lambda_validate_bundle.handler import _temporal_check_layers
from cairn_metadata.conditional import (
    ConditionalUpdateFailed,
    create_record,
    update_record_if_newer,
)
from cairn_oci.producer_attestation import ProducerAttestationReferrer
from cairn_signing.notation import NotationCommandError
from cairn_validation.crosscheck import (
    crosscheck_identity,
)
from cairn_validation.principal import (
    MissingUserIdentityError,
    extract_authenticated_push_principal,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_id_edge = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789")
_id_inner = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-")


@st.composite
def lc_id(draw: st.DrawFn) -> str:
    length = draw(st.integers(min_value=1, max_value=20))
    if length == 1:
        return draw(_id_edge)
    inner = draw(st.text(alphabet=_id_inner, min_size=length - 2, max_size=length - 2))
    return draw(_id_edge) + inner + draw(_id_edge)


@st.composite
def rfc3339_iso(draw: st.DrawFn, *, year_min: int = 2020, year_max: int = 2030) -> str:
    year = draw(st.integers(min_value=year_min, max_value=year_max))
    month = draw(st.integers(min_value=1, max_value=12))
    if month in (1, 3, 5, 7, 8, 10, 12):
        max_day = 31
    elif month == 2:
        max_day = 28
    else:
        max_day = 30
    day = draw(st.integers(min_value=1, max_value=max_day))
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    second = draw(st.integers(min_value=0, max_value=59))
    suffix = draw(st.sampled_from(["Z", "+00:00"]))
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}{suffix}"


def _claim_with(
    *,
    org_id: str = "org-acme",
    tenant_id: str = "tnt-foo",
    project_id: str = "alpha-bank",
) -> ProducerAttestationReferrer:
    """Producer claim carrying the identity-triple cross-check input.

    The producer-asserted identity tuple — the opaque ``orgId`` + ``tenantId`` +
    ``projectId`` — lives in the signed Producer_Attestation_Referrer, not the
    content Config_Blob; the ECR repo path stays the authoritative anchor each
    coordinate is cross-checked exact-match against.
    """
    return ProducerAttestationReferrer(
        orgId=org_id,
        tenantId=tenant_id,
        projectId=project_id,
        artifactKind="assessment-results",
    )


# ---------------------------------------------------------------------------
# [PIPELINE-013.01] — claim org/tenant/project must equal the ECR path segments
# ---------------------------------------------------------------------------
# Source: openspec/specs/processing-pipeline/spec.md ([PIPELINE-013])


@given(
    claim_org=lc_id(),
    claim_tenant=lc_id(),
    claim_project=lc_id(),
    repo_org=lc_id(),
    repo_tenant=lc_id(),
    repo_project=lc_id(),
    qualifier=st.from_regex(r"^[a-z0-9]+-[a-z0-9]+$", fullmatch=True),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=80)
def test_property_4f_identity_crosscheck_biconditional(
    claim_org: str,
    claim_tenant: str,
    claim_project: str,
    repo_org: str,
    repo_tenant: str,
    repo_project: str,
    qualifier: str,
) -> None:
    """**WHEN** a bundle is validated whose producer-claimed orgId / tenantId /
    projectId triple does not exactly match the corresponding
    {org-id}/{tenant-id}/{project-id} segment of the ECR repository URI in the
    triggering EventBridge event **THEN** the bundle is treated as a permanent
    validation failure, one criterion-(f) error per mismatched coordinate is
    produced naming both values, and the bundle is routed to the DLQ without
    further retries. The check is uniform across all three segments — there is
    exactly one path shape and no deployment-mode gate.

    Source: processing-pipeline/spec.md ([PIPELINE-013] — uniform org + tenant +
    project exact-match cross-check against the IAM-enforced path).
    """
    claim = _claim_with(org_id=claim_org, tenant_id=claim_tenant, project_id=claim_project)
    repo_path = f"artifact-{qualifier}/{repo_org}/{repo_tenant}/{repo_project}"
    errors = crosscheck_identity(claim, repo_path)

    claim_triple = (claim_org, claim_tenant, claim_project)
    repo_triple = (repo_org, repo_tenant, repo_project)
    mismatches = sum(1 for c, r in zip(claim_triple, repo_triple, strict=True) if c != r)
    assert len(errors) == mismatches, (
        f"expected one error per mismatched coordinate ({mismatches}); got {errors}"
    )
    if mismatches == 0:
        assert errors == []
    else:
        # Every error is a criterion-(f) failure that names BOTH the claimed
        # value and the ECR path segment it failed against.
        joined = " ".join(e.message for e in errors)
        for err in errors:
            assert err.criterion == "f"
        for claim_value, repo_value in zip(claim_triple, repo_triple, strict=True):
            if claim_value != repo_value:
                assert repr(claim_value) in joined
                assert repr(repo_value) in joined


def test_property_4f_identity_crosscheck_reports_each_mismatched_segment() -> None:
    """**WHEN** all three producer-claimed identity coordinates disagree with the
    ECR path segments **THEN** the cross-check surfaces exactly three
    criterion-(f) errors — one per coordinate (org, tenant, project) — rather
    than failing on the first mismatch.

    Source: processing-pipeline/spec.md ([PIPELINE-013] — every mismatched segment is
    reported so an `artifact.validation-failed` event can name each).
    """
    claim = _claim_with(org_id="org-claim", tenant_id="tnt-claim", project_id="prj-claim")
    repo_path = "artifact-cmh-alpha/org-path/tnt-path/prj-path"
    errors = crosscheck_identity(claim, repo_path)
    assert len(errors) == 3
    assert {e.field for e in errors} == {
        "attestation.orgId",
        "attestation.tenantId",
        "attestation.projectId",
    }
    assert all(e.criterion == "f" for e in errors)


# ---------------------------------------------------------------------------
# [PIPELINE-006.01] — permanent vs transient failure classification
# ---------------------------------------------------------------------------


_TRANSIENT_MARKERS = (
    "ThrottlingException",
    "throttling",
    "Throttling",
    "RequestTimeout",
    "TimeoutError",
    "i/o timeout",
    "connection reset",
    "connection refused",
    "ServiceUnavailable",
    "InternalServerError",
    "503",
    "500",
)

_PERMANENT_STDERR_SAMPLES = (
    "AccessDenied: not allowed to sign",
    "ResourceNotFoundException: profile missing",
    "InvalidParameterValue",
    "X.509 certificate has expired",
    "",  # empty stderr with nonzero rc — treated as permanent
)


@given(marker=st.sampled_from(_TRANSIENT_MARKERS), pad=st.text(max_size=40))
def test_property_13_transient_marker_classified_transient(marker: str, pad: str) -> None:
    """**WHEN** the `validate-bundle` Lambda classifies a failure as permanent
    ... **THEN** the bundle is routed to the DLQ without retries ... and bundles
    classified as transient (network errors, throttling, service unavailability)
    are retried instead of DLQ-routed.

    Source: processing-pipeline/spec.md:71-73 ([PIPELINE-006.01] — transient classification
    via the sign-bundle Lambda's stderr classifier; the validate-bundle
    Lambda's permanent classification is exercised via the negative-space test
    below).
    """
    err = NotationCommandError(
        returncode=1, stderr=f"{pad}{marker}{pad}", command=["notation", "sign"]
    )
    assert _is_transient(err) is True


@given(stderr=st.sampled_from(_PERMANENT_STDERR_SAMPLES))
def test_property_13_non_transient_marker_classified_permanent(stderr: str) -> None:
    """**WHEN** the `validate-bundle` Lambda classifies a failure as permanent
    ... **THEN** the bundle is routed to the DLQ without retries ...

    Source: processing-pipeline/spec.md:71-73 ([PIPELINE-006.01] — permanent classification:
    stderr that does not contain any transient marker MUST classify as permanent).
    """
    err = NotationCommandError(returncode=1, stderr=stderr, command=["notation", "sign"])
    assert _is_transient(err) is False


@given(rc=st.integers(min_value=-5, max_value=255))
def test_property_13_returncode_zero_is_anomaly_path_safer_to_retry(rc: int) -> None:
    """**WHEN** the `validate-bundle` Lambda classifies a failure as permanent
    ... **THEN** the bundle is routed to the DLQ without retries ...

    Source: processing-pipeline/spec.md:71-73 ([PIPELINE-006.01] — anomaly path: a
    NotationCommandError with rc==0 is the impossible-looking case where the
    Notation CLI succeeded but the wrapper still observed a failure; the spec's
    permanent-vs-transient policy must err on retrying so a real transient
    upstream issue is not mis-classified as permanent on a faulty error surface).
    """
    err = NotationCommandError(
        returncode=rc, stderr="some unmatched gibberish", command=["notation", "sign"]
    )
    if rc == 0:
        assert _is_transient(err) is True
    else:
        assert _is_transient(err) is False


@given(stderr=st.text(max_size=200))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=60)
def test_property_13_classification_biconditional_on_transient_marker_substring(
    stderr: str,
) -> None:
    """**WHEN** the `validate-bundle` Lambda classifies a failure as permanent
    ... **THEN** the bundle is routed to the DLQ without retries ... and bundles
    classified as transient (network errors, throttling, service unavailability)
    are retried instead of DLQ-routed.

    Source: processing-pipeline/spec.md:71-73 ([PIPELINE-006.01] — biconditional: for any
    stderr string with rc != 0, `_is_transient` MUST be true iff the stderr
    contains at least one of the canonical transient markers).
    """
    err = NotationCommandError(returncode=1, stderr=stderr, command=["notation", "sign"])
    contains_marker = any(m in stderr for m in _TRANSIENT_MARKERS)
    assert _is_transient(err) == contains_marker


# ---------------------------------------------------------------------------
# [PIPELINE-007.01] — re-processing the same event has no duplicate side effects
# ---------------------------------------------------------------------------
#
# Backed by moto-mocked DynamoDB so the Hypothesis-generated operation
# sequences exercise the real conditional-write code path.


@pytest.fixture
def metadata_table_factory(aws_region: str):
    """Boot a moto-backed DynamoDB once per pytest test and return a factory.

    The factory yields a fresh, empty Evidence_Bundle table on every call so
    Hypothesis examples cannot leak state across each other (a single moto
    context per pytest test, one table per Hypothesis example).
    """
    import uuid

    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=aws_region)

        def _make():
            name = f"MetadataPropTest-{uuid.uuid4().hex[:8]}"
            resource.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            return resource.Table(name)

        yield _make


@given(repeats=st.integers(min_value=1, max_value=5))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=10,
    deadline=None,
)
def test_property_14_re_processing_same_create_event_is_idempotent_no_op(
    metadata_table_factory: Any, repeats: int
) -> None:
    """**WHEN** any pipeline Lambda processes the same EventBridge event twice
    (e.g., due to at-least-once delivery semantics) **THEN** the observable
    outcome is identical to processing it once: no duplicate signatures are
    created, no duplicate WORM copies are written, no duplicate metadata
    records are inserted, and no duplicate downstream events are emitted.

    Source: processing-pipeline/spec.md:85-87 ([PIPELINE-007.01] — repeat create_record
    invocations on the same pk MUST collapse to one row in the table).
    """
    table = metadata_table_factory()
    pk = "alpha-bank"
    sk = "sha256:" + "0" * 64
    item = {
        "pk": pk,
        "sk": sk,
        "status": "validated",
        "createdAt": "2026-05-24T00:00:00Z",
        "updatedAt": "2026-05-24T00:00:00Z",
    }
    create_record(table, item)
    for _ in range(repeats):
        with pytest.raises(ConditionalUpdateFailed):
            create_record(table, item)
    response = table.scan()
    assert len(response["Items"]) == 1
    stored = response["Items"][0]
    assert stored["pk"] == item["pk"]
    assert stored["sk"] == item["sk"]
    assert stored["status"] == "validated"


@given(timestamps=st.lists(rfc3339_iso(year_min=2024, year_max=2027), min_size=2, max_size=6))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    max_examples=10,
    deadline=None,
)
def test_property_14_out_of_order_update_redelivery_settles_to_latest_timestamp(
    metadata_table_factory: Any, timestamps: list[str]
) -> None:
    """**WHEN** any pipeline Lambda processes the same EventBridge event twice
    (e.g., due to at-least-once delivery semantics) **THEN** the observable
    outcome is identical to processing it once: no duplicate signatures are
    created, no duplicate WORM copies are written, no duplicate metadata
    records are inserted, and no duplicate downstream events are emitted.

    Source: processing-pipeline/spec.md:85-87 ([PIPELINE-007.01] — under arbitrary
    re-orderings of update events, the resulting row's updatedAt converges to
    the maximum of the delivered timestamps, demonstrating that re-delivered or
    out-of-order events produce no observable duplication).
    """
    table = metadata_table_factory()
    pk = "alpha-bank"
    sk = "sha256:" + "1" * 64
    table.put_item(
        Item={
            "pk": pk,
            "sk": sk,
            "status": "validated",
            "updatedAt": "2000-01-01T00:00:00Z",
        }
    )
    successes: list[str] = []
    for ts in timestamps:
        try:
            update_record_if_newer(
                table,
                pk=pk,
                sk=sk,
                updates={"status": "signed", "lastEventTimestamp": ts},
                event_timestamp=ts,
            )
            successes.append(ts)
        except ConditionalUpdateFailed:
            pass
    item = table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["updatedAt"] == max(successes) == max(timestamps)
    response = table.scan()
    assert len(response["Items"]) == 1


# ---------------------------------------------------------------------------
# [PIPELINE-009.01] — temporal consistency classification
# ---------------------------------------------------------------------------


def _layer(digest: str, last_modified: str | None) -> dict[str, Any]:
    """Build one OSCAL-layer entry as `_temporal_check_layers` consumes it.

    After the universal-`layers` projection reshape, an OSCAL layer's enrichment
    rides in a per-entry `oscal` sub-object; the temporal check reads the
    serialized OSCAL `metadata` from `entry["oscal"]["metadata"]` (camel-spelling
    alias `last-modified`) and the digest from `entry["digest"]`. A `None` value
    models a layer with no in-band timestamp (graceful skip).
    """
    metadata: dict[str, Any] = {}
    if last_modified is not None:
        metadata["last-modified"] = last_modified
    return {"digest": digest, "oscal": {"metadata": metadata}}


@given(
    last_modified=rfc3339_iso(year_min=2024, year_max=2030),
    pushed=rfc3339_iso(year_min=2024, year_max=2030),
)
def test_property_19_temporal_consistency_biconditional(last_modified: str, pushed: str) -> None:
    """**WHEN** the per-OSCAL-layer temporal consistency check runs against each
    layer's in-band `metadata.last-modified` and `ecrPushedAt` (sourced from the
    EventBridge event timestamp, never from any producer-supplied field) **THEN**
    a layer authored strictly after `ecrPushedAt` is a permanent failure
    criterion routed to the DLQ; a layer whose `ecrPushedAt - last-modified`
    exceeds the configured staleness window (default 30 days) sets
    `temporalConsistencyWarning=true` (still accepted); bundles that pass the
    rejection check but trigger the warning are processed through all stages.

    Source: processing-pipeline/spec.md *Authenticated ECR push timestamp captured
    and temporal consistency enforced* ([PIPELINE-009.01] — per-layer
    `metadata.last-modified`).
    """
    from datetime import datetime, timedelta

    failed, warn_stale = _temporal_check_layers(
        layers=[_layer("sha256:" + "a" * 64, last_modified)],
        ecr_pushed_at_iso=pushed,
    )

    last_modified_dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
    pushed_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))

    if last_modified_dt > pushed_dt:
        assert len(failed) == 1
        assert failed[0]["criterion"] == "temporal"
        assert warn_stale is False
    else:
        assert failed == []
        drift = pushed_dt - last_modified_dt
        assert warn_stale == (drift > timedelta(days=30))


def test_property_19_future_dated_layer_permanently_invalid() -> None:
    """**WHEN** the per-OSCAL-layer temporal check runs **THEN** a layer whose
    `metadata.last-modified` is strictly after `ecrPushedAt` yields exactly one
    `temporal` failure criterion and never sets the staleness warning (evidence
    cannot be authored after it was submitted).

    Source: processing-pipeline/spec.md *Authenticated ECR push timestamp captured
    and temporal consistency enforced* ([PIPELINE-009.01] — future-dated layer).
    """
    failed, warn_stale = _temporal_check_layers(
        layers=[_layer("sha256:" + "b" * 64, "2026-06-01T00:00:00Z")],
        ecr_pushed_at_iso="2026-05-24T00:00:00Z",
    )
    assert len(failed) == 1
    assert failed[0]["criterion"] == "temporal"
    assert warn_stale is False


def test_property_19_layer_without_last_modified_skipped_not_failed() -> None:
    """**WHEN** the per-OSCAL-layer temporal check runs **THEN** a layer carrying
    NO in-band `metadata.last-modified` contributes no assertion — it is neither
    a failure criterion nor a staleness warning (graceful skip).

    Source: processing-pipeline/spec.md *Authenticated ECR push timestamp captured
    and temporal consistency enforced* ([PIPELINE-009.01] — missing per-layer timestamp).
    """
    failed, warn_stale = _temporal_check_layers(
        layers=[_layer("sha256:" + "c" * 64, None)],
        ecr_pushed_at_iso="2026-05-24T00:00:00Z",
    )
    assert failed == []
    assert warn_stale is False


def test_property_19_no_oscal_layers_is_a_no_op() -> None:
    """**WHEN** the per-OSCAL-layer temporal check runs over an empty layer set
    **THEN** it is a no-op: no failure criteria, no staleness warning.

    Source: processing-pipeline/spec.md *Authenticated ECR push timestamp captured
    and temporal consistency enforced* ([PIPELINE-009.01] — empty layer set).
    """
    failed, warn_stale = _temporal_check_layers(
        layers=[],
        ecr_pushed_at_iso="2026-05-24T00:00:00Z",
    )
    assert failed == []
    assert warn_stale is False


# ---------------------------------------------------------------------------
# [PIPELINE-010.01] — Authenticated push principal capture across identity shapes
# ---------------------------------------------------------------------------


@given(arn=st.text(min_size=1, max_size=80))
def test_property_20_iam_user_arn_captured_verbatim(arn: str) -> None:
    """**WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge
    event (covering same-account principal, cross-account principal, assumed-role
    session with `sessionContext` and `sourceIdentity`, federated session, absent
    block, or malformed block) with any producer-supplied `producer.identity`
    value in the Config_Blob **THEN** the Lambda captures the full `userIdentity`
    block (not only `arn`) into `authenticatedPushPrincipal` on the initial
    Metadata_Index create ...

    Source: processing-pipeline/spec.md:127-129 ([PIPELINE-010.01] — IAMUser shape: the
    captured arn is exactly what userIdentity declares).
    """
    event = {
        "detail": {
            "userIdentity": {
                "arn": arn,
                "type": "IAMUser",
                "accountId": "111122223333",
                "userName": "deploy-bot",
            }
        }
    }
    principal = extract_authenticated_push_principal(event)
    assert principal.arn == arn
    assert principal.type == "IAMUser"
    assert principal.account_id == "111122223333"
    assert principal.user_name == "deploy-bot"


def test_property_20_assumed_role_session_context_session_issuer_captured() -> None:
    """**WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge
    event (... assumed-role session with `sessionContext` and `sourceIdentity`,
    ...) **THEN** the Lambda captures the full `userIdentity` block (not only
    `arn`) ... preserves `sessionContext` and `sourceIdentity` when present ...

    Source: processing-pipeline/spec.md:127-129 ([PIPELINE-010.01] — AssumedRole shape
    with session context: principal MUST surface session issuer userName).
    """
    event = {
        "detail": {
            "userIdentity": {
                "arn": "arn:aws:sts::444455556666:assumed-role/pipeline/sess-1",
                "type": "AssumedRole",
                "accountId": "444455556666",
                "sessionContext": {
                    "sessionIssuer": {"userName": "pipeline-role"},
                    "attributes": {"mfaAuthenticated": "false"},
                },
                "sourceIdentity": "originating-user@example.com",
            }
        }
    }
    principal = extract_authenticated_push_principal(event)
    assert principal.type == "AssumedRole"
    assert principal.account_id == "444455556666"
    assert principal.user_name == "pipeline-role"
    assert principal.mfa_authenticated is False


@given(mfa_raw=st.sampled_from(["true", "True", "TRUE", "false", "False", "FALSE"]))
def test_property_20_mfa_string_normalized_to_bool(mfa_raw: str) -> None:
    """**WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge
    event ... **THEN** the Lambda captures the full `userIdentity` block (not
    only `arn`) into `authenticatedPushPrincipal` ...

    Source: processing-pipeline/spec.md:127-129 ([PIPELINE-010.01] — `mfaAuthenticated` is
    surfaced from CloudTrail as a string "true"/"false"; the extractor must
    normalize to bool so audit-trail consumers don't have to re-parse).
    """
    event = {
        "detail": {
            "userIdentity": {
                "arn": "arn:aws:iam::111:role/r",
                "type": "AssumedRole",
                "accountId": "111",
                "sessionContext": {"attributes": {"mfaAuthenticated": mfa_raw}},
            }
        }
    }
    principal = extract_authenticated_push_principal(event)
    assert principal.mfa_authenticated == (mfa_raw.lower() == "true")


@given(
    bad_event=st.one_of(
        st.just({}),
        st.just({"detail": None}),
        st.just({"detail": {}}),
        st.just({"detail": {"userIdentity": None}}),
        st.just({"detail": {"userIdentity": "not-a-dict"}}),
        st.just({"detail": {"userIdentity": {"arn": None, "type": None}}}),
        st.just({"detail": {"userIdentity": {"arn": "x"}}}),  # missing type
        st.just({"detail": {"userIdentity": {"type": "IAMUser"}}}),  # missing arn
    )
)
def test_property_20_absent_or_malformed_user_identity_rejected(bad_event: dict[str, Any]) -> None:
    """**WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge
    event (... absent block, or malformed block) ... **THEN** ... rejects the
    bundle as permanently invalid when the `userIdentity` block is absent,
    malformed, or lacks an identifiable principal.

    Source: processing-pipeline/spec.md:127-129 ([PIPELINE-010.01] — negative space).
    """
    with pytest.raises(MissingUserIdentityError):
        extract_authenticated_push_principal(bad_event)


@given(producer_arn=st.text(min_size=1, max_size=60), authed_arn=st.text(min_size=1, max_size=60))
def test_property_20_never_substitutes_producer_identity_for_authenticated_principal(
    producer_arn: str,
    authed_arn: str,
) -> None:
    """**WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge
    event ... with any producer-supplied `producer.identity` value in the
    Config_Blob **THEN** the Lambda ... never substitutes the producer-supplied
    `producer.identity` for `authenticatedPushPrincipal` ...

    Source: processing-pipeline/spec.md:127-129 ([PIPELINE-010.01] — extractor reads ONLY
    userIdentity; even when a producer arn is propagated in the event for
    contrast, it never bleeds into the captured principal).
    """
    event = {
        "detail": {
            "userIdentity": {
                "arn": authed_arn,
                "type": "IAMUser",
                "accountId": "111",
            },
            "configBlobProducer": {"identity": producer_arn},
        }
    }
    principal = extract_authenticated_push_principal(event)
    assert principal.arn == authed_arn
    assert principal.arn != producer_arn or producer_arn == authed_arn
