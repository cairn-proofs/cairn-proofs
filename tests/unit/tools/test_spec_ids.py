"""Unit tests for the stable-identifier linter (openspec/tools/spec_ids.py).

Pure-function tests against synthetic fixtures — no filesystem, no git, no live
corpus dependency (the corpus is not migrated until a later wave). Each test
builds its own in-memory `SpecDoc`/`Corpus`, so the suite is parallel-safe.

Scenario sub-sequences render zero-padded to two digits (`PREFIX-NNN.MM`). The
fixtures here use that 2-digit form; the canonical-identity tests deliberately
mix in single-digit renderings to prove identity is pad-width-agnostic (Option A).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC_IDS_PATH = _REPO_ROOT / "openspec" / "tools" / "spec_ids.py"
_spec = importlib.util.spec_from_file_location("spec_ids_under_test", _SPEC_IDS_PATH)
assert _spec and _spec.loader
spec_ids = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = spec_ids  # required for dataclass annotation resolution
_spec.loader.exec_module(spec_ids)


# --- fixture builders ------------------------------------------------------


def _spec_text(*requirement_blocks: str, ledger: str = "") -> str:
    body = ["## Purpose", "", "Synthetic capability.", "", "## Requirements", ""]
    body.extend(requirement_blocks)
    if ledger:
        body.append(ledger)
    return "\n".join(body)


def _requirement(spec_id: str, name: str, scenarios: list[tuple[str, str]]) -> str:
    heading = f"[{spec_id}] " if spec_id else ""
    lines = [f"### Requirement: {heading}{name}", "", "The system SHALL do the thing.", ""]
    for scn_id, scn_name in scenarios:
        scn_heading = f"[{scn_id}] " if scn_id else ""
        lines += [
            f"#### Scenario: {scn_heading}{scn_name}",
            "- **WHEN** something happens",
            "- **THEN** something results",
            "",
        ]
    return "\n".join(lines)


def _corpus(*texts_and_caps: tuple[str, str]) -> spec_ids.Corpus:
    docs = [spec_ids.parse_spec(text, cap, source=cap) for text, cap in texts_and_caps]
    return spec_ids.Corpus(docs=docs)


def _valid_doc() -> tuple[str, str]:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "alpha"), ("SIGN-001.02", "beta")]),
        _requirement("SIGN-002", "Second", [("SIGN-002.01", "gamma")]),
    )
    return text, "signing"


# --- parsing ---------------------------------------------------------------


def test_parse_extracts_requirement_and_scenario_ids() -> None:
    doc = spec_ids.parse_spec(_valid_doc()[0], "signing")
    req_ids = [h.spec_id for h in doc.requirements]
    scn_ids = [h.spec_id for h in doc.scenarios]
    assert req_ids == ["SIGN-001", "SIGN-002"]
    assert scn_ids == ["SIGN-001.01", "SIGN-001.02", "SIGN-002.01"]
    assert not doc.malformed_headings
    assert not doc.underpadded_scenarios


def test_parse_identifier_roundtrip() -> None:
    assert spec_ids.parse_identifier("SIGN-014") == ("SIGN", 14, None)
    assert spec_ids.parse_identifier("SIGN-014.03") == ("SIGN", 14, 3)
    # Identity is pad-width-agnostic (Option A): a single-digit rendering parses
    # to the SAME integer identity. Rendering discipline is the format gate's job.
    assert spec_ids.parse_identifier("SIGN-014.3") == ("SIGN", 14, 3)
    assert spec_ids.parse_identifier("not-an-id") is None
    assert spec_ids.parse_identifier("SIGN-14") is None  # too few digits


def test_identity_key_is_pad_width_agnostic() -> None:
    # The canonical-identity seam: `.1` and `.01` collapse to ONE key.
    assert spec_ids.identity_key("SIGN-009.1") == spec_ids.identity_key("SIGN-009.01")
    assert spec_ids.identity_key("SIGN-009.01") == ("SIGN", 9, 1)
    assert spec_ids.identity_key("SIGN-014") == ("SIGN", 14, None)
    # A string that does not parse is returned unchanged.
    assert spec_ids.identity_key("not-an-id") == "not-an-id"


# --- rule 1: presence ------------------------------------------------------


def test_valid_corpus_has_no_violations() -> None:
    corpus = _corpus(_valid_doc())
    assert spec_ids.validate_corpus(corpus) == []


def test_missing_requirement_id_is_flagged() -> None:
    text = _spec_text(_requirement("", "No Identifier", [("SIGN-001.01", "alpha")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_presence(corpus)
    assert len(violations) == 1
    assert "lacks a well-formed identifier" in violations[0]
    assert "No Identifier" in violations[0]


def test_missing_scenario_id_is_flagged() -> None:
    text = _spec_text(_requirement("SIGN-001", "First", [("", "no id scenario")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_presence(corpus)
    assert len(violations) == 1
    assert "scenario heading lacks" in violations[0]


def test_two_digit_sequence_is_malformed() -> None:
    text = _spec_text(_requirement("SIGN-01", "Bad pad", [("SIGN-01.1", "x")]))
    corpus = _corpus((text, "signing"))
    # Neither the requirement nor scenario id parses, so both are malformed.
    assert len(spec_ids.check_presence(corpus)) == 2


# --- rule 1 (format): scenario sub-sequence must be 2-digit zero-padded -----
# Realizes spec scenario [TOOLS-013.08].


def test_single_digit_scenario_subid_is_rejected_by_format_gate() -> None:
    # A bare single-digit sub-sequence ([PREFIX-NNN.M]) parses (identity preserved)
    # but fails Rule 1 / presence-and-format, naming the offending heading.
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.1", "alpha")]))
    corpus = _corpus((text, "signing"))
    # The identity is still extracted (pad-agnostic), so the heading is NOT
    # "malformed" — it is specifically an under-padded format violation.
    doc = corpus.docs[0]
    assert [h.spec_id for h in doc.scenarios] == ["SIGN-001.1"]
    assert not doc.malformed_headings
    assert [h.spec_id for h in doc.underpadded_scenarios] == ["SIGN-001.1"]
    violations = spec_ids.check_presence(corpus)
    assert len(violations) == 1
    assert "zero-padded to 2 digits" in violations[0]
    assert "[SIGN-001.1] alpha" in violations[0]  # names the offending heading


def test_single_digit_scenario_subid_fails_validate_corpus() -> None:
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.1", "alpha")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.validate_corpus(corpus)
    assert any("zero-padded to 2 digits" in v for v in violations)


def test_well_padded_scenario_subid_is_not_flagged() -> None:
    corpus = _corpus(_valid_doc())
    for doc in corpus.docs:
        assert doc.underpadded_scenarios == []


def test_over_padded_scenario_subid_is_rejected_by_format_gate() -> None:
    # An OVER-padded sub-sequence ([PREFIX-NNN.001]) shares the canonical identity
    # (SIGN,1,1) of [PREFIX-NNN.01] but carries a surplus leading zero, so it is a
    # format violation — it parses (identity preserved) but is not "malformed".
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.001", "alpha")]))
    corpus = _corpus((text, "signing"))
    doc = corpus.docs[0]
    assert [h.spec_id for h in doc.scenarios] == ["SIGN-001.001"]
    assert not doc.malformed_headings
    assert [h.spec_id for h in doc.underpadded_scenarios] == ["SIGN-001.001"]
    violations = spec_ids.check_presence(corpus)
    assert len(violations) == 1
    assert "zero-padded to 2 digits" in violations[0]
    assert "[SIGN-001.001] alpha" in violations[0]  # names the offending heading


def test_over_padded_010_rejected_but_canonical_100_accepted() -> None:
    # `.010` is over-padded (surplus leading zero: `010` != `f"{10:02d}"` == `10`)
    # and rejected; `.100` (scenario 100) is canonical (`100` == `f"{100:02d}"`)
    # and accepted. A fixed `len == 2` check would wrongly reject the legitimate
    # `.100` — the canonical-equality test does not.
    over = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.010", "ten")]))
    over_corpus = _corpus((over, "signing"))
    assert [h.spec_id for h in over_corpus.docs[0].underpadded_scenarios] == ["SIGN-001.010"]
    over_violations = spec_ids.check_presence(over_corpus)
    assert len(over_violations) == 1
    assert "zero-padded to 2 digits" in over_violations[0]
    assert "[SIGN-001.010] ten" in over_violations[0]

    canonical = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.100", "hundred")]))
    canonical_corpus = _corpus((canonical, "signing"))
    assert canonical_corpus.docs[0].underpadded_scenarios == []
    assert spec_ids.check_presence(canonical_corpus) == []


def test_over_padded_scenario_subid_fails_validate_corpus() -> None:
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.001", "alpha")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.validate_corpus(corpus)
    assert any("zero-padded to 2 digits" in v for v in violations)


# --- rule 2: uniqueness ----------------------------------------------------


def test_collision_is_reported() -> None:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "alpha")]),
        _requirement("SIGN-001", "Duplicate", [("SIGN-001.02", "beta")]),
    )
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_uniqueness(corpus)
    assert len(violations) == 1
    assert "duplicate identifier SIGN-001" in violations[0]


def test_collision_across_files_is_reported() -> None:
    a = _spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")]))
    b = _spec_text(_requirement("SIGN-001", "B", [("SIGN-001.02", "y")]))
    corpus = _corpus((a, "signing"), (b, "encryption"))
    assert any("duplicate identifier SIGN-001" in v for v in spec_ids.check_uniqueness(corpus))


def test_same_identity_different_pad_renderings_collide() -> None:
    # `[SIGN-001.01]` and `[SIGN-001.001]` share the canonical identity (SIGN,1,1).
    # Even though their literal strings differ, uniqueness is keyed on the identity,
    # so this is a duplicate — not a false-green pair of "distinct" scenarios.
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "First",
            [("SIGN-001.01", "alpha"), ("SIGN-001.001", "alpha-again")],
        )
    )
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_uniqueness(corpus)
    assert len(violations) == 1
    assert "duplicate identifier" in violations[0]
    # Both pad-renderings are named so the collision reads clearly.
    assert "SIGN-001.001" in violations[0]
    assert "SIGN-001.01" in violations[0]


def test_underpad_vs_canonical_renderings_collide() -> None:
    # The other rendering boundary: `[SIGN-001.1]` (under-padded) and
    # `[SIGN-001.01]` (canonical) are the same identity (SIGN,1,1) and collide.
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "First",
            [("SIGN-001.1", "alpha"), ("SIGN-001.01", "alpha-again")],
        )
    )
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_uniqueness(corpus)
    assert len(violations) == 1
    assert "duplicate identifier" in violations[0]
    assert "SIGN-001.1" in violations[0]
    assert "SIGN-001.01" in violations[0]


# --- rule 3: no vanish -----------------------------------------------------


def test_vanished_id_without_ledger_is_flagged() -> None:
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "First", [("SIGN-001.01", "a")]),
                _requirement("SIGN-002", "Second", [("SIGN-002.01", "b")]),
            ),
            "signing",
        )
    )
    current = _corpus(
        (_spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "a")])), "signing")
    )
    violations = spec_ids.check_no_vanish(committed, current)
    # SIGN-002 and SIGN-002.01 both vanished without a ledger row.
    assert any("SIGN-002 was removed" in v for v in violations)
    assert any("SIGN-002.01 was removed" in v for v in violations)


def test_vanished_id_with_withdrawn_ledger_passes() -> None:
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "First", [("SIGN-001.01", "a")]),
                _requirement("SIGN-002", "Second", [("SIGN-002.01", "b")]),
            ),
            "signing",
        )
    )
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-002 | withdrawn | |\n"
        "| SIGN-002.01 | withdrawn | |\n"
    )
    current = _corpus(
        (
            _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "a")]), ledger=ledger),
            "signing",
        )
    )
    assert spec_ids.check_no_vanish(committed, current) == []


# --- rule 4: no reuse ------------------------------------------------------


def test_reuse_of_retired_id_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-003 | withdrawn | |\n"
    )
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                _requirement("SIGN-002", "B", [("SIGN-002.01", "y")]),
                ledger=ledger,
            ),
            "signing",
        )
    )
    # Current corpus re-mints SIGN-003 as a live requirement — must be rejected.
    current = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                _requirement("SIGN-002", "B", [("SIGN-002.01", "y")]),
                _requirement("SIGN-003", "Reused!", [("SIGN-003.01", "z")]),
            ),
            "signing",
        )
    )
    violations = spec_ids.check_no_reuse(committed, current)
    assert any("SIGN-003 reuses or backfills" in v for v in violations)


def test_new_id_above_high_water_mark_passes() -> None:
    committed = _corpus(
        (_spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")])), "signing")
    )
    current = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                _requirement("SIGN-002", "New", [("SIGN-002.01", "y")]),
            ),
            "signing",
        )
    )
    assert spec_ids.check_no_reuse(committed, current) == []


def test_reused_scenario_subnumber_is_flagged() -> None:
    committed_ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-001.02 | withdrawn | |\n"
    )
    # Committed: SIGN-001.01 live, SIGN-001.02 retired -> sub high-water mark is 2.
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                ledger=committed_ledger,
            ),
            "signing",
        )
    )
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-001.02 | withdrawn | |\n"
    )
    # Re-mint SIGN-001.02 (retired) as a new live scenario — sub-number reuse.
    current = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x"), ("SIGN-001.02", "reused")]),
                ledger=ledger,
            ),
            "signing",
        )
    )
    violations = spec_ids.check_no_reuse(committed, current)
    assert any("SIGN-001.02 reuses a sub-number" in v for v in violations)


# --- canonical-identity seam: a `.1` -> `.01` pad-widen is NOT a renumber ----
# The load-bearing pad-widen reconciliation: the
# committed snapshot renders the sub single-digit, the working tree renders it
# 2-digit, and the gate must reconcile them to the SAME identity.


def test_pad_widen_is_not_seen_as_vanish_or_reuse() -> None:
    committed = _corpus(
        (_spec_text(_requirement("SIGN-009", "Timestamp", [("SIGN-009.1", "x")])), "signing")
    )
    current = _corpus(
        (_spec_text(_requirement("SIGN-009", "Timestamp", [("SIGN-009.01", "x")])), "signing")
    )
    # The committed single-digit heading still has an identity (pad-agnostic parse).
    assert "SIGN-009.1" in committed.live_ids()
    # ... that reconciles with the working tree's 2-digit rendering.
    assert spec_ids.check_no_vanish(committed, current) == []
    assert spec_ids.check_no_reuse(committed, current) == []


def test_pad_widen_passes_validate_corpus_against_committed_snapshot() -> None:
    committed = _corpus(
        (_spec_text(_requirement("SIGN-009", "Timestamp", [("SIGN-009.1", "x")])), "signing")
    )
    current = _corpus(
        (_spec_text(_requirement("SIGN-009", "Timestamp", [("SIGN-009.01", "x")])), "signing")
    )
    # The current corpus is well-padded, so the whole gate (presence + committed
    # rules) is clean — the migration is provably not a renumber.
    assert spec_ids.validate_corpus(current, committed=committed) == []


# --- rule 5: references resolve --------------------------------------------


def test_dangling_cross_reference_is_flagged() -> None:
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "Cites a missing id",
            [("SIGN-001.01", "see [SIGN-999] for context")],
        )
    )
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_references(corpus)
    assert len(violations) == 1
    assert "reference SIGN-999 does not resolve" in violations[0]


def test_resolving_cross_reference_passes() -> None:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "see [SIGN-002] below")]),
        _requirement("SIGN-002", "Second", [("SIGN-002.01", "target")]),
    )
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_references(corpus) == []


def test_single_digit_citation_resolves_against_two_digit_live_id() -> None:
    # A citation rendered single-digit still resolves against the 2-digit live id
    # (canonical identity), so a citation an editor missed updating does not break.
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "x")]),
        _requirement("SIGN-002", "Second", [("SIGN-002.01", "target")]),
    )
    corpus = _corpus((text, "signing"))
    single_digit_ref = spec_ids.Reference(spec_id="SIGN-002.1", line_no=3, source="x.md")
    assert spec_ids.check_references(corpus, extra_references=[single_digit_ref]) == []


def test_single_digit_citation_to_missing_identity_still_dangles() -> None:
    # Pad-agnostic resolution does NOT silently swallow a genuinely dead citation
    # (a stale single-digit citation surfaces, never a silent link).
    corpus = _corpus(_valid_doc())
    dead_ref = spec_ids.Reference(spec_id="SIGN-404.1", line_no=3, source="x.md")
    violations = spec_ids.check_references(corpus, extra_references=[dead_ref])
    assert len(violations) == 1
    assert "SIGN-404.1 does not resolve" in violations[0]


def test_reference_resolving_via_ledger_passes() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-009 | withdrawn | |\n"
    )
    text = _spec_text(
        _requirement("SIGN-001", "Cites a retired id", [("SIGN-001.01", "superseded [SIGN-009]")]),
        ledger=ledger,
    )
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_references(corpus) == []


def test_covers_marker_reference_is_checked() -> None:
    corpus = _corpus(_valid_doc())
    # Build the marker keyword indirectly so this test's own source does not
    # contain a literal scannable token that the repo-wide gate would treat as a
    # real (and here, deliberately dangling) citation.
    kw = "Covers" + ":"
    good = spec_ids.covers_references(f"    # {kw} SIGN-001.02\n", "tests/test_x.py")
    bad = spec_ids.covers_references(f"    # {kw} SIGN-404.01\n", "tests/test_x.py")
    assert spec_ids.check_references(corpus, extra_references=good) == []
    dangling = spec_ids.check_references(corpus, extra_references=bad)
    assert len(dangling) == 1
    assert "SIGN-404.01 does not resolve" in dangling[0]


def test_backlog_reference_extraction() -> None:
    refs = spec_ids.backlog_references(
        "See spec requirement [SIGN-001] and [OD-3] (legacy).\n", "doc.md"
    )
    # OD-3 has only one digit and is NOT an identifier under this scheme.
    assert [r.spec_id for r in refs] == ["SIGN-001"]


# --- rule 6: positional / Property-N ban -----------------------------------


def test_property_numbering_is_flagged() -> None:
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "Legacy property reference",
            [("SIGN-001.01", "this verifies Property 4 of the bundle")],
        )
    )
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_no_positional(corpus)
    assert any("Property 4" in v for v in violations)


def test_positional_ordinal_reference_is_flagged() -> None:
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "Positional",
            [("SIGN-001.01", "as stated in the third criterion above")],
        )
    )
    corpus = _corpus((text, "signing"))
    assert any("third criterion" in v for v in spec_ids.check_no_positional(corpus))


def test_legacy_req_numbering_is_flagged() -> None:
    text = _spec_text(_requirement("SIGN-001", "Legacy (Source: Req 3.2)", [("SIGN-001.01", "x")]))
    corpus = _corpus((text, "signing"))
    assert any("Req 3.2" in v for v in spec_ids.check_no_positional(corpus))


def test_property_free_corpus_passes_rule_six() -> None:
    corpus = _corpus(_valid_doc())
    assert spec_ids.check_no_positional(corpus) == []


# --- ledger validation -----------------------------------------------------


def test_absent_ledger_is_not_an_error() -> None:
    corpus = _corpus(_valid_doc())
    assert all(doc.ledger == [] for doc in corpus.docs)
    assert spec_ids.check_ledger(corpus) == []


def test_valid_withdrawn_and_relocated_ledger_rows_pass() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-005 | withdrawn | |\n"
        "| SIGN-006 | relocated | ENCRYPT-012 |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    # The relocation destination must be live somewhere in the corpus.
    encryption = _spec_text(_requirement("ENCRYPT-012", "Destination", [("ENCRYPT-012.01", "y")]))
    corpus = _corpus((text, "signing"), (encryption, "encryption"))
    assert spec_ids.check_ledger(corpus) == []
    # The ledger ids are parsed.
    doc = corpus.docs[0]
    statuses = {e.spec_id: e.status for e in doc.ledger}
    assert statuses == {"SIGN-005": "withdrawn", "SIGN-006": "relocated"}


def test_relocated_without_destination_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-006 | relocated | |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    corpus = _corpus((text, "signing"))
    assert any("has no destination identifier" in v for v in spec_ids.check_ledger(corpus))


def test_relocated_to_dead_destination_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-006 | relocated | ENCRYPT-999 |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    corpus = _corpus((text, "signing"))
    assert any("not a live identifier" in v for v in spec_ids.check_ledger(corpus))


def test_invalid_ledger_status_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-006 | archived | |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    corpus = _corpus((text, "signing"))
    assert any("invalid status" in v for v in spec_ids.check_ledger(corpus))


def test_duplicate_ledger_entry_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-006 | withdrawn | |\n"
        "| SIGN-006 | withdrawn | |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    corpus = _corpus((text, "signing"))
    assert any("duplicate ledger entry for SIGN-006" in v for v in spec_ids.check_ledger(corpus))


def test_ledger_missing_required_column_is_flagged() -> None:
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Resolves To |\n"
        "| --- | --- |\n"
        "| SIGN-006 | ENCRYPT-001 |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    corpus = _corpus((text, "signing"))
    assert any("missing required" in v for v in spec_ids.check_ledger(corpus))


# --- seed allocator --------------------------------------------------------


def test_allocator_allocates_forward_from_high_water() -> None:
    alloc = spec_ids.SeedAllocator(existing_ids={"SIGN-001", "SIGN-002"})
    assert alloc.allocate_requirement("SIGN") == "SIGN-003"
    assert alloc.allocate_requirement("SIGN") == "SIGN-004"
    # A fresh prefix starts at 001.
    assert alloc.allocate_requirement("WORM") == "WORM-001"


def test_allocator_never_reuses_retired_numbers() -> None:
    # Seed with a retired id at the high-water mark; the next mint must skip it.
    alloc = spec_ids.SeedAllocator(existing_ids={"SIGN-001", "SIGN-002", "SIGN-003"})
    assert alloc.allocate_requirement("SIGN") == "SIGN-004"


def test_allocator_scenarios_allocate_forward_and_emit_two_digit_subids() -> None:
    # Seeded from single-digit renderings (pad-agnostic identity), the allocator
    # advances the integer high-water mark and EMITS 2-digit zero-padded sub-ids.
    alloc = spec_ids.SeedAllocator(existing_ids={"SIGN-001", "SIGN-001.1", "SIGN-001.2"})
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.03"
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.04"
    # Scenarios under a different requirement start at .01.
    assert alloc.allocate_scenario("SIGN-002") == "SIGN-002.01"


def test_allocator_rejects_malformed_inputs() -> None:
    alloc = spec_ids.SeedAllocator()
    with pytest.raises(ValueError):
        alloc.allocate_requirement("sign")  # not uppercase
    with pytest.raises(ValueError):
        alloc.allocate_scenario("SIGN-1")  # not a well-formed requirement id


# --- end-to-end validate_corpus -------------------------------------------


def test_validate_corpus_aggregates_committed_rules() -> None:
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                _requirement("SIGN-002", "B", [("SIGN-002.01", "y")]),
            ),
            "signing",
        )
    )
    # SIGN-002 silently dropped (no ledger) -> no-vanish violation.
    current = _corpus(
        (_spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")])), "signing")
    )
    violations = spec_ids.validate_corpus(current, committed=committed)
    assert any("SIGN-002 was removed" in v for v in violations)


def test_validate_corpus_clean_with_committed_snapshot() -> None:
    text, cap = _valid_doc()
    committed = _corpus((text, cap))
    current = _corpus((text, cap))
    assert spec_ids.validate_corpus(current, committed=committed) == []


# --- seam regression 1: live repo-wide gate must NOT self-poison -----------
#
# Earlier the repo-wide run false-positived because a `Covers:`-style example
# marker living ONLY inside a test's own source was scanned by
# `gather_test_references` and treated as a real (dangling) citation. Tests now
# split that keyword so no literal scannable token leaks into tests/. This
# regression locks the property in end-to-end: the actual CLI over the live
# corpus + the real tests/ trees must exit 0.
#
# WAVE-ORDERING NOTE: the two full-CLI live-gate tests below assert the gate
# exits 0 over the REAL corpus. With the 2-digit format gate now in force, that
# can only hold once the corpus has been re-minted to `PREFIX-NNN.MM` (wave 1).
# Until then the live corpus still renders single-digit sub-ids, so these two
# tests are skipped; they re-arm automatically once the corpus is migrated and
# are exercised by the orchestrator's post-wave `mise run check`.


def _live_corpus_has_underpadded_scenarios() -> bool:
    corpus = spec_ids.read_current_corpus(_REPO_ROOT / "openspec" / "specs")
    return any(doc.underpadded_scenarios for doc in corpus.docs)


_LIVE_CORPUS_PRE_MIGRATION = _live_corpus_has_underpadded_scenarios()
_PRE_MIGRATION_REASON = (
    "live corpus still renders single-digit scenario sub-ids; the 2-digit re-mint "
    "(wave 1) must land before the full-CLI gate can exit 0 over the real corpus"
)


@pytest.mark.skipif(_LIVE_CORPUS_PRE_MIGRATION, reason=_PRE_MIGRATION_REASON)
def test_live_repo_wide_gate_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(_SPEC_IDS_PATH)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "live spec_ids gate must pass over the real corpus/tests/backlog "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


@pytest.mark.skipif(_LIVE_CORPUS_PRE_MIGRATION, reason=_PRE_MIGRATION_REASON)
def test_live_gate_no_git_variant_exits_zero() -> None:
    # The git-free path (no-vanish/no-reuse skipped) must also be clean — this is
    # the mode CI uses on a shallow/detached checkout with no HEAD spec history.
    result = subprocess.run(
        [sys.executable, str(_SPEC_IDS_PATH), "--no-git"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_test_covers_markers_in_repo_are_not_self_poisoning() -> None:
    # Directly exercise the scanner the seam bug lived in: gathering `Covers:`
    # markers across the real tests/ tree, then resolving them against the live
    # corpus, must yield zero dangling references. (A literal `Covers: SIGN-xxx`
    # token committed into a negative fixture would resurface here.) Resolution is
    # pad-agnostic, so this holds before and after the corpus re-mint.
    corpus = spec_ids.read_current_corpus(_REPO_ROOT / "openspec" / "specs")
    test_refs = spec_ids.gather_test_references(_REPO_ROOT / "tests")
    assert spec_ids.check_references(corpus, extra_references=test_refs) == []


# --- seam regression 2: rule-6 ordinal-positional matcher is well-defined ---
#
# The ordinal matcher fires only on `the <ordinal> <noun>` where <noun> is one
# of the spec-structural words; an ordinal-free locational phrase like
# "by its position" must NOT trip it (that was over-broad before the fix).


@pytest.mark.parametrize(
    "phrase",
    [
        "as stated in the third criterion above",
        "see the first scenario",
        "per the twelfth requirement",
        "THE THIRD CRITERION",  # IGNORECASE
        "matches the second bullet",
    ],
)
def test_ordinal_positional_phrase_is_flagged(phrase: str) -> None:
    text = _spec_text(_requirement("SIGN-001", "Ord", [("SIGN-001.01", phrase)]))
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_no_positional(corpus), f"expected {phrase!r} to be flagged"


@pytest.mark.parametrize(
    "phrase",
    [
        "identified by its position in the list",  # no ordinal at all
        "the third place in line",  # ordinal but non-structural noun
        "third criterion without a leading article",  # ordinal+noun but no 'the'
        "the criterion that follows",  # noun but no ordinal
        "positioned thirteenth in the order",  # ordinal word outside the bounded set
    ],
)
def test_non_ordinal_phrase_is_not_flagged(phrase: str) -> None:
    text = _spec_text(_requirement("SIGN-001", "NoOrd", [("SIGN-001.01", phrase)]))
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_no_positional(corpus) == [], f"expected {phrase!r} NOT flagged"


def test_property_and_req_matchers_are_case_sensitive() -> None:
    # `Property N` / `Req N.N` are intentionally capitalized-only (no IGNORECASE):
    # lowercase prose like "property of the bundle" must not be a false positive.
    text = _spec_text(
        _requirement(
            "SIGN-001",
            "Lowercase prose",
            [("SIGN-001.01", "this is a property 4 description, see req 3.2 of the doc")],
        )
    )
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_no_positional(corpus) == []


# --- Covers: marker extraction edge cases ----------------------------------


def test_covers_marker_extracts_multiple_comma_separated_ids() -> None:
    kw = "Covers" + ":"
    refs = spec_ids.covers_references(f"    # {kw} SIGN-001, SIGN-002.03 and ENC-005\n", "t.py")
    assert [r.spec_id for r in refs] == ["SIGN-001", "SIGN-002.03", "ENC-005"]


def test_covers_marker_accepts_bracketed_ids() -> None:
    kw = "Covers" + ":"
    refs = spec_ids.covers_references(f"# {kw} [SIGN-001] [SIGN-002.01]\n", "t.py")
    assert [r.spec_id for r in refs] == ["SIGN-001", "SIGN-002.01"]


def test_covers_marker_absent_yields_no_references() -> None:
    assert spec_ids.covers_references("just an ordinary comment line\n", "t.py") == []


# --- heading-embedded citations (_scan_heading_extras) ---------------------


def test_citation_in_requirement_heading_title_is_a_reference_not_a_redefinition() -> None:
    # The leading `[SIGN-001]` is the heading's own identifier; a SECOND token in
    # the title is a citation and must be gathered as a reference (and resolved).
    text = _spec_text(
        _requirement("SIGN-001", "supersedes [SIGN-002]", [("SIGN-001.01", "x")]),
        _requirement("SIGN-002", "Target", [("SIGN-002.01", "y")]),
    )
    corpus = _corpus((text, "signing"))
    ref_ids = [r.spec_id for r in corpus.references()]
    assert "SIGN-002" in ref_ids
    assert "SIGN-001" not in ref_ids  # the defining id is not double-counted as a citation
    # And it resolves cleanly because SIGN-002 is live.
    assert spec_ids.check_references(corpus) == []


def test_dangling_citation_in_scenario_heading_title_is_flagged() -> None:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "see also [SIGN-777]")]),
    )
    corpus = _corpus((text, "signing"))
    assert any("SIGN-777 does not resolve" in v for v in spec_ids.check_references(corpus))


# --- rule 4: no-reuse happy paths (sub-number / fresh prefix) --------------


def test_new_scenario_subnumber_above_high_water_passes() -> None:
    committed = _corpus(
        (_spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")])), "signing")
    )
    current = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x"), ("SIGN-001.02", "new sub")])
            ),
            "signing",
        )
    )
    assert spec_ids.check_no_reuse(committed, current) == []


def test_first_requirement_for_fresh_prefix_passes() -> None:
    committed = _corpus(
        (_spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")])), "signing")
    )
    current = _corpus(
        (_spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")])), "signing"),
        (_spec_text(_requirement("WORM-001", "B", [("WORM-001.01", "y")])), "worm"),
    )
    assert spec_ids.check_no_reuse(committed, current) == []


# --- rule 5 (anchor form): backlog GitHub-anchor link resolution -----------
#
# A GitHub-anchor spec link is `](../../../openspec/specs/signing/spec.md#requirement-sign-009-…)`,
# not a bracketed [ID] token. The scanner parses each such link and validates its
# fragment against the set of anchors computed from the live headings.


def test_github_anchor_matches_github_slugger_for_an_id_heading() -> None:
    # The full rendered heading text (label + [ID] bracket + name), lowercased,
    # non-(word/space/hyphen) chars dropped, spaces -> hyphens.
    assert (
        spec_ids.github_anchor("Requirement: [SIGN-009] TimestampProvider Strategy (Phased)")
        == "requirement-sign-009-timestampprovider-strategy-phased"
    )
    assert (
        spec_ids.github_anchor("Scenario: [SIGN-009.03] Dual-TSA countersignature")
        == "scenario-sign-00903-dual-tsa-countersignature"
    )


def test_corpus_anchors_indexes_requirement_and_scenario_anchors_per_capability() -> None:
    text = _spec_text(
        _requirement("SIGN-009", "TimestampProvider Strategy (Phased)", [("SIGN-009.01", "alpha")])
    )
    corpus = _corpus((text, "signing"))
    anchors = spec_ids.corpus_anchors(corpus)
    assert "requirement-sign-009-timestampprovider-strategy-phased" in anchors["signing"]
    assert "scenario-sign-00901-alpha" in anchors["signing"]


def test_backlog_anchor_reference_extraction() -> None:
    line = "See the [strategy](../../../openspec/specs/signing/spec.md#requirement-sign-009-foo).\n"
    refs = spec_ids.backlog_anchor_references(line, "doc.md")
    assert len(refs) == 1
    assert refs[0].capability == "signing"
    assert refs[0].anchor == "requirement-sign-009-foo"


def test_resolving_backlog_anchor_link_passes() -> None:
    text = _spec_text(_requirement("SIGN-009", "Timestamp Strategy", [("SIGN-009.01", "x")]))
    corpus = _corpus((text, "signing"))
    anchor = spec_ids.github_anchor("Requirement: [SIGN-009] Timestamp Strategy")
    refs = [
        spec_ids.BacklogAnchorRef(capability="signing", anchor=anchor, line_no=8, source="doc.md")
    ]
    assert spec_ids.check_backlog_anchor_refs(corpus, refs) == []


def test_dangling_backlog_anchor_link_is_flagged() -> None:
    text = _spec_text(_requirement("SIGN-009", "Timestamp Strategy", [("SIGN-009.01", "x")]))
    corpus = _corpus((text, "signing"))
    refs = [
        spec_ids.BacklogAnchorRef(
            capability="signing",
            anchor="requirement-sign-009-a-stale-renamed-heading",
            line_no=8,
            source="doc.md",
        )
    ]
    violations = spec_ids.check_backlog_anchor_refs(corpus, refs)
    assert len(violations) == 1
    assert "does not resolve to a live heading anchor" in violations[0]


def test_backlog_anchor_link_to_unknown_capability_is_flagged() -> None:
    text = _spec_text(_requirement("SIGN-009", "Timestamp Strategy", [("SIGN-009.01", "x")]))
    corpus = _corpus((text, "signing"))
    refs = [
        spec_ids.BacklogAnchorRef(
            capability="nonexistent-capability",
            anchor="requirement-foo",
            line_no=8,
            source="doc.md",
        )
    ]
    violations = spec_ids.check_backlog_anchor_refs(corpus, refs)
    assert any("targets unknown capability" in v for v in violations)


def test_no_anchor_refs_yields_no_violations() -> None:
    corpus = _corpus(_valid_doc())
    assert spec_ids.check_backlog_anchor_refs(corpus, None) == []
    assert spec_ids.check_backlog_anchor_refs(corpus, []) == []


# --- rule 7: identifier prefix matches its capability's assigned prefix -----


def test_prefix_matching_capability_passes() -> None:
    text = _spec_text(_requirement("ENCRYPT-001", "At Rest", [("ENCRYPT-001.01", "x")]))
    corpus = _corpus((text, "encryption"))
    assert spec_ids.check_prefix_matches_capability(corpus) == []


def test_requirement_prefix_mismatching_capability_is_flagged() -> None:
    # A SIGN-prefixed requirement living in the encryption capability is wrong:
    # encryption's assigned prefix is ENCRYPT.
    text = _spec_text(_requirement("SIGN-001", "Misfiled", [("SIGN-001.01", "x")]))
    corpus = _corpus((text, "encryption"))
    violations = spec_ids.check_prefix_matches_capability(corpus)
    assert any("SIGN-001" in v and "requires prefix 'ENCRYPT'" in v for v in violations)


def test_scenario_prefix_mismatching_capability_is_flagged() -> None:
    text = _spec_text(_requirement("WORM-001", "First", [("WORM-001.01", "x")]))
    corpus = _corpus((text, "signing"))  # signing requires SIGN, not WORM
    violations = spec_ids.check_prefix_matches_capability(corpus)
    # Both the requirement and the scenario carry the wrong prefix.
    assert len(violations) == 2
    assert all("requires prefix 'SIGN'" in v for v in violations)


def test_unknown_capability_is_not_subject_to_prefix_rule() -> None:
    # A synthetic capability dir not in the canonical map is skipped (the rule
    # only governs the real capabilities in CAPABILITY_PREFIXES).
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]))
    corpus = _corpus((text, "synthetic-capability"))
    assert spec_ids.check_prefix_matches_capability(corpus) == []


def test_all_eighteen_capability_prefixes_are_mapped() -> None:
    # Lock the assigned-prefix map.
    # producer-claims/PCLAIM is a distinct capability from metadata-index,
    # bringing the count to 17; identity-registry/IDREG is a control-plane
    # capability, bringing it to 18.
    assert spec_ids.CAPABILITY_PREFIXES == {
        "artifact-ingestion": "INGEST",
        "signing": "SIGN",
        "worm-archive": "WORM",
        "metadata-index": "META",
        "producer-claims": "PCLAIM",
        "processing-pipeline": "PIPELINE",
        "oscal-interop": "OSCAL",
        "access-control": "ACCESS",
        "retention-lifecycle": "RETAIN",
        "runtime-slos": "SLO",
        "encryption": "ENCRYPT",
        "observability": "OBS",
        "deployment-modes": "DEPLOY",
        "platform-constraints": "PLATFORM",
        "artifact-tui": "TUI",
        "toolchain": "TOOLS",
        "artifact-verifiability": "VERIFY",
        "identity-registry": "IDREG",
    }


# --- rule 8: a scenario's PREFIX-NNN matches its enclosing requirement -------


def test_scenario_under_matching_requirement_passes() -> None:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "a"), ("SIGN-001.02", "b")]),
        _requirement("SIGN-002", "Second", [("SIGN-002.01", "c")]),
    )
    corpus = _corpus((text, "signing"))
    assert spec_ids.check_scenario_under_requirement(corpus) == []


def test_scenario_with_wrong_number_under_requirement_is_flagged() -> None:
    # Scenario SIGN-002.01 sits under requirement SIGN-001 — number mismatch.
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-002.01", "stray")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_scenario_under_requirement(corpus)
    assert len(violations) == 1
    assert "SIGN-002.01 sits under requirement SIGN-001" in violations[0]


def test_scenario_with_wrong_prefix_under_requirement_is_flagged() -> None:
    # Scenario WORM-001.01 sits under requirement SIGN-001 — prefix mismatch.
    text = _spec_text(_requirement("SIGN-001", "First", [("WORM-001.01", "stray")]))
    corpus = _corpus((text, "signing"))
    violations = spec_ids.check_scenario_under_requirement(corpus)
    assert any("WORM-001.01 sits under requirement SIGN-001" in v for v in violations)


def test_scenario_binding_records_enclosing_requirement_during_parse() -> None:
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.01", "a")]),
        _requirement("SIGN-002", "Second", [("SIGN-002.01", "b")]),
    )
    doc = spec_ids.parse_spec(text, "signing")
    bindings = {s.spec_id: s.enclosing_req_id for s in doc.scenarios}
    assert bindings == {"SIGN-001.01": "SIGN-001", "SIGN-002.01": "SIGN-002"}


# --- zero-pad edge cases: the single->double-digit sub-sequence boundary -----
#
# These harden the 2-digit re-mint at the one place a pad scheme is fragile:
# the 9 -> 10 boundary. Identity (and the high-water mark) is an INTEGER tuple,
# so a `.09` neighbour of a `.10` must order by integer value, never by the
# lexical string ("09" < "10" happens to agree, but ".9" vs ".10" does NOT) —
# and the allocator must render the carry as a 2-digit `:02d`.


def test_allocator_scenario_crosses_single_to_double_digit_boundary() -> None:
    # Seeded at .09, the next mint is .10 (not .010, not .9+1 mis-rendered). The
    # integer high-water mark + :02d rendering carry the boundary together.
    alloc = spec_ids.SeedAllocator(existing_ids={"SIGN-001", "SIGN-001.09"})
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.10"
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.11"


def test_allocator_subid_high_water_is_integer_not_lexical() -> None:
    # Seeded with both .09 and .10 present: the integer max is 10, so the next
    # mint is .11. A lexical high-water mark would wrongly pick ".09" > ".10"
    # (string compare) and re-emit .10 — a collision.
    alloc = spec_ids.SeedAllocator(existing_ids={"SIGN-001", "SIGN-001.09", "SIGN-001.10"})
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.11"


def test_requirement_with_more_than_ten_scenarios_parses_in_order_and_allocates_forward() -> None:
    # A requirement carrying >=10 scenarios (.01 .. .12): all parse, none are
    # under-padded, and the next minted sub-id is .13 — proving the per-requirement
    # high-water mark is the INTEGER max (12), not the lexical max (".09" > ".12").
    scenarios = [(f"SIGN-001.{n:02d}", f"scenario {n}") for n in range(1, 13)]
    text = _spec_text(_requirement("SIGN-001", "Many scenarios", scenarios))
    doc = spec_ids.parse_spec(text, "signing")
    assert [s.spec_id for s in doc.scenarios] == [sid for sid, _ in scenarios]
    assert doc.underpadded_scenarios == []
    assert doc.malformed_headings == []
    alloc = spec_ids.SeedAllocator(existing_ids=set(doc.live_ids()))
    assert alloc.allocate_scenario("SIGN-001") == "SIGN-001.13"


def test_two_digit_subid_at_boundary_is_well_padded() -> None:
    # `.09` and `.10` are both exactly 2 digits — neither is an under-pad format
    # violation (the boundary does not accidentally trip Rule 1).
    text = _spec_text(
        _requirement("SIGN-001", "Boundary", [("SIGN-001.09", "nine"), ("SIGN-001.10", "ten")])
    )
    corpus = _corpus((text, "signing"))
    assert corpus.docs[0].underpadded_scenarios == []
    assert spec_ids.check_presence(corpus) == []


def test_subid_high_water_across_ten_boundary_in_no_reuse_check() -> None:
    # End-to-end through check_no_reuse: with sub-ids .02 .. .10 retired (integer
    # high-water 10), re-minting .10 is reuse, while .11 allocates forward. The
    # integer high-water is what distinguishes them — a lexical max would mis-rank.
    retired_rows = "".join(f"| SIGN-001.{n:02d} | withdrawn | |\n" for n in range(2, 11))
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n" + retired_rows
    )
    committed = _corpus(
        (
            _spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")]), ledger=ledger),
            "signing",
        )
    )
    # Re-mint .10 (retired, at the high-water mark) as a live scenario -> reuse.
    reuse = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x"), ("SIGN-001.10", "reused")]),
                ledger=ledger,
            ),
            "signing",
        )
    )
    assert any(
        "SIGN-001.10 reuses a sub-number" in v for v in spec_ids.check_no_reuse(committed, reuse)
    )
    # Allocate forward past the boundary to .11 -> clean.
    forward = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x"), ("SIGN-001.11", "new")]),
                ledger=ledger,
            ),
            "signing",
        )
    )
    assert spec_ids.check_no_reuse(committed, forward) == []


# --- zero-pad edge cases: pad-width-mismatched citation <-> heading ----------


def test_two_digit_citation_resolves_against_single_digit_live_heading() -> None:
    # The vice-versa of the single-digit-citation case: the live heading renders
    # its sub single-digit (a format violation Rule 1 flags separately), yet a
    # 2-digit citation to it still RESOLVES by canonical identity. Reference
    # resolution never depends on the heading's rendered pad width.
    text = _spec_text(
        _requirement("SIGN-001", "First", [("SIGN-001.1", "underpadded live heading")])
    )
    corpus = _corpus((text, "signing"))
    two_digit_ref = spec_ids.Reference(spec_id="SIGN-001.01", line_no=3, source="x.md")
    assert spec_ids.check_references(corpus, extra_references=[two_digit_ref]) == []
    # ... and the under-padded heading is still independently flagged (orthogonal).
    assert any("zero-padded to 2 digits" in v for v in spec_ids.check_presence(corpus))


# --- zero-pad edge cases: 2-digit sub-ids through the retired-ID ledger -------


def test_two_digit_subid_citation_resolves_through_withdrawn_ledger_entry() -> None:
    # A retired scenario sub-id rendered 2-digit in its ledger cell; a citation to
    # it resolves through the ledger (not the live set), in either pad rendering.
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-009.03 | withdrawn | |\n"
    )
    text = _spec_text(
        _requirement(
            "SIGN-001", "Cites a retired sub-id", [("SIGN-001.01", "superseded [SIGN-009.03]")]
        ),
        ledger=ledger,
    )
    corpus = _corpus((text, "signing"))
    # The ledger cell parsed to a 2-digit sub-id entry.
    assert corpus.docs[0].ledger_ids() == ["SIGN-009.03"]
    assert spec_ids.check_references(corpus) == []
    # A single-digit rendering of the same retired sub-id resolves by identity too.
    single = spec_ids.Reference(spec_id="SIGN-009.3", line_no=3, source="x.md")
    assert spec_ids.check_references(corpus, extra_references=[single]) == []


def test_relocated_subid_resolves_to_destination_across_pad_widths() -> None:
    # A relocation whose source and destination are scenario sub-ids: the
    # destination is matched by canonical identity, so a single-digit ledger cell
    # ("ENCRYPT-012.1") still resolves to the 2-digit live destination.
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-006.02 | relocated | ENCRYPT-012.1 |\n"
    )
    text = _spec_text(_requirement("SIGN-001", "First", [("SIGN-001.01", "x")]), ledger=ledger)
    encryption = _spec_text(_requirement("ENCRYPT-012", "Dest", [("ENCRYPT-012.01", "y")]))
    corpus = _corpus((text, "signing"), (encryption, "encryption"))
    assert spec_ids.check_ledger(corpus) == []


def test_pad_widen_retire_via_ledger_is_not_a_vanish() -> None:
    # An identifier committed single-digit and now RETIRED with a 2-digit ledger
    # cell reconciles by canonical identity: it is covered by the ledger entry,
    # not flagged as a silent vanish.
    committed = _corpus(
        (
            _spec_text(
                _requirement("SIGN-001", "A", [("SIGN-001.01", "x")]),
                _requirement("SIGN-009", "Old", [("SIGN-009.1", "gone")]),
            ),
            "signing",
        )
    )
    ledger = (
        "## Relocated and Retired IDs\n\n"
        "| ID | Status | Resolves To |\n"
        "| --- | --- | --- |\n"
        "| SIGN-009 | withdrawn | |\n"
        "| SIGN-009.01 | withdrawn | |\n"
    )
    current = _corpus(
        (
            _spec_text(_requirement("SIGN-001", "A", [("SIGN-001.01", "x")]), ledger=ledger),
            "signing",
        )
    )
    assert spec_ids.check_no_vanish(committed, current) == []


# --- rule 9: no rotting backlog pins in the permanent surfaces ---------------


def test_backlog_pin_detects_slug_after_backlog_keyword() -> None:
    text = "Customer_CMK support is deferred (backlog `customer-cmk-support`).\n"
    pins = spec_ids.find_backlog_slug_pins(text, "openspec/specs/encryption/spec.md")
    assert len(pins) == 1


def test_backlog_pin_detects_slug_before_backlog_keyword() -> None:
    text = "see the suppress-redundant-oci-index-manifests backlog item) are hidden\n"
    pins = spec_ids.find_backlog_slug_pins(text, "cli/cairn_cli/tui/app.py")
    assert len(pins) == 1


def test_backlog_pin_allows_bare_backlog_mention() -> None:
    # Naming the backlog as a concept (no multi-segment slug adjacent) is not a pin.
    text = "deferred work is tracked in the backlog until it graduates into a change\n"
    assert spec_ids.find_backlog_slug_pins(text, "cli/README.md") == []


def test_backlog_pin_allows_non_slug_prose_near_backlog() -> None:
    # "the curated backlog", "backlog item", "backlog TSA-1" (one hyphen + caps)
    # are not multi-segment kebab slugs and must not trip the guard.
    text = (
        "Defer work via the curated backlog, never a doc.\n"
        "TSA procurement per backlog TSA-1 is left for the remigrate change.\n"
        "the backlog item is tracked elsewhere\n"
    )
    assert spec_ids.find_backlog_slug_pins(text, "libs/signing/x.py") == []


def test_backlog_pin_reports_one_violation_per_line_with_line_numbers() -> None:
    text = (
        "clean line one\n"
        "deferred (backlog `worm-restore-tooling`)\n"
        "clean line three\n"
        "the foo-bar-baz-tooling backlog item covers this\n"
    )
    pins = spec_ids.find_backlog_slug_pins(text, "openspec/specs/worm-archive/spec.md")
    assert len(pins) == 2
    assert ":2:" in pins[0]
    assert ":4:" in pins[1]


def test_gather_backlog_slug_pins_clean_on_live_tree() -> None:
    # The committed permanent surfaces must be free of backlog pins (this is the
    # invariant the rule enforces); a regression here means a pin was reintroduced.
    assert spec_ids.gather_backlog_slug_pins() == []


# --- rule 10: glossary governing-requirement links resolve ------------------


def _glossary_fixture_corpus() -> spec_ids.Corpus:
    """A signing capability whose sole requirement is SIGN-009 TimestampProvider Strategy."""
    text = _spec_text(_requirement("SIGN-009", "TimestampProvider Strategy", []))
    return _corpus((text, "signing"))


# GitHub heading anchor of `### Requirement: [SIGN-009] TimestampProvider Strategy`.
_SIGN009_ANCHOR = "requirement-sign-009-timestampprovider-strategy"


def test_glossary_link_matching_text_and_anchor_passes() -> None:
    corpus = _glossary_fixture_corpus()
    line = (
        "- **Governed by** — [TimestampProvider Strategy]"
        f"(specs/signing/spec.md#{_SIGN009_ANCHOR}) ([SIGN-009])"
    )
    pointers = spec_ids.parse_glossary_pointers(line, "GLOSSARY.md")
    assert len(pointers) == 1
    assert pointers[0].anchor == _SIGN009_ANCHOR
    assert spec_ids.check_glossary_anchors(corpus, pointers) == []


def test_glossary_link_stale_text_is_flagged() -> None:
    # A requirement retitle (here the "(Phased)" suffix dropped from SIGN-009)
    # that the glossary link text did not track.
    corpus = _glossary_fixture_corpus()
    line = (
        "- **Governed by** — [TimestampProvider Strategy (Phased)]"
        f"(specs/signing/spec.md#{_SIGN009_ANCHOR}) ([SIGN-009])"
    )
    violations = spec_ids.check_glossary_anchors(
        corpus, spec_ids.parse_glossary_pointers(line, "GLOSSARY.md")
    )
    assert len(violations) == 1
    assert "matches no live requirement name" in violations[0]


def test_glossary_link_stale_anchor_is_flagged() -> None:
    # The link TEXT is correct but the #fragment is stale — e.g. the requirement
    # was retitled, the text updated, the fragment forgotten. The anchor check
    # catches what the text check alone would miss.
    corpus = _glossary_fixture_corpus()
    line = (
        "- **Governed by** — [TimestampProvider Strategy]"
        "(specs/signing/spec.md#requirement-sign-009-timestampprovider-strategy-phased)"
        " ([SIGN-009])"
    )
    violations = spec_ids.check_glossary_anchors(
        corpus, spec_ids.parse_glossary_pointers(line, "GLOSSARY.md")
    )
    assert len(violations) == 1
    assert "stale fragment" in violations[0]


def test_glossary_link_unknown_capability_is_flagged() -> None:
    corpus = _glossary_fixture_corpus()
    line = (
        "- **Governed by** — [Anything](specs/nope-capability/spec.md#requirement-x-001-anything)"
    )
    violations = spec_ids.check_glossary_anchors(
        corpus, spec_ids.parse_glossary_pointers(line, "GLOSSARY.md")
    )
    assert len(violations) == 1
    assert "unknown capability" in violations[0]


def test_glossary_multiple_links_one_bogus() -> None:
    # A field may link several governing requirements; each is checked independently.
    corpus = _glossary_fixture_corpus()
    line = (
        "- **Governed by** — "
        f"[TimestampProvider Strategy](specs/signing/spec.md#{_SIGN009_ANCHOR}), "
        "[Bogus Heading](specs/signing/spec.md#requirement-sign-099-bogus-heading) ([SIGN-009])"
    )
    pointers = spec_ids.parse_glossary_pointers(line, "GLOSSARY.md")
    assert len(pointers) == 2
    violations = spec_ids.check_glossary_anchors(corpus, pointers)
    assert len(violations) == 1
    assert "Bogus Heading" in violations[0]


def test_glossary_bare_file_link_checks_capability_existence() -> None:
    # A `Constrained by` file link with no fragment is valid iff the capability
    # exists; the requirement-name/anchor checks are skipped.
    corpus = _glossary_fixture_corpus()
    ok = spec_ids.parse_glossary_pointers(
        "- **Constrained by** — [signing](specs/signing/spec.md)", "GLOSSARY.md"
    )
    assert len(ok) == 1 and ok[0].anchor is None
    assert spec_ids.check_glossary_anchors(corpus, ok) == []
    bad = spec_ids.parse_glossary_pointers(
        "- **Constrained by** — [nope](specs/nope-capability/spec.md)", "GLOSSARY.md"
    )
    violations = spec_ids.check_glossary_anchors(corpus, bad)
    assert len(violations) == 1
    assert "unknown capability" in violations[0]


def test_glossary_link_outside_pointer_field_ignored() -> None:
    # A spec link in a non-pointer field (e.g. See also) is not a governing link.
    line = f"- **See also** — [TimestampProvider Strategy](specs/signing/spec.md#{_SIGN009_ANCHOR})"
    assert spec_ids.parse_glossary_pointers(line, "GLOSSARY.md") == []


def test_glossary_non_spec_link_ignored() -> None:
    # A non-spec link (e.g. an ADR) inside a pointer field is not a governing link.
    line = "- **Governed by** — [ADR-0019](../docs/adr/0019-actor-model.md)"
    assert spec_ids.parse_glossary_pointers(line, "GLOSSARY.md") == []


def test_check_glossary_anchors_clean_on_live_glossary() -> None:
    # Regression guard: every governing-requirement link in the committed
    # GLOSSARY.md resolves — link text to a live requirement name AND fragment to
    # that requirement's heading anchor. A failure means a requirement was
    # retitled without updating its glossary link (the drift
    # `openspec validate --strict` cannot see).
    corpus = spec_ids.read_current_corpus(_REPO_ROOT / "openspec" / "specs")
    pointers = spec_ids.gather_glossary_pointers(_REPO_ROOT / "openspec" / "GLOSSARY.md")
    assert pointers, "expected the live glossary to carry governing-requirement links"
    assert spec_ids.check_glossary_anchors(corpus, pointers) == []
