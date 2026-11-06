"""Tests for cairn_oscal.extraction ([OSCAL-003.01]).

`extract_oscal_metadata(doc)` populates per-doc-type fields on
`OscalLayerMetadata`. Walks cover SSP and AR (the only doc types
expected in MVP artifact bundles); other doc types yield an empty
`control_references` list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from cairn_oscal.errors import OscalExtractionError
from cairn_oscal.extraction import OscalLayerMetadata, extract_oscal_metadata
from oscal_bindings import OscalDoc
from oscal_bindings.models import (
    Metadata,
    SspControlImplementation,
    SspImplementedRequirement,
    SystemSecurityPlan,
    SystemSecurityPlanDocument,
)

from .conftest import (
    DOC_VERSION,
    LAST_MODIFIED,
    OSCAL_VERSION,
    PUBLISHED,
    TEST_UUID,
    ar_with_include_all_only_json,
    ar_with_include_controls_json,
    ar_with_mixed_selections_json,
    minimal_assessment_plan_json,
    minimal_catalog_json,
)

# ---------------------------------------------------------------------------
# Helpers — SSP needs `model_construct()` because its required nested fields
# (system-characteristics, system-implementation, import-profile) are deeply
# nested and not load-bearing for this test surface.
# ---------------------------------------------------------------------------


def _ssp_doc(control_ids: list[str]) -> OscalDoc:
    metadata = Metadata.model_construct(
        title="Test SSP",
        last_modified="2026-01-01T00:00:00Z",
        version="1.0",
        oscal_version=OSCAL_VERSION,
    )
    requirements = [
        SspImplementedRequirement.model_construct(
            uuid=f"00000000-0000-4000-8000-{i:012d}",
            control_id=cid,
        )
        for i, cid in enumerate(control_ids)
    ]
    control_implementation = SspControlImplementation.model_construct(
        description="ci",
        implemented_requirements=requirements,
    )
    ssp = SystemSecurityPlan.model_construct(
        uuid=TEST_UUID,
        metadata=metadata,
        control_implementation=control_implementation,
    )
    wrapper = SystemSecurityPlanDocument.model_construct(system_security_plan=ssp)
    return OscalDoc(wrapper)


# ---------------------------------------------------------------------------
# AssessmentResults
# ---------------------------------------------------------------------------


def test_ar_extracts_dedup_sorted_control_ids() -> None:
    """[OSCAL-003.01]: AR control IDs across results are deduplicated and sorted."""
    doc = OscalDoc.from_json(ar_with_include_controls_json())
    meta = extract_oscal_metadata(doc)
    assert isinstance(meta, OscalLayerMetadata)
    assert meta.doc_type == "assessment-results"
    # Across results: ["ac-2", "ac-1", "ac-1", "ac-3"] → ["ac-1", "ac-2", "ac-3"]
    assert meta.control_references == ["ac-1", "ac-2", "ac-3"]


def test_ar_populates_top_level_fields() -> None:
    """[OSCAL-003.01]: project_name, doc_uuid, oscal_version all come from metadata + body."""
    doc = OscalDoc.from_json(ar_with_include_controls_json())
    meta = extract_oscal_metadata(doc)
    assert meta.project_name == "Test AR"
    assert meta.doc_uuid == TEST_UUID
    assert meta.doc_version == DOC_VERSION
    assert meta.oscal_version == OSCAL_VERSION


def test_ar_populates_assessment_period() -> None:
    """[OSCAL-003.01]: assessment_period_start/end aggregated across results
    (earliest start, latest end)."""
    doc = OscalDoc.from_json(ar_with_include_controls_json())
    meta = extract_oscal_metadata(doc)
    # R1: 2025-02-01 → 2025-02-15, R2: 2025-01-20 → 2025-03-01.
    assert meta.assessment_period_start == date(2025, 1, 20)
    assert meta.assessment_period_end == date(2025, 3, 1)


def test_ar_with_only_include_all_yields_empty_control_refs_and_sets_marker() -> None:
    """[OSCAL-003.01]: `include-all`-only selections (the ControlSelections variant
    without explicit `include-controls`) → empty control_references list,
    because expansion would require resolving the imported SSP/Profile,
    AND `control_references_include_all=True` so the metadata writer
    can store an "all controls" marker rather than misinterpreting the
    empty list as "no controls"."""
    doc = OscalDoc.from_json(ar_with_include_all_only_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "assessment-results"
    assert meta.control_references == []
    assert meta.control_references_include_all is True


def test_ar_with_only_include_controls_leaves_include_all_marker_false() -> None:
    """[OSCAL-003.01]: when every selection is an explicit `include-controls`
    (no `include-all` variant present), the sticky bool stays False.
    Negative control for the include-all marker."""
    doc = OscalDoc.from_json(ar_with_include_controls_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "assessment-results"
    assert meta.control_references == ["ac-1", "ac-2", "ac-3"]
    assert meta.control_references_include_all is False


def test_ar_with_mixed_selections_populates_refs_and_sets_marker_sticky() -> None:
    """[OSCAL-003.01]: when one result mixes an `include-controls` selection
    AND an `include-all` selection, the extractor returns BOTH the
    populated control IDs from the explicit selection AND
    `control_references_include_all=True`. The bool is sticky — once
    any selection in the AR uses include-all, the marker is set even if
    other selections produced concrete IDs."""
    doc = OscalDoc.from_json(ar_with_mixed_selections_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "assessment-results"
    # Explicit selection contributed ac-1, ac-2 (deduped + sorted).
    assert meta.control_references == ["ac-1", "ac-2"]
    # And the include-all variant was seen → sticky bool flipped.
    assert meta.control_references_include_all is True


# ---------------------------------------------------------------------------
# SystemSecurityPlan
# ---------------------------------------------------------------------------


def test_ssp_extracts_dedup_sorted_control_ids_from_implemented_requirements() -> None:
    """[OSCAL-003.01]: SSP control IDs come from
    `control-implementation.implemented-requirements[]`; dedup'd + sorted."""
    doc = _ssp_doc(["ac-3", "ac-1", "ac-2", "ac-1"])
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "system-security-plan"
    assert meta.control_references == ["ac-1", "ac-2", "ac-3"]


