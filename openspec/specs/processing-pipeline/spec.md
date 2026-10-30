# processing-pipeline Specification

## Purpose
The EventBridge-driven Lambda pipeline orchestrates evidence after it lands in ECR: validate → sign → archive → update-metadata → mint-tag. It defines per-stage retry and DLQ semantics, idempotency, permanent-vs-transient failure classification, and the ALCOA+ anchoring the validate stage performs: capturing `ecrPushedAt` and `authenticatedPushPrincipal` from EventBridge (including federated, off-AWS producers — recording what `userIdentity` contains, never synthesizing it), and cross-checking the Attestation claim's `orgId`, `tenantId`, and `projectId` exact-match — uniform across all three segments — against the IAM-enforced ECR repository path. The pipeline is the system's reliability and provenance backbone.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [PIPELINE-001] ECR PUSH event triggers validation stage

The **Processing_Pipeline** is the EventBridge-driven Lambda sequence validate → sign → archive → update-metadata → mint-tag. WHEN a successful ECR `PutImage` event is detected for a repository matching the `{appName}-artifact-{regionCode}-{stage}/` prefix, THE Processing_Pipeline SHALL trigger the validation stage via EventBridge. The validation stage is the pipeline entry point; no upstream stage exists, and no pipeline work begins until EventBridge receives an authenticated ECR PUSH event and routes it to the `validate-bundle` Lambda.

THE EventBridge trigger rule SHALL enforce the admission conditions in its own event pattern, rather than delegate them to the in-handler classifier, so a non-actionable `PutImage` never invokes `validate-bundle`. The rule SHALL match a `PutImage` event only when ALL of these hold:

1. The call **succeeded** — the CloudTrail event carries no `errorCode`.
2. The target `requestParameters.repositoryName` begins with the `{appName}-artifact-{regionCode}-{stage}/` artifact-repository prefix.
3. The AWS-authored push principal (CloudTrail `userIdentity`) is **not** one of THIS deployment's own pipeline-stage Lambda principals (signing, archive, and canonical-tag-minting).

The principal exclusion SHALL key on the principal ARN, a field the AWS-authored `userIdentity` always carries, and SHALL be **deployment-qualified by `{regionCode}-{stage}`** so the rule excludes only its own deployment's pipeline-stage principals, never a co-resident sibling deployment's principals in the same account. Conditions 1 and 2 are footgun-free: `errorCode` is present-iff-failed and `repositoryName` is present on every successful authenticated `PutImage`. Condition 3 keys on an always-present field, not an optional field whose absence would mis-evaluate. This rule-level filtering is a strictly-upstream cost and blast-radius control layered over — never a replacement for — the authoritative in-handler classifier (artifact-ingestion *Pipeline-Internal Artifact Filtering Before Bundle Validation*). It SHALL fail safe: any push the rule does not exclude degrades to the existing invoke-then-classify behavior, never to promotion.

The principal exclusion is sound because the single positive submission trigger — the Producer_Attestation_Referrer push — always comes from a **producer** principal, never a pipeline-stage role. The pipeline-stage roles issue only non-trigger pushes: the Notation signatures the signing stage attaches, the WORM_Receipt and artifact-reference resource referrers the archive stage attaches, and the canonical tag the minting stage writes. None is a submission-complete trigger, so excluding them at the rule SHALL NOT drop any push that would have been promoted.

Validation responds to the producer's **submission-complete** signal: the push of the **Producer_Attestation_Referrer**, not the content manifest. A submission is two OCI objects:

1. A content manifest — pure content identity (content-descriptive config + content layers, no producer claim).
2. An Producer_Attestation_Referrer whose `subject` is the content manifest digest and which carries the producer's compliance claim (`projectId`, `artifactKind`, `controls`, `catalogOrProfileUuid`, `assessmentPeriod`, `assessor`, `assessmentId`, `supersedes`).

A referrer cannot precede its subject — it references the content digest — so triggering on the content push would race the not-yet-existent claim. Triggering on the Producer_Attestation_Referrer push makes "submission complete" explicit. (Design Decision D5.)

WHEN the triggering `PutImage` is the Producer_Attestation_Referrer (identified by its Cairn attestation `artifactType`), THE `validate-bundle` Lambda SHALL resolve the referrer's `subject` (the content manifest) via the Referrers API and SHALL read the producer's claim from the Producer_Attestation_Referrer — NOT from the content manifest's Config_Blob. A content manifest pushed without a corresponding Producer_Attestation_Referrer is an **incomplete submission**: it SHALL NOT be validated and SHALL NOT be advanced to signing. It is never promoted and is surfaced as a "did-not-land" entry, consistent with the failed-bundle visibility model. Pipeline-internal referrers (the Notation signatures the signing stage attaches and the WORM_Receipt / artifact-reference resource referrers the archive stage attaches) are dropped **upstream at the trigger rule** by their push principal (condition 3 above); any that still reach the Lambda are filtered before bundle validation by their `config.mediaType` (see artifact-ingestion *Pipeline-Internal Artifact Filtering Before Bundle Validation*). They likewise do not trigger validation.

Before any promotion (validate → sign → archive → update-metadata → mint-tag) proceeds, THE `validate-bundle` Lambda SHALL verify that the resolved `subject` content manifest resides in the **same IAM-enforced ECR repository path** as the triggering Producer_Attestation_Referrer (identical `{account-id}`/`{project-id}` path segments). An Producer_Attestation_Referrer whose `subject` resolves to content in a different repository path is a cross-scope attestation hijack: it SHALL be treated as a permanent validation failure, emit an `artifact.validation-failed` event identifying both paths, and route to the DLQ without promotion. This scope check governs the promotion-trigger path itself, not only the producer-asserted projectId/accountId field cross-checks below (see artifact-ingestion *Producer Claim Carried as Signed Attestation Referrer* — closes threats T-1 / T-5).

