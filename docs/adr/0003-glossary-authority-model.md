# ADR-0003: Glossary authority model — nominal-authoritative, normative-indexed

## Status

Accepted (2026-05-30)

## Context

We are introducing a canonical project glossary. It replaces the term definitions that lived at the top of the now-`SUPERSEDED` requirements document. A separate decision sets its physical home (see the glossary B-track). This ADR records only the *authority model*. Should the glossary be an **index** (pointing to definitions that live elsewhere) or **authoritative** (the canonical source of each term's meaning)?

An investigation mapped all 31 candidate terms to where their meaning is actually pinned. A first pass over-counted "defined" terms. A skeptical re-verification corrected it to this rough split:

- **Most** are governed by a Cairn spec requirement (e.g. `Config_Blob`, `WORM_Enabled`, `Manifest_Digest` ([INGEST-006]), `WORM_Receipt` ([WORM-005])).
- **A handful** are external/industry standards (`OSCAL`, `OCI`, `Referrer`, `ORAS`, `Notation`, `TSA`, `TST`).
- **A few** are **used-only**: used (and sometimes format-constrained) by specs but *defined* by no requirement — confirmed so far for `Evidence_Producer` and `Assessment_Period`. Their concept lived only in the old glossary.
- **One** is self-evident — `Cairn`, the product's own name.

The exact per-term map is finalized during glossary authoring. The headline that matters: "the spec *defines* this term" is frequently false. Specs more often *constrain* a term (a format, an enum), while only the glossary *names* the concept. That distinction motivates the layered model below. The used-only terms are its limiting case: where no requirement exists to index, the glossary's nominal authority simply stands alone.

The decisive finding concerns the spec-governed majority. For these terms, a term's meaning is **inseparable from a normative constraint** that lives in `openspec/specs/`. The constraint lives there *because* it is tested, validated by `openspec validate --strict`, and versioned through the change flow. Examples are `WORM_Enabled`'s true/false branches, `Config_Blob`'s required fields, `Evidence_Category`'s seven-value enum, and `Assessment_Period`'s format.

Making the glossary wholesale-authoritative would therefore do one of two things:

1. **Duplicate** those constraints — the dual-source rot the OpenSpec migration eliminated.
2. **Relocate** normative, tested behaviour into an un-validated, un-change-flowed markdown file. This inverts the dependency, so the specs depend *outward* on an ungoverned file for their own meaning.

OpenSpec has no first-class governed artifact for non-Requirement normative content (a glossary cannot carry scenarios or pass strict validation).

A term's meaning, however, has two layers, and only one is normative:

- **Nominal** — the concept the term denotes. No testable behaviour.
- **Normative** — enums, formats, structures, behaviour. Tested and validated.

## Decision

The glossary is **authoritative for the nominal layer** (a one-sentence concept statement per term) and for **external-term glosses** (with a link to the governing standard). It is an **index for the normative layer**: each spec-governed entry points to the single governing requirement, which remains the authority for constraints.

Each entry therefore is: an authoritative nominal sentence **plus** a pointer — to the governing requirement (spec-governed terms) or the external standard (external terms).

This keeps all normative, tested content inside the governed, validated, change-flowed spec tree (single-source). It also gives readers one authoritative place to learn *what a term is*. It is consistent with the `personas.md` precedent: a governed-tree file that is canonical for its non-normative content, not a validated spec.

## Consequences

**Positive**

- Single source preserved — normative constraints never leave `openspec/specs/`.
- One authoritative read for "what is this term" (nominal) without ungoverning any requirement.
- External terms get a clear local gloss plus a link to their real authority.

**Negative**

- The index pointer to a governing requirement is implicit and therefore fragile: an edit to that requirement without awareness can silently break the anchor. Mitigation is a follow-on decision, not adopted here. The mitigation is a named-requirement reference plus an optional `rules.specs` drift-check that flags when someone renames or removes a governing requirement.
- Requires discipline: a change that introduces a new domain term must add its glossary entry in the same delta. Enforcing this via a `rules.specs` rule is a separate decision.
- The glossary's nominal content is not checked by `openspec validate` — it is reference, not a validated artifact.

## Alternatives Considered

- **Fully authoritative glossary** (the glossary defines everything; specs merely reference defined terms — the contract / RFC / ISO "Defined Terms" model). Rejected: in Cairn the meaning of most terms *is* a normative, tested constraint. So this model either duplicates spec content or relocates tested behaviour out of the governed tree. It also inverts the spec→meaning dependency. The only principled route to a fully-authoritative glossary is a future OpenSpec feature. That feature is a first-class, governed, validated "terminology" artifact that flows through propose/validate/archive like a spec. That is a tool feature request and is deferred.
- **Pure index** (the glossary points everywhere and owns nothing). Rejected as the sole model for two reasons. First, the seven external terms have no in-repo authority to point to, so the glossary must locally own their gloss. Second, a bare pointer with no nominal sentence is less useful than an authoritative concept statement.

## References

- ADR-0001 (ADR adoption and the requirement-change vs no-requirement-change boundary rule).
- `AGENTS.md`, "Conventions / single source of truth".
- `openspec/personas/personas.md` — precedent for a governed-tree, non-validated, canonical reference.
- `/opsx:explore` session, 2026-05-30 — term→authority map: 24 spec-governed, 7 external, 0 orphaned.
