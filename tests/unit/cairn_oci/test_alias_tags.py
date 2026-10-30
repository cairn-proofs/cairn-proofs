"""Tests for the mutable alias tag forms.

Covers `libs/oci/cairn_oci/categories.py`:
- Round-trip build → parse → components for both alias forms
  (`{artifact-kind}--_als--{name}`, `{artifact-kind}--_chn--{channel}`).
- Fail-closed parsing of malformed / wrong-arity alias tags.
- Disjointness vs the canonical `_ref` form and between `_als`/`_chn`.
- The pure `is_alias_tag` grammar classifier and `ALIAS_FORMS` set.
- No regression: a canonical `_ref` tag still parses with a str digest.
- The reserved-token registry-consistency import gate.
"""

from __future__ import annotations

import pytest
from cairn_oci.categories import (
    ALIAS_FORMS,
    NAME_SLUG_MAX_LEN,
    TAG_FORM_ALS,
    TAG_FORM_CHN,
    TAG_FORM_REF,
    ArtifactLocator,
    InvalidArtifactKindError,
    InvalidTagError,
    ParsedTag,
    ReservedForm,
    _assert_reserved_token_registry,
    _build_als,
    _build_chn,
    _parse_als,
    alias_tag_of,
    is_alias_tag,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A valid 12-char lowercase-hex digest prefix used by the canonical-tag tests.
_DIGEST = "0123456789ab"


# ---------------------------------------------------------------------------
# Round-trip — both alias forms
# ---------------------------------------------------------------------------


def test_build_named_alias_emits_form() -> None:
    tag = ArtifactLocator().build_named_alias(artifact_kind="catalog", name="nist-800-53-latest")
    assert tag == "catalog--_als--nist-800-53-latest"


def test_build_channel_pointer_emits_form() -> None:
    tag = ArtifactLocator().build_channel_pointer(artifact_kind="catalog", channel="latest")
    assert tag == "catalog--_chn--latest"


def test_named_alias_round_trip() -> None:
    loc = ArtifactLocator()
    tag = loc.build_named_alias(artifact_kind="profile", name="my-baseline")
    parsed = loc.parse_tag(tag)
    assert parsed == ParsedTag(
        artifact_kind="profile",
        name="my-baseline",
        digest=None,
        form=TAG_FORM_ALS,
    )
    # The alias carries no embedded digest.
    assert parsed.digest is None


def test_channel_pointer_round_trip() -> None:
    loc = ArtifactLocator()
    tag = loc.build_channel_pointer(artifact_kind="catalog", channel="stable")
    parsed = loc.parse_tag(tag)
    assert parsed == ParsedTag(
        artifact_kind="catalog",
        name="stable",  # the channel slug lives in the `name` field
        digest=None,
        form=TAG_FORM_CHN,
    )
    assert parsed.digest is None


def test_build_named_alias_accepts_kind_alias_verbatim() -> None:
    # The leading kind routes through the resolver (alias accepted on input) but
    # is preserved verbatim in the tag, exactly like build_ref_tag.
    tag = ArtifactLocator().build_named_alias(artifact_kind="baseline", name="latest")
    assert tag == "baseline--_als--latest"
    assert ArtifactLocator().parse_tag(tag).artifact_kind == "baseline"


# ---------------------------------------------------------------------------
# Build-side fail-closed
# ---------------------------------------------------------------------------


def test_build_named_alias_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidArtifactKindError):
        ArtifactLocator().build_named_alias(artifact_kind="made-up", name="latest")


def test_build_channel_pointer_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidArtifactKindError):
        ArtifactLocator().build_channel_pointer(artifact_kind="made-up", channel="latest")


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",  # empty
        "Latest",  # uppercase
        "with_underscore",  # underscore forbidden
        "-leading-hyphen",  # leading hyphen
        "trailing-hyphen-",  # trailing hyphen
        "double--hyphen",  # `--` would break arity
        "x" * (NAME_SLUG_MAX_LEN + 1),  # over length
    ],
)
def test_build_named_alias_rejects_malformed_slug(bad_slug: str) -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().build_named_alias(artifact_kind="catalog", name=bad_slug)


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",
        "Stable",
        "chan_nel",
        "double--hyphen",
        "x" * (NAME_SLUG_MAX_LEN + 1),
    ],
)
def test_build_channel_pointer_rejects_malformed_slug(bad_slug: str) -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().build_channel_pointer(artifact_kind="catalog", channel=bad_slug)


