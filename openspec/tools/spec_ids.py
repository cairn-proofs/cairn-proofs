"""Stable-identifier linter for the OpenSpec corpus.

Enforces the "Stable Identifiers for Requirements and Scenarios" toolchain
Requirement: every `### Requirement:` and `#### Scenario:` heading in
`openspec/specs/` carries an explicit, immutable, capability-prefixed identifier
(`PREFIX-NNN` for a requirement, `PREFIX-NNN.MM` for a scenario), and a relocated
or retired identifier is recorded in that spec's `## Relocated and Retired IDs`
table rather than re-used or silently dropped.

The tool is **dependency-free** (stdlib only), **read-only** (it never mutates a
file), and deliberately uses only **linear, backtracking-free** regexes — bounded
digit runs and a single character-class repetition, no nested quantifiers — so a
hostile or malformed spec cannot drive it into catastrophic backtracking
(threat T4 / mitigation M4).

These rules are enforced (1-8 numbered to match the change's original design;
9-10 added since):

1. **Presence and format** — every requirement/scenario heading has a well-formed
   identifier, and a scenario sub-sequence is rendered in canonical 2-digit-minimum
   form (`PREFIX-NNN.MM`): it must equal `f"{int(sub):02d}"`, so both an
   under-padded (`PREFIX-NNN.M`) and an over-padded / surplus-leading-zero
   (`PREFIX-NNN.0M`, `PREFIX-NNN.00M`) rendering is a format violation that names
   the offending heading (a legitimately wider sub like `.100` carries no surplus
   zero and is canonical).
2. **Uniqueness** — no canonical identity is assigned to two headings; two
   renderings of the same identity (`.01` vs `.001`) are a duplicate, not two ids.
3. **No vanish** — an identifier present in the previous committed corpus
   (`git show HEAD:...`) that is absent from the current corpus MUST have a
   `## Relocated and Retired IDs` row; identifiers are never silently dropped.
4. **No reuse** — a newly introduced identifier's sequence number is strictly
   greater than the committed per-prefix high-water mark (retired numbers are
   reserved forever, so an allocate-forward sequence can never collide).
5. **References resolve** — every `[PREFIX-NNN]` / `[PREFIX-NNN.MM]` citation in
   `openspec/specs/` and every `Covers: <ID>` marker in `tests/` resolves: a
   bracketed `[ID]` citation to a live identifier or a ledger entry, and a
   GitHub-anchor link of the form `openspec/specs/<cap>/spec.md#<anchor>` to a
   heading anchor computed from a live heading in that capability.
6. **No positional references** — `Property N`, `Req N.N`, and ordinal phrases
   like "the third criterion" are banned (the rot the scheme replaces).
7. **Prefix matches capability** — each requirement/scenario identifier's PREFIX
   equals its capability's assigned prefix (the immutable capability-dir -> prefix
   map below): a `SIGN-NNN` heading may live only in `signing/spec.md`.
8. **Scenario binds to its requirement** — a scenario's `PREFIX-NNN` matches the
   `PREFIX-NNN` of its enclosing (most-recent-preceding) requirement heading.
9. **No backlog pins in permanent surfaces** — the current-state spec body and
   runtime code (`openspec/specs/`, `libs/`, `lambdas/`, `cdk/lib/`, `cli/`,
   `runbooks/`) carry no pointer to a deferred-work item by filename or slug
   (those rot when an item graduates or against the move to GitHub Issues); state
   the deferred capability by name instead. Point-in-time records (ADRs, audits,
   archived/in-flight changes) are exempt by not being scanned. Disable with
   `--no-backlog-pin-check`.
10. **Glossary links resolve** — every `Governed by` / `Carried as` /
    `Constrained by` link in `openspec/GLOSSARY.md` of the form
    `[<requirement name>](specs/<cap>/spec.md#<anchor>)` resolves: the link text
    equals a live requirement name in <cap> AND the `#<anchor>` fragment equals
    that requirement's GitHub heading anchor. Catches a glossary link left stale
    by a requirement retitle (an OpenSpec `RENAMED`) — both the visible name and
    the jump target — drift `openspec validate --strict` cannot see (the glossary
    is not a validated spec, and a stale link can still carry a live `[ID]`).
    Disable with `--no-glossary-anchor-check`.

The pure functions (`parse_spec`, `validate_corpus`, the `check_*` helpers, and
the `SeedAllocator`) take in-memory data so they are unit-testable without the
filesystem or git; `main()` is the thin CLI wrapper that reads the corpus, the
committed snapshot, the test `Covers:` markers, and the backlog links, then exits
non-zero on any violation.

The migration wave imports `SeedAllocator` to mint identifiers allocate-forward.

Usage:
    python3 openspec/tools/spec_ids.py                       # lint the live corpus
    python3 openspec/tools/spec_ids.py --no-git              # skip committed-snapshot rules
    python3 openspec/tools/spec_ids.py --no-backlog-pin-check  # skip rule 9
    python3 openspec/tools/spec_ids.py --no-glossary-anchor-check  # skip rule 10
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- regexes (all linear / backtracking-free) ------------------------------
#
# Sequence numbers are a bounded digit run (`\d{3,6}`): zero-padded to at least
# 3 per the resolved pad-width decision, capped so a pathological input cannot
# produce an unbounded match. Prefixes are a single character-class repetition
# (`[A-Z][A-Z0-9]*`), never a nested quantifier — no catastrophic backtracking.
#
# Scenario sub-sequences are rendered zero-padded to two digits (`PREFIX-NNN.MM`).
# That 2-digit discipline is a *rendering* rule enforced at exactly ONE boundary —
# the `#### Scenario:` heading where an identifier is *defined*: `parse_spec`
# extracts the identity pad-agnostically (`\d{1,4}`), then flags the heading by
# name (Rule 1) when the captured sub-sequence does not equal its canonical
# 2-digit-minimum rendering `f"{int(sub):02d}"` (under- OR over-padded).
# Everywhere an identifier is *referred to* or its
# *identity* is extracted (citations, `Covers:` markers, ledger cells,
# `parse_identifier`) the match likewise stays pad-width-agnostic: an
# identifier's identity is its `(prefix, number, sub)` integer tuple, independent
# of zero-pad width (design Decision 2, "Option A"). Keeping the reference/identity
# side lenient is what lets a `.1` -> `.01` pad-widen reconcile to the SAME
# identity across the commit boundary (no false vanish/reuse) and lets a
# single-digit citation still resolve against a 2-digit live id — while a
# genuinely dangling single-digit citation is still gathered and surfaced by
# Rule 5 (never silently dropped; design Risk 1).

# A requirement identifier embedded at the START of a heading name: `[SIGN-014]`.
REQ_ID_AT_START = re.compile(r"^\[([A-Z][A-Z0-9]*)-(\d{3,6})\]")
# A scenario identifier at the start of a scenario heading name: `[SIGN-014.03]`.
# Pad-width-agnostic (`\d{1,4}`) so a non-canonically-padded heading still yields
# an identity for the canonical comparison; the canonical 2-digit-minimum
# rendering rule (Rule 1) is enforced separately by `parse_spec` (sub literal vs
# `f"{int(sub):02d}"`).
SCN_ID_AT_START = re.compile(r"^\[([A-Z][A-Z0-9]*)-(\d{3,6})\.(\d{1,4})\]")
# Any bracketed identifier citation found in body prose (requirement or scenario).
# Pad-agnostic (`\d{1,4}`): a single-digit citation is gathered so it can resolve
# against a 2-digit live id (canonical identity) — or surface as dangling (Rule 5).
ID_TOKEN = re.compile(r"\[([A-Z][A-Z0-9]*)-(\d{3,6})(?:\.(\d{1,4}))?\]")
# A bare (unbracketed) identifier, used in `Covers:` markers and ledger cells.
# Pad-agnostic for the same reason as ID_TOKEN.
BARE_ID = re.compile(r"\b([A-Z][A-Z0-9]*)-(\d{3,6})(?:\.(\d{1,4}))?\b")
# Canonical full-string forms, for parsing a known identifier into parts. The
# scenario form is pad-agnostic (`\d{1,4}`): `parse_identifier` is THE identity
# function, and identity is independent of zero-pad width (Option A).
REQ_FULL = re.compile(r"^([A-Z][A-Z0-9]*)-(\d{3,6})$")
SCN_FULL = re.compile(r"^([A-Z][A-Z0-9]*)-(\d{3,6})\.(\d{1,4})$")

# Width the scenario sub-sequence is zero-padded to when rendered / minted.
_SCENARIO_SUB_PAD = 2

_COVERS_MARKER = re.compile(r"Covers:(.*)$")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-+:?$")

# Positional / legacy references the scheme bans (rule 6).
_ORDINALS = "first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth"
POSITIONAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bProperty\s+\d{1,4}\b"),
    re.compile(r"\bReq\s+\d{1,4}\.\d{1,4}\b"),
    re.compile(
        rf"\bthe\s+(?:{_ORDINALS})\s+"
        r"(?:criterion|criteria|scenario|requirement|bullet|item|property)\b",
        re.IGNORECASE,
    ),
)

_VALID_LEDGER_STATUSES = frozenset({"withdrawn", "relocated"})

# Capability-dir -> assigned identifier prefix (rule 7). Immutable: a live
# identifier's prefix MUST equal its capability's prefix, so a cross-capability
# move is a retire-here + remint-there (never a silent prefix change). Keyed on
# the spec file's parent directory name (== SpecDoc.capability).
CAPABILITY_PREFIXES: dict[str, str] = {
    "artifact-ingestion": "INGEST",
    "signing": "SIGN",
    "worm-archive": "WORM",
    "metadata-index": "META",
    "producer-claims": "PCLAIM",
    "identity-registry": "IDREG",
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
}

# A backlog spec link in GitHub-anchor form: `openspec/specs/<cap>/spec.md#<anchor>`.
# Both capture groups are single character-class repetitions (linear, no nested
# quantifier) — backtracking-free even on a hostile backlog document.
BACKLOG_ANCHOR_LINK = re.compile(r"openspec/specs/([a-z0-9-]+)/spec\.md#([\w-]+)")

# Chars GitHub's slugger drops when deriving a heading anchor: everything that is
# not a word-char, whitespace, or hyphen. Linear single-class match.
_ANCHOR_DROP = re.compile(r"[^\w\s-]")

_REQUIREMENT_PREFIX = "### Requirement:"
_SCENARIO_PREFIX = "#### Scenario:"
_LEDGER_SECTION_TITLE = "relocated and retired ids"

# Rule 10 (glossary links): a GLOSSARY.md pointer field, and each markdown link
# inside it to a capability spec — `[<text>](specs/<cap>/spec.md#<anchor>)`, where
# the link text is the requirement name and the fragment is its GitHub heading
# anchor (absent for a bare file link). Linear, backtracking-free patterns.
_GLOSSARY_POINTER_FIELD = re.compile(r"\*\*(?:Governed by|Carried as|Constrained by)\*\*")
_GLOSSARY_SPEC_LINK = re.compile(r"\[([^\]]+)\]\(specs/([a-z0-9-]+)/spec\.md(?:#([\w-]+))?\)")

# Rule 9 — ban rotting deferred-work pointers from the permanent surfaces (the
# current-state spec body and runtime code). Deferred work lives in GitHub
# Issues; a volatile slug pinned into a spec or into source code rots. Point-in-
# time records (ADRs, audits, archived/in-flight changes) legitimately cite such
# an item by slug and are exempt by NOT being scanned — see `_BACKLOG_PIN_ROOTS`.
#
# Detected form (a linear / backtracking-free pattern — a single character-class
# repetition, no nested quantifiers): a kebab slug adjacent to the word
# "backlog" (`backlog `the-slug``, or `the-slug backlog item`) — the captured
# token is post-filtered to a real multi-segment slug (>=2 hyphens) in code,
# keeping the regex linear.
_BACKLOG_SLUG_AFTER = re.compile(r"backlog\s+[`'\"]?([a-z0-9][a-z0-9-]*)")
_BACKLOG_SLUG_BEFORE = re.compile(r"([a-z0-9][a-z0-9-]*)[`'\"]?\s+backlog\b")


def find_backlog_slug_pins(text: str, source: str) -> list[str]:
    """Rule 9: report every rotting backlog-item pointer in `text`.

    Returns one violation per offending line (the first pin found on it), so a
    fixer sees the line and removes the pointer rather than re-pointing it.
    """
    violations: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        pin: str | None = None
        for pat in (_BACKLOG_SLUG_AFTER, _BACKLOG_SLUG_BEFORE):
            for m in pat.finditer(line):
                if m.group(1).count("-") >= 2:  # a real multi-segment slug
                    pin = m.group(0).strip()
                    break
            if pin is not None:
                break
        if pin is not None:
            violations.append(
                f"{source}:{line_no}: rotting backlog-item pointer is banned from the "
                f"permanent surfaces (state the deferred capability by name instead): {pin!r}"
            )
    return violations


# --- data model ------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    """A parsed requirement (level 3) or scenario (level 4) heading."""

    level: int
    raw_name: str
    line_no: int
    spec_id: str | None  # well-formed identifier, or None if the heading lacks one
    # For a scenario, the identifier of the most-recent-preceding requirement
    # heading (its enclosing requirement), or None if it precedes any requirement
    # or that requirement was malformed. Always None on a requirement heading.
    enclosing_req_id: str | None = None


@dataclass(frozen=True)
class LedgerEntry:
    """A row from a `## Relocated and Retired IDs` table."""

    spec_id: str
    status: str  # `withdrawn` | `relocated` (lower-cased as parsed; validated in check_ledger)
    resolves_to: str | None  # destination identifier for a relocation
    line_no: int


@dataclass(frozen=True)
class Reference:
    """An identifier citation found in body text, a test, or a backlog link."""

    spec_id: str
    line_no: int
    source: str


@dataclass(frozen=True)
class BacklogAnchorRef:
    """A GitHub-anchor spec link found in a backlog document.

    `openspec/specs/<capability>/spec.md#<anchor>` — resolved (rule 5) by
    computing the live heading anchors for `capability` and checking `anchor`
    against them.
    """

    capability: str
    anchor: str
    line_no: int
    source: str


@dataclass(frozen=True)
class PositionalRef:
    """A banned positional / legacy reference."""

    text: str
    line_no: int
    source: str


@dataclass(frozen=True)
class GlossaryPointer:
    """A governing-requirement link in `openspec/GLOSSARY.md` (rule 10).

    A markdown link inside a `Governed by` / `Carried as` / `Constrained by`
    field — `[<requirement name>](specs/<cap>/spec.md#<anchor>)`. Resolved by
    checking the link text against that capability's live requirement names AND
    the `#<anchor>` fragment against the requirement's GitHub heading anchor, so a
    retitle that stales either is caught (a bare `[ID]` citation, rule 5, misses
    both). `anchor` is None for a bare file link `[<cap>](specs/<cap>/spec.md)`.
    """

    capability: str
    heading: str
    anchor: str | None
    line_no: int
    source: str


@dataclass
class SpecDoc:
    """The parsed contents of a single spec file (or synthetic fixture)."""

    capability: str
    source: str
    requirements: list[Heading] = field(default_factory=list)
    scenarios: list[Heading] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    positional: list[PositionalRef] = field(default_factory=list)
    malformed_headings: list[Heading] = field(default_factory=list)
    # Scenario headings whose identifier parses (identity preserved) but whose
    # sub-sequence is rendered in non-canonical form — it does not equal its
    # canonical 2-digit-minimum rendering `f"{int(sub):02d}"`, i.e. it is either
    # under-padded (`.1`) or over-padded with a surplus leading zero (`.001`,
    # `.010`). A format violation reported by Rule 1 (presence-and-format).
    underpadded_scenarios: list[Heading] = field(default_factory=list)
    ledger_errors: list[str] = field(default_factory=list)

    def live_ids(self) -> list[str]:
        ids = [h.spec_id for h in self.requirements if h.spec_id]
        ids += [h.spec_id for h in self.scenarios if h.spec_id]
        return ids

    def ledger_ids(self) -> list[str]:
        return [e.spec_id for e in self.ledger]


@dataclass
class Corpus:
    """An aggregate of spec docs — the unit the rule checks operate on."""

    docs: list[SpecDoc] = field(default_factory=list)

    def live_ids(self) -> set[str]:
        return {spec_id for doc in self.docs for spec_id in doc.live_ids()}

    def ledger_ids(self) -> set[str]:
        return {spec_id for doc in self.docs for spec_id in doc.ledger_ids()}

    def all_committed_ids(self) -> set[str]:
        return self.live_ids() | self.ledger_ids()

    def references(self) -> list[Reference]:
        return [ref for doc in self.docs for ref in doc.references]

    def positional(self) -> list[PositionalRef]:
        return [ref for doc in self.docs for ref in doc.positional]


# --- identifier helpers ----------------------------------------------------


def parse_identifier(spec_id: str) -> tuple[str, int, int | None] | None:
    """Parse a canonical identifier into (prefix, number, sub|None), or None.

    `SIGN-014` -> ("SIGN", 14, None); `SIGN-014.03` -> ("SIGN", 14, 3).

    Pad-width-agnostic: a single-digit rendering (`SIGN-014.3`) parses to the
    SAME integer identity `("SIGN", 14, 3)`. Identity is the integer tuple; the
    2-digit zero-pad is a rendering rule enforced elsewhere (Rule 1 / the format
    gate), not part of identity (design Decision 2, "Option A").
    """
    m = SCN_FULL.match(spec_id)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    m = REQ_FULL.match(spec_id)
    if m:
        return m.group(1), int(m.group(2)), None
    return None


def identity_key(spec_id: str) -> tuple[str, int, int | None] | str:
    """Pad-width-independent identity key for membership comparisons.

    `parse_identifier` already yields an integer `(prefix, number, sub)` tuple, so
    `SIGN-009.1` and `SIGN-009.01` map to the SAME key `("SIGN", 9, 1)`. This is
    the canonical-identity seam (design Decision 2 / threat M2): the no-vanish,
    no-reuse, and reference-resolution checks compare these keys rather than the
    literal rendered strings, so a `.1` -> `.01` pad-widen is not seen as a
    vanished-then-reintroduced identifier and a single-digit citation resolves
    against a 2-digit live id. A string that does not parse is returned unchanged
    (Rule 1 reports it as malformed anyway).
    """
    parsed = parse_identifier(spec_id)
    return parsed if parsed is not None else spec_id


def _token_id(match: re.Match[str]) -> str:
    prefix, number, sub = match.group(1), match.group(2), match.group(3)
    return f"{prefix}-{number}" + (f".{sub}" if sub else "")


def requirement_name(heading: Heading) -> str:
    """A requirement heading's human name — its `raw_name` with the `[ID]` stripped."""
    m = REQ_ID_AT_START.match(heading.raw_name)
    return heading.raw_name[m.end() :].strip() if m else heading.raw_name.strip()


