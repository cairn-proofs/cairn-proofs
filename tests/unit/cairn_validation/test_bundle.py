"""Tests for cairn_validation.bundle ([INGEST-017.07] completeness).

Split by trust origin:
- ``validate_bundle`` covers the CONTENT manifest structure — criteria (a)
  required identity annotations, (b) layer titles, (c1-c4) Config_Blob media
  type + parse.
- ``validate_claim`` covers the producer's CLAIM, carried in the signed
  Producer_Attestation_Referrer — criteria (d) artifact-kind vocabulary, (c5)
  catalogOrProfileUuid, (h) control-id grammar.
"""

from __future__ import annotations

import json

import pytest
from cairn_oci.annotations import (
    LAYER_REQUIRED_TITLE,
    MANIFEST_REQUIRED,
    CairnAnnotations,
)
from cairn_oci.config_blob import CONFIG_BLOB_MEDIA_TYPE
from cairn_oci.manifest import (
    MAX_ARTIFACT_SIZE_BYTES,
    MAX_CONTENT_LAYERS,
    Descriptor,
    OciManifest,
)
from cairn_oci.producer_attestation import ProducerAttestationReferrer
from cairn_validation.bundle import (
    BundleValidationFailure,
    validate_bundle,
    validate_claim,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Helpers — build a valid bundle so individual tests can mutate one field.
# ---------------------------------------------------------------------------

# The required identity annotation floor is {PROJECT_ID} only — no `created`
# annotation and no artifact-kind in the content manifest.
VALID_ANNOTATIONS: dict[str, str] = {
    CairnAnnotations.PROJECT_ID: "alpha-bank",
}

# The reduced content Config_Blob carries only content-descriptive fields
# (projectId; no bundleType — kind is claim-only now).
VALID_CONFIG_BODY = json.dumps(
    {
        "projectId": "alpha-bank",
    }
).encode("utf-8")


def _claim(
    *,
    org_id: str = "org-acme",
    tenant_id: str = "tnt-foo",
    project_id: str = "alpha-bank",
    artifact_kind: str = "assessment-results",
    catalog_or_profile_uuid: str | None = None,
    controls: list[str] | None = None,
) -> ProducerAttestationReferrer:
    payload: dict[str, object] = {
        "orgId": org_id,
        "tenantId": tenant_id,
        "projectId": project_id,
        "artifactKind": artifact_kind,
    }
    if catalog_or_profile_uuid is not None:
        payload["catalogOrProfileUuid"] = catalog_or_profile_uuid
    if controls is not None:
        payload["controls"] = controls
    return ProducerAttestationReferrer.model_validate(payload)


def _layer(title: str = "evidence-1.json", media_type: str = "application/json") -> Descriptor:
    return Descriptor(
        mediaType=media_type,
        digest="sha256:" + "0" * 64,
        size=10,
        annotations={LAYER_REQUIRED_TITLE: title},
    )


def _config_desc(media_type: str = CONFIG_BLOB_MEDIA_TYPE) -> Descriptor:
    return Descriptor(mediaType=media_type, digest="sha256:" + "1" * 64, size=20)


def _valid_manifest() -> OciManifest:
    return OciManifest(
        config=_config_desc(),
        layers=[_layer("layer-a")],
        annotations=dict(VALID_ANNOTATIONS),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_bundle_accepts_well_formed_bundle() -> None:
    result = validate_bundle(_valid_manifest(), VALID_CONFIG_BODY)
    assert result.ok is True
    assert result.errors == []
    assert result.config_blob is not None
    assert result.config_blob.project_id == "alpha-bank"


# ---------------------------------------------------------------------------
# Criterion (a) — manifest annotations
# ---------------------------------------------------------------------------


def test_criterion_a_missing_required_manifest_annotation() -> None:
    manifest = _valid_manifest()
    manifest.annotations.pop(CairnAnnotations.PROJECT_ID)
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert not result.ok
    codes = [e.criterion for e in result.errors]
    fields = [e.field for e in result.errors]
    assert "a" in codes
    assert any(CairnAnnotations.PROJECT_ID in f for f in fields)


def test_criterion_a_reports_every_missing_annotation_not_just_first() -> None:
    manifest = _valid_manifest()
    manifest.annotations.clear()
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    # All required identity annotations missing — [INGEST-017.07] surfaces ALL of them.
    a_errors = [e for e in result.errors if e.criterion == "a"]
    assert len(a_errors) == len(MANIFEST_REQUIRED)


# ---------------------------------------------------------------------------
# Criterion (b) — layer titles
# ---------------------------------------------------------------------------


def test_criterion_b_layer_missing_title_annotation() -> None:
    bad_layer = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "0" * 64,
        size=10,
        annotations={},
    )
    manifest = OciManifest(
        config=_config_desc(),
        layers=[bad_layer],
        annotations=dict(VALID_ANNOTATIONS),
    )
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert any(e.criterion == "b" for e in result.errors)


def test_criterion_b_reports_index_of_offending_layer() -> None:
    layers = [
        _layer("ok"),
        Descriptor(mediaType="application/json", digest="sha256:" + "0" * 64, size=1),
    ]
    manifest = OciManifest(
        config=_config_desc(), layers=layers, annotations=dict(VALID_ANNOTATIONS)
    )
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    b_errors = [e for e in result.errors if e.criterion == "b"]
    assert len(b_errors) == 1
    assert "layers[1]" in b_errors[0].field


# ---------------------------------------------------------------------------
# Criteria (i1, i2) — content-layer count bounds ([INGEST-003.04])
# ---------------------------------------------------------------------------


def _manifest_with_layers(layers: list[Descriptor]) -> OciManifest:
    """Build a manifest with an arbitrary layer list, bypassing the pydantic
    ``max_length`` field-level cap so the validator's own i2 descriptor-count
    check can be exercised independently (defense-in-depth)."""
    return OciManifest.model_construct(
        config=_config_desc(),
        layers=layers,
        annotations=dict(VALID_ANNOTATIONS),
    )


def test_criterion_i1_zero_layer_bundle_rejected() -> None:
    manifest = _manifest_with_layers([])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    i1 = [e for e in result.errors if e.criterion == "i1"]
    assert i1, result.errors
    assert i1[0].field == "manifest.layers"
    assert i1[0].message  # human-readable, non-empty
    # No too-many error when there are too few.
    assert not any(e.criterion == "i2" for e in result.errors)


def test_one_layer_bundle_passes_count_bound() -> None:
    manifest = _manifest_with_layers([_layer("only")])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert not any(e.criterion in {"i1", "i2"} for e in result.errors)


def test_max_layer_bundle_passes_count_bound() -> None:
    layers = [_layer(f"layer-{i}") for i in range(MAX_CONTENT_LAYERS)]
    manifest = _manifest_with_layers(layers)
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert not any(e.criterion in {"i1", "i2"} for e in result.errors)


def test_criterion_i2_too_many_layers_rejected() -> None:
    layers = [_layer(f"layer-{i}") for i in range(MAX_CONTENT_LAYERS + 1)]
    manifest = _manifest_with_layers(layers)
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    i2 = [e for e in result.errors if e.criterion == "i2"]
    assert i2, result.errors
    assert i2[0].field == "manifest.layers"
    # Message names the constraint (max) and the actual count.
    assert str(MAX_CONTENT_LAYERS) in i2[0].message
    assert str(MAX_CONTENT_LAYERS + 1) in i2[0].message
    assert not any(e.criterion == "i1" for e in result.errors)


# ---------------------------------------------------------------------------
# Criterion (j) — ADVISORY per-layer descriptor-size bound ([INGEST-003.04])
# ---------------------------------------------------------------------------


def test_criterion_j_layer_at_size_limit_accepted() -> None:
    at_limit = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "a" * 64,
        size=MAX_ARTIFACT_SIZE_BYTES,
        annotations={LAYER_REQUIRED_TITLE: "big.json"},
    )
    manifest = _manifest_with_layers([at_limit])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert not any(e.criterion == "j" for e in result.errors), result.errors