#### Scenario: [PIPELINE-001.01] Producer_Attestation_Referrer pointing at foreign-scope content is rejected before promotion
- **WHEN** an authenticated `PutImage` succeeds for an Producer_Attestation_Referrer whose resolved `subject` content manifest lives in a different ECR repository path (a different `{account-id}`/`{project-id}`) than the referrer's own repository
- **THEN** the `validate-bundle` Lambda treats it as a permanent validation failure, emits an `artifact.validation-failed` event naming both repository paths, routes it to the DLQ, and does not advance it to signing, archive, or the Metadata_Index

#### Scenario: [PIPELINE-001.02] Producer_Attestation_Referrer PUSH on an artifact repository triggers validation
- **WHEN** an authenticated `PutImage` succeeds against `cairnproofs4af-artifact-pdx-prod/acct-foo/proj-bar` for an Producer_Attestation_Referrer (its Cairn attestation `artifactType`) whose `subject` is an already-pushed content manifest digest
- **THEN** EventBridge routes the event to the `validate-bundle` Lambda, the validation stage begins, and the Lambda resolves the referrer's `subject` content manifest and reads the producer's claim from the Producer_Attestation_Referrer

#### Scenario: [PIPELINE-001.03] Content-only manifest without an attestation is an incomplete submission, never promoted
- **WHEN** an authenticated `PutImage` succeeds for a content manifest (Cairn content Config_Blob media type, content layers only) and no Producer_Attestation_Referrer is ever pushed against its digest
- **THEN** the content push does not by itself trigger bundle validation, the bundle is never advanced to signing or promoted, and it is surfaced as a "did-not-land" / incomplete-submission entry rather than emitting an `artifact.validation-failed` event

#### Scenario: [PIPELINE-001.04] ECR PUSH on a non-artifact repository does not trigger the pipeline
- **WHEN** a `PutImage` succeeds against a repository whose path does not match the `{appName}-artifact-{regionCode}-{stage}/` prefix
- **THEN** the EventBridge trigger rule does not match the event and the Processing_Pipeline does not invoke the `validate-bundle` Lambda for that event

#### Scenario: [PIPELINE-001.05] A pipeline-stage push does not re-trigger validation
- **WHEN** the signing, archive, or canonical-tag-minting stage issues a `PutImage` (a Notation signature, a WORM_Receipt or artifact-reference resource referrer, or the canonical tag) under its pipeline-stage role into an artifact repository
- **THEN** the EventBridge trigger rule excludes the event by its push principal and `validate-bundle` is not invoked, so the pipeline does not re-trigger its own validation stage

#### Scenario: [PIPELINE-001.06] A failed PutImage does not trigger validation
- **WHEN** an ECR `PutImage` call fails (the CloudTrail event carries an `errorCode` and no `responseElements`/image digest), regardless of repository or principal
- **THEN** the EventBridge trigger rule does not match the event and `validate-bundle` is not invoked

#### Scenario: [PIPELINE-001.07] A sibling deployment's pipeline push is not excluded by this deployment's rule
- **WHEN** two Cairn deployments with distinct `{regionCode}-{stage}` qualifiers co-reside in one AWS account, and a pipeline-stage principal of the OTHER deployment issues a `PutImage`
- **THEN** this deployment's trigger rule does not treat the other deployment's principal as one of its own pipeline-stage principals (the exclusion is qualified by `{regionCode}-{stage}`), so cross-deployment interference does not occur

### Requirement: [PIPELINE-002] Successful validation triggers signing stage

WHEN validation succeeds, THE Processing_Pipeline SHALL trigger the signing stage via an `artifact.validated` EventBridge event. The signing stage MUST NOT be invoked until validation has produced a passing `artifact.validated` event; failed validation never advances to signing.

#### Scenario: [PIPELINE-002.01] Validation success emits an artifact.validated event that triggers signing
- **WHEN** the `validate-bundle` Lambda completes validation successfully for a bundle
- **THEN** an `artifact.validated` event is emitted to EventBridge and the `sign-bundle` Lambda is invoked for that bundle

### Requirement: [PIPELINE-003] Successful signing triggers archive stage regardless of WORM_Enabled

WHEN signing succeeds, THE Processing_Pipeline SHALL trigger the archive-to-S3 stage via an `artifact.signed` EventBridge event. The signing-to-archive transition is unconditional on `WORM_Enabled`; the archive stage runs identically either way. The only difference is the bucket configuration the archive Lambda writes against (Object Lock Compliance mode, per-object retention headers, deny-delete bucket policy), handled within the archive Lambda per the `worm-archive` capability.

#### Scenario: [PIPELINE-003.01] Signing success emits an artifact.signed event that triggers the archive stage when WORM_Enabled is true
- **WHEN** the `sign-bundle` Lambda completes signing successfully for a bundle in a deployment with `WORM_Enabled=true`
- **THEN** an `artifact.signed` event is emitted to EventBridge and the `archive-to-s3` Lambda is invoked, writing the bundle to the Object-Lock-enabled bucket with `ObjectLockMode=COMPLIANCE` and `ObjectLockRetainUntilDate` headers

