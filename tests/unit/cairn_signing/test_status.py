"""Tests for cairn_signing.status (multi-signature status aggregation, [SIGN-005]).

These assertions are anchored on the canonical contract in
openspec/specs/signing/spec.md *Signature Status Reporting on Retrieval*:
signed / unsigned / partial / revoked semantics and per-signature
signatureDetails (profile ARN, profile name, status, signing timestamp,
TSA endpoint), with per-profile status independence ([SIGN-005.05]).
"""

from __future__ import annotations

from cairn_signing.profile import PIPELINE_PROFILE_NAME, REVIEWER_PROFILE_NAME
from cairn_signing.status import (
    KNOWN_SIGNING_PROFILE_NAMES,
    BundleTrust,
    SignatureResult,
    SignatureRole,
    SignatureStatus,
    SigningStatus,
    aggregate_status,
    bundle_trust_from_projection,
    classify_bundle_trust,
)

PIPELINE_ARN = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-pipeline-signing"
REVIEWER_ARN = "arn:aws:signer:us-east-2:111122223333:/signing-profiles/artifact-reviewer-signoff"


def _pipeline(status: SignatureStatus = SignatureStatus.VALID) -> SignatureResult:
    return SignatureResult(
        profile_arn=PIPELINE_ARN,
        profile_name=PIPELINE_PROFILE_NAME,
        status=status,
        signing_timestamp="2026-06-05T12:00:00Z",
        tsa_endpoint=None,
        timestamp_provider="system-clock",
    )


def _reviewer(status: SignatureStatus = SignatureStatus.VALID) -> SignatureResult:
    return SignatureResult(
        profile_arn=REVIEWER_ARN,
        profile_name=REVIEWER_PROFILE_NAME,
        status=status,
        signing_timestamp="2026-06-05T13:30:00Z",
        tsa_endpoint=None,
        timestamp_provider="system-clock",
    )


# --- Overall status semantics ([SIGN-005]) -------------------------------------


def test_pipeline_and_reviewer_both_valid_yields_signed() -> None:
    # Coexisting pipeline + reviewer sigs, all valid → signed.
    agg = aggregate_status([_pipeline(), _reviewer()])
    assert agg.signing_status is SigningStatus.SIGNED
    assert len(agg.signature_details) == 2
    assert all(d.status is SignatureStatus.VALID for d in agg.signature_details)


def test_one_valid_one_revoked_yields_partial() -> None:
    # >=1 valid and >=1 revoked → partial (mixed).
    agg = aggregate_status([_pipeline(SignatureStatus.VALID), _reviewer(SignatureStatus.REVOKED)])
    assert agg.signing_status is SigningStatus.PARTIAL


def test_all_present_revoked_yields_revoked() -> None:
    # Every present signature revoked → revoked.
    agg = aggregate_status([_pipeline(SignatureStatus.REVOKED), _reviewer(SignatureStatus.REVOKED)])
    assert agg.signing_status is SigningStatus.REVOKED
    assert all(d.status is SignatureStatus.REVOKED for d in agg.signature_details)


def test_no_signatures_yields_unsigned_with_empty_details() -> None:
    # No signatures present → unsigned, empty signatureDetails.
    agg = aggregate_status([])
    assert agg.signing_status is SigningStatus.UNSIGNED
    assert agg.signature_details == ()


def test_single_valid_signature_yields_signed() -> None:
    agg = aggregate_status([_pipeline()])
    assert agg.signing_status is SigningStatus.SIGNED


def test_single_revoked_signature_yields_revoked() -> None:
    agg = aggregate_status([_pipeline(SignatureStatus.REVOKED)])
    assert agg.signing_status is SigningStatus.REVOKED


def test_three_present_all_valid_yields_signed() -> None:
    # >2 coexisting valid signatures (pipeline + two reviewer sign-offs) still
    # rolls up to signed; the spec floor is "at least 10 per bundle" so the
    # arithmetic must not be limited to a pair.
    a = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-a",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="2026-06-05T13:30:00Z",
    )
    b = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-b",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="2026-06-05T14:00:00Z",
    )
    agg = aggregate_status([_pipeline(), a, b])
    assert agg.signing_status is SigningStatus.SIGNED
    assert len(agg.signature_details) == 3


def test_one_revoked_among_three_yields_partial() -> None:
    # Spec scenario "Mixed valid and revoked": a single revoked among several
    # valid sigs is partial (not revoked), and surviving entries stay valid.
    a = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-a",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="2026-06-05T13:30:00Z",
    )
    revoked = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-b",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.REVOKED,
        signing_timestamp="2026-06-05T14:00:00Z",
    )
    agg = aggregate_status([_pipeline(), a, revoked])
    assert agg.signing_status is SigningStatus.PARTIAL
    by_arn = {d.profile_arn: d.status for d in agg.signature_details}
    assert by_arn[a.profile_arn] is SignatureStatus.VALID
    assert by_arn[PIPELINE_ARN] is SignatureStatus.VALID
    assert by_arn[revoked.profile_arn] is SignatureStatus.REVOKED


# --- Per-profile independence ([SIGN-005.05]) ----------------------------------


