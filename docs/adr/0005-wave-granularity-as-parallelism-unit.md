# ADR-0005: Wave granularity is the parallelism unit (no per-task subagents)

## Status

Accepted (2026-05-31)

## Context

The `/opsx` code-change pipeline dispatches each wave of `tasks.md` to an implementer subagent. `AGENTS.md` "Code-change workflow" documents this pipeline, and the `cairn` and `bugfix` schema `apply` instructions execute it. The Task Dependency Graph at the bottom of `tasks.md` is a DAG keyed on `dependsOn`. A **cohort** is the set of waves whose `dependsOn` prerequisites are already satisfied. The orchestrator runs every cohort concurrently, one subagent per wave, in a single assistant turn.

This ADR addresses a different axis of the same model than [ADR-0004](0004-single-post-wave-review-gate.md). ADR-0004 settled the **review cadence**: no per-wave review, and one aggregate gate after all waves. This ADR settles the **concurrency unit**: how the workflow should achieve *more* parallelism.

The triggering question: should the orchestrator parallelize *within* a wave? For example, it could spawn one subagent per wave-task. The goal would be to exploit independence between tasks that currently share a single wave's subagent.

Two facts frame the answer:

- **The orchestrator's only concurrency lever is spawning subagents, and sub-agents cannot spawn sub-agents.** The harness withholds the `Agent` tool from non-root contexts. The same constraint forces ADR-0004's orchestrator-driven review. A wave subagent therefore cannot fan its own tasks out concurrently. It runs them sequentially in one context. Any true parallelism must come from the *orchestrator* spawning more subagents.

- **A wave already encodes the preconditions for safe concurrent execution.** The wave-packaging rules require each wave to be file-disjoint from every other wave. They also require each wave to carry a self-contained test surface (tests ship in the same wave as the code they cover). Sibling waves in one cohort *already* express independent file-disjoint work, and the apply loop *already* runs those waves concurrently. Across 17 real `tasks.md` files, multi-task waves bundle tasks precisely because they share a file or a cohesive test surface. These are the cases that are *not* safe to split.

So "parallelize within a wave" is a category error. A single task does not individually satisfy the file-disjoint, test-self-contained, single-context preconditions. That is *why* tasks get grouped into a wave. When a set of tasks *does* satisfy those preconditions, the correct representation is separate sibling waves, not a new spawn layer below the wave. **Wave granularity at tasks-generation time** unlocks the latent parallelism that the question was reaching for. It does so through the cohort mechanism that already exists, with no change to the `apply` orchestration loop.

## Decision

**Wave granularity is the parallelism unit. There is no subagent-per-task.**

This decision is encoded as guidance in three places: the `tasks`-artifact instruction of both the `cairn` and `bugfix` schemas, their `tasks.md` templates, and the `AGENTS.md` "Code-change workflow" narrative.

1. **Target the finest *safe* wave.** A wave SHOULD be the smallest set of tasks that is (a) file-disjoint from every other wave and (b) carries a self-contained test surface. To run independent work in parallel, emit it as **separate sibling waves** sharing the same `dependsOn` (one cohort). Do not bundle several tasks into one wave for this purpose.

2. **`##` task-group headings are organizational, not wave boundaries.** A heading exists for human readability. Its tasks may split across waves, and one wave may draw tasks from several headings (the example DAGs put `1.1` and `4.1` in wave 0). File ownership + shared test surface decide wave membership, never the heading.

3. **No per-task spawning.** The orchestrator spawns exactly one implementer subagent per wave. It does not subdivide a wave across subagents.

4. **Anti-fragmentation guardrail.** Keep tasks in one wave when they share a file, share a test, or form a cohesive multi-file unit. A single subagent implements such a unit better with shared context. Each wave costs one implementer spawn (fixed per-spawn overhead + lost shared context). So atomizing a cohesive change into per-file waves is a net loss.

The `bugfix` schema's RED→fix `dependsOn` edge does not change. Reproduction tests ship in an earlier wave than the fix, so they can be proven to fail first. This edge holds regardless of granularity.

This is a pure tasks-generation guidance change. The `apply` orchestration loop is **untouched**. It already spawns per-wave and runs cohorts concurrently.

## Consequences

**Positive**

- The existing cohort mechanism exposes latent parallelism: wider cohorts, lower wall-clock, with zero new orchestration code and no new failure modes.
- The wave invariants stay intact. File-disjointness means no merge contention between concurrent subagents. Test co-location is the only in-loop gate, per ADR-0004. Both invariants hold precisely because the unit of concurrency remains the wave.
- A frequently-re-litigated question ("why not a subagent per task?") now has a recorded answer. The record keeps the rejected alternative and its reasoning.

**Negative**

- Wave-count can grow, and each wave is one implementer spawn. The anti-fragmentation guardrail (Decision 4) is a soft, judgement-based limit, not a hard cap. A tasks author who over-splits pays per-spawn overhead and forfeits shared context. This is the same cost axis ADR-0004 was sensitive to, now on the implementer side rather than the reviewer side.
- The model interprets the guidance at tasks-generation time, because the wave DAG has no code parser. So adherence is a soft guarantee, and a lapse surfaces only at the next `/opsx:apply`.

## Alternatives Considered

- **One subagent per wave-task (the triggering proposal).** Rejected. A task is not an independently ownable unit. It may share a file or test with its wave neighbours. So per-task subagents reintroduce the merge contention and the impl/test split that the wave abstraction exists to prevent. They also collide with the task-agnostic "no two waves edit the same file" rule. And they lose the shared context a single subagent has across a cohesive multi-file change.

- **A "fan-out impl / fan-in test" wave shape.** In this shape, the orchestrator spawns N file-disjoint implementer subagents for one wave, then runs a single shared test pass. This would address the one genuine tension, where a shared integration test currently forces several file-disjoint tasks into one coarse wave. **Deferred**. It adds a second concurrency tier and orchestration branching. The finer-sibling-waves guidance covers this case in the common path. Revisit only if shared-integration-test coupling is observed to be forcing materially coarser waves in practice.

- **Leaving the guidance implicit.** Rejected. The existing `tasks` instruction said only "group related tasks." That wording lets file-disjoint independent work be over-bundled into one wave (observed in archived changes). The result silently leaves parallelism on the table.

## References

- ADR-0001 (ADR adoption). It sets the requirement-change vs no-requirement-change boundary that places this process decision in an ADR rather than the spec flow.
- ADR-0004 (single post-wave quality gate) — the **review-cadence** axis of the same wave model. This ADR is the **concurrency-granularity** axis. Related, not superseding: both decisions stand.
- `AGENTS.md`, "Code-change workflow" (step 2, wave-orchestrated implement).
- `openspec/schemas/cairn/schema.yaml` and `openspec/schemas/bugfix/schema.yaml` — `tasks`-artifact instruction (Wave packaging rules). The `apply` instruction is unchanged.
- `openspec/schemas/cairn/templates/tasks.md` and `openspec/schemas/bugfix/templates/tasks.md` — trailing wave-granularity note.