#### Scenario: [PIPELINE-003.02] Signing success emits an artifact.signed event that triggers the archive stage when WORM_Enabled is false
- **WHEN** the `sign-bundle` Lambda completes signing successfully for a bundle in a deployment with `WORM_Enabled=false`
- **THEN** an `artifact.signed` event is emitted to EventBridge and the `archive-to-s3` Lambda is invoked, writing the bundle to a bucket without Object Lock and without `ObjectLockMode`/`ObjectLockRetainUntilDate` headers — but using the same pipeline transition path

### Requirement: [PIPELINE-004] All pipeline events trigger metadata update stage

WHEN any metadata-bearing pipeline event occurs (`artifact.validated`, `artifact.signed`, `artifact.signing-failed`, `artifact.archived`, `artifact.archive-failed`), THE Processing_Pipeline SHALL trigger the metadata update stage. The `update-metadata` Lambda runs for every such transition so the Metadata_Index reflects each bundle's current state as it moves through the pipeline. `artifact.validation-failed` is deliberately NOT routed to update-metadata: a bundle that fails validation never received a Metadata_Index record, so there is nothing to update, and it flows to the alarm path instead. The mint stage's `artifact.tagged` / `artifact.tag-failed` events are likewise not routed to update-metadata.

#### Scenario: [PIPELINE-004.01] Each pipeline event invokes update-metadata
- **WHEN** any of `artifact.validated`, `artifact.signed`, `artifact.signing-failed`, `artifact.archived`, or `artifact.archive-failed` is emitted for a bundle
- **THEN** the `update-metadata` Lambda is invoked for that event and the corresponding Metadata_Index record is updated

### Requirement: [PIPELINE-005] Transient failures retry with exponential backoff and route to DLQ on exhaustion

IF a pipeline stage (validation, signing, archival, or metadata update) fails due to a transient error (e.g., throttling, network error, service unavailability), THEN THE Processing_Pipeline SHALL emit a failure event to EventBridge, retry with exponential backoff (up to 24 hours via EventBridge, plus 2 Lambda automatic retries), and route failures that persist after all retry attempts are exhausted to a dead letter queue.

#### Scenario: [PIPELINE-005.01] Throttled DynamoDB update retries and eventually succeeds
- **WHEN** `update-metadata` receives a `ProvisionedThroughputExceededException` on its first attempt
- **THEN** EventBridge retries the event with exponential backoff and the update eventually succeeds without DLQ routing

#### Scenario: [PIPELINE-005.02] Persistent transient failure exhausts retries and routes to DLQ
- **WHEN** signing of a bundle fails with transient errors that persist beyond all 24 hours of EventBridge retries plus 2 Lambda automatic retries
- **THEN** the failure event is routed to the dead letter queue for manual intervention

### Requirement: [PIPELINE-006] Permanent validation failures route directly to DLQ without retry

IF validation determines that an Artifact_Bundle is permanently invalid (missing required annotations, invalid Config_Blob media type, OSCAL schema validation failure, accountId mismatch, temporal inconsistency between a per-OSCAL-layer `metadata.last-modified` and `ecrPushedAt`, or absent/malformed `userIdentity`), THEN THE Processing_Pipeline SHALL emit an `artifact.validation-failed` event to EventBridge, record the failure reason, and route the event directly to the dead letter queue without further retries. Permanent failures MUST NOT be retried — retries cannot fix a structurally invalid bundle and waste pipeline budget.

The `artifact.validation-failed` payload is producer-influenced in size: the failing-criteria list grows with the number of invalid manifest layers and annotations, which the producer controls. THE Processing_Pipeline SHALL bound the emitted failing-criteria detail so a single producer-supplied bundle cannot drive the event past the EventBridge per-entry size limit. The emitted detail SHALL carry at most a fixed maximum number of failing criteria, with each criterion's `field` and `message` bounded to a fixed maximum length. WHEN criteria are dropped to honor the bound, the emitted detail SHALL include an explicit truncation marker recording how many criteria were omitted and the total count.

The count and per-string-length bounds are necessary but not sufficient. The criteria content is producer-controlled, and non-ASCII characters expand when serialized, so a count/length-bounded payload can still exceed the per-entry limit in serialized bytes. THE Processing_Pipeline SHALL therefore also bound the SERIALIZED BYTE SIZE of the assembled `artifact.validation-failed` event detail, measured exactly as it serializes for delivery. WHEN the count/length-bounded detail would still serialize larger than the per-entry size limit, THE Processing_Pipeline SHALL replace the failing-criteria list with a single summary marker recording the total number of criteria omitted, so the serialized payload stays under the per-entry size limit and the mandated `artifact.validation-failed` event always fits and is delivered regardless of non-ASCII expansion.

These bounds apply only to the EventBridge event payload; the full, unbounded failing-criteria detail SHALL still be written to the structured failure log record (per the `observability` capability). WHEN a pipeline event emission reports a per-entry delivery failure (a non-zero failed-entry count from EventBridge), THE Processing_Pipeline SHALL surface that as an error rather than discard it silently, so a dropped notification becomes a retryable/DLQ-visible failure and the mandated event is never lost without a trace.

#### Scenario: [PIPELINE-006.01] permanent failures bypass retry, transient failures retry
- **WHEN** the `validate-bundle` Lambda classifies a failure as permanent (missing required annotation, invalid Config_Blob media type, OSCAL schema validation failure, cross-check mismatch, or absent/malformed `userIdentity`)
- **THEN** the bundle is routed to the DLQ without retries, an `artifact.validation-failed` event records the failure reason, and bundles classified as transient (network errors, throttling, service unavailability) are retried instead of DLQ-routed

