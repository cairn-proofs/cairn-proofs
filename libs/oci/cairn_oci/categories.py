"""Artifact_Kind vocabulary, ECR repository-name + image-tag construction.

The :class:`ArtifactLocator` is the single authority that maps
``(deployment_qualifier, org_id, tenant_id, project_id)`` onto an ECR
``repo_name`` and that builds/parses the single canonical image-tag form
``{artifact-kind}--_ref--{digest}`` ([INGEST-018], [INGEST-021]). There is
exactly one repository path shape — the uniform three-segment logical hierarchy
``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}`` — with nothing
branching per segment.

The canonical tag is derived purely from the validated artifact's intrinsic
metadata (see the artifact-ingestion "ECR Tag Format"):

- ``artifact-kind`` — drawn from the ``Artifact_Kind`` controlled vocabulary and
  **sourced (by the caller, at runtime) from the Producer_Attestation_Referrer's
  ``artifactKind`` claim field**, NOT from a content-manifest annotation
  (not a content-manifest annotation; see
  :class:`cairn_oci.producer_attestation.ProducerAttestationReferrer`). It is validated against
  the vocabulary at validate time before the tag is minted;
- ``digest`` — the leading lowercase-hex prefix of the bundle's **reproducible
  content** Manifest_Digest (:func:`digest_prefix`). The digest is a
  pure function of content bytes, so re-pushing the same
  content derives the identical ``digest`` segment.

The canonical tag carries NO ``name`` segment — no producer free text and no
content-type branch ever enters the immutable canonical address, so it cannot
misrepresent a multi-file bundle by naming it after one member and grants OSCAL
no naming privilege. Memorable human naming is the job of the mutable
``{artifact-kind}--_als--{name}`` alias form (see :meth:`build_named_alias`),
which still carries a render-safe slug derived via :func:`derive_slug`.

There is NO assessment-period segment and NO control-scope segment in the
canonical tag — period and control-scope are Producer_Attestation_Referrer claim fields,
never encoded in any tag.

The Manifest_Digest prefix is the terminal collision-breaker: the tag is a
deterministic function of the validated artifact's content, so the same content
→ identical tag (idempotent re-push, absorbed by ECR tag immutability); distinct
content → distinct tag. There is nothing to allocate.

A single repository holds every artifact-kind for a project; the artifact-kind
is carried in the image tag's leading segment, never in the repository path
([INGEST-018]). The repository path encodes only the three logical-hierarchy
segments::

    artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}
    tag:                  {artifact-kind}--_ref--{digest}

The producing identity (the AWS account, other cloud, or on-prem host a bundle
is pushed from) is NEVER a path segment — it is recorded per-artifact through
the signed claim, the CloudTrail push principal, and the signature.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

# The fifteen canonical Artifact_Kind values. Group A (8) are the
# verbatim OSCAL schema root names — kept un-singularized so they double as the
# OSCAL document-root identifier for the opportunistic OSCAL cross-check. Group B
# (7) are our own singular non-OSCAL document-role labels.
ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {
        # Group A — OSCAL document roles (verbatim schema root names).
        "catalog",
        "profile",
        "mapping-collection",
        "component-definition",
        "system-security-plan",
        "assessment-plan",
        "assessment-results",
        "plan-of-action-and-milestones",
        # Group B — non-OSCAL document roles (singular).
        "config-snapshot",
        "compliance-report",
        "attestation",
        "system-documentation",
        "governance-doc",
        "tool-output",
        "other",
    }
)

# alias → canonical. Every key is a non-canonical token that normalizes on input
# to its canonical value via the single :func:`normalize_artifact_kind` resolver
# on input. The set is authoritative against the artifact-ingestion *Reject
# Invalid Artifact_Kind* rules. Global uniqueness (no alias equals a
# canonical, no duplicate key, every value is canonical) is asserted at import
# time by :func:`_assert_artifact_kind_uniqueness` below.
_ARTIFACT_KIND_ALIASES: Final[dict[str, str]] = {
    # Natural plural of every Group-B singular canonical.
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
    # Governance sub-types that collapse to one kind.
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


def _assert_artifact_kind_uniqueness(canonical: frozenset[str], aliases: dict[str, str]) -> None:
    """Fail-closed global-uniqueness check over the canonical + alias token space.

    Asserts: no alias key collides with a canonical value, no alias
    value is non-canonical, and — since a Python ``dict`` literal already
    deduplicates keys — every token across the combined canonical-and-alias set
    resolves to exactly one canonical value. Run at module import AND as a unit
    test so neither a test-skip nor a lazy import can let an ambiguous map reach
    production. Raises :class:`ValueError` naming the colliding token.
    """
    for alias, target in aliases.items():
        if alias in canonical:
            raise ValueError(f"artifact-kind alias {alias!r} collides with a canonical value")
        if target not in canonical:
            raise ValueError(
                f"artifact-kind alias {alias!r} maps to non-canonical value {target!r}"
            )


def normalize_artifact_kind(token: str) -> str:
    """Resolve an artifact-kind token to its canonical value.

    A canonical token maps to itself; a known alias maps to its canonical value;
    anything else raises :class:`InvalidArtifactKindError`. This is the single
    fail-closed resolver every read site (the membership gate and ``parse_tag``'s
    leading-segment check) routes through — never a default. Matching is exact
    (lowercase); case-folding is not performed.
    """
    if token in ARTIFACT_KINDS:
        return token
    canonical = _ARTIFACT_KIND_ALIASES.get(token)
    if canonical is None:
        raise InvalidArtifactKindError(token)
    return canonical


# Lowercase alphanumeric + hyphens, no leading/trailing hyphen, 1-64 chars.
#
# Squatting invariant: the leading character class is
# ``[a-z0-9]`` — it forbids a leading underscore, so NO Artifact_Kind can ever
# shadow a reserved token (the reserved namespace is the ``_``-prefixed one,
# carved out of the grammar at the regex level). Anchored with ``\Z`` (NOT
# ``$``): Python's ``$`` also matches just before a trailing ``\n``, so a value
# like ``catalog\n`` would slip past a ``$``-anchored grammar yet not be
# render-safe. ``\Z`` matches end-of-string only, closing the trailing-newline
# injection class.
_ID_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?\Z")

# {regionCode}-{stage} per the Deployment_Qualifier convention.
_QUALIFIER_RE = re.compile(r"\A[a-z0-9]+-[a-z0-9]+\Z")

# Render-safe slug grammar for the alias ``name``/``channel`` tag segments
# . The canonical ``_ref`` form
# carries no slug segment; this grammar gates only the
# mutable ``_als``/``_chn`` slug payloads. Lowercase ASCII alphanumerics with
# SINGLE internal hyphens only — no leading/trailing or consecutive hyphens, no
# underscore, no ``--``. Anchored with ``\A``/``\Z`` (NOT ``$``): Python's ``$``
# also matches just before a trailing ``\n``, so a value like ``name\n`` would
# slip past a ``$``-anchored grammar yet not be render-safe. ``\Z`` matches
# end-of-string only, closing the trailing-newline injection class. A ``--`` in
# a segment would also break the ``parse_tag`` arity and the [INGEST-021.09]
# round-trip; the single-internal-hyphen rule forbids it.
_SLUG_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")

# Length bound for the alias ``name``/``channel`` slug.
# With the longest kind (29) + ``_als``/``_chn`` (4) + 2 separators (4) =
# 37 fixed, ~91 remain of the ECR 128-char tag limit; a slug <= 40 fits
# comfortably (the canonical ``_ref`` form carries no slug). Public because an
# alias-slug PRODUCER (e.g.
# ``cairn alias`` deriving a slug via :func:`derive_slug`) and the build/parse
# grammar (the slug GATE, here) must agree on the same bound — one source of
# truth, so a producer never slugs past a limit the grammar then rejects.
NAME_SLUG_MAX_LEN: Final[int] = 40

# The ``digest`` segment grammar: a fixed-length lowercase-hex prefix of the
# bundle's Manifest_Digest. Anchored ``\A``/``\Z`` (the same
# trailing-newline-safety reason ``_SLUG_RE`` carries); a short, over-long,
# uppercase, or non-hex digest segment is a fail-closed parse error.
_DIGEST_PREFIX_LEN: Final[int] = 12
_DIGEST_RE = re.compile(rf"\A[0-9a-f]{{{_DIGEST_PREFIX_LEN}}}\Z")

# The literal segment separator. Unambiguous because every token uses at most
# single internal hyphens.
_TAG_SEP: Final[str] = "--"


def _truncate_on_hyphen(slug: str, max_len: int) -> str:
    """Cut ``slug`` to ``<= max_len`` on a hyphen boundary, dropping a dangling ``-``.

    Drops a partial trailing token (truncate at the last internal ``-``) so the
    slug stays meaningful and never ends mid-word or with a trailing hyphen. May
    return an empty string if no internal hyphen survives the cut; callers decide
    whether that is a None (no derivable slug) or a fail-closed error.
    """
    if len(slug) <= max_len:
        return slug
    truncated = slug[:max_len]
    boundary = truncated.rfind("-")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip("-")


def derive_slug(text: str, max_len: int) -> str | None:
    """Derive a render-safe slug from producer-influenced free text.

    Pure function — the security boundary where free text becomes a tag segment.
    The canonical ``_ref`` form carries no slug; this helper serves the mutable
    ``_als``/``_chn`` alias slug producers. Steps: lowercase → keep ASCII
    ``[a-z0-9]`` (every other code point, including non-ASCII letters and ``_``,
    becomes a separator) → collapse every run of non-alphanumerics to a single
    ``-`` → strip leading/trailing ``-`` → truncate to ``max_len`` on a hyphen
    boundary (never leaving a trailing hyphen or splitting a token mid-word when
    avoidable). Returns ``None`` if the result is empty (e.g. an all-non-ASCII
    title) — the caller then declines to emit a degenerate alias slug.

    The result, when non-``None``, always matches :data:`_SLUG_RE` and is
    ``<= max_len`` chars, so callers re-validate against the segment regex as a
    belt-and-suspenders fail-closed check (the slug function is the producer, the
    regex is the gate).
    """
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    # Map non-[a-z0-9] (incl. non-ASCII and underscore) to a single separator,
    # collapsing runs; strip leading/trailing separators.
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = _truncate_on_hyphen(slug, max_len)
    return slug or None


class InvalidArtifactKindError(ValueError):
    """Raised when an artifact-kind is not in the controlled vocabulary ([INGEST-029])."""

    def __init__(self, kind: str) -> None:
        allowed = ", ".join(sorted(ARTIFACT_KINDS))
        super().__init__(f"invalid artifact-kind {kind!r}; allowed canonical values: {allowed}")
        self.kind = kind


class InvalidIdError(ValueError):
    """Raised when a logical-hierarchy segment (org/tenant/project-id) is malformed."""

    def __init__(self, field: str, value: str) -> None:
        super().__init__(
            f"invalid {field} {value!r}: must be lowercase alphanumeric with hyphens, "
            "1-64 chars, no leading/trailing hyphen"
        )
        self.field = field
        self.value = value


class InvalidTagError(ValueError):
    """Raised when a tag (or a tag component) is malformed ([INGEST-021]).

    Carries the offending tag string in the ``tag`` ATTRIBUTE so the validation
    layer can route the bundle to the DLQ as a permanent failure rather than
    silently defaulting a category into the cross-check.

    Error-message hygiene: the rendered ``str(e)`` reports the offending
    tag-form CLASS (``form_class``) plus a bounded ``reason``, and does NOT echo
    the full raw ``tag`` content — an unparseable tag may carry attacker- or
    customer-controlled bytes, so dumping it verbatim into a log line is a
    leak/log-injection vector. The raw value remains available structurally via
    the ``tag`` attribute for fail-closed DLQ routing, never via the message.
    """

    def __init__(self, tag: str, reason: str, *, form_class: str = "tag") -> None:
        super().__init__(f"invalid {form_class}: {reason}")
        self.tag = tag
        self.reason = reason
        self.form_class = form_class


def digest_prefix(manifest_digest: str) -> str:
    """Return the canonical ``digest`` tag segment from a Manifest_Digest.

    Strips an optional leading ``sha256:`` algorithm prefix, lowercases, and
    takes the leading :data:`_DIGEST_PREFIX_LEN` characters — the short
    collision-breaker tail of the canonical tag. The result is re-validated
    against :data:`_DIGEST_RE` so a too-short, non-hex, or otherwise
    non-render-safe input fails closed rather than producing a degenerate
    segment. Mint imports this to compose the tag from its addressing anchor.

    Raises :class:`InvalidTagError` (``form_class="digest"``) on a value that
    cannot produce a conforming prefix. Error-message hygiene: the message names the form
    class plus a bounded reason and does NOT echo the raw digest content.
    """
    if not isinstance(manifest_digest, str):
        raise InvalidTagError(
            str(manifest_digest),
            "manifest digest must be a string",
            form_class="digest",
        )
    value = manifest_digest
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    prefix = value.lower()[:_DIGEST_PREFIX_LEN]
    if not _DIGEST_RE.match(prefix):
        raise InvalidTagError(
            manifest_digest,
            f"manifest digest does not yield a {_DIGEST_PREFIX_LEN}-char lowercase-hex prefix",
            form_class="digest",
        )
    return prefix


#: Discriminator for the canonical ``_ref`` reference form
#: ``{artifact-kind}--_ref--{digest}`` (the single tag form).
TAG_FORM_REF: Final[str] = "_ref"

#: Discriminator for the mutable named-alias form
#: ``{artifact-kind}--_als--{name}``. The reserved
#: token is ``_``+3 chars, matching ``_ref`` for namespace uniformity.
TAG_FORM_ALS: Final[str] = "_als"

#: Discriminator for the mutable channel-pointer form
#: ``{artifact-kind}--_chn--{channel}``. Kind-scoped
#: and enumerable as ``{artifact-kind}--_chn--*``.
TAG_FORM_CHN: Final[str] = "_chn"


@dataclass(frozen=True)
class ParsedTag:
    """The decomposed components of an image tag (canonical or alias).

    The canonical tag form is ``{artifact-kind}--_ref--{digest}``;
    the two mutable alias forms are
    ``{artifact-kind}--_als--{name}`` and ``{artifact-kind}--_chn--{channel}``.
    ``form`` is the content-discriminated tag form,
    keyed off the MIDDLE ``--`` segment (NOT the segment count) via the
    reserved-token registry, so a dropped/extra-segment tag is a fail-closed
    error rather than a misread (see :meth:`ArtifactLocator.parse_tag`).

    ``digest`` carries the leading lowercase-hex prefix of the bundle's
    Manifest_Digest (the terminal collision-breaker) for the canonical ``_ref``
    form, and is ``None`` for the alias forms — an alias carries no embedded
    digest (it is resolved to one at the registry by the CLI layer).

    ``name`` carries the render-safe slug payload of the ALIAS forms only: for
    ``_als`` it is the operator-supplied alias label; for ``_chn`` it is the
    channel name (``latest``, ``stable``, …). The two alias forms reuse the
    ``name`` field for their single slug payload rather than introducing a
    parallel field. The canonical ``_ref`` form carries NO slug — it sets
    ``name=None`` (the canonical form carries no name segment).
    ``artifact_kind`` is the leading segment.
    """

    artifact_kind: str
    name: str | None = None
    digest: str | None = None
    form: str = TAG_FORM_REF


@dataclass(frozen=True)
class ArtifactLocation:
    """The concrete registry coordinates for an Artifact_Bundle."""

    repo_name: str
    tag: str


# -- reserved-token registry ------------------------------------
#
# The reserved namespace is the ``_``-prefixed middle-segment space. Each
# reserved token binds to a :class:`ReservedForm` describing how to parse and
# build that form. ``parse_tag`` looks the MIDDLE segment up here (content-
# discrimination); ``build_ref_tag``/minting emit through the matching
# entry's ``build_handler``. This registry is the SINGLE extension point: adding
# a reserved form is one entry + its two handlers, with no edits to the
# parse/build control flow. The fail-closed rule (a ``_``-prefixed but
# unregistered middle segment raises :class:`InvalidTagError`) is one check in
# ``parse_tag`` against this mapping's key set.


@dataclass(frozen=True)
class ReservedForm:
    """A reserved tag-form binding: parse + build handlers, arity, scope rule.

    ``arity`` is the total number of ``--``-separated segments the form carries
    (including the leading ``{artifact-kind}`` and the reserved token itself).
    ``scope_rule`` is a short human-readable note on the form's namespacing.
    The ``build_handler`` takes the keyword identity components and returns the
    assembled tag string.
    """

    token: str
    arity: int
    scope_rule: str
    parse_handler: Callable[[str, list[str]], ParsedTag]
    build_handler: Callable[..., str]


def _validate_leading_kind(tag: str, kind: str, *, form_class: str) -> None:
    """Fail-closed check that a tag's leading segment resolves to an artifact-kind.

    Shared by every parse path (canonical and reserved forms): a leading segment
    that does not resolve via :func:`normalize_artifact_kind` is a malformed tag.
    The message names the failure CLASS, not the raw segment content; the
    supplied spelling is preserved verbatim by the caller.
    """
    try:
        normalize_artifact_kind(kind)
    except InvalidArtifactKindError as e:
        raise InvalidTagError(
            tag,
            "leading segment is not a known artifact-kind",
            form_class=form_class,
        ) from e


def _parse_ref(tag: str, parts: list[str]) -> ParsedTag:
    """Parse the canonical form ``{artifact-kind}--_ref--{digest}``.

    Arity (3) is checked by the caller against the registry entry; here we
    validate the leading artifact-kind and the fixed-length lowercase-hex
    ``digest``, then assemble the :class:`ParsedTag` with ``name=None`` (the
    canonical form carries no slug). Every
    component is re-validated against its ``\\A…\\Z``-anchored grammar so a
    non-render-safe segment fails closed (the message names the form class,
    never the raw segment).
    """
    kind, _token, digest = parts
    _validate_leading_kind(tag, kind, form_class=f"reserved-{TAG_FORM_REF}")
    _validate_tag_digest(tag, digest)
    return ParsedTag(
        artifact_kind=kind,
        name=None,
        digest=digest,
        form=TAG_FORM_REF,
    )


def _build_ref(artifact_kind: str, digest: str) -> str:
    """Build the canonical form ``{artifact-kind}--_ref--{digest}`` (arity 3).

    Validates each segment (kind via the resolver, digest against the
    fixed-length lowercase-hex grammar) and assembles the 3-segment tag. The tag is a
    fixed-shape ``{kind}--_ref--{12-hex}``: the longest kind (29) + ``_ref`` (4)
    + the 12-char digest + 2 separators (4) = 49 chars, well under the ECR
    128-char limit, so no length backstop or truncation is needed.
    """
    _validate_kind(artifact_kind)
    _validate_build_digest(digest)
    return f"{artifact_kind}{_TAG_SEP}{TAG_FORM_REF}{_TAG_SEP}{digest}"


# -- alias forms -------------------------------------
#
# Two MUTABLE alias forms, each a reserved-MIDDLE-token, kind-leading entry of
# arity 3 — structurally a coordinate with the terminal ``digest`` segment
# replaced by an operator-supplied slug. They slot into the existing
# :meth:`_parse_reserved_form` dispatch with ZERO control-flow change: the
# dispatch looks the middle segment up here, checks ``arity``, and fail-closes
# on a reserved-but-shape-mismatched token. An alias carries NO embedded digest;
# its slug payload (the named-alias label or the channel name) lives in the
# :class:`ParsedTag` ``name`` field, with ``digest=None``.


def _validate_alias_slug(tag: str, value: str, *, form_class: str) -> None:
    """Parse-side validator for an alias slug payload (name or channel).

    Gates on the SAME render-safe slug grammar the canonical ``name`` segment
    uses (:data:`_SLUG_RE` + :data:`NAME_SLUG_MAX_LEN`): lowercase alphanumeric
    with single internal hyphens, no ``_``, no ``--``, bounded length. The
    ``--``/``_`` exclusion is what keeps the alias arity fixed at 3 and the form
    disjoint from a forged reserved token. Error-message hygiene: the message names the
    form CLASS, never the raw segment content; the raw ``tag`` rides on the
    :class:`InvalidTagError` attribute for fail-closed routing.
    """
    if len(value) > NAME_SLUG_MAX_LEN or not _SLUG_RE.match(value):
        raise InvalidTagError(tag, "alias slug segment is malformed", form_class=form_class)


def _validate_build_alias_slug(value: str, *, form_class: str) -> None:
    """Build-side validator for an alias slug payload (name or channel).

    Gates on the render-safe slug grammar (:data:`_SLUG_RE`) and the length
    bound (:data:`NAME_SLUG_MAX_LEN`), raising with a bounded reason (no raw
    segment echo).
    """
    if not isinstance(value, str) or len(value) > NAME_SLUG_MAX_LEN or not _SLUG_RE.match(value):
        raise InvalidTagError(
            str(value),
            "alias slug must be a render-safe slug (lowercase alphanumeric, single "
            f"internal hyphens, 1-{NAME_SLUG_MAX_LEN} chars)",
            form_class=form_class,
        )


def _parse_als(tag: str, parts: list[str]) -> ParsedTag:
    """Parse the named-alias form ``{artifact-kind}--_als--{name}`` (arity 3).

    Arity is checked by the caller against the registry entry; here we validate
    the leading artifact-kind and the ``name`` slug, then assemble a
    :class:`ParsedTag` with the slug in ``name``, ``digest=None`` (an alias
    carries no embedded digest), and ``form=_als``. The message names the
    form class, never the raw segment.
    """
    kind, _token, name = parts
    _validate_leading_kind(tag, kind, form_class=f"reserved-{TAG_FORM_ALS}")
    _validate_alias_slug(tag, name, form_class=f"reserved-{TAG_FORM_ALS}")
    return ParsedTag(artifact_kind=kind, name=name, digest=None, form=TAG_FORM_ALS)


def _parse_chn(tag: str, parts: list[str]) -> ParsedTag:
    """Parse the channel-pointer form ``{artifact-kind}--_chn--{channel}`` (arity 3).

    As :func:`_parse_als`, but the slug payload is the channel name; it is stored
    in the ``name`` field of the returned :class:`ParsedTag` (the single slug
    payload field), with ``digest=None`` and ``form=_chn``.
    """
    kind, _token, channel = parts
    _validate_leading_kind(tag, kind, form_class=f"reserved-{TAG_FORM_CHN}")
    _validate_alias_slug(tag, channel, form_class=f"reserved-{TAG_FORM_CHN}")
    return ParsedTag(artifact_kind=kind, name=channel, digest=None, form=TAG_FORM_CHN)


def _build_als(artifact_kind: str, name: str) -> str:
    """Build the named-alias form ``{artifact-kind}--_als--{name}`` (arity 3).

    Validates the kind via the resolver and the ``name`` against the render-safe
    slug grammar + length bound, then assembles the 3-segment tag. The slug
    grammar's ``--`` exclusion keeps the arity fixed, so no length backstop is
    needed (the kind + token + bounded slug + 2 separators cannot exceed the ECR
    128-char limit).
    """
    _validate_kind(artifact_kind)
    _validate_build_alias_slug(name, form_class=f"reserved-{TAG_FORM_ALS}")
    return f"{artifact_kind}{_TAG_SEP}{TAG_FORM_ALS}{_TAG_SEP}{name}"


def _build_chn(artifact_kind: str, name: str) -> str:
    """Build the channel-pointer form ``{artifact-kind}--_chn--{channel}`` (arity 3).

    As :func:`_build_als`, for the channel form. The ``name`` parameter carries
    the channel slug (the registry dispatch keys every build handler on the same
    ``name=`` slug payload keyword, uniform with ``_ref``/``_als``).
    """
    _validate_kind(artifact_kind)
    _validate_build_alias_slug(name, form_class=f"reserved-{TAG_FORM_CHN}")
    return f"{artifact_kind}{_TAG_SEP}{TAG_FORM_CHN}{_TAG_SEP}{name}"


_RESERVED_TOKENS: Final[Mapping[str, ReservedForm]] = {
    TAG_FORM_REF: ReservedForm(
        token=TAG_FORM_REF,
        arity=3,
        scope_rule="artifact-kind-scoped reference: {artifact-kind}--_ref--{digest}",
        parse_handler=_parse_ref,
        build_handler=_build_ref,
    ),
    TAG_FORM_ALS: ReservedForm(
        token=TAG_FORM_ALS,
        arity=3,
        scope_rule="artifact-kind-scoped mutable named alias: {artifact-kind}--_als--{name}",
        parse_handler=_parse_als,
        build_handler=_build_als,
    ),
    TAG_FORM_CHN: ReservedForm(
        token=TAG_FORM_CHN,
        arity=3,
        scope_rule="artifact-kind-scoped mutable channel pointer: {artifact-kind}--_chn--{channel}",
        parse_handler=_parse_chn,
        build_handler=_build_chn,
    ),
}

#: Pure-grammar classifier set: the reserved tokens that name a MUTABLE alias
#: form (as opposed to the immutable canonical ``_ref``). See :func:`is_alias_tag`.
ALIAS_FORMS: Final[frozenset[str]] = frozenset({TAG_FORM_ALS, TAG_FORM_CHN})


def is_alias_tag(parsed: ParsedTag) -> bool:
    """Return ``True`` iff ``parsed`` is one of the mutable alias forms.

    PURE grammar-level classifier — no I/O. It only inspects the already-parsed
    ``form`` against :data:`ALIAS_FORMS`. Alias RESOLUTION (mapping an alias to
    its underlying digest) and the filed-digest precondition are I/O concerns
    that live in the CLI layer, not here.
    """
    return parsed.form in ALIAS_FORMS


def alias_tag_of(tag: object) -> str | None:
    """Return ``tag`` iff it is a well-formed mutable alias tag, else ``None``.

    The SINGLE string-level alias classifier: every read
    path (``pull`` / ``verify`` / ``query`` / TUI) routes through this one grammar
    authority so they classify identically. PURE — no I/O: a non-string, an empty
    value, or any tag the reserved-token parser rejects (a canonical ``_ref`` tag,
    a malformed tag) FAILS CLOSED to ``None``; only a tag that parses to one of the
    mutable alias forms (``_als`` / ``_chn``) returns itself. An alias is only ever
    a mutable LABEL over the immutable digest — resolving it to that digest is an
    I/O concern the CLI layer owns, not this grammar surface. The tag is
    producer-influenceable, so terminal-write callers MUST still route the returned
    value through their own render-safety choke point.
    """
    if not isinstance(tag, str) or not tag:
        return None
    try:
        parsed = _ALIAS_CLASSIFY_LOCATOR.parse_tag(tag)
    except InvalidTagError:
        return None
    return tag if is_alias_tag(parsed) else None


# The reserved-token shape: ``_`` + exactly three lowercase letters (``_ref``,
# ``_als``, ``_chn`` — namespace uniformity). The leading ``_`` is the
# squatting carve-out (no artifact-kind can begin with ``_`` per ``_ID_RE``).
_RESERVED_TOKEN_RE = re.compile(r"\A_[a-z]{3}\Z")


def _assert_reserved_token_registry(registry: Mapping[str, ReservedForm]) -> None:
    """Fail-closed consistency check over the reserved-token registry.

    Asserts every entry's map key equals its ``token``, every token matches the
    reserved ``_``+3-letter shape, and ``ALIAS_FORMS`` is a subset of the
    registered tokens (no alias classifier names an unregistered form). Run at
    module import AND as a unit test so a malformed or squatting registry
    addition fails the import, not production. The Python ``dict`` literal
    already dedups keys, so token uniqueness is structural. Raises
    :class:`ValueError` naming the offending token.
    """
    for key, form in registry.items():
        if key != form.token:
            raise ValueError(f"reserved-token registry key {key!r} != form.token {form.token!r}")
        if not _RESERVED_TOKEN_RE.match(form.token):
            raise ValueError(
                f"reserved token {form.token!r} is not '_'+3 lowercase letters (namespace shape)"
            )
    unregistered = ALIAS_FORMS - registry.keys()
    if unregistered:
        raise ValueError(
            f"ALIAS_FORMS names unregistered reserved token(s): {sorted(unregistered)}"
        )


def _validate_id(field: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidIdError(field, value)


def _validate_qualifier(value: str) -> None:
    if not isinstance(value, str) or not _QUALIFIER_RE.match(value):
        raise InvalidIdError("deployment-qualifier", value)


def _validate_kind(value: str) -> None:
    # Fail-closed validation only: route through the single resolver so aliases
    # are accepted (normalize-on-input), but DO NOT substitute the canonical —
    # callers (repo_name / build_ref_tag) preserve the producer-supplied surface
    # spelling verbatim in the tag/path.
    normalize_artifact_kind(value)


# -- canonical-form digest validators ----------------------------------------
#
# The canonical ``_ref`` form carries a single producer-influenced segment, the
# ``digest`` (no ``name`` segment).
# Two flavors: a parse-side validator that carries the raw ``tag`` for
# fail-closed DLQ routing (and reports the form CLASS, never the raw segment —
# error-message hygiene), and a build-side validator that raises with a bounded reason.
# Both gate on the same ``\A…\Z``-anchored grammar.


def _validate_tag_digest(tag: str, value: str) -> None:
    if not _DIGEST_RE.match(value):
        raise InvalidTagError(
            tag,
            "digest segment is not a fixed-length lowercase-hex prefix",
            form_class=f"reserved-{TAG_FORM_REF}",
        )


def _validate_build_digest(value: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise InvalidTagError(
            str(value),
            f"digest must be a {_DIGEST_PREFIX_LEN}-char lowercase-hex prefix",
            form_class=f"reserved-{TAG_FORM_REF}",
        )


class ArtifactLocator:
    """Maps artifact coordinates to an ECR repository name + image tag.

    The single layout authority: all repo-name and tag
    construction/parsing for a given deployment funnels through one instance.
    There is exactly one repository path shape — the uniform three-segment
    logical hierarchy ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}``
    — so the locator carries no layout parameter and nothing branches per
    segment.
    """

    def repo_name(
        self,
        *,
        deployment_qualifier: str,
        org_id: str,
        tenant_id: str,
        project_id: str,
    ) -> str:
        """Build the ECR repository name ([INGEST-018]).

        Constructs the single uniform three-segment logical hierarchy
        ``artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}`` — exactly one
        path shape, every segment mandatory, nothing branching per segment.
        The artifact-kind is NOT a path segment: a single
        repository holds every artifact-kind for the project, the kind being
        carried in the image tag (see :meth:`build_ref_tag`). The producing
        identity (the AWS account, other cloud, or on-prem host) is likewise
        never a path segment — it is recorded per-artifact.

        Each of ``org_id`` / ``tenant_id`` / ``project_id`` is a stable, opaque,
        immutable, allocate-once identifier validated against the segment
        grammar (:data:`_ID_RE`); an invalid segment raises
        :class:`InvalidIdError`.
        """
        _validate_qualifier(deployment_qualifier)
        _validate_id("org-id", org_id)
        _validate_id("tenant-id", tenant_id)
        _validate_id("project-id", project_id)
        return f"artifact-{deployment_qualifier}/{org_id}/{tenant_id}/{project_id}"

    def build_ref_tag(self, *, artifact_kind: str, digest: str) -> str:
        """Build the canonical tag ``{artifact-kind}--_ref--{digest}`` (arity 3).

        Routes through the reserved-token registry's build handler:
        the registry is the single emit path, so this method carries no
        form-specific assembly of its own. Each segment is validated (kind via
        the resolver, digest against the fixed-length lowercase-hex grammar). The
        canonical address carries NO ``name`` segment — no producer free text and no content-type
        branch, so it cannot misrepresent a multi-file bundle and grants OSCAL no
        naming privilege. Memorable naming is the job of the mutable
        ``{artifact-kind}--_als--{name}`` alias (see :meth:`build_named_alias`).
        The assembled fixed-shape tag (kind + ``_ref`` + 12-hex digest) is always
        well under the ECR 128-char limit, so no length backstop is needed.

        Per artifact-ingestion "ECR Tag Format", the ``artifact_kind`` the caller
        passes here is sourced from the Producer_Attestation_Referrer's ``artifactKind``
        claim (not a content-manifest annotation), and ``digest`` is the
        prefix of the bundle's reproducible content Manifest_Digest — so the same
        content re-pushed derives the identical tag.
        """
        return _RESERVED_TOKENS[TAG_FORM_REF].build_handler(
            artifact_kind=artifact_kind, digest=digest
        )

    def build_named_alias(self, *, artifact_kind: str, name: str) -> str:
        """Build the mutable named-alias tag ``{artifact-kind}--_als--{name}``.

        Routes through the reserved-token registry's build handler,
        exactly like :meth:`build_ref_tag`: the registry is the single emit path.
        The kind is validated via the resolver and the ``name`` against the
        render-safe slug grammar + length bound. The wrapper only constructs the
        well-formed tag string — resolving it to a filed digest and authorizing
        the move are I/O concerns the CLI layer owns, not this
        pure-grammar surface.
        """
        return self._build_alias(TAG_FORM_ALS, artifact_kind=artifact_kind, slug=name)

    def build_channel_pointer(self, *, artifact_kind: str, channel: str) -> str:
        """Build the mutable channel-pointer tag ``{artifact-kind}--_chn--{channel}``.

        As :meth:`build_named_alias`, for the channel form: ``{channel}`` is a
        render-safe slug (``latest``, ``stable``, …), kind-scoped and enumerable
        as ``{artifact-kind}--_chn--*``.
        """
        return self._build_alias(TAG_FORM_CHN, artifact_kind=artifact_kind, slug=channel)

    def _build_alias(self, token: str, *, artifact_kind: str, slug: str) -> str:
        """Shared fail-closed emit path for the alias forms.

        Both alias build entry points funnel here so the registry-handler
        dispatch lives in one place.
        """
        return _RESERVED_TOKENS[token].build_handler(artifact_kind=artifact_kind, name=slug)

    def parse_tag(self, tag: str) -> ParsedTag:
        """Fail-closed inverse of :meth:`build_ref_tag` ([INGEST-021]).

        Splits on ``--`` and CONTENT-DISCRIMINATES on the MIDDLE segment, not on
        the segment count. The canonical form
        ``{artifact-kind}--_ref--{digest}`` and the two mutable alias forms
        ``{artifact-kind}--_als--{name}`` / ``{artifact-kind}--_chn--{channel}``
        all dispatch via the reserved-token registry:

        - If the middle segment is a registered reserved token (``_ref``,
          ``_als``, ``_chn``), the matching parse handler is selected after its
          declared ``arity`` (3 for every form) is checked. A 4-segment
          ``_ref`` tag fails this arity check and raises
          :class:`InvalidTagError` — a HARD CUT, never silently re-read as an
          alias (alias forms are discriminated by the reserved MIDDLE token, not
          by arity).
        - If the middle segment begins with ``_`` but is NOT a registered token
          (e.g. ``_latest``), the tag is in the reserved namespace yet
          unallocated here: it raises :class:`InvalidTagError`, never a silent
          default.
        - Any other middle segment (no leading underscore) is malformed and fails
          closed: every tag form carries a reserved middle token.

        Empty, garbage, wrong-arity, reserved-but-unallocated, or
        out-of-vocabulary input raises :class:`InvalidTagError` — never a silent
        default — so a malformed tag becomes a permanent validation failure.
        """
        if not isinstance(tag, str) or not tag:
            raise InvalidTagError(repr(tag), "tag must be a non-empty string")

        parts = tag.split(_TAG_SEP)
        return self._parse_reserved_form(tag, parts)

    def _parse_reserved_form(self, tag: str, parts: list[str]) -> ParsedTag:
        """Content-discriminated parse of the single image-tag layout.

        The middle segment is the discriminator: a registered reserved token
        (``_ref``, ``_als``, ``_chn``) routes to its handler after its declared
        arity is checked; a ``_``-prefixed unregistered token fails closed;
        anything else is malformed (every tag form carries a reserved middle
        token). The alias forms are registry entries, not a control-flow branch here.
        """
        # The middle segment is the discriminator. It exists once there are at
        # least two segments; a single-segment tag is unambiguously malformed.
        if len(parts) < 2:
            raise InvalidTagError(
                tag, f"expected a '--'-separated tag, got {len(parts)} segment(s)"
            )
        middle = parts[1]

        if not middle.startswith("_"):
            # A non-reserved middle segment is malformed (every tag form carries
            # a reserved '_'-prefixed middle token). Name the class, not the raw
            # token.
            raise InvalidTagError(
                tag,
                "middle segment is not a reserved '_'-prefixed token",
                form_class="non-reserved-middle",
            )

        form = _RESERVED_TOKENS.get(middle)
        if form is None:
            # Reserved namespace, but no handler is registered for this token —
            # fail closed, never a silent default. The message
            # names the class, not the raw token.
            raise InvalidTagError(
                tag,
                "middle segment is in the reserved '_'-prefixed namespace but is "
                "not a registered reserved token",
                form_class="reserved-unallocated",
            )
        if len(parts) != form.arity:
            raise InvalidTagError(
                tag,
                f"reserved '{form.token}' form expects {form.arity} "
                f"'--'-separated segments, got {len(parts)}",
                form_class=f"reserved-{form.token}",
            )
        return form.parse_handler(tag, parts)


# A single parser shared by :func:`alias_tag_of`. ``parse_tag`` carries no
# per-call state, so one immutable instance is reused — read-only module config,
# never per-invocation state. Bound here (after ArtifactLocator is defined); the
# classifier only touches it at call time.
_ALIAS_CLASSIFY_LOCATOR: Final[ArtifactLocator] = ArtifactLocator()


# Import-time global-uniqueness gate: a colliding alias-map addition
# fails the import, not production. Mirrored by a unit test in mise run check.
_assert_artifact_kind_uniqueness(ARTIFACT_KINDS, _ARTIFACT_KIND_ALIASES)

# Import-time reserved-token registry-consistency gate: a squatting or
# malformed reserved-form addition fails the import. Mirrored by a unit test.
_assert_reserved_token_registry(_RESERVED_TOKENS)
