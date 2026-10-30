# ADR-0007: `cairn` workflow — per-wave test scoping and templates as the forcing-function surface

## Status

Accepted (2026-06-01)

## Context

A review of the `cairn` opsx workflow looked for runtime-cost and fidelity improvements. This workflow is the spec-authoring + code-change pipeline that `AGENTS.md` documents and `openspec/schemas/cairn/schema.yaml` executes. The flow is structurally sound:

1. Wave parallelism is real (verified against archived `tasks.md` DAGs).
2. The single post-wave review gate is correct ([ADR-0004](0004-single-post-wave-review-gate.md)).
3. Threat-modeling is correctly positioned at propose.

The review surfaced two refinements that are cross-cutting enough to record. Each has a rejected alternative worth preserving.

**1. Test execution.** Exactly one full-suite run is structurally required: `mise run check` at the end of the aggregate gate (the single pre-merge truth). Everything before it *should* be scoped to touched files. This includes each wave implementer's run and the `code-simplifier` re-confirm. But `pyproject.toml` sets `addopts = -n auto`, so a bare `pytest` runs the whole repo, and the prompts said only "run your tests." So the full suite likely ran W+ times per change. At the current suite size (~7-8s, runs inside parallel subagents) this is cheap in wall-clock. But it wastes tokens, because each run's output goes into a subagent's context, and it scales badly. The waves are file-disjoint by construction ([ADR-0005](0005-wave-granularity-as-parallelism-unit.md)). So a wave cannot regress another wave's tests, which makes scoped in-loop runs safe.

**2. Forcing-function location.** The `cairn` schema artifact templates were skeletal. Some constraints make an artifact high-fidelity:

1. the Personas rule
2. the silent-failure 4-hashtag-scenario trap
3. the MODIFIED-block-copy + exact-header convention
4. the threat-model status state machine and DFD

These constraints lived only in `openspec/config.yaml` rules and the `schema.yaml` `instruction:` blocks. An author fills in the *template*. So an author can discover guidance that lives anywhere else only by cross-referencing, and skips it silently under time pressure. This is also the workflow's largest *wall-clock* lever, indirectly. Clearer specs reduce propose-phase threat-model loop-backs and apply rework. Agent round-trips dominate runtime far more than test seconds.

Both refinements change **no** system Requirement and are not a spec capability. So they ship as direct edits to the `cairn` schema customization surface, and this ADR records them, rather than the `/opsx` flow. This follows the [ADR-0001](0001-adopt-architecture-decision-records.md) boundary, with the same placement as ADR-0004/0005.

## Decision

**1. Per-wave and simplifier test runs are scoped; `mise run check` is the one full-suite gate.** The per-wave implementer contract (`cairn` schema `apply` instruction) and the `code-simplifier` agent now run pytest scoped to the modules they touched (e.g. `uv run pytest tests/unit/cairn_<module>/`). They never run a bare repo-wide `pytest`. The post-wave `mise run check` remains the single full-suite, cross-wave, cross-tool (ruff/CDK/validate) pre-merge truth. This is safe because waves are file-disjoint. A wave cannot regress another wave's tests, and the simplifier is behavior-preserving by mandate.

**2. The templates the author fills in carry the forcing-functions.** Guidance an author must act on belongs in `openspec/schemas/cairn/templates/`, not only in `config.yaml` rules / `schema.yaml` instructions. The change realizes this as:

1. a `Personas served` section in `proposal.md`
2. the 4-hashtag silent-failure warning plus MODIFIED/REMOVED stubs in `spec.md`
3. the `Diagrams: N/A` affordance in `design.md`
4. the wave-packaging note in `tasks.md`
5. a `cairn`-schema-shaped `threat-model.tc.json` skeleton (empty image, status state machine, `custom:residualRisk`, `mitigationLinks`, DFD-in-`dataflow.description`) that replaces the generic vendored AWS sample

Rules/instructions stay authoritative. The template is the discoverable echo at the point of authoring.

The same change applies a corollary. In one case, two live surfaces stated the same rule: the threat-status state machine appeared in both the `schema.yaml` `threatmodel` instruction and the `threat-model` agent. The change collapses the schema copy to a compact summary + pointer, which makes the agent the single source.

## Consequences

**Positive**
- Lower token cost per change (scoped in-loop runs) with no loss of safety — the full-suite gate is unchanged.
- Higher artifact fidelity: forcing-functions are visible where the author works. This reduces skipped conventions (silent 4-hashtag failures, missing personas rationale, MODIFIED header drift) and propose-phase loop-backs.
- Single-source-of-truth for the threat-status semantics reduces drift risk.

**Negative**
- Scoping is model-interpreted guidance, not enforced. An implementer could still run a bare `pytest`. The cost of non-adherence is only wasted tokens, and `mise run check` still catches everything, so the failure mode is benign.
- Templates now duplicate a thin summary of rules that remain authoritative in `config.yaml`/`schema.yaml`. Accepted: the duplication is intentional (echo at point of use) and bounded. The rule files stay the normative source.

## Alternatives Considered

- **Keep full-suite runs everywhere (status quo).** Rejected — pure token waste that scales with suite size. The file-disjoint wave invariant already makes scoped runs safe.
- **Enforce scoping mechanically** (orchestrator computes per-wave paths and injects them). Deferred — it adds orchestration complexity for a benign, token-only failure mode. The guidance form suffices at current scale.
- **Leave forcing-functions only in rules/instructions.** Rejected — proven low-discoverability. Authors act on the template. (The diagram rule added in a prior change already followed the echo-in-template pattern this ADR generalizes.)
- **A new pre-archive linter** (`openspec/tools/lint.py`) to *enforce* MODIFIED-header match, wave-DAG well-formedness, and 3-hashtag scenarios that `openspec validate --strict` misses. Deferred to a separate change (captured as a backlog item). It is new machinery, weighed on its own merits, independent of these two refinements.

## References

- ADR-0001 (ADR adoption, and the requirement-change vs no-requirement-change boundary).
- ADR-0004 (single post-wave quality gate) — the **cost axis** of the wave model this ADR's test-scoping decision extends.
- ADR-0005 (wave granularity is the parallelism unit) — establishes the file-disjoint wave invariant that makes scoped per-wave runs safe. It also sets the precedent of encoding process guidance in `tasks.md` templates.
- `openspec/schemas/cairn/schema.yaml` — `apply` instruction (per-wave implementer scoping) and `threatmodel` instruction (status-machine pointer).
- `openspec/schemas/cairn/templates/{proposal,spec,design,tasks,threat-model.tc}.md` / `.json` — forcing-function echoes.
- `.claude/agents/code-simplifier.md` — scoped re-confirm.
- `.claude/agents/threat-model.md` — single source for threat status semantics.
- `openspec/tools/tc.py` — fail-closed hardening (unknown `--shipped` ids, malformed `custom:residualRisk`) shipped alongside, in its own commit.
