"""Tests for cairn_oci.categories.

Covers the Artifact_Kind vocabulary + resolver, the single canonical tag grammar
(``{artifact-kind}--_ref--{digest}``), the reserved-token registry, derive_slug,
and digest_prefix. Repository-path construction is covered by ``test_locator.py``.
The category-layout machinery (RepoLayout, CATEGORY_IN_PATH, the legacy
repo-path wrappers) is removed.
"""

from __future__ import annotations

import pytest
from cairn_oci.categories import (
    _ARTIFACT_KIND_ALIASES,
    _DIGEST_PREFIX_LEN,
    _ID_RE,
    _RESERVED_TOKENS,
    ARTIFACT_KINDS,
    NAME_SLUG_MAX_LEN,
    TAG_FORM_REF,
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidTagError,
    ParsedTag,
    ReservedForm,
    _assert_artifact_kind_uniqueness,
    derive_slug,
    digest_prefix,
    normalize_artifact_kind,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A valid 12-char lowercase-hex digest prefix used across the canonical-tag tests.
_DIGEST = "0123456789ab"

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_CANONICAL = frozenset(
    {
        # Group A — OSCAL document roles.
        "catalog",
        "profile",
        "mapping-collection",
        "component-definition",
        "system-security-plan",
        "assessment-plan",
        "assessment-results",
        "plan-of-action-and-milestones",
        # Group B — non-OSCAL document roles.
        "config-snapshot",
        "compliance-report",
        "attestation",
        "system-documentation",
        "governance-doc",
        "tool-output",
        "other",
    }
)


def test_artifact_kinds_are_the_fifteen_canonical_values() -> None:
    assert ARTIFACT_KINDS == _CANONICAL
    assert len(ARTIFACT_KINDS) == 15


def test_artifact_kinds_is_frozen() -> None:
    # Defensive: a mutable categories set would let downstream code corrupt
    # the controlled vocabulary at runtime.
    assert isinstance(ARTIFACT_KINDS, frozenset)


# ---------------------------------------------------------------------------
# normalize_artifact_kind — the single fail-closed resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("canonical", sorted(_CANONICAL))
def test_normalize_canonical_maps_to_itself(canonical: str) -> None:
    assert normalize_artifact_kind(canonical) == canonical


# Spec-authoritative alias → canonical expectations.
_ALIAS_EXPECTATIONS = {
    # Group-B plurals.
    "config-snapshots": "config-snapshot",
    "compliance-reports": "compliance-report",
    "attestations": "attestation",
    "system-documentations": "system-documentation",
    "governance-docs": "governance-doc",
    "tool-outputs": "tool-output",
    "others": "other",
    # OSCAL short forms.
    "control-catalog": "catalog",
    "control-framework": "catalog",
    "baseline": "profile",
    "mapping": "mapping-collection",
    "cdef": "component-definition",
    "ssp": "system-security-plan",
    "ap": "assessment-plan",
    "sap": "assessment-plan",
    "ar": "assessment-results",
    "sar": "assessment-results",
    "poam": "plan-of-action-and-milestones",
    "mitigation-plan": "plan-of-action-and-milestones",
    # Governance sub-types.
    "policy": "governance-doc",
    "policies": "governance-doc",
    "standard": "governance-doc",
    "standards": "governance-doc",
    "guideline": "governance-doc",
    "guidelines": "governance-doc",
    # Output synonyms.
    "scan-report": "tool-output",
    "scan-reports": "tool-output",
    "api-output": "tool-output",
    "api-outputs": "tool-output",
}


@pytest.mark.parametrize(("alias", "canonical"), sorted(_ALIAS_EXPECTATIONS.items()))
def test_normalize_alias_maps_to_expected_canonical(alias: str, canonical: str) -> None:
    assert normalize_artifact_kind(alias) == canonical


def test_alias_map_matches_spec_authoritative_expectations() -> None:
    # The real module map must contain at minimum every spec-listed alias,
    # each binding to the expected canonical.
    for alias, canonical in _ALIAS_EXPECTATIONS.items():
        assert _ARTIFACT_KIND_ALIASES[alias] == canonical


# Plural/synonym aliases that MUST keep resolving. `scan-reports` is a legitimate
# output synonym and `config-snapshots` the natural plural of `config-snapshot`;
# both are valid aliases, not invalid-kind tokens.
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("scan-reports", "tool-output"),
        ("config-snapshots", "config-snapshot"),
        ("attestations", "attestation"),
    ],
)
def test_surviving_plural_synonym_aliases_still_resolve(alias: str, canonical: str) -> None:
    assert normalize_artifact_kind(alias) == canonical


