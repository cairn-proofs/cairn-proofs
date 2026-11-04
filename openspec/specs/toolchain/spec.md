# toolchain Specification

## Purpose
How the Cairn project is built, verified, and shipped: language and tool pins via mise, the uv Python workspace, hatchling as the PEP 517 build backend, ruff lint/format, pre-commit hooks for fast inner-loop verification, and the `mise run check` aggregator that gates merges. `toolchain` covers contributor-side build and verification; `platform-constraints` covers the deployed Provenance_System's runtime and deployment constraints.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements
### Requirement: [TOOLS-001] Toolchain Pinned and Bootstrapped via mise

THE project SHALL pin every language runtime and required project tool in `mise.toml` at the repo root, so that running `mise install` produces a reproducible developer environment. Pinned tools include Node, Python, `uv`, the AWS CLI, `pre-commit`, and `openspec`. When `mise.toml` and any other pin file (e.g., `.python-version`) disagree, `mise.toml` is authoritative. New project tools SHALL be added to `mise.toml` rather than installed per-machine via `brew`, `curl | sh`, or similar.

#### Scenario: [TOOLS-001.01] Fresh checkout reaches a working toolchain via mise install

- **WHEN** a new contributor clones the repository and runs `mise install` from the repo root
- **THEN** every tool pinned in `mise.toml` is installed at the pinned version and placed on PATH via `mise activate` (or shell integration), with no further per-machine setup required for the toolchain layer

#### Scenario: [TOOLS-001.02] mise.toml is authoritative over redundant pin files

- **WHEN** `mise.toml` pins a language version and another file (`.python-version`, `.nvmrc`, etc.) pins a different version of the same language
- **THEN** the contributor and CI both treat `mise.toml` as authoritative and the divergent file is treated as drift to be corrected

#### Scenario: [TOOLS-001.03] New tools are added to mise.toml rather than per-machine installs

- **WHEN** the project adopts a new developer-facing tool
- **THEN** the tool is added to `mise.toml` using an appropriate mise backend (e.g., `npm:` for npm-packaged CLIs, `aws-cli` plugin, `ubi`/`aqua`/`asdf`) and contributors install it via `mise install`, not via per-machine package managers

### Requirement: [TOOLS-002] Python Workspace Managed by uv