def test_criterion_j_oversized_layer_rejected_and_named() -> None:
    over = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "b" * 64,
        size=MAX_ARTIFACT_SIZE_BYTES + 1,
        annotations={LAYER_REQUIRED_TITLE: "huge.json"},
    )
    manifest = _manifest_with_layers([_layer("ok"), over])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    j = [e for e in result.errors if e.criterion == "j"]
    assert j, result.errors
    # Names the offending layer index AND digest in field/message.
    assert j[0].field == "manifest.layers[1].size"
    assert "sha256:" + "b" * 64 in j[0].message
    assert "[1]" in j[0].message


def test_criterion_j_collects_all_oversized_layers_not_just_first() -> None:
    """GAP: [INGEST-017.07] collect-all. When MULTIPLE layers are oversized, EVERY one
    is reported (not fail-fast on the first), each naming its own index+digest."""
    over0 = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "c" * 64,
        size=MAX_ARTIFACT_SIZE_BYTES + 1,
        annotations={LAYER_REQUIRED_TITLE: "huge-0.json"},
    )
    over2 = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "d" * 64,
        size=MAX_ARTIFACT_SIZE_BYTES + 999,
        annotations={LAYER_REQUIRED_TITLE: "huge-2.json"},
    )
    manifest = _manifest_with_layers([over0, _layer("ok-middle"), over2])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    j = [e for e in result.errors if e.criterion == "j"]
    assert len(j) == 2, result.errors
    fields = {e.field for e in j}
    assert fields == {"manifest.layers[0].size", "manifest.layers[2].size"}
    # Non-zero index is named correctly (not hard-coded to 0/1).
    j2 = next(e for e in j if e.field == "manifest.layers[2].size")
    assert "[2]" in j2.message
    assert "sha256:" + "d" * 64 in j2.message
    # The honest middle layer is NOT flagged.
    assert "manifest.layers[1].size" not in fields