def _heading_full_text(heading: Heading) -> str:
    """Reconstruct the rendered heading text GitHub slugs into an anchor.

    GitHub computes the fragment from the whole heading line minus its leading
    `###`/`####` marker — i.e. including the literal `Requirement:`/`Scenario:`
    label and the `[ID]` bracket. `parse_spec` strips that label into `raw_name`,
    so we put it back here.
    """
    label = "Requirement:" if heading.level == 3 else "Scenario:"
    return f"{label} {heading.raw_name}"


def github_anchor(heading_text: str) -> str:
    """Compute the GitHub heading anchor for `heading_text`.

    Algorithm (matching GitHub's slugger for our heading shapes): lowercase, drop
    every char that is not a word-char / whitespace / hyphen, then replace spaces
    with hyphens. Backtracking-free (one linear character-class substitution).
    """
    lowered = heading_text.lower()
    cleaned = _ANCHOR_DROP.sub("", lowered)
    return cleaned.replace(" ", "-")


def corpus_anchors(corpus: Corpus) -> dict[str, set[str]]:
    """Map each capability to the set of GitHub anchors of its live headings."""
    by_cap: dict[str, set[str]] = {}
    for doc in corpus.docs:
        anchors = by_cap.setdefault(doc.capability, set())
        for heading in (*doc.requirements, *doc.scenarios):
            anchors.add(github_anchor(_heading_full_text(heading)))
    return by_cap


