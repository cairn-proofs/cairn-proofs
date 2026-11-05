# Developing Cairn Proofs

This is the contributor guide: local-development setup, the build/test gate, and how to build and deploy. For the system overview, capability map, and the "what and why," see [README.md](README.md).

## Building and deploying

You stand up — or fork — the stack via the pieces below.

| Piece | What it is |
|---|---|
| [`cdk/`](cdk) | TypeScript CDK app. One stack, `cairnproofs4af-cmh-alpha`. Per-resource constructs under `cdk/lib/constructs/` (encryption, repositories, archive, metadata-table, controls-table, signing, IAM roles, Lambda functions, etc.) |
| [`lambdas/`](lambdas) | Five Python 3.12 Lambda packages — [`validate_bundle`](lambdas/validate_bundle), [`sign_bundle`](lambdas/sign_bundle), [`archive_to_s3`](lambdas/archive_to_s3), [`update_metadata`](lambdas/update_metadata), [`mint_tag`](lambdas/mint_tag). EventBridge-driven pipeline. One handler per package |
| [`cli/`](cli) | The `cairn` CLI — `push`, `pull`, `verify`, `query` against a deployed stack |
| [`lambda-layers/notation/`](lambda-layers/notation) | Notation CLI v1.3.2 + AWS Signer plugin v1.0.2292 (arm64) Lambda layer with SHA-256 + GPG verification, built by [`lambda-layers/notation/build.sh`](lambda-layers/notation/build.sh) |

Lambda code never imports `aws-cdk-lib`. The CDK stack reads Lambda bundles from `lambda-bundles/`. [`cdk/scripts/build-lambdas.sh`](cdk/scripts/build-lambdas.sh) produces these bundles. [AGENTS.md](AGENTS.md) holds the full one-way dependency map — `cdk/` → `lambdas/` → `libs/`.