def test_criterion_j_and_b_both_surface_on_same_layer() -> None:
    """GAP: criteria are collect-all ACROSS criterion families, not just within
    one. An oversized layer that ALSO lacks its required title surfaces BOTH the
    size (j) and the missing-title (b) error — neither masks the other."""
    over_and_untitled = Descriptor(
        mediaType="application/json",
        digest="sha256:" + "e" * 64,
        size=MAX_ARTIFACT_SIZE_BYTES + 1,
        annotations={},  # missing org.opencontainers.image.title -> criterion b
    )
    manifest = _manifest_with_layers([over_and_untitled])
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    codes = {e.criterion for e in result.errors}
    assert "j" in codes and "b" in codes, result.errors


# ---------------------------------------------------------------------------
# Criteria (c1, c2, c3, c4) — Config_Blob
# ---------------------------------------------------------------------------


def test_criterion_c1_config_descriptor_wrong_media_type() -> None:
    manifest = OciManifest(
        config=_config_desc("application/vnd.wrong"),
        layers=[_layer()],
        annotations=dict(VALID_ANNOTATIONS),
    )
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert any(e.criterion == "c1" for e in result.errors)


def test_criterion_c2_no_config_blob_body_supplied() -> None:
    result = validate_bundle(_valid_manifest(), None)
    assert any(e.criterion == "c2" for e in result.errors)


def test_criterion_c3_config_blob_is_not_json() -> None:
    result = validate_bundle(_valid_manifest(), b"not json {{{")
    assert any(e.criterion == "c3" for e in result.errors)


def test_criterion_c4_config_blob_missing_required_pydantic_fields() -> None:
    bad = json.dumps({}).encode("utf-8")  # missing required projectId
    result = validate_bundle(_valid_manifest(), bad)
    c4 = [e for e in result.errors if e.criterion == "c4"]
    assert c4
    # Should mention the missing required content-descriptive field.
    assert any("projectId" in e.field for e in c4)


# ---------------------------------------------------------------------------
# Criterion (d) — artifact-kind vocabulary, now CLAIM-sourced (validate_claim)
# ---------------------------------------------------------------------------


def test_criterion_d_artifact_kind_outside_vocabulary() -> None:
    # A token that resolves to NO canonical value fails criterion (d); the error
    # lists the allowed CANONICAL values, not the alias spellings.
    errors = validate_claim(_claim(artifact_kind="not-a-category"))
    d_errors = [e for e in errors if e.criterion == "d"]
    assert d_errors
    msg = d_errors[0].message
    assert "tool-output" in msg
    assert "plan-of-action-and-milestones" in msg
    assert "scan-reports" not in msg


def test_criterion_d_canonical_artifact_kind_accepted() -> None:
    errors = validate_claim(_claim(artifact_kind="attestation"))
    assert not [e for e in errors if e.criterion == "d"]


def test_criterion_d_alias_artifact_kind_resolves_and_passes() -> None:
    # An alias (canonicalizes on input) passes criterion (d) — the whole point
    # of normalize-before-membership.
    assert not [
        e for e in validate_claim(_claim(artifact_kind="attestations")) if e.criterion == "d"
    ]