def test_revoking_one_profile_does_not_change_other_profiles_status() -> None:
    # Profiles A (pipeline) and B (reviewer) both valid; revoke only A.
    # B's recorded status must remain valid; overall recomputes to partial.
    both_valid = aggregate_status([_pipeline(), _reviewer()])
    assert both_valid.signing_status is SigningStatus.SIGNED

    after_revoke_a = aggregate_status(
        [_pipeline(SignatureStatus.REVOKED), _reviewer(SignatureStatus.VALID)]
    )
    assert after_revoke_a.signing_status is SigningStatus.PARTIAL

    by_profile = {d.profile_name: d for d in after_revoke_a.signature_details}
    assert by_profile[PIPELINE_PROFILE_NAME].status is SignatureStatus.REVOKED
    assert by_profile[REVIEWER_PROFILE_NAME].status is SignatureStatus.VALID

    # B's entry is byte-for-byte unchanged from the all-valid aggregation.
    both_valid_by_profile = {d.profile_name: d for d in both_valid.signature_details}
    reviewer_before = both_valid_by_profile[REVIEWER_PROFILE_NAME]
    assert by_profile[REVIEWER_PROFILE_NAME] == reviewer_before


def test_independence_holds_for_two_reviewer_profiles_via_distinct_arns() -> None:
    # Two distinct sign-offs under the reviewer profile name but different
    # signer ARNs: revoking one leaves the other's status untouched.
    a = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-a",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.REVOKED,
        signing_timestamp="2026-06-05T13:30:00Z",
    )
    b = SignatureResult(
        profile_arn=REVIEWER_ARN + "/principal-b",
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="2026-06-05T14:00:00Z",
    )
    agg = aggregate_status([_pipeline(), a, b])
    by_arn = {d.profile_arn: d.status for d in agg.signature_details}
    assert by_arn[a.profile_arn] is SignatureStatus.REVOKED
    assert by_arn[b.profile_arn] is SignatureStatus.VALID
    assert agg.signing_status is SigningStatus.PARTIAL


# --- signatureDetails content + ordering ------------------------------------


def test_signature_details_carry_full_per_signature_provenance() -> None:
    detail = aggregate_status([_pipeline()]).signature_details[0]
    assert detail.profile_arn == PIPELINE_ARN
    assert detail.profile_name == PIPELINE_PROFILE_NAME
    assert detail.status is SignatureStatus.VALID
    assert detail.signing_timestamp == "2026-06-05T12:00:00Z"
    assert detail.tsa_endpoint is None
    assert detail.timestamp_provider == "system-clock"


def test_signature_details_records_rfc3161_tsa_endpoint() -> None:
    sig = SignatureResult(
        profile_arn=PIPELINE_ARN,
        profile_name=PIPELINE_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="2026-06-05T12:00:00Z",
        tsa_endpoint="https://tsa.example.com",
        timestamp_provider="rfc3161",
    )
    detail = aggregate_status([sig]).signature_details[0]
    assert detail.tsa_endpoint == "https://tsa.example.com"
    assert detail.timestamp_provider == "rfc3161"


def test_revoked_signature_detail_carries_full_provenance() -> None:
    # A revoked entry must still surface every [SIGN-005] field — revocation flips
    # only `status`, it does not blank the profile ARN / name / timestamp / TSA
    # / provider an reviewer needs to attribute the (now-revoked) signature.
    revoked = SignatureResult(
        profile_arn=REVIEWER_ARN,
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.REVOKED,
        signing_timestamp="2026-06-05T13:30:00Z",
        tsa_endpoint="https://tsa.example.com",
        timestamp_provider="rfc3161",
    )
    detail = aggregate_status([revoked]).signature_details[0]
    assert detail.profile_arn == REVIEWER_ARN
    assert detail.profile_name == REVIEWER_PROFILE_NAME
    assert detail.status is SignatureStatus.REVOKED
    assert detail.signing_timestamp == "2026-06-05T13:30:00Z"
    assert detail.tsa_endpoint == "https://tsa.example.com"
    assert detail.timestamp_provider == "rfc3161"


def test_signature_details_preserves_input_order() -> None:
    agg = aggregate_status([_reviewer(), _pipeline()])
    assert [d.profile_name for d in agg.signature_details] == [
        REVIEWER_PROFILE_NAME,
        PIPELINE_PROFILE_NAME,
    ]


def test_aggregation_is_pure_does_not_mutate_input() -> None:
    results = [_pipeline(), _reviewer(SignatureStatus.REVOKED)]
    snapshot = list(results)
    aggregate_status(results)
    assert results == snapshot


# --- Known profile registry ([SIGN-003] reviewer wiring) ------------------------


def test_known_profiles_include_pipeline_and_reviewer() -> None:
    assert PIPELINE_PROFILE_NAME in KNOWN_SIGNING_PROFILE_NAMES
    assert REVIEWER_PROFILE_NAME in KNOWN_SIGNING_PROFILE_NAMES


def test_status_module_has_no_boto3_dependency() -> None:
    # Aggregation must be index-independent / side-effect free: no AWS SDK.
    import cairn_signing.status as status_mod

    assert not hasattr(status_mod, "boto3")
    assert "boto3" not in status_mod.__dict__


