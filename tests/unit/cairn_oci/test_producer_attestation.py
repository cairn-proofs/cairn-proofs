"""Tests for cairn_oci.producer_attestation — the Producer_Attestation_Referrer claim model."""

from __future__ import annotations

import pytest
from cairn_oci.producer_attestation import (
    INTOTO_STATEMENT_MEDIA_TYPE,
    PRODUCER_ATTESTATION_ARTIFACT_TYPE,
    PRODUCER_PREDICATE_TYPE,
    InvalidStatementError,
    ProducerAttestationReferrer,
    build_producer_statement,
    read_claim_from_statement,
    subject_digest_matches,
)
from pydantic import ValidationError

# A canonical prefixed content Manifest_Digest (sha256: + 64 lowercase hex).
_HEX = "a" * 64
_SUBJECT_DIGEST = f"sha256:{_HEX}"


def _claim(**overrides: object) -> ProducerAttestationReferrer:
    """A minimal valid claim carrying the full opaque identity triple.

    The identity tuple orgId + tenantId + projectId plus artifactKind is the
    irreducible REQUIRED claim identity (INGEST-005); helper keeps the many
    statement-shape tests focused on the Statement structure, not the claim.
    """
    fields: dict[str, object] = {
        "orgId": "org-acme",
        "tenantId": "tnt-alpha-bank",
        "projectId": "prj-controls",
        "artifactKind": "catalog",
    }
    fields.update(overrides)
    return ProducerAttestationReferrer.model_validate(fields)


def test_attestation_artifact_type_constant() -> None:
    assert (
        PRODUCER_ATTESTATION_ARTIFACT_TYPE
        == "application/vnd.cairnproofs.producer-attestation.v1+json"
    )


def test_attestation_exported_from_package() -> None:
    import cairn_oci

    assert cairn_oci.PRODUCER_ATTESTATION_ARTIFACT_TYPE == PRODUCER_ATTESTATION_ARTIFACT_TYPE
    assert cairn_oci.ProducerAttestationReferrer is ProducerAttestationReferrer


def test_attestation_minimal_construction() -> None:
    # The opaque identity triple orgId + tenantId + projectId plus artifactKind
    # is the required claim identity; the rest default.
    ar = ProducerAttestationReferrer(
        orgId="org-acme",
        tenantId="tnt-alpha-bank",
        projectId="prj-controls",
        artifactKind="assessment-results",
    )
    assert ar.org_id == "org-acme"
    assert ar.tenant_id == "tnt-alpha-bank"
    assert ar.project_id == "prj-controls"
    assert ar.artifact_kind == "assessment-results"
    assert ar.controls == []
    assert ar.catalog_or_profile_uuid is None
    assert ar.assessment_period is None
    assert ar.assessor is None
    assert ar.assessment_id is None
    assert ar.supersedes is None
    # The model has no account_id; identity is the opaque tenant_id (D.5).
    assert not hasattr(ar, "account_id")


def test_account_id_is_no_longer_a_model_field() -> None:
    # D.5: the producer-claimed accountId became the opaque tenantId; account_id
    # is not a model field anymore (a stale producer's accountId rides as an
    # extra at most, never as load-bearing identity).
    assert "account_id" not in ProducerAttestationReferrer.model_fields
    assert "org_id" in ProducerAttestationReferrer.model_fields
    assert "tenant_id" in ProducerAttestationReferrer.model_fields


