"""Property-based tests for the oscal-interop capability.

Covers: [OSCAL-003.01] (OSCAL metadata extraction), [OSCAL-007.01] (OSCAL evidence reference
generation).
"""

from __future__ import annotations

import json
import uuid as _uuid

import pytest
from cairn_oscal.extraction import OscalLayerMetadata, extract_oscal_metadata
from cairn_oscal.reference import OCI_IMAGE_MANIFEST_MEDIA_TYPE, make_artifact_reference
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from oscal_bindings import OscalDoc
from oscal_bindings.models import (
    Metadata,
    SspControlImplementation,
    SspImplementedRequirement,
    SystemSecurityPlan,
    SystemSecurityPlanDocument,
)

OSCAL_VERSION = "1.2.1"
DOC_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def hex_digest(draw: st.DrawFn) -> str:
    return draw(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))


@st.composite
def malformed_digest(draw: st.DrawFn) -> str:
    # make_artifact_reference now requires the canonical PREFIXED `sha256:<64-hex>`
    # form, so "malformed" is anything that is not exactly that — both the
    # missing-prefix cases (incl. a bare 64-hex digest) and prefixed-but-malformed
    # bodies.
    return draw(
        st.one_of(
            st.just(""),
            st.just("a" * 64),  # bare hex, no prefix — the flipped contract rejects this
            st.just("a" * 63),
            st.just("A" * 64),  # uppercase, no prefix
            st.just("z" * 64),  # non-hex, no prefix
            st.text(min_size=1, max_size=63),
            st.just("sha256:" + "a" * 63),  # prefixed but too short
            st.just("sha256:" + "A" * 64),  # prefixed but uppercase
            st.just("sha256:"),  # prefix only
            st.just("sha512:" + "a" * 64),  # wrong algorithm
        )
    )


_CONTROL_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"


@st.composite
def control_id(draw: st.DrawFn) -> str:
    body = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz",
            min_size=2,
            max_size=4,
        )
    )
    seq = draw(st.text(alphabet="0123456789", min_size=1, max_size=3))
    return f"{body}-{seq}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ar_with_controls_json(control_ids: list[str], project_name: str = "Test AR") -> str:
    return json.dumps(
        {
            "assessment-results": {
                "uuid": DOC_UUID,
                "metadata": {
                    "title": project_name,
                    "last-modified": "2026-01-01T00:00:00Z",
                    "version": "1.0",
                    "oscal-version": OSCAL_VERSION,
                },
                "import-ap": {"href": "https://example.com/ap.json"},
                "results": [
                    {
                        "uuid": DOC_UUID,
                        "title": "R1",
                        "description": "first",
                        "start": "2025-02-01T00:00:00Z",
                        "end": "2025-02-15T00:00:00Z",
                        "reviewed-controls": {
                            "control-selections": [
                                {"include-controls": [{"control-id": cid} for cid in control_ids]}
                            ]
                            if control_ids
                            else [{"include-all": {}}]
                        },
                    }
                ],
            }
        }
    )


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
    control_impl = SspControlImplementation.model_construct(
        description="ci",
        implemented_requirements=requirements,
    )
    ssp = SystemSecurityPlan.model_construct(
        uuid=DOC_UUID,
        metadata=metadata,
        control_implementation=control_impl,
    )
    wrapper = SystemSecurityPlanDocument.model_construct(system_security_plan=ssp)
    return OscalDoc(wrapper)


# ---------------------------------------------------------------------------
# [OSCAL-003.01] — OSCAL metadata extraction
# ---------------------------------------------------------------------------


@given(
    control_ids=st.lists(control_id(), min_size=1, max_size=8, unique=True),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=15)
def test_property_15_ar_control_ids_deduped_and_sorted(control_ids: list[str]) -> None:
    """**WHEN** a valid OSCAL Assessment Results document is processed and its
    embedded `metadata.oscal-version` is a supported 1.x value **THEN** the
    metadata extraction function extracts system name, control references,
    assessment start date, assessment end date, Assessment Results UUID, SSP
    UUID, and the document-embedded `oscal-version`, and all extracted values
    match the values in the source document.

    Source: oscal-interop/spec.md:59-62 ([OSCAL-003.01] — AR control extraction:
    duplicates dedup, output is sorted).
    """
    # Inject duplicates to exercise the dedup invariant.
    duplicated = control_ids + control_ids[:2]
    doc = OscalDoc.from_json(_ar_with_controls_json(duplicated))
    meta = extract_oscal_metadata(doc)
    assert isinstance(meta, OscalLayerMetadata)
    assert meta.doc_type == "assessment-results"
    assert meta.doc_uuid == DOC_UUID
    assert meta.oscal_version == OSCAL_VERSION
    assert meta.control_references == sorted(set(control_ids))
    assert meta.control_references == sorted(meta.control_references)
    assert len(meta.control_references) == len(set(meta.control_references))


@given(control_ids=st.lists(control_id(), min_size=0, max_size=8, unique=True))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=15)
def test_property_15_ssp_control_extraction_deduped_and_sorted(control_ids: list[str]) -> None:
    """**WHEN** a valid OSCAL Assessment Results document is processed and its
    embedded `metadata.oscal-version` is a supported 1.x value **THEN** the
    metadata extraction function extracts system name, control references,
    assessment start date, assessment end date, Assessment Results UUID, SSP
    UUID ...

    Source: oscal-interop/spec.md:59-62 ([OSCAL-003.01] — SSP control extraction
    via implemented-requirements).
    """
    duplicated = control_ids + control_ids[:1] if control_ids else []
    doc = _ssp_doc(duplicated)
    meta = extract_oscal_metadata(doc)
    assert meta.doc_type == "system-security-plan"
    assert meta.control_references == sorted(set(control_ids))
    assert meta.assessment_period_start is None
    assert meta.assessment_period_end is None