# --- parsing ---------------------------------------------------------------


def _scan_positional(text: str, line_no: int, source: str, sink: list[PositionalRef]) -> None:
    for pattern in POSITIONAL_PATTERNS:
        for m in pattern.finditer(text):
            sink.append(PositionalRef(text=m.group(0), line_no=line_no, source=source))


def _scan_heading_extras(
    name: str,
    defining_match: re.Match[str] | None,
    line_no: int,
    source: str,
    doc: SpecDoc,
) -> None:
    """Scan a heading name for positional refs and for citations OTHER than its own ID.

    The leading `[PREFIX-NNN]` is the heading's definition, not a reference; any
    further `[ID]` token in the name (a citation embedded in the title) is.
    """
    _scan_positional(name, line_no, source, doc.positional)
    rest = name[defining_match.end() :] if defining_match else name
    for token in ID_TOKEN.finditer(rest):
        doc.references.append(Reference(spec_id=_token_id(token), line_no=line_no, source=source))


def _normalize_header_cell(cell: str) -> str:
    return cell.strip().lower().replace("-", " ")


def _ledger_columns(cells: list[str]) -> dict[str, int] | None:
    """Map id/status/resolves logical columns to their indices, or None if incomplete."""
    cols: dict[str, int] = {}
    for idx, raw in enumerate(cells):
        name = _normalize_header_cell(raw)
        words = name.split()
        if "id" not in cols and ("id" in words or "identifier" in words):
            cols["id"] = idx
        if "status" not in cols and "status" in name:
            cols["status"] = idx
        if "resolves" not in cols and any(
            k in name for k in ("resolve", "destination", "target", "new")
        ):
            cols["resolves"] = idx
    if "id" not in cols or "status" not in cols:
        return None
    return cols


