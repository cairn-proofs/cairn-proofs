#!/usr/bin/env bash
# Build the Notation Lambda layer (mvp-evidence-store task 3.1).
#
# Downloads pinned arm64 binaries for the Notation CLI (from GitHub Releases)
# and the AWS Signer notation plugin (from the AWS-published CloudFront
# distribution), verifies them by SHA-256 AND — for the AWS Signer artifacts —
# by GPG signature against a fingerprint-pinned public key, and assembles a
# Lambda-layer-shaped zip at build/notation-layer.zip.
#
# Trust model (see runbooks/notation-layer.md for the full discussion):
#   * AWS Signer plugin: SHA-256 pins (integrity) + GPG signatures (provenance)
#     against an AWS public key whose fingerprint is pinned in this script.
#     The fingerprint pin (sourced from the AWS Signer developer guide on
#     docs.aws.amazon.com) is the load-bearing trust root — all plugin
#     artifacts come from the same CloudFront origin, so SHA-256 and GPG sigs
#     do NOT provide independent provenance.
#   * Notation CLI: SHA-256 only. The Notary Project does not publish detached
#     signatures or Sigstore attestations for the v1.3.x release artifacts.
#     Documented as a known gap; tracked for Post-MVP improvement.
#
# Lambda layer layout (extracts under /opt at runtime):
#   /opt/bin/notation                                                   (on PATH)
#   /opt/notation/plugins/com.amazonaws.signer.notation.plugin/
#     notation-com.amazonaws.signer.notation.plugin
#     LICENSE
#     THIRD_PARTY_LICENSES
#
# Consuming Lambdas MUST set XDG_CONFIG_HOME=/opt so notation discovers the
# plugin at /opt/notation/plugins/.../notation-<plugin-name>.
#
# Prerequisites: bash, curl, unzip, shasum, gpg (>=2.1), gpgv, zip. See
# runbooks/notation-layer.md for installation guidance on macOS / Linux.
#
# To bump a pinned version, update the version + SHA-256 pin pair below AND
# the matching note in runbooks/notation-layer.md (the pinned-version section
# is the contract; this script enforces it). The runbook's version-bump
# policy requires independent verification of the new bytes before the pin
# is committed — re-running this script is not sufficient.
#
# Skip GPG verification (emergency use only — supply-chain hygiene downgrade):
#   SKIP_GPG=1 I_ACKNOWLEDGE_SUPPLY_CHAIN_DOWNGRADE=1 ./build.sh
# CI environments are refused entirely (the build sets a non-zero exit) to
# prevent SKIP_GPG from becoming a routine shortcut.
#
# CONCURRENCY SAFETY (per openspec/specs/toolchain/spec.md "Tests Safe Under
# Parallel Execution"): the script acquires an exclusive `flock` on a per-
# user, non-world-writable lock file for its entire lifetime, so two
# concurrent invocations of this script (e.g., a parallel CI job that
# rebuilds the layer alongside something else that re-invokes it) cannot
# race on the `rm -rf` of `build/`. The lock releases when the script
# exits via fd 9 closure. The lock path uses XDG_RUNTIME_DIR (per-user,
# 0700 on Linux when set) with a $TMPDIR / /tmp fallback that includes a
# `$(id -u)` suffix — avoids the /tmp/ symlink-attack class where a co-
# tenant pre-creates the lock file as a symlink to a victim file (which
# `exec 9>file` would then truncate). On hosts without `flock` (e.g.,
# default macOS — install via `brew install flock`), the gate is a no-op
# + a stderr warning.

set -euo pipefail

if command -v flock >/dev/null 2>&1; then
  LOCK_DIR="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cairn-build-$(id -u)"
  mkdir -p -m 700 "${LOCK_DIR}"
  exec 9>"${LOCK_DIR}/build-notation.lock"
  flock -x 9
else
  echo "WARNING: flock unavailable; concurrent invocations may race" >&2
fi

# ── Pinned versions ──────────────────────────────────────────────────────────
NOTATION_VERSION="1.3.2"
NOTATION_SHA256="34143c7b816a50104824278080a5cf12bbf855488e003f62e6acf27ed25c423c"
NOTATION_URL="https://github.com/notaryproject/notation/releases/download/v${NOTATION_VERSION}/notation_${NOTATION_VERSION}_linux_arm64.tar.gz"

