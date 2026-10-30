# AGENTS.md — Cairn Proofs

Shared operating manual for agent sessions working in this repository. See `README.md` for the human-facing system overview and capability map, and `DEVELOPING.md` for build/test/deploy. A generated documentation knowledge base lives under `docs/` — start at `docs/index.md`.

## Overview

Cairn Proofs is a tamper-evident artifact store: producers push artifacts in OCI bundles to Amazon ECR; an EventBridge-driven, five-stage Lambda pipeline validates, signs, archives (S3 + optional WORM), indexes (DynamoDB), and tags each bundle. The CDK app is **TypeScript**; Lambdas, shared libraries, and the CLI are **Python 3.12** in a `uv` workspace. The load-bearing invariant is that artifact integrity is anchored in the data plane (digest, signature, WORM receipt) and is independent of whoever operates the substrate; verification never reads the index.

## Where to start (subsystem map)

| Area | Path | What lives here / entry point |
|---|---|---|
| Infrastructure | `cdk/` | One stack (`ArtifactStoreStack`, `lib/artifact-store-stack.ts`); per-resource constructs in `lib/constructs/`; `bin/artifact-store.ts` |
| Pipeline Lambdas | `lambdas/<name>/` | One handler per package (`handler.py` → `lambda_handler`): `validate_bundle`, `sign_bundle`, `archive_to_s3`, `update_metadata`, `mint_tag` |
| Shared libraries | `libs/<name>/` | `oci`, `validation`, `oscal`, `metadata`, `signing`, `observability` (imported by Lambdas + CLI) |
| CLI + TUI | `cli/cairn_cli/` | `cairn` verbs (`push`/`pull`/`verify`/`query`/`sign`/`alias`/`config`) via `main.py`; read-only TUI in `cli/cairn_cli/tui/` |
| Spec source of truth | `openspec/specs/` | One `spec.md` per capability (see `README.md` for the current list); tools in `openspec/tools/` |
| Engineering reference | `docs/` | `architecture-as-deployed.md`, `data-model.md`, `adr/`, and the generated set indexed by `docs/index.md` |
| Notation layer | `lambda-layers/notation/` | arm64 Notation + AWS Signer layer build |
| Tests | `tests/` | `tests/unit/` mirrors `libs/<lib>/`; `tests/property/` (hypothesis); `tests/integration/` (needs a deployed stack) |

Dependency direction is one-way: `cdk/` → Lambda bundles built from `lambdas/` → which import `libs/`. `libs/` never imports from `lambdas/` or `cdk/`.

## Repo-specific patterns worth knowing

Non-obvious things an agent would otherwise miss (authoritative detail is in the Custom Instructions below, in `openspec/specs/`, or in `docs/`):

- **Toolchain is 100% mise-managed** (`mise.toml`) — runtimes and every tool. `mise install` is the whole setup story; add new tools to `mise.toml`, not per-machine.
- **`mise run check` is the pre-merge gate** — it fans out ruff, pytest (parallel), openspec validation + spec-id lint, CDK typecheck + jest, semgrep, and gitleaks. `cdk-synth` is intentionally not in it.
- **Tests run parallel by default** (`pytest -n auto`); per-invocation state must live in `contextvars`/OTEL baggage, never module-level mutables (a serial-only-passing test is a test bug).
- **CloudFormation Exports are prohibited** — cross-stack values flow through SSM Parameter Store.
- **One Lambda handler per package**; Lambda code never imports `aws-cdk-lib`. Bundling is a pure `uv` arm64 cross-install — no container daemon on the routine deploy path.
- **The metadata index is a rebuildable projection**, never the root of trust; `cairn verify` re-derives its verdict from ECR content + referrers + a trust policy and never reads DynamoDB.
- **OCI tag grammar and artifact-kind vocabulary are closed and fail-closed** (`libs/oci/categories.py`); the CLI does no client-side tag composition — the pipeline mints the canonical tag.
- **Spec-driven change flow** — requirement changes go through the `/openspec-*` OpenSpec workflows; direct edits to `openspec/specs/` outside that flow are not canonical.

