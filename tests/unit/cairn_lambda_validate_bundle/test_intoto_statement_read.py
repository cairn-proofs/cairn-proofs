"""Validate read-path tests for the in-toto Statement claim envelope.

Covers the in-toto Statement unwrap behavior:

- happy-path unwrap: a valid in-toto Statement → the claim is read from the
  Statement's `predicate` and the pipeline proceeds;
- `predicateType` mismatch → permanent validation failure / DLQ;
- Statement-subject vs OCI-envelope-subject divergence → permanent failure / DLQ;
- zero-subject and >1-subject Statement arrays → permanent failure / DLQ;
- malformed (non-Statement / bad JSON) claim layer → the EXISTING
  malformed-claim → DLQ branch;
- regression: the unwrapped claim equals the legacy flat ProducerAttestationReferrer
  shape so the downstream Property-4 cross-check + Metadata_Index projection are
  unchanged.

These exercise the real handler against the FakeEcr / EventRecorder / FakeSqs
edges (conftest), the same mocks the rest of the validate-bundle suite uses.
"""

from __future__ import annotations

import json

from cairn_oci import (
    INTOTO_STATEMENT_MEDIA_TYPE,
    PRODUCER_PREDICATE_TYPE,
    ProducerAttestationReferrer,
)

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
    producer_statement_bytes,
    submit,
)


def _failure_detail(handler):
    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1, f"expected exactly one validation-failed event, got {len(failed)}"
    return failed[0]


def _dlq_bodies(handler):
    return handler._test_fake_sqs.bodies()


# --------------------------------------------------------------------------- #
# Happy-path unwrap — valid Statement → claim read, pipeline proceeds.
# --------------------------------------------------------------------------- #


def test_valid_statement_unwraps_and_pipeline_proceeds(reload_handler, lambda_context) -> None:
    """A valid in-toto Statement layer unwraps to the producer claim, the bundle
    validates, and a single artifact.validated event carries the projected claim."""
    handler = reload_handler
    bundle = build_bundle()
    attestation, event = submit(handler, bundle=bundle)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    # The layer is a real in-toto Statement (format announced via mediaType).
    claim_layer = attestation.manifest_dict["layers"][0]
    assert claim_layer["mediaType"] == INTOTO_STATEMENT_MEDIA_TYPE
    # The claim was read from the Statement predicate and projected verbatim.
    assert payload["attestation"]["projectId"] == "alpha-bank"
    assert payload["attestation"]["artifactKind"] == "assessment-results"
    assert payload["attestation"]["assessmentPeriod"] == "2026-q2"
    assert payload["attestation"]["controls"] == ["ac-1", "ac-2"]


# --------------------------------------------------------------------------- #
# D5 — predicateType mismatch is a permanent failure routed to the DLQ.
# --------------------------------------------------------------------------- #


def test_wrong_predicate_type_is_permanent_failure_to_dlq(reload_handler, lambda_context) -> None:
    """A Statement whose predicateType is not the Cairn URI is rejected as a
    permanent failure and routed to the DLQ — never resolved/signed/indexed."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        predicate_type="https://in-toto.io/attestation/some-other/v1",
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    # The criterion message names the predicateType disagreement.
    assert any(PRODUCER_PREDICATE_TYPE in c["message"] for c in detail["failedCriteria"])
    # Permanent failures deposit exactly one reason-bearing DLQ rejection ([PIPELINE-006]).
    assert len(_dlq_bodies(handler)) == 1


# --------------------------------------------------------------------------- #
# D4 — Statement subject must AGREE with the OCI envelope subject.
# --------------------------------------------------------------------------- #


def test_statement_subject_divergence_is_permanent_failure_to_dlq(
    reload_handler, lambda_context
) -> None:
    """The OCI envelope subject is the sole anti-hijack authority; a Statement
    whose inner subject digest diverges from it is a permanent failure to the DLQ
    The envelope subject (content_digest) still resolves the real bundle, so
    the divergence — not a scope/resolve failure — is what rejects it."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    diverging = "sha256:" + ("b" * 64)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,  # OCI envelope subject = real content
        statement_subject_digest=diverging,  # Statement inner subject = a lie
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "statement_subject_divergence"
    assert any(c["criterion"] == "statement_subject" for c in detail["failedCriteria"])
    assert len(_dlq_bodies(handler)) == 1