# AWS Signer plugin: CloudFront only exposes /latest/ — no versioned URL.
# Pinning happens via the SHA-256 below; if AWS rotates `latest`, this script
# fails fast and the contributor refreshes the pin per the runbook's policy
# (independent verification required).
PLUGIN_VERSION="1.0.2292"
PLUGIN_ZIP_SHA256="b416954ac98a20d71f968c4f7cd7b13905328352f43298a405a8261016a8be51"
PLUGIN_BINARY_SHA256="c65940608ff6d5c7532915ce917365c0b6edb2dd7e7e4504eac6b3170488ec77"
PLUGIN_BASE="https://d2hvyiie56hcat.cloudfront.net/linux/arm64/plugin/latest"
PLUGIN_ZIP_URL="${PLUGIN_BASE}/notation-aws-signer-plugin.zip"
PLUGIN_ZIP_SIG_URL="${PLUGIN_BASE}/notation-aws-signer-plugin.zip.sig"
PLUGIN_BINARY_SIG_URL="${PLUGIN_BASE}/notation-com.amazonaws.signer.notation.plugin.sig"

AWS_PUBLIC_KEY_URL="https://d2hvyiie56hcat.cloudfront.net/linux/public.key"
AWS_PUBLIC_KEY_SHA256="e215d69bf0bd59d7a2efe165d7fb777201364f76328702248f8b82b30ec41991"

# Per https://docs.aws.amazon.com/signer/latest/developerguide/image-signing-prerequisites.html
# This is the load-bearing trust root. Do NOT update without independent
# confirmation via AWS Security Bulletin / AWS support — a fingerprint
# mismatch at build time is a credible signal of supply-chain compromise.
AWS_SIGNER_KEY_FINGERPRINT="E84AF8A2A9B52F1F4435AE71A3B52DA65461CF90"

PLUGIN_NAME="com.amazonaws.signer.notation.plugin"
PLUGIN_BINARY="notation-${PLUGIN_NAME}"
# ─────────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
DOWNLOAD_DIR="${BUILD_DIR}/download"
STAGE_DIR="${BUILD_DIR}/stage"
OUTPUT_ZIP="${BUILD_DIR}/notation-layer.zip"

rm -rf "${BUILD_DIR}"
mkdir -p "${DOWNLOAD_DIR}" "${STAGE_DIR}/bin" "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}"

require_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required tool '$1' not found on PATH." >&2
    echo "       See runbooks/notation-layer.md for install guidance." >&2
    exit 1
  }
}
require_gpg_version() {
  # `gpg --show-keys` was introduced in gpg 2.1.23 (2017). Reject older.
  local ver major minor
  ver="$(gpg --version | head -1 | awk '{print $NF}')"
  major="${ver%%.*}"
  minor="${ver#*.}"; minor="${minor%%.*}"
  if (( major < 2 )) || { (( major == 2 )) && (( minor < 2 )); }; then
    echo "ERROR: gpg ${ver} is too old; need >= 2.2 for --show-keys." >&2
    exit 1
  fi
}
for tool in curl unzip shasum zip tar; do require_tool "$tool"; done

