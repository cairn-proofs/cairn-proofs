# ADR-0001: Adopt Architecture Decision Records (ADRs)

## Status

Accepted (2026-05-30)

## Context

OpenSpec splits the system's documentation into two halves. The normative **WHAT** lives in `openspec/specs/`. It is living, it accumulates over time, and it is the single source of truth for requirements. The point-in-time **WHY** lives in each change's `design.md`. When the change is archived, its `design.md` is frozen into `openspec/changes/archive/`. There is no living, cross-cutting **design narrative** in between. Nothing carries forward the reasoning behind decisions that span multiple capabilities or that no one re-opens.

A steady-state `docs/` design narrative was meant to fill that gap, but that approach is structurally prone to drift. A narrative restates the current steady-state. Steady-state restatements rot against the specs as the specs evolve. The narrative is structurally a duplicate of `openspec/specs/`, and no mechanism keeps the two in sync.

The decisions that suffer most from this gap are cross-cutting decisions, deferrals, and "considered & rejected" rationale that map to **no single Requirement change**. They have nowhere canonical to live. They are too broad for one spec, they are orphaned from any owning change, and they are corrosive to the narrative doc.

## Decision

Adopt numbered, append-only Architecture Decision Records under `docs/adr/NNNN-kebab-title.md`.

ADRs are **immutable once Accepted**. To reverse a decision, write a *new* ADR and set the old one's Status to "Superseded by ADR-NNNN". Never edit the body of the old one. This preserves intact the record of what we believed and why, at the time we believed it.

The boundary rule against the opsx change flow is explicit:

- A decision that **changes a Requirement** goes through the opsx change flow. Its rationale lives in that change's `design.md`. The normative WHAT lives in the spec delta. It is **not** also written as an ADR.
- A decision that **changes no Requirement** — a deferral, a technology evaluation, a cross-cutting standard — is recorded as an ADR.

WHY lives in the ADR, and WHAT lives in the spec. The two are cross-linked, never duplicated. This honors the single-source-of-truth rule in `AGENTS.md`.

ADRs live under `docs/adr/`, not inside the `openspec/` tree, for two reasons.

1. **Scope.** ADR-0001 mandates a single, chronological decision log. But ADR subjects range well beyond the spec system: specs, infrastructure, toolchain, and process (this very ADR is about the docs process). A split of the log by subject would destroy its "one place" value, so the log must live in a scope-neutral tree. `openspec/` is spec-scoped and would mis-signal a toolchain or process ADR as spec governance.
2. **Coupling direction.** A glossary or personas file earns a place in `openspec/` because the generation flow *reads and injects* it. ADRs are records *produced alongside* decisions. They are only cross-referenced, never injected into artifact generation or validated. An input to the flow belongs in the flow's tree. An output record does not.

`docs/` is already the project-wide engineering-reference tree, which matches ADRs' project-wide scope.

## Consequences

**Positive**

- Drift-resistant by construction: ADRs are point-in-time and append-only. So they cannot rot against the evolving specs the way a steady-state narrative does.
- A greppable, chronological decision log — the cross-cutting WHY is discoverable in one place instead of reconstructed from git and archived change directories.
- Fills the opsx living-narrative gap without re-introducing a dual source of truth.

**Negative**

- Another documentation surface that requires discipline to write at decision time rather than after the fact.
- Requires the boundary rule above to be observed, or ADRs will overlap with change `design.md` content.
- Does not by itself retire the pre-existing `docs/` design narrative — that is a separate cleanup.
- Adoption is only load-bearing once `AGENTS.md`, and ideally the opsx flow, reference ADRs as the home for no-Requirement-change rationale.

## Alternatives Considered

- **Keep rationale in the `docs/` narrative design doc.** Rejected: narrative restates current steady-state and drifts against the specs as they evolve. This is the exact failure a steady-state `docs/` design narrative exhibits.
- **Force all rationale into opsx change `design.md`.** Rejected: orphan and deferral decisions have no owning change. Also, `design.md` freezes on archive, so its rationale is discoverable only by git or archive archaeology.
- **Embed ADRs as sections inside a steady-state `docs/` design narrative.** Rejected: re-inherits that narrative's drift gravity — the new records would rot alongside it.

## References

- `AGENTS.md`, sections "docs/ is engineering reference, not spec" and "Conventions / single source of truth".
- Michael Nygard, "Documenting Architecture Decisions" — the originating ADR format.
