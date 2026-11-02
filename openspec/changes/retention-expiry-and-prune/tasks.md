## 1. Retention settings and runtime period

- [x] 1.1 Add stack props `retentionPeriodDays` (default 2555, bounds 1–36135) and `unarchivedManifestWindowDays` (default 30, bounds 1–365) with synth-time bounds checks, and publish the retention period as an SSM `StringParameter` written only by the stack. Files: `cdk/lib/artifact-store-stack.ts`, `cdk/bin/artifact-store.ts`. Jest tests for [RETAIN-007.01] and [RETAIN-007.02], plus a happy-path synth.
- [x] 1.2 In `archive_to_s3`, read the retention period from the SSM parameter by name, with no label or version selector (M31). Fail the archive with `artifact.archive-failed`, writing nothing, when the value is missing, not a whole number, or out of bounds ([RETAIN-007.03]). Grant the Lambda `ssm:GetParameter` on that parameter only and pass the parameter name as an env var; remove `WORM_RETENTION_YEARS`. Files: `lambdas/archive_to_s3/cairn_lambda_archive_to_s3/handler.py`, `cdk/lib/constructs/lambda-functions.ts`. Tests: valid value, missing, non-integer, and out-of-bounds cases under `tests/unit/`.
- [x] 1.3 Compute Retain_Until as `archived_at + timedelta(days=period)` from a single UTC clock reading, in both `WORM_Enabled` modes. Write it to the receipt body (the ECR referrer and the S3 `worm-receipt.json` copy) as `retainUntil`, and to the receipt referrer manifest annotation `dev.cairnproofs.artifact.retain-until` in RFC 3339 UTC. Under WORM, set every `ObjectLockRetainUntilDate` to the same value. Files: `lambdas/archive_to_s3/cairn_lambda_archive_to_s3/handler.py`, `receipt.py`. Tests for [RETAIN-003.01], [RETAIN-003.02], [RETAIN-003.03] (frozen time 2028-02-29 with WORM on), [WORM-005.01], and [WORM-005.02]. If the leap-day bugfix has already landed, rebase onto it and keep its test.

## 2. Remove automatic deletion and lock the repository

- [x] 2.1 Remove the `TagStatus.UNTAGGED` lifecycle rule and its doc comment from `cdk/lib/constructs/repositories.ts`. Add jest assertions for [RETAIN-006.01]: no artifact repository has a lifecycle policy, and the archive bucket lifecycle (`cdk/lib/constructs/archive.ts`) has transitions only, with no expiration of any kind (M4).
- [x] 2.2 Add the stack-owned repository policy of [ACCESS-010] in `cdk/lib/constructs/repositories.ts`. Write every exception as an `aws:PrincipalArn` condition, never `NotPrincipal`, and take the deployment-role exception from the synthesizer's execution role (M37, T28). It denies `ecr:BatchDeleteImage` except to the pruner role and, when `isProd=false`, the integration-test cleanup role; denies `ecr:UpdateImageStorageClass` to every principal; denies `ecr:DeleteRepository`, the lifecycle-policy actions, and the repository-policy actions except to the deployment role (M36); and grants nothing cross-account. Jest tests for [ACCESS-010.01] through [ACCESS-010.04], including that `isProd=true` synthesizes no cleanup-role exception. Depends on 3.1 for the role ARNs.

## 3. IAM boundaries

- [x] 3.1 Add the per-project pruner role in `cdk/lib/constructs/iam-roles.ts` ([ACCESS-007], [ACCESS-008]):
  1. A trust policy that names designated principals (stack prop), requires `Bool aws:MultiFactorAuthPresent=true` and an `sts:SourceIdentity` bound to the caller's identity, and sets `MaxSessionDuration` to one hour (M5, M29).
  2. Deletes limited to `ecr:BatchDeleteImage` on the project repository, `s3:DeleteObjectVersion` under `{org}/{tenant}/{project}/*`, and `dynamodb:DeleteItem` on the Metadata_Index and ControlClaims under `dynamodb:LeadingKeys`.
  3. The classification reads; append-only access to the prune audit log group; no retention, legal-hold, lifecycle, SSM-write, or log-deletion permission (M7).

  4. `kms:Decrypt` in exactly two statements (Decision 8): via S3 only, limited by `kms:EncryptionContext:aws:s3:arn` to `{bucket}/{org}/{tenant}/{project}/*/worm-receipt.json`; and via DynamoDB only, limited by `kms:EncryptionContext:aws:dynamodb:tableName` to the Metadata_Index and ControlClaims names. No decrypt through ECR.

  The designated principals come from `CAIRN_PRUNER_PRINCIPAL_ARNS`, read in `cdk/bin/artifact-store.ts`, which fails synth when the list is empty or holds anything other than an IAM ARN in the deployment account. Add the non-production-only integration-test cleanup role (`isProd=false`), trusting only the principals in `CAIRN_INTEG_CLEANUP_PRINCIPAL_ARNS`, or the designated pruners when that is empty. Jest tests for [ACCESS-007.01], [ACCESS-007.03], [ACCESS-007.04], [ACCESS-008.01], and [ACCESS-008.03], plus policy-document assertions for the exact `kms:Decrypt` scope and that the archive bucket does not enable S3 Bucket Keys.
