# ADR-0016: OpenSpec workflow schemas are harness-agnostic; per-harness mechanics live in operating manuals

## Status

Accepted (2026-06-12)

## Context

OpenSpec schema instructions (`openspec/schemas/*/schema.yaml`) and `config.yaml` (`context` + per-artifact `rules`) drive the `/opsx` workflow. When a session runs a flow, `openspec instructions <artifact> --change <name>` synthesizes the schema's instruction text. It then injects that text **verbatim** into the calling agent. The bytes are the same regardless of which agent harness asked.

This repository drives that workflow from **two harnesses on a single branch**: Claude Code (`.claude/`) and Kiro CLI (`.kiro/`). They share `openspec/` (schemas, specs, changes, tools) and a single operating manual, `AGENTS.md`, that both harnesses read. They split only the per-harness **surfaces**: the generated `openspec-*` skills and the agent definitions under `.claude/` and `.kiro/`.

Over time, the schema `apply` and `tasks` instructions accreted **Claude Code-specific execution mechanics**:

1. the `Agent` tool,
2. "multiple tool-use blocks in one message,"
3. reporting an agent id so the orchestrator could `SendMessage`-**resume** an implementer for fixes.
4. a cohort-by-cohort orchestration loop with a barrier between cohorts.

`openspec instructions` ships those bytes to *every* harness. So a Kiro session received Claude vocabulary that does not map to Kiro's primitives:

