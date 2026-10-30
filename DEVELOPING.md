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

> **Threat-model diagram rendering:** The `threat-model` agent and `mise run tm-render` use `mmdc` (`@mermaid-js/mermaid-cli`) to render threat-model DFDs to PNG for the Threat Composer UI. `mise install` provisions the `mmdc` binary. Its headless browser is a **separate, one-time `mise run mmdc-setup`** (~160MB `chrome-headless-shell`). `mise install` deliberately does not auto-download the browser, so CI and `deploy.sh` never pay for it. This step is **dev-time only**, off the `deploy.sh` path and out of `mise run check`. It degrades gracefully if the browser is absent — diagram slots stay empty and threat models still validate. See [`docs/adr/0013-accept-chromium-dependency-for-threat-model-diagram-rendering.md`](docs/adr/0013-accept-chromium-dependency-for-threat-model-diagram-rendering.md).

First-time setup:

```bash
# 1. Install mise once per machine (skip if already present)
brew install mise            # macOS / Linuxbrew
# or:
curl https://mise.run | sh   # other unix
# Then enable shell activation per https://mise.jdx.dev/installing-mise.html#shells
# (mise activate runs automatically when entering directories with a mise.toml).

# 2. Provision the entire pinned toolchain (Node 24, Python 3.12, uv, aws,
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
| `cdk-install`   | `npm ci` in `cdk/` (cached on `package.json` + `package-lock.json`) | transitively|
| `cdk-typecheck` | `npm run build` (tsc) in `cdk/`                  |     yes     |
| `cdk-test`      | `npm test` (jest) in `cdk/`                      |     yes     |
| `cdk-synth`     | `npx cdk synth --quiet` in `cdk/`                |  **no**     |

`lint` carries the security dimension for Python. `pyproject.toml` enables Ruff's `flake8-bandit` (`S`) rule group. Bandit-class rules therefore ride the existing `lint` subtask rather than a standalone tool. Ruff stays the single Python linter. `semgrep` is the SAST gate over both the Python workspace and the TypeScript CDK app. It scans **offline** against rule content vendored under `tools/semgrep/rules/`. The scan never resolves the mutable `p/` registry at scan time. Refreshing those vendored rules is a deliberate, reviewable re-vendor commit. The procedure and provenance live in [`tools/semgrep/README.md`](tools/semgrep/README.md). Semgrep installs via mise's `pipx` backend, so `mise install` remains the whole setup story — no extra per-machine step.

`cdk-synth` is intentionally outside `check`. Once Lambdas land, CDK asset bundling will make `cdk synth` depend transitively on Python source under `lambdas/` and `libs/`. Correct `sources`/`outputs` caching at that point needs per-construct knowledge of bundled paths — a stale-cache failure mode is hostile. Until that shape is real, `cdk-synth` runs un-cached on demand. See [`openspec/specs/toolchain/spec.md`](openspec/specs/toolchain/spec.md) for the canonical spec.

`cdk-install` can mtime-skip when it should not. This is rare. It typically follows a `git checkout` of a branch with a different `package-lock.json`, or a manual delete of `node_modules/`. Force a re-run with `mise run cdk-install --force`.

The CDK app is **not bootstrapped** by checkout. An operator with appropriate IAM permissions runs bootstrap (`cdk bootstrap aws://<account>/us-east-2`) once per AWS account. A developer machine cannot run it without those credentials. See `runbooks/deploy-alpha.md` for the procedure.

`WORM_Enabled` is hard-coded to `false` in the `cmh-alpha` stack (`cdk/bin/artifact-store.ts`). See [`worm-archive`](openspec/specs/worm-archive/spec.md) for the canonical behavior.

## How to change the system

All requirement changes flow through the OpenSpec `/openspec-*` skill commands — `openspec/specs/` is the canonical spec:

- `/openspec-explore` — think through ideas and clarify requirements before committing to a change
- `/openspec-propose` — scaffold `openspec/changes/<change-name>/` with proposal, design, spec deltas, and tasks
- `/openspec-apply-change` — walk the change's `tasks.md`, implementing each task
- `/openspec-archive-change` — validate and fold the spec delta into `openspec/specs/`

The skill commands wrap the underlying `openspec` CLI (`openspec status`, `openspec instructions`, `openspec new change`, `openspec validate`, `openspec archive`). You generally will not invoke those directly.

You edit engineering documents in `docs/` directly, without an OpenSpec change. These are the as-deployed architecture diagram and ADRs. They describe how the system works, not what the system *must* do. GitHub Issues track deferred work, not `docs/`.
