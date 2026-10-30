"""Tests for the control-id grammar + family derivation primitives."""

import pytest
from cairn_oci.controls import (
    CONTROL_ID_RE,
    derive_families,
    is_valid_control_id,
)


@pytest.mark.parametrize(
    "control_id",
    [
        "ac-2",  # NIST control
        "ac-2.1",  # NIST control enhancement
        "5.1",  # ISO 27002 numeric
        "8.16",  # ISO 27002 numeric
        "a",  # single segment
        "ac-2.1.3",  # multi-segment
    ],
)
def test_grammar_accepts_well_formed(control_id: str) -> None:
    assert is_valid_control_id(control_id) is True


# Cyrillic confusable for "AC-2" — constructed via escapes so the source file
# itself holds no ambiguous characters (keeps ruff RUF001 quiet) while still
# exercising the non-ASCII rejection path.
_CYRILLIC_AC2 = "\N{CYRILLIC CAPITAL LETTER A}\N{CYRILLIC CAPITAL LETTER ES}-2"


@pytest.mark.parametrize(
    "control_id",
    [
        "",  # empty
        "ac/2",  # path separator (PK forgery)
        "ac#2",  # fragment
        " ac",  # leading whitespace
        "ac 2",  # internal whitespace
        "AC",  # uppercase
        "ac--2",  # consecutive separators
        "-ac",  # leading separator
        "ac-",  # trailing separator
        "ac.",  # trailing dot separator
        ".ac",  # leading dot separator
        "ac..2",  # consecutive dot separators
        "açao",  # non-ASCII (Latin c-cedilla)
        _CYRILLIC_AC2,  # Cyrillic confusable (non-ASCII)
        "ac-2\n",  # trailing newline ($-anchor / fullmatch footgun)
        "ac-2\r",  # trailing carriage return
        "5.1\n",  # trailing newline on an ISO-style id
        "\nac-2",  # leading newline
        "ac-2\x00",  # trailing NUL control byte
    ],
)
def test_grammar_rejects_malformed(control_id: str) -> None:
    assert is_valid_control_id(control_id) is False


@pytest.mark.parametrize("bad_input", [None, 123, ["ac-2"], b"ac-2"])
def test_grammar_rejects_non_str(bad_input: object) -> None:
    assert is_valid_control_id(bad_input) is False  # type: ignore[arg-type]


def test_regex_is_anchored() -> None:
    # Anchored at both ends: an embedded valid id within junk does not match.
    assert CONTROL_ID_RE.match("xx ac-2") is None
    assert CONTROL_ID_RE.match("ac-2 xx") is None
    assert CONTROL_ID_RE.pattern.startswith("^")
    assert CONTROL_ID_RE.pattern.endswith("$")


def test_regex_linear_no_catastrophic_backtracking() -> None:
    # A long non-matching input must resolve quickly (the disjoint separator
    # class precludes nested-quantifier backtracking). No timing assertion —
    # this simply exercises a pathological-shaped input and must return.
    pathological = "a" * 10_000 + "!"
    assert is_valid_control_id(pathological) is False


def test_derive_families_nist() -> None:
    assert derive_families(["ac-2", "au-6", "si-4"]) == {"AC", "AU", "SI"}


def test_derive_families_enhancement_uses_first_hyphen_segment() -> None:
    assert derive_families(["ac-2.1"]) == {"AC"}


def test_derive_families_iso_numeric_contributes_nothing() -> None:
    assert derive_families(["5.1", "8.16"]) == set()


def test_derive_families_dedup() -> None:
    assert derive_families(["ac-2", "ac-3"]) == {"AC"}


def test_derive_families_mixed_nist_and_iso() -> None:
    # Only the NIST (hyphenated) ids yield families; ISO numerics drop out.
    assert derive_families(["ac-2", "5.1", "au-6", "8.16"]) == {"AC", "AU"}


def test_derive_families_empty_input() -> None:
    assert derive_families([]) == set()


def test_derive_families_accepts_arbitrary_iterable() -> None:
    # Iterable, not just list — a generator works (consumed once).
    assert derive_families(c for c in ["ac-2", "au-6"]) == {"AC", "AU"}
