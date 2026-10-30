#!/usr/bin/env bash
# Deploy the Cairn Proofs CDK stack to cmh-alpha.
#
# The stack name is derived from the app (`cdk deploy --all`), so it tracks the
# appName token in cdk/app-config.json — `{appName}-cmh-alpha`, e.g.
# `cairnproofs4af-cmh-alpha`. No hardcoded stack name here.
#
# Preconditions:
#   - mise environment active (Node 24 + Python 3.12 + aws CLI + uv on PATH).
#     mise loads the gitignored root .env, which supplies the deployment
#     account and region as CAIRN_ALPHA_ACCOUNT / CAIRN_ALPHA_REGION (see
#     .env.example). The account/region are NOT hard-coded here.
#   - aws credentials configured for the CAIRN_ALPHA_ACCOUNT / CAIRN_ALPHA_REGION
#     named in that .env
#   - lambda-layers/notation/build.sh has been run (or this script runs it)
#   - The deployment account has an active CloudTrail trail with management
#     events enabled (signing [SIGN-006] + access-control [ACCESS-004]). This script
#     enforces the requirement via a preflight check.
#
# CloudTrail check override (use only when knowingly deploying to a
# non-production account that lacks audit coverage):
#   ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1 ./deploy.sh
# When the override is used, the stack's AuditCoverageStatus CFN output
# records the violation as `override-acknowledged` so audit reviewers can
# detect compliance gaps post-hoc via `aws cloudformation describe-stacks`.
#
# Idempotent: re-runs without changes are no-ops at the CFN-changeset level.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(cd .. && pwd)"

# ── CloudTrail preflight ────────────────────────────────────────────────────
# Default to "passed" (CDK reads via --context); demote to
# "override-acknowledged" if the override is set and the check fails.
CLOUDTRAIL_CHECK_RESULT="passed"

cloudtrail_preflight() {
  echo "==> CloudTrail preflight (signing [SIGN-006], access-control [ACCESS-004])"

  local trail_count
  trail_count=$(aws cloudtrail describe-trails --include-shadow-trails \
    --query 'length(trailList)' --output text 2>/dev/null || echo "0")

  if [[ "${trail_count}" == "0" || "${trail_count}" == "None" ]]; then
    echo "    ✗ no CloudTrail trails found in this region/account"
    return 1
  fi

  local trail_names
  trail_names=$(aws cloudtrail describe-trails --include-shadow-trails \
    --query 'trailList[].TrailARN' --output text)
  local active=0
  for arn in ${trail_names}; do
    local logging
    logging=$(aws cloudtrail get-trail-status --name "${arn}" \
      --query 'IsLogging' --output text 2>/dev/null || echo "False")
    if [[ "${logging}" == "True" ]]; then
      local mgmt
      mgmt=$(aws cloudtrail get-event-selectors --trail-name "${arn}" \
        --query '(EventSelectors[?IncludeManagementEvents==`true`] || AdvancedEventSelectors[?contains(FieldSelectors[?Field==`eventCategory`].Equals[], `Management`)])' \
        --output text 2>/dev/null || echo "")
      if [[ -n "${mgmt}" ]]; then
        echo "    ✓ trail with active management-event logging: ${arn}"
        active=$((active + 1))
      fi
    fi
  done

  if (( active < 1 )); then
    echo "    ✗ found ${trail_count} trail(s), but none have active management-event logging"
    return 1
  fi
  return 0
}

if cloudtrail_preflight; then
  echo "    CloudTrail coverage OK"
else
  if [[ "${ALLOW_MISSING_CLOUDTRAIL:-0}" == "1" && "${I_ACKNOWLEDGE_AUDIT_GAP:-0}" == "1" ]]; then
    echo "    ⚠ override acknowledged; stack will deploy with AuditCoverageStatus=override-acknowledged"
    CLOUDTRAIL_CHECK_RESULT="override-acknowledged"
  else
    echo "ERROR: CloudTrail preflight failed. The deployment account requires" >&2
    echo "       an active trail with management-event logging (signing [SIGN-006]," >&2
    echo "       access-control [ACCESS-004]). Provision one via aws-cloudtrail" >&2
    echo "       (account-level or org-level) before re-running." >&2
    echo "       To deploy anyway (non-production only), set both:" >&2
    echo "         ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1" >&2
    exit 1
  fi
fi
# ─────────────────────────────────────────────────────────────────────────────

echo "==> Building Notation Lambda layer (idempotent)"
if [[ ! -f "${REPO_ROOT}/lambda-layers/notation/build/notation-layer.zip" ]]; then
  "${REPO_ROOT}/lambda-layers/notation/build.sh"
else
  echo "    (notation-layer.zip already present; skipping. Re-run build.sh to rebuild.)"
fi

echo "==> Bundling Lambda function packages"
"${REPO_ROOT}/cdk/scripts/build-lambdas.sh"

echo "==> npm install (cached on package-lock.json)"
npm ci --silent

echo "==> CDK synth (typecheck + CFN generation)"
npx cdk synth --quiet

echo "==> CDK deploy"
npx cdk deploy --all --require-approval never \
  --context "cloudTrailCheck=${CLOUDTRAIL_CHECK_RESULT}" "$@"

echo "==> Done"