def test_statement_subject_agreeing_with_extra_algorithm_proceeds(
    reload_handler, lambda_context
) -> None:
    """A Statement DigestSet carrying ADDITIONAL algorithms (beyond the shared
    sha256) still matches by algorithm key — map equality is NOT required."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    bare = bundle.manifest_digest.split(":", 1)[-1]
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        statement_subjects=[
            {"digest": {"sha256": bare, "sha512": "f" * 128}},
        ],
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    payload = handler.lambda_handler(event, lambda_context)
    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["attestation"]["projectId"] == "alpha-bank"


# --------------------------------------------------------------------------- #
# Not-exactly-one subject — zero and >1 — is a permanent failure.
# --------------------------------------------------------------------------- #


def test_zero_subject_statement_is_permanent_failure(reload_handler, lambda_context) -> None:
    """A Statement with an empty subject array cannot bind a single content
    manifest → permanent failure (raised by read_claim_from_statement)."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        statement_subjects=[],
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    assert len(_dlq_bodies(handler)) == 1


def test_multi_subject_statement_is_permanent_failure(reload_handler, lambda_context) -> None:
    """A Statement with more than one subject entry → permanent failure (raised by
    read_claim_from_statement before any subject is trusted)."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    bare = bundle.manifest_digest.split(":", 1)[-1]
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        statement_subjects=[
            {"digest": {"sha256": bare}},
            {"digest": {"sha256": "c" * 64}},
        ],
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    assert len(_dlq_bodies(handler)) == 1


# --------------------------------------------------------------------------- #
# Malformed claim layer (non-Statement / bad JSON) → existing DLQ branch.
# --------------------------------------------------------------------------- #


def test_non_json_claim_layer_routes_to_existing_dlq_branch(reload_handler, lambda_context) -> None:
    """A claim layer that is not valid JSON routes through the SAME fail-closed
    malformed-claim → DLQ branch (no new unbounded parse surface)."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        wrap_statement=False,
        layer_bytes=b"this is not json {{{",
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    assert len(_dlq_bodies(handler)) == 1


def test_non_object_statement_layer_routes_to_dlq_branch(reload_handler, lambda_context) -> None:
    """A claim layer that is valid JSON but not a Statement OBJECT (e.g. a bare
    array) is rejected through the malformed-claim → DLQ branch."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        wrap_statement=False,
        layer_bytes=json.dumps(["not", "a", "statement"]).encode("utf-8"),
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    assert len(_dlq_bodies(handler)) == 1


# --------------------------------------------------------------------------- #
# Predicate fails the ProducerAttestationReferrer schema — the distinct ValidationError
# classification branch (attestation_schema, field-loc) vs. the structural one.
# --------------------------------------------------------------------------- #


def test_predicate_missing_required_field_routes_to_dlq_via_schema_branch(
    reload_handler, lambda_context
) -> None:
    """A structurally-valid Statement (single subject, Cairn predicateType) whose
    `predicate` is missing a REQUIRED claim field (projectId) is a permanent
    failure routed to the DLQ. This exercises the handler's pydantic
    ValidationError classification branch (criterion `attestation_schema`,
    distinct from the InvalidStatementError structure branch) — a path the
    structure-only negatives never reach."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    # Build the Statement layer by hand (wrap_statement=False) so a predicate
    # missing the required projectId reaches the handler — the ProducerAttestationReferrer
    # builder inside build_attestation would otherwise reject the bad claim at
    # fixture-build time, before it could exercise the handler's read boundary.
    bare = bundle.manifest_digest.split(":", 1)[-1]
    bad_statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"digest": {"sha256": bare}}],
        "predicateType": PRODUCER_PREDICATE_TYPE,
        "predicate": {"artifactKind": "catalog"},  # missing required projectId
    }
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        wrap_statement=False,
        layer_bytes=json.dumps(bad_statement).encode("utf-8"),
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    # The ValidationError branch tags the criterion attestation_schema and carries
    # a field loc under the attestation. prefix (NOT the structure branch's flat
    # attestation.statement field).
    assert any(c["criterion"] == "attestation_schema" for c in detail["failedCriteria"])
    assert any(c["field"].startswith("attestation.") for c in detail["failedCriteria"])
    assert len(_dlq_bodies(handler)) == 1


