# Review notes — Cairn Proofs documentation set

> Generated: 2026-07-01 by the codebase-summary SOP. Records consistency and
> completeness findings for the generated documentation under `docs/` and
> flags where it depends on limited analysis.

## Consistency checks

### Finding C1 — capability count disagreement (source inconsistency, not a doc bug)
`README.md` and the live spec tree both show **18** capabilities under
`openspec/specs/` (confirmed by directory listing). `AGENTS.md` states "**16**
capability specs" in two places ("Spec authoring model" and the "Repository
layout" section). This is a pre-existing inconsistency in the source material.
- **Generated docs use 18** (the verifiable count).
- **Recommendation:** update `AGENTS.md`'s "16" to "18" (the two spec dirs added
  since that text was written appear to be `producer-claims` and
  `identity-registry`, both of which `README.md` already lists).

### Finding C2 — CLI command surface understated in AGENTS.md
`AGENTS.md` describes the CLI as "push/pull/verify/query". The actual CLI also
ships `sign`, `alias`, `config`, and `tui` subcommands (confirmed in
`cli/cairn_cli/main.py` and the per-command modules). Generated docs list the
full surface.
- **Recommendation:** broaden the AGENTS.md CLI one-liner, or leave it as an
  intentional highlight of the primary verbs — low severity.

### Finding C3 — event detail-type strings verified only for validate stage
The exact EventBridge detail-type literals were read directly for
`validate_bundle` (`artifact.validated`, `artifact.validation-failed`) and the
shared `EVENT_SOURCE` (`dev.cairnproofs.artifact`). The other stages'
literals were inferred from the `artifact.<event>` convention and their
`emit_*` helper names (present in the symbol map) rather than read verbatim.
- **Impact:** low — the convention is consistent and the helper names are
  confirmed. `interfaces.md` states this scoping honestly.
- **Recommendation:** if exact strings matter for a task, read
  `lambdas/*/events.py` for that stage.

### Finding C4 — no filename collisions, deliberate naming distinction
Generated files (`architecture.md`, `data_models.md`, …) do not collide with the
curated `docs/architecture-as-deployed.md` or `docs/data-model.md`. The two
data documents are intentionally distinct: `data_models.md` (code-level types)
vs `data-model.md` (persisted DynamoDB schema, canonical). Cross-references
point readers to the canonical file.

### Consistent facts (spot-checked against ground truth)
- Workspace members (`cli`, `lambdas/*`, `libs/*`), 5 Lambdas, 6 libraries —
  match `pyproject.toml` and the symbol map.
- Toolchain pins, `mise run check` composition, ruff rule set (`E,F,I,W,B,S,UP,RUF`)
  — match `mise.toml` / `pyproject.toml`.
- Event/field vocabularies — match `libs/observability/keys.py`.
- Single-source-of-truth convention respected: generated docs defer to specs,
  `architecture-as-deployed.md`, and `data-model.md` rather than restating them.

## Completeness checks

### Gap G1 — persisted record schema deferred (by design)
`data_models.md` documents code-level models but defers the DynamoDB item
attribute layout to the canonical `docs/data-model.md`. This avoids duplicating
a source of truth; it means the generated set alone does not fully describe
persisted attributes. Intentional.

### Gap G2 — runbooks and per-change threat models not summarized
Operational runbooks (`runbooks/`) and per-change threat models
(`threat-model.tc.json`) are referenced but not summarized in the generated set.
A reader needing operational detail must open those files.

### Gap G3 — CDK construct internals described at role level only
`components.md` lists the CDK constructs and their roles but does not document
each construct's props or resource-by-resource configuration. For the deployed
resource detail, `docs/architecture-as-deployed.md` is the reference.

### Gap G4 — retention/SLO/observability breadth is summarized, not exhaustive
The `retention-lifecycle`, `runtime-slos`, and `observability` capabilities are
rich; generated docs capture their headline behavior and route to the specs for
the full requirement set.

## Language-support limitations

- **Bash** (`cdk/scripts/*.sh`, `lambda-layers/notation/build.sh`,
  `cli/scripts/*.sh`, `scripts/*.sh`): described by role and effect, not
  line-by-line. Concurrency-locking and verification behavior is summarized from
  `AGENTS.md` and the symbol map.
- **`oscal-bindings` (PyPI)**: out of scope for symbol analysis;
  documented only as the SDK that `libs/oscal` adapts.
- **Generated / vendored trees** (`node_modules`, `lambda-bundles`, `.venv`):
  excluded.
- **TypeScript CDK**: analyzed via constructs and jest tests; construct-internal
  prop wiring not exhaustively enumerated (see G3).

## Recommendations

1. Fix the capability count in `AGENTS.md` (C1) — the only clear source error
   surfaced by this pass.
2. Optionally broaden the AGENTS.md CLI description (C2).
3. Keep the generated set as *navigation* reference; keep behavioral authority in
   `openspec/specs/` and persisted-schema authority in `docs/data-model.md`.
4. Regenerate this set after significant structural changes; the consolidated
   `AGENTS.md` Custom Instructions section survives regeneration.
5. When exact event literals or DynamoDB attributes matter for a change, read the
   corresponding source module rather than relying on these summaries.
