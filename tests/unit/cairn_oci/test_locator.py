"""Tests for cairn_oci.categories.ArtifactLocator.

Covers:
- [INGEST-018.01/.02] — repo_name construction for the single uniform
  three-segment path ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}``.
- [INGEST-018.03] — the producing identity is never a path segment (colocation):
  the path is a pure function of (org, tenant, project).
- [INGEST-021.09] — canonical tag build/parse round-trip
  ``{artifact-kind}--_ref--{digest}`` (single 3-segment form).
- Fail-closed tag parsing: empty, no-delimiter, wrong-arity,
  out-of-vocabulary leading segment.
- The category-layout machinery (RepoLayout, CATEGORY_IN_PATH, the
  service_hosted_repo_path / customer_hosted_repo_path wrappers) is removed
  (D.14) — these tests assert it is gone.
"""

from __future__ import annotations

import pytest
from cairn_oci.categories import (
    ARTIFACT_KINDS,
    TAG_FORM_REF,
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidIdError,
    InvalidTagError,
    ParsedTag,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A valid 12-char lowercase-hex digest prefix used across the canonical-tag tests.
_DIGEST = "0123456789ab"

# ---------------------------------------------------------------------------
# The category-layout machinery is removed
# ---------------------------------------------------------------------------


def test_locator_takes_no_layout_argument() -> None:
    # The locator takes no layout parameter — there is exactly one
    # repository path shape.
    with pytest.raises(TypeError):
        ArtifactLocator("category-in-path")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# repo_name — the single uniform three-segment path ([INGEST-018])
# ---------------------------------------------------------------------------


def test_repo_name_uniform_three_segment_path() -> None:
    loc = ArtifactLocator()
    name = loc.repo_name(
        deployment_qualifier="cmh-alpha",
        org_id="org-acme",
        tenant_id="tnt-alpha-bank",
        project_id="prj-controls",
    )
    assert name == "artifact-cmh-alpha/org-acme/tnt-alpha-bank/prj-controls"
    # Exactly three logical-hierarchy segments after the qualifier prefix.
    assert name.count("/") == 3


def test_repo_name_holds_no_artifact_kind_segment() -> None:
    # [INGEST-018]: a single repository holds every artifact-kind for a project;
    # the kind is never a path segment. repo_name takes no artifact_kind at all.
    loc = ArtifactLocator()
    with pytest.raises(TypeError):
        loc.repo_name(  # type: ignore[call-arg]
            deployment_qualifier="cmh-alpha",
            org_id="org-acme",
            tenant_id="tnt-alpha-bank",
            project_id="prj-controls",
            artifact_kind="assessment-results",
        )


def test_repo_name_rejects_invalid_qualifier() -> None:
    with pytest.raises(InvalidIdError):
        ArtifactLocator().repo_name(
            deployment_qualifier="badqualifier",
            org_id="org-acme",
            tenant_id="tnt-a",
            project_id="prj-a",
        )


@pytest.mark.parametrize("field", ["org_id", "tenant_id", "project_id"])
def test_repo_name_rejects_invalid_segment(field: str) -> None:
    kwargs = {
        "deployment_qualifier": "cmh-alpha",
        "org_id": "org-acme",
        "tenant_id": "tnt-a",
        "project_id": "prj-a",
    }
    kwargs[field] = "-leading-hyphen"
    with pytest.raises(InvalidIdError):
        ArtifactLocator().repo_name(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["org_id", "tenant_id", "project_id"])
def test_repo_name_rejects_uppercase_segment(field: str) -> None:
    kwargs = {
        "deployment_qualifier": "cmh-alpha",
        "org_id": "org-acme",
        "tenant_id": "tnt-a",
        "project_id": "prj-a",
    }
    kwargs[field] = "UPPER"
    with pytest.raises(InvalidIdError):
        ArtifactLocator().repo_name(**kwargs)  # type: ignore[arg-type]


def test_invalid_id_error_carries_field_and_value() -> None:
    try:
        ArtifactLocator().repo_name(
            deployment_qualifier="cmh-alpha",
            org_id="BAD",
            tenant_id="tnt-a",
            project_id="prj-a",
        )
    except InvalidIdError as e:
        assert e.field == "org-id"
        assert e.value == "BAD"
    else:
        raise AssertionError("expected InvalidIdError")


# ---------------------------------------------------------------------------
# [INGEST-018.03] — producing identity is not a path segment (colocation)
# ---------------------------------------------------------------------------


def test_repo_name_is_pure_function_of_org_tenant_project() -> None:
    # Two bundles for the same {org, tenant, project} land in the identical
    # repository regardless of producing identity — the producing AWS account /
    # cloud / host is recorded per-artifact, never as a path segment (D.5).
    loc = ArtifactLocator()
    first = loc.repo_name(
        deployment_qualifier="cmh-alpha",
        org_id="org-acme",
        tenant_id="tnt-bank",
        project_id="prj-controls",
    )
    second = loc.repo_name(
        deployment_qualifier="cmh-alpha",
        org_id="org-acme",
        tenant_id="tnt-bank",
        project_id="prj-controls",
    )
    assert first == second


# ---------------------------------------------------------------------------
# build_ref_tag — single canonical form (layout machinery removed)
# ---------------------------------------------------------------------------


def test_build_ref_tag_emits_canonical_form() -> None:
    tag = ArtifactLocator().build_ref_tag(artifact_kind="scan-reports", digest=_DIGEST)
    assert tag == f"scan-reports--_ref--{_DIGEST}"


def test_build_ref_tag_rejects_unknown_category() -> None:
    with pytest.raises(InvalidArtifactKindError):
        ArtifactLocator().build_ref_tag(artifact_kind="made-up", digest=_DIGEST)


# ---------------------------------------------------------------------------
# parse_tag — fail-closed
# ---------------------------------------------------------------------------


def test_parse_tag_round_trip() -> None:
    parsed = ArtifactLocator().parse_tag(f"scan-reports--_ref--{_DIGEST}")
    assert parsed == ParsedTag(
        artifact_kind="scan-reports",
        name=None,
        digest=_DIGEST,
        form=TAG_FORM_REF,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "garbage-no-delimiter",  # no `--`
        f"catalog--_ref--n--{_DIGEST}",  # 4 segments: legacy name-bearing form (hard cut)
        f"general--_ref--n--extra--{_DIGEST}",  # 5 segments
        f"not-a-category--_ref--{_DIGEST}",  # out-of-vocabulary leading segment
        f"catalog--2026-q1--{_DIGEST}",  # non-reserved middle segment
        "catalog--_ref--not-a-digest",  # malformed digest terminal
        f"catalog--_ref--{_DIGEST.upper()}",  # uppercase digest
        "catalog--_ref--0123456789a",  # digest too short
        "catalog--_ref",  # 2 segments (missing digest)
    ],
)
def test_parse_tag_fail_closed(bad: str) -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag(bad)


