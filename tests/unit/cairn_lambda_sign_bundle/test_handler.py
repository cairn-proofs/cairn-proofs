"""Unit tests for the sign-bundle Lambda handler."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.unit.cairn_lambda_sign_bundle.conftest import (
    ACCOUNT_ID,
    REGION,
    REPO_NAME,
    SIGNER_PROFILE_ARN,
    attestation_digest,
    manifest_digest,
    signature_digest,
    validated_event,
)


def _parse_emf(out: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "_aws" not in line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "_aws" in doc:
            docs.append(doc)
    return docs


def _metric_value(doc: dict[str, Any], name: str) -> float | None:
    value = doc.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def test_happy_path_signs_and_emits_signed(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    event = validated_event(md=md, att_digest=att)

    payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.signed"]
    signed = handler._test_recorder.details("artifact.signed")[0]
    assert signed["manifestDigest"] == md
    assert signed["signatureDigest"] == signature_digest("b")
    assert signed["signingProfileArn"] == SIGNER_PROFILE_ARN
    assert signed["timestampProvider"] == "system-clock"
    assert signed["idempotentShortCircuit"] is False
    assert payload["signatureDigest"] == signature_digest("b")

    # Both subjects are signed (the both-signatures promotion gate); the content
    # signature carries the back-compat top-level signatureDigest.
    assert len(handler._test_sign_calls) == 2
    content_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{md}"
    content_call = next(
        c for c in handler._test_sign_calls if c["artifact_reference"] == content_ref
    )
    assert content_call["profile_arn"] == SIGNER_PROFILE_ARN


def test_idempotency_short_circuit_when_existing_signature(reload_handler, lambda_context) -> None:
    handler = reload_handler
    existing = signature_digest("c")
    handler._test_existing_signature_state["value"] = existing
    md = manifest_digest("a")
    att = attestation_digest("d")
    # Both subjects already carry a signature → whole-invocation short-circuit.
    handler._test_per_subject_existing[att] = signature_digest("e")
    event = validated_event(md=md, att_digest=att)

    payload = handler.lambda_handler(event, lambda_context)

    assert payload["signatureDigest"] == existing
    assert payload["idempotentShortCircuit"] is True
    assert handler._test_sign_calls == []
    signed = handler._test_recorder.details("artifact.signed")[0]
    assert signed["signatureDigest"] == existing
    assert signed["idempotentShortCircuit"] is True


_RESOURCE_REFERENCE = {
    "uuid": "11111111-2222-3333-4444-555555555555",
    "title": "Assessment Results Bundle",
    "rlinks": [
        {
            "href": "sha256:" + ("a" * 64),
            "media-type": "application/vnd.oci.image.manifest.v1+json",
        }
    ],
}


def test_resource_reference_forwarded_on_fresh_sign(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    event = validated_event(md=md, att_digest=attestation_digest("d"))
    event["detail"]["artifactResourceReference"] = _RESOURCE_REFERENCE

    handler.lambda_handler(event, lambda_context)

    signed = handler._test_recorder.details("artifact.signed")[0]
    assert signed["idempotentShortCircuit"] is False
    assert signed["artifactResourceReference"] == _RESOURCE_REFERENCE


def test_resource_reference_forwarded_on_short_circuit(reload_handler, lambda_context) -> None:
    handler = reload_handler
    handler._test_existing_signature_state["value"] = signature_digest("c")
    md = manifest_digest("a")
    att = attestation_digest("d")
    # Both subjects already signed → whole-invocation short-circuit.
    handler._test_per_subject_existing[att] = signature_digest("e")
    event = validated_event(md=md, att_digest=att)
    event["detail"]["artifactResourceReference"] = _RESOURCE_REFERENCE

    payload = handler.lambda_handler(event, lambda_context)

    assert payload["idempotentShortCircuit"] is True
    signed = handler._test_recorder.details("artifact.signed")[0]
    assert signed["artifactResourceReference"] == _RESOURCE_REFERENCE


def test_resource_reference_absent_when_input_lacks_it(reload_handler, lambda_context) -> None:
    handler = reload_handler
    event = validated_event(md=manifest_digest("a"), att_digest=attestation_digest("d"))
    assert "artifactResourceReference" not in event["detail"]

    payload = handler.lambda_handler(event, lambda_context)

    assert "artifactResourceReference" not in payload
    signed = handler._test_recorder.details("artifact.signed")[0]
    assert "artifactResourceReference" not in signed


def test_rejects_non_system_clock_timestamp_provider(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.timestamp_provider import TimestampProvider

    monkeypatch.setattr(
        handler,
        "read_timestamp_provider_from_ssm",
        lambda stage: TimestampProvider.RFC3161,
    )
    event = validated_event()

    with pytest.raises(RuntimeError, match="only system-clock is built"):
        handler.lambda_handler(event, lambda_context)


def test_event_without_detail_dict_raises_value_error(reload_handler, lambda_context) -> None:
    handler = reload_handler
    with pytest.raises(ValueError, match=r"event\.detail missing or not a dict"):
        event = {"detail-type": "artifact.validated", "detail": None}
        handler.lambda_handler(event, lambda_context)


def test_permanent_notation_failure_emits_signing_failed_and_marks_unsigned(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    def _fail(*args, **kwargs):
        raise NotationCommandError(
            returncode=2,
            stderr="AccessDeniedException: principal not authorized to sign\n",
            command=["notation", "sign"],
        )

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    md = manifest_digest("a")
    event = validated_event(md=md, att_digest=attestation_digest("d"))

    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    failed = handler._test_recorder.details("artifact.signing-failed")
    assert len(failed) == 1
    assert failed[0]["manifestDigest"] == md
    assert "AccessDeniedException" in failed[0]["signingError"]


def test_transient_notation_failure_reraises_without_signing_failed_event(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    def _fail(*args, **kwargs):
        raise NotationCommandError(
            returncode=1,
            stderr="ThrottlingException: rate exceeded",
            command=["notation", "sign"],
        )

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    with pytest.raises(NotationCommandError):
        handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)

    assert handler._test_recorder.details("artifact.signing-failed") == []


def test_zero_returncode_anomaly_classified_as_transient(reload_handler) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    err = NotationCommandError(returncode=0, stderr="", command=["notation", "list"])
    assert handler._is_transient(err) is True


def test_unknown_stderr_classified_as_permanent(reload_handler) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    err = NotationCommandError(
        returncode=2, stderr="UnknownErrorThatIsNotInTheMarkerList", command=["notation", "sign"]
    )
    assert handler._is_transient(err) is False


def test_throttling_stderr_classified_as_transient(reload_handler) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    err = NotationCommandError(
        returncode=1, stderr="ThrottlingException", command=["notation", "sign"]
    )
    assert handler._is_transient(err) is True


def test_observability_scope_invoked_with_sign_event_type(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    captured: list[str | None] = []
    original = handler.observability_scope

    from contextlib import contextmanager

    @contextmanager
    def recording_scope(event, **kwargs):
        captured.append(kwargs.get("event_type"))
        with original(event, **kwargs) as tc:
            yield tc

    monkeypatch.setattr(handler, "observability_scope", recording_scope)
    handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)

    assert captured == ["sign"]


def test_success_metric_on_signed_path(reload_handler, lambda_context, capsys) -> None:
    handler = reload_handler
    handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)
    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Success") == 1.0 for d in docs)
    assert any(_metric_value(d, "DurationMs") is not None for d in docs)


def test_failure_metric_on_permanent_signing_failure(
    reload_handler, lambda_context, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    def _fail(*args, **kwargs):
        raise NotationCommandError(returncode=2, stderr="permanent", command=["notation", "sign"])

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    with pytest.raises(NotationCommandError):
        handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert any(_metric_value(d, "Failure") == 1.0 for d in docs)


def test_permanent_failure_writes_unsigned_to_metadata_when_record_exists(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    md = manifest_digest("a")
    # The row is addressed by the claim's tenantId (partition key) and
    # {projectId}#{manifestDigest} (sort key) under the re-keyed Metadata_Index
    # ([META-001]).
    tenant_id = "tnt-alpha-bank"
    project_id = "prj-assessment"
    pk = tenant_id
    sk = f"{project_id}#{md}"
    handler._test_metadata_table.put_item(
        Item={
            "pk": pk,
            "sk": sk,
            "updatedAt": "2026-05-26T00:00:00Z",
            "signingStatus": "pending",
        }
    )

    def _fail(*args, **kwargs):
        raise NotationCommandError(
            returncode=2, stderr="AccessDenied", command=["notation", "sign"]
        )

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    claim = {"orgId": "org-acme", "tenantId": tenant_id, "projectId": project_id}
    event = validated_event(md=md, att_digest=attestation_digest("d"), attestation=claim)
    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    item = handler._test_metadata_table.get_item(Key={"pk": pk, "sk": sk})["Item"]
    assert item["signingStatus"] == "unsigned"
    assert "signingError" in item


def test_permanent_failure_metadata_update_swallows_when_record_absent(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    def _fail(*args, **kwargs):
        raise NotationCommandError(
            returncode=2, stderr="AccessDenied", command=["notation", "sign"]
        )

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    md = manifest_digest("a")
    tenant_id = "tnt-alpha-bank"
    project_id = "prj-assessment"
    claim = {"orgId": "org-acme", "tenantId": tenant_id, "projectId": project_id}
    # No row pre-seeded: the conditional unsigned-mark fails closed and is
    # swallowed (update-metadata creates the record from the signing-failed event).
    event = validated_event(md=md, att_digest=attestation_digest("d"), attestation=claim)
    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    item = handler._test_metadata_table.get_item(
        Key={"pk": tenant_id, "sk": f"{project_id}#{md}"}
    ).get("Item")
    assert item is None
    failed = handler._test_recorder.details("artifact.signing-failed")
    assert len(failed) == 1


def test_signer_profile_arn_missing_raises_runtime_error(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    monkeypatch.setattr(handler, "SIGNER_PROFILE_ARN", "")
    with pytest.raises(RuntimeError, match="SIGNER_PROFILE_ARN"):
        handler.lambda_handler(validated_event(att_digest=attestation_digest("d")), lambda_context)


def test_existing_signature_skips_signer_profile_arn_requirement(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    handler._test_existing_signature_state["value"] = signature_digest("e")
    # Both subjects already signed → whole-invocation short-circuit, so the
    # missing SIGNER_PROFILE_ARN is never reached.
    handler._test_per_subject_existing[att] = signature_digest("c")
    monkeypatch.setattr(handler, "SIGNER_PROFILE_ARN", "")

    payload = handler.lambda_handler(validated_event(md=md, att_digest=att), lambda_context)
    assert payload["idempotentShortCircuit"] is True


# ---------------------------------------------------------------------------
# Two independent signatures (content manifest + Attestation
# referrer). Decision D2: the same content can carry many attestations, so
# the two signings are independent and the promotion gate is BOTH.
# ---------------------------------------------------------------------------


def test_both_subjects_signed_as_distinct_signatures(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    event = validated_event(md=md, att_digest=att, attestation={"projectId": "alpha-bank"})

    payload = handler.lambda_handler(event, lambda_context)

    # Two independent Notation invocations — one per subject digest.
    refs = {c["artifact_reference"] for c in handler._test_sign_calls}
    content_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{md}"
    att_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{att}"
    assert refs == {content_ref, att_ref}
    assert len(handler._test_sign_calls) == 2

    # The two signatures are distinct OCI referrers with distinct digests.
    content_sig = payload["signatures"]["content"]["signatureDigest"]
    att_sig = payload["signatures"]["attestation"]["signatureDigest"]
    assert content_sig == signature_digest("b")
    assert att_sig is not None
    assert att_sig != content_sig
    assert payload["signatures"]["content"]["subjectDigest"] == md
    assert payload["signatures"]["attestation"]["subjectDigest"] == att

    # Back-compat top-level keys + carried-forward attestation context.
    assert payload["signatureDigest"] == content_sig
    assert payload["attestationDigest"] == att
    assert payload["attestationSignatureDigest"] == att_sig
    assert payload["attestation"] == {"projectId": "alpha-bank"}
    assert payload["idempotentShortCircuit"] is False


def test_signed_event_carries_attestation_forward(reload_handler, lambda_context) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    claim = {"projectId": "alpha-bank", "artifactKind": "assessment-results"}
    event = validated_event(md=md, att_digest=att, attestation=claim)

    handler.lambda_handler(event, lambda_context)

    signed = handler._test_recorder.details("artifact.signed")[0]
    assert signed["attestationDigest"] == att
    assert signed["attestation"] == claim
    assert signed["signatures"]["attestation"]["subjectDigest"] == att


def test_new_attestation_over_signed_content_signs_only_attestation(
    reload_handler, lambda_context
) -> None:
    """D2: a new claim over already-signed content signs only the claim."""
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    existing_content_sig = signature_digest("c")
    # Content already carries a valid signature; the attestation does not.
    handler._test_per_subject_existing[md] = existing_content_sig
    handler._test_per_subject_existing[att] = None

    event = validated_event(md=md, att_digest=att)
    payload = handler.lambda_handler(event, lambda_context)

    # Only the attestation subject is freshly signed; content is untouched.
    signed_refs = [c["artifact_reference"] for c in handler._test_sign_calls]
    att_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{att}"
    assert signed_refs == [att_ref]
    # Existing content signature is preserved, not regenerated.
    assert payload["signatures"]["content"]["signatureDigest"] == existing_content_sig
    assert payload["signatures"]["attestation"]["signatureDigest"] is not None
    # A fresh sign happened (the attestation), so not a whole-invocation short-circuit.
    assert payload["idempotentShortCircuit"] is False


def test_content_unsigned_with_signed_attestation_signs_only_content(
    reload_handler, lambda_context
) -> None:
    """Inverse of the new-attestation case: the attestation already carries a
    valid signature but the content does not, so the pipeline signs ONLY the
    content. The two signatures are independent (signing content does not
    re-sign the attestation), and the existing attestation signature is left
    untouched — completing the BOTH-required promotion gate (signing
    *Content and attestation signatures are produced independently*)."""
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    existing_att_sig = signature_digest("e")
    handler._test_per_subject_existing[md] = None
    handler._test_per_subject_existing[att] = existing_att_sig

    payload = handler.lambda_handler(validated_event(md=md, att_digest=att), lambda_context)

    # Only the content subject is freshly signed; the attestation is untouched.
    signed_refs = [c["artifact_reference"] for c in handler._test_sign_calls]
    content_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{md}"
    assert signed_refs == [content_ref]
    assert payload["signatures"]["attestation"]["signatureDigest"] == existing_att_sig
    assert payload["signatures"]["content"]["signatureDigest"] is not None
    assert payload["idempotentShortCircuit"] is False


def test_both_signatures_present_short_circuits_whole_invocation(
    reload_handler, lambda_context
) -> None:
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    handler._test_per_subject_existing[md] = signature_digest("c")
    handler._test_per_subject_existing[att] = signature_digest("e")

    payload = handler.lambda_handler(validated_event(md=md, att_digest=att), lambda_context)

    assert handler._test_sign_calls == []
    assert payload["idempotentShortCircuit"] is True
    assert payload["signatures"]["content"]["signatureDigest"] == signature_digest("c")
    assert payload["signatures"]["attestation"]["signatureDigest"] == signature_digest("e")


def test_attestation_signing_failure_fails_whole_invocation(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promotion gate is BOTH: if the attestation signature fails, no signed event."""
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    md = manifest_digest("a")
    att = attestation_digest("d")
    att_ref = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}@{att}"

    real_sign = handler.sign_artifact

    def _sign_fail_on_attestation(artifact_reference, *args, **kwargs):
        if artifact_reference == att_ref:
            raise NotationCommandError(
                returncode=2,
                stderr="AccessDeniedException: not authorized to sign attestation\n",
                command=["notation", "sign"],
            )
        return real_sign(artifact_reference, *args, **kwargs)

    monkeypatch.setattr(handler, "sign_artifact", _sign_fail_on_attestation)

    event = validated_event(md=md, att_digest=att)
    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    # No artifact.signed — a half-signed submission is never promoted.
    assert handler._test_recorder.details("artifact.signed") == []
    failed = handler._test_recorder.details("artifact.signing-failed")
    assert len(failed) == 1
    assert failed[0]["manifestDigest"] == md
    assert failed[0]["attestationDigest"] == att