@pytest.mark.parametrize(
    "missing",
    [
        {"tenantId": "t", "projectId": "p", "artifactKind": "catalog"},  # no orgId
        {"orgId": "o", "projectId": "p", "artifactKind": "catalog"},  # no tenantId
        {"orgId": "o", "tenantId": "t", "artifactKind": "catalog"},  # no projectId
        {"orgId": "o", "tenantId": "t", "projectId": "p"},  # no artifactKind
    ],
)
def test_attestation_missing_required_identity_coordinate_raises(missing: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ProducerAttestationReferrer.model_validate(missing)


def test_attestation_full_claim_round_trip() -> None:
    payload = {
        "orgId": "org-acme",
        "tenantId": "tnt-alpha-bank",
        "projectId": "prj-controls",
        "artifactKind": "assessment-results",
        "controls": ["ac-2", "ac-2.1", "5.1"],
        "catalogOrProfileUuid": "11111111-1111-4111-8111-111111111111",
        "assessmentPeriod": "2026Q1",
        "assessor": "auditor-quinn",
        "assessmentId": "asmt-2026-001",
        "supersedes": "catalog--_ref--0123456789ab",
    }
    ar = ProducerAttestationReferrer.model_validate(payload)
    assert ar.org_id == "org-acme"
    assert ar.tenant_id == "tnt-alpha-bank"
    assert ar.project_id == "prj-controls"
    assert ar.artifact_kind == "assessment-results"
    assert ar.controls == ["ac-2", "ac-2.1", "5.1"]
    assert ar.catalog_or_profile_uuid == "11111111-1111-4111-8111-111111111111"
    assert ar.assessment_period == "2026Q1"
    assert ar.assessor == "auditor-quinn"
    assert ar.assessment_id == "asmt-2026-001"
    assert ar.supersedes == "catalog--_ref--0123456789ab"
    # By-alias re-serialization round-trips back to the camelCase wire payload.
    assert ar.model_dump(by_alias=True, exclude_none=True) == payload


def test_attestation_allows_extra_vendor_fields() -> None:
    ar = _claim(vendorField="ok")
    assert ar.vendorField == "ok"


def test_attestation_populate_by_name() -> None:
    # populate_by_name: snake_case attribute names construct it too.
    ar = ProducerAttestationReferrer(
        org_id="org-acme",
        tenant_id="tnt-alpha-bank",
        project_id="prj-controls",
        artifact_kind="catalog",
    )
    assert ar.org_id == "org-acme"
    assert ar.tenant_id == "tnt-alpha-bank"
    assert ar.project_id == "prj-controls"


def test_attestation_identity_tuple_carries_no_display_names() -> None:
    # The identity tuple is opaque ids only; a stale producer that still sends an
    # accountId / display name has it tolerated as an extra, never promoted to a
    # load-bearing identity coordinate.
    ar = ProducerAttestationReferrer.model_validate(
        {
            "orgId": "org-acme",
            "tenantId": "tnt-alpha-bank",
            "projectId": "prj-controls",
            "artifactKind": "catalog",
            "accountId": "111122223333",
        }
    )
    assert not hasattr(ar, "account_id") or ar.tenant_id == "tnt-alpha-bank"
    # accountId rides as an extra, not as identity.
    assert ar.tenant_id == "tnt-alpha-bank"


# --- in-toto Statement wrapping (build / read round-trip) --------------------


def test_intoto_constants_exported_from_package() -> None:
    import cairn_oci

    assert cairn_oci.PRODUCER_PREDICATE_TYPE == PRODUCER_PREDICATE_TYPE
    assert cairn_oci.INTOTO_STATEMENT_MEDIA_TYPE == INTOTO_STATEMENT_MEDIA_TYPE
    assert cairn_oci.build_producer_statement is build_producer_statement
    assert cairn_oci.read_claim_from_statement is read_claim_from_statement
    assert cairn_oci.subject_digest_matches is subject_digest_matches
    assert cairn_oci.InvalidStatementError is InvalidStatementError


def test_intoto_constant_values() -> None:
    assert PRODUCER_PREDICATE_TYPE == "https://cairnproofs.dev/ns/attestation/producer/v1"
    assert INTOTO_STATEMENT_MEDIA_TYPE == "application/vnd.in-toto+json"


def test_build_producer_statement_shape() -> None:
    claim = _claim(artifactKind="assessment-results")
    stmt = build_producer_statement(_SUBJECT_DIGEST, claim)
    assert stmt["_type"] == "https://in-toto.io/Statement/v1"
    assert stmt["predicateType"] == PRODUCER_PREDICATE_TYPE
    # subject is a single-element array carrying a bare-hex DigestSet.
    assert stmt["subject"] == [{"digest": {"sha256": _HEX}}]
    # The opaque identity triple rides on the predicate; display names never do.
    assert stmt["predicate"]["orgId"] == "org-acme"
    assert stmt["predicate"]["tenantId"] == "tnt-alpha-bank"
    assert stmt["predicate"]["projectId"] == "prj-controls"
    assert stmt["predicate"]["artifactKind"] == "assessment-results"
    assert "accountId" not in stmt["predicate"]


def test_build_read_round_trip_preserves_all_claim_fields() -> None:
    payload = {
        "orgId": "org-acme",
        "tenantId": "tnt-alpha-bank",
        "projectId": "prj-controls",
        "artifactKind": "assessment-results",
        "controls": ["ac-2", "ac-2.1", "5.1"],
        "catalogOrProfileUuid": "11111111-1111-4111-8111-111111111111",
        "assessmentPeriod": "2026Q1",
        "assessor": "auditor-quinn",
        "assessmentId": "asmt-2026-001",
        "supersedes": "catalog--_ref--0123456789ab",
    }
    claim = ProducerAttestationReferrer.model_validate(payload)
    stmt = build_producer_statement(_SUBJECT_DIGEST, claim)
    # predicate is exactly today's camelCase wire payload.
    assert stmt["predicate"] == payload
    recovered = read_claim_from_statement(stmt)
    assert recovered.model_dump(by_alias=True, exclude_none=True) == payload


def test_build_read_round_trip_preserves_vendor_extras() -> None:
    claim = _claim(vendorField="ok")
    stmt = build_producer_statement(_SUBJECT_DIGEST, claim)
    recovered = read_claim_from_statement(stmt)
    assert recovered.vendorField == "ok"


def test_read_claim_rejects_predicate_type_mismatch() -> None:
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["predicateType"] = "https://cairnproofs.dev/ns/attestation/signoff/v1"
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_zero_subject() -> None:
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["subject"] = []
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_multi_subject() -> None:
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["subject"] = [
        {"digest": {"sha256": _HEX}},
        {"digest": {"sha256": "b" * 64}},
    ]
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_non_object_subject_element() -> None:
    # A producer-supplied single-element subject whose element is NOT an object
    # (e.g. a bare digest string) must raise InvalidStatementError here, so a
    # downstream subject[0].get("digest") in the validate handler cannot crash
    # into the transient retry path instead of the permanent-failure → DLQ path.
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["subject"] = [f"sha256:{_HEX}"]
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_non_list_subject() -> None:
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["subject"] = {"digest": {"sha256": _HEX}}
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


# --- dual-subject normalization / comparison --------------------------------


def test_subject_matches_correct_pair() -> None:
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha256": _HEX}) is True


def test_subject_matches_tolerates_extra_algorithms() -> None:
    # A DigestSet carrying additional algorithms is tolerated: the shared
    # algorithm must agree, NOT the whole map.
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha256": _HEX, "sha512": "f" * 128}) is True


def test_subject_rejects_algorithm_mismatch() -> None:
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha256": "b" * 64}) is False


def test_subject_rejects_missing_shared_algorithm() -> None:
    # Only a non-shared algorithm present → no shared algorithm → False.
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha512": "f" * 128}) is False


def test_subject_rejects_trailing_newline_contaminated_digest() -> None:
    # _ID_RE trailing-newline gotcha: parse_digest's `$` anchor matches before a
    # trailing \n, so a contaminated digest must be rejected up front.
    assert subject_digest_matches(f"{_SUBJECT_DIGEST}\n", {"sha256": _HEX}) is False


def test_subject_rejects_non_string_digest() -> None:
    assert subject_digest_matches(None, {"sha256": _HEX}) is False  # type: ignore[arg-type]


def test_subject_rejects_non_dict_digest_set() -> None:
    assert subject_digest_matches(_SUBJECT_DIGEST, None) is False  # type: ignore[arg-type]


def test_subject_rejects_extra_algorithms_when_shared_algorithm_differs() -> None:
    # The mirror of test_subject_matches_tolerates_extra_algorithms: extra
    # algorithms are tolerated, but the SHARED algorithm must agree. Here sha256
    # disagrees while sha512 is present — the disagreement on the shared key must
    # still reject (map equality is not the rule, but shared-key agreement is).
    assert (
        subject_digest_matches(_SUBJECT_DIGEST, {"sha256": "b" * 64, "sha512": "f" * 128}) is False
    )


def test_subject_rejects_empty_digest_set() -> None:
    # A DigestSet with no algorithms at all → no shared algorithm → no match.
    assert subject_digest_matches(_SUBJECT_DIGEST, {}) is False


def test_subject_rejects_leading_or_trailing_whitespace_digest() -> None:
    # The strip()-equality guard rejects any surrounding whitespace before
    # parse_digest's `$`-anchored regex can be coaxed into a false match.
    assert subject_digest_matches(f" {_SUBJECT_DIGEST}", {"sha256": _HEX}) is False
    assert subject_digest_matches(f"{_SUBJECT_DIGEST} ", {"sha256": _HEX}) is False


def test_subject_no_false_match_across_two_distinct_digests() -> None:
    # Two genuinely different content digests must never cross-match: the DigestSet
    # of digest A is rejected against the OCI prefixed form of digest B.
    other_hex = "b" * 64
    assert subject_digest_matches(f"sha256:{other_hex}", {"sha256": _HEX}) is False
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha256": other_hex}) is False


def test_subject_rejects_non_string_hex_value_in_digest_set() -> None:
    # A DigestSet whose sha256 value is not the bare hex string (e.g. None) cannot
    # equal the parsed bare hex → no match, no raise.
    assert subject_digest_matches(_SUBJECT_DIGEST, {"sha256": None}) is False  # type: ignore[dict-item]


# --- read_claim_from_statement edge cases (predicate shape / schema) ----------


def test_read_claim_predicate_extra_fields_round_trip_through_read_boundary() -> None:
    # A producer's vendor extras in the predicate must survive the read boundary
    # directly (extra="allow"), not only via the build path. Construct a raw
    # Statement dict (not built by build_producer_statement) to prove the unwrap.
    stmt = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"digest": {"sha256": _HEX}}],
        "predicateType": PRODUCER_PREDICATE_TYPE,
        "predicate": {
            "orgId": "org-acme",
            "tenantId": "tnt-alpha-bank",
            "projectId": "prj-controls",
            "artifactKind": "catalog",
            "vendorField": "ok",
            "n": 7,
        },
    }
    claim = read_claim_from_statement(stmt)
    assert claim.org_id == "org-acme"
    assert claim.tenant_id == "tnt-alpha-bank"
    assert claim.project_id == "prj-controls"
    assert claim.vendorField == "ok"
    assert claim.n == 7


def test_read_claim_predicate_missing_required_field_raises_validation_error() -> None:
    # A structurally-valid Statement (single subject, correct predicateType) whose
    # predicate is missing a REQUIRED claim field raises pydantic ValidationError —
    # NOT InvalidStatementError. The handler relies on this distinction to classify
    # it under the attestation_schema (field-loc) branch rather than the
    # statement-structure branch.
    stmt = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"digest": {"sha256": _HEX}}],
        "predicateType": PRODUCER_PREDICATE_TYPE,
        "predicate": {"tenantId": "t", "projectId": "p", "artifactKind": "catalog"},  # no orgId
    }
    with pytest.raises(ValidationError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_none_predicate() -> None:
    # predicate present but null → not an object → InvalidStatementError.
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    stmt["predicate"] = None
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)


def test_read_claim_rejects_missing_predicate_key() -> None:
    # predicate key absent entirely → InvalidStatementError (not a KeyError leak).
    stmt = build_producer_statement(_SUBJECT_DIGEST, _claim())
    del stmt["predicate"]
    with pytest.raises(InvalidStatementError):
        read_claim_from_statement(stmt)