# These four tokens are not aliases; the resolver fail-closes with the standard
# invalid-kind rejection.
@pytest.mark.parametrize(
    "removed",
    ["policy-document", "policy-documents", "poam-evidence", "general"],
)
def test_removed_backcompat_aliases_now_fail_closed(removed: str) -> None:
    with pytest.raises(InvalidArtifactKindError):
        normalize_artifact_kind(removed)


@pytest.mark.parametrize(
    "removed",
    ["policy-document", "policy-documents", "poam-evidence", "general"],
)
def test_removed_backcompat_aliases_absent_from_token_space(removed: str) -> None:
    # Structural complement to the behavioral fail-closed test: the four tokens
    # are absent from BOTH the alias map AND the canonical set — a regression
    # that re-added `general` as a canonical value (rather than an alias) would
    # still satisfy the resolver-rejection test's expectation only by accident,
    # so assert the token never resolves AT ALL by being present in neither
    # structure.
    assert removed not in _ARTIFACT_KIND_ALIASES
    assert removed not in ARTIFACT_KINDS


@pytest.mark.parametrize("unknown", ["screenshot", "not-a-category", "made-up", ""])
def test_normalize_unknown_token_fails_closed(unknown: str) -> None:
    # Fail-closed: never a silent default.
    with pytest.raises(InvalidArtifactKindError):
        normalize_artifact_kind(unknown)


@pytest.mark.parametrize(
    "mixed_case",
    [
        "Catalog",  # canonical, capitalized
        "CATALOG",  # canonical, upper
        "Attestation",  # canonical, capitalized
        "Attestations",  # alias, capitalized
        "BASELINE",  # alias, upper
        "PoaM",  # alias, mixed
        "Policies",  # alias, capitalized
    ],
)
def test_normalize_is_exact_lowercase_not_case_folded(mixed_case: str) -> None:
    # The resolver matches EXACT lowercase only — it does NOT case-fold. A
    # mixed/upper-case spelling of an otherwise-valid token is rejected. Pinning
    # this makes a future case-folding change a deliberate, test-breaking
    # decision rather than a silent behavior drift (brief item 2).
    with pytest.raises(InvalidArtifactKindError):
        normalize_artifact_kind(mixed_case)


@pytest.mark.parametrize(
    "whitespaced",
    [
        " catalog",  # leading space
        "catalog ",  # trailing space
        "\tcatalog",  # leading tab
        "catalog\n",  # trailing newline (the _ID_RE `$`-vs-newline gotcha class)
        "attes tation",  # internal space
        "  ",  # whitespace-only
        " ",  # single space
    ],
)
def test_normalize_does_not_strip_surrounding_whitespace(whitespaced: str) -> None:
    # The resolver performs no whitespace normalization: a token padded with
    # leading/trailing/internal whitespace does not match its bare canonical and
    # fails closed. (Guards against a trailing-newline bypass of the kind the
    # control-id `$`-anchor gotcha bit elsewhere in the codebase.)
    with pytest.raises(InvalidArtifactKindError):
        normalize_artifact_kind(whitespaced)


def test_invalid_kind_error_lists_canonical_values() -> None:
    try:
        normalize_artifact_kind("screenshot")
    except InvalidArtifactKindError as e:
        msg = str(e)
        for canonical in _CANONICAL:
            assert canonical in msg
        # These raw values are aliases, not in the allowed-values list.
        assert "allowed canonical values" in msg
    else:
        raise AssertionError("expected InvalidArtifactKindError")


def test_invalid_kind_error_carries_offending_value() -> None:
    try:
        normalize_artifact_kind("nope")
    except InvalidArtifactKindError as e:
        assert e.kind == "nope"
    else:
        raise AssertionError("expected InvalidArtifactKindError")