#### Scenario: [PIPELINE-006.02] Missing required manifest annotation routes directly to DLQ
- **WHEN** a bundle is submitted with no `dev.cairnproofs.artifact.project-id` annotation
- **THEN** `validate-bundle` emits an `artifact.validation-failed` event identifying the missing field and the event goes straight to the DLQ with no retry attempts

#### Scenario: [PIPELINE-006.03] Producer-influenced failing-criteria volume is bounded with a truncation marker
- **WHEN** a producer submits a manifest that produces more failing criteria than the configured maximum (for example a manifest with tens of thousands of structurally-invalid layers, each yielding its own criterion)
- **THEN** the emitted `artifact.validation-failed` event carries at most the configured maximum number of criteria plus an explicit truncation marker recording how many were omitted and the total, the per-criterion `field`/`message` strings are each bounded to the configured maximum length so the serialized event stays within the EventBridge per-entry size limit, and the full unbounded criteria detail is still written to the structured failure log record

#### Scenario: [PIPELINE-006.04] Byte-oversized failing-criteria payload collapses to a summary marker so the event still delivers
- **WHEN** a producer submits a permanently-invalid bundle whose failing-criteria — even after the count and per-string-length bounds are applied — would serialize larger than the EventBridge per-entry size limit (for example failing-criteria text dominated by non-ASCII characters that expand when serialized)
- **THEN** the emitted `artifact.validation-failed` event replaces the failing-criteria list with a single summary marker recording the total number of criteria omitted, the serialized event payload stays within the EventBridge per-entry size limit so the mandated event is delivered rather than silently dropped, and the full unbounded criteria detail is still written to the structured failure log record

#### Scenario: [PIPELINE-006.05] A dropped failure-event entry surfaces rather than being silently discarded
- **WHEN** an attempt to emit a pipeline event (`artifact.validation-failed` or `artifact.validated`) reports a non-zero failed-entry count from EventBridge
- **THEN** the emission surfaces as an error identifying the rejected entry, causing the invocation to fail and become retryable / DLQ-visible, rather than the Lambda returning successfully while the mandated event is silently lost

### Requirement: [PIPELINE-007] Pipeline stages are idempotent under re-delivery

THE Processing_Pipeline SHALL make all Lambda functions idempotent: re-validation produces the same result, re-signing checks for existing signatures before signing, re-archiving checks for existing WORM_Receipts before archiving, and metadata updates use conditional writes with event timestamps. Re-delivery of the same EventBridge event MUST NOT produce duplicate signatures, duplicate WORM copies, duplicate Metadata_Index records, or duplicate downstream events. Idempotency SHALL be keyed on the Manifest_Digest, which is stable across re-deliveries and across the untagged→tagged transition. In particular, the `PutImage`-with-tag the tag-minting stage emits re-fires the validate trigger on an already-processed digest, and validate MUST short-circuit it rather than re-run the pipeline or re-mint.

The validate short-circuit's de-duplication unit SHALL be the **(content Manifest_Digest, Producer_Attestation_Referrer digest) pair gated on terminal-success state**, not the content Manifest_Digest alone. Validate SHALL short-circuit a triggering Producer_Attestation_Referrer with the `re-seen-digest` disposition WHEN AND ONLY WHEN both hold:

1. The content-digest's durable record is in terminal-success state (`signingStatus=signed` AND `archiveStatus=archived`).
2. The triggering Producer_Attestation_Referrer digest is already recorded among the attestations validated for that content subject.

Two cases SHALL NOT be short-circuited:

1. A content record that is non-terminal (any `signingStatus` other than `signed`, or any `archiveStatus` other than `archived`) — re-pushing it re-drives the pipeline so a stalled bundle can progress without a manual DLQ redrive.
2. A triggering Producer_Attestation_Referrer whose digest is distinct from every attestation already validated for that content subject — it is processed as a new attestation (consistent with artifact-ingestion *Producer Claim Carried as Signed Attestation Referrer*, "one content, many attestations").

The validate stage records each validated Producer_Attestation_Referrer digest onto the durable content-keyed record as an accumulating, idempotent set, so the pair-wise discrimination is durable across re-deliveries.

#### Scenario: [PIPELINE-007.01] re-processing the same event has no duplicate side effects
- **WHEN** any pipeline Lambda processes the same EventBridge event twice (e.g., due to at-least-once delivery semantics)
- **THEN** the observable outcome is identical to processing it once: no duplicate signatures are created, no duplicate WORM copies are written, no duplicate metadata records are inserted, and no duplicate downstream events are emitted

#### Scenario: [PIPELINE-007.02] Re-signing skips signature creation when a signature already exists
- **WHEN** the `sign-bundle` Lambda is re-invoked for a Manifest_Digest that already has a Notation signature referrer in ECR for the active Signing_Profile
- **THEN** the Lambda detects the existing signature, skips re-signing, and emits the downstream `artifact.signed` event without producing a duplicate signature artifact

#### Scenario: [PIPELINE-007.03] Re-tag of an already-processed digest is short-circuited
- **WHEN** the tag-minting stage applies a tag and that `PutImage`-with-tag re-fires the validate trigger on a content digest whose durable record is terminal-success (`signingStatus=signed` AND `archiveStatus=archived`) for the same Producer_Attestation_Referrer that already validated it
- **THEN** the validate stage SHALL detect the prior completion using the durable digest-keyed record and short-circuit before re-running any stage side effects (and before re-minting), emitting a "re-seen digest, skipping" metric and no further events