def _split_table_row(stripped: str) -> list[str]:
    body = stripped.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    nonempty = [c for c in cells if c]
    return bool(nonempty) and all(_TABLE_SEPARATOR_CELL.match(c) for c in nonempty)


def parse_spec(text: str, capability: str, source: str | None = None) -> SpecDoc:
    """Parse one spec document into a SpecDoc (pure; no filesystem access)."""
    source = source or capability
    doc = SpecDoc(capability=capability, source=source)

    in_ledger = False
    ledger_cols: dict[str, int] | None = None
    ledger_header_seen = False
    current_req_id: str | None = None  # most-recent-preceding requirement (rule 8)

    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()

        # A level-2 heading toggles the ledger section on/off.
        if stripped.startswith("## ") and not stripped.startswith("### "):
            title = stripped[3:].strip().lower()
            in_ledger = title == _LEDGER_SECTION_TITLE
            ledger_cols = None
            ledger_header_seen = False
            continue

        if stripped.startswith(_REQUIREMENT_PREFIX):
            name = stripped[len(_REQUIREMENT_PREFIX) :].strip()
            m = REQ_ID_AT_START.match(name)
            spec_id = f"{m.group(1)}-{m.group(2)}" if m else None
            current_req_id = spec_id
            heading = Heading(level=3, raw_name=name, line_no=i, spec_id=spec_id)
            doc.requirements.append(heading)
            if spec_id is None:
                doc.malformed_headings.append(heading)
            _scan_heading_extras(name, m, i, source, doc)
            continue

        if stripped.startswith(_SCENARIO_PREFIX):
            name = stripped[len(_SCENARIO_PREFIX) :].strip()
            # Extract identity pad-agnostically (so a non-canonically-padded
            # heading still has an identity for the canonical comparison), then
            # enforce the canonical 2-digit-minimum rendering separately below.
            m = SCN_ID_AT_START.match(name)
            spec_id = f"{m.group(1)}-{m.group(2)}.{m.group(3)}" if m else None
            heading = Heading(
                level=4,
                raw_name=name,
                line_no=i,
                spec_id=spec_id,
                enclosing_req_id=current_req_id,
            )
            doc.scenarios.append(heading)
            if spec_id is None:
                doc.malformed_headings.append(heading)
            elif m.group(3) != f"{int(m.group(3)):0{_SCENARIO_SUB_PAD}d}":
                # Parses leniently (identity preserved) but the sub-sequence is
                # not in canonical 2-digit-minimum form. The canonical rendering
                # is `f"{int(sub):02d}"`, so this rejects BOTH under-padding
                # (`.1` != `.01`) AND over-padding with a surplus leading zero
                # (`.001` != `.01`, `.010` != `.10`) while still allowing a
                # legitimately wider sub that carries no surplus zero (scenario
                # 100 renders `.100` == f"{100:02d}"). A fixed `len == 2` test
                # would wrongly reject `.100`; the canonical-equality test does not.
                doc.underpadded_scenarios.append(heading)
            _scan_heading_extras(name, m, i, source, doc)
            continue

        if in_ledger and stripped.startswith("|"):
            cells = _split_table_row(stripped)
            if _is_separator_row(cells):
                continue
            if not ledger_header_seen:
                ledger_header_seen = True
                ledger_cols = _ledger_columns(cells)
                if ledger_cols is None:
                    doc.ledger_errors.append(
                        f"{source}:{i}: ledger table missing required 'ID' and/or "
                        f"'Status' column(s)"
                    )
                continue
            if ledger_cols is None:
                continue  # header was malformed; skip data rows (error already recorded)
            _parse_ledger_data_row(cells, ledger_cols, i, source, doc)
            continue

        # Generic body line: collect references and scan for positional refs.
        for m in ID_TOKEN.finditer(line):
            doc.references.append(Reference(spec_id=_token_id(m), line_no=i, source=source))
        _scan_positional(line, i, source, doc.positional)

    return doc