# ---------------------------------------------------------------------------
# Global-uniqueness invariant (Threat T4)
# ---------------------------------------------------------------------------


def test_real_alias_map_satisfies_uniqueness() -> None:
    # Holds against the production map (also enforced at import time).
    _assert_artifact_kind_uniqueness(ARTIFACT_KINDS, _ARTIFACT_KIND_ALIASES)


def test_no_alias_key_equals_a_canonical_value() -> None:
    assert not (set(_ARTIFACT_KIND_ALIASES) & set(ARTIFACT_KINDS))


def test_every_alias_maps_to_a_canonical_value() -> None:
    assert set(_ARTIFACT_KIND_ALIASES.values()) <= set(ARTIFACT_KINDS)


def test_uniqueness_check_rejects_alias_colliding_with_canonical() -> None:
    # A synthetic map whose alias key IS a canonical value must fail — tested
    # against the check function directly, never by mutating the real map.
    colliding = dict(_ARTIFACT_KIND_ALIASES)
    colliding["catalog"] = "profile"  # alias key == canonical "catalog"
    with pytest.raises(ValueError, match="catalog"):
        _assert_artifact_kind_uniqueness(ARTIFACT_KINDS, colliding)


def test_uniqueness_check_rejects_alias_to_noncanonical_value() -> None:
    bad = dict(_ARTIFACT_KIND_ALIASES)
    bad["totally-new-alias"] = "not-a-canonical-value"
    with pytest.raises(ValueError, match="totally-new-alias"):
        _assert_artifact_kind_uniqueness(ARTIFACT_KINDS, bad)


# ---------------------------------------------------------------------------
# Single canonical tag form: derive_slug, round-trip, content-discriminated
# parse, reserved registry, fail-closed namespace, squatting invariant, hygiene.
# (derived-ref-id-discriminator)
# ---------------------------------------------------------------------------

_LOCATOR = ArtifactLocator()


# -- derive_slug edge cases --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "max_len", "expected"),
    [
        ("NIST SP 800-53 Rev. 5", 40, "nist-sp-800-53-rev-5"),  # lowercase + collapse runs
        ("Hello,  World!!!", 40, "hello-world"),  # punctuation runs collapse to one '-'
        ("  --trim--  ", 40, "trim"),  # leading/trailing separators stripped
        ("UPPER_lower", 40, "upper-lower"),  # underscore is a separator (never kept)
        ("a___b", 40, "a-b"),  # underscore run collapses
        ("5.1.0", 40, "5-1-0"),  # dots are separators
        ("café", 40, "caf"),  # non-ASCII dropped (é is a separator)
        ("日本語", 40, None),  # all-non-ASCII -> empty -> None
        ("", 40, None),  # empty input -> None
        ("!!!", 40, None),  # all-separator -> None
        ("---", 40, None),  # only hyphens -> None
    ],
)
def test_derive_slug_edge_cases(text: str, max_len: int, expected: str | None) -> None:
    assert derive_slug(text, max_len) == expected


def test_derive_slug_truncates_on_hyphen_boundary() -> None:
    # "alpha-beta-gamma" (16 chars) capped at 12 -> "alpha-beta" (drops the
    # partial trailing token, never leaves a dangling hyphen).
    assert derive_slug("alpha-beta-gamma", 12) == "alpha-beta"


def test_derive_slug_truncation_yields_render_safe_slug() -> None:
    from cairn_oci.categories import _SLUG_RE

    out = derive_slug("one-two-three-four-five-six", 10)
    assert out is not None
    assert _SLUG_RE.match(out)
    assert len(out) <= 10
    assert not out.endswith("-")


def test_derive_slug_non_str_returns_none() -> None:
    assert derive_slug(None, 40) is None  # type: ignore[arg-type]


# -- digest_prefix helper ----------------------------------------------------


def test_digest_prefix_strips_sha256_and_lowercases() -> None:
    # A full `sha256:`-prefixed digest yields the leading 12 lowercase-hex chars.
    out = digest_prefix("sha256:0123456789ABCDEFdeadbeef" + "0" * 38)
    assert out == "0123456789ab"
    assert len(out) == _DIGEST_PREFIX_LEN


