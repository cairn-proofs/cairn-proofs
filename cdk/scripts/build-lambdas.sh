#!/usr/bin/env bash
# Bundle each pipeline Lambda for deployment.
#
# For each lambdas/<name>/ workspace package:
#   1. `uv export --package cairn-lambda-<name> --locked ...` writes the exact
#      dependency closure that uv.lock records, in two files: the hashed
#      third-party packages, and the workspace members (local paths, no hash).
#      `--locked` fails when uv.lock is out of date with the manifests.
#   2. The third-party packages install with `uv pip install --require-hashes
#      --only-binary :all:`, with wheels selected for the Lambda runtime (arm64 /
#      Python 3.12, Amazon Linux 2 == manylinux_2_17). Without the platform pin,
#      uv selects host-platform wheels (macOS) and compiled C extensions like
#      pydantic_core fail to import on Lambda with
#      `No module named 'pydantic_core._pydantic_core'`.
#   3. The workspace members build with a build backend that comes from the
#      hashed `build` dependency group of uv.lock (`--build-constraints`).
#   4. Strip Lambda-runtime-provided packages (s3transfer, jmespath, six,
#      dateutil) to keep the asset small. The Lambda runtime ships these.
#   5. Write BUILD-INFO.json (the SHA-256 of uv.lock) and the collector config.
#
# Every uv command runs with a minimal environment (PATH, HOME, TMPDIR, LANG), so
# UV_* overrides and AWS credentials in the caller's shell never reach uv or the
# build backends. `uv pip install` also runs with --no-config and an explicit
# default index. `uv export` keeps the repository configuration, because
# `--locked` needs the [tool.uv] release cooldown to match the lock.
#
# When the CI environment variable is set to a non-empty value, the script fails
# if source or lockfiles under lambdas/ libs/ cli/ uv.lock pyproject.toml have
# uncommitted changes. Developer machines skip that check.
#
# On any failure the partly built bundle is removed.
#
# The CDK stack consumes each bundle via lambda.Code.fromAsset; the asset
# hashing means re-runs without source changes don't re-upload.
#
# CONCURRENCY SAFETY (per openspec/specs/toolchain/spec.md "Tests Safe
# Under Parallel Execution"): each per-Lambda bundle step acquires an
# exclusive flock on a per-user, non-world-writable lock file (see
# LOCK_DIR below) so two concurrent invocations of this script (e.g.,
# parallel CI jobs) cannot race on the rm-and-rebuild of the same bundle
# directory. The lock is per-Lambda, so concurrent invocations bundling
# DIFFERENT Lambdas run in parallel without serializing. On hosts without
# `flock` (e.g., default macOS — install via `brew install flock`), the
# gate is a no-op + a stderr warning and the SHA-256 verification + the
# script's `set -euo pipefail` catch any resulting corruption loudly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLES_DIR="${REPO_ROOT}/lambda-bundles"
# ADOT collector config bundled into each Lambda asset so it lands at
# /var/task/collector.yaml at runtime (pointed at by
# OPENTELEMETRY_COLLECTOR_CONFIG_URI — see cdk/lib/constructs/lambda-functions.ts).
COLLECTOR_CONFIG="${REPO_ROOT}/cdk/collector.yaml"

cd "${REPO_ROOT}"

# CI only: refuse to bundle uncommitted source or lockfiles ([TOOLS-023]). A CI
# checkout is clean by construction, so a difference means that an earlier CI step
# changed files. Developer machines skip this check on purpose.
if [[ -n "${CI:-}" ]]; then
  if ! dirty="$(git status --porcelain --untracked-files=all -- lambdas libs cli uv.lock pyproject.toml)"; then
    echo "ERROR: CI build could not run 'git status' to check for uncommitted changes." >&2
    exit 1
  fi
  if [[ -n "${dirty}" ]]; then
    echo "ERROR: CI build with uncommitted changes (lambdas/ libs/ cli/ uv.lock pyproject.toml):" >&2
    echo "${dirty}" >&2
    exit 1
  fi
fi

# Every uv command runs with a minimal environment ([TOOLS-022]): no UV_* index or
# hash overrides and no AWS credentials reach uv or the build backends it starts.
uv_clean() {
  env -i "PATH=${PATH}" "HOME=${HOME:-/}" "TMPDIR=${TMPDIR:-/tmp}" "LANG=${LANG:-C}" uv "$@"
}

