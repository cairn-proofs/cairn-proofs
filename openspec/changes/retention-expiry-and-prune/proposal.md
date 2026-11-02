Captured-from: #51

## Why

The specs and the code give three conflicting answers for what happens to an aged, untagged registry manifest:

1. Delete it once a WORM_Receipt confirms archival (retention-lifecycle [RETAIN-002.01], metadata-index [META-006.01]).
2. Never delete it.
3. Delete it after 90 days regardless (artifact-ingestion [INGEST-027] and the ECR lifecycle rule in `cdk/lib/constructs/repositories.ts`).

The rule that actually runs is the third one. It cannot see referrers, and every referrer the pipeline writes is untagged: the Notation signature, the WORM_Receipt, the producer attestation, and the resource referrer. ECR is the long-term verification surface (`cairn verify` reads ECR, never the index), so if an age-based untagged rule reaps referrers, every bundle older than 90 days stops verifying. On top of that, nothing in the system handles retention expiry: there is no record of when a bundle's retention ends, and no deliberate way to delete an expired bundle.

## Personas served

1. Dana — GRC Program Manager: "Set retention once (up to 99 years) and trust the system to honor it." Her frustration "artifacts that someone can edit after the fact" also covers deletion: today an expired bundle has no explicit retention end, and an unarchived one can disappear on an unattended timer.
2. Marcus — External Auditor: "Independently verify cryptographic integrity and signature status." This needs the signature and WORM_Receipt referrers to stay in ECR for the bundle's whole life.
3. Alex — Platform / Security Engineer: "Get encryption, signing, retention, and access scoping right the first time." Alex needs one retention clock and one explicit deletion path, not three dispositions that contradict each other.
4. Robin — Service Operator / SRE: the frustration "any path by which operating a deployment could alter the artifacts it holds." Deletion becomes one explicit, separately authorized, audited operator action, and no automatic deletion path remains.

## What Changes

1. **BREAKING (infrastructure):** remove the age-based untagged-manifest ECR lifecycle rule. Neither ECR nor S3 carries any lifecycle rule that expires, deletes, or archives artifacts, and untagged manifests are retained regardless of age.
2. Make ECR the long-term verification surface explicitly. A bundle's ECR manifest and referrers live as long as the bundle does. The ECR copy is never removed while any of the bundle's S3 archive object versions are under retention.
3. One retention clock for both `WORM_Enabled` modes: **Retain_Until** = the archive timestamp + the deployment's configured retention period, in days. The period is bounded from 1 day to 99 years, and the default is 7 years. Under `WORM_Enabled=true`, it is also the S3 `ObjectLockRetainUntilDate`, which stays authoritative.
4. The WORM_Receipt records `retainUntil` in both modes, so retention expiry can be derived from the data plane without reading the index.
5. Add **Prune_Candidate** eligibility: an item that is eligible for deletion. Eligibility trusts only pipeline-written S3 data and deployment coordinates, never producer-writable ECR referrers or the index alone. There are two classes:
   1. An **expired bundle**: an archived bundle whose authoritative Retain_Until has passed for every S3 object version, and none of whose S3 object versions carries an S3 Object Lock legal hold.
   2. An **unarchived manifest**: a content manifest with no referrer of any kind, no Metadata_Index row, and no S3 archive object, pushed longer ago than the configured unarchived-manifest window (default 30 days). This class also covers a referrer, or a referrers-tag-schema index, whose subject is no longer in the repository. Each absence must be proven by a positive not-found response.