# --- Two-signature trust -----------------------------------------------------


def _content(status: SignatureStatus = SignatureStatus.VALID) -> SignatureResult:
    return SignatureResult(
        profile_arn=None,
        profile_name=PIPELINE_PROFILE_NAME,
        status=status,
        signing_timestamp="",
        role=SignatureRole.CONTENT,
    )


def _attestation(status: SignatureStatus = SignatureStatus.VALID) -> SignatureResult:
    return SignatureResult(
        profile_arn=None,
        profile_name=PIPELINE_PROFILE_NAME,
        status=status,
        signing_timestamp="",
        role=SignatureRole.ATTESTATION,
    )


def test_both_signatures_valid_is_fully_trusted() -> None:
    verdict = classify_bundle_trust([_content(), _attestation()])
    assert verdict.verdict is BundleTrust.FULLY_TRUSTED
    assert verdict.content_signed is True
    assert verdict.attestation_signed is True
    assert verdict.missing_roles == ()


def test_content_only_signed_is_partial_not_fully_trusted() -> None:
    # Content signed, attestation absent → partial.
    verdict = classify_bundle_trust([_content()])
    assert verdict.verdict is BundleTrust.PARTIAL
    assert verdict.verdict is not BundleTrust.FULLY_TRUSTED
    assert verdict.content_signed is True
    assert verdict.attestation_signed is False
    assert verdict.missing_roles == (SignatureRole.ATTESTATION,)


def test_attestation_only_signed_is_partial() -> None:
    verdict = classify_bundle_trust([_attestation()])
    assert verdict.verdict is BundleTrust.PARTIAL
    assert verdict.content_signed is False
    assert verdict.attestation_signed is True
    assert verdict.missing_roles == (SignatureRole.CONTENT,)


def test_no_signatures_is_unsigned() -> None:
    verdict = classify_bundle_trust([])
    assert verdict.verdict is BundleTrust.UNSIGNED
    assert verdict.missing_roles == (SignatureRole.CONTENT, SignatureRole.ATTESTATION)


def test_revoked_signature_does_not_satisfy_its_role() -> None:
    # A content signature that is REVOKED leaves the content role unsatisfied;
    # with a valid attestation that is partial, never fully trusted.
    verdict = classify_bundle_trust([_content(SignatureStatus.REVOKED), _attestation()])
    assert verdict.verdict is BundleTrust.PARTIAL
    assert verdict.content_signed is False
    assert verdict.attestation_signed is True


def test_both_revoked_is_unsigned_for_two_signature_trust() -> None:
    # Neither required role is satisfied by a valid signature → unsigned trust
    # (the valid/revoked aggregate_status separately reports `revoked`).
    verdict = classify_bundle_trust(
        [_content(SignatureStatus.REVOKED), _attestation(SignatureStatus.REVOKED)]
    )
    assert verdict.verdict is BundleTrust.UNSIGNED


def test_unknown_role_signature_never_satisfies_a_required_role() -> None:
    # An reviewer sign-off (default role UNKNOWN) is not a content/attestation
    # signature, so it does not, on its own, make the bundle fully trusted.
    reviewer = SignatureResult(
        profile_arn=REVIEWER_ARN,
        profile_name=REVIEWER_PROFILE_NAME,
        status=SignatureStatus.VALID,
        signing_timestamp="",
    )
    verdict = classify_bundle_trust([reviewer])
    assert verdict.verdict is BundleTrust.UNSIGNED
    assert reviewer.role is SignatureRole.UNKNOWN


def test_classify_bundle_trust_is_order_independent() -> None:
    a = classify_bundle_trust([_content(), _attestation()])
    b = classify_bundle_trust([_attestation(), _content()])
    assert a == b


def test_aggregate_status_carries_role_through_to_detail() -> None:
    agg = aggregate_status([_content(), _attestation()])
    roles = {d.role for d in agg.signature_details}
    assert roles == {SignatureRole.CONTENT, SignatureRole.ATTESTATION}


# --- Projection-backed trust mapping (cairn query / TUI read the index) ------


def test_projection_signed_maps_to_fully_trusted() -> None:
    # The update-metadata writer sets signingStatus=signed only after the
    # both-signatures promotion gate, so a projected `signed` is fully trusted.
    assert bundle_trust_from_projection("signed") is BundleTrust.FULLY_TRUSTED


def test_projection_unsigned_maps_to_unsigned() -> None:
    assert bundle_trust_from_projection("unsigned") is BundleTrust.UNSIGNED


def test_projection_absent_maps_to_unsigned() -> None:
    assert bundle_trust_from_projection(None) is BundleTrust.UNSIGNED
    assert bundle_trust_from_projection("") is BundleTrust.UNSIGNED


def test_projection_pending_maps_to_partial_not_fully_trusted() -> None:
    # A not-yet-fully-signed transient state must never read as fully trusted.
    verdict = bundle_trust_from_projection("pending")
    assert verdict is BundleTrust.PARTIAL
    assert verdict is not BundleTrust.FULLY_TRUSTED
