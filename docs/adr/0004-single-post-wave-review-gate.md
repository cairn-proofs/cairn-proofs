# ADR-0004: Single post-wave quality gate for the code-change workflow

## Status

Accepted (2026-05-30)

## Context

The `/opsx` code-change pipeline dispatches each wave of `tasks.md` to an implementer subagent. `AGENTS.md` "Code-change workflow" documents the pipeline, and the `cairn` and `bugfix` schema `apply` instructions execute it. Because sub-agents cannot spawn sub-agents, the main-session orchestrator runs all specialist review/simplify passes, not the wave subagent.

As originally written, the orchestrator ran a full review cycle **per wave**, *before* advancing the DAG:

1. `code-simplifier`.
2. `code-reviewer` + `security-reviewer` on each wave's diff.
3. A resume-the-implementer-and-re-review loop.

A final aggregate pass followed. The agent-spawn count (and therefore both wall-clock and token cost) scaled with the number of waves. A multi-wave change paid three-plus review-agent spawns and one or more fix/re-review loops for every wave.

An intermediate revision the same day narrowed per-wave review to **foundational** waves only. A foundational wave is a wave that appears in another wave's `dependsOn` list. The revision deferred **leaf** waves to the aggregate pass and moved `code-simplifier` to a single cumulative pass. This preserved defect locality where later waves build on a diff. But it still paid per-foundational-wave review cost and added DAG-position branching to the orchestration loop.

The binding constraint surfaced explicitly: the per-wave review workflow costs too much time and too many tokens to be acceptable, even in the foundational-only form.

Separately, the project gained `openspec/tools/tc.py` (a dependency-free threat-model `reconcile`/`gate` helper). The question of where to document its invocation intersects this workflow. Its invocation belongs in the OpenSpec source of truth, not hand-rolled into flow slash commands.

## Decision

**1. No per-wave agent review.** Inside the wave loop, an implementer subagent only implements its tasks and writes + runs its own tests to green (Bash, not an agent spawn). That test run is the sole in-loop gate. The orchestrator records each implementer's agent id (to resume it for fixes later) and does `tasks.md` bookkeeping. But it spawns **no** review/simplify/test agents while waves are still running.

**2. One aggregate quality gate after all waves.** Once every wave completes, the orchestrator runs a single gate on the **cumulative** diff, in order:

1. `code-simplifier` — the one simplify pass. It catches cross-wave duplication that a per-wave pass is structurally blind to.
2. `code-reviewer` + `security-reviewer` + `tester` spawned **in parallel**. Reviewers must report no Critical/High. `tester` fills coverage/edge-case gaps.
3. `mise run check` (full pre-merge gate).

The orchestrator fixes findings by resuming the relevant wave implementer via `SendMessage` (or directly for a pure cross-wave seam issue). Then it re-runs the gate.

`AGENTS.md` and both the `cairn` and `bugfix` schema `apply` + `tasks` instructions apply this identically.

**3. Threat-model reconcile/gate is sourced from OpenSpec, not the slash commands.** The `cairn` `threatmodel` artifact instruction names `openspec/tools/tc.py reconcile <change>` (verify) and `... gate <change>` (archive) plus the fail-closed conditions. That instruction is the single source of truth. The `/opsx:verify` and `/opsx:archive` slash commands stay thin wrappers that carry **no** threat-model logic. They were restored to their first-commit baseline after the removal of a bolt-on step. This is consistent with the established customization-path principle: flow logic lives in OpenSpec, and slash commands stay vendor-aligned wrappers.

## Consequences

**Positive**

- Review cost is now a single fixed set of agent spawns per change regardless of wave count. Before, it scaled with the number of (foundational) waves. This is the time/token reduction that motivated the change.
- The single simplify pass sees duplication *between* waves. The single review pass sees cross-wave composition / seam issues (e.g. a new caller inconsistent with an existing API). Both are invisible to per-wave passes.
- Wave sizing simplifies: size for clean file ownership and a self-contained test surface, not for review isolation. The DAG-position (foundational/leaf) review branching is removed entirely.
- Threat-model tooling is single-sourced in OpenSpec. The slash commands stay thin and survive upstream `openspec` updates.

**Negative**

- **Loss of defect locality.** A reviewer does not catch a bad pattern in an early wave until the end, after later waves have built on it. The fix can be larger and may require re-touching downstream waves. Two things partially mitigate this: the retained per-wave Bash test gate (broken behaviour is still caught in-loop), and resume-to-fix via the recorded implementer agent ids.
- Reviewers operate on a larger cumulative diff, where finite attention is more likely to miss something than on a focused per-wave diff.
- **No hard archive-time gate for the threat model.** OpenSpec 1.3.1's workflow schema supports only an `apply` phase. There is no `archive`/`verify` hook, and the schema validator silently strips unknown top-level keys (an `archive:` block would be a no-op that *looks* wired). `openspec archive` runs no schema-driven hook either. So `tc.py gate` firing pre-archive relies on the orchestrator consulting the `threatmodel` instruction (a soft guarantee). The available hard-guarantee option is a pre-commit hook keyed on the change-directory rename into `changes/archive/`. That option is **deferred**.

## Alternatives Considered

- **Per-wave review on every wave** (the original). Rejected: agent-spawn cost scales with wave count, which is the cost the user could not absorb.
- **Foundational-wave-gated review** (intermediate, superseded the same day — commit `924a8a5`). Rejected: it still pays per-foundational-wave review cost and adds DAG-position logic to the orchestration loop. The user judged even that too expensive. Its merit is defect locality on the waves others build on. That merit was deemed not worth the cost, given the per-wave Bash test gate already catches broken behaviour in-loop.
- **Re-adding a threat-model step (or even a thin "consult OpenSpec" pointer) to `verify.md`/`archive.md`.** Rejected: a diff against each file's first local commit showed neither had any threat-model step originally. The steps were a later bolt-on. Flow logic belongs in OpenSpec, so the steps were removed rather than rewritten.
- **A schema-level `archive:` hook for a hard pre-archive gate.** Not available in OpenSpec 1.3.1 (silently stripped). Deferred to a possible toolchain-level pre-commit hook.

## References

- ADR-0001 (ADR adoption; the requirement-change vs no-requirement-change boundary that places this process decision in an ADR rather than the spec flow).
- `AGENTS.md`, "Code-change workflow".
- `openspec/schemas/cairn/schema.yaml` and `openspec/schemas/bugfix/schema.yaml` — `apply` + `tasks` instructions; the `cairn` `threatmodel` instruction.
- `openspec/tools/tc.py` — `reconcile` + `gate`.
- Commits: `5eedfb5` (this decision — single post-wave gate; threat-model logic to OpenSpec), superseding `924a8a5` (the foundational-wave-gated intermediate).

## Related

_Appended 2026-05-31 (additive; this decision is unchanged and not superseded)._

- ADR-0005 (wave granularity is the parallelism unit; no per-task subagents) — the **concurrency-granularity** axis of the same wave model that this ADR settled on the **review-cadence** axis. Both decisions stand.