6. Add the operator verb `cairn prune`. It reports every Prune_Candidate with its digest, class, and reason. It deletes only the candidates named explicitly in the same invocation, and it checks eligibility again at the moment of deletion. A bundle is the unit of deletion, in a fixed order: ECR referrers, ECR manifest, every S3 object version, and the Metadata_Index row last. Every deletion is audited.
7. Add a dedicated, per-project, MFA-gated **pruner** role, the only sanctioned grant for `cairn prune` deletions. A stack-owned repository policy on each artifact repository makes the pruner the only principal that can delete images. The prune audit trail goes to a stack-owned log group that the pruner can't erase.
8. Lock the retention controls against the Operator and the pipeline. The Operator is denied lifecycle, Object Lock, repository-policy, retention-parameter, and alarm-wiring changes. Pipeline roles lose their incidental `dynamodb:DeleteItem` and `s3:PutObjectLegalHold` grants. The retention period becomes a stack-written SSM parameter that the archive stage validates at runtime.
9. Make deletion visible. Alarms fire on image deletes, lifecycle, repository-policy, and retention-configuration changes, and trail changes. CloudTrail coverage, enforced by the deploy preflight, includes data events for archive and index deletes.
10. Make [RETAIN-002] the single statement that an index record lives until its bundle is pruned. Retire [META-006] and [INGEST-027], whose rules move to retention-lifecycle.
11. The read-only TUI shows a selected bundle's Retain_Until and whether it has passed. Both values come from the WORM_Receipt referrer the TUI already reads.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

1. `retention-lifecycle`: adds the retention clock and settings, Prune_Candidate eligibility and its authoritative inputs, the operator-initiated prune workflow with ordered deletion and audit, the no-automatic-deletion rule, and the deletion alarms. Rewrites [RETAIN-002].
2. `artifact-ingestion`: removes [INGEST-027]. Its rule moves to retention-lifecycle.
3. `worm-archive`: [WORM-005] adds `retainUntil` to the WORM_Receipt.
4. `metadata-index`: removes [META-006]. [RETAIN-002] is the single statement of record retention.
5. `access-control`: extends [ACCESS-004] with delete data events, and adds the pruner role, the pruner's retention limits, the Operator's retention-configuration denials, and the stack-owned repository policy.
6. `artifact-tui`: the detail view shows Retain_Until and whether retention has expired.

## Impact

1. **CDK:** `cdk/lib/constructs/repositories.ts` (remove the lifecycle rule, add the repository policy), `cdk/lib/constructs/iam-roles.ts` (pruner role, Operator denials, and a non-production integration-test cleanup role), `cdk/lib/constructs/lambda-functions.ts` (narrowed pipeline grants, retention SSM parameter read), the alarm rules and prune audit log group, and the stack props for the retention period and the unarchived-manifest window, with synth-time bounds checks. `cdk/scripts/deploy.sh` gains a data-event check in its CloudTrail preflight.
2. **Lambdas:** `lambdas/archive_to_s3` computes Retain_Until with day arithmetic in both modes and writes `retainUntil` to the receipt.
3. **CLI:** new `cli/cairn_cli/prune.py` and its registration in `main.py`. The TUI detail view in `cli/cairn_cli/tui/`. Integration-test cleanup in `tests/integration/conftest.py` switches to the non-production cleanup role.
4. **Operators:** the account's CloudTrail trail must log data events for the archive bucket and the index tables before the next deploy. Pruning requires an AWS profile that assumes the pruner role with MFA.
5. **Docs:** `docs/architecture-as-deployed.md` (the lifecycle rule section), `openspec/GLOSSARY.md` (Retain_Until, Prune_Candidate), and the `## Purpose` of the retention-lifecycle spec, which lists these items as deferred.
6. **Ordering dependency:** the Notation `--force-referrers-tag=false` fix (SLO-005.01 conformance) has to ship with or after this change. Today the tag-schema index is the only thing protecting signatures from the age-based rule.
7. **Out of scope:** reconciling stale registry links (#19), the integration-test cleanup gaps tracked in #35 (this change only moves the existing cleanup onto the non-production cleanup role), building legal-hold workflow (#4; this change only honors S3 legal holds that already exist), and the leap-day `RetainUntilDate` defect, which is handled by its own bugfix.
