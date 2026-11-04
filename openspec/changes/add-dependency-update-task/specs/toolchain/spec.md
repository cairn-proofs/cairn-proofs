## ADDED Requirements

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

`cdk/scripts/build-lambdas.sh` SHALL run uv with only PATH, HOME, TMPDIR and LANG, with `--no-config` and the default index `https://pypi.org/simple`. It SHALL install third-party packages with `--only-binary :all:`. It SHALL build workspace members with a build backend from a hashed `build` dependency group in `uv.lock`. It SHALL write the SHA-256 of `uv.lock` into `BUILD-INFO.json` in each bundle.

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