def test_digest_prefix_without_algorithm_prefix() -> None:
    out = digest_prefix("abcdef0123456789" + "0" * 48)
    assert out == "abcdef012345"


def test_digest_prefix_lowercases_bare_hex() -> None:
    assert digest_prefix("ABCDEF012345" + "0" * 52) == "abcdef012345"


@pytest.mark.parametrize(
    "bad",
    [
        "sha256:short",  # too short to yield 12 hex chars
        "abc",  # bare, too short
        "sha256:zzzzzzzzzzzz" + "z" * 52,  # non-hex
        "",  # empty
        "sha256:",  # only the algorithm prefix
    ],
)
def test_digest_prefix_rejects_non_conforming(bad: str) -> None:
    with pytest.raises(InvalidTagError) as ei:
        digest_prefix(bad)
    assert ei.value.form_class == "digest"


def test_digest_prefix_non_str_raises() -> None:
    with pytest.raises(InvalidTagError):
        digest_prefix(None)  # type: ignore[arg-type]


def test_digest_prefix_message_omits_raw_digest() -> None:
    # R11: the message names the form class + bounded reason, not the raw value.
    secret = "sha256:" + "SECRETnonhexZZZZ" + "z" * 42
    try:
        digest_prefix(secret)
    except InvalidTagError as e:
        assert "SECRET" not in str(e)
        assert e.form_class == "digest"
    else:
        raise AssertionError("expected InvalidTagError")


# -- canonical form round-trip ([INGEST-021.09]) -----------------------------


def test_canonical_tag_round_trips() -> None:
    # The single canonical form is the
    # 3-segment {artifact-kind}--_ref--{digest}; it carries NO name segment.
    tag = _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=_DIGEST)
    assert tag == f"catalog--_ref--{_DIGEST}"
    parsed = _LOCATOR.parse_tag(tag)
    assert parsed == ParsedTag(
        artifact_kind="catalog",
        name=None,
        digest=_DIGEST,
        form=TAG_FORM_REF,
    )
    assert parsed.form == TAG_FORM_REF
    # The canonical form carries no slug payload.
    assert parsed.name is None


def test_canonical_tag_build_is_reproducible() -> None:
    # ECR Tag Format / Decision D3: because the digest is a pure function of
    # content bytes, building the same (artifact_kind, digest) twice yields the
    # byte-identical tag — re-push idempotency holds.
    first = _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=_DIGEST)
    second = _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=_DIGEST)
    assert first == second


def test_canonical_tag_parse_of_build_round_trips() -> None:
    # parse(build(...)) recovers the original components (kind + digest) for the
    # canonical form; name is None (no name segment).
    tag = _LOCATOR.build_ref_tag(artifact_kind="ssp", digest=_DIGEST)
    parsed = _LOCATOR.parse_tag(tag)
    assert (parsed.artifact_kind, parsed.name, parsed.digest) == ("ssp", None, _DIGEST)


def test_distinct_content_digests_yield_distinct_tags() -> None:
    # ECR Tag Format: two artifacts with distinct content (distinct digest
    # prefixes) derive distinct tags by virtue of the collision-breaking digest
    # segment, holding artifact_kind fixed.
    other_digest = "fedcba987654"
    assert other_digest != _DIGEST
    tag_a = _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=_DIGEST)
    tag_b = _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=other_digest)
    assert tag_a != tag_b


def test_canonical_tag_accepts_alias_leading_kind() -> None:
    # A leading alias resolves but is preserved verbatim.
    parsed = _LOCATOR.parse_tag(f"ssp--_ref--{_DIGEST}")
    assert parsed.artifact_kind == "ssp"
    assert parsed.form == TAG_FORM_REF
    assert parsed.name is None
    assert parsed.digest == _DIGEST


def test_digest_looking_hex_tail_parses_as_terminal_digest() -> None:
    # The trailing segment is the terminal digest: an all-hex 12-char tail is the
    # digest, never re-interpreted as a name (there is no name segment).
    parsed = _LOCATOR.parse_tag(f"catalog--_ref--{_DIGEST}")
    assert parsed.digest == _DIGEST
    assert parsed.name is None
    assert parsed.form == TAG_FORM_REF


