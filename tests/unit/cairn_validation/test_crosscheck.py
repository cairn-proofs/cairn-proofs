"""Tests for cairn_validation.crosscheck ([PIPELINE-013], [INGEST-005]).

The cross-check is uniform: the producer-asserted identity triple
``orgId``/``tenantId``/``projectId`` must each exact-match the corresponding
``{org-id}``/``{tenant-id}``/``{project-id}`` segment of the single
three-segment IAM-enforced ECR repository path. There is no deployment-mode
gate and no layout-conditioned path branch.
"""

from __future__ import annotations

from cairn_oci.producer_attestation import ProducerAttestationReferrer
from cairn_validation.crosscheck import crosscheck_identity


def _claim(
    *,
    org_id: str = "org-acme",
    tenant_id: str = "tnt-foo",
    project_id: str = "prj-bar",
    artifact_kind: str = "other",
) -> ProducerAttestationReferrer:
    """A Producer_Attestation_Referrer claim — the source of the
    org/tenant/project cross-check operands (the opaque identity triple)."""
    return ProducerAttestationReferrer.model_validate(
        {
            "orgId": org_id,
            "tenantId": tenant_id,
            "projectId": project_id,
            "artifactKind": artifact_kind,
        }
    )


# A canonical deployed three-segment path the claim's default coordinates match.
_REPO = "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar"


# ---------------------------------------------------------------------------
# Happy path — all three coordinates match
# ---------------------------------------------------------------------------


def test_all_three_coordinates_match_passes() -> None:
    # [PIPELINE-013.02]: matching org, tenant, and project passes for all three.
    assert crosscheck_identity(_claim(), _REPO) == []


def test_bare_artifact_namespace_match_passes() -> None:
    # The legacy bare `artifact-{qualifier}` namespace (no appName prefix) is
    # still accepted as the structural anchor.
    assert crosscheck_identity(_claim(), "artifact-cmh-alpha/org-acme/tnt-foo/prj-bar") == []


def test_pipeline_013_02_scenario_exact_values() -> None:
    # The spec scenario [PIPELINE-013.02] verbatim.
    claim = _claim(org_id="org-acme", tenant_id="tnt-foo", project_id="prj-bar")
    errors = crosscheck_identity(
        claim, "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar"
    )
    assert errors == []


# ---------------------------------------------------------------------------
# Per-coordinate mismatch — names both values, criterion (f)
# ---------------------------------------------------------------------------


def test_org_mismatch_fails() -> None:
    claim = _claim(org_id="org-acme")
    errors = crosscheck_identity(
        claim, "cairnproofs4af-artifact-cmh-alpha/org-evil/tnt-foo/prj-bar"
    )
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "attestation.orgId"
    assert "org-acme" in errors[0].message
    assert "org-evil" in errors[0].message


def test_tenant_mismatch_fails() -> None:
    claim = _claim(tenant_id="tnt-foo")
    errors = crosscheck_identity(
        claim, "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-other/prj-bar"
    )
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "attestation.tenantId"
    assert "tnt-foo" in errors[0].message
    assert "tnt-other" in errors[0].message


def test_project_mismatch_fails() -> None:
    # [PIPELINE-013.01]: the project coordinate is one of the three exact-match legs.
    claim = _claim(project_id="prj-bar")
    errors = crosscheck_identity(
        claim, "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-wrong"
    )
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "attestation.projectId"
    assert "prj-bar" in errors[0].message
    assert "prj-wrong" in errors[0].message


def test_all_three_mismatch_returns_three_errors() -> None:
    # ALL failing coordinates are surfaced at once (not fail-on-first), so the
    # validation-failed event can identify each mismatched segment.
    claim = _claim(org_id="org-acme", tenant_id="tnt-foo", project_id="prj-bar")
    errors = crosscheck_identity(claim, "cairnproofs4af-artifact-cmh-alpha/org-x/tnt-y/prj-z")
    assert len(errors) == 3
    fields = {e.field for e in errors}
    assert fields == {"attestation.orgId", "attestation.tenantId", "attestation.projectId"}
    assert all(e.criterion == "f" for e in errors)


def test_comparison_is_case_sensitive() -> None:
    # A case-only difference is a genuine mismatch: the address is byte-exact.
    claim = _claim(project_id="Prj-Bar")
    errors = crosscheck_identity(claim, _REPO)
    assert len(errors) == 1
    assert errors[0].field == "attestation.projectId"
    assert "Prj-Bar" in errors[0].message
    assert "prj-bar" in errors[0].message


# ---------------------------------------------------------------------------
# Missing / empty / whitespace claim coordinates fail closed
# ---------------------------------------------------------------------------


def test_empty_org_id_is_error() -> None:
    claim = _claim(org_id="")
    errors = crosscheck_identity(claim, "cairnproofs4af-artifact-cmh-alpha//tnt-foo/prj-bar")
    assert any(e.field == "attestation.orgId" and e.criterion == "f" for e in errors)


def test_whitespace_tenant_id_is_error() -> None:
    claim = _claim(tenant_id="   ")
    errors = crosscheck_identity(claim, "cairnproofs4af-artifact-cmh-alpha/org-acme/   /prj-bar")
    # Whitespace is treated as missing, never silently matched against a
    # whitespace path segment.
    assert any(e.field == "attestation.tenantId" and e.criterion == "f" for e in errors)


def test_empty_project_id_is_error() -> None:
    claim = _claim(project_id="")
    errors = crosscheck_identity(
        claim, "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar"
    )
    assert len(errors) == 1
    assert errors[0].field == "attestation.projectId"
    assert errors[0].criterion == "f"


# ---------------------------------------------------------------------------
# Malformed repo path fails closed at criterion (f) ecr.repository
# ---------------------------------------------------------------------------


def test_non_artifact_namespace_fails_closed() -> None:
    errors = crosscheck_identity(_claim(), "notvalid/org-acme/tnt-foo/prj-bar")
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"
    assert "artifact-" in errors[0].message


def test_legacy_evidence_prefix_fails_closed() -> None:
    # A STALE legacy `evidence-`-prefixed path — otherwise well-formed and a
    # genuine match — must FAIL CLOSED at the namespace check, not be parsed as a
    # current path.
    errors = crosscheck_identity(_claim(), "evidence-cmh-alpha/org-acme/tnt-foo/prj-bar")
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"
    assert "artifact-" in errors[0].message


def test_too_few_segments_fails_closed() -> None:
    # Two segments after the namespace is not a valid shape — fail closed.
    errors = crosscheck_identity(_claim(), "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo")
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"


def test_too_many_segments_fails_closed() -> None:
    # A trailing artifact-kind segment is a fourth segment — not valid, fail
    # closed.
    errors = crosscheck_identity(
        _claim(), "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar/other"
    )
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"


def test_trailing_slash_appends_empty_segment_fails_closed() -> None:
    # A trailing slash appends an empty terminal segment → four segments → the
    # exact-count gate fails closed rather than silently accepting.
    errors = crosscheck_identity(
        _claim(), "cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar/"
    )
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"


def test_namespace_only_no_identity_segments_fails_closed() -> None:
    errors = crosscheck_identity(_claim(), "cairnproofs4af-artifact-cmh-alpha")
    assert len(errors) == 1
    assert errors[0].criterion == "f"
    assert errors[0].field == "ecr.repository"
