"""Tests for cairn_oci.annotations."""

from __future__ import annotations

from cairn_oci.annotations import (
    LAYER_REQUIRED_TITLE,
    MANIFEST_OPTIONAL,
    MANIFEST_REQUIRED,
    PINNED_CREATED,
    CairnAnnotations,
)


def test_layer_required_title_is_opencontainers_key() -> None:
    assert LAYER_REQUIRED_TITLE == "org.opencontainers.image.title"


def test_manifest_required_keys_are_identity_set_only() -> None:
    # Identity floor = {project-id}. The synthetic `created` annotation is not
    # in the required set, and artifact-kind is not in the content manifest — it
    # is carried only by the Producer_Attestation_Referrer claim.
    assert MANIFEST_REQUIRED == frozenset(
        {
            CairnAnnotations.PROJECT_ID,
        }
    )


def test_created_is_not_a_required_annotation() -> None:
    # `org.opencontainers.image.created` is dropped from the required set: the
    # manifest carries no synthetic creation timestamp anymore.
    assert CairnAnnotations.CREATED not in MANIFEST_REQUIRED


def test_claim_annotations_are_not_in_the_manifest_sets() -> None:
    # The producer claim moved to the Producer_Attestation_Referrer; its annotation keys
    # are absent from BOTH manifest sets (the constants may survive for back-compat
    # importers, but they are not part of the content manifest's annotation floor).
    for key in (
        CairnAnnotations.CONTROLS,
        CairnAnnotations.ASSESSMENT_PERIOD,
        CairnAnnotations.ASSESSOR,
        CairnAnnotations.ASSESSMENT_ID,
        CairnAnnotations.SUPERSEDES,
    ):
        assert key not in MANIFEST_REQUIRED
        assert key not in MANIFEST_OPTIONAL


def test_manifest_optional_is_empty() -> None:
    # No manifest-level optional annotations remain; the producer claim lives in
    # the Producer_Attestation_Referrer, not on the content manifest.
    assert MANIFEST_OPTIONAL == frozenset()


def test_control_family_is_not_a_manifest_annotation() -> None:
    # control-family is a DERIVED projection, never a manifest annotation — it is
    # absent from both vocabulary frozensets AND the constant is removed entirely,
    # so it cannot be re-introduced into the signed manifest by accident.
    assert not hasattr(CairnAnnotations, "CONTROL_FAMILY")
    control_family_key = "dev.cairnproofs.artifact.control-family"
    assert control_family_key not in MANIFEST_REQUIRED
    assert control_family_key not in MANIFEST_OPTIONAL


def test_required_and_optional_are_disjoint() -> None:
    assert MANIFEST_REQUIRED.isdisjoint(MANIFEST_OPTIONAL)


def test_created_annotation_constant_retained_for_back_compat() -> None:
    # The CREATED constant exists on CairnAnnotations for importers that
    # reference the key string; it is not in MANIFEST_REQUIRED (see
    # test_created_is_not_a_required_annotation).
    assert CairnAnnotations.CREATED == "org.opencontainers.image.created"


def test_pinned_created_is_exported_from_package_root() -> None:
    # PINNED_CREATED is exported from the package root so `cairn push` can import
    # it (`from cairn_oci import PINNED_CREATED`). Guard the export wiring: a
    # missing/renamed re-export would break the producer's pin and silently let
    # oras re-inject now().
    import cairn_oci

    assert hasattr(cairn_oci, "PINNED_CREATED")
    assert cairn_oci.PINNED_CREATED is PINNED_CREATED
    assert "PINNED_CREATED" in cairn_oci.__all__


def test_pinned_created_is_fixed_epoch_sentinel() -> None:
    # PINNED_CREATED is a deterministic sentinel carrying NO ambient state — the
    # Unix epoch in RFC3339/Zulu form. If this ever drifts to a wall-clock or
    # per-build value the content Manifest_Digest stops being reproducible.
    assert PINNED_CREATED == "1970-01-01T00:00:00Z"


def test_artifact_kind_is_not_a_manifest_annotation() -> None:
    # The artifact-kind key is never a
    # signed-manifest annotation — it is absent from both vocabulary frozensets, so
    # it cannot be re-introduced into the signed manifest by accident. Kind is
    # carried solely as the Producer_Attestation_Referrer claim.
    artifact_kind_key = "dev.cairnproofs.artifact.artifact-kind"
    assert artifact_kind_key not in MANIFEST_REQUIRED
    assert artifact_kind_key not in MANIFEST_OPTIONAL


def test_cairn_keys_use_cairnproofs_artifact_namespace() -> None:
    cairn_keys = {
        CairnAnnotations.PROJECT_ID,
        CairnAnnotations.CONTROLS,
        CairnAnnotations.ASSESSMENT_PERIOD,
        CairnAnnotations.ASSESSOR,
        CairnAnnotations.ASSESSMENT_ID,
        CairnAnnotations.SUPERSEDES,
        CairnAnnotations.SOURCE_TOOL,
        CairnAnnotations.GENERATION_TIMESTAMP,
    }
    for key in cairn_keys:
        assert key.startswith("dev.cairnproofs.artifact."), key
