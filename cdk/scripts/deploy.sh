#!/usr/bin/env bash
# Deploy the Cairn Proofs CDK stack to cmh-alpha.
#
# The stack name is derived from the app (`cdk deploy --all`), so it tracks the
# appName token in cdk/app-config.json — `{appName}-cmh-alpha`, e.g.
# `cairnproofs4af-cmh-alpha`. No hardcoded stack name here.
#
# Preconditions:
#   - mise environment active (Node 26 + Python 3.12 + aws CLI + uv on PATH).
#     mise loads the gitignored root .env, which supplies the deployment
#     account and region as CAIRN_ALPHA_ACCOUNT / CAIRN_ALPHA_REGION (see
#     .env.example). The account/region are NOT hard-coded here.
#   - aws credentials configured for the CAIRN_ALPHA_ACCOUNT / CAIRN_ALPHA_REGION
#     named in that .env
#   - lambda-layers/notation/build.sh has been run (or this script runs it)
#   - The deployment account has an active CloudTrail trail with management
#     events enabled (signing [SIGN-006] + access-control [ACCESS-004]), AND
#     data events for object deletes on the archive bucket and item deletes on
#     the Metadata_Index / ControlClaims tables ([ACCESS-004.02]). This script
#     enforces both via preflight checks; the data-event check runs after synth
#     because it needs the stack name.
#   - CAIRN_PRUNER_PRINCIPAL_ARNS set in .env (the designated pruner principals,
#     [ACCESS-007]); synth fails without it.
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

# ── CI only: deploy committed lockfiles ([TOOLS-023]) ───────────────────────
# A CI checkout is clean by construction, so a difference means that an earlier CI
# step changed a lockfile or manifest. Developer machines skip this check on purpose.
if [[ -n "${CI:-}" ]]; then
  LOCKED_FILES=(uv.lock cdk/package-lock.json cdk/package.json pyproject.toml)
  git_status=0
  git -C "${REPO_ROOT}" diff --quiet HEAD -- "${LOCKED_FILES[@]}" || git_status=$?
  if [[ "${git_status}" -eq 1 ]]; then
    echo "ERROR: CI deploy with lockfiles or manifests that differ from HEAD:" >&2
    git -C "${REPO_ROOT}" diff --stat HEAD -- "${LOCKED_FILES[@]}" >&2
    exit 1
  elif [[ "${git_status}" -ne 0 ]]; then
    echo "ERROR: CI deploy could not run 'git diff' to compare the lockfiles with HEAD." >&2
    exit 1
  fi
fi

# ── CloudTrail preflight ────────────────────────────────────────────────────
# Default to "passed" (CDK reads via --context); demote to
# "override-acknowledged" if the override is set and the check fails.
CLOUDTRAIL_CHECK_RESULT="passed"
# Space-separated ARNs of trails with active management-event logging; the
# data-event preflight reads their event selectors.
ACTIVE_TRAILS=""

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
        ACTIVE_TRAILS="${ACTIVE_TRAILS} ${arn}"
      fi
    fi
  done

  if (( active < 1 )); then
    echo "    ✗ found ${trail_count} trail(s), but none have active management-event logging"
    return 1
  fi
  return 0
}

audit_override_acknowledged() {
  if [[ "${ALLOW_MISSING_CLOUDTRAIL:-0}" == "1" && "${I_ACKNOWLEDGE_AUDIT_GAP:-0}" == "1" ]]; then
    echo "    ⚠ override acknowledged; stack will deploy with AuditCoverageStatus=override-acknowledged"
    CLOUDTRAIL_CHECK_RESULT="override-acknowledged"
    return 0
  fi
  return 1
}

# Data-event preflight ([ACCESS-004.02], [ACCESS-004.03]). Needs the stack name
# (bucket and auto-named table ARNs derive from it), so it runs after synth.
cloudtrail_data_event_preflight() {
  local stack_name="$1"
  echo "==> CloudTrail data-event preflight (access-control [ACCESS-004.02])"
  # Stack name is {appName}-{qualifier}; appName carries no hyphen.
  local app_name="${stack_name%%-*}"
  local qualifier="${stack_name#*-}"
  local bucket="${app_name}-artifact-archive-${qualifier}"
  local table_prefix="arn:aws:dynamodb:${CAIRN_ALPHA_REGION}:${CAIRN_ALPHA_ACCOUNT}:table/${stack_name}-"
  local selectors
  selectors="$(mktemp)"
  {
    echo "["
    local first=1
    for arn in ${ACTIVE_TRAILS}; do
      if [[ ${first} -eq 0 ]]; then echo ","; fi
      first=0
      aws cloudtrail get-event-selectors --trail-name "${arn}" --output json 2>/dev/null || echo "{}"
    done
    echo "]"
  } > "${selectors}"
  local missing rc=0
  missing="$(python3 "${REPO_ROOT}/cdk/scripts/check_cloudtrail_data_events.py" \
    --bucket "${bucket}" --table-arn-prefix "${table_prefix}" < "${selectors}")" || rc=$?
  rm -f "${selectors}"
  if [[ ${rc} -ne 0 ]]; then
    echo "    ✗ active trails lack these delete data-event selectors:"
    echo "${missing}" | sed 's/^/        - /'
    return 1
  fi
  echo "    ✓ archive-bucket object deletes and index item deletes are logged as data events"
  return 0
}

if cloudtrail_preflight; then
  echo "    CloudTrail coverage OK"
elif ! audit_override_acknowledged; then
  echo "ERROR: CloudTrail preflight failed. The deployment account requires" >&2
  echo "       an active trail with management-event logging (signing [SIGN-006]," >&2
  echo "       access-control [ACCESS-004]). Provision one via aws-cloudtrail" >&2
  echo "       (account-level or org-level) before re-running." >&2
  echo "       To deploy anyway (non-production only), set both:" >&2
  echo "         ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1" >&2
  exit 1
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
npm ci --ignore-scripts --silent

echo "==> CDK synth (typecheck + CFN generation)"
npx cdk synth --quiet

STACK_NAME="$(npx cdk list 2>/dev/null | head -n 1)"
if [[ -z "${STACK_NAME}" ]]; then
  echo "ERROR: could not determine the stack name from 'cdk list'." >&2
  exit 1
fi
if [[ "${CLOUDTRAIL_CHECK_RESULT}" == "passed" ]] \
  && ! cloudtrail_data_event_preflight "${STACK_NAME}" \
  && ! audit_override_acknowledged; then
  echo "ERROR: CloudTrail data-event preflight failed. Add the data-event" >&2
  echo "       selectors listed above to an active trail (access-control" >&2
  echo "       [ACCESS-004.02]; see runbooks/deploy-alpha.md) before re-running." >&2
  echo "       To deploy anyway (non-production only), set both:" >&2
  echo "         ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1" >&2
  exit 1
fi

echo "==> CDK deploy"
npx cdk deploy --all --require-approval never \
  --context "cloudTrailCheck=${CLOUDTRAIL_CHECK_RESULT}" "$@"

echo "==> Done"
