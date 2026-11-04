# Runbook — Deploying `cmh-alpha`

First-deploy procedure for the `{appName}-cmh-alpha` stack — account `123456789012`, region `us-east-2`, stage `alpha`, `WORM_Enabled=false`.

Throughout this runbook, `{appName}` is the per-deployment application name (default `cairnproofs{token}`, e.g. `cairnproofs4af`). The first deploy generates its 3-character token once and freezes it in committed configuration. Substitute your deployment's actual value in every command.

> ⚠️ **Names deconflicted ≠ pipelines isolated.** The `{appName}` token deconflicts resource **names** only. Two deployments that coexist in one AWS account/region are **NOT isolated from each other**. Three gaps cause this:
>
> - IAM policies are not scoped to keep one deployment's principals out of the other's resources.
> - EventBridge rules can cross-trigger on the other deployment's ECR pushes.
> - The ECR/S3 namespaces share the account with no isolation boundary between them.
>
> Operators MUST NOT assume that coexisting deployments are isolated just because their names don't collide.

## What this runbook covers

This is the **first-deploy** procedure: one-time bootstrap (CDK toolkit, Notation layer, CloudTrail precondition) followed by the one-shot deploy and post-deploy verification. After you finish the bootstrap in an account, every subsequent deploy reduces to:

```bash
./cdk/scripts/deploy.sh
```

The script is idempotent. Re-runs without source changes are no-ops at the CloudFormation change-set level. This runbook documents the steps the script wraps so an operator can reason about (and debug) what's happening when something goes sideways. Troubleshooting lives in `runbooks/troubleshooting.md`.

## Prerequisites

