Captured-from: #40

## Why

Nobody refreshes the dependencies of the Python workspace and the CDK app on a routine basis, and no single command does it. `uv.lock` and `cdk/package-lock.json` have not been re-resolved since they were first written. The deployed Lambdas also do not run what the tests run. `cdk/scripts/build-lambdas.sh` installs each bundle with `uv pip install`, which does not read `uv.lock`. Each build therefore picks the newest versions that the declared ranges allow. A comparison of the built `validate_bundle` bundle against `uv.lock` shows 28 different versions, including protobuf 7.36.2 deployed against 6.33.6 locked. `mise run check` cannot protect a deployment that runs different code from the code it tested.

A routine refresh also pulls in new third-party releases, so the refresh itself needs controls: a release cooldown, no package code during resolution, and an audit of what the lockfiles record.

## Personas served

1. Alex — Platform / Security Engineer: "Deploy reproducibly (`cdk/scripts/deploy.sh`)". When bundles install exactly the locked versions, every deploy ships what `mise run check` tested.
2. The contributor who maintains the workspace has no persona. `openspec/personas/personas.md` models actors that have a system access path (IAM, `cairn` CLI, deployment). A repository contributor is outside that scope, so this change does not add one.

## What Changes

1. Add a `mise run update-deps` task. It updates the third-party dependencies of every uv workspace member (CLI, 5 Lambdas, 6 libraries, the root dev group) and the CDK app to the newest versions their declared ranges allow. It rewrites `uv.lock` and `cdk/package-lock.json` in one run.
2. A routine update never moves a direct dependency to a new major version. Every third-party direct dependency in a member `pyproject.toml` gets an upper bound below its next major version. The lower bound becomes the currently locked version. The CDK `package.json` caret and tilde ranges already do the same. The update task fails if a direct dependency would cross a major version anyway, or if a manifest uses a form that its range check cannot interpret. Transitive dependencies follow the ranges that their dependents declare.
3. The update task is all-or-nothing. If resolution fails, a registry is unreachable, the major-version check fails, or the process receives a termination signal, it restores both lockfiles, exits non-zero and names the package or step that failed. It refuses to start when a lockfile has uncommitted changes, and it allows one run at a time.
4. The update task prints each changed package with its old and new versions, writes the same report to a git-ignored local file, then runs `mise run check`.
5. The update task applies a 7-day release cooldown to both ecosystems. The value lives in the root `pyproject.toml` (`[tool.uv] exclude-newer`), so every `uv lock` in the repository uses it, and a committed `exclude-newer-package` entry exempts one package.
6. The update task resolves without running package code: `uv --no-build`, `npm --ignore-scripts`, a minimal child-process environment, and a refusal to run when the repository sets a package index or registry.
7. Add a `lock-audit` task to `mise run check`. It fails when `uv.lock` or `cdk/package-lock.json` records a package from a source other than the default registry, or without a hash. The update task runs it after resolution, together with `npm audit signatures`.
8. `cdk/scripts/build-lambdas.sh` installs each Lambda's dependencies from `uv.lock`, with hash verification for third-party packages and for the build backend. It ignores ambient uv configuration, installs third-party packages from wheels only, and records the lock digest in each bundle. The build fails if `uv.lock` is out of date with the manifests.
9. When the `CI` environment variable is set, `build-lambdas.sh` and `deploy.sh` fail on uncommitted changes to source and lockfiles. They do not check this on developer machines.
10. `cdk/scripts/deploy.sh` and the `cdk-install` task install the CDK dependencies with `npm ci --ignore-scripts`.
11. Bump the Node runtime pin in `mise.toml` from 24 to 26 (Active LTS), `@types/node` from `^20` to `^26` to match, and `typescript` from `~5.4` to `~5.9`, because `@types/node` 26 does not compile under TypeScript 5.4 or 5.5. Update the documents that name Node 24.
12. Run `mise run update-deps` once and commit the refreshed lockfiles, so the tests run against current versions.
13. When implementation is complete, file GitHub issues for two items outside this change: the deliberate major-version catch-up of the CDK dev dependencies (jest 29, @types/jest 29); and a known-vulnerability scan of the locked dependencies.

Deploy-visible effect: the first deploy after this change ships the versions in the refreshed `uv.lock` (item 12) instead of the versions resolved at build time. The refresh applies the cooldown, so it can select older versions than a refresh without one.

Scope note: issue #40 excludes updates to mise-managed runtimes. Item 11 is a deliberate addition by the change owner.

Out of scope:

1. Updates to mise-managed tools other than Node.
2. The exact `oscal-bindings` pin in `libs/oscal/pyproject.toml`. An `==` pin cannot move on update, so no special handling is necessary.
3. The ruff revision pinned in `.pre-commit-config.yaml`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

1. `toolchain`: adds requirements for the dependency update task (single command, no major-version moves for direct dependencies, all-or-nothing behavior, change report and gate, release cooldown, no package code during resolution, lockfile audit) and for the bundle and deploy scripts (locked versions, isolation from ambient configuration, CI checks, npm install without lifecycle scripts).

## Impact

1. New: `tools/update_deps.py` (stdlib only) and `tests/unit/tools/test_update_deps.py`; `update-deps` and `lock-audit` tasks in `mise.toml`.
2. Changed: `cdk/scripts/build-lambdas.sh` (install from the exported lock, isolation, CI check); `cdk/scripts/deploy.sh` (`--ignore-scripts`, CI check); every member `pyproject.toml` that declares a third-party dependency (bounds); the root `pyproject.toml` (`[tool.uv] exclude-newer`, a `build` dependency group); `cdk/package.json` (`@types/node`, `typescript`); `mise.toml` (Node 26, `cdk-install`, `check.depends`); `.gitignore` (`.update-deps-report.txt`).
3. Refreshed: `uv.lock`, `cdk/package-lock.json`.
4. Documents that name Node 24 or the contributor workflow: `AGENTS.md`, `DEVELOPING.md`, `openspec/config.yaml` (context text), `cdk/scripts/deploy.sh` (comment), `runbooks/deploy-alpha.md`, `docs/summary/codebase_info.md`, `docs/summary/dependencies.md`.
5. Deployed Lambdas: the dependency versions change to the refreshed lock at the next deploy. No infrastructure change.
