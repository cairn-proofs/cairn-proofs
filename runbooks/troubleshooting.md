# Runbook — Troubleshooting the Artifact Pipeline

First-incident triage for the common failure modes on the `{appName}-cmh-alpha` stack — account `123456789012`, region `us-east-2`, stage `alpha`, `WORM_Enabled=false`.

## Conventions used in this runbook

- **Stack qualifier**: `cmh-alpha` (region code `cmh`, stage `alpha`). Every resource name below ends in this suffix.
- **`{appName}`**: the per-deployment application name. The default is `cairnproofs{token}`. The token freezes at first deploy — see `runbooks/deploy-alpha.md`. Substitute your deployment's actual value. ⚠️ The token deconflicts **names** only. Coexisting deployments in one account/region are **not** isolated. IAM scoping, EventBridge cross-triggering, and ECR/S3 isolation do not partition by token. When you triage, do not assume an event or invocation originated from this deployment's pipeline just because names no longer collide.
- **Log groups**: `/aws/lambda/{appName}-validate-bundle-cmh-alpha`, `/aws/lambda/{appName}-sign-bundle-cmh-alpha`, `/aws/lambda/{appName}-archive-to-s3-cmh-alpha`, `/aws/lambda/{appName}-update-metadata-cmh-alpha`. Retention is 30 days. The `cairn prune` audit trail is `/{appName}/cmh-alpha/prune-audit` and never expires.
- **Structured-log fields** emitted on every line: `level`, `message`, `traceId`, `sessionId`, `spanId`, plus pipeline fields `requestId`, `eventType`, `projectId`, `manifestDigest`, `bundleType`, `accountId`, `ecrRepository`, `durationMs`, `status`. Searches in this runbook use these field names verbatim.
- **EventBridge bus**: `{appName}-artifact-cmh-alpha` (custom bus for `artifact.*` events). The ECR push rule lives on the default bus (source `aws.ecr`).
- **Metric namespace**: `CairnProofs/cmh-alpha`. Per-handler counter metric names: `Success` / `Failure` / `DurationMs`, plus `ValidationSkipped`.
- **All `aws` commands** below assume credentials targeting account `123456789012` and `--region us-east-2`. Each command includes the region flag so you can cut and paste it directly.

The provisioned alarms all wire to the `{appName}-alarms-cmh-alpha` SNS topic. These alarms are a DLQ-depth alarm per queue, one `DeadLetterErrors` alarm per pipeline Lambda, and the archive-to-s3 duration alarm. One operator email subscribes to that topic. Confirm subscription state before you rely on email for paging:

```bash
aws sns list-subscriptions-by-topic \
  --region us-east-2 \
  --topic-arn "arn:aws:sns:us-east-2:123456789012:{appName}-alarms-cmh-alpha"
```

If `SubscriptionArn` reads `PendingConfirmation`, the subscriber never clicked the opt-in link. No email pages leave the stack. Fix that first.

---

## Pipeline alarm surface and manual failure detection

The stack provisions a DLQ-depth alarm per queue, one `DeadLetterErrors` alarm per pipeline Lambda, and an archive-to-s3 duration alarm. On this non-production deployment their SNS actions stay disabled. The alarms still evaluate and show `ALARM` state in the CloudWatch console, but they page no one. Failure detection is manual. Poll for firing alarms. Then filter the Lambda log groups for the failure messages the sections below name:

```bash
aws cloudwatch describe-alarms \
  --region us-east-2 \
  --alarm-name-prefix {appName}- \
  --state-value ALARM
```

To confirm whether the validate-bundle Lambda is silently skipping bundles, query the `ValidationSkipped` / `Failure` metrics directly:

```bash
aws cloudwatch get-metric-statistics \
  --region us-east-2 \
  --namespace CairnProofs/cmh-alpha \
  --metric-name ValidationSkipped \
  --dimensions Name=Stage,Value=alpha \
  --start-time "$(date -u -v-24H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u --date='24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 \
  --statistics Sum
```

---

## 1. Pipeline stuck on validate

The producer's `cairn push` (or raw `oras push`) returned success, but no downstream `artifact.validated` event appeared and `cairn query` returns nothing for the manifest. The validate-bundle Lambda either errored out, rejected the bundle as a permanent failure, or never received the ECR push event.

### What you'd see

- Alarm transition: none directly for the success-but-rejected path — `artifact.validation-failed` flows to SNS via the `cairn-validation-failed-cmh-alpha` EventBridge rule rather than a CloudWatch alarm. For the transient-retry-then-DLQ path: `{appName}-dlq-depth-ValidateBundleDlq-cmh-alpha` transitions to `ALARM`.
- Symptom in CloudWatch Logs (`/aws/lambda/{appName}-validate-bundle-cmh-alpha`): a `WARNING`-level record with `message: "validate-bundle permanent failure"` and a `reason` field summarizing the failure class.
- Symptom in DynamoDB (`{appName}-ArtifactMetadata-cmh-alpha`): **no row exists** for the manifest digest. By design — the bundle never crossed the validate-bundle success boundary, so update-metadata was never invoked. Distinguishes this failure mode from signing/archive failures, which DO create rows.
- Symptom for the producer: `cairn pull <digest>` against the original ECR ref succeeds (the bytes are in ECR), but `cairn query --manifest-digest <digest>` returns no rows.

### Root-cause classes