# ── SKIP_GPG: gated downgrade ────────────────────────────────────────────────
USE_GPG=1
if [[ "${SKIP_GPG:-0}" == "1" ]]; then
  if [[ "${I_ACKNOWLEDGE_SUPPLY_CHAIN_DOWNGRADE:-0}" != "1" ]]; then
    echo "ERROR: SKIP_GPG=1 requires I_ACKNOWLEDGE_SUPPLY_CHAIN_DOWNGRADE=1." >&2
    echo "       Skipping GPG verification downgrades the supply-chain" >&2
    echo "       posture of the layer; both env vars must be set explicitly" >&2
    echo "       so the downgrade is intentional." >&2
    exit 1
  fi
  if [[ -n "${CI:-}" || -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "ERROR: SKIP_GPG is refused in CI environments (CI / GITHUB_ACTIONS set)." >&2
    echo "       CI builds must always verify GPG signatures." >&2
    exit 1
  fi
  echo "WARNING: SKIP_GPG=1 — skipping signature verification (emergency mode)" >&2
  USE_GPG=0
else
  for tool in gpg gpgv; do require_tool "$tool"; done
  require_gpg_version
fi
# ─────────────────────────────────────────────────────────────────────────────

verify_sha256() {
  local file="$1" expected="$2"
  local actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: SHA-256 mismatch for $file" >&2
    echo "       expected: $expected" >&2
    echo "       actual:   $actual" >&2
    echo "       Either the upstream artifact was rotated or the pin is stale;" >&2
    echo "       refresh the pin intentionally — do not silently update." >&2
    exit 1
  fi
  echo "  ✓ SHA-256 ok: $(basename "$file")"
}

# Verify a signature with gpgv AND assert the signing key fingerprint matches
# the pinned fingerprint — defends against a multi-key keyring where any key
# could have produced the signature.
verify_signature() {
  local sig="$1" data="$2"
  local status
  if ! status="$(gpgv --status-fd 1 --keyring "${KEYRING_FILE}" "${sig}" "${data}" 2>/dev/null)"; then
    echo "ERROR: gpgv signature verification failed for $(basename "$data")" >&2
    exit 1
  fi
  if ! grep -qE "^\[GNUPG:\] VALIDSIG ${AWS_SIGNER_KEY_FINGERPRINT}( |$)" <<<"${status}"; then
    echo "ERROR: signature on $(basename "$data") verified, but signing key" >&2
    echo "       fingerprint does not match the pinned AWS Signer key." >&2
    echo "       Pinned: ${AWS_SIGNER_KEY_FINGERPRINT}" >&2
    echo "       Status:" >&2
    sed 's/^/         /' <<<"${status}" >&2
    exit 1
  fi
  echo "  ✓ GPG signature ok: $(basename "$data")  (signed by pinned fingerprint)"
}

echo "==> Downloading Notation CLI v${NOTATION_VERSION} (arm64) from GitHub Releases"
curl -sSfL --retry 3 -o "${DOWNLOAD_DIR}/notation.tar.gz" "${NOTATION_URL}"
verify_sha256 "${DOWNLOAD_DIR}/notation.tar.gz" "${NOTATION_SHA256}"

echo "==> Downloading AWS Signer plugin v${PLUGIN_VERSION} (arm64) from AWS CloudFront"
curl -sSfL --retry 3 -o "${DOWNLOAD_DIR}/plugin.zip" "${PLUGIN_ZIP_URL}"
verify_sha256 "${DOWNLOAD_DIR}/plugin.zip" "${PLUGIN_ZIP_SHA256}"

if [[ "${USE_GPG}" == "1" ]]; then
  echo "==> Downloading detached signatures + AWS public key for GPG verification"
  curl -sSfL --retry 3 -o "${DOWNLOAD_DIR}/plugin.zip.sig" "${PLUGIN_ZIP_SIG_URL}"
  curl -sSfL --retry 3 -o "${DOWNLOAD_DIR}/${PLUGIN_BINARY}.sig" "${PLUGIN_BINARY_SIG_URL}"
  curl -sSfL --retry 3 -o "${DOWNLOAD_DIR}/public.key" "${AWS_PUBLIC_KEY_URL}"
  verify_sha256 "${DOWNLOAD_DIR}/public.key" "${AWS_PUBLIC_KEY_SHA256}"

  # Standalone keyring file consumed by `gpgv` — agent-free path (works
  # across macOS Homebrew + Linux without touching the contributor's
  # default keyring or starting gpg-agent).
  KEYRING_FILE="${DOWNLOAD_DIR}/aws-signer.gpg"

  # Extract EVERY fingerprint from the armored key file; assert exactly one
  # and that it matches the pin. Required because `gpg --dearmor` writes
  # ALL keys from the input — a crafted file with the pinned key first +
  # an attacker key second would bypass a "first fpr only" check.
  # Portable read-into-array loop (mapfile is bash >= 4 only; macOS ships 3.2).
  key_fprs=()
  while IFS= read -r line; do
    key_fprs+=("$line")
  done < <(gpg --batch --with-colons --show-keys "${DOWNLOAD_DIR}/public.key" \
    | awk -F: '/^fpr:/ {print $10}')
  if (( ${#key_fprs[@]} != 1 )); then
    echo "ERROR: public.key contains ${#key_fprs[@]} fingerprints; expected exactly 1." >&2
    printf '       %s\n' "${key_fprs[@]}" >&2
    exit 1
  fi
  if [[ "${key_fprs[0]}" != "${AWS_SIGNER_KEY_FINGERPRINT}" ]]; then
    echo "ERROR: AWS Signer key fingerprint mismatch" >&2
    echo "       expected: ${AWS_SIGNER_KEY_FINGERPRINT}" >&2
    echo "       actual:   ${key_fprs[0]}" >&2
    echo "       Do NOT update the pin without independent confirmation" >&2
    echo "       (AWS Security Bulletin, AWS support case). A mismatch is a" >&2
    echo "       credible signal of supply-chain compromise." >&2
    exit 1
  fi
  echo "  ✓ AWS Signer public key fingerprint matches pin"

  gpg --batch --dearmor --output "${KEYRING_FILE}" "${DOWNLOAD_DIR}/public.key"

  verify_signature "${DOWNLOAD_DIR}/plugin.zip.sig" "${DOWNLOAD_DIR}/plugin.zip"
fi

echo "==> Extracting Notation CLI"
tar -xzf "${DOWNLOAD_DIR}/notation.tar.gz" -C "${DOWNLOAD_DIR}"
if [[ ! -f "${DOWNLOAD_DIR}/notation" ]]; then
  echo "ERROR: Notation tarball did not contain expected top-level 'notation' binary." >&2
  echo "       Contents:" >&2
  tar -tzf "${DOWNLOAD_DIR}/notation.tar.gz" | sed 's/^/         /' >&2
  echo "       Upstream tarball layout may have changed; investigate before bumping the pin." >&2
  exit 1
fi
mv "${DOWNLOAD_DIR}/notation" "${STAGE_DIR}/bin/notation"
chmod 755 "${STAGE_DIR}/bin/notation"

echo "==> Extracting AWS Signer plugin"
unzip -q -o "${DOWNLOAD_DIR}/plugin.zip" -d "${DOWNLOAD_DIR}/plugin"
verify_sha256 "${DOWNLOAD_DIR}/plugin/${PLUGIN_BINARY}" "${PLUGIN_BINARY_SHA256}"
mv "${DOWNLOAD_DIR}/plugin/${PLUGIN_BINARY}" "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}/${PLUGIN_BINARY}"
mv "${DOWNLOAD_DIR}/plugin/LICENSE" "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}/LICENSE"
mv "${DOWNLOAD_DIR}/plugin/THIRD_PARTY_LICENSES" "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}/THIRD_PARTY_LICENSES"
chmod 755 "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}/${PLUGIN_BINARY}"

if [[ "${USE_GPG}" == "1" ]]; then
  # Verify the extracted binary against its own detached signature.
  verify_signature "${DOWNLOAD_DIR}/${PLUGIN_BINARY}.sig" \
    "${STAGE_DIR}/notation/plugins/${PLUGIN_NAME}/${PLUGIN_BINARY}"
else
  # Sentinel — carries the downgrade evidence in the layer itself so a
  # downstream reviewer can detect that this artifact was built without
  # signature verification.
  echo "Built with SKIP_GPG=1 on $(date -u +%Y-%m-%dT%H:%M:%SZ) by $(whoami)@$(hostname)" \
    > "${STAGE_DIR}/.SKIP_GPG_USED"
fi

echo "==> Assembling Lambda layer zip"
(cd "${STAGE_DIR}" && zip -qr "${OUTPUT_ZIP}" .)

# Final report
SIZE_BYTES="$(stat -f %z "${OUTPUT_ZIP}" 2>/dev/null || stat -c %s "${OUTPUT_ZIP}")"
SIZE_MIB=$(awk -v b="${SIZE_BYTES}" 'BEGIN { printf "%.2f", b/1024/1024 }')

echo
echo "==> Build complete"
echo "    output:        ${OUTPUT_ZIP}"
echo "    size:          ${SIZE_MIB} MiB (${SIZE_BYTES} bytes)"
echo "    Notation:      v${NOTATION_VERSION} (SHA-256 pinned; no upstream signature)"
echo "    AWS Signer:    v${PLUGIN_VERSION} (SHA-256 + GPG pinned to ${AWS_SIGNER_KEY_FINGERPRINT:0:16}…)"
echo "    architecture:  arm64 / linux"
if [[ "${USE_GPG}" == "0" ]]; then
  echo "    ⚠ SKIP_GPG=1 — layer carries .SKIP_GPG_USED sentinel"
fi
echo
echo "    Lambda must set XDG_CONFIG_HOME=/opt to discover the plugin."