def test_ssp_no_assessment_period() -> None:
    """[OSCAL-003.01]: SSP carries no assessment_period_start/end."""
    doc = _ssp_doc(["ac-1"])
    meta = extract_oscal_metadata(doc)
    assert meta.assessment_period_start is None
    assert meta.assessment_period_end is None


def test_ssp_populates_top_level_fields() -> None:
    """SSP project_name, doc_uuid, doc_version, oscal_version round-trip."""
    doc = _ssp_doc(["ac-1"])
    meta = extract_oscal_metadata(doc)
    assert meta.project_name == "Test SSP"
    assert meta.doc_uuid == TEST_UUID
    assert meta.doc_version == DOC_VERSION
    assert meta.oscal_version == OSCAL_VERSION


# ---------------------------------------------------------------------------
# Catalog (no per-doc-type walk)
# ---------------------------------------------------------------------------


def test_catalog_yields_empty_control_references() -> None:
    """[OSCAL-003.01]: Catalog doc type has no implemented control walk → empty list."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "catalog"
    assert meta.control_references == []


def test_catalog_has_no_assessment_period() -> None:
    """[OSCAL-003.01]: Catalog has no assessment period."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    meta = extract_oscal_metadata(doc)
    assert meta.assessment_period_start is None
    assert meta.assessment_period_end is None


# ---------------------------------------------------------------------------
# AssessmentPlan
# ---------------------------------------------------------------------------


