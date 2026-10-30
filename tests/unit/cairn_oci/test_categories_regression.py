"""Regression / edge-case hardening for the single canonical tag grammar.

These tests complement ``test_categories.py``; they lock load-bearing fail-closed
invariants on the canonical ``{artifact-kind}--_ref--{digest}`` form
(the canonical tag has no ``{name}`` segment): the
unknown-leading-artifact-kind rejection, the ``\\Z``-anchor / trailing-newline
gotcha class on the (now sole) producer-influenced terminal ``digest`` segment,
and a small explicit round-trip matrix so a future grammar tweak that breaks
recovery is caught.

The ``\\Z``-anchor lock matters because Python's ``$`` also matches just before a
trailing ``\\n``, so a value like ``deadbeef0000\\n`` would slip past a
``$``-anchored grammar yet not be render-safe. The digest grammar is
``\\A…\\Z``-anchored; these tests pin that a trailing newline (or other
non-render-safe spelling) is rejected fail-closed and a future regression that
reopens the leniency fails loudly.
"""

from __future__ import annotations

import pytest
from cairn_oci.categories import (
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidTagError,
)

_LOCATOR = ArtifactLocator()
_DIGEST = "0123456789ab"


# ---------------------------------------------------------------------------
# Unknown leading artifact-kind in the canonical form (locked).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        f"not-a-kind--_ref--{_DIGEST}",
        f"screenshot--_ref--{_DIGEST}",
        f"made-up-kind--_ref--{_DIGEST}",
    ],
)
def test_unknown_leading_artifact_kind_raises(tag: str) -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(tag)


def test_unknown_kind_raises_invalid_tag_not_invalid_kind() -> None:
    # parse_tag wraps the underlying InvalidArtifactKindError as an InvalidTagError
    # so the validation layer routes a malformed tag (not a bad standalone kind)
    # to the DLQ. The chained cause is preserved for diagnosis.
    with pytest.raises(InvalidTagError) as ei:
        _LOCATOR.parse_tag(f"totally-bogus--_ref--{_DIGEST}")
    assert isinstance(ei.value.__cause__, InvalidArtifactKindError)


# ---------------------------------------------------------------------------
# Trailing-newline / render-safety on every producer-influenced segment.
# ---------------------------------------------------------------------------


def test_tag_with_trailing_newline_is_rejected() -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--_ref--{_DIGEST}\n")


@pytest.mark.parametrize(
    "name_segment",
    [
        "name\n",  # trailing newline (the \Z gotcha)
        "na me",  # internal space
        "na_me",  # underscore (forbidden char)
        "UPPER",  # uppercase
        "-lead",  # leading hyphen
        "nist-sp-800-53-rev-5",  # an otherwise render-safe slug
    ],
)
def test_legacy_name_bearing_ref_fails_closed_on_arity(name_segment: str) -> None:
    # The canonical _ref form carries no name segment, so a 4-segment
    # `{kind}--_ref--{would-be-name}--{digest}` is an arity violation — it fails
    # closed regardless of whether the spliced-in segment is itself render-safe.
    # The canonical tag grammar has no name segment, so the governing invariant
    # is the arity rejection.
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--_ref--{name_segment}--{_DIGEST}")


@pytest.mark.parametrize(
    "digest_segment",
    [
        f"{_DIGEST}\n",  # trailing newline
        _DIGEST.upper(),  # uppercase
        "0123456789a",  # too short
        "0123456789abc",  # too long
        "0123456789ag",  # non-hex char
        "0123_56789ab",  # underscore not hex
    ],
)
def test_noncanonical_digest_segment_is_rejected(digest_segment: str) -> None:
    # The digest is now the terminal segment of the 3-segment canonical form;
    # a non-render-safe spelling fails closed.
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--_ref--{digest_segment}")


# ---------------------------------------------------------------------------
# Round-trip property matrix: build then parse recovers every component.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "digest"),
    [
        ("catalog", _DIGEST),
        ("assessment-results", "abcdef012345"),
        ("governance-doc", "deadbeef0000"),
        ("system-security-plan", "0a1b2c3d4e5f"),
    ],
)
def test_canonical_form_round_trip_matrix(kind: str, digest: str) -> None:
    # The single canonical form is {kind}--_ref--{digest}; build → parse recovers
    # exactly (kind, digest) with no name segment (name is None).
    tag = _LOCATOR.build_ref_tag(artifact_kind=kind, digest=digest)
    assert tag == f"{kind}--_ref--{digest}"
    parsed = _LOCATOR.parse_tag(tag)
    recovered = (parsed.artifact_kind, parsed.name, parsed.digest)
    assert recovered == (kind, None, digest)