- Kiro spawns concurrency through a single `subagent` tool call whose `stages` carry `depends_on` edges. The harness scheduler runs the DAG. (Subagents cannot spawn subagents. Claude shares that constraint, and it is the basis of [ADR-0004](0004-single-post-wave-review-gate.md).)
- Kiro subagent sessions **do not persist or resume**. The orchestrator re-dispatches fixes (a fresh stage, or the `subagent` tool's `loop_to`). So "`SendMessage` to its agent id (it still holds the context)" is a guaranteed failed detour on Kiro.

The schema instruction is re-injected fresh on every invocation, so it is a *salient* channel. It repeatedly surfaced the **wrong** mechanics. The Kiro orchestrator, whose authoritative `AGENTS.md` says the opposite, had to override them on every `apply`.

OpenSpec offers no native per-tool branching for this. There is no `instruction.claude` / `instruction.kiro`. The instruction is a single string, injected as-is. So the question is how to maintain harness-specific execution detail **without** separate branches and **without** a per-tool switch that the tooling does not provide.

## Decision

**OpenSpec schema instructions and `config.yaml` are harness-agnostic. They specify only WHAT the workflow must accomplish and name no harness-specific spawn / parallelism / re-dispatch primitive. The per-harness HOW lives in that harness's operating manual.**

Concretely, a three-layer split:

1. **WHAT — workflow semantics (single source, harness-agnostic).** `schemas/*/schema.yaml` instructions + `config.yaml` `context`/`rules`. They describe:
   1. artifact order,
   2. the wave DAG,
   3. the single post-wave simplify/review/test gate,
   4. the threat-model reconcile/gate.
   5. the dataflow rule that a wave runs as soon as its `dependsOn` prerequisites are met (no artificial cohort barrier).

   They are injected verbatim into every harness.

2. **HOW — execution mechanics (shared manual, always-present).** `AGENTS.md`, "Code-change workflow" section. Both harnesses load this one operating manual as an always-present resource. It describes the mechanics in reference terms: dispatch the whole wave DAG, run the aggregate gate as a follow-up, re-dispatch fixes. It names the harness-specific *primitives* only by pointing down to the surfaces layer.

3. **Surfaces — invocation + agents (per-harness).** The OpenSpec-generated `openspec-*` skills (`.kiro/skills/` and `.claude/skills/`) under this repo's skills-delivery mode, plus the agent definitions: Kiro's `.kiro/agents/*.json` + `.kiro/agent-prompts/*.md`, and Claude's `.claude/agents/*.md`. These are parallel per-harness trees that present the same `/openspec-*` UX.

   Kiro's wave-orchestration primitives live in its hand-authored `.kiro/agents/orchestrator.*`, not in the generated skills (which `openspec update` overwrites). The primitives are:
   1. one `subagent` call that carries the wave DAG as `depends_on` stages,
   2. the gate as a follow-up call.
   3. fixes re-dispatched via `loop_to`.

The load-bearing rule: **harness-specific mechanics appearing in a schema or `config.yaml` is a layering bug. The fix is to push it down into the operating manual.** For the same reason, cross-references name the **agent** (e.g. "the `threat-model` agent's contract"), never a harness-rooted file path. To add a third harness, add a new operating manual + surface tree. The schema and rules stay untouched.

This standard changes **no Requirement** (the 16 capability specs are unaffected). That is why this ADR records it, not the opsx change flow ([ADR-0001](0001-adopt-architecture-decision-records.md) boundary).

Two refinements are consequences of this standard, not separate decisions:

- **Dataflow dispatch.** With mechanics moved to the manual, the Kiro realization hands the whole wave DAG to one `subagent` call. The scheduler then releases each dependent the moment its prerequisites complete. This collapses the old cohort barrier (whose makespan was the sum of per-level maxima) to the DAG's critical path. It evolves [ADR-0005](0005-wave-granularity-as-parallelism-unit.md)'s concurrency model. It does not supersede it.
- **Wave-authoring heuristics** (minimize DAG depth / widen the root cohort, and split a wave only when it shortens the critical path) are self-documenting in the `tasks` artifact instruction.

## Consequences

**Positive**

- One source of truth for workflow semantics, re-injected fresh on every invocation. The shared HOW manual sits on an always-present, uncompactable surface (the orchestrator system prompt + its `AGENTS.md` resource). No branch divergence for what is one workflow.
- Removes a standing contradiction. The salient, re-injected schema instruction no longer ships stale Claude vocabulary that the Kiro orchestrator had to override every `apply`. It also no longer ships a guaranteed-failed `SendMessage`-resume detour.
- Work to add or maintain a harness stays local to its operating manual + surface tree. `openspec/` is untouched.
- Applied to **both** schemas (`cairn` and `bugfix`), so the second schema cannot silently re-leak the same mechanics.

**Negative**

- A fact about the workflow can now appear in two places (schema WHAT, `AGENTS.md` HOW). A harness-specific primitive can appear in a third (that harness's surfaces). Discipline is required to keep WHAT out of the manual and HOW out of the schema. This ADR is the guardrail against re-leaking.
- The shared manual must describe the HOW in reference terms without binding to one harness's primitives. The per-harness surfaces must each carry complete, correct primitive mappings. They can drift in *behavior* if not maintained together (the manual only guarantees that they pursue the same mechanics).
- Adherence is a soft guarantee that the model interprets at instruction-read time. A future edit can re-introduce harness-specific text into a schema, and only the next `/opsx` run surfaces it.

## Alternatives Considered

- **A branch (or fork) per harness.** Rejected: divergent branches for a single shared workflow. It adds a perpetual merge burden across `openspec/specs`, `changes/`, and `tools/`, which are identical for both harnesses.
- **Per-tool branching inside the schema instruction** (`instruction.kiro` / `instruction.claude`, or templating by tool). Rejected: OpenSpec has no such mechanism. `openspec instructions` injects one instruction string verbatim with no tool conditional.
- **Keep mechanics in the schema and let each harness translate on the fly.** Rejected: this is the status quo that leaked. It produced fresh, salient, contradictory Claude vocabulary on every Kiro `apply`, plus a failed-resume round-trip. Relying on the agent to repeatedly override its own authoritative instructions is fragile.
- **Put the per-harness HOW only in the OpenSpec-generated `openspec-*` skills.** Rejected as the *durable* home. `openspec update` regenerates those skills and overwrites edits inside them. The operating manual (loaded as a resource / inlined in the orchestrator system prompt) survives regeneration and is not subject to context compaction. A harness's own agent definitions may carry its primitives, but the shared manual is authoritative.

## References

- `openspec/README.md`, "Harness compatibility (Claude Code + Kiro)" — the WHAT/HOW layout (the *how-it's-wired* companion to this *why*). WHY in this ADR, layout in the README, cross-linked, not duplicated.
- [ADR-0001](0001-adopt-architecture-decision-records.md) — the no-Requirement-change vs Requirement-change boundary that places this standard in an ADR, not the opsx flow.
- [ADR-0004](0004-single-post-wave-review-gate.md) — the orchestrator-driven single gate (the subagents-cannot-spawn-subagents constraint that this standard also rests on).
- [ADR-0005](0005-wave-granularity-as-parallelism-unit.md) — the wave-as-concurrency-unit model. The dataflow dispatch refinement evolves it.
- [ADR-0007](0007-cairn-test-scoping-and-template-forcing-functions.md) — sibling schema-authoring decision.
- `AGENTS.md`, "Code-change workflow" — the shared HOW manual.
- `openspec/schemas/cairn/schema.yaml` and `openspec/schemas/bugfix/schema.yaml` — harness-agnostic `apply` + `tasks` instructions.