THE project SHALL use [uv](https://docs.astral.sh/uv/) as the Python dependency manager, lockfile holder, and workspace driver. The root `pyproject.toml` declares a uv workspace via `[tool.uv.workspace]` whose members are `cli/`, every directory under `lambdas/`, and every directory under `libs/`. The root package is marked `package = false` — a workspace shell, not a distributable package. A single `uv.lock` at the repo root resolves all members. Cross-workspace dependencies SHALL be declared via `[tool.uv.sources] = { workspace = true }` so editable installs into the root `.venv` work without external publication.

#### Scenario: [TOOLS-002.01] uv sync installs all workspace members editable

- **WHEN** a contributor runs `uv sync` from the repo root
- **THEN** every workspace member listed in `[tool.uv.workspace.members]` is installed editably into `.venv` along with all transitive dependencies, and the resolution matches `uv.lock` exactly

#### Scenario: [TOOLS-002.02] Cross-workspace dependencies resolve to workspace members

- **WHEN** one workspace member depends on another (e.g., `cairn-validation` depends on `cairn-oci`)
- **THEN** the dependency is declared in `[project.dependencies]` and shadowed in `[tool.uv.sources]` with `{ workspace = true }`, so `uv sync` installs the depended-on member from the workspace rather than from an index

#### Scenario: [TOOLS-002.03] Root package is not a distributable

- **WHEN** any tool inspects the root `pyproject.toml`
- **THEN** `[tool.uv] package = false` indicates the root is a workspace shell, and no wheel or sdist is produced from the root directory

### Requirement: [TOOLS-003] Hatchling as PEP 517 Build Backend for Distributable Subpackages

EACH distributable Python subpackage in the workspace (the CLI and every library under `libs/`) SHALL use [hatchling](https://hatch.pypa.io/) as its PEP 517 build backend, declared via `[build-system] requires = ["hatchling"]` and `build-backend = "hatchling.build"`. Hatchling is the build backend only; project-level orchestration (envs, scripts, tests) is uv's job and SHALL NOT be migrated to `hatch`'s project-manager facets.

#### Scenario: [TOOLS-003.01] Each distributable subpackage declares hatchling as build-system

- **WHEN** any tool reads the `pyproject.toml` of a distributable workspace member (`cli/`, `libs/*/`)
- **THEN** the `[build-system]` table specifies `requires = ["hatchling"]` and `build-backend = "hatchling.build"`, and `[tool.hatch.build.targets.wheel].packages` names the source package directory

#### Scenario: [TOOLS-003.02] Project-manager responsibilities stay with uv

- **WHEN** project-manager concerns arise (env management, dep resolution, lock files, workspace cross-references, ad-hoc command running)
- **THEN** they are handled by `uv` invocations, not by `hatch env` / `hatch run` / `hatch-pip-compile`; hatch's role is strictly the PEP 517 build backend

### Requirement: [TOOLS-004] Ruff as Python Linter and Formatter

THE project SHALL use [ruff](https://docs.astral.sh/ruff/) as the single Python linter and formatter, configured in the root `pyproject.toml` under `[tool.ruff]`. The configuration pins `target-version = "py312"`, `line-length = 100`, the lint rule selection `["E", "F", "I", "W", "B", "S", "UP", "RUF"]`, and a format style of double-quote string literals with space indentation. The `S` group is [`flake8-bandit`](https://docs.astral.sh/ruff/rules/#flake8-bandit-s), giving Ruff a Bandit-equivalent Python security-linting mandate alongside its style and correctness rules. Test code SHALL be exempted from the assert-statement security rule (`S101`) via `[tool.ruff.lint.per-file-ignores]` scoped to test paths, because `assert` is pytest's idiomatic assertion mechanism; the exemption SHALL be scoped to test paths rather than disabling the rule globally. No other Python linter (flake8, pylint, standalone bandit) or formatter (black, autopep8, yapf) SHALL be configured or invoked — Bandit-class security coverage comes through Ruff's `S` group, not a separate tool.

#### Scenario: [TOOLS-004.01] ruff check is the canonical lint invocation

- **WHEN** a contributor lints Python code in the repo
- **THEN** `uv run ruff check.` (or its `mise run lint` wrapper) is the invocation that reports lint findings, using the rule selection in `[tool.ruff.lint.select]` — including the `flake8-bandit` (`S`) security rules

#### Scenario: [TOOLS-004.02] ruff format is the canonical format invocation

- **WHEN** a contributor formats Python code in the repo
- **THEN** `uv run ruff format.` (or its `mise run fmt` wrapper) applies the format style declared in `[tool.ruff.format]`, and `--check` is used for read-only verification

#### Scenario: [TOOLS-004.03] flake8-bandit security rules run on production code but assert is allowed in tests

- **WHEN** `ruff check` runs over the workspace
- **THEN** the `S` (`flake8-bandit`) rules are enforced on production code under `cli/`, `lambdas/`, and `libs/`, AND test paths are exempted from `S101` (assert) via `[tool.ruff.lint.per-file-ignores]`, so pytest's idiomatic `assert` usage does not trip the security rule while production code remains covered

### Requirement: [TOOLS-005] Pre-Commit Hooks for Fast Inner-Loop Verification

THE project SHALL configure [pre-commit](https://pre-commit.com/) hooks in `.pre-commit-config.yaml` that run on every `git commit`. Pre-commit hooks SHALL be limited to the fast checks — `ruff --fix`, `ruff-format`, and `openspec validate --specs`. Pre-commit SHALL NOT include slow checks such as pytest, CDK type-check, or CDK tests; `mise run check` gates those instead. This keeps `git commit` latency low so contributors do not develop a `--no-verify` habit.

#### Scenario: [TOOLS-005.01] git commit invokes ruff and openspec validate

- **WHILE** pre-commit hooks are installed locally
- **WHEN** a contributor runs `git commit`
- **THEN** ruff (lint + format) and `openspec validate --specs` run against staged files, and the commit is blocked if any hook fails

#### Scenario: [TOOLS-005.02] pytest is NOT in pre-commit

- **WHEN** any party considers adding pytest, CDK type-check, or CDK tests to `.pre-commit-config.yaml`
- **THEN** the change is rejected on the grounds that slow checks belong in `mise run check`, not in the per-commit gate

### Requirement: [TOOLS-006] Single Pre-Merge Verification Command

THE project SHALL provide a single command — `mise run check` — that runs the pre-merge verification suite covering Python lint, Python format-check, Python tests, OpenSpec spec validation, CDK TypeScript type-check, CDK tests, and static application security testing (SAST). The aggregator SHALL be defined in `mise.toml` as a task whose `depends` field lists the subtasks, so mise's task runner fans them out in parallel where independent and reports each subtask's status separately. Contributors invoke `mise run check` before pushing and CI invokes it pre-merge.

#### Scenario: [TOOLS-006.01] mise run check exercises every fast pre-merge gate

- **WHEN** a contributor invokes `mise run check`
- **THEN** the following subtasks execute (in parallel where independent): `lint` (ruff check, including the `flake8-bandit` security rules), `fmt-check` (ruff format --check), `test` (pytest), `spec-validate` (openspec validate --specs), `cdk-typecheck` (tsc via npm run build in `cdk/`), `cdk-test` (jest via npm test in `cdk/`), and `semgrep` (SAST over the Python workspace and the TypeScript CDK app)

#### Scenario: [TOOLS-006.02] A single subtask failure fails the aggregate

- **WHEN** any subtask of `check` exits non-zero
- **THEN** the `mise run check` invocation as a whole exits non-zero, and the failing subtask's output is preserved in the report so the contributor can see which gate failed

#### Scenario: [TOOLS-006.03] cdk-install is a transitive dependency of CDK subtasks

- **WHEN** `cdk-typecheck` or `cdk-test` runs
- **THEN** mise resolves their `depends` on `cdk-install`, which runs `npm ci` in `cdk/` with mtime-based caching keyed on `cdk/package.json` and `cdk/package-lock.json`, so `node_modules/` is always populated and `cdk-install` is a no-op on warm runs (re-runs only when one of those two files changes)

### Requirement: [TOOLS-007] CDK Synthesis Available as Standalone Task, Not in Check

THE project SHALL define a `cdk-synth` task in `mise.toml` that invokes `npx cdk synth --quiet` in `cdk/`. The `cdk-synth` task SHALL NOT be listed in `check.depends`, and SHALL NOT declare `sources`/`outputs` incremental caching. CDK asset bundling makes `cdk synth` depend transitively on the Python source under `lambdas/` and `libs/` (pre-built via `cdk/scripts/build-lambdas.sh`); declaring `sources` correctly requires per-construct knowledge of which paths are bundled, and a stale-cache failure mode is hostile, so `cdk-synth` runs un-cached on demand.

#### Scenario: [TOOLS-007.01] mise run cdk-synth synthesizes the CDK app

- **WHEN** a contributor invokes `mise run cdk-synth`
- **THEN** the task changes into `cdk/`, runs `npx cdk synth --quiet`, and produces synthesized CloudFormation templates under `cdk/cdk.out/`

#### Scenario: [TOOLS-007.02] cdk-synth is NOT executed by mise run check

- **WHEN** a contributor invokes `mise run check`
- **THEN** the subtasks executed do NOT include `cdk-synth`; the synth gate is invoked separately or in dedicated CI workflow

#### Scenario: [TOOLS-007.03] cdk-synth has no sources/outputs caching

- **WHEN** any tool inspects the `cdk-synth` task definition in `mise.toml`
- **THEN** the task has no `sources` or `outputs` fields declared, so every invocation re-runs synth from scratch and cannot silently skip on stale inputs

### Requirement: [TOOLS-008] Tests Safe Under Parallel Execution

THE project's automated test suites — pytest under `tests/` and jest under `cdk/test/` — SHALL be safe to execute in parallel at the process level. The `test` subtask of `mise run check` SHALL invoke pytest with `-n auto` (via [pytest-xdist](https://pytest-xdist.readthedocs.io/)) by default, fanning the suite out across worker processes equal to the CPU count. Jest SHALL continue to run test files in parallel worker processes (its default). A test that fails under parallel execution but passes serially is a bug in the test (shared state, order dependency, fixed-path filesystem write, module-level mutable state) — the fix lives in the test or the library code it exercises, NEVER by reverting to serial execution. Per-invocation ambient state (logging MDC, request context, OpenTelemetry baggage, anything cleared and re-populated per request) SHALL be stored in `contextvars.ContextVar` (Python), OpenTelemetry baggage (itself ContextVar-backed), or `AsyncLocalStorage` (Node) — never in module-level mutable storage. Read-only configuration captured at import time (e.g., `STAGE = os.environ.get("STAGE")`) is permitted as a module global because it is immutable after process start.

#### Scenario: [TOOLS-008.01] pytest runs in parallel by default via addopts

- **WHEN** any contributor or CI invocation runs `uv run pytest` (or `mise run test`) without explicit worker arguments
- **THEN** pytest reads `addopts = "-n auto"` from `[tool.pytest.ini_options]` in the root `pyproject.toml` and distributes the test suite across xdist worker processes equal to the available CPU count, with no further configuration required

#### Scenario: [TOOLS-008.02] A parallel-only failure is a test bug

- **WHEN** a test passes under `uv run pytest -n 0` (serial) but fails under `uv run pytest` (parallel via the addopts default)
- **THEN** the test is treated as buggy — likely cause is shared state via module globals, order dependency on another test in the same file, or fixed-path filesystem write — and the fix lands in the test code or the library code it exercises, not by reverting to serial execution

#### Scenario: [TOOLS-008.03] Per-invocation ambient state uses ContextVar, never module globals

- **WHEN** a library or handler needs to hold per-invocation ambient state that any deeper code in the call graph reads (e.g., logging MDC, trace context, request metadata)
- **THEN** the storage primitive is `contextvars.ContextVar`, or OpenTelemetry baggage (preferred when the ADOT layer is loaded — itself ContextVar-backed and propagates across service boundaries), or Node's `AsyncLocalStorage` — never a module-level mutable dict, list, or attribute

#### Scenario: [TOOLS-008.04] Read-only import-time configuration is permitted as a module global

- **WHEN** library or handler code captures configuration that is set once at process start and read many times without mutation (e.g., `STAGE = os.environ.get("STAGE")`, `_LOG = logging.getLogger(__name__)`)
- **THEN** storing the value as a module-level binding is permitted because the value is immutable after process start and cannot leak per-invocation state between tests or async tasks

#### Scenario: [TOOLS-008.05] Interactive debugging opts out via -n 0

- **WHEN** a contributor needs to drop into a debugger (pdb / breakpoint) during test execution
- **THEN** the contributor invokes pytest with `-n 0` (or with `--pdb`, which xdist supports) to disable worker distribution and run serially in the main process; the default `-n auto` behavior is restored on the next invocation without further configuration

#### Scenario: [TOOLS-008.06] Per-test ambient-state reset for tests that exercise ContextVar storage directly

- **WHEN** a test file exercises per-invocation ambient state directly (e.g., calling `set_mdc(...)` / `get_mdc(...)` in tests for `cairn_observability.log` and `cairn_observability.scope`, rather than going through `observability_scope()` which clears the state on exit)
- **THEN** the test file installs an autouse fixture that resets the ambient state before AND after each test, because `ContextVar` isolates state between threads / asyncio tasks / `Context.run()` invocations but NOT between sequential sync pytest tests sharing a worker process — under either serial execution or `pytest -n auto`, xdist routes multiple tests to the same worker and they share that worker's ContextVar state. The fixture provides the per-test isolation that ContextVar's structural mechanism cannot

#### Scenario: [TOOLS-008.07] Concurrent-prone bash build scripts are gated against races

- **WHEN** `cdk/scripts/build-lambdas.sh` or `lambda-layers/notation/build.sh` is invoked concurrently with another instance of itself (e.g., from a parallel CI job or a contributor running two bundling commands)
- **THEN** each invocation acquires a `flock`-based exclusive lock around its `rm -rf` + write step so the scripts serialize-when-concurrent without callers needing to remember orchestration discipline, and the script's deterministic output is preserved

### Requirement: [TOOLS-009] Contributor Guide Separated from User-Facing README

The repository SHALL maintain contributor-facing setup, build, test, and deploy documentation in a dedicated repo-root `DEVELOPING.md`, distinct from the user-facing `README.md`. `README.md` is the user-facing overview and SHALL NOT carry contributor build/test/deploy tooling; its structure and contents are otherwise at the author's discretion (this Requirement governs the split, not the README's table of contents). `DEVELOPING.md` SHALL carry the contributor workflow (mise toolchain bootstrap, `uv sync`, pre-commit install, the `mise run check` pre-merge gate and its subtasks, the build scripts, and the deploy steps). The two files SHALL cross-link each other, and no contributor instruction SHALL appear in both — each fact lives in exactly one file (single source of truth). `AGENTS.md` (the agent-facing instruction file) is out of scope of this split and unaffected.

#### Scenario: [TOOLS-009.01] Contributor workflow lives in DEVELOPING.md

- **WHEN** a contributor needs the local-development workflow (mise install, `uv sync`, pre-commit, `mise run check` and its subtasks, the Lambda/layer build scripts, or the deploy steps)
- **THEN** that material is found in repo-root `DEVELOPING.md`, not in `README.md`

#### Scenario: [TOOLS-009.02] README is the user-facing overview

- **WHEN** a first-time evaluator opens `README.md`
- **THEN** it presents a user-facing overview of what the system is and the problem it solves, without contributor build/test/deploy tooling

#### Scenario: [TOOLS-009.03] The two docs cross-link without duplicating content

- **WHEN** a reader lands on either `README.md` or `DEVELOPING.md`
- **THEN** each links to the other (`README.md` → `DEVELOPING.md` for contributing; `DEVELOPING.md` → `README.md` for the overview), and no contributor instruction appears in both files

### Requirement: [TOOLS-010] Integration Coverage for Content-Addressed Dedup and Behavioral Drift

The content-addressed idempotency guarantee (`artifact-ingestion` *Content-Addressable Deduplication* / *Bundle Identity Is Pure Content Addressing*) and the stability of the pipeline's observable outputs SHALL be exercised by the `CAIRN_INTEGRATION_ENV`-gated integration suite — a separate gate run via `mise run integration`, NOT part of `mise run check`. The suite SHALL verify, end-to-end against a real deployment, that byte-identical content pushed twice resolves to a single bundle with idempotent index rows, that distinct content yields a distinct bundle (no over-collapse), and that a stable input fixture produces observable outputs matching pinned expectations so behavioral drift fails the test. These dedup/drift tests SHALL push into the pre-provisioned repository (see *Integration Suite Reuses Pre-Provisioned ECR Repositories*), because idempotency is observable only across two pushes into the same repository.

This Requirement specifies a *test-coverage obligation*; `artifact-ingestion` normatively specifies the underlying dedup and identity behavior, which this Requirement neither restates nor alters.

#### Scenario: [TOOLS-010.01] Identical content pushed twice yields exactly one bundle
- **WHEN** an artifact bundle is pushed via `cairn push` into the env-specified ECR repository and, after the pipeline completes, byte-identical content is pushed again into the same repository
- **THEN** the integration test SHALL assert ECR holds exactly one manifest for that content (the same `Manifest_Digest` both times) and no duplicate manifest is created

#### Scenario: [TOOLS-010.02] Idempotent index rows under re-push
- **WHEN** identical content is pushed twice and both pushes complete the pipeline
- **THEN** the integration test SHALL assert the Metadata_Index and ControlsIndex contain no duplicate or conflicting rows keyed on the shared `Manifest_Digest` (re-push is idempotent at the index layer)

#### Scenario: [TOOLS-010.03] Trivially-distinct content is not over-collapsed
- **WHEN** content whose true content bytes differ from a previously pushed bundle is pushed into the same repository
- **THEN** the integration test SHALL assert a new, distinct `Manifest_Digest` and a distinct bundle ARE created, confirming dedup keys on real content identity rather than over-collapsing

#### Scenario: [TOOLS-010.04] Observable outputs are pinned against drift
- **WHEN** a stable input fixture is pushed through the pipeline
- **THEN** the integration test SHALL assert the bundle's observable outputs — manifest-digest determinism, archive S3 key shape, and metadata-projection shape — match pinned expected values, so any unintended behavioral drift fails the test

#### Scenario: [TOOLS-010.05] Dedup gate excluded from the fast pre-merge check
- **WHEN** `mise run check` runs without `CAIRN_INTEGRATION_ENV` set
- **THEN** the dedup/drift tests SHALL be skipped (they belong to the separate `mise run integration` gate and do not run in the fast pre-merge path)

### Requirement: [TOOLS-011] Integration Suite Reuses Pre-Provisioned ECR Repositories

The `CAIRN_INTEGRATION_ENV`-gated integration suite SHALL push evidence into the ECR repository the deployment already provisions for its configured project (`{appName}-artifact-{qualifier}/{projectId}`; because artifact-kind collapses into the image tag rather than the repository path, one repository per project serves all artifact kinds). Every test in the suite — happy-path, failure-path, and dedup/drift alike — SHALL resolve its target project from the environment (defaulting to the stack's configured project) and SHALL NOT create an ECR repository for tests or delete one on teardown. WHEN the required pre-provisioned repository is absent, a test SHALL skip with a clear message rather than create it. Tests SHALL still clean up the manifests, index rows, and S3 objects they create, leaving the shared repository intact. Because the test path no longer creates or deletes repositories, it SHALL NOT require `ecr:CreateRepository` or `ecr:DeleteRepository`.

#### Scenario: [TOOLS-011.01] Suite pushes into the pre-provisioned repository
- **WHEN** any integration test runs against a deployment whose configured-project repository is pre-provisioned
- **THEN** the test SHALL push into that existing repository and SHALL NOT call `CreateRepository`

#### Scenario: [TOOLS-011.02] Missing pre-provisioned repository skips, not creates
- **WHEN** an integration test runs and the configured-project repository does not exist
- **THEN** the test SHALL skip with a clear message and SHALL NOT create the repository on demand

#### Scenario: [TOOLS-011.03] Teardown leaves the shared repository intact
- **WHEN** an integration test completes (pass or fail) and its cleanup runs
- **THEN** the cleanup SHALL delete only the manifests, index rows, and S3 objects that test created and SHALL NOT call `DeleteRepository` on the shared repository

### Requirement: [TOOLS-012] Static Application Security Testing Gate

THE project SHALL run static application security testing (SAST) as part of the pre-merge gate using [Semgrep](https://semgrep.dev/), pinned in `mise.toml` per the *Toolchain Pinned and Bootstrapped via mise* requirement and invoked via a dedicated `semgrep` task. The `semgrep` task SHALL scan both the Python workspace (`cli/`, `lambdas/`, `libs/`) and the TypeScript CDK app (`cdk/`) against rule *content* that is vendored and committed into the repository (or pinned to an immutable registry version/digest), selected via `--config <committed-path>`, so the rule set is reproducible from the repository alone and does NOT depend on fetching mutable rule packs from the Semgrep registry at scan time. The task SHALL run offline with telemetry disabled and no dependency on a logged-in Semgrep account or third-party platform. The `semgrep` task SHALL be listed in `check.depends` so it runs as part of `mise run check`, and SHALL fail the aggregate (non-zero exit) when any finding is reported. Semgrep complements — and does not replace — Ruff's `flake8-bandit` Python security rules: Semgrep also covers the TypeScript CDK app, which Ruff does not analyze.

#### Scenario: [TOOLS-012.01] semgrep runs as part of the pre-merge gate

- **WHEN** a contributor invokes `mise run check`
- **THEN** the `semgrep` subtask executes (in parallel with the other subtasks) and scans the Python workspace and the TypeScript CDK app against the committed rule configuration

#### Scenario: [TOOLS-012.02] A Semgrep finding fails the aggregate gate

- **WHEN** the `semgrep` subtask reports one or more findings (non-zero exit)
- **THEN** the `mise run check` invocation as a whole exits non-zero and the finding output is preserved in the report, consistent with every other `check` subtask

#### Scenario: [TOOLS-012.03] Semgrep is pinned via mise, not installed per-machine

- **WHEN** a new contributor runs `mise install` on a fresh checkout
- **THEN** Semgrep is installed at the version pinned in `mise.toml` via an appropriate mise backend and placed on PATH, requiring no per-machine `brew`/`pipx`/`curl | sh` install and no Semgrep account login

#### Scenario: [TOOLS-012.04] Semgrep scans reproducibly from committed rule content

- **WHEN** the `semgrep` task runs in any environment (contributor or CI)
- **THEN** it scans against rule *content* vendored and committed into the repository (or pinned to an immutable registry version/digest), selected via `--config <committed-path>`, running offline with telemetry disabled, so the rule set is fully reproducible from the repository alone and cannot be weakened by mutation of, or loss of access to, the remote Semgrep registry at scan time

### Requirement: [TOOLS-013] Stable Identifiers for Requirements and Scenarios

Every `### Requirement:` and `#### Scenario:` in `openspec/specs/` SHALL carry a stable identifier, and the `mise run check` gate SHALL fail when the identifier discipline below is violated.

- **Format.** A requirement identifier is `PREFIX-NNN` — an uppercase capability prefix, a hyphen, and a 3-digit zero-padded sequence — at the start of the heading name: `### Requirement: [PREFIX-NNN] <Requirement Name>`. A scenario identifier is `PREFIX-NNN.MM`, a 2-digit zero-padded sub-sequence under its requirement: `#### Scenario: [PREFIX-NNN.MM] <Scenario Name>`. Both halves SHALL be zero-padded to their width; a bare single-digit sub-sequence (`PREFIX-NNN.M`) is not well-formed. Each capability has exactly one prefix.
- **Explicit, not positional.** Identifiers SHALL be assigned, never derived from list position. Sequences (and scenario sub-sequences) allocate forward from a monotonic high-water mark and SHALL NOT be reused, even after retirement.
- **Immutable.** An identifier, once assigned, SHALL NOT be renumbered or change meaning. Its identity is its `(prefix, sequence, sub-sequence)` values, independent of zero-pad rendering width; widening the zero-pad of an existing identifier (e.g. `PREFIX-009.1` to `PREFIX-009.01`) is a rendering change, not a renumber, and SHALL NOT be treated as a vanished or new identifier. Editing a requirement's human name is a retitle (an OpenSpec `RENAMED` operation) that leaves the identifier unchanged.
- **Relocation and retirement.** When a requirement moves to another capability it SHALL be retired under its current prefix and re-minted under the destination prefix (its scenarios re-mint with it). A retired or relocated identifier SHALL be recorded in that spec's `## Relocated and Retired IDs` table — never as a `### Requirement:` block — with its status (`withdrawn` or `relocated`) and, for a relocation, the destination identifier. References to it SHALL resolve through that table.
- **Cross-references.** A reference to a requirement or scenario SHALL cite its identifier, and every such reference SHALL resolve to a live identifier or a ledger entry. Positional references — citing by position rather than by identifier — SHALL NOT be used.
- **Correctness properties.** A correctness property SHALL be expressed as a scenario and referenced by that scenario's identifier; there SHALL be no separate property-numbering scheme. Whether a scenario is verified by a property-based test is a test-layer concern, not encoded in the spec.

#### Scenario: [TOOLS-013.01] Every requirement and scenario carries an identifier
- **WHEN** the gate runs and a `### Requirement:` or `#### Scenario:` heading in `openspec/specs/` lacks a well-formed identifier
- **THEN** `mise run check` fails and names the offending heading

#### Scenario: [TOOLS-013.02] Identifiers are unique
- **WHEN** the gate runs and the same identifier is assigned to two headings
- **THEN** `mise run check` fails and reports the collision

#### Scenario: [TOOLS-013.03] Identifiers are immutable and never reused
- **WHEN** an identifier present in the previous committed corpus is absent from the current corpus without a corresponding `## Relocated and Retired IDs` entry, or a new heading reuses a previously retired identifier
- **THEN** `mise run check` fails

#### Scenario: [TOOLS-013.04] Retitling preserves the identifier
- **WHEN** a requirement's human name is edited while its `[PREFIX-NNN]` identifier is unchanged
- **THEN** the gate treats it as the same requirement and does not flag it as missing or renumbered

#### Scenario: [TOOLS-013.05] Relocation is recorded and resolves through the ledger
- **WHEN** a requirement moves capabilities — retired under its old prefix, re-minted under the new one, with a `relocated` row pointing at the new identifier
- **THEN** the gate passes and references to the old identifier resolve to the new one

#### Scenario: [TOOLS-013.06] Cross-references must resolve
- **WHEN** the gate runs and a cross-reference cites an identifier that is neither live nor present in any `## Relocated and Retired IDs` table
- **THEN** `mise run check` fails and reports the dangling reference

#### Scenario: [TOOLS-013.07] Correctness properties are referenced by scenario identifier
- **WHEN** a property-based test or a spec cross-reference needs to cite a correctness property
- **THEN** it cites the scenario's identifier, and no separate `Property N` numbering exists in the corpus

#### Scenario: [TOOLS-013.08] Scenario sub-sequences are zero-padded to two digits
- **WHEN** the gate runs and a `#### Scenario:` heading carries a bare single-digit sub-sequence (`[PREFIX-NNN.M]`) instead of the 2-digit zero-padded form (`[PREFIX-NNN.MM]`)
- **THEN** `mise run check` fails and names the offending scenario heading
- **AND** widening an existing identifier's sub-sequence pad (`[PREFIX-NNN.M]` to `[PREFIX-NNN.0M]`) does not trip the immutability or no-reuse checks, because identity is the `(prefix, sequence, sub-sequence)` values independent of rendering width

### Requirement: [TOOLS-014] Single Dependency Update Command

THE project SHALL provide one command, `mise run update-deps`, that updates the third-party dependencies of every uv workspace member, the root `dev` dependency group, and the CDK app in `cdk/`. One invocation SHALL re-resolve `uv.lock` and `cdk/package-lock.json` to the newest versions that the declared ranges allow. The command SHALL NOT change mise-managed tool versions.

#### Scenario: [TOOLS-014.01] One invocation refreshes both lockfiles
- **WHEN** a contributor runs `mise run update-deps` on a clean checkout and newer in-range versions exist in both ecosystems
- **THEN** `uv.lock` and `cdk/package-lock.json` both record the newest in-range versions, `uv lock --check` passes, and `npm ci` in `cdk/` installs from the new lockfile without changing it

#### Scenario: [TOOLS-014.02] Exact pins do not move
- **WHEN** a member declares a dependency with an exact `==` pin, such as `oscal-bindings` in `libs/oscal/pyproject.toml`, and a newer version of it exists
- **THEN** `uv.lock` records the pinned version after the update

### Requirement: [TOOLS-015] Direct Dependencies Stay Within Their Major Version

A direct dependency is a third-party package that a workspace member, the root `dev` group, or `cdk/package.json` declares. Its major version is the first number of its release version. Each direct dependency's range SHALL exclude its next major version. `mise run update-deps` SHALL fail when a range does not, or when a locked major version would change. Transitive dependencies follow their dependents' ranges.

#### Scenario: [TOOLS-015.01] A newer major version is not adopted
- **WHEN** a direct dependency has a newer major version and a newer minor version on its registry
- **THEN** the update records the newer minor version and keeps the current major version

#### Scenario: [TOOLS-015.02] A range without a major-version bound fails the update
- **WHEN** a Python direct dependency declares no upper bound below its next major version, or a CDK direct dependency declares a range that admits its next major version
- **THEN** `mise run update-deps` exits non-zero, names the package and its manifest, and leaves both lockfiles unchanged

#### Scenario: [TOOLS-015.03] A 0.x minor release is not a major change
- **WHEN** a direct dependency is locked at `0.63b1` and `0.66b2` is in range
- **THEN** the update records `0.66b2` and the major-version check passes

#### Scenario: [TOOLS-015.04] A transitive major change is reported, not blocked
- **WHEN** a direct dependency's new in-range version requires or permits a new major version of a transitive dependency, and the resolver selects it
- **THEN** the update succeeds and the change report lists the transitive package with its old and new versions

#### Scenario: [TOOLS-015.05] A form the range check cannot interpret fails the update
- **WHEN** a manifest declares a dependency in a table or form that the range check does not interpret, such as a direct URL requirement or a `[tool.uv.sources]` entry that is not `workspace = true`
- **THEN** `mise run update-deps` exits non-zero before any lockfile changes and names the manifest and the entry

#### Scenario: [TOOLS-015.06] A manifest that changes during the run fails the update
- **WHEN** a manifest differs from the content that the range check read, at the time the lock step starts
- **THEN** `mise run update-deps` exits non-zero and both lockfiles match their content before the run

#### Scenario: [TOOLS-015.07] A workspace member with another build backend fails the update
- **WHEN** a workspace member's `[build-system] requires` differs from `["hatchling"]`
- **THEN** `mise run update-deps` exits non-zero before any lockfile changes and names the member

### Requirement: [TOOLS-016] Dependency Update Is All-or-Nothing

WHEN `mise run update-deps` fails before the gate runs, for example because a dependency cannot be resolved, a registry is unreachable, or the major-version check fails, `uv.lock` and `cdk/package-lock.json` SHALL be byte-identical to their content before the run. The command SHALL exit non-zero, and its error SHALL name the failing package where the failure identifies one, or the failing step otherwise.

#### Scenario: [TOOLS-016.01] A failure in the second ecosystem restores the first
- **WHEN** the Python resolution succeeds and the CDK update then fails
- **THEN** both lockfiles match their content before the run, and the command exits non-zero and names the CDK update step

#### Scenario: [TOOLS-016.02] An unresolvable dependency is named
- **WHEN** the resolver cannot satisfy a declared range
- **THEN** both lockfiles match their content before the run, and the error output contains the name of the package that the resolver reports

#### Scenario: [TOOLS-016.03] Uncommitted lockfile changes stop the run before any change
- **WHEN** `git status --porcelain` reports a change to `uv.lock` or `cdk/package-lock.json` at the start of the run
- **THEN** the command exits non-zero before it changes any file, and the error prints the command that restores both lockfiles

#### Scenario: [TOOLS-016.04] Termination signals restore the lockfiles
- **WHEN** the command receives SIGINT, SIGTERM or SIGHUP after the first lockfile has changed
- **THEN** the command stops its child process, restores both lockfiles to their content before the run, and exits non-zero

#### Scenario: [TOOLS-016.05] A failed restore of one file does not skip the other
- **WHEN** the restore of one lockfile fails
- **THEN** the command still restores the other lockfile, exits non-zero, and names the file that it could not restore

#### Scenario: [TOOLS-016.06] A second concurrent run stops
- **WHEN** one `mise run update-deps` run is active and a second starts
- **THEN** the second run exits non-zero without changing any file

### Requirement: [TOOLS-017] Dependency Update Reports Changes and Runs the Gate

AFTER both lockfiles are updated, `mise run update-deps` SHALL print one line per changed package in each ecosystem, giving the package name and its old and new versions, or marking it added or removed. It SHALL then run `mise run check`, and its exit status SHALL be non-zero when the gate fails. A gate failure SHALL leave the updated lockfiles in place.

#### Scenario: [TOOLS-017.01] The change report precedes the gate
- **WHEN** an update changes one or more package versions
- **THEN** the output lists each changed package with its old and new versions before the output of `mise run check`

#### Scenario: [TOOLS-017.02] A gate failure keeps the update for investigation
- **WHEN** the lockfiles update and `mise run check` then fails
- **THEN** `mise run update-deps` exits non-zero, the updated lockfiles stay in the working tree, and the change report and the failing `check` subtask are both in the output

#### Scenario: [TOOLS-017.03] A source or hash change at an equal version is reported
- **WHEN** a lockfile entry changes its source, hash or integrity value and keeps its version
- **THEN** the change report lists the package with the changed field, and control characters in package names and versions are escaped

#### Scenario: [TOOLS-017.04] The report is also written to a local file
- **WHEN** a run ends, whether it succeeds, fails or restores
- **THEN** the file `.update-deps-report.txt`, which git ignores, holds the report, the lock-audit result, the SHA-256 of both lockfiles before and after, and the restore result

#### Scenario: [TOOLS-017.05] A report print failure does not hide the gate result
- **WHEN** printing the report fails, for example because the output pipe closes
- **THEN** the command still runs the gate and exits with the exit status of `mise run check`

### Requirement: [TOOLS-018] Lambda Bundles Install the Locked Versions

EACH bundle that `cdk/scripts/build-lambdas.sh` builds SHALL contain the versions that `uv.lock` records for that Lambda's dependency closure, excluding the packages the script strips as runtime-provided. The build SHALL verify each third-party distribution against its `uv.lock` hash. The build SHALL fail, and leave no bundle for that Lambda, when `uv.lock` is out of date with the workspace manifests or a hash does not match.

#### Scenario: [TOOLS-018.01] Bundle versions equal locked versions
- **WHEN** `cdk/scripts/build-lambdas.sh` completes
- **THEN** for every third-party `*.dist-info` directory in `lambda-bundles/<name>/`, the version equals the version `uv.lock` records for that package

#### Scenario: [TOOLS-018.02] A stale lockfile stops the build
- **WHEN** a member `pyproject.toml` changes a dependency and `uv.lock` is not re-locked
- **THEN** `cdk/scripts/build-lambdas.sh` exits non-zero and names the out-of-date lockfile

#### Scenario: [TOOLS-018.03] A hash mismatch stops the build
- **WHEN** a downloaded third-party distribution does not match the hash in `uv.lock`
- **THEN** `cdk/scripts/build-lambdas.sh` exits non-zero and no bundle remains in `lambda-bundles/<name>/` for that Lambda

### Requirement: [TOOLS-019] Release Cooldown for Dependency Updates

`mise run update-deps` SHALL select only package versions that their registry published at least N days before the run, in both ecosystems. N is the day count of the `exclude-newer` entry in `[tool.uv]` of the root `pyproject.toml`, and that entry applies to every `uv lock` run in the repository. An `exclude-newer-package` entry in the same table exempts one package. The command SHALL fail when `exclude-newer` is missing or is not of the form `N days`.

#### Scenario: [TOOLS-019.01] A release inside the cooldown is not selected
- **WHEN** a registry published a newer version of a dependency less than N days before the run
- **THEN** the update records the newest version that is at least N days old, in both lockfiles

#### Scenario: [TOOLS-019.02] A lock made under the cooldown passes the locked commands
- **WHEN** `update-deps` finishes
- **THEN** `uv lock --check` and `uv export --locked` pass without any cooldown option on the command line

#### Scenario: [TOOLS-019.03] A package exemption selects a newer version
- **WHEN** `exclude-newer-package` names a package and a version newer than the cooldown cutoff exists
- **THEN** the update may record that version for that package only, and `uv lock --check` passes

#### Scenario: [TOOLS-019.04] A missing or malformed cooldown fails the update
- **WHEN** `exclude-newer` is absent from `[tool.uv]`, or its value is not of the form `N days`
- **THEN** `mise run update-deps` exits non-zero before any lockfile changes

### Requirement: [TOOLS-020] Dependency Resolution Runs No Package Code

`mise run update-deps` SHALL resolve without package code. It SHALL run uv with `--no-build` and npm with `--ignore-scripts`. It SHALL start each child process, `mise run check` included, with only PATH, HOME, TMPDIR, LANG, TERM, `XDG_*` and `MISE_*`. It SHALL fail before any change when `uv.toml`, `.npmrc`, `pip.conf` or `[tool.uv]` sets a package index or registry.

#### Scenario: [TOOLS-020.01] A package that needs a source build fails the update
- **WHEN** a dependency has only a source distribution
- **THEN** `mise run update-deps` exits non-zero, names the package, and both lockfiles match their content before the run

#### Scenario: [TOOLS-020.02] Credentials in the environment do not reach child processes
- **WHEN** `AWS_ACCESS_KEY_ID`, `UV_INDEX_URL` or `npm_config_registry` is set in the caller's environment
- **THEN** none of them is visible to uv, npm or `mise run check` during the run

#### Scenario: [TOOLS-020.03] A repository index setting fails the update
- **WHEN** the repository sets a package index or registry in a file that uv or npm reads
- **THEN** `mise run update-deps` exits non-zero before any lockfile changes and names the file

### Requirement: [TOOLS-021] Lockfiles Are Audited for Source and Integrity

THE project SHALL provide a `lock-audit` task in `check.depends` that reads `uv.lock` and `cdk/package-lock.json` and fails on any package that does not come from the default registry with a hash. Workspace members SHALL have an editable or virtual source. A dependency bundled inside its parent package is exempt. `mise run update-deps` SHALL run the audit after resolution, and `npm audit signatures`, and restore both lockfiles on failure.

#### Scenario: [TOOLS-021.01] The committed lockfiles pass the audit
- **WHEN** `mise run lock-audit` runs on a checkout whose lockfiles come from `update-deps`
- **THEN** it exits zero

#### Scenario: [TOOLS-021.02] A foreign source fails the gate
- **WHEN** a lockfile entry has a registry other than `https://pypi.org/simple` or `https://registry.npmjs.org/`, a git, file or link source, or no hash or integrity value
- **THEN** `mise run lock-audit` exits non-zero and names the package

#### Scenario: [TOOLS-021.03] A failed audit after an update restores the lockfiles
- **WHEN** the audit or `npm audit signatures` fails after the lockfiles update
- **THEN** both lockfiles match their content before the run and the command exits non-zero

### Requirement: [TOOLS-022] Lambda Bundle Build Is Isolated From Ambient Configuration

`cdk/scripts/build-lambdas.sh` SHALL run every uv command with only PATH, HOME, TMPDIR and LANG. It SHALL run `uv pip install` with `--no-config` and the default index `https://pypi.org/simple`, and SHALL install third-party packages with `--only-binary :all:`. It SHALL build workspace members with a build backend from a hashed `build` dependency group in `uv.lock`. It SHALL write the SHA-256 of `uv.lock` into `BUILD-INFO.json` in each bundle.

#### Scenario: [TOOLS-022.01] UV variables do not change the build
- **WHEN** `UV_INDEX_URL`, `UV_EXTRA_INDEX_URL` or `UV_NO_VERIFY_HASHES` is set in the caller's environment
- **THEN** the build ignores it and installs from the default index with hash verification

#### Scenario: [TOOLS-022.02] The build backend is verified by hash
- **WHEN** a build-backend distribution does not match the hash in `uv.lock`
- **THEN** `cdk/scripts/build-lambdas.sh` exits non-zero and no bundle remains for that Lambda

#### Scenario: [TOOLS-022.03] Each bundle records its lock digest
- **WHEN** a bundle build completes
- **THEN** `BUILD-INFO.json` in the bundle holds the SHA-256 of `uv.lock`, and no commit hash or build path

### Requirement: [TOOLS-023] CI Builds and Deploys Use Committed Content

WHEN the `CI` environment variable is set to a non-empty value, `cdk/scripts/build-lambdas.sh` SHALL fail if `git status --porcelain --untracked-files=all` reports a change under `lambdas/`, `libs/`, `cli/`, `uv.lock` or `pyproject.toml`. In the same case `cdk/scripts/deploy.sh` SHALL fail if `uv.lock`, `cdk/package-lock.json` or either `package.json` or `pyproject.toml` differs from `HEAD`. Neither script SHALL run these checks when `CI` is unset.

#### Scenario: [TOOLS-023.01] A dirty tree stops a CI build
- **WHEN** `CI` is set and a tracked or untracked file under `lambdas/` or `libs/` differs from `HEAD`
- **THEN** `cdk/scripts/build-lambdas.sh` exits non-zero and lists the files

#### Scenario: [TOOLS-023.02] Changed lockfiles stop a CI deploy
- **WHEN** `CI` is set and `uv.lock` or `cdk/package-lock.json` differs from `HEAD`
- **THEN** `cdk/scripts/deploy.sh` exits non-zero before it builds a bundle

#### Scenario: [TOOLS-023.03] A developer machine is not blocked
- **WHEN** `CI` is unset and the working tree has uncommitted changes
- **THEN** both scripts run without these checks

### Requirement: [TOOLS-024] CDK Dependencies Install Without Lifecycle Scripts

THE project SHALL install the CDK app's dependencies with `npm ci --ignore-scripts`, in `cdk/scripts/deploy.sh` and in the `cdk-install` task of `mise.toml`, so that no npm lifecycle script runs with the operator's credentials in the environment.

#### Scenario: [TOOLS-024.01] Installing runs no lifecycle script
- **WHEN** `mise run cdk-install` or `cdk/scripts/deploy.sh` installs the CDK dependencies
- **THEN** the install runs `npm ci --ignore-scripts`, and `mise run cdk-typecheck` and `mise run cdk-test` pass afterwards