#### Scenario: [PIPELINE-007.04] Re-push of a bundle parked in a non-terminal state re-drives the pipeline
- **WHEN** an Producer_Attestation_Referrer is re-pushed for a content digest whose durable record carries `validateStatus=validated` but is non-terminal (for example `signingStatus=pending` after a transient sign failure that never advanced to `signed`)
- **THEN** the validate stage SHALL NOT short-circuit it as `re-seen-digest`; it SHALL process the bundle and re-emit `artifact.validated` so the non-terminal record can progress to signed and archived without a manual DLQ redrive

#### Scenario: [PIPELINE-007.05] A new attestation over an already-validated content subject is processed
- **WHEN** an Producer_Attestation_Referrer whose digest is distinct from every Producer_Attestation_Referrer already validated for a content subject is pushed, even when that content's record is terminal-success
- **THEN** the validate stage SHALL NOT short-circuit it as `re-seen-digest`; it SHALL process the new attestation so the new signed claim is validated and signed, treating the (content Manifest_Digest, Producer_Attestation_Referrer digest) pair as a distinct unit

### Requirement: [PIPELINE-008] DLQ arrival logs details and alarms for manual intervention

WHEN a dead letter queue receives a failed event, THE Processing_Pipeline SHALL log the failure details — Manifest_Digest, pipeline stage, and failure reason — and emit a CloudWatch alarm for manual intervention. DLQ depth MUST NOT grow without operator visibility.

#### Scenario: [PIPELINE-008.01] DLQ enqueue triggers structured log and CloudWatch alarm
- **WHEN** any failed event arrives in the Processing_Pipeline dead letter queue
- **THEN** a structured log entry is written recording the Manifest_Digest, the pipeline stage that failed, and the failure reason, and a CloudWatch alarm in ALARM state notifies operators via the configured SNS topic

### Requirement: [PIPELINE-009] Authenticated ECR push timestamp captured and temporal consistency enforced

WHEN the `validate-bundle` Lambda processes an ECR PUSH event, THE Provenance_System SHALL extract the EventBridge event timestamp as the authenticated ECR push timestamp (`ecrPushedAt`) and store it in the Metadata_Index. AWS sets `ecrPushedAt` and EventBridge forwards it without producer involvement, so it serves as a cryptographically-grounded, unmanipulable upper bound for when the artifact bundle first existed in the system (ALCOA+ Contemporaneous).

There is no synthetic, bundle-level creation timestamp: the `org.opencontainers.image.created` manifest annotation is not part of identity and is not relied upon for temporal consistency. Temporal consistency is instead enforced **per layer that carries an in-band authoring timestamp** — OSCAL extraction's `metadata.last-modified` (see `oscal-interop`), the seam being generic to any interop module that extracts an in-band timestamp from its content family. THE `validate-bundle` Lambda SHALL treat as a permanent validation failure and route to the DLQ any bundle in which a layer's in-band authoring timestamp is strictly after `ecrPushedAt` (temporally impossible — evidence cannot be authored after it was submitted). THE `validate-bundle` Lambda SHALL set a `temporalConsistencyWarning` flag in the Metadata_Index when, for any layer carrying an in-band authoring timestamp, `ecrPushedAt` exceeds that timestamp by more than a configurable staleness window (default: 30 days), signalling that the evidence may have been submitted well after the claimed authoring time. Bundles that trigger the warning SHALL still be accepted and processed normally.

THE per-layer check SHALL be skipped gracefully — neither a failure nor a warning — for any layer (or whole bundle) that exposes no in-band authoring timestamp: a layer whose format carries none, or an OSCAL layer whose `metadata` carries no `last-modified`, contributes no temporal-consistency assertion. A bundle with no timestamped layer at all is neither rejected nor warned on temporal-consistency grounds. The `ecrPushedAt` value SHALL always come from the EventBridge event timestamp and SHALL NEVER be taken from any producer-supplied field.

#### Scenario: [PIPELINE-009.01] temporal consistency classification
- **WHEN** the temporal consistency check runs against each layer's in-band authoring timestamp and `ecrPushedAt` (sourced from the EventBridge event timestamp, never from any producer-supplied field)
- **THEN** the bundle is classified as permanently invalid and routed to the DLQ if any layer's in-band authoring timestamp is strictly after `ecrPushedAt`; `temporalConsistencyWarning=true` is set in the Metadata_Index if, for any layer carrying an in-band authoring timestamp, `ecrPushedAt` minus that timestamp exceeds the configured staleness window (default 30 days); layers (and whole bundles) with no in-band timestamp are skipped gracefully and contribute no rejection or warning; and bundles that pass the rejection check but trigger the warning are accepted and processed through all pipeline stages

#### Scenario: [PIPELINE-009.02] Future-dated OSCAL last-modified is rejected as permanently invalid
- **WHEN** a bundle is submitted whose OSCAL layer carries `metadata.last-modified=2030-01-01T00:00:00Z` while `ecrPushedAt=2026-05-23T12:00:00Z`
- **THEN** `validate-bundle` routes the bundle to the DLQ as a permanent validation failure without any retry attempts

#### Scenario: [PIPELINE-009.03] Bundle with no in-band timestamp skips the temporal-consistency check
- **WHEN** a bundle carries only layers that expose no in-band authoring timestamp (a format that carries none, or OSCAL layers whose `metadata` carries no `last-modified`) so no layer has an intrinsic timestamp
- **THEN** the temporal-consistency check is skipped gracefully, the bundle is neither rejected nor flagged with `temporalConsistencyWarning` on temporal grounds, and validation proceeds to subsequent checks