def test_missing_attestation_digest_fails_closed(reload_handler, lambda_context) -> None:
    """A missing attestationDigest fails closed — never degrades to content-only.

    The promotion gate is BOTH signatures; a submission with
    no Attestation referrer to sign is a permanent failure: no signing happens,
    no artifact.signed is emitted, the bundle is marked unsigned, and
    artifact.signing-failed routes it to the DLQ.
    """
    handler = reload_handler
    md = manifest_digest("a")

    with pytest.raises(RuntimeError, match="attestationDigest"):
        handler.lambda_handler(validated_event(md=md), lambda_context)

    # No content-only signing happened — fail closed before any sign call.
    assert handler._test_sign_calls == []
    # No promotion: no artifact.signed, and the failure is surfaced for triage.
    assert handler._test_recorder.details("artifact.signed") == []
    failed = handler._test_recorder.details("artifact.signing-failed")
    assert len(failed) == 1
    assert failed[0]["manifestDigest"] == md


def test_identity_triple_sourced_from_attestation_claim_on_permanent_failure(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unsigned-mark row coordinates come from the claim triple, not configBlob.

    Per [INGEST-005] the bundle's identity is the opaque
    orgId/tenantId/projectId triple on the signed Producer Attestation referrer
    claim (projected under ``attestation``). On a permanent failure the
    unsigned-mark must address the row by the claim's tenantId (partition key)
    and projectId (sort-key sub-hierarchy), never an AWS account number or
    configBlob field. We drive a permanent Notation failure and capture the
    coordinates the unsigned-mark was called with.
    """
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    md = manifest_digest("a")
    att = attestation_digest("d")
    claim = {"orgId": "org-acme", "tenantId": "tnt-alpha-bank", "projectId": "prj-assessment"}

    captured: dict[str, Any] = {}

    def _capture_unsigned(*, manifest_digest, tenant_id, project_id, error_message):
        captured["tenant_id"] = tenant_id
        captured["project_id"] = project_id

    monkeypatch.setattr(handler, "_mark_unsigned_in_metadata", _capture_unsigned)

    def _sign_perm_fail(artifact_reference, *args, **kwargs):
        raise NotationCommandError(
            returncode=2,
            stderr="AccessDeniedException: not authorized\n",
            command=["notation", "sign"],
        )

    monkeypatch.setattr(handler, "sign_artifact", _sign_perm_fail)

    event = validated_event(md=md, att_digest=att, attestation=claim)
    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    # tenantId / projectId came from the claim triple (configBlob carries neither).
    assert captured["tenant_id"] == "tnt-alpha-bank"
    assert captured["project_id"] == "prj-assessment"


def test_signing_failed_event_threads_identity_triple_for_update_metadata(
    reload_handler, lambda_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact.signing-failed event carries the claim's identity triple.

    update_metadata's signing-failed path sources the
    orgId/tenantId/projectId triple from ``detail.attestation`` to address the
    unsigned row ([META-001]). The sign stage must thread the claim
    forward (not drop it) and keep configBlob passthrough intact.
    """
    handler = reload_handler
    from cairn_signing.notation import NotationCommandError

    def _fail(*args, **kwargs):
        raise NotationCommandError(
            returncode=2, stderr="AccessDenied", command=["notation", "sign"]
        )

    monkeypatch.setattr(handler, "sign_artifact", _fail)

    md = manifest_digest("a")
    att = attestation_digest("d")
    claim = {"orgId": "org-acme", "tenantId": "tnt-alpha-bank", "projectId": "prj-assessment"}
    event = validated_event(md=md, att_digest=att, attestation=claim)

    with pytest.raises(NotationCommandError):
        handler.lambda_handler(event, lambda_context)

    failed = handler._test_recorder.details("artifact.signing-failed")[0]
    assert failed["attestation"] == claim
    assert failed["attestation"]["orgId"] == "org-acme"
    assert failed["attestation"]["tenantId"] == "tnt-alpha-bank"
    assert failed["attestation"]["projectId"] == "prj-assessment"
    # configBlob passthrough preserved.
    assert "configBlob" in failed


def test_signing_failed_on_missing_attestation_digest_threads_claim(
    reload_handler, lambda_context
) -> None:
    """The fail-closed (missing attestationDigest) path also threads the claim
    so update_metadata can address the unsigned row from the triple."""
    handler = reload_handler
    md = manifest_digest("a")
    claim = {"orgId": "org-acme", "tenantId": "tnt-alpha-bank", "projectId": "prj-assessment"}
    # No attestationDigest → fail closed; attestation claim is still present.
    event = validated_event(md=md, attestation=claim)

    with pytest.raises(RuntimeError, match="attestationDigest"):
        handler.lambda_handler(event, lambda_context)

    failed = handler._test_recorder.details("artifact.signing-failed")[0]
    assert failed["attestation"] == claim


def test_tenant_id_metric_dimension_emitted_from_claim(
    reload_handler, lambda_context, capsys
) -> None:
    """A per-invocation TenantId dimension (value = path-bound tenant id from the
    claim) attaches to the emitted EMF ([OBS-009] — uniform tenant-id dimension
    replacing the former Service_Hosted-gated AccountId dimension)."""
    handler = reload_handler
    md = manifest_digest("a")
    att = attestation_digest("d")
    claim = {"orgId": "org-acme", "tenantId": "tnt-alpha-bank", "projectId": "prj-assessment"}
    event = validated_event(md=md, att_digest=att, attestation=claim)

    handler.lambda_handler(event, lambda_context)

    docs = _parse_emf(capsys.readouterr().out)
    assert docs, "expected at least one EMF metric document"
    assert any(d.get("TenantId") == "tnt-alpha-bank" for d in docs), (
        f"expected TenantId='tnt-alpha-bank' on an EMF line; "
        f"saw {[d.get('TenantId') for d in docs]!r}"
    )


# ===========================================================================
# Post-sign Referrers-API propagation poll (live two-signature flap regression).
# ===========================================================================


def test_poll_signature_digest_rides_out_referrers_propagation_lag(
    reload_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-sign lookup polls past ECR's Referrers-API eventual-consistency lag.

    Regression for the live two-signature flap: a fast invocation listed the
    referrers before the just-attached signature propagated and raised
    "no signature referrer found", burning the async-retry budget per subject.
    The bounded poll returns the digest as soon as it appears.
    """
    handler = reload_handler
    monkeypatch.setattr(handler, "SIGNATURE_LOOKUP_DELAY_SECONDS", 0.0)

    calls = {"n": 0}
    resolved = "sha256:" + "a" * 64

    def _lagging(*, registry_id: str, repository_name: str, image_digest: str):
        calls["n"] += 1
        return resolved if calls["n"] >= 3 else None  # two misses, then a hit

    monkeypatch.setattr(handler, "existing_signature_digest", _lagging)

    digest = handler._poll_signature_digest(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/mvp-tenant",
        subject_digest="sha256:" + "c" * 64,
    )
    assert digest == resolved
    assert calls["n"] == 3


def test_poll_signature_digest_returns_none_after_max_attempts(
    reload_handler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the signature never propagates within the bounded polls, return None so
    the caller raises a transient RuntimeError (async retry), never a false hit."""
    handler = reload_handler
    monkeypatch.setattr(handler, "SIGNATURE_LOOKUP_DELAY_SECONDS", 0.0)

    calls = {"n": 0}

    def _never(*, registry_id: str, repository_name: str, image_digest: str):
        calls["n"] += 1
        return None

    monkeypatch.setattr(handler, "existing_signature_digest", _never)

    digest = handler._poll_signature_digest(
        registry_id="111122223333",
        repository_name="artifact-cmh-alpha/mvp-tenant",
        subject_digest="sha256:" + "c" * 64,
    )
    assert digest is None
    assert calls["n"] == handler.SIGNATURE_LOOKUP_MAX_ATTEMPTS
