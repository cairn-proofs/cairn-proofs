"""Handler-level cross-check negative space for [PIPELINE-013] / [PIPELINE-013.01]
(gap-fill area 2).

``crosscheck_identity`` (the lib) is unit-tested exhaustively in
``tests/unit/cairn_validation/test_crosscheck.py``; the validate handler is
tested for a tenant-id and a project-id claim mismatch in ``test_handler.py``.
This module closes the two integration-level gaps those leave:

1. The **org-id** leg — a claimed ``orgId`` disagreeing with the path's
   ``{org-id}`` segment is a permanent validation failure naming both values.
   (The handler-level suite covered tenant and project but not org.)
2. The **all-three-mismatch** case — when org, tenant, AND project all disagree,
   the single emitted ``artifact.validation-failed`` event identifies EACH
   mismatched segment (not fail-on-first), per [PIPELINE-013.01] "an
   artifact.validation-failed event identifying each mismatched segment".

Each case must route to a PERMANENT failure (``handler._REJECTED``, no retry).
"""

from __future__ import annotations

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    ORG_ID,
    PROJECT_ID,
    REPO_NAME,
    TENANT_ID,
    attestation_claim,
    attestation_push_event,
    build_attestation,
    build_bundle,
)


def _submit_with_claim(handler, lambda_context, claim_bytes):
    bundle = build_bundle(repo_name=REPO_NAME)
    handler._test_fake_ecr.add_bundle(bundle)
    attestation = build_attestation(
        subject_digest=bundle.manifest_digest,
        repo_name=REPO_NAME,
        claim_bytes=claim_bytes,
    )
    handler._test_fake_ecr.add_attestation(attestation)
    event = attestation_push_event(attestation=attestation)
    result = handler.lambda_handler(event, lambda_context)
    failed = handler._test_recorder.details("artifact.validation-failed")
    return result, failed


def test_org_id_mismatch_from_claim_routes_to_validation_failed(
    reload_handler, lambda_context
) -> None:
    """A claimed orgId disagreeing with the ECR path's {org-id} segment is a
    permanent failure naming both values — the org leg of the uniform
    org+tenant+project exact-match cross-check (no mode gate)."""
    handler = reload_handler
    result, failed = _submit_with_claim(
        handler, lambda_context, attestation_claim(org_id="org-foreign")
    )

    assert result is handler._REJECTED
    criteria = failed[-1]["failedCriteria"]
    org_errors = [
        c for c in criteria if c["criterion"] == "f" and c["field"] == "attestation.orgId"
    ]
    assert org_errors, f"expected an attestation.orgId criterion-(f) error; got {criteria!r}"
    msg = org_errors[0]["message"]
    assert "org-foreign" in msg
    assert ORG_ID in msg


def test_all_three_mismatch_surfaces_every_segment_in_one_event(
    reload_handler, lambda_context
) -> None:
    """[PIPELINE-013.01]: when org, tenant, AND project all disagree, the single
    validation-failed event identifies EACH mismatched segment (all-at-once, not
    fail-on-first), so an operator sees the full picture before triage."""
    handler = reload_handler
    result, failed = _submit_with_claim(
        handler,
        lambda_context,
        attestation_claim(org_id="org-evil", tenant_id="tnt-evil", project_id="prj-evil"),
    )

    assert result is handler._REJECTED
    assert len(failed) == 1, "a permanent failure emits exactly one validation-failed event"
    criteria = failed[0]["failedCriteria"]
    mismatch_fields = {
        c["field"]
        for c in criteria
        if c["criterion"] == "f" and c["field"].startswith("attestation.")
    }
    assert {"attestation.orgId", "attestation.tenantId", "attestation.projectId"} <= mismatch_fields

    # Each mismatched segment names BOTH the claimed value and the path segment.
    by_field = {c["field"]: c["message"] for c in criteria if c["field"].startswith("attestation.")}
    assert "org-evil" in by_field["attestation.orgId"] and ORG_ID in by_field["attestation.orgId"]
    assert (
        "tnt-evil" in by_field["attestation.tenantId"]
        and TENANT_ID in by_field["attestation.tenantId"]
    )
    assert (
        "prj-evil" in by_field["attestation.projectId"]
        and PROJECT_ID in by_field["attestation.projectId"]
    )