## Documentation knowledge base

The generated documentation set is navigation reference, not canonical — entry point `docs/index.md`, with the individual docs under `docs/summary/`. Source-of-truth order: behavioral contracts → `openspec/specs/`; as-deployed infra → `docs/architecture-as-deployed.md`; persisted DynamoDB schema → `docs/data-model.md`; architecturally-significant decisions → `docs/adr/`. Individual generated docs under `docs/summary/`: `codebase_info.md`, `architecture.md`, `components.md`, `interfaces.md`, `data_models.md`, `workflows.md`, `dependencies.md`, `review_notes.md`.

## Custom Instructions
<!-- This section is for human and agent-maintained operational knowledge.
     Add repo-specific conventions, gotchas, and workflow rules here.
     This section is preserved exactly as-is when re-running codebase-summary. -->

### Spec authoring model

OpenSpec is the single source of truth for system requirements, across three surfaces:

1. **`openspec/specs/` — current state.** The capability specs describe the system as built. Requirements carry no phase annotations.
2. **`openspec/changes/` — work in progress.** An in-flight change's deltas supersede the canonical specs until `/openspec-archive-change` folds them in.
3. **GitHub Issues — work to be done.** Every deferred or future requirement lives as a GitHub issue on the public repo until it graduates into a change.

Every requirement change goes through the OpenSpec workflows — `/openspec-*` skill commands under `.kiro/skills/openspec-*`, wrapping the `openspec` CLI. Mutating commands (`openspec new change`, `openspec archive`) go through the `/openspec-propose` and `/openspec-archive-change` flows. Read-only inspections (`openspec list`, `status`, `instructions`, `validate --specs --strict`) are fair game for any session — see "OpenSpec CLI".

- `/openspec-explore` — think through ideas and clarify requirements before scaffolding.
- `/openspec-propose` — scaffold `openspec/changes/<name>/` with proposal, design, spec deltas, and tasks.
- `/openspec-apply-change` — walk the change's `tasks.md`, implementing each task and updating artifacts.
- `/openspec-archive-change` — validate and fold the spec delta into `openspec/specs/`.

Do not edit `openspec/specs/` outside this flow; such edits are not canonical.

### docs/ is engineering reference, not spec

The `docs/` directory holds engineering reference outside the OpenSpec artifact taxonomy:

- `docs/architecture-as-deployed.md` — current-state diagram of the deployed stack, derived from the CDK app (the infrastructure view; `openspec/specs/` is the behavioral view). Edit it directly when the deployed architecture changes.
- `docs/adr/` — Architecture Decision Records (`NNNN-kebab-title.md`, zero-padded) for decisions that change **no** Requirement (deferrals, technology evaluations, cross-cutting standards). Append-only and immutable once Accepted; a later ADR supersedes an earlier one (`Status: Superseded by ADR-NNNN`) rather than editing it. **Boundary vs the OpenSpec flow:** a decision that changes a Requirement goes through `/openspec-propose` (WHY in `design.md`, WHAT in the spec delta), not an ADR. WHY in the ADR, WHAT in the spec, cross-linked, never duplicated. See `docs/adr/0001-adopt-architecture-decision-records.md`.
- **GitHub Issues** — the home for deferred work (user stories + acceptance criteria), upstream of and non-canonical to the OpenSpec flow. An issue graduates into a change via `/openspec-propose`, which seeds the change from the issue (cite the issue in the proposal); `/openspec-archive-change` closes it with `Closes #N`. Defer work by filing an issue, never by reviving a curated backlog doc.

### WORM_Enabled is opt-in per deployment

A per-deployment `WORM_Enabled` boolean (default `false`) gates the Artifact Store's WORM (Object Lock) behavior. The S3 archive workflow runs identically either way; `WORM_Enabled=true` adds Object Lock Compliance mode at bucket creation plus per-object retention headers. **This is irreversible** — S3 Object Lock must be enabled at bucket creation and cannot be added later. `WORM_Enabled=false` is non-production only; production must set `WORM_Enabled=true`. Canonical behavior: `openspec/specs/worm-archive/spec.md`.

