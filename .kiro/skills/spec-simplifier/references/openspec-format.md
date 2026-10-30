# OpenSpec Format (this project)

This project (Cairn Proofs) uses OpenSpec as the single source of truth for requirements. When the spec being simplified lives under `openspec/`, preserve the structure below exactly — simplify only the prose inside it. Breaking structure breaks validation or, worse, silently corrupts the spec.

## Three surfaces — know which one you're editing

1. `openspec/specs/<capability>/spec.md` — the system **as built** (current state). 16 capabilities. Requirements carry **no** phase annotations.
2. `openspec/changes/<name>/specs/<capability>/spec.md` — a proposed **delta**, not a full spec. While a change is in flight, the delta supersedes the canonical spec.
3. GitHub Issues — deferred/future work, upstream of the flow.

Direct edits to `openspec/specs/` outside the `/openspec-*` slash-command flow are **not canonical** and should not be made (AGENTS.md). If asked to simplify a canonical spec in place, simplify the language but flag that the change should land through `/openspec-propose` → `/openspec-apply-change` → `/openspec-archive-change`, not a raw edit. Simplifying an in-flight delta is fine.

## Canonical spec file structure

```
# <capability> Specification

## Purpose
<one paragraph>

## Requirements
### Requirement: [CAP-001] Title

THE <Defined_Term> SHALL <normative statement>.

<supporting normative prose using MUST / MUST NOT / SHALL NOT>

#### Scenario: [CAP-001.01] Title

- **WHEN** <condition>
- **THEN** <expected outcome>
```

## Delta file structure (changes/)

Deltas group requirements under these headers — keep them verbatim:

- `## ADDED Requirements`
- `## MODIFIED Requirements`
- `## REMOVED Requirements`
- `## RENAMED Requirements`

A `MODIFIED` block copies the **full** requirement block, with header text matching the canonical spec exactly, then edits it. `validate --strict` does **not** catch a header mismatch or dropped scenarios — so never alter a `### Requirement:` heading or drop a `#### Scenario:` while simplifying a MODIFIED delta.

## Preserve verbatim — never "simplify" these

1. **RFC 2119 keywords**: MUST, MUST NOT, SHALL, SHALL NOT, SHOULD, SHOULD NOT, MAY, REQUIRED. These are normative requirement levels, not weasel words. The writing rule "remove should/may/might" does **not** apply to them.
2. **Structural keywords** in requirement and scenario text: THE, WHEN, THEN, WHERE — and the `- **WHEN** … - **THEN** …` bullet shape of scenarios.
3. **Hashtag depth**: `### Requirement:` (3) and `#### Scenario:` (exactly 4). A scenario with 3 hashtags fails validation **silently**.
4. **IDs**: requirement IDs like `[ENCRYPT-001]`, scenario IDs like `[ENCRYPT-001.01]`, and any `Source: N.N` numbers. Keep them and keep them aligned (scenario `.01` under requirement `001`).
5. **Defined terms** in `Capitalized_Underscore` form: `Provenance_System`, `Artifact_Bundle`, `Content_Layer`, `Config_Blob`, `Manifest_Digest`, `Metadata_Index`, `Processing_Pipeline`, `WORM_Receipt`, etc. These are glossary terms — do not rephrase, lowercase, or substitute synonyms. `openspec/GLOSSARY.md` is authoritative for each term's concept.
6. **Media types, ARNs, enums, numeric thresholds, and units** — copy exactly; never round or soften.

## Change history

Historical narration, temporal or phase tags (e.g., `*Phase: Post-MVP*`), or statements of what changed relative to previous requirements are prohibited in specs. A spec is not a change log, it defines intended, steady state. If a history lesson or decision log is warranted, it belongs in an ADR, git log, or README. If you're not certain whether something is safe to remove, surface it to the user and confirm before deleting.

## What to simplify

Apply the writing rules to the **prose** only: the Purpose paragraph, the supporting sentences under a Requirement heading, and the descriptive parts of WHEN/THEN clauses. Cut redundant pairs, dead qualifiers, passive voice, and vague hedging that is **not** a normative keyword. Keep one requirement per Requirement block and one clear condition→outcome per scenario.

## Verify after simplifying

These are read-only and safe to run in any session:

- `openspec validate --specs --strict` — must pass clean.
- `openspec list --json`, `openspec status --change <name> --json` — confirm the change/capability you touched.

If `openspec` is not on PATH in the shell, note that the user should run `openspec validate --specs --strict` (or `mise run spec-validate`) before committing.