- **Missing required manifest annotation** ([INGEST-013]): the producer didn't set the content manifest's required identity annotation `dev.cairnproofs.artifact.project-id`. The failed-criteria array lists the missing field. (Artifact-kind is not a manifest annotation. The Producer_Attestation_Referrer's `artifactKind` claim carries it, and validation happens there. There is no `created` or `bundle-type` annotation requirement.)
- **Bad Config_Blob** ([INGEST-016]): config blob media type wrong, JSON malformed, or required field missing. `errorKind: invalid_config_blob` on the failure event.
- **AccountId mismatch** ([PIPELINE-013]): the `accountId` declared in Config_Blob doesn't match the account hosting the ECR repository. Usually means the producer copy-pasted a Config_Blob from another account.
- **Invalid artifact-kind claim** ([INGEST-029]): the `artifactKind` in the Producer_Attestation_Referrer claim doesn't resolve to a canonical `Artifact_Kind` via the single normalize-on-input resolver. A canonical value or a known alias resolves. Artifact-kind is not in the content manifest. The pipeline does not cross-check it across tag, annotation, or Config_Blob. Kind correctness rests solely on this fail-closed vocabulary check against the signed claim.
- **OSCAL parse failure** ([OSCAL-001]): a layer with media type `application/oscal.*` failed Pydantic v2 schema validation against the declared OSCAL schema version. `errorKind: oscal_parse_error` and a `layerDigest` field point at the offending layer.
- **Missing user identity** ([PIPELINE-010]): the EventBridge ECR push event didn't carry a `userIdentity` block. This usually means the push happened via a credential the account's CloudTrail doesn't capture. This case is extremely rare in practice and worth investigating before resolving.
- **Temporal violation** ([PIPELINE-009]): `cairn.created` is strictly after the ECR `pushedAt` timestamp, or the timestamp couldn't be parsed.
- **Transient ECR fetch failure** (no failed event emitted): the Lambda raised `EcrFetchError`, and Lambda's built-in retry started. Three retries later the message landed on the validate-bundle DLQ. Symptom is the DLQ alarm without a corresponding `artifact.validation-failed` event.

### Diagnostic commands

```bash
# 1. Was the ECR push even observed by EventBridge?
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-validate-bundle-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '"validate-bundle permanent failure"'

# 2. Pull the full failure event from the structured logs. Replace
#    sha256:abc... with the manifest digest the producer is asking about.
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-validate-bundle-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '{ $.manifestDigest = "sha256:abc..." }'

# 3. Has any artifact.validation-failed event landed in the last hour?
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-validate-bundle-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '"artifact.validation-failed"'

# 4. Is there a Metadata_Index row for this digest? (Should be absent —
#    confirms validate never succeeded.) PK = projectId for Customer_Hosted;
#    SK = manifest digest.
aws dynamodb get-item \
  --region us-east-2 \
  --table-name {appName}-ArtifactMetadata-cmh-alpha \
  --key '{"pk":{"S":"<projectId>"},"sk":{"S":"sha256:abc..."}}'

# 5. Did the Lambda's failure metric tick? Reason=Permanent => permanent
#    failure; absence + the DLQ alarm => transient classification.
aws cloudwatch get-metric-statistics \
  --region us-east-2 \
  --namespace CairnProofs/cmh-alpha \
  --metric-name Failure \
  --dimensions Name=Stage,Value=alpha Name=Reason,Value=Permanent \
  --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u --date='1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 \
  --statistics Sum
```

### Resolution playbook

1. Run command 2 with the producer's manifest digest. The `failedCriteria` array is the canonical answer. Share it back to the producer. They correct the bundle and re-push.
2. If no failure record appears, run command 3. If there's no `artifact.validation-failed` event at all, the failure was transient. Check the validate-bundle DLQ (Section 2) for the original EventBridge envelope. Inspect upstream ECR / IAM rather than the bundle contents.
3. **Permanent failures are non-redrivable.** The producer must re-push a corrected bundle. There is no retry path. The manifest's content-addressed digest changes once the producer corrects any annotation. The second push is a structurally distinct artifact.
4. If the same producer keeps tripping the same criterion, escalate to the producer team. Consider whether to tighten the `cairn push` CLI's pre-push validation. Track that as a follow-up, not an in-flight fix.

### Cross-references

- Spec: `openspec/specs/artifact-ingestion/spec.md` [INGEST-013], [INGEST-016], [INGEST-029]. `openspec/specs/processing-pipeline/spec.md` [PIPELINE-009], [PIPELINE-010], [PIPELINE-013]. `openspec/specs/oscal-extraction/spec.md` [OSCAL-001].
- Source: `lambdas/validate_bundle/cairn_lambda_validate_bundle/handler.py` (the `_handle` body + `_emit_failure_event`).

---

## 2. DLQ depth alarm firing

A pipeline Lambda exhausted its retries and SQS now holds the failed event. One alarm per queue:

| Alarm name | Source DLQ |
|---|---|
| `{appName}-dlq-depth-ValidateBundleDlq-cmh-alpha` | `{appName}-validate-bundle-dlq-cmh-alpha` |
| `{appName}-dlq-depth-SignBundleDlq-cmh-alpha` | `{appName}-sign-bundle-dlq-cmh-alpha` |
| `cairn-dlq-depth-ArchiveToS3Dlq-cmh-alpha` | `{appName}-archive-to-s3-dlq-cmh-alpha` |
| `cairn-dlq-depth-UpdateMetadataDlq-cmh-alpha` | `{appName}-update-metadata-dlq-cmh-alpha` |

All four queues use KMS encryption (service CMK) and 14-day message retention. That 14-day window is the triage budget. After 14 days SQS drops the message permanently. The underlying bundle's state remains whatever the Lambda left it in (often stuck mid-pipeline).

### What you'd see

- Alarm transition: the per-queue alarm above goes `ALARM` within ~1 minute of the first message landing.
- Symptom in CloudWatch Logs: the Lambda whose DLQ filled shows one or more `ERROR`-level lines just before the message hits the queue. Filter the matching log group on `"level":"ERROR"` to find them.
- Symptom in DynamoDB: the symptom depends on which DLQ. A validate-bundle DLQ means no row exists. The sign/archive/update DLQs mean a row exists but is stuck in a pending or partially-updated state (signingStatus=pending, archiveStatus=pending, etc.).

### Root-cause classes

- **Validate-bundle DLQ**: ECR fetch returned 5xx / 429 three times in a row. OR the ECR push event was structurally malformed (extremely rare — EventBridge schema enforcement upstream).
- **Sign-bundle DLQ**: Notation/AWS Signer call returned a transient error (`ThrottlingException`, 5xx, timeout) three times in a row. OR the SSM `timestamp-provider` parameter returned something other than `system-clock` (handler raises explicitly on non-system-clock values — see [SIGN-009] split tag).
- **Archive-to-s3 DLQ**: S3 PutObject 5xx three times in a row, OR an `IntegrityError` (covered in Section 3 — also routes here because the handler re-raises).
- **Update-metadata DLQ**: DynamoDB throttle three times in a row, OR an event with an unknown `detail-type` (handler raises `ValueError` on unknown types).

### Diagnostic commands

```bash
# 1. Inspect the queue depth and an in-flight sample without consuming.
#    `--visibility-timeout 0` makes the messages immediately re-visible
#    after the receive; substitute the queue name as needed.
aws sqs get-queue-attributes \
  --region us-east-2 \
  --queue-url "https://sqs.us-east-2.amazonaws.com/123456789012/{appName}-validate-bundle-dlq-cmh-alpha" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

aws sqs receive-message \
  --region us-east-2 \
  --queue-url "https://sqs.us-east-2.amazonaws.com/123456789012/{appName}-validate-bundle-dlq-cmh-alpha" \
  --max-number-of-messages 10 \
  --visibility-timeout 0 \
  --message-attribute-names All \
  --attribute-names All

# 2. Pull the structured error lines from the Lambda log group around the
#    DLQ-landing time. Replace the timestamp with what the alarm's
#    StateChangeTime reports.
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-validate-bundle-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '"level":"ERROR"'

# 3. List all in-ALARM CloudWatch alarms for the stack — confirms whether
#    the DLQ alarm is the only firing alarm or just one of several.
aws cloudwatch describe-alarms \
  --region us-east-2 \
  --alarm-name-prefix {appName}- \
  --state-value ALARM
```

### Resolution playbook

1. Identify which DLQ breached (the alarm name encodes it). Run command 1 with the matching `--queue-url`.
2. Inspect the message body. SQS preserves the full EventBridge envelope verbatim. `detail.manifestDigest` identifies the bundle.
3. Open the matching Lambda's log group and filter to the request that produced the failure. The structured log's `requestId` field correlates to the SQS message's `MessageAttributes.RequestID` (Lambda destination-on-failure pattern). The exception traceback identifies the root cause.
4. **If the cause is transient and the upstream condition has cleared** (Signer throttling subsided, S3 5xx storm over): redrive the message. Send the captured message to the original event source. For an `artifact.validated` event stuck on the sign-bundle DLQ, re-publish to the `{appName}-artifact-cmh-alpha` bus:
   ```bash
   aws events put-events \
     --region us-east-2 \
     --entries '[{"EventBusName":"{appName}-artifact-cmh-alpha","Source":"dev.cairnproofs.artifact","DetailType":"artifact.validated","Detail":"<the captured detail JSON>"}]'
   ```
   Then delete the original DLQ message (`aws sqs delete-message`) to acknowledge it. Lambda idempotency ([PIPELINE-007]) handles the case where the original retry actually succeeded after the DLQ landing.
5. **If the cause is permanent** (malformed envelope, bug in handler that the deploy hasn't fixed yet): drain the message after you fix the underlying bug. Accept-and-drain instead if you are abandoning the bundle. The producer drives recovery for an abandoned bundle (push again with corrected contents).
6. **Be aware of the 14-day retention budget.** If the DLQ has stayed in ALARM for ten days without progress, escalate. The messages are about to expire.

### Cross-references

- Spec: `openspec/specs/processing-pipeline/spec.md` [PIPELINE-005], [PIPELINE-006], [PIPELINE-007].
- Source: `cdk/lib/constructs/dead-letter-queues.ts` (queue config). `cdk/lib/constructs/alarms.ts` (alarm config).

---

## 3. Integrity mismatch during archive

The archive-to-s3 Lambda detected a SHA-256 disagreement between a manifest-declared blob digest and the bytes it fetched from ECR. The Lambda re-raised, and the message landed on the archive-to-s3 DLQ. Detect it via the `{appName}-dlq-depth-ArchiveToS3Dlq-cmh-alpha` DLQ-depth alarm. The ERROR log line `digest mismatch during archive` in the archive-to-s3 log group also signals it.

This is **structurally rare** in normal operation. ECR is content-addressed, so a blob fetched by digest can only mismatch for three reasons:

- (a) ECR itself returned corrupt bytes.
- (b) The digest verification function has a bug.
- (c) Someone deliberately crafted the bundle to trip the check during testing.

Treat as a security-grade signal, not a routine pipeline failure.

### What you'd see

- Detection signal: the archive-to-s3 DLQ-depth alarm (`{appName}-dlq-depth-ArchiveToS3Dlq-cmh-alpha`) fires because the handler re-raises after the mismatch.
- Symptom in CloudWatch Logs (`/aws/lambda/{appName}-archive-to-s3-cmh-alpha`): an `ERROR`-level line with `message: "digest mismatch during archive"` and three structured fields — `computed`, `expected`, `artifact` (where `artifact` is `manifest`, `config`, or `layer[sha256:...]`).
- Symptom in DynamoDB: the bundle's Metadata_Index row shows `archiveStatus: failed` after update-metadata consumes the corresponding `evidence.archive-failed` event.
- Symptom for the producer: `cairn pull <digest>` succeeds in fetching the bundle from ECR, but `cairn query` reports the bundle as `archiveStatus: failed`.

### Root-cause classes

- **ECR-side corruption** (most credible): a layer's bytes got corrupted at rest or in transit. Rare, but it's the class to rule out first.
- **A bug in the digest computation** in `libs/oci/cairn_oci/digest.py` — `compute_sha256_digest` or `verify_digest`. Less credible because every push exercises the same function.
- **Deliberate test artifact**: someone is exercising the alarm wiring with a hand-crafted malformed bundle. Confirm with the on-call team before opening a security incident.

### Diagnostic commands

```bash
# 1. Pull the exact failure record. Replace the digest if you have it from
#    the alarm's reason string.
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-archive-to-s3-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '"digest mismatch during archive"'

# 2. Re-fetch the manifest from ECR and inspect what the registry returns
#    today. Replace <project> with the ECR path the failure
#    logged in `repositoryName` and <digest> with the manifest digest.
aws ecr batch-get-image \
  --region us-east-2 \
  --registry-id 123456789012 \
  --repository-name "{appName}-artifact-cmh-alpha/<project>" \
  --image-ids imageDigest=<digest> \
  --accepted-media-types "application/vnd.oci.image.manifest.v1+json" \
  --query 'images[0].imageManifest' \
  --output text > /tmp/manifest.json

# 3. Re-compute the manifest digest locally and compare with what ECR claims.
shasum -a 256 /tmp/manifest.json | awk '{print "sha256:" $1}'
aws ecr describe-images \
  --region us-east-2 \
  --registry-id 123456789012 \
  --repository-name "{appName}-artifact-cmh-alpha/<project>" \
  --image-ids imageDigest=<digest> \
  --query 'imageDetails[0].imageDigest' \
  --output text

# 4. Re-pull a specific layer that the failure log named and re-compute.
#    Replace <blob-digest> with the value of `artifact` from the failure log
#    (strip the `layer[...]` wrapper).
aws ecr get-download-url-for-layer \
  --region us-east-2 \
  --registry-id 123456789012 \
  --repository-name "{appName}-artifact-cmh-alpha/<project>" \
  --layer-digest "<blob-digest>" \
  --query downloadUrl \
  --output text \
  | xargs curl -s -o /tmp/layer.bin
shasum -a 256 /tmp/layer.bin | awk '{print "sha256:" $1}'

# 5. Check the metric's recent series — distinguish a single-shot from
#    repeated mismatches on the same bundle (Lambda will retry once before
#    the message hits the DLQ).
aws cloudwatch get-metric-statistics \
  --region us-east-2 \
  --namespace CairnProofs/cmh-alpha \
  --metric-name IntegrityMismatch \
  --start-time "$(date -u -v-24H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u --date='24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 \
  --statistics Sum
```

### Resolution playbook

1. Run command 1 to capture the `computed`, `expected`, and `artifact` fields. Note them — they're the canonical evidence for the incident report.
2. Run command 3 (or 4 for a layer mismatch) to determine whether ECR currently returns the same digest the alarm reported. Two possibilities:
   - **Local SHA matches ECR's reported digest AND matches the manifest's declared digest**: the bundle is intact today. The mismatch was transient (network corruption between the Lambda and ECR), and a redrive will succeed. Proceed per Section 2's resolution playbook for the archive-to-s3 DLQ.
   - **Local SHA doesn't match ECR's reported digest**: ECR is serving corrupted bytes. Treat as a security incident: open an AWS support case, do not redrive, do not delete the DLQ message.
3. **Never silently delete an integrity-mismatch DLQ message.** The DLQ is the durable record of a [WORM-003.02] violation. Deletion without root-cause closure forecloses the audit trail. If you are abandoning the bundle, document the disposition in the incident write-up before draining.
4. If command 5 shows multiple `IntegrityMismatch` ticks on different manifest digests in a short window, escalate immediately. It points at a systemic ECR or KMS-key issue, not a single-bundle accident.

### Cross-references

- Spec: `openspec/specs/encryption/spec.md` [ENCRYPT-004]. `openspec/specs/worm-archive/spec.md` [WORM-003].
- Source: `lambdas/archive_to_s3/cairn_lambda_archive_to_s3/handler.py` (`_archive_blobs`). `libs/oci/cairn_oci/digest.py` (`IntegrityError`, `verify_digest`).

---

## 4. Signature verification failure on retrieval

A consumer running `cairn verify <ref>` or `cairn pull <ref>` (which verifies before extracting) sees the command fail with a Notation error. The bundle in ECR may be perfectly fine. Verification depends on the consumer's local Notation trust policy AND on the signing profile state.

### What you'd see

- Alarm transition: **none on the producer side.** This is a consumer-local failure. The pipeline has no awareness of failed verifications. If the alarm topic is silent and only one consumer is reporting failures, the issue is on their host.
- Symptom for the consumer: `cairn verify <ref>` exits with code `4` and prints either `Notation trust policy not found at <path>` or a `notation` stderr message (typically `signature verification failed: ...`).
- Symptom in CloudWatch Logs: **none** — `cairn verify` never calls into the deployed stack beyond two SSM `get-parameter` requests.

### Root-cause classes

- **Missing trust policy file** at `~/.config/notation/trustpolicy.json` (or `$XDG_CONFIG_HOME/notation/trustpolicy.json` if set). The CLI prints an install hint pointing at the SSM parameter.
- **Stale trust policy**: the consumer installed the policy before the last stack redeploy. The `trustedIdentities` field now references an old signing-profile version ARN that no longer matches the ARN the bundle carries its signature against. Re-fetch from SSM.
- **Bundle was never signed**: `sign-bundle` emitted `artifact.signing-failed` for this manifest. The Metadata_Index row shows `signingStatus: unsigned`. No signature referrer exists in ECR. Covered in Section 5.
- **Notation CLI missing on the consumer's PATH**: `cairn verify` invokes `notation` as a subprocess. A `RuntimeError: notation not available` confirms the cause.
- **Wrong stage or region in the consumer's CLI invocation**: `cairn verify --stage prod --region-code cmh ...` against a bundle signed by the `alpha` stack fails to verify. The trust policy's `registryScopes` won't match.

### Diagnostic commands

```bash
# 1. Confirm the trust policy file exists and what version ARN it pins.
cat ~/.config/notation/trustpolicy.json | python -m json.tool

# 2. Re-fetch the canonical trust policy from SSM and compare. If the
#    `trustedIdentities` value differs, the local copy is stale.
aws ssm get-parameter \
  --region us-east-2 \
  --name /{appName}/alpha/notation/trust-policy \
  --query 'Parameter.Value' \
  --output text \
  | python -m json.tool

# 3. Install or refresh the trust policy in one shot.
mkdir -p ~/.config/notation
aws ssm get-parameter \
  --region us-east-2 \
  --name /{appName}/alpha/notation/trust-policy \
  --query 'Parameter.Value' \
  --output text > ~/.config/notation/trustpolicy.json

# 4. Confirm the bundle was actually signed by checking the Metadata_Index.
#    A row with signingStatus="signed" + a signatureDigest means a
#    referrer should exist in ECR. PK = projectId (Customer_Hosted),
#    SK = manifest digest.
aws dynamodb get-item \
  --region us-east-2 \
  --table-name {appName}-ArtifactMetadata-cmh-alpha \
  --key '{"pk":{"S":"<projectId>"},"sk":{"S":"sha256:abc..."}}' \
  --projection-expression 'signingStatus, signatureDigest, signingError, archiveStatus'

# 5. Confirm a Notation signature referrer is actually attached in ECR.
#    Replace <repo> and <digest> with the bundle's path and manifest digest.
aws ecr list-images \
  --region us-east-2 \
  --registry-id 123456789012 \
  --repository-name "{appName}-artifact-cmh-alpha/<project>" \
  --filter tagStatus=ANY
# Then re-run cairn verify with NOTATION_DEBUG=1 for stderr detail:
NOTATION_DEBUG=1 cairn verify --stage alpha --region-code cmh "<ref>"

# 6. Confirm the signing profile is still ACTIVE (not revoked).
aws signer describe-signing-job \
  --region us-east-2 \
  --job-id <jobId>   # from CloudTrail event for the original sign call
aws signer list-signing-profiles \
  --region us-east-2 \
  --query 'profiles[?contains(profileName, `{appName}_artifact_pipeline_signing_cmh_alpha`)]'
```

### Resolution playbook

1. Run command 1 on the consumer's host. If the file is missing, run command 3 and retry `cairn verify`.
2. If the file is present, run command 2 and `diff` the JSON against the local copy. If `trustedIdentities` differs, the local policy is stale. Run command 3 to refresh.
3. If the trust policy is current AND verification still fails, run command 4 + 5. These confirm the bundle has both a `signingStatus: signed` row AND a referrer in ECR. If the row says `signingStatus: unsigned`, the producer never got a signature. See Section 5.
4. If everything checks out and verification still fails, run command 6. If the signing profile is missing or revoked, the consumer is observing a (rare) profile-state issue. The fix requires re-signing every affected bundle, which is a multi-bundle recovery beyond a single incident response. Escalate.
5. Common false alarm: the consumer ran `cairn verify` against an ECR ref that the producer pushed BEFORE `sign-bundle` succeeded for that manifest. Wait 30 seconds (typical pipeline latency) and retry.

### Cross-references

- Spec: `openspec/specs/signing/spec.md` [SIGN-005]. `openspec/specs/access-control/spec.md` [ACCESS-003] (consumer-side trust policy).
- Source: `cli/cairn_cli/verify.py` (verification entry point). `cdk/lib/artifact-store-stack.ts` (the rendered trust policy at the SSM path).

---

## 5. Signing failure on the producer side

The sign-bundle Lambda hit a permanent error from Notation/AWS Signer, emitted `artifact.signing-failed`, and re-raised so the message lands on the sign-bundle DLQ. Detect it via the sign-bundle DLQ-depth alarm, the `artifact.signing-failed` → SNS EventBridge rule, and the ERROR log line `permanent notation failure` in the sign-bundle log group.

### What you'd see

- Detection signal: the `artifact.signing-failed` → SNS EventBridge rule fires, and `{appName}-dlq-depth-SignBundleDlq-cmh-alpha` transitions to `ALARM`.
- Symptom in CloudWatch Logs (`/aws/lambda/{appName}-sign-bundle-cmh-alpha`): an `ERROR`-level line with `message: "permanent notation failure; marking bundle unsigned"` and `returncode` + `stderr` fields (stderr truncated to 500 chars).
- Symptom in DynamoDB: the bundle's Metadata_Index row shows `signingStatus: unsigned`. The sign-bundle Lambda sets it via a best-effort update, and update-metadata confirms it when it consumes the `artifact.signing-failed` event. The `signingError` field carries the notation stderr.
- Symptom for the consumer: `cairn pull` and `cairn verify` both fail. The bundle has no signature referrer in ECR.

### Root-cause classes

- **Signing profile revoked or in `Cancelled` state**: AWS Signer's RBAC layer rejects `signer:SignPayload`. Stderr usually names the profile and the cancellation. Check the profile status. If rotated, the stack needs a redeploy with a new profile version.
- **Permission denied at AWS Signer level**: the sign-bundle Lambda's IAM role lost the `signer:SignPayload` grant. Confirm with `aws iam simulate-principal-policy` against the role.
- **Notation CLI binary corrupted in the Lambda layer**: extremely rare. Stderr usually names the binary or `notation: command not found`. This indicates a broken layer build. Re-run `./lambda-layers/notation/build.sh` and redeploy.
- **Non-`system-clock` value in the timestamp-provider SSM parameter**: handler raises `RuntimeError("Unexpected TimestampProvider ...")` and the message goes to DLQ. Someone hand-edited the SSM parameter. Revert it.
- **AWS Signer service-side issue** (typically marked transient): `artifact.signing-failed` fires across many bundles in a short window. In that case the transient classifier may be missing a new stderr marker. Escalate.

### Diagnostic commands

```bash
# 1. Pull the exact failure record.
aws logs filter-log-events \
  --region us-east-2 \
  --log-group-name /aws/lambda/{appName}-sign-bundle-cmh-alpha \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern '"permanent notation failure"'

# 2. Confirm Metadata_Index reflects the failure. PK = projectId
#    (Customer_Hosted), SK = manifest digest.
aws dynamodb get-item \
  --region us-east-2 \
  --table-name {appName}-ArtifactMetadata-cmh-alpha \
  --key '{"pk":{"S":"<projectId>"},"sk":{"S":"sha256:abc..."}}' \
  --projection-expression 'signingStatus, signingError'

# 3. Confirm the signing profile is still active.
aws signer list-signing-profiles \
  --region us-east-2 \
  --query 'profiles[?contains(profileName, `{appName}_artifact_pipeline_signing_cmh_alpha`)].[profileName, status, profileVersion]' \
  --output table

# 4. Confirm the SSM timestamp-provider parameter still reads system-clock.
aws ssm get-parameter \
  --region us-east-2 \
  --name /{appName}/alpha/signing/timestamp-provider \
  --query 'Parameter.Value' \
  --output text

# 5. Confirm sign-bundle's Lambda role still has signer:SignPayload.
aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::123456789012:role/{appName}-cmh-alpha-SignBundleFnRole" \
  --action-names signer:SignPayload \
  --resource-arns "$(aws ssm get-parameter --region us-east-2 --name /{appName}/alpha/signing/profile-version-arn --query 'Parameter.Value' --output text)"
```

### Resolution playbook

1. Run command 1 to capture the stderr message. The notation `returncode` distinguishes Signer service errors (nonzero, stderr names the API) from Notation CLI errors (nonzero, stderr names a notation subcommand).
2. Run commands 3, 4, and 5 to rule out the three most common configuration drifts (profile state, SSM parameter, IAM grant). If any of these returns an unexpected value, fix it and proceed to step 4.
3. If the failure affects a single bundle, the producer has two recovery paths:
   - **Re-sign** by pushing again with the same Config_Blob and a bumped sequence tag. The content-addressed manifest digest will change because at least one annotation will differ, so this is structurally a fresh bundle.
   - **Accept the unsigned status** if the bundle's evidentiary value is non-load-bearing. The Metadata_Index row stays as `signingStatus: unsigned`, and auditor tooling filters on it. There's no in-place "re-sign this exact manifest" path because the pipeline already recorded the original push.
4. After fixing the underlying configuration (if any), redrive the sign-bundle DLQ per Section 2's resolution playbook. Idempotency ([PIPELINE-007]) handles the case where the original retry actually succeeded.
5. If multiple bundles failed and the root cause is a configuration drift that's now fixed, the producer must re-push each affected manifest. There is no batch re-sign path.

### Cross-references

- Spec: `openspec/specs/signing/spec.md` [SIGN-008], [SIGN-009], [SIGN-006]. `openspec/specs/processing-pipeline/spec.md` [PIPELINE-007].
- Source: `lambdas/sign_bundle/cairn_lambda_sign_bundle/handler.py` (`_handle`, `_is_transient`, `_mark_unsigned_in_metadata`).

---

## 6. DLQ delivery health (`DeadLetterErrors` alarm firing)

A `{appName}-<lambda>-dlq-delivery-errors-cmh-alpha` alarm transitioned to ALARM. This is distinct from Section 2's "DLQ depth" alarm. Depth-greater-than-zero means *something landed* in the DLQ (operator-visible failed event awaiting triage). `DeadLetterErrors` instead means *Lambda failed to write* to the DLQ at all. Lambda DROPPED the failed payload rather than queueing it. The DLQ depth stays at 0 in this failure mode, which is why a dedicated alarm exists.

Root causes are always in the DLQ-write IAM/KMS chain. One of four conditions holds:

- The Lambda execution role is missing `sqs:SendMessage` on the DLQ ARN.
- The role is missing `kms:GenerateDataKey` on the DLQ's KMS encryption key.
- The queue resource policy denies the Lambda's principal.
- Someone deleted the queue itself while the function still referenced it.

### What you'd see

- SNS notification: `Threshold Crossed: 1 datapoint [<n>] was greater than the threshold (0.0)` for one of the four `{appName}-<lambda>-dlq-delivery-errors-cmh-alpha` alarms.
- `aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name DeadLetterErrors --dimensions Name=FunctionName,Value={appName}-<lambda>-cmh-alpha` returns non-zero `Sum` for recent periods.
- The DLQ depth (`ApproximateNumberOfMessages`) does NOT increase. That is the diagnostic signature distinguishing this from a normal "DLQ depth > 0" event.
- The Lambda's own `Errors` metric will typically be non-zero across the same window. The handler is raising, Lambda's retry chain is firing, and only the final DLQ delivery is failing.

### What to do (in order)

```bash
# 1. Confirm the DeadLetterErrors signal — get the count over the last hour.
aws cloudwatch get-metric-statistics \
  --region us-east-2 \
  --namespace AWS/Lambda \
  --metric-name DeadLetterErrors \
  --dimensions Name=FunctionName,Value={appName}-<lambda>-cmh-alpha \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 \
  --statistics Sum

# 2. Confirm the queue depth + age-of-oldest-message side. Depth stays at 0
#    in the DLQ-write-failure case; age stays at 0 because nothing landed.
aws sqs get-queue-attributes \
  --region us-east-2 \
  --queue-url "$(aws sqs get-queue-url --region us-east-2 --queue-name {appName}-<lambda>-dlq-cmh-alpha --query 'QueueUrl' --output text)" \
  --attribute-names ApproximateNumberOfMessages ApproximateAgeOfOldestMessage KmsMasterKeyId Policy

# 3. Check the Lambda's execution role for the required DLQ-write actions:
#    sqs:SendMessage on the DLQ ARN, AND kms:GenerateDataKey on the DLQ's
#    KMS key (auto-grant from `queue.grantSendMessages(role)` does NOT
#    always materialize the KMS leg — that was the 2026-05-27 incident).
aws iam list-attached-role-policies \
  --role-name "$(aws lambda get-function-configuration --region us-east-2 --function-name {appName}-<lambda>-cmh-alpha --query 'Role' --output text | awk -F/ '{print $NF}')"

aws iam list-role-policies \
  --role-name "$(aws lambda get-function-configuration --region us-east-2 --function-name {appName}-<lambda>-cmh-alpha --query 'Role' --output text | awk -F/ '{print $NF}')"

# Then dump each policy and grep for kms:GenerateDataKey on the DLQ key ARN
# (the DLQ KMS key id comes from step 2's KmsMasterKeyId attribute).

# 4. Check the DLQ's KMS key policy hasn't drifted (default behavior is
#    that the role-policy's kms:GenerateDataKey is sufficient — key policy
#    grants kms:* to the account root; explicit role-policy permission is
#    required AND sufficient).
aws kms get-key-policy \
  --region us-east-2 \
  --key-id "$(aws sqs get-queue-attributes --region us-east-2 --queue-url <dlq-url> --attribute-names KmsMasterKeyId --query 'Attributes.KmsMasterKeyId' --output text)" \
  --policy-name default
```

### Resolution playbook

1. **If step 3 shows the role lacks `kms:GenerateDataKey` on the DLQ KMS key ARN** — that's the 2026-05-27 incident class. Verify the CDK source (`cdk/lib/constructs/lambda-functions.ts`) still includes the narrow grant helper for the affected Lambda. Then re-deploy. The `addScopedKmsGenerateDataKeyForSqs` helper wires validate-bundle specifically. The other three Lambdas receive the action via `addScopedKmsEncryptDecrypt`. A future refactor that drops either path trips the CDK assertion in `cdk/test/artifact-store.test.ts` (regression guard added with this change). This class should not recur silently from a source-side regression.
2. **If step 3 shows the role lacks `sqs:SendMessage`** — that's a CDK auto-grant regression. Inspect the Lambda construct in `cdk/lib/constructs/lambda-functions.ts` and confirm the `deadLetterQueue:` prop is still set. Re-deploy.
3. **If step 2 shows the DLQ's `KmsMasterKeyId` differs from the service CMK** — the queue was re-created or pointed at a different key. Confirm the stack hasn't been partially redeployed. Restore the service CMK reference.
4. **Step 4 surfaces a non-default key policy that explicitly denies the Lambda role or the relevant principal.** This means someone modified the key policy via Console or out-of-band IaC. It violates threat-model M3 (the service KMS key policy should be untouched). Revert the policy change.
5. **None of the above** — capture the relevant role-policy and key-policy outputs and escalate. This is a new failure class. It warrants its own diagnosis and a follow-up OpenSpec change to add a regression guard.

### Cross-references

- Spec: `openspec/specs/observability/spec.md` *DeadLetterErrors alarm per pipeline Lambda*.
- CDK: `cdk/lib/constructs/alarms.ts` (alarm wiring), `cdk/lib/constructs/lambda-functions.ts` (`addScopedKmsGenerateDataKeyForSqs` helper + validate-bundle call).
- CDK regression guards: `cdk/test/artifact-store.test.ts` (`validate-bundle role has narrow kms:GenerateDataKey on DLQ KMS key via SQS only`, `all four pipeline Lambda roles can produce kms:GenerateDataKey on the service KMS key`, `four DeadLetterErrors alarms wired to pipeline-alerts SNS topic`).

## 7. `cairn tui` referrer view shows "referrers unavailable"

The read-only operator TUI (`cairn tui`) renders the OCI referrers attached to a selected bundle's manifest digest — the Notation signature and the WORM receipt. It renders them by issuing `ecr:ListImageReferrers` against that digest as the subject. If the operator's own credentials lack that action, the referrer view degrades to a render-safe **"referrers unavailable"** indication. The session keeps running and does NOT crash. This is distinct from a successful read that returns **"no referrers"** (the bundle genuinely has none attached yet).

This is a **client-side read permission on the operator's own credentials** — the principal you authenticate as before you start `cairn tui`. The TUI never assumes a role of its own. This involves **no CDK/pipeline runtime change**. The pipeline Lambdas don't call `ListImageReferrers`, so this permission lives only on the human operator / read-only auditor principal.

### What you'd see

- The bundle detail view loads, but its referrer section reads "referrers unavailable" rather than listing a signature + receipt.
- With `NOTATION_DEBUG=1` or AWS SDK debug logging, the underlying call surfaces an `AccessDeniedException` on `ecr:ListImageReferrers` for the operator's principal.

### Resolution

Grant the operator (or read-only auditor) principal `ecr:ListImageReferrers` on the artifact ECR repositories. Grant it alongside the `ecr:DescribeImages` / `ecr:BatchGetImage` reads the TUI already relies on. The action is read-only. It returns nothing but the referrer index for a given subject digest and confers no mutation or signing capability.

### Cross-references

- Code: `cli/cairn_cli/tui/_readonly_client.py` (the structural read-only allowlist, including `list_image_referrers`), `cli/cairn_cli/tui/_sources.py` (`get_bundle_referrers` unavailable-vs-empty degradation).

---

## 8. Retention alarm (`[RETAIN-013]`) on the alarm topic

An SNS message starting `[RETAIN-013] <eventName> by <caller ARN>` reports a CloudTrail event that deleted an artifact image, changed its storage class, deleted an artifact repository, changed a repository's lifecycle or repository policy, changed the archive bucket's lifecycle or Object Lock configuration, wrote the retention-period parameter, updated archive-to-s3's configuration, or stopped or reconfigured a CloudTrail trail. The rules publish on denied attempts too; check `errorCode` in the message.

### What to do

1. **Image delete by the pruner role.** Match it to a `cairn prune` audit record (same digest, `step` `ecr-referrers` or `ecr-manifest`):
   ```bash
   aws logs filter-log-events --region us-east-2 \
     --log-group-name "/{appName}/cmh-alpha/prune-audit" \
     --filter-pattern '"<digest>"'
   ```
   No matching audit record means someone used the pruner session against the raw API and bypassed the eligibility checks. Treat that as a security incident: identify the session from the caller ARN and its `sourceIdentity` in CloudTrail.
2. **Image delete by the integration-test cleanup role** (non-production only) is expected during integration runs.
3. **Any other caller, or any configuration change outside a stack deploy,** is unexpected: the repository policy and the Operator DENY should have refused it. If `errorCode` is empty the change succeeded; find the principal in CloudTrail and escalate. A configuration change made by the CloudFormation execution role during a deploy is expected.

---

## 9. Deploy fails the CloudTrail data-event preflight

`deploy.sh` prints `active trails lack these delete data-event selectors:` and lists S3 and/or DynamoDB data-event selectors. Add them to an active trail as shown in `runbooks/deploy-alpha.md` ("Delete data events"), then re-run. `put-event-selectors` replaces every selector on the trail, so include the existing ones. The checker counts a selector only when it can prove the selector records the deletes: a `NotStartsWith` / `EndsWith` operator or an unrecognized field makes it ignore that selector. The override `ALLOW_MISSING_CLOUDTRAIL=1 I_ACKNOWLEDGE_AUDIT_GAP=1` is for non-production only and records `override-acknowledged` in `AuditCoverageStatus`.

---

## 10. `cairn prune` refused or failed an item

The command prints one line per named digest: `deleted`, `refused <reason>`, or `failed <digest> step <step>: <error>`, and exits non-zero unless every item was deleted.

- **`not a session of the pruner role ... and is not an IAM user`** (exit 3): the active credentials are neither a pruner-role session nor an IAM user's. Run delete mode with your designated IAM user's credentials, as in `runbooks/deploy-alpha.md` "Pruning".
- **`the MFA code must be six digits`** / **`cannot assume the pruner role ...`** (exit 3): re-run and enter the current code. An `AccessDenied` means the IAM user is not in `CAIRN_PRUNER_PRINCIPAL_ARNS`, lacks an identity-policy `sts:AssumeRole` grant, or the MFA device is wrong (pass `--mfa-serial`).
- **`pruner session expires at ...`** / **`cannot determine the pruner session's remaining lifetime`**: start a fresh session and re-run.
- **`retention in force until ...`**, **`legal hold ON ...`**, **`multiple WORM_Receipt referrers: ...`**, **`... disagrees with the S3 receipt copy`**: the item is not eligible; nothing was deleted. Multiple receipt referrers or a disagreement can mean a forged referrer; investigate before taking any other action.
- **`<check> failed: <code>`**: a read returned throttling, access denied, or another error that is not a positive not-found. The item was refused; re-run later.
- **`failed ... step s3-versions`** (or another step): the item's later steps were skipped and the `Metadata_Index` record is still present. Fix the cause and re-run `cairn prune` naming the same digest; it classifies from the S3 receipt copy and finishes the remaining steps.
- **`audit write failed`**: no further delete ran. Check `logs:PutLogEvents` on the audit group and re-run.