# Options shared by the `uv pip install` calls. --no-config ignores user and project
# uv configuration; the index is explicit.
UV_INSTALL_COMMON=(
  --python-platform aarch64-manylinux_2_17
  --python-version 3.12
  --no-deps
  --no-config
  --default-index https://pypi.org/simple
  --quiet
)

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cairn-build-lambdas.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT
trap 'exit 1' INT TERM HUP

UV_LOCK_SHA256="$(sha256_of "${REPO_ROOT}/uv.lock")"

echo "==> Exporting hashed build constraints (build dependency group of uv.lock)"
BUILD_CONSTRAINTS="${WORK_DIR}/build-constraints.txt"
if ! uv_clean export --only-group build --no-emit-workspace --no-emit-project --locked \
    --no-header --no-annotate -o "${BUILD_CONSTRAINTS}" >/dev/null; then
  echo "ERROR: could not export the build dependency group; uv.lock may be out of date (run 'uv lock')." >&2
  exit 1
fi

# Concurrency gate — `flock` not strictly required (only matters when
# the script is invoked concurrently), but warn loudly if absent.
USE_FLOCK=1
if ! command -v flock >/dev/null 2>&1; then
  USE_FLOCK=0
  echo "WARNING: flock unavailable; concurrent invocations may race" >&2
fi

# Per-user lock directory — avoids /tmp/ symlink-attack class (a malicious
# co-tenant pre-creating /tmp/cairn-*.lock as a symlink to e.g.
# ~/.aws/credentials would have that file truncated by `exec 9>file`).
# XDG_RUNTIME_DIR is per-user and 0700 on Linux when set; falls back to
# $TMPDIR (per-user on macOS via /var/folders/.../T) then /tmp/ as a last
# resort. The $(id -u) suffix guards against same-host UID collisions in
# the fallback /tmp/ case.
LOCK_DIR="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cairn-build-$(id -u)"
mkdir -p -m 700 "${LOCK_DIR}"

run_with_lock() {
  # run_with_lock <lock-name> <cmd> [args...]
  local lock_name="$1"
  shift
  local lock_file="${LOCK_DIR}/${lock_name}.lock"
  if [[ "${USE_FLOCK}" == "1" ]]; then
    # bundle_one_locked etc. are shell functions; inherited automatically
    # by the () subshell. Don't refactor to `bash -c` without re-exporting.
    (
      flock -x 200
      "$@"
    ) 200>"${lock_file}"
  else
    "$@"
  fi
}

# Packages Lambda runtime provides — exclude from each bundle.
# boto3 + botocore are NOT stripped: the runtime's boto3 lags behind the
# ECR OCI Referrers API surface (no `list_image_referrers` attribute on
# the ECR client) that sign-bundle + archive-to-s3 depend on. Shipping
# our pinned boto3 (>=1.34) keeps the API surface stable across runtime
# updates. urllib3 is also kept (archive_to_s3/ecr_client.py imports it
# directly and the runtime ships urllib3 1.x while boto3>=1.34 wants 2.x).
# s3transfer + jmespath + dateutil + six remain stripped — they're stable
# transitive deps of boto3 with no recent API churn we depend on.
LAMBDA_PROVIDED=(
  s3transfer jmespath six dateutil
)