### Requirement: [PIPELINE-010] Authenticated push principal captured from EventBridge userIdentity

WHEN the `validate-bundle` Lambda processes an ECR PUSH event, THE Provenance_System SHALL extract the IAM principal from the EventBridge event `userIdentity` block and store the full block (not only the top-level `arn`) as `authenticatedPushPrincipal` in the Metadata_Index. This value is the AWS-verified identity that performed the ECR `PutImage` call; the producer cannot set or manipulate it. It serves as the ALCOA+ Attributable anchor for the artifact bundle and is available only at ingestion time — the EventBridge event is transient and CloudTrail retains call records for only 90 days.

THE Provenance_System SHALL NOT substitute the producer-supplied `producer.identity` in the Config_Blob for `authenticatedPushPrincipal`. The captured push principal is the AWS-verified identity that performed the `PutImage` call, captured regardless of where the producing client runs. A producer running off-AWS (another cloud, on-prem, or hybrid) authenticates through a **federated AWS principal** — IAM Roles Anywhere or an OIDC web-identity role — and that federated principal is the identity AWS records in `userIdentity`; THE Provenance_System SHALL capture it identically to a same-account or cross-account principal, so the AWS-only platform constraint binds the store, not the producer's location. For any cross-account, assumed-role, or federated push, THE `validate-bundle` Lambda SHALL capture the full `userIdentity` block so the originating identity (`sessionContext`, `sourceIdentity`) is preserved across an `sts:AssumeRole` chain or a federation exchange, and SHALL reject the bundle as permanently invalid if the `userIdentity` block is absent, malformed, or lacks an identifiable principal. Cross-account and cross-boundary attribution preservation via session tags or `sts:SourceIdentity` propagation is an operational requirement on the federating or assumed role in the producing environment; THE Provenance_System SHALL record what `userIdentity` contains and SHALL NOT synthesize or infer the originating identity.