# ---------------------------------------------------------------------------
# Parse-side fail-closed (wrong arity, malformed slug, bad kind)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "catalog--_als",  # 2 segments (missing name) — wrong arity
        "catalog--_als--n--extra",  # 4 segments — wrong arity (looks like _ref shape)
        f"catalog--_als--n--{_DIGEST}",  # 4 segments — _als with arity 4
        "catalog--_chn",  # 2 segments — wrong arity
        "catalog--_chn--latest--extra",  # 4 segments — wrong arity
        "not-a-category--_als--latest",  # out-of-vocabulary leading segment
        "not-a-category--_chn--latest",  # out-of-vocabulary leading segment
        "catalog--_als--UP_PER",  # malformed name slug (uppercase + underscore)
        "catalog--_chn--with_underscore",  # malformed channel slug
        "catalog--_als--",  # empty name slug
    ],
)
def test_parse_alias_fail_closed(bad: str) -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag(bad)


def test_parse_alias_wrong_arity_carries_offending_tag() -> None:
    bad = f"catalog--_als--n--{_DIGEST}"
    try:
        ArtifactLocator().parse_tag(bad)
    except InvalidTagError as e:
        assert e.tag == bad
        # R11: the rendered message must not echo the raw tag content.
        assert bad not in str(e)
    else:
        raise AssertionError("expected InvalidTagError")


def test_parse_alias_malformed_slug_does_not_leak_segment() -> None:
    # R11 error hygiene: the malformed segment content must not appear in str(e).
    secret = "UP_PER_secret"
    bad = f"catalog--_als--{secret}"
    try:
        ArtifactLocator().parse_tag(bad)
    except InvalidTagError as e:
        assert secret not in str(e)
        assert e.tag == bad
    else:
        raise AssertionError("expected InvalidTagError")


# ---------------------------------------------------------------------------
# Disjointness — canonical vs alias, and between alias forms
# ---------------------------------------------------------------------------


def test_canonical_ref_never_matches_an_alias_form() -> None:
    parsed = ArtifactLocator().parse_tag(f"catalog--_ref--{_DIGEST}")
    assert parsed.form == TAG_FORM_REF
    assert not is_alias_tag(parsed)


def test_als_and_chn_dispatch_to_distinct_forms() -> None:
    loc = ArtifactLocator()
    als = loc.parse_tag("catalog--_als--latest")
    chn = loc.parse_tag("catalog--_chn--latest")
    assert als.form == TAG_FORM_ALS
    assert chn.form == TAG_FORM_CHN
    assert als.form != chn.form


def test_unregistered_reserved_middle_token_still_fails_closed() -> None:
    # A `_`-prefixed but unregistered middle token is fail-closed (not an alias).
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag("catalog--_latest--x")


def test_non_reserved_middle_segment_is_malformed() -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag("catalog--als--latest")  # no leading underscore


# ---------------------------------------------------------------------------
# is_alias_tag classifier + ALIAS_FORMS
# ---------------------------------------------------------------------------


def test_alias_forms_set_contents() -> None:
    assert ALIAS_FORMS == frozenset({TAG_FORM_ALS, TAG_FORM_CHN})
    assert TAG_FORM_REF not in ALIAS_FORMS


def test_is_alias_tag_true_for_alias_forms() -> None:
    loc = ArtifactLocator()
    assert is_alias_tag(loc.parse_tag("catalog--_als--latest"))
    assert is_alias_tag(loc.parse_tag("catalog--_chn--latest"))


def test_is_alias_tag_false_for_canonical() -> None:
    parsed = ArtifactLocator().parse_tag(f"catalog--_ref--{_DIGEST}")
    assert not is_alias_tag(parsed)