def test_build_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidArtifactKindError):
        _LOCATOR.build_ref_tag(artifact_kind="not-a-kind", digest=_DIGEST)


def test_build_ref_no_longer_accepts_name_argument() -> None:
    # Public-surface hard cut: build_ref_tag's keyword-only `name` parameter is
    # removed; passing it is a TypeError (downstream waves must drop the arg).
    with pytest.raises(TypeError):
        _LOCATOR.build_ref_tag(  # type: ignore[call-arg]
            artifact_kind="catalog", name="x", digest=_DIGEST
        )


@pytest.mark.parametrize(
    "digest",
    [
        _DIGEST.upper(),  # uppercase hex is rejected (lowercase only)
        "0123456789a",  # too short (11 chars)
        "0123456789abc",  # too long (13 chars)
        "0123456789ag",  # non-hex char
        f"{_DIGEST}\n",  # trailing newline
        "sha256:0123",  # the build side takes a bare prefix, not sha256:-form
        "",
    ],
)
def test_build_rejects_bad_digest(digest: str) -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.build_ref_tag(artifact_kind="catalog", digest=digest)


# -- length ceiling ----------------------------------------------------------


def test_assembled_canonical_tag_is_well_under_128() -> None:
    # With the name segment dropped, the canonical tag is fixed-shape
    # {kind}--_ref--{12-hex}: the longest kind (29) + 4 + 12 + 4 separators = 49
    # chars, well under the ECR 128-char limit. No truncation path remains.
    kind = "plan-of-action-and-milestones"  # 29 chars, the longest canonical
    tag = _LOCATOR.build_ref_tag(artifact_kind=kind, digest=_DIGEST)
    assert tag == f"{kind}--_ref--{_DIGEST}"
    assert len(tag) <= 128
    parsed = _LOCATOR.parse_tag(tag)
    assert parsed.digest == _DIGEST
    assert parsed.artifact_kind == kind
    assert parsed.name is None


# -- arity discrimination + hard cut on legacy 4-segment ---------------------


@pytest.mark.parametrize(
    "bad",
    [
        f"catalog--_ref--a-name--{_DIGEST}",  # 4 segments: legacy name-bearing form
        f"catalog--_ref--name--extra--{_DIGEST}",  # 5 segments (extra)
        "catalog--_ref--name--00",  # 4 segments
        "catalog--_ref",  # 2 segments
    ],
)
def test_wrong_arity_ref_is_error(bad: str) -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(bad)


def test_legacy_four_segment_ref_fails_closed_not_read_as_alias() -> None:
    # Hard cut: a legacy 4-segment _ref tag is REJECTED with an
    # InvalidTagError carrying the _ref form class — it is NEVER silently re-read
    # as an alias (alias forms are discriminated by the reserved MIDDLE token
    # _als/_chn, not by arity, so a 4-segment _ref can only fail the arity check).
    legacy = f"catalog--_ref--nist-sp-800-53-rev-5--{_DIGEST}"
    with pytest.raises(InvalidTagError) as ei:
        _LOCATOR.parse_tag(legacy)
    assert ei.value.form_class == f"reserved-{TAG_FORM_REF}"
    # The reserved middle token is still _ref (not misclassified as an alias).
    assert "_als" not in str(ei.value)
    assert "_chn" not in str(ei.value)


def test_three_segment_ref_parses_but_four_segment_does_not() -> None:
    # Direct arity-discrimination contrast: the 3-segment form parses; the
    # otherwise-identical 4-segment form (with a name spliced in) does not.
    three = f"catalog--_ref--{_DIGEST}"
    four = f"catalog--_ref--some-name--{_DIGEST}"
    assert _LOCATOR.parse_tag(three).digest == _DIGEST
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(four)


def test_non_reserved_middle_is_error_no_bare_coordinate_form() -> None:
    # Every canonical tag carries the `_ref` middle token: a non-`_`-prefixed
    # middle segment is malformed, never read as a coordinate form.
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--2026-q1--{_DIGEST}")


def test_single_segment_tag_is_error() -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag("catalog")


def test_parse_rejects_unknown_leading_kind() -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"not-a-kind--_ref--{_DIGEST}")