#### Scenario: [PIPELINE-010.01] authenticated principal capture across identity shapes
- **WHEN** the `validate-bundle` Lambda processes any ECR PUSH EventBridge event (covering same-account principal, cross-account principal, assumed-role session with `sessionContext` and `sourceIdentity`, an off-AWS producer's federated IAM Roles Anywhere or OIDC web-identity session, absent block, or malformed block) with any producer-supplied `producer.identity` value in the Config_Blob
- **THEN** the Lambda captures the full `userIdentity` block (not only `arn`) into `authenticatedPushPrincipal` on the initial Metadata_Index create (not a later update), never substitutes the producer-supplied `producer.identity` for `authenticatedPushPrincipal`, preserves `sessionContext` and `sourceIdentity` when present, and rejects the bundle as permanently invalid when the `userIdentity` block is absent, malformed, or lacks an identifiable principal

#### Scenario: [PIPELINE-010.02] Producer-supplied identity is never substituted for authenticated principal
- **WHEN** a Config_Blob includes `producer.identity=arn:aws:iam::111122223333:user/spoofed` but the EventBridge `userIdentity.arn` is `arn:aws:iam::444455556666:role/real-pipeline`
- **THEN** the Metadata_Index records `authenticatedPushPrincipal` with the full `userIdentity` block reflecting `arn:aws:iam::444455556666:role/real-pipeline` and never uses the producer-supplied value

#### Scenario: [PIPELINE-010.03] Off-AWS producer's federated principal is captured as the push identity
- **WHEN** a producer running on-prem or on another cloud pushes a bundle by assuming a federated AWS role via IAM Roles Anywhere or an OIDC web-identity exchange, so the EventBridge `userIdentity` block reflects that federated role's session
- **THEN** the Lambda captures the full federated `userIdentity` block (including `sessionContext` and `sourceIdentity` when present) into `authenticatedPushPrincipal`, treating it identically to a same-account or cross-account principal, and rejects the bundle as permanently invalid only if the block is absent, malformed, or lacks an identifiable principal

### Requirement: [PIPELINE-011] Canonical Tag Minting Stage

THE Processing_Pipeline SHALL mint the canonical ECR tag (artifact-ingestion *ECR Tag Format*) as a post-archive step, applying the tag to the already-stored manifest by its digest. Minting after archive ensures the artifact is durable in the WORM archive before it receives its human-readable address, so a mint failure leaves a recoverable archived-but-untagged artifact rather than a tagged-but-unarchived one. The mint is an ECR `PutImage`-with-tag on the existing digest, requiring an `ecr:PutImage` grant on the minting role scoped to the project repository. The mint stage emits an `artifact.tagged` EventBridge event on success and an `artifact.tag-failed` event on failure.

#### Scenario: [PIPELINE-011.01] Tag minted after archive completes

- **WHEN** the archive stage has written the Artifact_Bundle to durable storage and recorded the WORM_Receipt (Object Lock retention applies only when WORM_Enabled is true)
- **THEN** the mint step SHALL build the single canonical tag form `{artifact-kind}--_ref--{digest}` (artifact-ingestion *ECR Tag Format*)
- **AND** it SHALL emit an ECR `PutImage`-with-tag on the existing manifest digest (same digest, now carrying `imageTag`)

#### Scenario: [PIPELINE-011.02] Mint reads only validated coordinates

- **WHEN** the mint step builds the canonical tag
- **THEN** it SHALL source the `artifact-kind` segment only from the durable record the validate stage wrote (the artifact-kind validated from the Producer_Attestation_Referrer's claim) and the `digest` segment from the content Manifest_Digest it is addressing
- **AND** it SHALL NOT re-derive them from the triggering event, so a tag can never assert coordinates the pipeline did not validate

#### Scenario: [PIPELINE-011.03] Mint is deterministic and idempotent on the digest

- **WHEN** the mint step builds the canonical tag
- **THEN** the tag SHALL be a deterministic function of the validated artifact-kind and the content Manifest_Digest — NO sequence counter and NO uuid SHALL be consulted or allocated
- **AND** a re-mint of the same digest SHALL derive the genuinely identical tag and be absorbed as an idempotent no-op (the `PutImage` rejected by ECR tag immutability because the tag is already on this digest)

#### Scenario: [PIPELINE-011.04] Mint failure leaves an archived, untagged, digest-addressable artifact

- **WHEN** the mint step fails after the archive stage has succeeded
- **THEN** the artifact SHALL remain durable and resolvable by its Manifest_Digest
- **AND** the failure SHALL be surfaced as a metric, never silent
- **AND** the mint step SHALL be safely re-drivable (idempotent on the digest)

### Requirement: [PIPELINE-012] Pipeline signs and archives both the content manifest and the Producer_Attestation_Referrer

WHEN a submission validates successfully, THE Processing_Pipeline SHALL sign AND archive BOTH the content manifest and the triggering Producer_Attestation_Referrer as distinct objects. The content manifest is signed and archived as a distinct object. The Producer_Attestation_Referrer is signed (its own Notation signature referrer, subject = the Producer_Attestation_Referrer digest) and archived alongside the content. Content and claim are signed independently — two signatures — so the same content can carry multiple independent attestations and a future human co-sign over a claim does not require re-signing content (Design Decision D2). The signing capability owns HOW each object is signed (Signing_Profile, timestamp provider); this Requirement establishes only that BOTH objects are carried through sign and archive.

The Producer_Attestation_Referrer's own `PutImage` — when it is signed and re-pushed, plus the signature/receipt referrers the pipeline attaches to it — SHALL NOT re-trigger a fresh bundle validation for an **already-completed** attestation. The triggering Producer_Attestation_Referrer is identified by its Cairn attestation `artifactType` and de-duplicated against the durable digest-keyed record by the **(content Manifest_Digest, Producer_Attestation_Referrer digest) pair gated on terminal-success state** (see *Pipeline stages are idempotent under re-delivery*):

1. A re-push of the **same** Producer_Attestation_Referrer digest over a terminal-success content record is short-circuited.
2. A re-push of a **distinct** Producer_Attestation_Referrer over the same content subject is processed as a new attestation (consistent with artifact-ingestion's "one content, many attestations").
3. A re-push against a non-terminal content record re-drives the pipeline rather than being suppressed.

The pipeline-internal signature/receipt referrers the Producer_Attestation_Referrer carries are filtered before bundle validation by their `config.mediaType` (see artifact-ingestion *Pipeline-Internal Artifact Filtering Before Bundle Validation*), exactly as the content manifest's own internal referrers are.

#### Scenario: [PIPELINE-012.01] Both content and attestation are signed and archived
- **WHEN** a submission (content manifest + Producer_Attestation_Referrer) validates successfully
- **THEN** the signing stage produces a Notation signature referrer over the content manifest AND a separate Notation signature referrer over the Producer_Attestation_Referrer, and the archive stage writes both the content and the Producer_Attestation_Referrer to durable storage

#### Scenario: [PIPELINE-012.02] Re-push of the signed Producer_Attestation_Referrer does not re-trigger validation
- **WHEN** the pipeline signs the Producer_Attestation_Referrer and the resulting signature/receipt referrer pushes (and any re-delivery of the **same** Producer_Attestation_Referrer's own `PutImage` on a content digest whose record is terminal-success) re-enter the validate stage
- **THEN** the signature/receipt referrers are filtered as pipeline-internal by their `config.mediaType` and the already-validated, terminal-success Producer_Attestation_Referrer is short-circuited via the durable digest-keyed record, so no fresh validation runs and no duplicate downstream events are emitted

### Requirement: [PIPELINE-015] Artifact reference generation for every validated bundle

WHEN an Artifact_Bundle is validated and an archive bucket is configured, THE Processing_Pipeline SHALL generate a portable, self-describing artifact reference that points to the stored artifact by Manifest_Digest — for **every** validated bundle, regardless of content format. This is a format-agnostic foundation behavior; the owning interop module supplies the reference body's *encoding* (the OSCAL back-matter `resource` form — see the oscal-interop *OSCAL encoding of the universal artifact reference* Requirement). The reference SHALL include both an OCI registry URI (the ECR URI for the bundle) and an S3 archive URI, each carrying a SHA-256 hash matching the Manifest_Digest. The reference's resource identity is content-agnostic and deterministic, derived from the Manifest_Digest alone, so it is generated identically whether or not the bundle carries structured content.

Both the ECR rlink and the S3 rlink are generated regardless of `WORM_Enabled`, because the archive workflow always writes to S3; the S3 URI is meaningful whether or not the bucket has Object Lock applied.

THE Processing_Pipeline SHALL include the generated artifact reference in the `artifact.validated` event payload and SHALL forward it unchanged across the pipeline event chain (`artifact.validated` → `artifact.signed`) so the archive stage can persist it without re-deriving it. The reference is persisted as a first-class OCI Referrer of the bundle manifest (see the worm-archive *Artifact reference OCI Referrer attachment* Requirement). The referrer-borne reference is authoritative; the Metadata_Index `artifactResourceReference` field is a projection of it (see the metadata-index *Cross-reference storage between ECR, S3, and Signing_Profile* Requirement). The guarantee applies to every artifact.

#### Scenario: [PIPELINE-015.01] Generated reference has rlinks to both ECR and S3

- **WHEN** artifact bundle metadata (manifest digest, ECR repository, S3 key prefix, project-id, controls, assessment-period, signing status, WORM status) is provided to the reference generation function for a validated bundle with an archive bucket configured
- **THEN** the function produces a reference containing rlink entries for both the OCI registry URI (ECR) and the S3 archive URI, each entry carries a SHA-256 hash that matches the Manifest_Digest, and the reference is emitted in the `artifact.validated` event payload

#### Scenario: [PIPELINE-015.02] S3 rlink is generated even when WORM_Enabled is false

- **WHEN** the deployment has `WORM_Enabled=false` and an Artifact_Bundle passes validation
- **THEN** the generated artifact reference still includes an S3 rlink (in addition to the ECR rlink), pointing to the bundle's location in the S3 archive bucket

#### Scenario: [PIPELINE-015.03] Reference is generated for a non-OSCAL bundle

- **WHEN** a non-OSCAL Artifact_Bundle (e.g. a PDF, a scan output, a markdown governance doc) is validated with an archive bucket configured
- **THEN** the Processing_Pipeline still generates an artifact reference with ECR and S3 rlinks and a deterministic digest-derived identity, and emits it on `artifact.validated` exactly as for an OSCAL bundle

#### Scenario: [PIPELINE-015.04] Generated reference is persisted as an OCI Referrer, not only on the event

- **WHEN** any bundle's artifact reference is generated and the bundle is successfully archived
- **THEN** the reference is both emitted on `artifact.validated` (and forwarded onto `artifact.signed`) AND attached as an OCI Referrer of the bundle manifest, so a consumer holding only the bundle can discover the authoritative description via the Referrers API without querying the Metadata_Index

### Requirement: [PIPELINE-013] Attestation claim org, tenant, and project must match ECR repository path segments

WHEN the `validate-bundle` Lambda processes an ECR PUSH event, THE Provenance_System SHALL extract the `{org-id}`, `{tenant-id}`, and `{project-id}` segments from the ECR repository path in the EventBridge event and cross-check each, exact-match, against the corresponding producer-asserted identity coordinate claimed in the Producer_Attestation_Referrer (resolved from the triggering Producer_Attestation_Referrer; not read from the content manifest's Config_Blob). The claim carries the full identity triple:

1. `orgId` SHALL exact-match the `{org-id}` path segment.
2. `tenantId` SHALL exact-match the `{tenant-id}` path segment.
3. `projectId` SHALL exact-match the `{project-id}` path segment.

The cross-check is uniform across all three segments: the path is the single three-segment shape `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}`, so every segment is present, none is optional, and none is gated on a deployment mode. IF any of the three claimed values does not exactly match its corresponding path segment, THE Provenance_System SHALL treat the bundle as permanently invalid, emit an `artifact.validation-failed` event identifying each mismatched segment and both values for it, and route the bundle to the DLQ without further retries (consistent with the *Permanent validation failures route directly to DLQ without retry* Requirement).

A producer-asserted coordinate SHALL never legitimately differ from its IAM-enforced path segment: IAM enforces the ECR repository path at push time, and that path reflects the org, tenant, and project under which the producer was authorized to push, whereas the referrer's `orgId`/`tenantId`/`projectId` are producer-asserted and could be set to any value. The same path-based IAM resource scoping that covers tenant and project enforces the `org-id` cross-check, because the repository ARN already contains the `{org-id}` segment.

The cross-check is load-bearing for the WORM archive key: `org-id`, `tenant-id`, and `project-id` are the producer-asserted identity coordinates the canonical WORM archive key is composed from (see `worm-archive` *Canonical S3 archive key structure* for the key's shape). Verifying each against the IAM-enforced repository path keeps the immutable WORM key honest-by-construction, rather than asserting producer-claimed coordinates the bundle was not pushed under.

#### Scenario: [PIPELINE-013.01] Any of org, tenant, or project mismatch fails the cross-check
- **WHEN** a bundle is validated whose Producer_Attestation_Referrer `orgId`, `tenantId`, or `projectId` does not exactly match the corresponding `{org-id}`/`{tenant-id}`/`{project-id}` segment of the ECR repository path in the triggering EventBridge event
- **THEN** the bundle is treated as a permanent validation failure, an `artifact.validation-failed` event identifying each mismatched segment and both values is emitted, and the bundle is routed to the DLQ without further retries

#### Scenario: [PIPELINE-013.02] Matching org, tenant, and project passes the cross-check
- **WHEN** a bundle is pushed to `cairnproofs4af-artifact-cmh-alpha/org-acme/tnt-foo/prj-bar` with Producer_Attestation_Referrer `orgId=org-acme`, `tenantId=tnt-foo`, and `projectId=prj-bar`
- **THEN** the cross-check passes for all three segments and validation proceeds to subsequent checks

#### Scenario: [PIPELINE-013.03] Matched coordinates are verified against the WORM key's identity coordinates
- **WHEN** a bundle whose Producer_Attestation_Referrer `orgId`/`tenantId`/`projectId` all match their ECR-path segments is archived
- **THEN** the org, tenant, and project identity coordinates the canonical WORM archive key is composed from (see `worm-archive` *Canonical S3 archive key structure*) have each been verified against the IAM-enforced ECR repository path, so the immutable key asserts no unverified producer-claimed coordinate