# ---------------------------------------------------------------------------
# No regression — canonical _ref still parses with a str digest
# ---------------------------------------------------------------------------


def test_canonical_ref_parse_sets_str_digest() -> None:
    parsed = ArtifactLocator().parse_tag(f"scan-reports--_ref--{_DIGEST}")
    assert parsed.digest == _DIGEST
    assert isinstance(parsed.digest, str)
    assert parsed.form == TAG_FORM_REF
    # The canonical form carries no slug payload (name segment dropped).
    assert parsed.name is None


def test_default_parsed_tag_digest_is_none() -> None:
    # The dataclass default flipped to None; canonical parses always set a str,
    # so this only affects the alias forms and any explicit-default construction.
    assert ParsedTag(artifact_kind="catalog", name="n").digest is None
    assert ParsedTag(artifact_kind="catalog", name="n").form == TAG_FORM_REF


# ---------------------------------------------------------------------------
# Direct handler coverage (the registry dispatches through these)
# ---------------------------------------------------------------------------


def test_parse_als_handler_direct() -> None:
    parsed = _parse_als("catalog--_als--latest", ["catalog", "_als", "latest"])
    assert parsed == ParsedTag(
        artifact_kind="catalog", name="latest", digest=None, form=TAG_FORM_ALS
    )


def test_build_handlers_use_uniform_name_keyword() -> None:
    # Both alias build handlers key on the `name=` slug payload keyword (uniform
    # with _ref), which is what the registry dispatch relies on.
    assert _build_als("catalog", "latest") == "catalog--_als--latest"
    assert _build_chn("catalog", "latest") == "catalog--_chn--latest"


# ---------------------------------------------------------------------------
# Reserved-token registry-consistency gate
# ---------------------------------------------------------------------------


def test_reserved_token_registry_gate_passes_for_canonical_registry() -> None:
    # The real registry must satisfy the import-time gate (mirrors the import).
    from cairn_oci.categories import _RESERVED_TOKENS

    _assert_reserved_token_registry(_RESERVED_TOKENS)


def test_reserved_token_registry_gate_rejects_key_token_mismatch() -> None:
    bad = {"_xyz": ReservedForm("_abc", 3, "x", _parse_als, _build_als)}
    with pytest.raises(ValueError, match="key"):
        _assert_reserved_token_registry(bad)


def test_reserved_token_registry_gate_rejects_malformed_token_shape() -> None:
    bad = {"_toolong": ReservedForm("_toolong", 3, "x", _parse_als, _build_als)}
    with pytest.raises(ValueError, match="namespace shape"):
        _assert_reserved_token_registry(bad)


# ---------------------------------------------------------------------------
# Property — alias round-trip over the full slug space (hypothesis)
# ---------------------------------------------------------------------------

_valid_slug = st.from_regex(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z", fullmatch=True).filter(
    lambda s: len(s) <= NAME_SLUG_MAX_LEN
)
_valid_kind = st.sampled_from(["catalog", "profile", "scan-reports", "attestation"])


@given(kind=_valid_kind, slug=_valid_slug)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_named_alias_round_trip(kind: str, slug: str) -> None:
    loc = ArtifactLocator()
    tag = loc.build_named_alias(artifact_kind=kind, name=slug)
    assert len(tag) <= 128
    parsed = loc.parse_tag(tag)
    assert parsed == ParsedTag(artifact_kind=kind, name=slug, digest=None, form=TAG_FORM_ALS)
    assert is_alias_tag(parsed)


@given(kind=_valid_kind, slug=_valid_slug)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_property_channel_pointer_round_trip(kind: str, slug: str) -> None:
    loc = ArtifactLocator()
    tag = loc.build_channel_pointer(artifact_kind=kind, channel=slug)
    assert len(tag) <= 128
    parsed = loc.parse_tag(tag)
    assert parsed == ParsedTag(artifact_kind=kind, name=slug, digest=None, form=TAG_FORM_CHN)
    assert is_alias_tag(parsed)


# ---------------------------------------------------------------------------
# Slug length boundary — exactly at / one past NAME_SLUG_MAX_LEN
# ---------------------------------------------------------------------------