| Requirement | Why | How |
|---|---|---|
| `mise install` complete + `uv sync` in the repo | Node 26 + Python 3.12 + `uv` + `aws` CLI + `openspec` all on PATH | See [Local development](../README.md#local-development) |
| AWS credentials for account `123456789012` | Target of the `cmh-alpha` stack | `aws sts get-caller-identity` should report this account |
| Privileges sufficient for the stack's resource surface | CloudFormation, IAM, KMS, ECR, S3, DynamoDB, Lambda, EventBridge, Signer, SNS, SQS, SSM, CloudWatch Logs | Use an admin role for first deploy |
| `gpg` 2.2+ | Build-time signature verification of the AWS Signer plugin | `brew install gnupg` (macOS) / `apt-get install gnupg` (Debian) |
| `flock` (macOS only) | Concurrency-safe build scripts. Without it the scripts warn and rely on downstream SHA verification | `brew install flock` |
| A subscriber email address | The stack subscribes one email to the alarm SNS topic | Set via CDK context `notificationEmail`. When unset (the default), the stack creates no email subscription. |
| `CAIRN_PRUNER_PRINCIPAL_ARNS` in `.env` | The designated principals that may assume the pruner role ([ACCESS-007]). Synth fails when it is empty or holds anything other than an IAM ARN in the deployment account | Comma-separated IAM user ARNs; see [Pruning](#pruning-cairn-prune) |

The mise-loaded `.env` supplies the target account and region at deploy time (`CAIRN_ALPHA_ACCOUNT` / `CAIRN_ALPHA_REGION`). Copy `.env.example` to `.env` and fill them in. The region must be one registered in `cdk/lib/region-codes.ts`. That registry derives the `{regionCode}` airport-code segment of the `Deployment_Qualifier`. An unregistered region (or an unset account/region) fails fast at synth. Each deployment is still pinned to a single region for its lifetime (changing region means standing up a new deployment).

## One-time bootstrap (per AWS account)

### 1. CDK bootstrap

The CDK CLI requires a per-account/region bootstrap stack (`CDKToolkit`). This stack holds the asset S3 bucket, ECR repo, and bootstrap IAM roles CDK uses for asset upload. Run once per account+region pair:

```bash
npx cdk bootstrap aws://123456789012/us-east-2
```

Expected output ends with `Environment aws://123456789012/us-east-2 bootstrapped`. The bootstrap stack is independent of this project's stack — it stays in place after `cdk destroy` of `cmh-alpha`.

Skip this step on re-deploy: `aws cloudformation describe-stacks --stack-name CDKToolkit --region us-east-2` confirms the bootstrap stack already exists.

### 2. Notation Lambda layer build

The `sign-bundle` Lambda mounts a layer containing the Notation CLI + AWS Signer plugin (arm64). The layer is built locally, verified by SHA-256 + GPG fingerprint pin, and consumed by CDK as a file asset:

```bash
./lambda-layers/notation/build.sh
```

The build is hermetic — it never touches the contributor's default GPG keyring. Output lands at `lambda-layers/notation/build/notation-layer.zip` (~9 MiB). See `runbooks/notation-layer.md` for the layer's trust model, pin sources, and version-bump policy.

`deploy.sh` runs this automatically when the zip is absent. Run it manually first when you want the verification output visible.

### 3. CloudTrail preflight

Both the `signing` and `access-control` capability specs require an active CloudTrail trail with management-event logging in the deployment account (`signing` [SIGN-006], `access-control` [ACCESS-004]). `deploy.sh` runs a preflight that confirms this before invoking `cdk deploy` and refuses to deploy without it.

Confirm a qualifying trail exists:

```bash
aws cloudtrail describe-trails --include-shadow-trails \
  --query 'trailList[].TrailARN' --output text
```

For each ARN returned:

```bash
aws cloudtrail get-trail-status --name <arn> --query IsLogging --output text
aws cloudtrail get-event-selectors --trail-name <arn>
```

At least one trail must have `IsLogging=true` AND `IncludeManagementEvents=true` (classic selectors) or an `eventCategory=Management` advanced selector. If no trail qualifies, provision one via the AWS console or a separate IaC pipeline before deploying.

**Delete data events ([ACCESS-004.02]).** The active trails must also record, as data events, object deletes on the archive bucket and item deletes on the `Metadata_Index` and `ControlClaims` tables. A management-only trail does not see an S3 object-version delete or a DynamoDB `DeleteItem`, which are exactly the operations `cairn prune` performs. `deploy.sh` checks this after synth (it needs the stack name) with `cdk/scripts/check_cloudtrail_data_events.py`, and fails with a list of the missing selectors. Set the selectors before the deploy. `put-event-selectors` REPLACES every selector on the trail, so the command below keeps the management-event selector:

```bash
aws cloudtrail put-event-selectors --trail-name <arn> --advanced-event-selectors '[
  {"Name": "Management events",
   "FieldSelectors": [{"Field": "eventCategory", "Equals": ["Management"]}]},
  {"Name": "Cairn archive deletes",
   "FieldSelectors": [
     {"Field": "eventCategory", "Equals": ["Data"]},
     {"Field": "resources.type", "Equals": ["AWS::S3::Object"]},
     {"Field": "readOnly", "Equals": ["false"]},
     {"Field": "resources.ARN", "StartsWith": ["arn:aws:s3:::{appName}-artifact-archive-cmh-alpha/"]}]},
  {"Name": "Cairn index deletes",
   "FieldSelectors": [
     {"Field": "eventCategory", "Equals": ["Data"]},
     {"Field": "resources.type", "Equals": ["AWS::DynamoDB::Table"]},
     {"Field": "readOnly", "Equals": ["false"]},
     {"Field": "resources.ARN", "StartsWith": ["arn:aws:dynamodb:us-east-2:123456789012:table/{appName}-cmh-alpha-"]}]}
]'
```

The tables are auto-named `{stackName}-{logicalId}-{suffix}`, so the `table/{appName}-cmh-alpha-` prefix covers both. Merge these selectors with any your trail already carries rather than dropping them. The checker accepts a selector only when every field it sets provably admits the delete; a selector with a `NotStartsWith`/`EndsWith` operator or an unknown field does not count. An `eventName` field on the index selector must admit `DeleteItem`, `BatchWriteItem`, and `TransactWriteItems`, because CloudTrail logs an item delete under the name of the API that made it. Run the check by hand with:

```bash
aws cloudtrail get-event-selectors --trail-name <arn> --output json \
  | jq -s . \
  | python3 cdk/scripts/check_cloudtrail_data_events.py \
      --bucket {appName}-artifact-archive-cmh-alpha \
      --table-arn-prefix arn:aws:dynamodb:us-east-2:123456789012:table/{appName}-cmh-alpha-
```

**Organization trails.** A trail managed from the organization's management account can be stopped or reconfigured from that account, and the deployment account's [RETAIN-013] trail-change alarm never sees those events. Name the person or team that watches organization-trail changes in the management account before relying on an organization trail for this deployment, and record them in the deployment's ownership notes.

**Override (non-production only):** if you must deploy to an account without CloudTrail coverage (e.g., a personal sandbox), invoke `deploy.sh` with both override flags set:

```bash
ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1 ./cdk/scripts/deploy.sh
```

The deploy will proceed and the stack's `AuditCoverageStatus` CloudFormation output records the gap as `override-acknowledged` so audit reviewers can detect it post-hoc. Production deployments MUST NOT use this override.

## Deploy

### One-shot deploy

After the three bootstrap steps above are complete:

```bash
./cdk/scripts/deploy.sh
```

The script:

1. Runs the CloudTrail management-event preflight (above).
2. Builds the Notation layer if `lambda-layers/notation/build/notation-layer.zip` is absent.
3. Bundles each Lambda package into `lambda-bundles/<name>/` via `cdk/scripts/build-lambdas.sh`.
4. Runs `npm ci --ignore-scripts --silent` against `cdk/package-lock.json`.
5. Runs `npx cdk synth --quiet` (typecheck + CloudFormation template generation).
6. Runs the CloudTrail delete data-event preflight (above) against the synthesized stack name.
7. Runs `npx cdk deploy --all --require-approval never` with the preflight result threaded through as a CDK context value.

First-deploy wall-clock is dominated by the CFN stack creation pass: typically 10-15 minutes for the ~80 resources in the stack. Subsequent change-set deploys are much faster.

Any additional `cdk deploy` arguments can be passed positionally — `deploy.sh` forwards `"$@"` to `cdk deploy` after its own flags.

### What CDK creates

The `{appName}-cmh-alpha` stack creates:

| Resource | Construct | Naming |
|---|---|---|
| KMS Customer Managed Key | `Encryption` | alias `alias/{appName}-service-cmh-alpha` (RETAIN) |
| 1 ECR repository | `Repositories` | `{appName}-artifact-cmh-alpha/mvp-tenant` (one per project, with artifact-kind in the image tag, not the repo path) |
| S3 archive bucket + access-log bucket | `Archive` | `{appName}-artifact-archive-cmh-alpha` + `{appName}-artifact-archive-logs-cmh-alpha` (both RETAIN) |
| DynamoDB Metadata_Index | `MetadataTable` | `{appName}-ArtifactMetadata-cmh-alpha` (RETAIN) |
| DynamoDB Controls table | `ControlsTable` | `{appName}-Controls-cmh-alpha` (RETAIN) |
| 5 SQS dead-letter queues | `DeadLetterQueues` | `{appName}-validate-bundle-dlq-cmh-alpha`, `{appName}-sign-bundle-dlq-cmh-alpha`, `{appName}-archive-to-s3-dlq-cmh-alpha`, `{appName}-update-metadata-dlq-cmh-alpha`, `{appName}-mint-tag-dlq-cmh-alpha` |
| SNS alarm topic + email subscription | `Notifications` | `{appName}-alarms-cmh-alpha` |
| CloudWatch log groups | `LogGroups` | per-Lambda, fixed retention |
| Custom EventBridge bus + 8 rules | `EventBus` | `{appName}-artifact-cmh-alpha` + `{appName}-{ecr-push,artifact-validated,artifact-signed,artifact-archived,artifact-lifecycle,validation-failed,signing-failed,archive-failed}-cmh-alpha` |
| CloudWatch alarms | `Alarms` | `{appName}-{IntegrityMismatch,SigningFailure,ArchiveFailure}-cmh-alpha` |
| AWS Signer profile | `Signing` | `{appName}_artifact_pipeline_signing_cmh_alpha` (Notation-OCI-SHA384-ECDSA, 5-year validity) |
| SSM parameters | top of `artifact-store-stack.ts` + `Signing` | `/{appName}/alpha/{registry/ecr-host, signing/profile-version-arn, signing/timestamp-provider, notation/trust-policy}` |
| Notation Lambda layer | `LambdaFunctions` | `{appName}-notation-cmh-alpha` (arm64, Python 3.12 compatible, sign-bundle only) |
| ADOT collector layer | `LambdaFunctions` | AWS-managed `aws-otel-collector-arm64-ver-0-117-0:1` (publisher acct `901920570463`), collector-only (Go binary, no Python SDK — the app installs its own TracerProvider), attached to all 5 pipeline functions, exports OTLP→X-Ray |
| 5 Lambda functions | `LambdaFunctions` | `{appName}-{validate-bundle,sign-bundle,archive-to-s3,update-metadata,mint-tag}-cmh-alpha` |
| Lambda IAM roles | per-Lambda in `LambdaFunctions` + helper templates in `IamPolicyTemplates` | least-privilege, with KMS access gated by `kms:ViaService` |

Retain-on-destroy resources (KMS CMK, both S3 buckets, both DynamoDB tables) are deliberate — losing them would render every archived bundle unreadable.

## Post-deploy verification

### Stack outputs

```bash
aws cloudformation describe-stacks \
  --stack-name {appName}-cmh-alpha \
  --region us-east-2 \
  --query 'Stacks[0].Outputs'
```

Useful output keys:

| OutputKey | Expected value |
|---|---|
| `AuditCoverageStatus` | `passed` (preflight OK), `override-acknowledged` (override used), or `unverified` (deployed without `deploy.sh`) |
| `EcrRegistryHost` | `123456789012.dkr.ecr.us-east-2.amazonaws.com` |
| `SigningProfileVersionArn` | `arn:aws:signer:us-east-2:123456789012:/signing-profiles/{appName}_artifact_pipeline_signing_cmh_alpha/<version-id>` |
| `NotationTrustPolicyParamName` | `/{appName}/alpha/notation/trust-policy` |

`unverified` indicates someone ran `cdk deploy` directly without going through `deploy.sh`. Re-deploy through the script to reconcile.

### SSM parameters

The signing timestamp-provider parameter pins the timestamp strategy:

```bash
aws ssm get-parameter \
  --name /{appName}/alpha/signing/timestamp-provider \
  --query Parameter.Value --output text
```

Must return `system-clock`. Do not edit this parameter manually.

The pre-rendered Notation trust policy is available for verifiers (CLIs, auditor laptops) to install at `~/.config/notation/trustpolicy.json`:

```bash
aws ssm get-parameter \
  --name /{appName}/alpha/notation/trust-policy \
  --query Parameter.Value --output text > ~/.config/notation/trustpolicy.json
```

### Confirm the SNS subscription

CDK creates the subscription with a `PendingConfirmation` status. The subscriber email receives a confirmation message — the recipient must click the confirmation link before any alarm notification will land in the inbox.

Confirm the subscription:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-2:123456789012:{appName}-alarms-cmh-alpha \
  --query 'Subscriptions[].SubscriptionArn'
```

A `SubscriptionArn` of `PendingConfirmation` means the link hasn't been clicked yet. Any other ARN value means the subscription is active.

### Smoke test

The smoke test exercises the validate → sign → archive → metadata pipeline against the live stack via the `pytest` integration marker:

```bash
export CAIRN_INTEGRATION_ENV=cmh-alpha
uv run pytest -m integration
```

The integration suite under `tests/integration/` runs three steps:

1. It discovers the deployed stack's outputs via the `stack_outputs` fixture in `tests/integration/conftest.py`.
2. It pushes a minimal OSCAL Assessment Results bundle through the pipeline (via the `cairn` CLI's `push` command).
3. It asserts the Metadata_Index row reaches `archiveStatus=archived` AND `signingStatus=signed` within the 10-minute SLO (`runtime-slos` [SLO-001]).

#### Target project + pre-provisioned-repo prerequisite

The whole integration suite (happy-path, failure-path, and dedup/drift tests) reuses the **single ECR repository the stack already provisions for its configured project** — `artifact-{qualifier}/{projectId}`. Under the MVP `CATEGORY_IN_TAG` layout the artifact-kind collapses into the image tag, not the repo path, so one repository per project serves all kinds. The suite **describes, never creates**. It no longer mints or force-deletes per-test repositories, and its test path needs neither `ecr:CreateRepository` nor `ecr:DeleteRepository`.

- The suite reads the target project from **`CAIRN_INTEGRATION_PROJECT_ID`**, defaulting to **`mvp-tenant`** (the stack's configured project — see `cdk/bin/artifact-store.ts`). Override it only when running against a deployment provisioned for a different project.
- That project's repository **must already exist** in the target deployment (the stack provisions it). If it is absent, the suite **skips with a clear message** rather than creating it. So run the integration gate only against a deployed stack:

```bash
export CAIRN_INTEGRATION_ENV=cmh-alpha
# Optional — defaults to mvp-tenant:
# export CAIRN_INTEGRATION_PROJECT_ID=mvp-tenant
mise run integration
```

The suite runs serially (it is NOT part of the fast `mise run check` pre-merge gate). It cleans up only the manifests, index rows, and S3 objects each test created. It leaves the shared repository itself intact.

## Onboarding federated (off-AWS) producers

Producers are location-agnostic (ADR-0019 §7). A workload on another cloud, on-prem, or hybrid pushes to ECR by authenticating through a **federated AWS principal** rather than a long-lived IAM user. The two federation paths are IAM Roles Anywhere (X.509) and OIDC web-identity (`AssumeRoleWithWebIdentity`). The IAM path scoping, CloudTrail provenance capture, and signature verification hold regardless of where the client runs. The federating principal is only the auth bridge into AWS.

**Attribution granularity is an onboarding responsibility the store cannot enforce.** A federated push lands in CloudTrail as the *assumed-role session*. So per-producer attribution is only as precise as the session identity the federation propagates. The pipeline records whatever `userIdentity` the push event carries — it never synthesizes a principal. A federation that omits a stable session identity yields a coarse, shared-session `authenticatedPushPrincipal`. That is a federation-side gap, not a store defect. Close it at onboarding by configuring the trust so each session carries a producer-specific identity:

- Set **`sts:SourceIdentity`** (allow `sts:SetSourceIdentity` in the role trust policy). AWS then stamps the originating identity on every downstream CloudTrail event, and that identity stays immutable for the session's lifetime.
- Propagate **session tags** (`sts:TagSession`) for the producing tenant/project wherever the upstream IdP can assert them.
- Scope each federated role to a single `{org-id}/{tenant-id}/{project-id}` ECR path prefix, exactly as for an in-account `Artifact_Producer`.

Before relying on a federated push's attribution, confirm post-onboarding that it records the expected `userIdentity` (a stable `sourceIdentity`, not a bare session name). See `runbooks/troubleshooting.md` §1 (missing/coarse user identity).

## Tearing down (non-production only)

### Pre-teardown checklist (hard-cut redeploy)

A hard-cut redeploy tears the stack down. It then redeploys under the `{org-id}/{tenant-id}/{project-id}` path/key model. The cut uses no migration and no dual-path. Teardown deletes the alpha corpus, so confirm both preconditions **before** running any destroy command below:

1. **`WORM_Enabled=false` on the alpha.** With Object Lock Compliance active, archived objects cannot be deleted or rewritten and the cut would strand them. The stack header above records `WORM_Enabled=false`. Re-confirm against the deployed bucket before teardown:
   ```bash
   aws s3api get-object-lock-configuration \
     --bucket cairnproofs4af-artifact-archive-cmh-alpha \
     --region us-east-2
   ```
   A `NoSuchObjectLockConfiguration` error (or no `ObjectLockEnabled: Enabled`) confirms Object Lock is off and teardown is safe. Any active retention mode means **STOP** — a `WORM_Enabled=true` deployment MUST NOT be hard-cut.
2. **Capture the frozen `appName` token.** The redeploy MUST reuse the same token so resource names, SSM paths, and the ECR namespace line up across the cut. Record it before teardown and preserve `cdk/app-config.json` unchanged through the redeploy:
   ```bash
   jq -r '.appNameToken' cdk/app-config.json   # → 4af  (appName = cairnproofs4af)
   ```
   Confirm the value matches the deployed stack's `{appName}` (`cairnproofs4af-cmh-alpha`) before destroying.

Proceed with the teardown only after you confirm both preconditions.

```bash
npx cdk destroy --all
```

CFN leaves behind the five `RemovalPolicy.RETAIN` resources (KMS CMK, both S3 buckets, both DynamoDB tables). To fully clean up after teardown:

```bash
# Empty + delete the S3 buckets (Object Lock not in play because WORM_Enabled=false).
aws s3 rb s3://{appName}-artifact-archive-cmh-alpha --force
aws s3 rb s3://{appName}-artifact-archive-logs-cmh-alpha --force

# Delete the DynamoDB tables.
aws dynamodb delete-table --table-name {appName}-ArtifactMetadata-cmh-alpha
aws dynamodb delete-table --table-name {appName}-Controls-cmh-alpha

# Schedule the KMS CMK for deletion (7-30 day waiting period required).
KEY_ID=$(aws kms describe-key --key-id alias/{appName}-service-cmh-alpha \
  --query KeyMetadata.KeyId --output text)
aws kms schedule-key-deletion --key-id "${KEY_ID}" --pending-window-in-days 7
```

The stack-owned repository policy ([ACCESS-010]) denies `ecr:DeleteRepository` to every principal except the stack's CloudFormation execution role, so a manual `aws ecr delete-repository` fails with `AccessDenied`. On this non-production stack the repository is `DESTROY` + `emptyOnDelete`, and `cdk destroy` removes it through the execution role. If a repository is left behind, redeploy and destroy the stack again rather than editing its policy by hand.

Never run any of this against a production deployment. The retain policies exist precisely to make "delete the stack" a recoverable action.

## Retention settings

The stack props `retentionPeriodDays` (default 2,555 = 7 years, bounds 1–36,135) and `unarchivedManifestWindowDays` (default 30, bounds 1–365) are checked at synth ([RETAIN-007]). Override them with `cdk --context retentionPeriodDays=N` / `unarchivedManifestWindowDays=N` (pass them through `deploy.sh`). The stack publishes both to SSM under `/{appName}/alpha/retention/`; archive-to-s3 reads the period by name on every archive and fails the archive closed when it is missing or invalid. Only the stack deployment writes the parameter; the Operator is denied every write, and any write raises a [RETAIN-013] alarm.

**Lowering the retention period on a production stack requires a recorded acknowledgement before the deploy.** A shorter period shortens Retain_Until (and, under `WORM_Enabled=true`, the Object Lock date) of every bundle archived afterwards, and `cairn prune` can then delete those bundles sooner. Bundles already archived keep the Retain_Until written in their receipts. Get written sign-off from the deployment's retention owner naming the old and new values and the reason, attach it to the change record, then deploy. A non-production stack may use short periods (for example `retentionPeriodDays=1`) to exercise expiry.

## ECR image quota

Nothing expires images automatically ([RETAIN-006]), so a repository's image count only grows until an operator prunes. ECR's "Images per repository" quota (service-quotas code `L-03A36CE1`, default 100,000 in `us-east-2`) caps it; a push past the quota fails. Check the current value and the repository's image count:

```bash
aws service-quotas get-service-quota --service-code ecr --quota-code L-03A36CE1 --region us-east-2
aws ecr list-images --repository-name "{appName}-artifact-cmh-alpha/mvp-org/mvp-tenant/mvp-project" \
  --region us-east-2 --query 'length(imageIds)'
```

Request an increase well before the count reaches the quota:

```bash
aws service-quotas request-service-quota-increase --service-code ecr \
  --quota-code L-03A36CE1 --desired-value <N> --region us-east-2
```

Run `cairn prune` in report mode periodically to see how many candidates (expired bundles and junk manifests) could be removed.

## Pruning (`cairn prune`)

`cairn prune` is the only sanctioned path that deletes artifacts ([RETAIN-005]). Report mode needs only read access:

```bash
cairn prune --target mvp-org/mvp-tenant/mvp-project
```

Deleting requires a session of the project's pruner role `{appName}-pruner-cmh-alpha` ([ACCESS-007]). Its trust policy admits only the IAM users in `CAIRN_PRUNER_PRINCIPAL_ARNS` (synth rejects role ARNs), requires MFA, requires `sts:SourceIdentity` equal to the caller's IAM user name, and caps sessions at one hour.

Run delete mode with your IAM user's own credentials. `cairn prune` makes the `sts:AssumeRole` call itself: it prompts for the MFA code, then sends your MFA device ARN, the code, and `SourceIdentity` set to your IAM user name. Do not use a profile with `role_arn`: botocore's profile assume-role provider does not send a source identity, so the trust policy denies it.

```bash
AWS_PROFILE=<your IAM-user profile> cairn prune --target mvp-org/mvp-tenant/mvp-project \
  --digest sha256:<64-hex> [--digest sha256:<64-hex> ...]
# MFA code for arn:aws:iam::123456789012:mfa/<your-iam-user>: ******
```

The MFA device defaults to `arn:aws:iam::<account>:mfa/<your-iam-user>`. Pass `--mfa-serial <arn>` when your device has a different name. Credentials that already are a pruner-role session (for example from a `credential_process` helper that sets the source identity) are used as they are, with no prompt.

The command refuses to delete unless `sts:GetCallerIdentity` is a session of the pruner role, refuses an item when the session has less than five minutes left, re-checks every named digest immediately before its first delete, and writes an audit record to `/{appName}/cmh-alpha/prune-audit` before each delete step and after each outcome. Every image delete also raises a [RETAIN-013] alarm.

**Federated pruners.** Only IAM-user credentials can obtain a pruner session. A federated (IdP / SSO) principal has no `aws:username` and no IAM MFA device, so it fails the trust conditions. Do not relax the `aws:MultiFactorAuthPresent` or `sts:SourceIdentity` condition to admit one. Using a federated pruner needs a recorded design decision that substitutes IdP-enforced MFA and an IdP-supplied source identity, made through the OpenSpec flow.

**Integration-test cleanup.** On this non-production stack the integration harness deletes its test images through `{appName}-integ-cleanup-cmh-alpha` (ARN in SSM `/{appName}/alpha/integration/cleanup-role-arn`). The role trusts only the principals in `CAIRN_INTEG_CLEANUP_PRINCIPAL_ARNS` (IAM user or role ARNs in the account), or the designated pruners when that variable is unset. It never trusts the account root. Run the integration suite as one of those principals; it also needs an identity-policy `sts:AssumeRole` grant on the role. A production stack has no such role.

**Non-production teardown.** The repository uses `emptyOnDelete`, so `cdk destroy` deletes the remaining images as the CloudFormation execution role. On a non-production stack that role is an exception to the image-delete deny of [ACCESS-010]. A production stack retains its repository and has no such exception.

## Rollback warning

Rolling the stack back past this change restores the age-based ECR lifecycle rule that expires untagged images after 90 days. Every referrer (signatures, WORM_Receipt, Producer_Attestation_Referrer) is untagged, so that rule would reap them and every bundle older than 90 days would stop verifying. Roll back only while no bundle has been in the repository for 90 days. The Notation `--force-referrers-tag=false` fix must ship after this change, never before: until this change is deployed, the tagged referrers-schema index is the only thing that protects signatures from the 90-day rule.

## Troubleshooting

Failure-mode triage, recovery procedures, and known-symptoms catalog live in `runbooks/troubleshooting.md`. That runbook documents the common first-deploy failures (CloudTrail preflight, Notation layer build, `cdk bootstrap` missing, SNS confirmation pending).

## Version-bump policy

Pinned dependencies for this stack live in three places and follow the project-wide "pins are the contract" stance from `runbooks/notation-layer.md`:

| Pin | Source of truth | Bump procedure |
|---|---|---|
| Notation CLI + AWS Signer plugin | `lambda-layers/notation/build.sh` (versions + SHA-256 + GPG fingerprint) | See "Version-bump policy" in `runbooks/notation-layer.md` — requires independent verification before commit |
| ADOT (AWS Distro for OpenTelemetry) collector-only arm64 managed layer — pinned at `aws-otel-collector-arm64-ver-0-117-0:1` (publisher account `901920570463`) | `cdk/lib/constructs/lambda-functions.ts` (`ADOT_COLLECTOR_ARM64_LAYER_VERSION`) | Bump the constant to the new collector-layer name/version, re-run `mise run check`, commit. Before deploy, confirm the new version exists in `us-east-2` (`aws lambda get-layer-version-by-arn`). Synth passes regardless, but deploy fails fast at layer resolution if the ARN is unavailable in-region. The pins for the app's own OpenTelemetry SDK versions live separately in `libs/observability/pyproject.toml`. |
| CDK + npm dependencies | `cdk/package.json` + `cdk/package-lock.json` | Bump via `npm install <pkg>@<version>` in `cdk/`, re-run `mise run check`, commit the lockfile change with the source change |
| Python dependencies (Lambdas + libs) | `pyproject.toml` per workspace member, `uv.lock` at repo root | Bump via `uv add <pkg>@<version>` in the affected workspace member, re-run `mise run check`, commit the lockfile change with the source change |
| Toolchain (Node, Python, `uv`, `aws`, `openspec`, `pre-commit`) | `mise.toml` | Edit the version pin in `mise.toml`, run `mise install`, re-run `mise run check`, commit |

`mise.toml` is authoritative when pin files drift. Never bump a pin silently to make a failing build pass — investigate first.