@given(
    project_name=st.text(
        alphabet=st.characters(
            min_codepoint=0x20,
            max_codepoint=0x7E,
            blacklist_characters='"\\',
        ),
        min_size=1,
        max_size=80,
    ).filter(lambda s: s.strip() == s and s != ""),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=10)
def test_property_15_project_name_preserves_metadata_title(project_name: str) -> None:
    """**WHEN** a valid OSCAL Assessment Results document is processed and its
    embedded `metadata.oscal-version` is a supported 1.x value **THEN** ...
    extracts system name ... all extracted values
    match the values in the source document ...

    Source: oscal-interop/spec.md:59-62 ([OSCAL-003.01] — project_name is taken
    verbatim from metadata.title).
    """
    doc = OscalDoc.from_json(_ar_with_controls_json(["ac-1"], project_name=project_name))
    meta = extract_oscal_metadata(doc)
    assert meta.project_name == project_name


# ---------------------------------------------------------------------------
# [OSCAL-007.01] — OSCAL evidence reference generation
# ---------------------------------------------------------------------------


@given(
    digest=hex_digest(),
    ecr_repo=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-/", min_size=1, max_size=30),
    s3_bucket=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=3, max_size=30),
    s3_key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-/.", min_size=1, max_size=40),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=30)
def test_property_16_reference_carries_ecr_and_s3_rlinks_with_matching_digests(
    digest: str, ecr_repo: str, s3_bucket: str, s3_key: str
) -> None:
    """**WHEN** evidence bundle metadata (manifest digest, ECR repository, S3
    key prefix, project-id, controls, assessment-period, signing status, WORM
    status) is provided to the OSCAL reference generation function for a bundle
    that passed OSCAL validation **THEN** the function produces a valid OSCAL
    resource element containing rlink entries for both the OCI registry URI
    (ECR) and the S3 archive URI, each entry carries a SHA-256 hash that
    matches the Manifest_Digest, and the reference is emitted in the
    `artifact.validated` event payload.

    Source: oscal-interop/spec.md:92-95 ([OSCAL-007.01]).
    """
    ecr_uri = f"123.dkr.ecr.us-east-2.amazonaws.com/{ecr_repo}@sha256:{digest}"
    s3_uri = f"s3://{s3_bucket}/{s3_key}"
    resource = make_artifact_reference(
        # Builder takes the canonical PREFIXED digest; the rlink Hash value it
        # emits is the bare hex (asserted below).
        manifest_digest=f"sha256:{digest}",
        ecr_uri=ecr_uri,
        s3_uri=s3_uri,
    )
    assert resource.rlinks is not None
    assert len(resource.rlinks) == 2

    # The ECR rlink must come first, S3 second — the spec's "OCI registry URI
    # AND S3 archive URI" listing is order-fixed in the producer code.
    assert resource.rlinks[0].href == ecr_uri
    assert resource.rlinks[1].href == s3_uri

    for rlink in resource.rlinks:
        assert rlink.media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE
        assert rlink.hashes is not None
        assert len(rlink.hashes) == 1
        h = rlink.hashes[0]
        # Hash value must equal the supplied digest; algorithm must be SHA-256.
        assert h.value == digest
        # Algorithm enum membership — sha-256 is the canonical OSCAL form.
        assert "sha" in str(h.algorithm).lower()
        assert "256" in str(h.algorithm)


@given(bad_digest=malformed_digest())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=30)
def test_property_16_reference_rejects_malformed_manifest_digest(bad_digest: str) -> None:
    """**WHEN** evidence bundle metadata ... is provided to the OSCAL reference
    generation function for a bundle that passed OSCAL validation **THEN** the
    function produces a valid OSCAL resource element containing rlink entries
    ... each entry carries a SHA-256 hash that matches the Manifest_Digest ...

    Source: oscal-interop/spec.md:92-95 ([OSCAL-007.01] — negative space:
    malformed digest must raise so the produced reference can never silently
    carry a hash a consumer cannot verify against the bundle).
    """
    with pytest.raises(ValueError):
        make_artifact_reference(
            manifest_digest=bad_digest,
            ecr_uri="ecr-uri",
            s3_uri="s3://bucket/key",
        )


def test_property_16_resource_uuid_is_explicit_when_passed(_aws_env=None) -> None:
    """**WHEN** evidence bundle metadata ... is provided to the OSCAL reference
    generation function for a bundle that passed OSCAL validation **THEN** the
    function produces a valid OSCAL resource element ...

    Source: oscal-interop/spec.md:92-95 ([OSCAL-007.01] — explicit `resource_uuid`
    flows through verbatim; the auto-generated UUID branch is exercised in
    the existing unit tests).
    """
    digest = "sha256:" + "a" * 64
    explicit_uuid = str(_uuid.uuid4())
    resource = make_artifact_reference(
        manifest_digest=digest,
        ecr_uri="ecr-uri",
        s3_uri="s3://bucket/key",
        resource_uuid=explicit_uuid,
    )
    assert resource.uuid == explicit_uuid
