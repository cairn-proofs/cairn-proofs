# ADR-0021: Retire wave orchestration and keep the aggregate gate and harness-agnostic layering

## Status

Accepted (2026-11-01)

## Summary

This decision retires the wave model: the static Task Dependency Graph in `tasks.md`, the `wave_ledger.py` state tracker, and the per-harness orchestration prompts. Portable surfaces now describe a plain ordered checklist followed by one aggregate quality gate on the cumulative diff, and each harness realizes them with its own native primitives. On Kiro, implementation parallelism moves from the author-planned graph to workflows that Kiro generates for each change, and the repository schedules nothing.

Replaces ADR-0004, ADR-0005, ADR-0007, and ADR-0016 (deleted). Their text is in git history, not in the working tree.

## Context

The wave model was hand-rolled orchestration. `tasks.md` carried a `## Task Dependency Graph` JSON block that grouped tasks into file-disjoint waves. The orchestrator parsed the block, dispatched each wave to an implementer sub-agent, and tracked per-wave status with `openspec/tools/wave_ledger.py`. Earlier harness versions forced this design, because they had no nested sub-agents and no native DAG or loop primitives.

The hand-rolled layer cost more than it returned:

- **Full-suite re-runs scaled with the number of waves.** The root `pyproject.toml` sets `addopts = -n auto`, so a bare `pytest` runs the whole repository. Each wave implementer and the simplify re-confirm ran tests, so the full suite ran about once per wave plus once more per change. Each run's output landed in a sub-agent's context, and the cost grew with the suite.
- **Harness mechanics leaked across harnesses.** `openspec instructions` ships the schema `apply` and `tasks` text verbatim to every harness. Kiro sessions therefore received Claude mechanics (an `Agent` tool, `SendMessage` resume, and a cohort barrier) that have no Kiro equivalent. The orchestrator overrode them on every apply.
- **State tracking was fragile.** `wave_ledger.py` kept per-wave status in a per-user ephemeral directory and carried its own path-safety and atomic-write code.
- **Every change paid a planning cost.** An author had to build the graph block and keep it file-disjoint, and the orchestrator had to parse it. The only return was author-planned static parallelism.

The harnesses have since changed. Claude Code allows three levels of agent nesting by default. Kiro 1.4.62 (CLI engine V3) supplies native DAG and loop primitives through workflows, and it generates a workflow for the task at hand.

Before this decision, six feasibility checks (F1 to F6) confirmed the Kiro mechanism, and all six passed. The Kiro `validate_workflow` tool is not offered in these sessions, so F1 used the launch-time validation of `run_workflow` on an inert copy of the recipe. The copy launched with no errors and no warnings.

## Decision

**D1.** Schemas, `config.yaml`, and `AGENTS.md` are harness-agnostic. Per-harness mechanics live in that harness's own surfaces (on Kiro: the workflow recipe, steering, and agent configs). A harness-specific mechanic in a portable surface is a layering bug, and the fix moves it down. A new harness adds its own surface tree and leaves the portable surfaces unchanged. The rule covers both the `cairn` and `bugfix` schemas.

**D2.** Cross-references name the agent (for example "the `threat-model` agent's contract"), never a harness-rooted file path.

**D3.** Per-harness mechanics live in hand-authored surfaces, never in the generated `openspec-*` skills, which `openspec update` overwrites.

**D4.** Implementation is a plain ordered checklist, the floor every harness supports. A harness can run tasks in parallel when they write different files and their prerequisites are complete. A task never starts before its prerequisites, and no barrier beyond the prerequisites delays it.

**D5.** The implement phase has no review or simplify pass. Each task's passing scoped tests are the only gate in the phase.

**D6.** One aggregate gate runs once on the cumulative diff: `code-simplifier`, then `code-reviewer`, `security-reviewer`, and `tester` in parallel, then `mise run check`. A Critical or High finding blocks. Fixes go back to an implementation role, and the gate runs again. Implementation never restarts from the beginning.

**D7.** The threat-model reconcile and gate are single-sourced in the OpenSpec schema instructions and run through `openspec/tools/tc.py`. Flow wrappers (slash commands, skills, the workflow recipe) carry no threat-model logic of their own.

**D8.** A hard archive-time threat-model gate is deferred. OpenSpec offers no schema archive hook. The possible hard guarantee is a pre-commit hook on the move into `openspec/changes/archive/`.

**D9.** In-phase test runs (implementers, the `code-simplifier` re-confirm, and `tester`) are scoped to the touched modules. `mise run check` is the one full-suite run. This is safe because a harness that runs tasks in parallel keeps them on different files (D4, D14), and the full gate catches cross-module regressions. Scoping is guidance, and a missed scope wastes tokens only. Mechanical enforcement of scoping is deferred. Full-suite runs everywhere are rejected.