def test_invalid_tag_error_carries_offending_tag() -> None:
    bad = f"not-a-category--_ref--{_DIGEST}"
    try:
        ArtifactLocator().parse_tag(bad)
    except InvalidTagError as e:
        assert e.tag == bad
    else:
        raise AssertionError("expected InvalidTagError")


# ---------------------------------------------------------------------------
# [INGEST-018.02] — repo_name construction (hypothesis)
# ---------------------------------------------------------------------------

_valid_id_chars = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=1)
_valid_id_body = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=0, max_size=62)


@st.composite
def valid_id(draw: st.DrawFn) -> str:
    head = draw(_valid_id_chars)
    body = draw(_valid_id_body)
    if not body:
        return head
    tail = draw(_valid_id_chars)
    middle = body[:-1] if body.endswith("-") else body
    candidate = head + middle + tail
    return candidate[:64]


_alnum_segment = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)
valid_qualifier = st.builds(lambda a, b: f"{a}-{b}", _alnum_segment, _alnum_segment)
valid_category = st.sampled_from(sorted(ARTIFACT_KINDS))


@given(
    qualifier=valid_qualifier,
    org=valid_id(),
    tenant=valid_id(),
    project=valid_id(),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_repo_name_uniform_path(
    qualifier: str, org: str, tenant: str, project: str
) -> None:
    name = ArtifactLocator().repo_name(
        deployment_qualifier=qualifier,
        org_id=org,
        tenant_id=tenant,
        project_id=project,
    )
    assert name == f"artifact-{qualifier}/{org}/{tenant}/{project}"
    head, *segments = name.split("/")
    assert head == f"artifact-{qualifier}"
    assert segments == [org, tenant, project]


# ---------------------------------------------------------------------------
# [INGEST-021.09] — canonical tag round-trip over the kind/digest cross-product.
# ---------------------------------------------------------------------------

_valid_digest = st.from_regex(r"\A[0-9a-f]{12}\Z", fullmatch=True)


@given(
    category=valid_category,
    digest=_valid_digest,
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_canonical_tag_round_trip(category: str, digest: str) -> None:
    """[INGEST-021.09] — build_ref_tag → parse_tag recovers every component.

    The single canonical form is ``{kind}--_ref--{digest}``; parse recovers
    exactly ``(kind, None, digest)`` — there is no name segment.
    """
    loc = ArtifactLocator()
    tag = loc.build_ref_tag(artifact_kind=category, digest=digest)
    assert len(tag) <= 128
    parsed = loc.parse_tag(tag)
    assert parsed == ParsedTag(
        artifact_kind=category,
        name=None,
        digest=digest,
        form=TAG_FORM_REF,
    )
