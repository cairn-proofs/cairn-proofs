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
2. **`openspec/changes/` — work in progress.** An in-flight change's deltas supersede the canonical specs until `/openspec-archive-change` folds them in. `openspec/changes/archive/` holds the history of completed changes. Read it only to run the archive flow or when the user asks. Current behavior lives in `openspec/specs/`.
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
- `docs/adr/` — Architecture Decision Records (`NNNN-kebab-title.md`, zero-padded) for decisions that change **no** Requirement (deferrals, technology evaluations, cross-cutting standards). An ADR in force is never edited. A new ADR replaces an old one by restating every decision that still stands, and the old ADR is then deleted. ADR numbers are never reused. ADR-0001 records why a replaced ADR is deleted. **Boundary vs the OpenSpec flow:** a decision that changes a Requirement goes through `/openspec-propose` (WHY in `design.md`, WHAT in the spec delta), not an ADR. WHY in the ADR, WHAT in the spec, cross-linked, never duplicated. See `docs/adr/0001-adopt-architecture-decision-records.md`.
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
- **No history in specs, docs, or code comments.** Specs, reference docs, code comments, and docstrings describe the current state. Do not add historical narration, temporal tags, or notes about what changed from an earlier version. Put that in an ADR, the commit message, a README, or a CHANGELOG.
- **Run shell commands from the repository root.** Do not prefix a command with `cd <path> &&`. A `cd` prefix stops the command from matching the approved-command allowlist and causes a permission prompt.

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

### Code-change workflow

Every change to source or infrastructure follows this pipeline. The specialist roles are `code-reviewer`, `security-reviewer`, `tester`, `code-simplifier`, `threat-model`, `spec-writer`, and `implementer`. Each harness defines them under its own agent directory. Each harness also keeps its own execution detail, such as how it starts agents, runs work in parallel, and retries, in its own configuration. This section says what each phase does, not how a harness does it.

1. **Spec gate.** If the change adds or modifies a Requirement, use `/openspec-propose`. For exploratory thinking first, use `/openspec-explore`.

2. **Implement.** An implementation role does this phase. It is a different actor from the session that runs the full gate and the commit, so the actor that writes the code does not also run the final gate. Implementation follows the `openspec-apply-change` skill. The command `openspec instructions apply --change <name> --json` lists the tasks. A task is marked `- [x]` in `tasks.md` only when its behavior is fully implemented. If the command reports `blocked`, stop and report. If it reports `all_done`, go to the aggregate gate. A harness without delegation runs this phase in a separate session from the one that runs the full gate and the commit.

   `tasks.md` is a plain ordered checklist. Tasks are grouped under `##` headings, and each task comes after the tasks it depends on. A harness without concurrency runs the checklist from top to bottom. A harness with concurrency can run tasks at the same time when they write different files and all their prerequisites are complete. A task never starts before its prerequisites are complete.

   Rules for the implement phase:
   - Read the neighboring code first. Match its style, error handling, test layout, and libraries. Do not add a new library or pattern when an existing one does the job.
   - Every code path that a task adds or changes gets a happy-path test and at least one error-path test. Silent failures are the most severe bug class in this repository.
   - Tests ship in the same task as the code they cover. Mock at the AWS boundary (`moto` or `boto3` stubs in Python, `aws-sdk-client-mock` in TypeScript), and never call real AWS. Unit tests mirror the source layout under `tests/unit/cairn_<lib>/`.
   - Run only the tests for the modules you touched, for example `uv run pytest tests/unit/cairn_<module>/` or `mise run cdk-test`. A bare `pytest` runs the whole suite because of `addopts`, so do not run it. `mise run check` in the aggregate gate is the one full-suite run.
   - Before you hand off Python edits, run `uv run ruff check --fix <files>` (safe fixes only, never `--unsafe-fixes`) and `uv run ruff format <files>` on the files you edited.
   - If a task needs a matching infrastructure change, such as a Lambda environment variable, an IAM permission, a resource, or wiring in `cdk/lib/constructs/lambda-functions.ts`, make that change in the same task.
   - Use secure-by-default patterns. Validate producer-asserted fields, keep secrets out of logs, and build shell commands with parameters or escaping.
   - The implement phase has no review or simplify pass. Each task's passing tests are the only gate in this phase.

3. **Aggregate gate.** When implementation is complete, run one quality gate on the cumulative diff, which is all the changes of the change together. The order is:
   - a. `code-simplifier`. This is the one simplify pass. It runs here so that it can find duplication between tasks.
   - b. `code-reviewer`, `security-reviewer`, and `tester` at the same time, on the simplified diff. A Critical or High finding from a reviewer blocks the commit. `tester` fills coverage and edge-case gaps and adds regression tests. A harness without concurrency runs them one after another.
   - c. `mise run check`, the full pre-merge gate. The session that commits runs it.

   To fix findings, give them to an implementation role, then run the gate again until it is clean. Do not restart implementation from the beginning. This pass is the only review and the only simplify for the change. If the change has a `threat-model.tc.json`, the session reconciles and gates it with `openspec/tools/tc.py` (`reconcile`, then `gate`) after `mise run check` passes and before the commit. Then `/openspec-verify-change` and `/openspec-archive-change` find it settled.

4. **Commit.** Only the session that ran the full gate commits, and only when the user asks. Use Conventional Commits, no Co-Authored-By trailer, and named-file `git add` (never `git add -A`). Make one logical commit per change unit, and do not bundle unrelated work.

Skip a step only with explicit user approval. The pipeline is the same for a one-line fix and a multi-Lambda change. Review and test are never skipped. Threat modeling belongs in the propose phase. A threat found after implementation becomes a follow-up OpenSpec change with its own propose-phase threat-model checkpoint.

The rationale for this pipeline, for the single aggregate gate, and for the split between harness-agnostic and per-harness content is in `docs/adr/0021-checklist-floor-and-native-orchestration.md`.