@pytest.mark.parametrize(
    "digest_seg",
    [
        _DIGEST.upper(),  # uppercase hex
        "0123456789a",  # too short
        "0123456789abc",  # too long
        "0123456789ag",  # non-hex
        f"{_DIGEST}\n",  # trailing newline (the \Z gotcha on the terminal segment)
    ],
)
def test_parse_rejects_bad_digest_segment(digest_seg: str) -> None:
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--_ref--{digest_seg}")


# -- fail-closed reserved namespace ------------------------------------------


@pytest.mark.parametrize("token", ["_latest", "_pinned", "_alias", "_", "_ref2"])
def test_unallocated_reserved_token_fails_closed(token: str) -> None:
    # A `_`-prefixed middle segment that is not a REGISTERED reserved token
    # raises — never a silent default.
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"catalog--{token}--{_DIGEST}")


def test_alias_leading_token_form_fails_closed() -> None:
    # A tag LEADING with a reserved token (no resolvable artifact-kind) fails
    # closed: a real artifact-kind can never carry a leading underscore, so the
    # canonical _ref form can never be forged with a reserved leading segment.
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"_als--_ref--{_DIGEST}")
    with pytest.raises(InvalidTagError):
        _LOCATOR.parse_tag(f"_chn--_ref--{_DIGEST}")


def test_reserved_registry_is_the_single_extension_point() -> None:
    assert TAG_FORM_REF in _RESERVED_TOKENS
    form = _RESERVED_TOKENS[TAG_FORM_REF]
    assert isinstance(form, ReservedForm)
    assert form.token == TAG_FORM_REF
    # _ref is arity 3 (kind, _ref, digest).
    assert form.arity == 3
    assert callable(form.parse_handler)
    assert callable(form.build_handler)
    assert form.scope_rule  # non-empty human-readable note
    # Every registered token lives in the reserved (`_`-prefixed) namespace.
    assert all(token.startswith("_") for token in _RESERVED_TOKENS)


# -- squatting invariant: _ID_RE forbids leading underscore ------------------


@pytest.mark.parametrize("squatter", ["_ref", "_latest", "_", "_anything"])
def test_id_re_forbids_leading_underscore(squatter: str) -> None:
    assert _ID_RE.match(squatter) is None


def test_id_re_uses_end_of_string_anchor_not_dollar() -> None:
    assert _ID_RE.match("catalog\n") is None
    assert _ID_RE.match("catalog") is not None


def test_underscore_leading_kind_is_not_a_valid_artifact_kind() -> None:
    # A `_`-prefixed token can never resolve as an artifact-kind, so it can never
    # become the leading segment of a canonical tag (the squatting carve-out).
    with pytest.raises(InvalidArtifactKindError):
        normalize_artifact_kind("_ref")
    with pytest.raises(InvalidArtifactKindError):
        _LOCATOR.build_ref_tag(artifact_kind="_ref", digest=_DIGEST)


# -- error-message hygiene: no raw content leaked ----------------------------


def test_invalid_tag_error_message_omits_raw_tag_content() -> None:
    secret = f"catalog--_ref--SECRETxyz--{_DIGEST}--extra"
    try:
        _LOCATOR.parse_tag(secret)
    except InvalidTagError as e:
        assert "SECRETxyz" not in str(e)
        assert e.tag == secret  # raw value preserved structurally for routing
        assert e.form_class  # a class label is reported
    else:
        raise AssertionError("expected InvalidTagError")


def test_invalid_tag_error_unallocated_reports_class_not_token() -> None:
    weird = f"catalog--_TOTALLYWEIRD--{_DIGEST}"
    try:
        _LOCATOR.parse_tag(weird)
    except InvalidTagError as e:
        assert "_TOTALLYWEIRD" not in str(e)
        assert e.form_class == "reserved-unallocated"
        assert e.tag == weird
    else:
        raise AssertionError("expected InvalidTagError")


def test_invalid_tag_error_unknown_kind_omits_raw_kind() -> None:
    weird = f"WEIRDKIND--_ref--{_DIGEST}"
    try:
        _LOCATOR.parse_tag(weird)
    except InvalidTagError as e:
        assert "WEIRDKIND" not in str(e)
        assert e.form_class == f"reserved-{TAG_FORM_REF}"
    else:
        raise AssertionError("expected InvalidTagError")