def _parse_ledger_data_row(
    cells: list[str],
    cols: dict[str, int],
    line_no: int,
    source: str,
    doc: SpecDoc,
) -> None:
    def cell(key: str) -> str:
        idx = cols.get(key)
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx]

    id_match = BARE_ID.search(cell("id"))
    if id_match is None:
        doc.ledger_errors.append(
            f"{source}:{line_no}: ledger row has no well-formed identifier in its ID column"
        )
        return
    spec_id = id_match.group(0)
    status = cell("status").strip().lower()
    resolves_match = BARE_ID.search(cell("resolves"))
    resolves_to = resolves_match.group(0) if resolves_match else None
    doc.ledger.append(
        LedgerEntry(spec_id=spec_id, status=status, resolves_to=resolves_to, line_no=line_no)
    )


# --- reference gathering for non-spec sources ------------------------------


def covers_references(text: str, source: str) -> list[Reference]:
    """Extract `Covers: <ID>[, <ID> ...]` markers (bracketed or bare) from a test file."""
    refs: list[Reference] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _COVERS_MARKER.search(line)
        if not m:
            continue
        for token in BARE_ID.finditer(m.group(1)):
            refs.append(Reference(spec_id=token.group(0), line_no=i, source=source))
    return refs


def backlog_references(text: str, source: str) -> list[Reference]:
    """Extract bracketed `[PREFIX-NNN]` spec citations from a backlog document."""
    refs: list[Reference] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for m in ID_TOKEN.finditer(line):
            refs.append(Reference(spec_id=_token_id(m), line_no=i, source=source))
    return refs


def backlog_anchor_references(text: str, source: str) -> list[BacklogAnchorRef]:
    """Extract `openspec/specs/<cap>/spec.md#<anchor>` GitHub-anchor links."""
    refs: list[BacklogAnchorRef] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for m in BACKLOG_ANCHOR_LINK.finditer(line):
            refs.append(
                BacklogAnchorRef(capability=m.group(1), anchor=m.group(2), line_no=i, source=source)
            )
    return refs


def parse_glossary_pointers(text: str, source: str) -> list[GlossaryPointer]:
    """Extract every governing-requirement pointer from a GLOSSARY.md body (pure).

    A pointer is a markdown link to a capability spec —
    `[<requirement name>](specs/<cap>/spec.md#<anchor>)` — inside a `Governed by`
    / `Carried as` / `Constrained by` field. A bare file link
    `[<cap>](specs/<cap>/spec.md)` (no fragment, e.g. a `Constrained by`
    capability reference) yields a pointer with `anchor` None. Links outside a
    pointer field, and non-spec links (e.g. an ADR), yield nothing.
    """
    pointers: list[GlossaryPointer] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not _GLOSSARY_POINTER_FIELD.search(line):
            continue
        for m in _GLOSSARY_SPEC_LINK.finditer(line):
            pointers.append(
                GlossaryPointer(
                    capability=m.group(2),
                    heading=m.group(1),
                    anchor=m.group(3),
                    line_no=i,
                    source=source,
                )
            )
    return pointers


# --- rule checks (pure) ----------------------------------------------------


def check_presence(corpus: Corpus) -> list[str]:
    """Rule 1: every requirement/scenario heading carries a well-formed identifier
    and a scenario sub-sequence is zero-padded to two digits (`PREFIX-NNN.MM`)."""
    violations: list[str] = []
    for doc in corpus.docs:
        for heading in doc.malformed_headings:
            kind = "requirement" if heading.level == 3 else "scenario"
            violations.append(
                f"{doc.source}:{heading.line_no}: {kind} heading lacks a well-formed "
                f"identifier: {heading.raw_name!r}"
            )
        for heading in doc.underpadded_scenarios:
            violations.append(
                f"{doc.source}:{heading.line_no}: scenario identifier sub-sequence is not "
                f"zero-padded to {_SCENARIO_SUB_PAD} digits in canonical form (use "
                f"[PREFIX-NNN.MM] with no surplus leading zero): {heading.raw_name!r}"
            )
    return violations


def check_uniqueness(corpus: Corpus) -> list[str]:
    """Rule 2: no two headings share a canonical identity.

    Keyed on the pad-width-independent `identity_key` (design Decision 2 /
    "Option A"), so two renderings of the SAME identity collide even though their
    literal strings differ — `SIGN-009.01` vs `SIGN-009.001`, or `SIGN-009.1` vs
    `SIGN-009.01`, are a duplicate, not two distinct ids. The format gate (Rule 1)
    independently flags any non-canonical rendering, but uniqueness must not be
    fooled into a false-green by a pad-rendering difference over one identity.
    """
    seen: dict[tuple[str, int, int | None] | str, tuple[str, str]] = {}
    violations: list[str] = []
    for doc in corpus.docs:
        for heading in (*doc.requirements, *doc.scenarios):
            if heading.spec_id is None:
                continue
            origin = f"{doc.source}:{heading.line_no}"
            key = identity_key(heading.spec_id)
            if key in seen:
                prev_id, prev_origin = seen[key]
                if prev_id == heading.spec_id:
                    detail = f"duplicate identifier {heading.spec_id}"
                else:
                    detail = (
                        f"duplicate identifier {heading.spec_id} (same canonical identity "
                        f"as {prev_id})"
                    )
                violations.append(f"{detail}: defined at {prev_origin} and {origin}")
            else:
                seen[key] = (heading.spec_id, origin)
    return violations