**D10.** The templates carry the forcing functions an author must act on: `Personas served` in `proposal.md`, the 4-hashtag warning and MODIFIED/REMOVED stubs in `spec.md`, `Diagrams: N/A` in `design.md`, the ordering and file-naming note in `tasks.md`, and the `cairn`-shaped `threat-model.tc.json` skeleton. The rules and instructions stay authoritative, and the template is the echo at the point of use. Placing the forcing functions only in rules and instructions is rejected.

**D11.** The threat-status state machine has one source, the `threat-model` agent. The schema carries a summary and a pointer.

**D12.** A pre-archive linter for the MODIFIED-header match and 3-hashtag scenarios that `openspec validate --strict` misses is deferred.

**D13.** `##` headings in `tasks.md` are organizational. They group tasks for readers and do not bound concurrency.

**D14.** A harness that runs tasks concurrently keeps tasks that share a file or a test in one unit of work.

**D15.** In a bugfix, the RED reproduction tasks complete, and their tests are confirmed to fail, before any fix task starts.

**D16.** Rejected: a branch per harness, per-tool branching inside a schema instruction, mechanics in the schema that each harness translates, review inside the implement phase, and a fixed recipe that schedules checklist tasks into parallel implementer slots.

**D17.** On Kiro, the saved `opsx-apply` recipe runs one implement step, the review-loop gate, and the full gate. The gate verdict is read from the adjudicator's captured output, not from a file. Parallel implementation comes from Kiro-generated workflows, and the repository adds no scheduling machinery.

## Retired approach

The wave model planned implementation parallelism before implementation started. The author grouped tasks into file-disjoint waves in a static graph in `tasks.md`, the orchestrator ran one implementer per wave, and a ledger tracked each wave's status. It ended because native harness primitives made the hand-rolled orchestration unnecessary, and because the graph, the ledger, and the leaked harness mechanics cost more than the parallelism returned. Keeping concurrent tasks on different files is now the job of whichever harness runs tasks in parallel (D4, D14), not of a static graph. This section records a rejected direction and gives no guidance.

## Consequences

**Positive**

- No author writes or maintains a dependency graph, and no orchestrator parses one.
- No ledger exists. The checkboxes in `tasks.md` hold the implementation progress.
- One gated entry point exists on Kiro: the saved `opsx-apply` workflow. The former steering alias `.kiro/steering/opsx-apply.md` ran the task loop with no review gate, and it is removed. Every apply through the workflow ends in the aggregate gate.
- The portable surfaces name no harness primitive, so any harness can run the checklist from top to bottom.
- The repository holds no scheduling state.
- The implementer may write the repository-root `pyproject.toml` and `uv.lock`, so a task that adds a dependency needs no other role.

**Negative**

- The saved recipe implements in one step, so a large checklist runs in one session unless the user asks Kiro for a generated parallel workflow.
- A generated workflow is not reviewed in the repository before it runs. Kiro shows its plan for approval, and the same aggregate gate runs on its result.
- Under the amended ADR-0001 rule, the text of a replaced ADR leaves the working tree and stays only in git history. A reader who needs it must look it up in history on purpose.

## Alternatives considered

- **A fixed recipe that schedules checklist tasks into parallel implementer slots** through planner and reconcile steps, a plan file, and a baseline file. Rejected. It rebuilt the task-management machinery that this decision retires, and three review rounds kept finding new edge cases in it.
- **A branch or fork per harness.** Rejected. It duplicates the contract, and the copies drift.
- **Per-tool branching inside a schema instruction.** Rejected. It puts harness mechanics in a portable surface, which the layering rule forbids.
- **Mechanics in the schema that each harness translates.** Rejected. Every harness receives the schema text verbatim, so each one must override mechanics it cannot run.
- **Review after each unit of implementation.** Rejected. It multiplies review cost and cannot see duplication across tasks, which the single simplify pass on the cumulative diff finds.

## References

- `AGENTS.md`, section "Code-change workflow" — the harness-agnostic pipeline.
- `openspec/schemas/cairn/schema.yaml` and `openspec/schemas/bugfix/schema.yaml` — the `tasks` and `apply` instructions and the threat-model reconcile and gate.
- `.kiro/workflows/opsx-apply.workflow.json` — the saved Kiro recipe: the implement step, the aggregate gate loop, and the full gate.
- `openspec/tools/tc.py` — the threat-model `reconcile` and `gate` commands.
- ADR-0001 — the rule that a new ADR restates every decision that still stands and the replaced ADR is deleted.
