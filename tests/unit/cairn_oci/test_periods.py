"""Tests for the assessment-period resolver (minting-time, evidence-only).

The tag is the *output* of minting, not an input, so no period is ever read
back from a tag. Reference-form artifacts have no period coordinate
and bypass the resolver entirely.
"""

import inspect

import pytest
from cairn_oci.periods import resolve_assessment_period


def test_config_present_is_used() -> None:
    assert resolve_assessment_period("2026-q1", "2024-01-01T00:00:00Z") == "2026-q1"


def test_config_absent_falls_back_to_created_date() -> None:
    assert resolve_assessment_period(None, "2026-05-30T14:22:00Z") == "2026-05-30"


def test_created_bare_date_passes_through() -> None:
    assert resolve_assessment_period(None, "2026-05-30") == "2026-05-30"


@pytest.mark.parametrize(
    ("config_period", "created", "expected"),
    [
        ("", "2026-05-30T00:00:00Z", "2026-05-30"),  # empty config treated as absent
        ("2026-q1", "", "2026-q1"),  # empty created irrelevant when config present
    ],
)
def test_empty_string_treated_as_absent(
    config_period: str | None,
    created: str | None,
    expected: str,
) -> None:
    assert resolve_assessment_period(config_period, created) == expected


def test_reference_form_bypasses_resolution() -> None:
    # Even with a fully resolvable evidence record, a reference-form artifact has
    # no period coordinate and must short-circuit to None before any source.
    assert resolve_assessment_period("2026-q1", "2026-05-30T14:22:00Z", is_reference=True) is None


def test_reference_form_bypasses_even_with_no_sources() -> None:
    # Reference bypass takes precedence over the all-unusable ValueError path.
    assert resolve_assessment_period(None, None, is_reference=True) is None


@pytest.mark.parametrize(
    ("config_period", "created"),
    [
        (None, None),  # nothing supplied at all
        ("", ""),  # all empty strings
        (None, "T14:22:00Z"),  # created with an empty date portion
        ("", "T"),  # bare separator, no date
    ],
)
def test_no_usable_source_raises_on_evidence_path(
    config_period: str | None,
    created: str | None,
) -> None:
    with pytest.raises(ValueError, match="cannot resolve assessment period"):
        resolve_assessment_period(config_period, created)


def test_tag_is_never_a_source() -> None:
    # The tag is the OUTPUT of minting, not an input. Guard against the dropped
    # ``tag_period`` parameter regressing back in: the resolver's
    # only sources are the Config_Blob period and the createdAt timestamp.
    sig = inspect.signature(resolve_assessment_period)
    param_names = set(sig.parameters)
    assert "tag_period" not in param_names
    assert param_names == {"config_period", "created", "is_reference"}