def check_no_vanish(committed: Corpus, current: Corpus) -> list[str]:
    """Rule 3: a committed identifier absent from the current corpus needs a ledger row.

    Membership is compared on the canonical identity key (pad-width-independent),
    so re-minting `.1` -> `.01` is not seen as a vanished identifier.
    """
    current_keys = {identity_key(s) for s in current.live_ids()}
    current_ledger_keys = {identity_key(s) for s in current.ledger_ids()}
    violations: list[str] = []
    for spec_id in sorted(committed.live_ids()):
        key = identity_key(spec_id)
        if key in current_keys or key in current_ledger_keys:
            continue
        violations.append(
            f"identifier {spec_id} was removed from the corpus without a "
            f"'Relocated and Retired IDs' ledger entry"
        )
    return violations


def _high_water_marks(ids: set[str]) -> tuple[dict[str, int], dict[tuple[str, int], int]]:
    req_hwm: dict[str, int] = {}
    scn_hwm: dict[tuple[str, int], int] = {}
    for spec_id in ids:
        parsed = parse_identifier(spec_id)
        if parsed is None:
            continue
        prefix, number, sub = parsed
        req_hwm[prefix] = max(req_hwm.get(prefix, 0), number)
        if sub is not None:
            key = (prefix, number)
            scn_hwm[key] = max(scn_hwm.get(key, 0), sub)
    return req_hwm, scn_hwm


def check_no_reuse(committed: Corpus, current: Corpus) -> list[str]:
    """Rule 4: a newly introduced identifier exceeds the committed per-prefix high-water mark.

    "Newly introduced" is judged on the canonical identity key, so a `.1` -> `.01`
    pad-widen of an already-committed identifier is not treated as a new id.
    """
    committed_keys = {identity_key(s) for s in committed.live_ids()}
    req_hwm, scn_hwm = _high_water_marks(committed.all_committed_ids())
    violations: list[str] = []
    for spec_id in sorted(current.live_ids()):
        if identity_key(spec_id) in committed_keys:
            continue  # already live in a prior commit (pad-width-agnostic) — not new
        parsed = parse_identifier(spec_id)
        if parsed is None:
            continue  # malformed; rule 1 reports it
        prefix, number, sub = parsed
        if sub is None:
            cap = req_hwm.get(prefix, 0)
            if number <= cap:
                violations.append(
                    f"identifier {spec_id} reuses or backfills a sequence number at or "
                    f"below the committed high-water mark {prefix}-{cap:03d}"
                )
        else:
            cap = scn_hwm.get((prefix, number))
            if cap is not None and sub <= cap:
                violations.append(
                    f"scenario identifier {spec_id} reuses a sub-number at or below the "
                    f"committed high-water mark {prefix}-{number:03d}.{cap:02d}"
                )
    return violations


def check_references(corpus: Corpus, extra_references: list[Reference] | None = None) -> list[str]:
    """Rule 5: every citation resolves to a live identifier or a ledger entry.

    Resolution compares the canonical identity key (pad-width-independent), so a
    single-digit citation (`[SIGN-009.1]`, or a `Covers:`/ledger cell rendering
    it that way) resolves against a 2-digit live id (`SIGN-009.01`) — while a
    citation whose identity is not live anywhere still surfaces as dangling.
    """
    universe = {identity_key(s) for s in (corpus.live_ids() | corpus.ledger_ids())}
    violations: list[str] = []
    references = list(corpus.references())
    if extra_references:
        references.extend(extra_references)
    for ref in references:
        if identity_key(ref.spec_id) not in universe:
            violations.append(
                f"{ref.source}:{ref.line_no}: reference {ref.spec_id} does not resolve to a "
                f"live identifier or a ledger entry"
            )
    return violations


def check_backlog_anchor_refs(
    corpus: Corpus, anchor_refs: list[BacklogAnchorRef] | None
) -> list[str]:
    """Rule 5 (anchor form): every backlog GitHub-anchor link resolves to a live heading."""
    if not anchor_refs:
        return []
    by_cap = corpus_anchors(corpus)
    violations: list[str] = []
    for ref in anchor_refs:
        anchors = by_cap.get(ref.capability)
        if anchors is None:
            violations.append(
                f"{ref.source}:{ref.line_no}: backlog link targets unknown capability "
                f"{ref.capability!r}"
            )
        elif ref.anchor not in anchors:
            violations.append(
                f"{ref.source}:{ref.line_no}: backlog link fragment #{ref.anchor} does not "
                f"resolve to a live heading anchor in {ref.capability}/spec.md"
            )
    return violations


def check_prefix_matches_capability(corpus: Corpus) -> list[str]:
    """Rule 7: a heading's identifier prefix equals its capability's assigned prefix."""
    violations: list[str] = []
    for doc in corpus.docs:
        expected = CAPABILITY_PREFIXES.get(doc.capability)
        if expected is None:
            continue  # not a canonical capability dir (e.g. a synthetic fixture)
        for heading in (*doc.requirements, *doc.scenarios):
            if heading.spec_id is None:
                continue  # rule 1 reports a missing identifier
            parsed = parse_identifier(heading.spec_id)
            if parsed is None:
                continue
            prefix = parsed[0]
            if prefix != expected:
                kind = "requirement" if heading.level == 3 else "scenario"
                violations.append(
                    f"{doc.source}:{heading.line_no}: {kind} identifier {heading.spec_id} has "
                    f"prefix {prefix!r}, but capability {doc.capability!r} requires prefix "
                    f"{expected!r}"
                )
    return violations


def check_scenario_under_requirement(corpus: Corpus) -> list[str]:
    """Rule 8: a scenario's `PREFIX-NNN` matches its enclosing requirement's `PREFIX-NNN`."""
    violations: list[str] = []
    for doc in corpus.docs:
        for scn in doc.scenarios:
            if scn.spec_id is None:
                continue  # rule 1 reports a missing identifier
            parsed = parse_identifier(scn.spec_id)
            if parsed is None:
                continue
            if scn.enclosing_req_id is None:
                continue  # scenario precedes any requirement, or its requirement is malformed
            parent = parse_identifier(scn.enclosing_req_id)
            if parent is None:
                continue
            if (parsed[0], parsed[1]) != (parent[0], parent[1]):
                violations.append(
                    f"{doc.source}:{scn.line_no}: scenario {scn.spec_id} sits under requirement "
                    f"{scn.enclosing_req_id}, but its requirement-level identifier "
                    f"{parsed[0]}-{parsed[1]:03d} does not match"
                )
    return violations