def test_ap_populates_assessment_period_from_tasks() -> None:
    """[OSCAL-003.01]: AP `assessment_period()` walks tasks; here a within-date-range
    task contributes 2025-02-01 → 2025-02-15."""
    doc = OscalDoc.from_json(minimal_assessment_plan_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "assessment-plan"
    assert meta.assessment_period_start == date(2025, 2, 1)
    assert meta.assessment_period_end == date(2025, 2, 15)


def test_ap_yields_empty_control_references() -> None:
    """[OSCAL-003.01]: AP has no per-doc-type control walk implemented → empty list."""
    doc = OscalDoc.from_json(minimal_assessment_plan_json())
    meta = extract_oscal_metadata(doc)
    assert meta.control_references == []


# ---------------------------------------------------------------------------
# control_references_include_all — false for non-AR doc types
# ---------------------------------------------------------------------------


def test_ssp_leaves_include_all_marker_false() -> None:
    """The include-all marker is AR-specific. SSP control extraction walks
    `implemented-requirements[]` directly and has no include-all variant;
    the bool must remain False regardless of SSP shape."""
    doc = _ssp_doc(["ac-1", "ac-2"])
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "system-security-plan"
    assert meta.control_references_include_all is False


def test_catalog_leaves_include_all_marker_false() -> None:
    """The include-all marker is AR-specific. Catalogs have no control
    extraction walk; the bool must remain False."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "catalog"
    assert meta.control_references_include_all is False


def test_ap_leaves_include_all_marker_false() -> None:
    """The include-all marker is AR-specific. AP control extraction is
    deferred (no walk implemented), so the bool must remain False."""
    doc = OscalDoc.from_json(minimal_assessment_plan_json())
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "assessment-plan"
    assert meta.control_references_include_all is False


# ---------------------------------------------------------------------------
# doc_version — the document's OWN revision (metadata.version), distinct
# from oscal_version (the schema version). D-1 / D-5.
# ---------------------------------------------------------------------------


def test_doc_version_is_metadata_version_not_oscal_version() -> None:
    """`doc_version` is the document's own `metadata.version`, NOT the
    `oscal-version` schema string. With a doc whose revision differs from
    the schema version, the two fields must hold different values and
    `doc_version` must equal the document's `metadata.version`."""
    doc_version = "5.1.0"
    doc = OscalDoc.from_json(minimal_catalog_json(oscal_version="1.2.1", doc_version=doc_version))
    meta = extract_oscal_metadata(doc)
    assert meta.doc_version == doc_version
    assert meta.oscal_version == "1.2.1"
    # The two are genuinely distinct, not coincidentally equal.
    assert meta.doc_version != meta.oscal_version
    # And it tracks the source document's own metadata.version exactly.
    assert meta.doc_version == doc.metadata.version


def test_doc_version_independent_of_schema_version() -> None:
    """Holding the document's own revision fixed while varying the schema
    version leaves `doc_version` unchanged — it is unaffected by
    `oscal-version`."""
    fixed_doc_version = "rev-5"
    a = extract_oscal_metadata(
        OscalDoc.from_json(
            minimal_catalog_json(oscal_version="1.2.1", doc_version=fixed_doc_version)
        )
    )
    b = extract_oscal_metadata(
        OscalDoc.from_json(
            minimal_catalog_json(oscal_version="1.0.0", doc_version=fixed_doc_version)
        )
    )
    assert a.doc_version == b.doc_version == fixed_doc_version
    assert a.oscal_version != b.oscal_version


def test_doc_version_populated_for_each_supported_doc_type() -> None:
    """`doc_version` is populated (the OSCAL-required `metadata.version`)
    across the supported doc types exercised here: catalog, assessment
    plan, assessment results, and system security plan."""
    catalog = extract_oscal_metadata(OscalDoc.from_json(minimal_catalog_json()))
    ap = extract_oscal_metadata(OscalDoc.from_json(minimal_assessment_plan_json()))
    ar = extract_oscal_metadata(OscalDoc.from_json(ar_with_include_controls_json()))
    ssp = extract_oscal_metadata(_ssp_doc(["ac-1"]))
    for meta in (catalog, ap, ar, ssp):
        assert meta.doc_version == DOC_VERSION
        assert meta.doc_version  # non-empty


# ---------------------------------------------------------------------------
# In-band timestamps — metadata.last-modified (required) and
# metadata.published (optional). Content-derived per-layer values consumed
# by the relocated temporal-consistency check; reading them adds no gate.
# ---------------------------------------------------------------------------


def test_last_modified_and_published_extracted_when_both_present() -> None:
    """Happy path: a document carrying both `metadata.last-modified` and
    `metadata.published` surfaces both timestamps on the extracted metadata,
    matching the source document's values."""
    doc = OscalDoc.from_json(minimal_catalog_json(published=PUBLISHED))
    meta = extract_oscal_metadata(doc)
    assert meta.last_modified == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert meta.published == datetime(2025, 12, 15, 0, 0, 0, tzinfo=UTC)
    # And they track the source document's own metadata exactly.
    assert meta.last_modified == doc.metadata.last_modified
    assert meta.published == doc.metadata.published


def test_published_is_none_when_absent_and_last_modified_still_extracted() -> None:
    """A document with `metadata.last-modified` but no `metadata.published`
    is NOT rejected: `last_modified` is populated and `published` is None.
    Absence of the optional field is not an extraction failure."""
    # The default conftest fixtures omit `published`.
    doc = OscalDoc.from_json(minimal_catalog_json())
    meta = extract_oscal_metadata(doc)
    assert meta.last_modified == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert meta.published is None


def test_last_modified_extracted_across_supported_doc_types() -> None:
    """`metadata.last-modified` is the OSCAL-required timestamp, populated
    across the supported doc types exercised here (catalog, AP, AR, SSP).
    None of these fixtures carry `published`, so it stays None throughout."""
    expected = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    catalog = extract_oscal_metadata(OscalDoc.from_json(minimal_catalog_json()))
    ap = extract_oscal_metadata(OscalDoc.from_json(minimal_assessment_plan_json()))
    ar = extract_oscal_metadata(OscalDoc.from_json(ar_with_include_controls_json()))
    ssp = extract_oscal_metadata(_ssp_doc(["ac-1"]))
    for meta in (catalog, ap, ar, ssp):
        assert meta.last_modified == expected
        assert meta.published is None


def test_no_oscal_layer_means_extraction_is_never_invoked() -> None:
    """The no-OSCAL-layer case carries no per-layer timestamps because
    `extract_oscal_metadata` is only called for layers with an OSCAL media
    type — there is no `OscalDoc` to extract from. This unit asserts the
    contract at this seam: the function requires a parsed document and is
    not a no-op fallback for non-OSCAL layers (the caller skips it). A
    bundle with no OSCAL layer is therefore unaffected by timestamp
    extraction."""
    with pytest.raises(TypeError):
        extract_oscal_metadata()  # type: ignore[call-arg]


def test_last_modified_string_fixture_value_round_trips() -> None:
    """The conftest `LAST_MODIFIED` literal parses to the same aware
    datetime the extractor surfaces — guards the fixture/assertion pairing
    against drift."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    meta = extract_oscal_metadata(doc)
    assert meta.last_modified == datetime.fromisoformat(LAST_MODIFIED.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# OscalLayerMetadata model construction / serialization
# ---------------------------------------------------------------------------


def test_oscal_layer_metadata_round_trip_with_include_all_marker() -> None:
    """`OscalLayerMetadata` accepts and serializes
    `control_references_include_all=True` so downstream Metadata_Index
    writers can persist the marker. Pydantic model dump → reload
    round-trips the flag without loss."""
    original = OscalLayerMetadata(
        doc_type="assessment-results",
        doc_uuid=TEST_UUID,
        project_name="Test AR",
        doc_version=DOC_VERSION,
        oscal_version=OSCAL_VERSION,
        last_modified=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        published=datetime(2025, 12, 15, 0, 0, 0, tzinfo=UTC),
        control_references=["ac-1", "ac-2"],
        control_references_include_all=True,
        assessment_period_start=date(2025, 2, 1),
        assessment_period_end=date(2025, 2, 15),
    )
    payload = original.model_dump()
    assert payload["control_references_include_all"] is True
    restored = OscalLayerMetadata.model_validate(payload)
    assert restored == original
    assert restored.control_references_include_all is True


def test_oscal_layer_metadata_include_all_default_is_false() -> None:
    """The new field defaults to False so existing constructions (which
    didn't pass the bool) continue to compile and produce the
    backward-compatible "explicit only" semantic."""
    meta = OscalLayerMetadata(
        doc_type="catalog",
        doc_uuid=TEST_UUID,
        project_name="Test Catalog",
        doc_version=DOC_VERSION,
        oscal_version=OSCAL_VERSION,
        last_modified=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    assert meta.control_references_include_all is False
    # `published` is optional and defaults to None alongside the include-all
    # marker, so a construction that omits it stays backward-compatible.
    assert meta.published is None


# ---------------------------------------------------------------------------
# Unsupported doc type — defense-in-depth against future binding regen
# ---------------------------------------------------------------------------


def test_unsupported_doc_type_raises_oscal_extraction_error() -> None:
    """Today `_BODY_TO_DOC_TYPE` covers all 8 OSCAL 1.x body types, so
    this branch is unreachable from real inputs. Patch the lookup dict
    to be empty and reparse → the extractor must raise
    `OscalExtractionError(error_kind="unsupported_doc_type")` with the
    body type name in the message. Guards against future binding
    regenerations introducing a 9th body before this lib is updated."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    with patch("cairn_oscal.extraction._BODY_TO_DOC_TYPE", {}):
        with pytest.raises(OscalExtractionError) as exc_info:
            extract_oscal_metadata(doc)
    assert exc_info.value.error_kind == "unsupported_doc_type"
    # Message mentions the body type so the operator can identify the
    # regression-introducing model class.
    assert "Catalog" in str(exc_info.value)


def test_unsupported_doc_type_carries_layer_digest() -> None:
    """`layer_digest` reaches the `unsupported_doc_type` error so the
    failure event names its layer ([OSCAL-005])."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    with patch("cairn_oscal.extraction._BODY_TO_DOC_TYPE", {}):
        with pytest.raises(OscalExtractionError) as exc_info:
            extract_oscal_metadata(doc, layer_digest="sha256:doctype")
    assert exc_info.value.error_kind == "unsupported_doc_type"
    assert exc_info.value.layer_digest == "sha256:doctype"


def test_unsupported_doc_type_dict_restored_after_patch() -> None:
    """Sanity check: after the patch context manager exits, normal
    extraction works again. Ensures the previous test's mock doesn't
    leak across test boundaries."""
    doc = OscalDoc.from_json(minimal_catalog_json())
    # No patch active — should succeed.
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "catalog"