### Toolchain

[mise](https://mise.jdx.dev/) manages the full toolchain via `mise.toml` — language runtimes (Node 24, Python 3.12) and project tools (`uv`, `aws` CLI, `pre-commit`, `openspec`). Run `mise install` once; everything pinned in `mise.toml` lands on PATH via `mise activate`. See `README.md` "Local development".

Add new project tools to `mise.toml`, not per-machine `brew install` / `curl | sh`. Mise's backend coverage is broad — `npm:` for npm CLIs (e.g. `"npm:@fission-ai/openspec"`), the `aws-cli` plugin, and `ubi`/`aqua`/`asdf` for most else. Fall back to per-machine installation only when mise has no working backend.

When `mise.toml` and another pin file (e.g. `.python-version`) drift, `mise.toml` wins.

### Build & test commands

Pre-merge gate: **`mise run check`** — ruff lint + format-check, pytest (parallel via xdist), `openspec validate --specs --strict`, CDK TypeScript typecheck, and CDK jest tests. It fans out in parallel where independent.

Individual subtasks for narrower iteration:

- `mise run lint` / `mise run fmt` / `mise run fmt-check` — ruff (Python)
- `mise run test` — pytest under `-n auto` (xdist default; pass `-n 0` for interactive pdb)
- `mise run spec-validate` — openspec strict validation
- `mise run cdk-typecheck` / `mise run cdk-test` — CDK gates
- `mise run cdk-synth` — CDK synth (on-demand; intentionally NOT in `check`)

Narrower test invocations (inherit the `-n auto` default unless overridden):

- Single test file: `uv run pytest tests/unit/cairn_oscal/test_dispatch.py`
- Single test: `uv run pytest tests/unit/cairn_oscal/test_dispatch.py::test_parse_oscal_layer_happy_path`
- Interactive debugging: `uv run pytest -n 0 --pdb tests/...`

Lambda + layer builds (called by `deploy.sh`; safe to run standalone):

- `./cdk/scripts/build-lambdas.sh` — bundles all pipeline Lambdas under `lambda-bundles/`
- `./lambda-layers/notation/build.sh` — builds the Notation arm64 layer with SHA-256 + GPG verification

Deploy: **`./cdk/scripts/deploy.sh`** runs the Notation layer build + Lambda bundling + CloudTrail preflight + `cdk deploy --all`. The target AWS account and region are sourced from the mise-loaded root `.env` (`CAIRN_ALPHA_ACCOUNT` / `CAIRN_ALPHA_REGION`, e.g. `123456789012` / `us-east-2`), not a fixed source pin; `regionCode` is derived from the region via the committed `cdk/lib/region-codes.ts` registry. Data-residency control now lives in that committed region-code registry membership plus the retained single-region pinning (threat T6), not a spec deploy-location allowlist. Override the CloudTrail preflight with `ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1` (non-production only — records the gap in the stack's `AuditCoverageStatus` output).

### Repository layout

```
cdk/              TypeScript CDK app (one stack: cairnproofs4af-cmh-alpha)
  lib/constructs/   per-resource constructs — encryption, repositories, archive,
                    metadata-table, controls-table, dead-letter-queues, event-bus,
                    log-groups, alarms, notifications, signing, iam-roles,
                    lambda-functions
  scripts/          deploy.sh + build-lambdas.sh

lambdas/          5 pipeline Lambda packages (uv workspace members)
  validate_bundle/ sign_bundle/ archive_to_s3/ update_metadata/ mint_tag/

libs/             6 shared Python libraries (uv workspace members)
  oci/             OCI manifest + Config_Blob + digest helpers
  validation/      [INGEST-016/017] bundle validation + cross-checks
  oscal/           OSCAL adapter on top of the oscal-bindings PyPI package
  metadata/        DynamoDB facades (MetadataIndex + ControlsIndex)
  signing/         Notation CLI wrapper + SSM timestamp-provider read
  observability/   Trace context extraction, MDC, structured JSON formatter,
                   put_metric — slated for OTEL realignment by the
                   observability-otel-alignment change

cli/              `cairn` CLI — push/pull/verify/query (cairn_cli/) plus a
                  read-only operator TUI (`cairn tui`, cairn_cli/tui/)

lambda-layers/notation/  Notation CLI v1.3.2 + AWS Signer plugin v1.0.2292
                         (arm64) layer build with SHA-256 + GPG verification

tests/unit/       pytest unit tests per lib, Lambda, and the CLI/TUI
                  (tests/unit/cairn_cli/tui/ covers the read-only TUI).

openspec/         Spec source of truth — capabilities under specs/,
                  in-flight changes under changes/, archived under
                  changes/archive/

docs/             Engineering reference (see "docs/ is engineering reference"
                  section above for which files are authoritative vs superseded)

runbooks/         Operational docs (notation-layer.md, deploy-alpha.md,
                  troubleshooting.md)
```

Dependency direction is one-way: `cdk/` consumes Lambda bundles built from `lambdas/`, which import `libs/`. `libs/` never imports from `lambdas/` or `cdk/`. Tests under `tests/unit/cairn_<lib>/` mirror the `libs/<lib>/` layout.

External dependency worth knowing: **`oscal-bindings`** ([PyPI](https://pypi.org/project/oscal-bindings/), source `github.com/cairn-proofs/oscal-bindings-python`) ships the OSCAL Pydantic v2 models; `libs/oscal/` is a thin adapter over it and pins it exactly (`libs/oscal/pyproject.toml`). The models are strict (`extra='forbid'`), so bumping the pin changes what the validate path accepts — treat it as a behavior change. File upstream feature requests as issues on that repo.

### OpenSpec CLI

The `openspec` CLI is on the contributor's PATH (a required tool per `README.md` "Local development"). All sessions — including the specialist subagents in `.kiro/agents/` — should invoke it directly for read-only inspection rather than guessing change state from filesystem layout:

- `openspec list --json` — enumerate active changes.
- `openspec status --change <name> --json` — artifact/task state for one change.
- `openspec instructions apply --change <name> --json` — the canonical task list for an in-flight change, which `code-reviewer`, `tester`, and the main session ground their work in.
- `openspec validate --specs --strict` — re-run the pre-commit validation ad-hoc.

When a change is in flight, the spec deltas at `openspec/changes/<name>/specs/<capability>/spec.md` supersede the canonical `openspec/specs/<capability>/spec.md` until `/openspec-archive-change` folds them in; reviewers and testers consult the delta first. Mutating invocations (`openspec new change`, `openspec archive`) run through the `/openspec-*` skill commands — do not call them directly.

### Conventions

- **Commit format:** Conventional Commits (`docs:`, `feat:`, `fix:`, `refactor:`, …).
- **No Co-Authored-By trailer** in commit messages.
- **Linear history — no merge commits.** Integrate with rebase or fast-forward only (`git rebase`, `git merge --ff-only`). Squash noisy interim commits (WIP, "fix typo", review churn) into one Conventional Commit per logical change unit so `main` stays clean and bisectable.
- **Single source of truth:** each fact lives in one canonical place across `openspec/specs/`, `docs/`, and this agent-context file (`AGENTS.md`); others reference it.
- **OpenSpec changes must validate cleanly before committing.** The `/openspec-apply-change` and `/openspec-archive-change` flows run validation; `openspec validate --strict` re-runs it ad-hoc.
- **Scenario format:** exactly 4 hashtags (`#### Scenario:`); 3 hashtags fail validation silently.

#### CDK conventions

- **CloudFormation Exports are prohibited.** `CfnOutput` without `exportName` is allowed (display-only, queryable via `describe-stacks`, no cross-stack coupling). `CfnOutput` WITH `exportName` creates an Export that ratchets the producing stack — it cannot be deleted or renamed until every consumer un-imports. Share cross-stack values through SSM Parameter Store instead: producers write via `aws_ssm.StringParameter`, consumers read via `StringParameter.fromStringParameterName`. SSM is loose-coupled (read-on-demand, no deletion ratchet) and surfaces to CLIs, scripts, sibling stacks, and operators via the AWS Console.
- **One Lambda handler per package** under `lambdas/<name>/` (a uv workspace member). Its CDK construct lives in `cdk/lib/constructs/lambda-functions.ts`; Lambda code never imports `aws-cdk-lib`.
- **Stack assets pre-build locally; no mandatory container runtime on the routine build path.** `cdk/scripts/build-lambdas.sh` produces `lambda-bundles/<name>/` via a pure `uv` cross-install (`uv pip install --python-platform aarch64-manylinux_2_17` fetches Linux/arm64 wheels, so native deps like `pydantic_core` need no in-container bundling); `cdk/scripts/deploy.sh` runs it before `cdk deploy`. The load-bearing principle: `mise install` is the whole setup story, so `deploy.sh` must run with PATH tools alone — no routine-path asset may *require* a container daemon/VM (Docker, Finch). A `DockerImageAsset` / container-image Lambda is still permitted where warranted (it lands in CDK bootstrap's `cdk-hnb659fds-container-assets-*` ECR repo, never the `artifact-*` evidence repos, and a contributor opts in via `CDK_DOCKER=finch`); it just must not become a routine-deploy prerequisite. See `docs/adr/0011-defer-lambda-packaging-consolidation.md` (including why a shared `uv`-built layer, not a container image, is the preferred consolidation path).

#### Concurrency-safe ambient state

- **Tests SHALL be safe under process-parallel execution.** `mise run check`'s `test` subtask runs `pytest -n auto` (pytest-xdist) via `addopts` in the root `pyproject.toml`. A test that passes serially (`-n 0`) but fails in parallel is a test bug — shared module-level state, order dependency, or a fixed-path write. Fix the test, not the runner. Canonical Requirement: `openspec/specs/toolchain/spec.md` *Tests Safe Under Parallel Execution*.
- **Per-invocation ambient state** (logging MDC, request context, OpenTelemetry baggage, tracing fields — anything cleared and repopulated per request) lives in `contextvars.ContextVar` (Python), OpenTelemetry baggage (preferred when the ADOT layer is loaded — baggage is ContextVar-backed AND propagates across W3C service boundaries via the `Baggage` header), or `AsyncLocalStorage` (Node). Never use module-level mutable state (`_mdc = {}`, `threading.local`, project-local context dicts) for per-invocation data — it leaks across parallel tests and breaks under async event loops.
- **Read-only configuration captured at import time** (`STAGE = os.environ.get("STAGE")`, `_LOG = logging.getLogger(__name__)`, module-level constants) is a permitted module global — immutable after process start, so it cannot leak per-invocation state.
- **Interactive debugging** opts out of parallelism via `pytest -n 0` (or `--pdb`); `-n auto` returns on the next invocation.
- **Bash build scripts** touching fixed paths (`cdk/scripts/build-lambdas.sh`, `lambda-layers/notation/build.sh`) take an exclusive `flock` around their write step so concurrent invocations serialize instead of racing. Lock files live under a per-user `${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cairn-build-$(id -u)/` directory (created 0700) to avoid the `/tmp`-symlink attack class. **macOS contributors: `brew install flock`** — Apple doesn't ship it and mise has no backend; without it the scripts warn on stderr and rely on downstream checksum verification to catch concurrency-induced corruption.
- **The wave-status ledger** (`openspec/tools/wave_ledger.py`) writes one `wave-<id>.json` per wave under a per-user `${XDG_RUNTIME_DIR:-<tempdir>}/cairn-opsx-$(id -u)/<change>/` directory (created 0700). It is ephemeral run state, never committed. Per-wave files keep concurrent writes file-disjoint (no shared-file race, no `flock`); each write is atomic via temp-file + `os.replace`. Change name and wave id are validated as safe path components (no `/`, `.`, `..`).

### Code-change workflow

Every change to source or infrastructure follows this pipeline. Specialist roles — `code-reviewer`, `security-reviewer`, `tester`, `code-simplifier`, `threat-model`, `spec-writer`, `implementer` — are defined per harness under its agent directory; invoke them through whatever delegation mechanism your harness provides. The main session is the **orchestrator**: it delegates implementation to wave sub-agents, then runs a single simplify/review/test gate itself after all waves complete. Sub-agents cannot spawn sub-agents, so those passes live one level up (step 3).

1. **Spec gate.** If the change adds or modifies a Requirement, use `/openspec-propose`. For exploratory thinking first, use `/openspec-explore`.

2. **Implement (wave-orchestrated).** `/openspec-apply-change` puts the main session in the orchestrator role. Parse the Task Dependency Graph at the bottom of `tasks.md` (a DAG keyed on `dependsOn`) and dispatch each wave to an implementer sub-agent. The orchestrator MUST NOT implement wave tasks directly.

   A wave runs once its `dependsOn` prerequisites are met; independent waves run concurrently. How concurrency is scheduled — continuously, or a ready batch at a time — is harness-specific. As each wave completes, mark its tasks `- [x]` in `tasks.md`.

   Parallelism comes from wave granularity, not per-task spawning — a single task isn't independently ownable. A wave is the finest set of tasks that stays file-disjoint and test-self-contained; a `##` task-group heading is organizational, not a wave boundary. Each wave sub-agent implements its tasks and writes and runs its own tests to green, then reports the files it touched. Tests ship in the same wave as the code they cover — this keeps the wave's test run meaningful and stops a broken wave from poisoning later ones; it is the only correctness gate inside the wave loop and is never deferred.

   There is NO per-wave review or simplify. A wave sub-agent cannot spawn sub-agents, so a wave told to "invoke `code-reviewer`" would degrade to a self-review — not the bar. Review is orchestrator-driven and runs once, in step 3.

3. **Aggregate gate.** After all waves complete, the orchestrator runs the single quality gate on the **cumulative** diff, in order:
   - a. `code-simplifier` — the one simplify pass, here so it catches duplication *between* waves that no per-wave pass could.
   - b. `code-reviewer` + `security-reviewer` + `tester` — run in parallel on the simplified diff. Reviewers must report no Critical/High; `tester` fills coverage and edge-case gaps and adds regression tests.
   - c. `mise run check` — the full pre-merge gate (ruff, full pytest, `openspec validate --specs --strict`, CDK typecheck + jest), run by the orchestrator on the main session (repo-wide, single-writer).

   Fix findings by **re-dispatching** the affected work — re-run a reviewer-flagged wave with the findings as feedback, or fix a pure cross-wave seam issue directly — then re-run the gate until clean. Recover a failed wave by re-running only that wave, never the whole DAG. This single pass is the only review and the only simplify for the change; it catches cross-wave seam issues (e.g. a new caller inconsistent with an existing API) that no per-wave pass could. For a change carrying a `threat-model.tc.json`, a completed apply reconciles and gates it via `openspec/tools/tc.py` before commit, so `/openspec-verify-change` and `/openspec-archive-change` find it settled.

4. **Commit** (main session only). Conventional Commits, no Co-Authored-By, named-file `git add` (never `git add -A`). One logical commit per change unit; do not bundle unrelated work.

Skip steps only with explicit user approval. The pipeline is identical for a one-line fix and a multi-Lambda change; review and test are never skipped. After-the-fact threat modeling — finding new threats after implementation — is wasted work; route it as a follow-up OpenSpec change with its own propose-phase threat-model checkpoint.

The concrete spawn / parallelism / re-dispatch primitives are harness-specific and live in your harness's orchestrator agent definition, which is authoritative for how your harness executes this workflow. Rationale for the single post-wave gate and for wave granularity as the parallelism unit: `docs/adr/0004-single-post-wave-review-gate.md` and `docs/adr/0005-wave-granularity-as-parallelism-unit.md`.