# --------------------------------------------------------------------------- #
# D4 — a subject entry missing its `digest` map is a divergence (not a crash).
# --------------------------------------------------------------------------- #


def test_non_object_subject_element_is_permanent_failure(reload_handler, lambda_context) -> None:
    """A single-element subject whose element is a bare string (not an object)
    must be a PERMANENT failure via the malformed-claim → DLQ branch, not an
    AttributeError on subject[0].get(...) that falls through to the transient
    retry path (code-review High finding)."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    bare = bundle.manifest_digest.split(":", 1)[-1]
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        statement_subjects=[f"sha256:{bare}"],
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "malformed_attestation_claim"
    assert len(_dlq_bodies(handler)) == 1


def test_subject_entry_without_digest_key_is_divergence_failure(
    reload_handler, lambda_context
) -> None:
    """A single-element subject array whose lone entry has NO `digest` key passes
    the exactly-one-subject gate but yields a None DigestSet at the D4 check. The
    handler must treat that as a subject divergence (the envelope subject is the
    sole authority and nothing agreed with it) — a permanent failure to the DLQ,
    NOT an unhandled KeyError."""
    handler = reload_handler
    bundle = build_bundle()
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        statement_subjects=[{"name": "no-digest-here"}],
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED
    detail = _failure_detail(handler)
    assert detail["errorKind"] == "statement_subject_divergence"
    assert any(c["criterion"] == "statement_subject" for c in detail["failedCriteria"])
    assert len(_dlq_bodies(handler)) == 1


# --------------------------------------------------------------------------- #
# Regression — the unwrapped claim equals the legacy flat ProducerAttestationReferrer.
# --------------------------------------------------------------------------- #


def test_unwrapped_claim_equals_legacy_flat_shape(reload_handler, lambda_context) -> None:
    """The claim read from the Statement predicate is byte-for-byte the legacy
    flat ProducerAttestationReferrer projection — so the Property-4 cross-check and the
    Metadata_Index projection (which consume `payload["attestation"]`) are
    unchanged by the in-toto envelope."""
    handler = reload_handler
    bundle = build_bundle()
    flat_claim_bytes = attestation_claim(
        project_id="alpha-bank",
        artifact_kind="assessment-results",
        controls=["ac-1", "ac-2"],
        assessment_period="2026-q2",
        assessor="Acme Auditors",
        assessment_id="assess-123",
    )
    _attestation, event = submit(handler, bundle=bundle, claim_bytes=flat_claim_bytes)

    payload = handler.lambda_handler(event, lambda_context)

    # What the flat shape would have projected (by_alias, exclude_none) — the
    # exact downstream contract.
    expected = ProducerAttestationReferrer.model_validate_json(flat_claim_bytes).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert payload["attestation"] == expected

    # And the on-the-wire Statement layer the fixture built round-trips back to
    # that same flat claim via the libs/oci read boundary.
    statement = json.loads(
        producer_statement_bytes(
            subject_digest=bundle.manifest_digest, claim_bytes=flat_claim_bytes
        )
    )
    assert statement["predicateType"] == PRODUCER_PREDICATE_TYPE
    assert statement["predicate"] == expected