# Build one bundle. set -e is inactive in here (the caller tests the result), so
# every command carries an explicit check.
build_one() {
  local lambda_name="$1"
  local package="cairn-lambda-${lambda_name//_/-}"
  local src="${REPO_ROOT}/lambdas/${lambda_name}"
  local target="${BUNDLES_DIR}/${lambda_name}"
  local third_party="${WORK_DIR}/${lambda_name}.third-party.txt"
  local members="${WORK_DIR}/${lambda_name}.members.txt"

  if [[ ! -d "${src}" ]]; then
    echo "ERROR: no source dir at ${src}" >&2
    return 1
  fi

  echo "==> Bundling ${lambda_name}"
  rm -rf "${target}" || return 1
  mkdir -p "${target}" || return 1

  if ! uv_clean export --package "${package}" --locked --no-dev --no-editable \
      --no-emit-workspace --no-header --no-annotate -o "${third_party}" >/dev/null; then
    echo "ERROR: uv export failed for ${package}; uv.lock may be out of date with the manifests (run 'uv lock')." >&2
    return 1
  fi
  if ! uv_clean export --package "${package}" --locked --no-dev --no-editable \
      --only-emit-workspace --no-hashes --no-header --no-annotate -o "${members}" >/dev/null; then
    echo "ERROR: uv export failed for the workspace members of ${package}; uv.lock may be out of date (run 'uv lock')." >&2
    return 1
  fi

  if ! uv_clean pip install "${UV_INSTALL_COMMON[@]}" --target "${target}" \
      --require-hashes --only-binary :all: -r "${third_party}"; then
    echo "ERROR: installing the locked third-party packages of ${package} failed (hash mismatch or missing wheel)." >&2
    return 1
  fi
  # --no-cache: uv reuses a cached wheel of a local package while its
  # pyproject.toml is unchanged, even when its source files changed. The members
  # are this repository's own code, so they must build from the current source.
  if ! uv_clean pip install "${UV_INSTALL_COMMON[@]}" --target "${target}" \
      --no-cache --build-constraints "${BUILD_CONSTRAINTS}" -r "${members}"; then
    echo "ERROR: building the workspace members of ${package} failed." >&2
    return 1
  fi

  # Strip Lambda-provided packages + their dist-info dirs + any leftover bin/.
  (
    cd "${target}" || exit 1
    for pkg in "${LAMBDA_PROVIDED[@]}"; do
      rm -rf "${pkg}" "${pkg}-"*".dist-info" "${pkg}_"*".dist-info" "${pkg}.py" || exit 1
    done
    # NOTE: bundled OpenTelemetry is INTENTIONALLY kept. The architecture pivot
    # (design.md §Proposed Fix) switched the ADOT layer to the COLLECTOR-ONLY
    # layer (a Go binary with NO Python packages) plus manual instrumentation, so
    # there is no layer-side OTel to shadow or conflict with. We bundle our own
    # opentelemetry-{api,sdk,instrumentation,...} and the app installs its own
    # TracerProvider at runtime. (An earlier wave stripped opentelemetry here to
    # de-shadow the auto-instrumentation Python layer's older OTel — that layer is
    # gone, so that strip is reverted.)
    # uv-installed scripts dir; not useful in Lambda.
    rm -rf bin || exit 1
    # Compiled artifacts (Lambda recompiles on cold start; smaller asset).
    find . -type d -name __pycache__ -prune -exec rm -rf {} + || exit 1
    find . -type f -name '*.pyc' -delete || exit 1
  ) || return 1

  # Record which lock the bundle came from. No commit hash and no path: the CDK
  # asset hash then changes only when the lock changes.
  printf '{"uv_lock_sha256": "%s"}\n' "${UV_LOCK_SHA256}" > "${target}/BUILD-INFO.json" || return 1

  # Bundle the ADOT collector config so it lands at /var/task/collector.yaml.
  # Idempotent: `cp` overwrites any prior copy; runs inside the per-Lambda
  # flock (callers reach bundle_one_locked only via run_with_lock).
  if [[ -f "${COLLECTOR_CONFIG}" ]]; then
    cp "${COLLECTOR_CONFIG}" "${target}/collector.yaml" || return 1
  else
    echo "ERROR: collector config not found at ${COLLECTOR_CONFIG}" >&2
    return 1
  fi
  local size_bytes size_mib
  size_bytes="$(du -sk "${target}" | awk '{print $1*1024}')" || return 1
  size_mib="$(awk -v b="${size_bytes}" 'BEGIN { printf "%.2f", b/1024/1024 }')" || return 1
  echo "    ${target} = ${size_mib} MiB"
}

# Runs inside the per-Lambda flock. A failed build removes its partial bundle.
bundle_one_locked() {
  local lambda_name="$1"
  if ! build_one "${lambda_name}"; then
    rm -rf "${BUNDLES_DIR:?}/${lambda_name}"
    return 1
  fi
}
bundle_one() {
  local lambda_name="$1"
  run_with_lock "build-lambdas-${lambda_name}" bundle_one_locked "${lambda_name}"
}

for lambda in validate_bundle sign_bundle archive_to_s3 update_metadata mint_tag; do
  bundle_one "${lambda}"
done

echo
echo "==> Lambda bundles built at ${BUNDLES_DIR}/"