# -- [INGEST-021.09] round-trip over generated valid components (hypothesis) --

_valid_digest = st.from_regex(r"\A[0-9a-f]{12}\Z", fullmatch=True)


@given(
    kind=st.sampled_from(sorted(ARTIFACT_KINDS)),
    digest=_valid_digest,
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property6_canonical_round_trip(kind: str, digest: str) -> None:
    # The single canonical form is {kind}--_ref--{digest}; parse recovers exactly
    # (kind, None, digest) — there is no name segment.
    tag = _LOCATOR.build_ref_tag(artifact_kind=kind, digest=digest)
    parsed = _LOCATOR.parse_tag(tag)
    assert parsed == ParsedTag(artifact_kind=kind, name=None, digest=digest, form=TAG_FORM_REF)


# ---------------------------------------------------------------------------
# Cross-cutting regression (derived-ref-id-discriminator tester gate)
# ---------------------------------------------------------------------------
# [INGEST-021.09] end-to-end from RAW producer free text: the existing property test
# starts from already-conforming slugs; these start from realistic OSCAL
# `metadata.title` / filename strings, run them through the `derive_slug` trust
# boundary, pair them with a `digest_prefix` of a full Manifest_Digest, and
# assert the build→parse round-trip recovers exactly what the pipeline emits.


@pytest.mark.parametrize(
    ("title", "full_digest", "kind"),
    [
        # Long title (would once have been slugged into the tag).
        (
            "NIST Special Publication 800-53 Revision 5 Security and Privacy Controls",
            "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "catalog",
        ),
        # A non-OSCAL filename-style title.
        ("acme-scan-2026.json", "sha256:" + "ab" * 32, "tool-output"),
        # Punctuation-heavy title.
        ("Acme, Inc. — System Security Plan (v2)", "sha256:" + "cd" * 32, "system-security-plan"),
    ],
)
def test_canonical_tag_carries_no_producer_free_text(
    title: str, full_digest: str, kind: str
) -> None:
    # drop-canonical-tag-name-segment: the canonical _ref tag is a pure function
    # of (validated kind, content digest) — producer free text (an OSCAL title or
    # filename) NEVER enters it, even where derive_slug would yield a valid slug.
    # derive_slug still exists (for the mutable alias follow-on); we compute the
    # slug here only to prove it is NOT present in the canonical tag.
    name = derive_slug(title, NAME_SLUG_MAX_LEN)
    digest = digest_prefix(full_digest)
    assert name is not None
    assert len(digest) == _DIGEST_PREFIX_LEN

    tag = _LOCATOR.build_ref_tag(artifact_kind=kind, digest=digest)
    assert tag == f"{kind}--_ref--{digest}"
    assert name not in tag  # the derived slug does not appear in the address
    assert len(tag) <= 128
    parsed = _LOCATOR.parse_tag(tag)
    assert parsed == ParsedTag(artifact_kind=kind, name=None, digest=digest, form=TAG_FORM_REF)


# -- derive_slug truncation boundary edges -----------------------------------


def test_derive_slug_truncation_boundary_drops_partial_trailing_token() -> None:
    # "ab-cd-ef" capped at 5: slug[:5] == "ab-cd"; rfind('-')==2 drops the
    # partial token -> "ab" (never split a token mid-word, never trail a hyphen).
    assert derive_slug("ab-cd-ef", 5) == "ab"


def test_derive_slug_token_fitting_exactly_at_max_is_kept_whole() -> None:
    # "abcd-ef" capped at 4: slug[:4] == "abcd" has no hyphen to split on, so the
    # whole leading token is kept (rfind returns -1, the boundary branch is
    # skipped) rather than degenerating to None or an empty slug.
    assert derive_slug("abcd-ef", 4) == "abcd"


def test_derive_slug_trailing_hyphen_at_cap_is_stripped() -> None:
    # "abc-defg" capped at 4: slug[:4] == "abc-"; rfind('-')==3 -> "abc". The
    # truncation lands exactly on a hyphen and the dangling hyphen is removed.
    out = derive_slug("abc-defg", 4)
    assert out == "abc"
    assert not out.endswith("-")