def test_named_alias_slug_at_max_len_round_trips() -> None:
    # A slug of exactly NAME_SLUG_MAX_LEN chars is the inclusive upper bound:
    # it must build AND round-trip through parse.
    slug = "a" * NAME_SLUG_MAX_LEN
    loc = ArtifactLocator()
    tag = loc.build_named_alias(artifact_kind="catalog", name=slug)
    assert tag == f"catalog--_als--{slug}"
    assert loc.parse_tag(tag) == ParsedTag(
        artifact_kind="catalog", name=slug, digest=None, form=TAG_FORM_ALS
    )


def test_channel_slug_at_max_len_round_trips() -> None:
    slug = "b" * NAME_SLUG_MAX_LEN
    loc = ArtifactLocator()
    tag = loc.build_channel_pointer(artifact_kind="catalog", channel=slug)
    assert loc.parse_tag(tag) == ParsedTag(
        artifact_kind="catalog", name=slug, digest=None, form=TAG_FORM_CHN
    )


def test_named_alias_slug_one_over_max_len_rejected_on_parse() -> None:
    # The build side rejects over-length (covered above); the parse side must
    # ALSO fail closed on an over-length slug (a forged registry-shaped tag).
    over = "a" * (NAME_SLUG_MAX_LEN + 1)
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag(f"catalog--_als--{over}")


def test_channel_slug_one_over_max_len_rejected_on_parse() -> None:
    over = "b" * (NAME_SLUG_MAX_LEN + 1)
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag(f"catalog--_chn--{over}")


# ---------------------------------------------------------------------------
# Slug grammar — leading underscore / embedded `--`
# ---------------------------------------------------------------------------


def test_named_alias_leading_underscore_slug_rejected_on_build() -> None:
    # A leading `_` in the slug is forbidden (the squatting carve-out belongs to
    # the reserved-token namespace, never a user slug).
    with pytest.raises(InvalidTagError):
        ArtifactLocator().build_named_alias(artifact_kind="catalog", name="_leading")


def test_named_alias_leading_underscore_slug_rejected_on_parse() -> None:
    with pytest.raises(InvalidTagError):
        ArtifactLocator().parse_tag("catalog--_als--_leading")


def test_named_alias_double_hyphen_slug_rejected_on_build() -> None:
    # An embedded `--` would forge an extra segment and break the fixed arity.
    with pytest.raises(InvalidTagError):
        ArtifactLocator().build_named_alias(artifact_kind="catalog", name="a--b")


# ---------------------------------------------------------------------------
# alias_tag_of — the pure STRING-level classifier (read-path authority)
# ---------------------------------------------------------------------------


def test_alias_tag_of_returns_named_alias_string() -> None:
    tag = "catalog--_als--latest"
    assert alias_tag_of(tag) == tag


def test_alias_tag_of_returns_channel_pointer_string() -> None:
    tag = "catalog--_chn--stable"
    assert alias_tag_of(tag) == tag


def test_alias_tag_of_rejects_canonical_ref_tag() -> None:
    assert alias_tag_of(f"catalog--_ref--{_DIGEST}") is None


def test_alias_tag_of_rejects_digest_reference() -> None:
    # A by-digest reference is not a tag at all → fail closed to None.
    assert alias_tag_of("sha256:" + "c" * 64) is None


@pytest.mark.parametrize("bad", ["", None, 123, b"catalog--_als--latest", object()])
def test_alias_tag_of_rejects_empty_and_non_str(bad: object) -> None:
    assert alias_tag_of(bad) is None


@pytest.mark.parametrize(
    "bad",
    [
        "catalog--_als",  # wrong arity
        "catalog--_als--UP_PER",  # malformed slug
        "catalog--_latest--x",  # reserved-but-unregistered middle token
        "not-a-category--_als--latest",  # bad leading kind
        "totally-not-a-cairn-tag",  # garbage
    ],
)
def test_alias_tag_of_rejects_malformed_tags(bad: str) -> None:
    assert alias_tag_of(bad) is None