def test_criterion_d_mixed_case_artifact_kind_rejected() -> None:
    # The membership gate routes through the exact-lowercase resolver, so a
    # mixed-case spelling of an otherwise-valid kind is a criterion (d) failure.
    errors = validate_claim(_claim(artifact_kind="Attestation"))
    d_errors = [e for e in errors if e.criterion == "d"]
    assert d_errors
    assert "Attestation" in d_errors[0].message
    assert d_errors[0].field == "attestation.artifactKind"


def test_criterion_d_surviving_alias_literal_resolves_and_passes() -> None:
    # Surviving back-compat aliases resolve to a canonical kind and pass
    # criterion (d).
    for alias in ("scan-reports", "config-snapshots", "policies", "poam"):
        assert not [e for e in validate_claim(_claim(artifact_kind=alias)) if e.criterion == "d"], (
            alias
        )


# ---------------------------------------------------------------------------
# OSCAL-bearing bundles. The OSCAL schema version is read from each layer's
# `metadata.oscal-version` during extraction ([OSCAL-001]), not by
# validate_bundle.
# ---------------------------------------------------------------------------


def _oscal_layer_manifest() -> OciManifest:
    """An OSCAL-bearing manifest (one application/oscal+json layer)."""
    oscal_layer = Descriptor(
        mediaType="application/oscal+json",
        digest="sha256:" + "2" * 64,
        size=10,
        annotations={LAYER_REQUIRED_TITLE: "ar.json"},
    )
    return OciManifest(
        config=_config_desc(),
        layers=[oscal_layer],
        annotations=dict(VALID_ANNOTATIONS),
    )


def test_oscal_bundle_is_accepted() -> None:
    result = validate_bundle(_oscal_layer_manifest(), VALID_CONFIG_BODY)
    assert result.ok, result.errors
    assert result.errors == []


def test_non_oscal_bundle_is_accepted() -> None:
    result = validate_bundle(_valid_manifest(), VALID_CONFIG_BODY)
    assert result.ok


# ---------------------------------------------------------------------------
# ValidationResult API
# ---------------------------------------------------------------------------


def test_validation_result_ok_is_false_when_errors_present() -> None:
    result = validate_bundle(_valid_manifest(), None)
    assert result.ok is False


def test_validation_result_raise_for_errors_raises_when_invalid() -> None:
    result = validate_bundle(_valid_manifest(), None)
    with pytest.raises(BundleValidationFailure) as excinfo:
        result.raise_for_errors()
    assert excinfo.value.errors == result.errors


def test_validation_result_raise_for_errors_no_op_when_ok() -> None:
    validate_bundle(_valid_manifest(), VALID_CONFIG_BODY).raise_for_errors()  # no raise


def test_validate_bundle_raise_on_error_flag() -> None:
    with pytest.raises(BundleValidationFailure):
        validate_bundle(_valid_manifest(), None, raise_on_error=True)


def test_bundle_validation_failure_message_concatenates_codes_and_fields() -> None:
    result = validate_bundle(_valid_manifest(), None)
    err = BundleValidationFailure(result.errors)
    assert "[c2]" in str(err)


# ---------------------------------------------------------------------------
# [INGEST-017.07] — completeness: accepts iff all criteria pass.
# ---------------------------------------------------------------------------