def check_no_positional(corpus: Corpus) -> list[str]:
    """Rule 6: no `Property N`, `Req N.N`, or ordinal positional references."""
    return [
        f"{ref.source}:{ref.line_no}: positional/legacy reference is banned: {ref.text!r}"
        for ref in corpus.positional()
    ]


def check_ledger(corpus: Corpus) -> list[str]:
    """Validate `## Relocated and Retired IDs` rows: well-formed, unique, resolvable."""
    live = {identity_key(s) for s in corpus.live_ids()}
    violations: list[str] = []
    seen: dict[str, str] = {}
    for doc in corpus.docs:
        violations.extend(doc.ledger_errors)
        for entry in doc.ledger:
            origin = f"{doc.source}:{entry.line_no}"
            if entry.spec_id in seen:
                violations.append(
                    f"duplicate ledger entry for {entry.spec_id}: at {seen[entry.spec_id]} "
                    f"and {origin}"
                )
            else:
                seen[entry.spec_id] = origin
            if entry.status not in _VALID_LEDGER_STATUSES:
                violations.append(
                    f"{origin}: ledger entry for {entry.spec_id} has invalid status "
                    f"{entry.status!r} (expected 'withdrawn' or 'relocated')"
                )
            if entry.status == "relocated":
                if entry.resolves_to is None:
                    violations.append(
                        f"{origin}: relocated entry for {entry.spec_id} has no destination "
                        f"identifier"
                    )
                elif identity_key(entry.resolves_to) not in live:
                    violations.append(
                        f"{origin}: relocated entry for {entry.spec_id} points at "
                        f"{entry.resolves_to}, which is not a live identifier"
                    )
    return violations


def check_glossary_anchors(corpus: Corpus, pointers: list[GlossaryPointer]) -> list[str]:
    """Rule 10 — every GLOSSARY.md governing-requirement link resolves verbatim.

    For a deep link `[<name>](specs/<cap>/spec.md#<anchor>)`, the link text must
    equal the human name of a live requirement in <cap> AND the fragment must
    equal that requirement's GitHub heading anchor — so a retitle that stales the
    visible name OR the jump target is caught. Drift `openspec validate --strict`
    cannot see, since the glossary is not a validated spec and a stale link can
    still carry a live `[ID]`. A bare file link (no fragment) is checked for
    capability existence only.
    """
    anchors_by_cap: dict[str, dict[str, str]] = {}
    for doc in corpus.docs:
        names = anchors_by_cap.setdefault(doc.capability, {})
        for req in doc.requirements:
            names[requirement_name(req)] = github_anchor(_heading_full_text(req))
    violations: list[str] = []
    for pointer in pointers:
        if pointer.capability not in anchors_by_cap:
            violations.append(
                f"{pointer.source}:{pointer.line_no}: glossary link targets unknown "
                f'capability "{pointer.capability}"'
            )
            continue
        if pointer.anchor is None:
            continue  # bare file link (e.g. a Constrained-by capability ref) — existence suffices
        live = anchors_by_cap[pointer.capability]
        if pointer.heading not in live:
            violations.append(
                f"{pointer.source}:{pointer.line_no}: glossary link text "
                f'"{pointer.heading}" matches no live requirement name in '
                f"{pointer.capability} (retitled requirement? update the link)"
            )
        elif pointer.anchor != live[pointer.heading]:
            violations.append(
                f"{pointer.source}:{pointer.line_no}: glossary link to {pointer.capability} "
                f'requirement "{pointer.heading}" has stale fragment #{pointer.anchor} '
                f"(expected #{live[pointer.heading]})"
            )
    return violations


def validate_corpus(
    current: Corpus,
    *,
    committed: Corpus | None = None,
    extra_references: list[Reference] | None = None,
    backlog_anchor_refs: list[BacklogAnchorRef] | None = None,
) -> list[str]:
    """Run every applicable rule; return the consolidated list of violations.

    Rules 3 (no-vanish) and 4 (no-reuse) require a `committed` snapshot; they are
    skipped (with no violation) when one is unavailable.
    """
    violations: list[str] = []
    violations.extend(check_presence(current))
    violations.extend(check_uniqueness(current))
    violations.extend(check_ledger(current))
    violations.extend(check_references(current, extra_references))
    violations.extend(check_backlog_anchor_refs(current, backlog_anchor_refs))
    violations.extend(check_no_positional(current))
    violations.extend(check_prefix_matches_capability(current))
    violations.extend(check_scenario_under_requirement(current))
    if committed is not None:
        violations.extend(check_no_vanish(committed, current))
        violations.extend(check_no_reuse(committed, current))
    return violations


# --- seed allocator --------------------------------------------------------


class SeedAllocator:
    """Per-prefix, allocate-forward identifier minter for the migration wave.

    Seeded from the set of identifiers already in use (live + retired), it hands
    out the next free sequence number per prefix and the next free sub-number per
    requirement — never re-using a number, even one that has been retired.

        alloc = SeedAllocator(existing_ids={"SIGN-001", "SIGN-002"})
        alloc.allocate_requirement("SIGN")      # -> "SIGN-003"
        alloc.allocate_scenario("SIGN-003")     # -> "SIGN-003.01"
    """

    def __init__(self, existing_ids: set[str] | list[str] | None = None, pad: int = 3) -> None:
        self._pad = pad
        self._req_hwm: dict[str, int] = {}
        self._scn_hwm: dict[tuple[str, int], int] = {}
        for spec_id in existing_ids or ():
            self._observe(spec_id)

    def _observe(self, spec_id: str) -> None:
        parsed = parse_identifier(spec_id)
        if parsed is None:
            return
        prefix, number, sub = parsed
        self._req_hwm[prefix] = max(self._req_hwm.get(prefix, 0), number)
        if sub is not None:
            key = (prefix, number)
            self._scn_hwm[key] = max(self._scn_hwm.get(key, 0), sub)

    def allocate_requirement(self, prefix: str) -> str:
        """Return the next free `PREFIX-NNN`, advancing the high-water mark."""
        if not prefix or not prefix.isupper() or not prefix.isalnum():
            raise ValueError(f"invalid capability prefix: {prefix!r}")
        nxt = self._req_hwm.get(prefix, 0) + 1
        self._req_hwm[prefix] = nxt
        return f"{prefix}-{nxt:0{self._pad}d}"

    def allocate_scenario(self, requirement_id: str) -> str:
        """Return the next free `PREFIX-NNN.MM` under `requirement_id`."""
        m = REQ_FULL.match(requirement_id)
        if m is None:
            raise ValueError(f"not a well-formed requirement identifier: {requirement_id!r}")
        prefix, number = m.group(1), int(m.group(2))
        key = (prefix, number)
        nxt = self._scn_hwm.get(key, 0) + 1
        self._scn_hwm[key] = nxt
        # Keep the parent's requirement high-water mark coherent if seeded scenario-first.
        self._req_hwm[prefix] = max(self._req_hwm.get(prefix, 0), number)
        return f"{requirement_id}.{nxt:0{_SCENARIO_SUB_PAD}d}"


