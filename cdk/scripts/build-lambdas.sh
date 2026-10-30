#!/usr/bin/env bash
# Bundle each pipeline Lambda for deployment.
#
# For each lambdas/<name>/ workspace package:
#   1. `uv pip install --python-platform aarch64-manylinux_2_17
#      --python-version 3.12 --target lambda-bundles/<name> --no-editable .` —
#      copies the handler package AND all transitive deps into a Lambda-asset-
#      shaped dir, with wheels selected for the Lambda runtime (arm64 /
#      Python 3.12, Amazon Linux 2 == manylinux_2_17). Without the platform
#      pin, uv selects host-platform wheels (macOS) and compiled C extensions
#      like pydantic_core fail to import on Lambda with
#      `No module named 'pydantic_core._pydantic_core'`.
#   2. Strip Lambda-runtime-provided packages (boto3, botocore, s3transfer,
#      jmespath, urllib3, six, dateutil) to keep the asset under the
#      50 MiB zipped layer limit. The Lambda runtime ships these.
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

bundle_one_locked() {
  local lambda_name="$1"
  local src="${REPO_ROOT}/lambdas/${lambda_name}"
  local target="${BUNDLES_DIR}/${lambda_name}"

  if [[ ! -d "${src}" ]]; then
    echo "ERROR: no source dir at ${src}" >&2
    return 1
  fi

  echo "==> Bundling ${lambda_name}"
  rm -rf "${target}"
  mkdir -p "${target}"

  uv pip install \
    --python-platform aarch64-manylinux_2_17 \
    --python-version 3.12 \
    --target "${target}" \
    --no-editable \
    --reinstall \
    --quiet \
    "${src}"

  # Strip Lambda-provided packages + their dist-info dirs + any leftover bin/.
  pushd "${target}" >/dev/null
  for pkg in "${LAMBDA_PROVIDED[@]}"; do
    rm -rf "${pkg}" "${pkg}-"*".dist-info" "${pkg}_"*".dist-info" "${pkg}.py"
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
  rm -rf bin
  # Compiled artifacts (Lambda recompiles on cold start; smaller asset).
  find . -type d -name __pycache__ -prune -exec rm -rf {} +
  find . -type f -name '*.pyc' -delete
  popd >/dev/null

  # Bundle the ADOT collector config so it lands at /var/task/collector.yaml.
  # Idempotent: `cp` overwrites any prior copy; runs inside the per-Lambda
  # flock (callers reach bundle_one_locked only via run_with_lock).
  if [[ -f "${COLLECTOR_CONFIG}" ]]; then
    cp "${COLLECTOR_CONFIG}" "${target}/collector.yaml"
  else
    echo "ERROR: collector config not found at ${COLLECTOR_CONFIG}" >&2
    return 1
  fi

  local size_bytes
  size_bytes="$(du -sk "${target}" | awk '{print $1*1024}')"
  local size_mib
  size_mib="$(awk -v b="${size_bytes}" 'BEGIN { printf "%.2f", b/1024/1024 }')"
  echo "    ${target} = ${size_mib} MiB"
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