@given(
    annotation_to_drop=st.sampled_from(sorted(MANIFEST_REQUIRED)),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property4_dropping_any_required_annotation_invalidates(annotation_to_drop: str) -> None:
    manifest = _valid_manifest()
    manifest.annotations.pop(annotation_to_drop, None)
    result = validate_bundle(manifest, VALID_CONFIG_BODY)
    assert not result.ok
    assert any(annotation_to_drop in e.field for e in result.errors)


def test_property4_full_valid_input_accepts() -> None:
    """[INGEST-017.07] — the canonical valid bundle MUST be accepted."""
    assert validate_bundle(_valid_manifest(), VALID_CONFIG_BODY).ok


def test_property4_multiple_simultaneous_failures_are_all_reported() -> None:
    """[INGEST-017.07] — rejection MUST surface ALL failing criteria, not just first.

    Spans both entry points: drop an annotation (a) + break a layer (b) on the
    content manifest, AND a vocabulary failure (d) on the claim.
    """
    manifest = _valid_manifest()
    manifest.annotations.pop(CairnAnnotations.PROJECT_ID)
    bad_layer = Descriptor(mediaType="application/json", digest="sha256:" + "0" * 64, size=1)
    manifest.layers.append(bad_layer)
    bundle_errors = validate_bundle(manifest, VALID_CONFIG_BODY).errors
    claim_errors = validate_claim(_claim(artifact_kind="not-a-category"))
    codes = {e.criterion for e in (*bundle_errors, *claim_errors)}
    assert "a" in codes and "b" in codes and "d" in codes


# ---------------------------------------------------------------------------
# Control linkage is OPTIONAL — identity-only bundles/claims are accepted.
# ---------------------------------------------------------------------------


def _identity_only_manifest() -> OciManifest:
    """A reference-artifact manifest: identity annotations only."""
    return OciManifest(
        config=_config_desc(),
        layers=[_layer("catalog.json")],
        annotations={
            CairnAnnotations.PROJECT_ID: "alpha-bank",
        },
    )


IDENTITY_ONLY_CONFIG = json.dumps({"projectId": "alpha-bank"}).encode("utf-8")


def test_identity_only_bundle_is_accepted() -> None:
    # A reference artifact: identity-only content manifest + identity-only claim
    # (no controls, no catalogOrProfileUuid) is valid.
    result = validate_bundle(_identity_only_manifest(), IDENTITY_ONLY_CONFIG)
    assert result.ok, result.errors
    assert validate_claim(_claim(artifact_kind="other")) == []


def test_missing_identity_annotation_still_rejected() -> None:
    # The identity floor is still enforced: dropping project-id rejects.
    # (artifact-kind is not in the floor — kind is not in the content manifest.)
    manifest = _identity_only_manifest()
    manifest.annotations.pop(CairnAnnotations.PROJECT_ID)
    result = validate_bundle(manifest, IDENTITY_ONLY_CONFIG)
    assert not result.ok
    assert any(e.criterion == "a" and CairnAnnotations.PROJECT_ID in e.field for e in result.errors)


# ---------------------------------------------------------------------------
# Criterion (c5) — claim catalogOrProfileUuid validated ONLY when present.
# ---------------------------------------------------------------------------


def test_catalog_uuid_not_validated_when_absent() -> None:
    # Omitting catalogOrProfileUuid is fine even when controls are present.
    errors = validate_claim(_claim(artifact_kind="other", controls=["ac-2"]))
    assert errors == []


def test_catalog_uuid_validated_when_present_and_malformed() -> None:
    errors = validate_claim(_claim(artifact_kind="other", catalog_or_profile_uuid="not-a-uuid"))
    assert any(e.criterion == "c5" for e in errors)
    assert any(e.field == "attestation.catalogOrProfileUuid" for e in errors)


def test_catalog_uuid_well_formed_accepted() -> None:
    errors = validate_claim(
        _claim(
            artifact_kind="other",
            catalog_or_profile_uuid="22222222-2222-4222-8222-222222222222",
        )
    )
    assert not [e for e in errors if e.criterion == "c5"]


# ---------------------------------------------------------------------------
# Criterion (h) — claim control-id grammar enforced when controls present.
# ---------------------------------------------------------------------------


def test_criterion_h_well_formed_controls_accepted() -> None:
    errors = validate_claim(
        _claim(
            artifact_kind="other",
            catalog_or_profile_uuid="22222222-2222-4222-8222-222222222222",
            controls=["ac-2", "ac-2.1", "5.1"],
        )
    )
    assert errors == []


@pytest.mark.parametrize(
    "bad_id",
    ["", "ac/2", "ac#2", " ac", "ac 2", "AC", "ac--2", "ac-2\n"],
)
def test_criterion_h_malformed_control_id_rejected_and_named(bad_id: str) -> None:
    # A malformed control id is a PERMANENT failure that NAMES the offending id.
    # "ac-2\n" pins the fullmatch (not match) behavior — a trailing newline must
    # not slip past the $ anchor.
    errors = validate_claim(
        _claim(
            artifact_kind="other",
            catalog_or_profile_uuid="22222222-2222-4222-8222-222222222222",
            controls=["ac-2", bad_id],
        )
    )
    h_errors = [e for e in errors if e.criterion == "h"]
    assert h_errors, errors
    assert any(repr(bad_id) in e.message for e in h_errors)
    assert all(e.field == "attestation.controls" for e in h_errors)


def test_criterion_h_skipped_when_no_controls() -> None:
    errors = validate_claim(_claim(artifact_kind="other"))
    assert not any(e.criterion == "h" for e in errors)


def test_validate_claim_raise_on_error_flag() -> None:
    with pytest.raises(BundleValidationFailure):
        validate_claim(_claim(artifact_kind="not-a-category"), raise_on_error=True)