# --- filesystem / git IO (CLI only) ----------------------------------------


def _discover_specs(specs_root: Path) -> list[tuple[str, Path]]:
    """Return (capability, path) for each `openspec/specs/<cap>/spec.md`."""
    if not specs_root.is_dir():
        return []
    return [(f.parent.name, f) for f in sorted(specs_root.glob("*/spec.md"))]


def read_current_corpus(specs_root: Path) -> Corpus:
    corpus = Corpus()
    for capability, path in _discover_specs(specs_root):
        text = path.read_text(encoding="utf-8")
        corpus.docs.append(parse_spec(text, capability, source=str(path)))
    return corpus


def _git_show(repo_relative_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{repo_relative_path}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_tracked_specs(specs_repo_root: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", specs_repo_root],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.endswith("/spec.md")]


def read_committed_corpus(specs_repo_root: str = "openspec/specs") -> Corpus | None:
    """Reconstruct the HEAD-committed corpus via `git show`, or None if unavailable."""
    tracked = _git_tracked_specs(specs_repo_root)
    if tracked is None:
        return None
    corpus = Corpus()
    for rel in tracked:
        if not rel.endswith("/spec.md"):
            continue
        text = _git_show(rel)
        if text is None:
            continue
        capability = Path(rel).parent.name
        corpus.docs.append(parse_spec(text, capability, source=f"HEAD:{rel}"))
    return corpus


def gather_test_references(tests_root: Path) -> list[Reference]:
    refs: list[Reference] = []
    if not tests_root.is_dir():
        return refs
    for path in sorted(tests_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        refs.extend(covers_references(text, source=str(path)))
    return refs


# The permanent surfaces guarded against rotting backlog pins (rule 9): the
# current-state spec body and runtime code. Excluded by omission — point-in-time
# records that legitimately cite a backlog item by slug: `docs/adr/`,
# `openspec/changes/` (archived + in-flight), the tool's own fixtures under
# `tests/`, and the top-level docs that describe the inbox as a concept
# (README/AGENTS/CHANGELOG).
_BACKLOG_PIN_ROOTS = ("openspec/specs", "libs", "lambdas", "cdk/lib", "cli", "runbooks")
_BACKLOG_PIN_SUFFIXES = (".py", ".ts", ".md")


def _git_tracked_files(roots: tuple[str, ...]) -> list[str] | None:
    """Return git-tracked files under `roots` (so gitignored build output is skipped)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", *roots],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def gather_backlog_slug_pins(roots: tuple[str, ...] = _BACKLOG_PIN_ROOTS) -> list[str]:
    """Scan the permanent surfaces for rotting backlog pins (rule 9).

    Reads git-tracked files only, so gitignored build artifacts are never scanned;
    if git is unavailable it falls back to a filesystem walk of the same roots.
    """
    tracked = _git_tracked_files(roots)
    if tracked is None:
        tracked = [
            str(f)
            for root in roots
            if Path(root).is_dir()
            for suffix in _BACKLOG_PIN_SUFFIXES
            for f in Path(root).rglob(f"*{suffix}")
        ]
    violations: list[str] = []
    for rel in sorted(tracked):
        if not rel.endswith(_BACKLOG_PIN_SUFFIXES) or rel.endswith(".d.ts"):
            continue
        try:
            text = Path(rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        violations.extend(find_backlog_slug_pins(text, rel))
    return violations


def gather_glossary_pointers(glossary_path: Path) -> list[GlossaryPointer]:
    """Read GLOSSARY.md governing-requirement pointers, or [] when absent (rule 10 I/O)."""
    if not glossary_path.is_file():
        return []
    text = glossary_path.read_text(encoding="utf-8", errors="replace")
    return parse_glossary_pointers(text, source=str(glossary_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spec_ids", description=__doc__)
    parser.add_argument(
        "--specs-root",
        default="openspec/specs",
        help="root of the spec corpus (default: openspec/specs)",
    )
    parser.add_argument(
        "--tests-root",
        default="tests",
        help="root scanned for `Covers: <ID>` markers (default: tests)",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="skip the committed-snapshot rules (no-vanish, no-reuse)",
    )
    parser.add_argument(
        "--no-backlog-pin-check",
        action="store_true",
        help="disable rule 9 (ban rotting backlog-item pins from specs + runtime code)",
    )
    parser.add_argument(
        "--glossary",
        default="openspec/GLOSSARY.md",
        help="glossary scanned for governing-requirement pointers (default: openspec/GLOSSARY.md)",
    )
    parser.add_argument(
        "--no-glossary-anchor-check",
        action="store_true",
        help="disable rule 10 (glossary governing-requirement anchors resolve verbatim)",
    )
    args = parser.parse_args(argv)

    specs_root = Path(args.specs_root)
    current = read_current_corpus(specs_root)
    if not current.docs:
        print(f"error: no spec files found under {specs_root}", file=sys.stderr)
        return 2

    extra_references = gather_test_references(Path(args.tests_root))

    committed: Corpus | None = None
    if not args.no_git:
        committed = read_committed_corpus(args.specs_root)
        if committed is None:
            print(
                "note: no committed snapshot available (not a git repo or no HEAD); "
                "skipping no-vanish/no-reuse rules",
                file=sys.stderr,
            )

    violations = validate_corpus(
        current,
        committed=committed,
        extra_references=extra_references,
    )
    if not args.no_backlog_pin_check:
        violations.extend(gather_backlog_slug_pins())
    if not args.no_glossary_anchor_check:
        violations.extend(
            check_glossary_anchors(current, gather_glossary_pointers(Path(args.glossary)))
        )
    if violations:
        print(f"spec_ids: FAILED ({len(violations)} violation(s)):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print(
        f"spec_ids: OK ({len(current.live_ids())} identifiers across "
        f"{len(current.docs)} spec file(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
