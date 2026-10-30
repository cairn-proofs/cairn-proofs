"""Property-based tests for opaque logical-id validation in the uniform
repository path grammar.

Covers: [INGEST-018] (the `{org-id}` / `{tenant-id}` / `{project-id}` opaque-id
validation facet, exercised through the `ArtifactLocator.repo_name` uniform
three-segment builder).

There is exactly one repository path shape —
`artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}` — and the
`{tenant-id}` segment is an opaque logical **tenant** that is never conflated
with an AWS account. These properties exercise the
lowercase-alphanumeric-and-hyphen 1..64 biconditional uniformly across all three
opaque segments.
"""

from __future__ import annotations

import re

from cairn_oci.categories import (
    ArtifactLocator,
    InvalidIdError,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_LOCATOR = ArtifactLocator()

# The opaque-id segment positions of the uniform path. The biconditional holds
# identically for each — no segment branches on a deployment mode.
_SEGMENTS = ("org_id", "tenant_id", "project_id")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Mirror the spec wording: lowercase alphanumeric and hyphens, 1..64 chars,
# no leading or trailing hyphen ([INGEST-018]).
_OPAQUE_ID_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?\Z")
_EDGE = "abcdefghijklmnopqrstuvwxyz0123456789"
_INNER = "abcdefghijklmnopqrstuvwxyz0123456789-"


@st.composite
def valid_opaque_id(draw: st.DrawFn) -> str:
    length = draw(st.integers(min_value=1, max_value=64))
    if length == 1:
        return draw(st.sampled_from(_EDGE))
    inner = draw(st.text(alphabet=_INNER, min_size=length - 2, max_size=length - 2))
    return draw(st.sampled_from(_EDGE)) + inner + draw(st.sampled_from(_EDGE))


@st.composite
def maybe_invalid_text(draw: st.DrawFn) -> str:
    """Arbitrary text that may or may not satisfy the opaque-id rule.

    Pulled from a broad printable space so the biconditional check (accept iff
    canonically valid) gets meaningful negative space alongside valid ids.
    """
    return draw(
        st.one_of(
            st.text(max_size=80),
            st.text(alphabet=_INNER, max_size=80),
            st.text(alphabet="-", min_size=1, max_size=10),
            st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", max_size=80),
            valid_opaque_id(),
            st.just(""),
            st.just("a" * 65),
            st.just("-leading"),
            st.just("trailing-"),
            st.just("under_score"),
            st.just("UPPER"),
            st.just("with space"),
            st.just("dot.notation"),
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_canonically_valid(value: str) -> bool:
    """Mirror the spec wording: lowercase alphanumeric and hyphens, 1..64,
    no leading or trailing hyphen ([INGEST-018])."""
    if not isinstance(value, str):
        return False
    if not (1 <= len(value) <= 64):
        return False
    return _OPAQUE_ID_RE.match(value) is not None


def _build_with_segment(position: str, value: str) -> str:
    """Build a repo path with ``value`` placed at the named opaque-id segment.

    The other two segments are held to known-valid values so the only segment
    that can drive acceptance/rejection is the one under test — the validators
    run in segment order (qualifier, org, tenant, project) and the fixed
    segments are valid, so the candidate is the sole failure point.
    """
    kwargs: dict[str, str] = {
        "deployment_qualifier": "cmh-alpha",
        "org_id": "org-default",
        "tenant_id": "tnt-default",
        "project_id": "prj-default",
    }
    kwargs[position] = value
    return _LOCATOR.repo_name(**kwargs)


# ---------------------------------------------------------------------------
# [INGEST-018] — Opaque-id validation facet (org/tenant/project segments)
# ---------------------------------------------------------------------------


@given(opaque_id=valid_opaque_id(), position=st.sampled_from(_SEGMENTS))
def test_property_17_valid_opaque_id_accepted_by_repo_path_builder(
    opaque_id: str, position: str
) -> None:
    """**WHEN** the opaque-id validation function is invoked with any input string
    on any of the `{org-id}` / `{tenant-id}` / `{project-id}` segments **THEN** it
    accepts the input if and only if it contains only lowercase alphanumeric
    characters and hyphens, is 1..64 characters long, and does not start or end
    with a hyphen; otherwise it rejects the input.

    Source: artifact-ingestion/spec.md ([INGEST-018] — positive space). Each of
    the three segments is validated by the same opaque-id grammar inside
    `cairn_oci.categories._validate_id`, exposed via `ArtifactLocator.repo_name`.
    """
    assert _is_canonically_valid(opaque_id)
    # Round-tripping a valid id through the repo-path builder MUST succeed and
    # place the id verbatim at its segment.
    path = _build_with_segment(position, opaque_id)
    assert f"/{opaque_id}/" in path or path.endswith(f"/{opaque_id}")


@given(candidate=maybe_invalid_text(), position=st.sampled_from(_SEGMENTS))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=80)
def test_property_17_opaque_id_biconditional_canonical_format(
    candidate: str, position: str
) -> None:
    """**WHEN** the opaque-id validation function is invoked with any input string
    on any of the three uniform-path segments **THEN** it accepts the input if and
    only if it contains only lowercase alphanumeric characters and hyphens, is
    1..64 characters long, and does not start or end with a hyphen.

    Source: artifact-ingestion/spec.md ([INGEST-018] — biconditional: the
    validator accepts iff the canonical-form rule passes, uniformly for the
    org/tenant/project segments).
    """
    canonically_valid = _is_canonically_valid(candidate)
    try:
        _build_with_segment(position, candidate)
        accepted = True
    except InvalidIdError:
        accepted = False

    assert accepted == canonically_valid, (
        f"opaque id {candidate!r} at segment {position!r}: validator "
        f"accepted={accepted} but canonical rule says valid={canonically_valid}"
    )


def test_property_17_twelve_digit_value_handled_as_opaque_tenant_id_not_aws_account() -> None:
    """**WHEN** a 12-digit numeric string equal to an AWS account id is supplied as
    the `{tenant-id}` segment **THEN** validation treats the value purely as an
    opaque logical tenant-id per the lowercase-alphanumeric-and-hyphen rule and
    does not conflate it with any AWS account identifier.

    Source: artifact-ingestion/spec.md ([INGEST-018] — `{tenant-id}` is a logical
    tenant, NOT an AWS account; a 12-digit value is just a valid opaque id).
    """
    twelve_digit = "123456789012"
    path = _LOCATOR.repo_name(
        deployment_qualifier="cmh-alpha",
        org_id="org-acme",
        tenant_id=twelve_digit,
        project_id="prj-bar",
    )
    assert f"/{twelve_digit}/" in path