- [x] 3.2 Extend the Operator explicit DENY in `iam-roles.ts` per [ACCESS-009] (M2, M28, M31). Move `ecr:PutLifecyclePolicy` and `ecr:SetRepositoryPolicy` out of `OperatorEcrSubstrateManagement` into the DENY, and add `ecr:DeleteRepositoryPolicy`, the S3 lifecycle, Object Lock, and legal-hold actions, the SSM put, label, and delete actions on the retention parameter, `lambda:UpdateFunctionConfiguration` on archive-to-s3, and every action on the alarm path: the rule ARNs (`events:PutRule`, `PutTargets`, `RemoveTargets`, `DisableRule`, `DeleteRule`), the alarms (`cloudwatch:PutMetricAlarm`, `DisableAlarmActions`, `DeleteAlarms`), and the topic (`sns:Unsubscribe`, `SetTopicAttributes`, `DeleteTopic`, `RemovePermission`). Jest tests for [ACCESS-009.01], [ACCESS-009.02], [ACCESS-009.04], and [ACCESS-009.05]. Depends on 5.1 for the alarm resource ARNs, so implement 5.1 first or in the same task.
- [x] 3.3 Narrow the pipeline grants in `cdk/lib/constructs/lambda-functions.ts`. Replace `metadataTable.grantReadWriteData` (validate-bundle, update-metadata), `metadataTable.grantWriteData` (sign-bundle), and `controlsTable.grantReadWriteData` (update-metadata) with explicit Get/Put/Update/Query grants that exclude `DeleteItem` and `BatchWriteItem` (M14, T8). Replace `archiveBucket.grantPut(archiveToS3)` with explicit `s3:PutObject` and `s3:PutObjectRetention` (M15, T9). Jest tests for [ACCESS-007.02] and [ACCESS-009.03]. Then run the Lambda unit tests to confirm no handler depends on a removed action.

## 4. Audit and CloudTrail coverage

- [x] 4.1 Add the prune audit CloudWatch Logs group with retention set to never expire, owned by the stack, and its name published to SSM for the CLI. File: `cdk/lib/constructs/log-groups.ts`. Jest test for the never-expire retention.
- [x] 4.2 Extend the CloudTrail preflight in `cdk/scripts/deploy.sh` to require data-event selectors for archive-bucket object deletes and Metadata_Index/ControlClaims item deletes, failing with a message that names the missing selectors ([ACCESS-004.02], [ACCESS-004.03], M10). Raise the archive access-log bucket expiration in `cdk/lib/constructs/archive.ts` from 90 days to at least the retention period (M10). Add a test for the preflight's selector check if the script has a test harness; otherwise document the manual check in the runbook (task 8.2).

## 5. Alarms

- [x] 5.1 Add EventBridge rules on CloudTrail events, targeting the existing SNS notification topic, for every [RETAIN-013] event class: `BatchDeleteImage`, `UpdateImageStorageClass`, and `DeleteRepository` on the artifact namespace (M6, M36); ECR lifecycle and repository-policy changes; S3 lifecycle and Object Lock configuration changes; SSM put, label, and delete on the retention parameter; `UpdateFunctionConfiguration` on archive-to-s3 (M3); and `StopLogging`, `DeleteTrail`, `UpdateTrail`, and `PutEventSelectors` (M30). The message names the caller principal and the resource. Files: `cdk/lib/constructs/event-bus.ts`, `cdk/lib/constructs/alarms.ts`. Jest tests for [RETAIN-013.01] through [RETAIN-013.03].
- [x] 5.2 The image-count quota alarm (M20, T14) is deferred to #34 AC 5: ECR publishes no images-per-repository metric, so the alarm needs a custom metric. Until #34 ships, the "ECR image quota" check in `runbooks/deploy-alpha.md` (quota `L-03A36CE1`, image count, increase request) covers it.

## 6. `cairn prune`

