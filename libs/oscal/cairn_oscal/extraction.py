"""Per-layer OSCAL metadata extraction ([OSCAL-003]).

Produces a `OscalLayerMetadata` record per OSCAL layer for the
update-metadata Lambda to merge into the bundle's Metadata_Index entry.
The extractor pulls fields that exist uniformly across layouts via
`OscalDoc` (project name, doc UUID, OSCAL version, assessment period)
and walks per-doc-type structures for control references.

Walks are implemented for Assessment Results and System Security Plan
— the two supported OSCAL document types.
Assessment Plan, POA&M, Profile, Catalog, Component-Definition, and
Mapping-Collection bodies return an empty `control_references` list:
- AP: [OSCAL-003.01] doesn't list AP control extraction; deferred.
- Others: no GSI1 control-id query path is defined for these doc types.
Extend when a real use case lands. Per-doc-type control walks live here,
not in `oscal-bindings`.

When an Assessment Results body uses `include-all` instead of an
explicit `include-controls` list, `control_references` is left empty
AND `control_references_include_all` is set to True so downstream
metadata writers can store an "all controls" marker rather than treating
the empty list as "no controls." Without this flag, GSI1 queries by
control ID would silently miss `include-all`-scoped AR bundles.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Literal

from oscal_bindings import OscalDoc
from oscal_bindings.models import (
    AssessmentPlan,
    AssessmentResults,
    Catalog,
    ComponentDefinition,
    ControlSelectionWithIncludeControls,
    MappingCollection,
    PlanOfActionAndMilestones,
    Profile,
    SystemSecurityPlan,
)
from pydantic import BaseModel, Field

from cairn_oscal.errors import OscalExtractionError

OscalDocType = Literal[
    "catalog",
    "profile",
    "system-security-plan",
    "assessment-plan",
    "assessment-results",
    "plan-of-action-and-milestones",
    "component-definition",
    "mapping-collection",
]

_BODY_TO_DOC_TYPE: dict[type, OscalDocType] = {
    Catalog: "catalog",
    Profile: "profile",
    SystemSecurityPlan: "system-security-plan",
    AssessmentPlan: "assessment-plan",
    AssessmentResults: "assessment-results",
    PlanOfActionAndMilestones: "plan-of-action-and-milestones",
    ComponentDefinition: "component-definition",
    MappingCollection: "mapping-collection",
}


class OscalLayerMetadata(BaseModel):
    """Extracted metadata for one OSCAL layer of an Artifact_Bundle.

    `doc_version` is the document's OWN revision (`metadata.version`),
    deliberately distinct from `oscal_version` (the schema version the
    document conforms to). Two revisions of the same catalog differ in
    `doc_version`, not `oscal_version`. `metadata.version` is required by
    the OSCAL metadata schema, so it is always present for valid OSCAL.

    `last_modified` is the document's in-band `metadata.last-modified`
    timestamp — the canonical content-time source consumed by the
    per-OSCAL-layer temporal-consistency check and carried into the
    `artifact.validated` event. It is required by the OSCAL metadata
    schema, so it is always present for valid OSCAL. `published`
    (`metadata.published`) is optional in OSCAL; it is `None` when the
    document omits it. Reading either timestamp adds no validation gate.
    """

    doc_type: OscalDocType
    doc_uuid: str
    project_name: str
    doc_version: str
    oscal_version: str
    last_modified: datetime
    published: datetime | None = None
    control_references: list[str] = Field(default_factory=list)
    control_references_include_all: bool = False
    assessment_period_start: date | None = None
    assessment_period_end: date | None = None


def extract_oscal_metadata(doc: OscalDoc, *, layer_digest: str | None = None) -> OscalLayerMetadata:
    """Extract Metadata_Index-bound fields from one parsed OSCAL document.

    Project name is taken from `metadata.title`. The in-band
    `metadata.last-modified` and optional `metadata.published` timestamps
    are read from the same parsed document (no extra validation gate;
    `published` is left `None` when the document omits it). Control
    references are extracted for SSP and AR (other doc types yield `[]`).
    Assessment period is populated only for AP and AR bodies.

    `layer_digest` names the layer on a raised `OscalExtractionError`.

    Raises:
        OscalExtractionError(error_kind="unsupported_doc_type"): If the
            parsed body's type is outside the known OSCAL 1.x set. Today
            `_BODY_TO_DOC_TYPE` covers all 8; this guard is defense
            against future binding regenerations that introduce a new
            top-level body before this lib is updated.
    """
    body = doc.body
    body_type = type(body)
    try:
        doc_type = _BODY_TO_DOC_TYPE[body_type]
    except KeyError as e:
        raise OscalExtractionError(
            f"Unsupported OSCAL document body type: {body_type.__name__}",
            error_kind="unsupported_doc_type",
            layer_digest=layer_digest,
        ) from e

    control_references: list[str] = []
    include_all = False
    if isinstance(body, SystemSecurityPlan):
        control_references = _ssp_control_references(body)
    elif isinstance(body, AssessmentResults):
        control_references, include_all = _ar_control_references(body)

    assessment_start: date | None = None
    assessment_end: date | None = None
    if isinstance(body, (AssessmentPlan, AssessmentResults)):
        assessment_start, assessment_end = doc.assessment_period()

    return OscalLayerMetadata(
        doc_type=doc_type,
        doc_uuid=doc.uuid,
        project_name=doc.metadata.title,
        doc_version=doc.metadata.version,
        oscal_version=doc.oscal_version,
        last_modified=doc.metadata.last_modified,
        published=doc.metadata.published,
        control_references=control_references,
        control_references_include_all=include_all,
        assessment_period_start=assessment_start,
        assessment_period_end=assessment_end,
    )


def _ssp_control_references(ssp: SystemSecurityPlan) -> list[str]:
    """Pull control IDs from `control-implementation.implemented-requirements[]`."""
    return _dedup_sorted(
        req.control_id for req in ssp.control_implementation.implemented_requirements
    )


def _ar_control_references(ar: AssessmentResults) -> tuple[list[str], bool]:
    """Pull control IDs and an include-all marker from each result.

    Each `reviewed-controls.control-selections[]` entry is either
    `ControlSelectionWithIncludeControls` (with explicit
    `include-controls`) or a selection using `include-all`. The latter
    can only be resolved against the imported SSP/Profile, which is not
    in scope here. Bundles using `include-all` therefore set the boolean
    marker on the returned metadata so the writer doesn't store an empty
    list that misrepresents "all controls" as "no controls".
    """
    ids: list[str] = []
    include_all = False
    for result in ar.results:
        for selection in result.reviewed_controls.control_selections:
            if isinstance(selection, ControlSelectionWithIncludeControls):
                ids.extend(c.control_id for c in selection.include_controls)
            else:
                include_all = True
    return _dedup_sorted(ids), include_all


def _dedup_sorted(ids: Iterable[str]) -> list[str]:
    return sorted(set(ids))