Deploying:
- `mise run cdk-deploy` — one-shot command to deploy the full stack to AWS.
- [`cdk/scripts/deploy.sh`](cdk/scripts/deploy.sh) is the underlying script that mise runs. It builds the Notation layer, bundles the five Lambdas, runs the CloudTrail preflight, and `cdk deploy --all`s into the pinned account/region.
- `runbooks/deploy-alpha.md` is the step-by-step first-deploy procedure, including the one-time `cdk bootstrap`.
- See [Local development](#local-development) below for `mise install` / `uv sync` / `mise run check` — the dev loop that gates the deploy.

## Local development

**[mise](https://mise.jdx.dev/) manages the full toolchain via `mise.toml`.** A single `mise install` pins and installs Node, Python, uv, the AWS CLI, pre-commit, the openspec CLI, and the Mermaid CLI (`mmdc`). The only per-machine prerequisite is mise itself.

> **Threat-model diagram rendering:** `mise run tm-render` uses `mmdc` (`@mermaid-js/mermaid-cli`) to render threat-model DFDs to PNG for the Threat Composer UI. `mise install` provisions the `mmdc` binary. Its headless browser is a **separate, one-time `mise run mmdc-setup`** (~160MB `chrome-headless-shell`). `mise install` deliberately does not auto-download the browser, so CI and `deploy.sh` never pay for it. This step is **dev-time only**, off the `deploy.sh` path and out of `mise run check`. It degrades gracefully if the browser is absent — diagram slots stay empty and threat models still validate. No step of the OpenSpec flow calls it: the `threat-composer` MCP server renders each threat model's DFD itself. See [`docs/adr/0013-accept-chromium-dependency-for-threat-model-diagram-rendering.md`](docs/adr/0013-accept-chromium-dependency-for-threat-model-diagram-rendering.md).

### Threat models

Threat models are private. They live in a private store, not in this repo. The store holds one branch per
`cairn-proofs` public repo, named after the repo, with the model files at the branch root. Set up a working copy once:

```bash
THREAT_MODEL_STORE_URL=<url> scripts/setup-threat-model.sh
```

See internal onboarding for `THREAT_MODEL_STORE_URL`. This clones the store into `./threat-models` (gitignored), which
is a separate git repository. Commit model changes there with `git -C threat-models ...`, never with this repo's git.

During `/openspec-propose`, the `threat-model` skill builds a model for each change at
`threat-models/<change-name>.tc.json`. The `threat-composer` MCP server is the only writer. Its project config
(`.mcp.json` for Claude Code, `.kiro/settings/mcp.json` for Kiro) sets `THREAT_COMPOSER_AI_MODEL_DIR=threat-models`.
The change itself carries only `threat-model-report.md`, a sanitized summary that `openspec/tools/tc.py report`
writes. Open a `.tc.json` with the Threat Composer VS Code extension or web app.

### Supported platforms

Development is supported on **macOS and Linux** (including WSL2 on Windows). Native Windows is **not supported**: the build and deploy scripts (`cdk/scripts/*.sh`, `lambda-layers/notation/build.sh`) and the bash-based `mise` tasks assume a POSIX shell and standard Unix tools (`git`, `tar`, `mktemp`). macOS contributors also need `brew install flock`; see the "Concurrency-safe ambient state" notes in [AGENTS.md](AGENTS.md).

First-time setup:

```bash
# 1. Install mise once per machine (skip if already present)
brew install mise            # macOS / Linuxbrew
# or:
curl https://mise.run | sh   # other unix
# Then enable shell activation per https://mise.jdx.dev/installing-mise.html#shells
# (mise activate runs automatically when entering directories with a mise.toml).

# 2. Provision the entire pinned toolchain (Node 26, Python 3.12, uv, aws,
#    pre-commit, openspec, mermaid-cli) into mise's per-tool install dirs:
mise install

# 3. CDK npm deps
cd cdk && npm install && cd ..

# 4. Python workspace (.venv at repo root, dev tooling + any uv workspace members)
uv sync

# 5. Pre-commit hooks
pre-commit install

# 6. (Optional, threat-model authors only) Provision the headless browser mmdc 
#    uses to render threat-model diagrams in the OpenSpec propose flow. One-time, idempotent, ~160MB.
mise run mmdc-setup

```

> **ASD-STE100 skill (required, installed automatically):** the repo's agents depend on the ASD-STE100 writing skill. A `postinstall` hook in `mise.toml` runs `mise run install-asd-ste100` after every `mise install`, so no manual step is needed. The task writes **outside the repo**, to your user-level skill dirs (`~/.claude/skills/asd-ste100-skill` and `~/.kiro/skills/asd-ste100-skill`). It affects every project on the machine and replaces any existing copy there. It installs a commit pinned in `mise.toml` (bump `ASD_STE100_REF` deliberately), needs network access to github.com, and skips the fetch when both copies already match the pin. Run it by hand with `mise run install-asd-ste100` if the hook was skipped (`--no-hooks`, `MISE_NO_HOOKS=1`).

Adding a new project tool: prefer pinning it in `mise.toml` over `brew install`-ing it per machine. Mise supports a wide set of backends (`aws` via the aws-cli plugin, npm packages via `npm:` prefix, generic ubi/aqua/asdf backends for almost everything else). Fall back to brew/curl only when mise has no backend.

`mise` tasks expose the common commands. The canonical pre-merge gate is `mise run check`:

```bash
# Full pre-merge pipeline — Python lint + format-check + pytest, OpenSpec
# spec validation + spec-id lint, CDK type-check + tests, Semgrep SAST, and
# gitleaks secret scan. Subtasks fan out in parallel where independent. Run
# before pushing.
mise run check

# CDK synth (on-demand; deliberately NOT part of `check` — see
# openspec/specs/toolchain/spec.md for the rationale).
mise run cdk-synth

# Discover every task with its description.
mise tasks
```

Individual subtasks can be invoked on their own when you want to focus on a single gate, e.g. `mise run lint`, `mise run fmt`, `mise run test`, `mise run cdk-typecheck`, `mise run semgrep`. Pre-commit hooks (`ruff` + `openspec validate --specs --strict`) still run on every `git commit` — `mise run check` is the comprehensive layer above that.

The full task inventory (also visible via `mise tasks`):

| Task            | Wraps                                            | In `check`? |
| --------------- | ------------------------------------------------ | :---------: |
| `check`         | (aggregator over the subtasks below)             |      —      |
| `lint`          | `uv run ruff check .` (includes the `flake8-bandit` `S` security rules) |     yes     |
| `fmt`           | `uv run ruff format .`                           |  no (writes)|
| `fmt-check`     | `uv run ruff format --check .`                   |     yes     |
| `test`          | `uv run pytest`                                  |     yes     |
| `spec-validate` | `openspec validate --specs --strict`             |     yes     |
| `spec-ids`      | `python3 openspec/tools/spec_ids.py` — stable spec-identifier linter |     yes     |
| `semgrep`       | `semgrep scan` offline against vendored rules in `tools/semgrep/rules/` (Python workspace + CDK) |     yes     |
| `gitleaks`      | `gitleaks git` secret detection over full git history (offline, embedded rules) |     yes     |
| `lock-audit`    | `python3 tools/update_deps.py --audit-locks` — lockfile source and integrity audit (offline) |     yes     |
| `cdk-install`   | `npm ci --ignore-scripts` in `cdk/` (cached on `package.json` + `package-lock.json`) | transitively|
| `cdk-typecheck` | `npm run build` (tsc) in `cdk/`                  |     yes     |
| `cdk-test`      | `npm test` (jest) in `cdk/`                      |     yes     |
| `cdk-synth`     | `npx cdk synth --quiet` in `cdk/`                |  **no**     |
| `update-deps`   | `python3 tools/update_deps.py` — refresh both lockfiles, then run `check` (needs network) |  **no**     |

`lint` carries the security dimension for Python. `pyproject.toml` enables Ruff's `flake8-bandit` (`S`) rule group. Bandit-class rules therefore ride the existing `lint` subtask rather than a standalone tool. Ruff stays the single Python linter. `semgrep` is the SAST gate over both the Python workspace and the TypeScript CDK app. It scans **offline** against rule content vendored under `tools/semgrep/rules/`. The scan never resolves the mutable `p/` registry at scan time. Refreshing those vendored rules is a deliberate, reviewable re-vendor commit. The procedure and provenance live in [`tools/semgrep/README.md`](tools/semgrep/README.md). Semgrep installs via mise's `pipx` backend, so `mise install` remains the whole setup story — no extra per-machine step.

`cdk-synth` is intentionally outside `check`. Once Lambdas land, CDK asset bundling will make `cdk synth` depend transitively on Python source under `lambdas/` and `libs/`. Correct `sources`/`outputs` caching at that point needs per-construct knowledge of bundled paths — a stale-cache failure mode is hostile. Until that shape is real, `cdk-synth` runs un-cached on demand. See [`openspec/specs/toolchain/spec.md`](openspec/specs/toolchain/spec.md) for the canonical spec.

`cdk-install` can mtime-skip when it should not. This is rare. It typically follows a `git checkout` of a branch with a different `package-lock.json`, or a manual delete of `node_modules/`. Force a re-run with `mise run cdk-install --force`.

The CDK app is **not bootstrapped** by checkout. An operator with appropriate IAM permissions runs bootstrap (`cdk bootstrap aws://<account>/us-east-2`) once per AWS account. A developer machine cannot run it without those credentials. See `runbooks/deploy-alpha.md` for the procedure.

`WORM_Enabled` is hard-coded to `false` in the `cmh-alpha` stack (`cdk/bin/artifact-store.ts`). See [`worm-archive`](openspec/specs/worm-archive/spec.md) for the canonical behavior.

## Updating dependencies

`mise run update-deps` refreshes the third-party dependencies of every uv workspace member, the root `dev` group, and the CDK app in one run. It re-resolves `uv.lock` and `cdk/package-lock.json` inside the declared ranges, prints each changed package, and then runs `mise run check`. It needs network access, and it starts only from lockfiles that match `HEAD`.

- **No major-version bumps.** Every third-party direct dependency declares an upper bound below its next major version (`>=8.2.7,<9`; a 0.x package gets `<1`). The run fails, and names the package, when a range admits the next major or a manifest uses a form that the check cannot read. A major bump is a deliberate manifest edit: make it, re-lock, and commit both before the next `update-deps` run, because the run starts only from lockfiles that match `HEAD` and treats a major change against `HEAD` as a failure. Transitive dependencies follow the ranges that their dependents declare. The change report lists them, so a transitive major move is visible.
- **7-day cooldown.** `[tool.uv] exclude-newer = "7 days"` in the root `pyproject.toml` keeps every `uv lock` on releases that are at least 7 days old. The update task reads that value and applies the same cutoff to npm. To take a newer release of one Python package, for example a security fix, add it to `exclude-newer-package` in the same table. For one npm package, install the version directly with `npm install NAME@VERSION` outside `update-deps`.
- **No package code runs during resolution.** The task runs `uv lock --upgrade --no-build` and `npm update --package-lock-only --ignore-scripts`, with a minimal environment (`PATH`, `HOME`, `TMPDIR`, `LANG`, `TERM`, `XDG_*`, `MISE_*`) for every child process. A dependency that has only a source distribution fails the run. The run also fails when `uv.toml`, `.npmrc`, `pip.conf` or `[tool.uv]` sets a package index or registry.
- **All or nothing.** Any failure or termination signal before the gate restores both lockfiles and names the package or step. If a restore itself fails, or the process is killed with SIGKILL, run `git restore uv.lock cdk/package-lock.json`. The next run refuses to start while a lockfile has uncommitted changes, and only one run exists at a time.
- **The report.** The change report prints before the gate. The run also writes it, the lock-audit result, the SHA-256 of both lockfiles before and after, and the restore result to `.update-deps-report.txt`, which git ignores. A gate failure keeps the updated lockfiles so that you can read the failing subtask next to the report. Review `git diff uv.lock cdk/package-lock.json` before you commit.

`mise run lock-audit` runs offline and is part of `check`. It fails when a lockfile records a package from a source other than PyPI or the npm registry, a file without a hash or integrity value, or a git, file or link source. Workspace members must be editable or virtual. A dependency that npm bundles inside its parent package is exempt, because the parent's integrity value covers it. `update-deps` also runs `npm audit signatures` and restores the lockfiles when it fails.

`cdk/scripts/build-lambdas.sh` builds each Lambda bundle from `uv.lock`: the versions are the locked ones, and the third-party packages install from hashed wheels only. It builds the workspace members with a hashed build backend (the `build` dependency group), and it ignores `UV_*` variables and user uv configuration. Each bundle carries a `BUILD-INFO.json` with the SHA-256 of `uv.lock`. The build fails, and leaves no bundle for that Lambda, when `uv.lock` is out of date with the manifests (run `uv lock`) or a hash does not match.

When the `CI` environment variable is set to a non-empty value, `build-lambdas.sh` fails on uncommitted changes under `lambdas/`, `libs/`, `cli/`, `uv.lock` and `pyproject.toml`, and `deploy.sh` fails when `uv.lock`, `cdk/package-lock.json`, `cdk/package.json` or `pyproject.toml` differs from `HEAD`. Neither script runs these checks on a developer machine. `deploy.sh` and `cdk-install` install the CDK dependencies with `npm ci --ignore-scripts`, so no npm lifecycle script runs.

## How to change the system

All requirement changes flow through the OpenSpec `/openspec-*` skill commands — `openspec/specs/` is the canonical spec:

- `/openspec-explore` — think through ideas and clarify requirements before committing to a change
- `/openspec-propose` — scaffold `openspec/changes/<change-name>/` with proposal, design, spec deltas, and tasks
- `/openspec-apply-change` — walk the change's `tasks.md`, implementing each task
- `/openspec-archive-change` — validate and fold the spec delta into `openspec/specs/`

The skill commands wrap the underlying `openspec` CLI (`openspec status`, `openspec instructions`, `openspec new change`, `openspec validate`, `openspec archive`). You generally will not invoke those directly.

You edit engineering documents in `docs/` directly, without an OpenSpec change. These are the as-deployed architecture diagram and ADRs. They describe how the system works, not what the system *must* do. GitHub Issues track deferred work, not `docs/`.