- [x] 6.1 Implement the eligibility classifier in `cli/cairn_cli/_prune_eligibility.py`, with unit tests under `tests/unit/cairn_cli/` mocked at the boto3 boundary:
  1. The S3 prefix and index key are derived from deployment coordinates and the digest, with a positive-found coordinate check (M11, M32).
  2. The authoritative Retain_Until comes from the S3 `worm-receipt.json` copy and the Object Lock dates, falling back per [RETAIN-009] (M12).
  3. The item is refused on multiple receipt referrers, and the refusal reports all of them (M33), or on an ECR/S3 receipt disagreement.
  4. An expired bundle needs at least one S3 version, and delete markers are counted (M32).
  5. A legal-hold veto applies.
  6. The unarchived-manifest class requires no referrer of any kind, no index row (strongly consistent `GetItem`), and no S3 object, with absence accepted only from a positive not-found response (M13, M19).

  Cover [RETAIN-004.01] through [RETAIN-004.04], [RETAIN-008.01] through [RETAIN-008.05], [RETAIN-009.01] and [RETAIN-009.02], and [RETAIN-012.01] through [RETAIN-012.03].
- [x] 6.2 Implement the `cairn prune` command in `cli/cairn_cli/prune.py` and register it in `cli/cairn_cli/main.py`:
  1. Report-only mode by default.
  2. Explicit `--digest` selection only, rejecting wildcards and class-wide selectors.
  3. Deletion refused unless `sts:GetCallerIdentity` is a pruner-role session ([ACCESS-007]).
  4. Re-classification immediately before each item's first delete (M16), and a remaining-session-lifetime check (M35).
  5. Ordered deletion: referrers, manifest, every S3 version and delete marker, ControlClaims rows, index row (M18).
  6. Halt-and-report on a failed step, and resume from the S3 receipt copy when the ECR parts are gone (M17).
  7. A [RETAIN-011] audit record written to the audit log group before each delete step and after each outcome, with the STS caller identity, stopping on an audit-write failure (M9).

  Unit tests for [RETAIN-005.01] through [RETAIN-005.04], [RETAIN-010.01] through [RETAIN-010.03], [RETAIN-011.01], [RETAIN-011.02], and [RETAIN-002.01] and [RETAIN-002.02]. Each path gets a happy-path and an error-path test.

## 7. TUI and integration-test cleanup

- [x] 7.1 Show Retain_Until and the expired/unknown status in the TUI bundle detail view. The value comes from the WORM_Receipt referrer annotation in the existing `ListImageReferrers` response, is parsed strictly as RFC 3339 UTC, and is rendered through `_safe`. The view surfaces the `cairn prune` report command and offers no delete action. Files: `cli/cairn_cli/tui/`. Tests under `tests/unit/cairn_cli/tui/` for [TUI-016.01] through [TUI-016.03], plus an allowlist test confirming no new AWS call ([TUI-001.01]).
- [x] 7.2 Switch `BundleCleanup` in `tests/integration/conftest.py` to assume the non-production integration-test cleanup role for its deletes, so it passes the [ACCESS-010] repository policy. Update its harness-seam unit tests.

## 8. Documentation and follow-up

- [x] 8.1 Update `docs/architecture-as-deployed.md`: remove the 90-day lifecycle text and describe no expiry, the repository policy, the pruner role, the audit log group, and the alarms. Add Retain_Until and Prune_Candidate to `openspec/GLOSSARY.md`, with `Governed by` links to [RETAIN-003] and [RETAIN-004].
- [x] 8.2 Update `runbooks/deploy-alpha.md` and `runbooks/troubleshooting.md`:
  1. CloudTrail data-event setup before deploy.
  2. Pruner session setup (IAM-user credentials; `cairn prune` assumes the pruner role itself with MFA and `SourceIdentity`), with the federated-pruner MFA note (M29).
  3. A rollback warning: rolling back past this change brings back the 90-day rule, so do it only before any bundle is 90 days old, and the Notation `--force-referrers-tag=false` fix ships after this change (M21).
  4. ECR image-quota increase steps (M20).
  5. Acknowledgement required before lowering production retention (M31).
  6. Who watches organization-trail changes made from the management account (M30).
- [x] 8.3 When implementation is complete, file the follow-up GitHub issues with the capture workflow (filed as #80, #81, #82):
  1. Record validation rejections on the data plane so that rejected bundles become prunable ([design Open Question 2](design.md#open-questions)).
  2. Remove decoy WORM_Receipt referrers that make an item unprunable (T25, M33).
  3. The Notation `--force-referrers-tag=false` bugfix (SLO-005.01), which must ship after this change.
