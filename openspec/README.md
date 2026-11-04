# `openspec/` — spec source of truth

This directory is the **single source of truth for what Cairn Proofs must do**. Requirements live here as machine-validated specs. Every change to a requirement flows through a structured, versioned change pipeline before it folds back into the canonical specs.

[OpenSpec](https://github.com/Fission-AI/OpenSpec) (installed via `mise`) drives this directory. Project-specific customizations extend it: a custom workflow **schema** (`cairn`), per-artifact **rules**, and a few **tools**, described below. The agent-facing operating manual is [`AGENTS.md`](../AGENTS.md) ("Spec authoring model" / "Code-change workflow"). This README gives the human orientation to the directory and to what we layer on top of stock OpenSpec.

## Layout

```
openspec/
├── config.yaml          Project config: active schema, embedded context, per-artifact rules
├── GLOSSARY.md          Canonical nominal meaning of every Capitalized_Underscore domain term
├── personas/            User personas + their goals/frustrations (proposals must cite one)
├── specs/               CANONICAL specs — the intended steady state, one dir per capability
│   └── <capability>/spec.md
├── changes/             In-flight changes — each a proposed delta to the specs
│   ├── <change-name>/   Active change (proposal, design, specs deltas, threat-model report, tasks)
│   └── archive/         Completed changes, dated YYYY-MM-DD-<name>, kept for provenance
├── schemas/             Custom workflow definitions (artifact pipelines)
│   ├── cairn/           Default schema: proposal → specs → design → threat-model → tasks
│   └── bugfix/          Defect schema: bugfix-analysis → design → tasks
└── tools/               Helper scripts (threat-model reconcile/gate/report)
```

## The two halves: `specs/` vs `changes/`

- **`specs/`** describes the system's **current state as built** — nothing aspirational. Each capability owns one `spec.md` of `### Requirement:` blocks. Each block carries `#### Scenario:` acceptance cases. Requirements carry no phase annotations. Deferred or future requirements live in GitHub Issues (the single source of work to be done) until they graduate into a change.

- **`changes/<name>/`** holds a **proposed delta** to those specs. Its `specs/<capability>/spec.md` files are **not** full specs — they are deltas under `## ADDED` / `## MODIFIED` / `## REMOVED` / `## RENAMED Requirements` headers. While a change is in flight, its delta **supersedes** the canonical spec. `/openspec-archive-change` (or `/openspec-sync-specs`) folds the delta into `specs/` and moves the change into `changes/archive/`. Spec deltas never introduce phase annotations. A GitHub issue captures work deferred out of a change.

> **Don't hand-edit `specs/` directly.** Canonical specs change only by archiving a change. Direct edits bypass validation and provenance and are non-canonical. (Rare, confident doc-only tweaks are the documented exception — see `AGENTS.md`.)

## How a change moves: the `/openspec-*` workflow

Changes are driven by the `/openspec-*` skill commands, which wrap the `openspec` CLI and run the project's review/gate pipeline around it. Typical lifecycle:

| Command | Does |
|---------|------|
| `/openspec-explore` | Think through an idea before scaffolding anything |
| `/openspec-propose` / `/openspec-ff-change` | Scaffold `changes/<name>/` and generate its artifacts (propose = staged, ff = all at once) |
| `/openspec-bugfix` | Same, but on the `bugfix` schema (defect-analysis first) |
| `/openspec-continue-change` | Generate the next single artifact for an existing change |
| `/openspec-apply-change` | Implement the `tasks.md` checklist, run the aggregate review/test gate, reconcile+gate the threat model. On Kiro, the saved `opsx-apply` workflow runs this. |
| `/openspec-verify-change` | Match the implementation to the artifacts before archiving |
| `/openspec-sync-specs` | Fold a change's spec deltas into canonical `specs/` (intelligent merge) |
| `/openspec-archive-change` / `/openspec-bulk-archive-change` | Validate, sync, and move completed change(s) to `archive/` |

Read-only inspection is fair game in any session without a slash command: `openspec list --json`, `openspec status --change <name> --json`, `openspec instructions <artifact> --change <name> --json`, `openspec validate --specs --strict`.

## Custom schemas (`schemas/`)

OpenSpec's stock flow is `proposal → specs → design → tasks`. We extend it:

- **`cairn`** (the active schema, set in `config.yaml`) inserts a **threat-model checkpoint** between specs/design and tasks: `proposal → specs → design → threat-model-report.md → tasks`. The `threat-model` skill builds the threat model during `/openspec-propose`, so STRIDE findings shape requirements and task ordering *before* implementation. The model itself is private: the `threat-composer` MCP server writes it to `threat-models/<change>.tc.json`, a separate private repository. The change directory gets only `threat-model-report.md`, a sanitized summary (counts, gate result, model hash), and `tasks.md` uses neutral language with no threat ids. `apply` then implements the ordered `tasks.md` checklist and then runs the single simplify/review/test gate on the cumulative diff. See `schemas/cairn/schema.yaml` for the full per-artifact instructions.
- **`bugfix`** is a leaner schema for defects: `bugfix-analysis → design → tasks`, where `bugfix.md` states Current/Expected/Unchanged behavior with `**WHEN**/**THEN**/**SHALL**` keywords that seed RED→GREEN→regression tests. Used via `/openspec-bugfix`.

## Harness compatibility (Claude Code + Kiro)

This repo drives the OpenSpec workflow from **two agent harnesses on a single branch** — Claude Code and Kiro CLI. It uses no per-harness branch or fork. Compatibility comes from a deliberate **WHAT-vs-HOW split** across three layers:

| Layer | Carries | Lives in | Per-harness? |
|-------|---------|----------|--------------|
| **WHAT** — workflow semantics | Artifacts and their order, the ordered task checklist (the sequential floor), the single aggregate simplify/review/test gate, the threat-model reconcile/gate | `schemas/*/schema.yaml` instructions + `config.yaml` `context`/`rules` | **No** — single source, injected **verbatim** into every harness by `openspec instructions` |
| **HOW** — execution mechanics | Per-harness execution. On Kiro, the saved `opsx-apply` workflow (`.kiro/workflows/opsx-apply.workflow.json`) runs implement, then the aggregate gate loop, then the full gate. For parallel implementation, Kiro generates a workflow for the change from its `tasks.md`; nothing in the repository schedules it. `AGENTS.md` stays harness-agnostic. | each harness's own configuration (Kiro: `.kiro/workflows/`, `.kiro/steering/`, `.kiro/agents/`) | **Yes** — each harness keeps its own execution detail; `AGENTS.md` stays harness-agnostic |
| **Surfaces** — invocation + agents | The generated `openspec-*` skills and the specialist/orchestrator agent definitions | `.kiro/skills/openspec-*` + `.kiro/agents/*.json` (+ `.kiro/agent-prompts/*.md`) · `.claude/skills/openspec-*` + `.claude/agents/*.md` | **Yes** — parallel per-harness trees, same `/openspec-*` UX |

The load-bearing rule: **the schema says *what* the workflow must accomplish and names no harness-specific primitive. `AGENTS.md` is harness-agnostic and holds the operating rules that every harness shares. Each harness's own configuration holds its execution detail** (on Kiro: the saved `opsx-apply` workflow, which runs implement, then the gate loop, then the full gate; Kiro-generated workflows for parallel implementation; the steering files; and the agent configs). `openspec instructions <artifact>` hands the schema instruction to whichever harness calls it. So anything harness-specific left in the schema (an `Agent` tool-use block, a `subagent` stage, `SendMessage`, `loop_to`) would leak into the *other* harness. That is a layering bug. The fix is to move it down into that harness's own configuration. For the same reason, cross-references name the **agent, not a tool path** (e.g. "the `threat-model` skill", never a `.claude/`- or `.kiro/`-rooted file).

Why this needs no branches: every harness loads the same `AGENTS.md` and receives the same schema instructions, byte for byte, on every invocation. Only each harness's own configuration differs, and no harness reads another harness's configuration. Adding a third harness means a new configuration tree, and it leaves the schema, rules, and `AGENTS.md` untouched.

## Per-artifact rules (`config.yaml`)

`config.yaml` carries the active schema, an embedded `context:` block (injected into every artifact prompt), and **`rules:`** — forcing functions enforced per artifact:

- **proposal** — must name a specific **persona goal/frustration** from `personas/personas.md` (or honestly state "Personas served: none" with a reason). When a change starts from a GitHub issue, cite the issue so `/openspec-archive-change` can close it with `Closes #N`.
- **design** — must carry a mermaid **architecture and/or sequence diagram** (or `N/A — <reason>`). Decisions that change *no* Requirement go to `docs/adr/`, not inline.
- **specs** — new `Capitalized_Underscore` terms must be added to `GLOSSARY.md` in the same change.

## Tools (`tools/`)

| Script | Purpose |
|--------|---------|
| `tc.py` | Reads the private model and never writes it. `reconcile` prints the mitigation and threat status changes that the checked tasks justify, from `task:<N.M>` tags on the mitigations; the session applies them with the MCP tool `tm_set_status`. `gate` is fail-closed: nonzero if a threat has no valid `Priority`, a High threat has no mitigation, or an open threat depends on an unresolved mitigation. `report` writes the sanitized `threat-model-report.md`; `report --check` fails if it is stale. Run at the end of `apply`. Dependency-free. |
| `tc_render.py` | No flow step calls it. Best-effort render of the threat model's mermaid DFD/architecture blocks to embedded PNGs (via `mmdc`) so it displays in Threat Composer. Fail-open. |
| `provision-mmdc-browser.sh`, `mmdc-puppeteer.json` | Provision/config for the mermaid renderer used by `tc_render.py`. |

Example:

```bash
python3 openspec/tools/tc.py reconcile <change>
python3 openspec/tools/tc.py gate <change>
python3 openspec/tools/tc.py report <change> [--check]
```

## Conventions (quick reference)

- **`#### Scenario:` needs exactly 4 hashtags** — 3 fails validation *silently*.
- **`MODIFIED` deltas copy the full requirement block** (header text matching the canonical spec exactly), then edit. `validate --strict` does **not** catch a header mismatch or dropped scenarios.
- **`GLOSSARY.md` is authoritative for a term's nominal concept.** The spec is authoritative for its normative constraints. Add new terms in the change that introduces them.
- **Validation gates merge**: `openspec validate --specs --strict` runs in `mise run check` and pre-commit.

## See also

- [`../AGENTS.md`](../AGENTS.md) — canonical agent operating manual (spec authoring model, code-change workflow).
- [`../README.md`](../README.md) — human-facing system overview.
- [`../docs/adr/`](../docs/adr/) — architecturally-significant decisions that change no Requirement.
- **GitHub Issues** — untriaged quick-capture of deferred work, upstream of `/openspec-propose`.
