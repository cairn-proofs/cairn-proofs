"""Threat T3 — producer-supplied artifact-kind audit trail.

Back-compat aliases silently remap producer intent (``policies`` →
``governance-doc``, ``scan-reports`` → ``tool-output``, …). Recording ONLY the resolved canonical
loses what the producer literally sent. The artifact-kind is sourced from the
Producer_Attestation_Referrer claim (``artifactKind``), not a content-manifest
annotation or tag. These tests assert the handler
preserves the producer-SUPPLIED token alongside the resolved canonical on:

- the SUCCESS path (structured ``validate-bundle validated`` log + the emitted
  ``artifact.validated`` payload), and
- the ``artifact.validation-failed`` event / its ``LOG.warning`` extra.
"""

from __future__ import annotations

import logging

from tests.unit.cairn_lambda_validate_bundle.conftest import (
    attestation_claim,
    build_bundle,
    submit,
)


def test_success_path_records_supplied_alias_and_resolved_canonical(
    reload_handler, lambda_context, caplog
) -> None:
    """A claim whose artifactKind is the back-compat alias ``policies`` records BOTH
    supplied ``policies`` and resolved canonical ``governance-doc`` on the success-path
    structured log and the emitted ``artifact.validated`` payload."""
    handler = reload_handler
    # The claim's artifactKind is the back-compat alias ``policies``; kind is
    # claim-only now (no manifest annotation / Config_Blob bundleType).
    bundle = build_bundle()
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(artifact_kind="policies")
    )

    with caplog.at_level(logging.INFO):
        payload = handler.lambda_handler(event, lambda_context)

    assert handler._test_recorder.detail_types() == ["artifact.validated"]
    assert payload["suppliedArtifactKind"] == "policies"
    assert payload["resolvedArtifactKind"] == "governance-doc"

    emitted = handler._test_recorder.details("artifact.validated")[0]
    assert emitted["suppliedArtifactKind"] == "policies"
    assert emitted["resolvedArtifactKind"] == "governance-doc"

    records = [
        r
        for r in caplog.records
        if r.getMessage() == "validate-bundle validated" and hasattr(r, "suppliedArtifactKind")
    ]
    assert records, "no structured success log record carried suppliedArtifactKind"
    assert records[-1].suppliedArtifactKind == "policies"
    assert records[-1].resolvedArtifactKind == "governance-doc"


def test_failure_event_carries_supplied_artifact_kind_from_claim(
    reload_handler, lambda_context, caplog
) -> None:
    """A cross-check failure emits an ``artifact.validation-failed`` event that
    surfaces the supplied/resolved artifact-kind (claim-sourced) for the audit
    trail. Here the [PIPELINE-013] projectId cross-check fails (claim projectId vs the
    ECR repository path's project-id segment), while the claim's artifactKind
    drives the T3 audit."""
    handler = reload_handler
    bundle = build_bundle()
    _attestation, event = submit(
        handler,
        bundle=bundle,
        claim_bytes=attestation_claim(project_id="wrong-bank", artifact_kind="policies"),
    )

    with caplog.at_level(logging.WARNING):
        assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    detail = failed[0]

    # T3: supplied artifact-kind is the CLAIM's artifactKind (verbatim) + resolved.
    assert detail["suppliedArtifactKind"] == "policies"
    assert detail["resolvedArtifactKind"] == "governance-doc"

    # The criterion-(f) messages identify the projectId mismatch.
    f_messages = " ".join(
        c["message"] for c in detail["failedCriteria"] if c.get("criterion") == "f"
    )
    assert f_messages, "expected at least one criterion-(f) cross-check failure"
    assert "wrong-bank" in f_messages

    warn_records = [r for r in caplog.records if hasattr(r, "suppliedArtifactKind")]
    assert warn_records, "no failure log record carried suppliedArtifactKind"
    assert warn_records[-1].suppliedArtifactKind == "policies"
    assert warn_records[-1].resolvedArtifactKind == "governance-doc"


def test_unparseable_config_blob_failure_still_surfaces_claim_kind(
    reload_handler, lambda_context
) -> None:
    """When the content Config_Blob cannot be parsed, the failure event still
    surfaces the supplied/resolved artifact-kind from the CLAIM (which is parsed
    from the Producer_Attestation_Referrer before the content Config_Blob), without crashing
    the already-failing path (Threat T3)."""
    handler = reload_handler
    bundle = build_bundle(config_bytes=b'{"bundleType": "assessment-results"}')  # no projectId
    _attestation, event = submit(
        handler, bundle=bundle, claim_bytes=attestation_claim(artifact_kind="assessment-results")
    )

    assert handler.lambda_handler(event, lambda_context) is handler._REJECTED

    failed = handler._test_recorder.details("artifact.validation-failed")
    assert len(failed) == 1
    assert failed[0]["suppliedArtifactKind"] == "assessment-results"
    assert failed[0]["resolvedArtifactKind"] == "assessment-results"
