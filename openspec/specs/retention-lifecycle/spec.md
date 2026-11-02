# retention-lifecycle Specification

## Purpose
Retention policy and lifecycle management for evidence over its full storage lifetime. Defines:

1. The S3 lifecycle tier schedule — Standard → Glacier Instant Retrieval at 90 days → Glacier Deep Archive at 1095 days (3 years).
2. Metadata_Index record retention for the full retention period.
3. The single retention clock (Retain_Until) and its settings.
4. Prune_Candidate eligibility and its authoritative inputs.
5. The operator-initiated prune workflow (`cairn prune`), its ordered deletion and audit record, and the rule that nothing deletes artifacts automatically.
6. Alarms on deletions and on retention-configuration changes.

Per-kind retention configurability and LTA re-timestamping are deferred.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*

## Requirements

### Requirement: [RETAIN-001] S3 lifecycle tiering

THE Provenance_System SHALL transition S3 archive objects through a fixed lifecycle schedule: S3 Standard → Glacier Instant Retrieval at 90 days → Glacier Deep Archive at 1095 days (3 years).

WHILE operating with `WORM_Enabled=true`, lifecycle tier transitions SHALL NOT affect Object Lock retention (S3 enforces this natively).

#### Scenario: [RETAIN-001.01] Lifecycle schedule transitions archived objects

- **WHEN** the archive bucket is provisioned
- **THEN** the S3 lifecycle configuration SHALL transition objects to Glacier Instant Retrieval at 90 days and to Glacier Deep Archive at 1095 days (3 years)

#### Scenario: [RETAIN-001.04] Tier transitions do not break Object Lock retention

- **WHEN** an archived object is transitioned from Glacier IR to Glacier Deep Archive while under an unexpired Object Lock `RetainUntilDate` in a `WORM_Enabled=true` deployment
- **THEN** the Object Lock retention SHALL remain in force across the tier transition and the object SHALL remain undeletable

---

### Requirement: [RETAIN-002] Metadata_Index records retained for the full retention period

THE Metadata_Index SHALL retain the record of each Artifact_Bundle at least until the bundle's Retain_Until has passed (see [RETAIN-003](#requirement-retain-003-single-retention-clock-for-every-archived-bundle)). THE Metadata_Index SHALL remove the record only as the final step of an operator-initiated prune of that bundle (see [RETAIN-010](#requirement-retain-010-ordered-bundle-deletion)), never earlier and never as a side effect of any other operation.

#### Scenario: [RETAIN-002.01] Metadata record survives ECR cleanup

- **WHEN** an operator prune of an expired bundle has deleted the bundle's ECR referrers and ECR manifest but has not yet deleted every S3 object version
- **THEN** the corresponding `Metadata_Index` record SHALL remain queryable and SHALL continue to reference the S3 archive object, and SHALL be removed only after every S3 object version of the bundle is deleted

#### Scenario: [RETAIN-002.02] Metadata record persists until its bundle is pruned

- **WHEN** an archived Artifact_Bundle's Retain_Until has passed and no operator has pruned the bundle
- **THEN** the corresponding `Metadata_Index` record SHALL remain queryable and SHALL continue to reference both the ECR manifest and the S3 archive object

---

### Requirement: [RETAIN-003] Single retention clock for every archived bundle

THE Provenance_System SHALL assign every archived Artifact_Bundle a **Retain_Until**, the instant at which the bundle's retention period ends. Retain_Until SHALL equal the bundle's archive timestamp plus the configured retention period in days (see [RETAIN-007](#requirement-retain-007-retention-settings-and-bounds)), computed in UTC from a single reading of the clock, whatever the `WORM_Enabled` setting. WHILE operating with `WORM_Enabled=true`, every S3 object version of the bundle SHALL carry `ObjectLockRetainUntilDate` equal to Retain_Until.

#### Scenario: [RETAIN-003.01] Retain_Until derives from the archive timestamp and the configured period

- **WHEN** a bundle is archived at instant T in a deployment whose configured retention period is D days
- **THEN** the bundle's Retain_Until SHALL be T + D days in UTC, whether `WORM_Enabled` is `true` or `false`

#### Scenario: [RETAIN-003.02] Object Lock retention equals Retain_Until under WORM_Enabled

- **WHEN** a bundle is archived in a deployment with `WORM_Enabled=true`
- **THEN** every S3 object version written for the bundle SHALL carry `ObjectLockRetainUntilDate` equal to the Retain_Until recorded on its WORM_Receipt

#### Scenario: [RETAIN-003.03] Archive on 29 February computes a valid Retain_Until

- **WHEN** a bundle is archived on 29 February
- **THEN** the archive SHALL succeed and Retain_Until SHALL be exactly the configured number of days after the archive timestamp

---

### Requirement: [RETAIN-007] Retention settings and bounds

Each deployment SHALL configure two settings, each a whole number of days:

1. The **retention period**: default 2,555 (7 years), bounded from 1 to 36,135 (99 × 365).
2. The **unarchived-manifest window**: default 30, bounded from 1 to 365.

THE Provenance_System SHALL reject an out-of-bounds setting at synthesis time, before provisioning any resource. The stack SHALL publish both settings to SSM parameters that only the stack deployment writes. The archive stage SHALL read the period from SSM, and SHALL fail the archive rather than write an object when the value is missing or out of bounds.

#### Scenario: [RETAIN-007.01] Out-of-bounds retention period is rejected at synthesis

- **WHEN** a deployment is synthesized with a retention period of 0 days or of more than 36,135 days
- **THEN** synthesis SHALL fail with an error that names the retention-period bounds, and no resource SHALL be provisioned

#### Scenario: [RETAIN-007.02] Out-of-bounds unarchived-manifest window is rejected at synthesis

- **WHEN** a deployment is synthesized with an unarchived-manifest window of 0 days or of more than 365 days
- **THEN** synthesis SHALL fail with an error that names the window bounds, and no resource SHALL be provisioned

#### Scenario: [RETAIN-007.04] cairn prune refuses invalid runtime settings

- **WHEN** `cairn prune` reads a retention-period or unarchived-manifest-window SSM parameter that is missing, is not a string of ASCII digits, or is outside its bounds
- **THEN** it SHALL classify nothing, delete nothing, and exit with its deployment-discovery error naming the parameter

#### Scenario: [RETAIN-007.03] Archive stage refuses an invalid runtime retention period

- **WHEN** the archive stage runs and the retention-period SSM parameter is missing, not a whole number, or outside 1 to 36,135
- **THEN** the archive SHALL fail with an `artifact.archive-failed` event that names the invalid setting, and SHALL write no S3 object and no WORM_Receipt

---

### Requirement: [RETAIN-004] Expired-bundle Prune_Candidate

A **Prune_Candidate** is an item that an operator may delete through `cairn prune`. An archived bundle SHALL be a Prune_Candidate of class *expired bundle* only when all of these hold:

1. S3 object versions exist under the canonical prefix, or it is an index remnant ([RETAIN-010.04]).
2. The authoritative Retain_Until has passed (see [RETAIN-012](#requirement-retain-012-authoritative-retention-inputs)).
3. WHILE `WORM_Enabled=true`, every S3 object version's `ObjectLockRetainUntilDate` is in the past.
4. No S3 object version under the prefix carries a legal hold with status `ON`.

#### Scenario: [RETAIN-004.01] Bundle past Retain_Until with no legal hold is an expired-bundle candidate

- **WHEN** an archived bundle's authoritative Retain_Until has passed, every one of its S3 object versions is past retention, and none carries a legal hold
- **THEN** the bundle SHALL be classified as a Prune_Candidate of class expired bundle

#### Scenario: [RETAIN-004.02] Legal hold vetoes eligibility

- **WHEN** an archived bundle's Retain_Until has passed but one of its S3 object versions carries an S3 Object Lock legal hold with status `ON`
- **THEN** the bundle SHALL NOT be classified as a Prune_Candidate

#### Scenario: [RETAIN-004.03] Bundle under retention is not a candidate

- **WHEN** an archived bundle's authoritative Retain_Until has not passed
- **THEN** the bundle SHALL NOT be classified as a Prune_Candidate, whatever the state of its Metadata_Index record or its ECR annotations

#### Scenario: [RETAIN-004.04] Empty prefix is never vacuously expired

- **WHEN** no S3 object version exists under a named bundle's canonical prefix and the bundle is not an index remnant of [RETAIN-010.04]
- **THEN** the bundle SHALL NOT be classified as a Prune_Candidate of class expired bundle

---

### Requirement: [RETAIN-012] Authoritative retention inputs

`cairn prune` SHALL derive a bundle's S3 canonical prefix and Metadata_Index key from the deployment's org, tenant, and project coordinates and the bundle digest, never from a WORM_Receipt body or an annotation. The authoritative Retain_Until SHALL be the value in the pipeline-written S3 copy of the WORM_Receipt under that prefix and, WHILE operating with `WORM_Enabled=true`, the latest `ObjectLockRetainUntilDate` of the prefix's object versions. The ECR receipt referrer and its annotation SHALL serve only as a pre-filter and a display value.

#### Scenario: [RETAIN-012.01] Forged ECR receipt cannot redirect eligibility

- **WHEN** an ECR WORM_Receipt referrer for a bundle names a different S3 prefix or a past Retain_Until, while the S3 receipt copy under the derived prefix shows retention still in force
- **THEN** the bundle SHALL NOT be classified as a Prune_Candidate

#### Scenario: [RETAIN-012.02] Disagreement between ECR and S3 receipts refuses the item

- **WHEN** a bundle has more than one WORM_Receipt referrer, or its ECR receipt's Retain_Until differs from the S3 receipt copy
- **THEN** `cairn prune` SHALL refuse the item and report the disagreement as the reason

#### Scenario: [RETAIN-012.03] Index record alone cannot make a bundle eligible

- **WHEN** a bundle's Metadata_Index record indicates a Retain_Until in the past but the S3 receipt copy or the S3 object versions show retention still in force
- **THEN** the bundle SHALL NOT be classified as a Prune_Candidate

---

### Requirement: [RETAIN-009] Retain_Until for a receipt that does not record it

WHEN the S3 receipt copy of a bundle does not record Retain_Until, THE Provenance_System SHALL take the bundle's Retain_Until from the latest `ObjectLockRetainUntilDate` among its S3 object versions WHILE operating with `WORM_Enabled=true`, and from the S3 receipt copy's archive timestamp plus the configured retention period WHILE operating with `WORM_Enabled=false`.

#### Scenario: [RETAIN-009.01] Object Lock date stands in for a missing receipt value

- **WHEN** a bundle in a `WORM_Enabled=true` deployment has an S3 receipt copy with no Retain_Until
- **THEN** its Retain_Until SHALL be the latest `ObjectLockRetainUntilDate` among its S3 object versions

#### Scenario: [RETAIN-009.02] Archive timestamp stands in when Object Lock is off

- **WHEN** a bundle in a `WORM_Enabled=false` deployment has an S3 receipt copy with no Retain_Until
- **THEN** its Retain_Until SHALL be the S3 receipt copy's archive timestamp plus the configured retention period

---

### Requirement: [RETAIN-008] Unarchived-manifest Prune_Candidate

An ECR manifest pushed longer ago than the unarchived-manifest window ([RETAIN-007]) SHALL be a Prune_Candidate of class *unarchived manifest* only when one of these holds:

1. It is a content manifest with no referrer of any kind, no Metadata_Index record, and no S3 object under its derived canonical prefix.
2. It is a referrer, or a referrers-tag-schema image index, whose `subject` manifest is absent from the repository, neither it nor its subject has an S3 object, and no Metadata_Index record of the project names either digest.

Each absence SHALL be established only by a positive not-found response; any other error SHALL refuse the item.

#### Scenario: [RETAIN-008.01] Aged manifest with no referrer, no record, and no archive is a candidate

- **WHEN** an ECR content manifest has no referrer, no Metadata_Index record, no S3 object under its derived prefix, and was pushed longer ago than the unarchived-manifest window
- **THEN** the manifest SHALL be classified as a Prune_Candidate of class unarchived manifest

#### Scenario: [RETAIN-008.02] Bundle in or failed in the pipeline is retained

- **WHEN** an ECR content manifest has a Metadata_Index record, a Producer_Attestation_Referrer, a signature referrer, or any S3 object under its derived prefix, however long ago it was pushed
- **THEN** the manifest SHALL NOT be classified as a Prune_Candidate

#### Scenario: [RETAIN-008.03] Referrer whose subject is gone is a candidate

- **WHEN** a Notation signature referrer, or the referrers-tag-schema image index that lists it, names a `subject` digest for which ECR returns `ImageNotFoundException`, and it was pushed longer ago than the unarchived-manifest window
- **THEN** it SHALL be classified as a Prune_Candidate of class unarchived manifest

#### Scenario: [RETAIN-008.07] Referrer of an absent but archived subject is retained

- **WHEN** a signature or WORM_Receipt referrer's `subject` manifest is absent from the repository, but S3 object versions exist under the subject's derived prefix
- **THEN** the referrer SHALL NOT be classified as a Prune_Candidate, whatever its age

#### Scenario: [RETAIN-008.08] A referrer named by any bundle's index record is retained

- **WHEN** a referrer or referrers-tag-schema index has an absent subject and empty S3 prefixes, but its digest or its subject's digest appears in any Metadata_Index record of the project as `manifestDigest`, `attestationDigest`, `signatureDigest`, `attestationSignatureDigest`, or a member of `validatedAttestationDigests`
- **THEN** it SHALL NOT be classified as a Prune_Candidate, and the report SHALL name the record that holds the digest

#### Scenario: [RETAIN-008.04] Manifest inside the window is retained

- **WHEN** an ECR content manifest meets every other unarchived-manifest condition but was pushed less than the unarchived-manifest window ago
- **THEN** the manifest SHALL NOT be classified as a Prune_Candidate

#### Scenario: [RETAIN-008.05] A transient error is not proof of absence

- **WHEN** an existence check for a manifest's subject, Metadata_Index record, or S3 prefix fails with throttling, access denied, or any error other than a positive not-found response (`ImageNotFoundException` for that exact digest, a strongly consistent `GetItem` that returns no item, or an empty `ListObjectVersions`)
- **THEN** the item SHALL NOT be classified as a Prune_Candidate and the report SHALL name the failed check

#### Scenario: [RETAIN-008.06] A forged subject cannot bypass the archive check

- **WHEN** a producer pushes a content manifest whose `subject` names an absent digest, and S3 object versions exist under that manifest's own derived prefix
- **THEN** the manifest SHALL be classified only as an expired-bundle candidate under [RETAIN-004](#requirement-retain-004-expired-bundle-prune_candidate), and SHALL NOT be classified as an unarchived manifest

---

### Requirement: [RETAIN-005] Operator-initiated prune

`cairn prune` SHALL be the only sanctioned path that deletes an Artifact_Bundle, an ECR manifest, an ECR referrer, or an S3 archive object version, apart from the teardown of a non-production stack. It SHALL behave as follows:

1. Without explicit digests, it SHALL report every Prune_Candidate in the target project with its digest, class, and reason, and SHALL delete nothing.
2. It SHALL delete only Prune_Candidates whose digests the operator names explicitly in the same invocation, and SHALL reject a wildcard or class-wide selector.
3. Immediately before deleting a named item, it SHALL classify the item again, and SHALL refuse and report an item that is no longer a Prune_Candidate without stopping the other named items.

#### Scenario: [RETAIN-005.01] Report-only invocation deletes nothing

- **WHEN** an operator runs `cairn prune` for a project without naming any digest
- **THEN** the command SHALL list every Prune_Candidate with its digest, class, and reason, and SHALL issue no delete operation

#### Scenario: [RETAIN-005.02] Only named candidates are deleted

- **WHEN** an operator runs `cairn prune` naming one digest while the project holds three Prune_Candidates
- **THEN** only the named candidate SHALL be deleted, and the other two SHALL remain intact

#### Scenario: [RETAIN-005.03] Eligibility is re-checked at deletion time

- **WHEN** an operator names a digest that was a Prune_Candidate when reported, but an S3 legal hold was placed on one of its object versions before the delete ran
- **THEN** `cairn prune` SHALL refuse that item, report the legal hold as the reason, and delete none of its parts

#### Scenario: [RETAIN-005.04] Wildcard selection is rejected

- **WHEN** an operator invokes `cairn prune` with a wildcard or a class-wide selector instead of explicit digests
- **THEN** the command SHALL reject the invocation with a usage error and SHALL delete nothing

---

### Requirement: [RETAIN-010] Ordered bundle deletion

WHEN `cairn prune` deletes an expired bundle, it SHALL delete every ECR referrer, then the ECR manifest, then every S3 object version under the derived prefix, then the bundle's ControlClaims rows, and then its Metadata_Index record. It SHALL NOT delete any ECR part while any S3 object version is under retention. IF a step fails, THEN it SHALL skip that item's later steps, report the step, and exit non-zero. A later run naming the digest SHALL resume, classifying from the S3 receipt copy when the ECR parts are gone.

#### Scenario: [RETAIN-010.01] Bundle deletion follows the fixed order

- **WHEN** an operator prunes an expired bundle
- **THEN** the ECR referrers SHALL be deleted first, then the ECR manifest, then every S3 object version, then the ControlClaims rows, and the Metadata_Index record last

#### Scenario: [RETAIN-010.02] Failed step halts the item and the next run resumes it

- **WHEN** a prune of an expired bundle fails while deleting an S3 object version, and the operator later runs `cairn prune` naming the same digest
- **THEN** the first run SHALL exit non-zero with the failed step reported and the Metadata_Index record still present, and the second run SHALL classify the bundle from its S3 receipt copy and complete the remaining steps without error

#### Scenario: [RETAIN-010.03] ECR copy is kept while any S3 version is retained

- **WHEN** an operator names a bundle for which one S3 object version is still under retention
- **THEN** `cairn prune` SHALL delete no ECR referrer, no ECR manifest, and no S3 object version of that bundle

#### Scenario: [RETAIN-010.04] Index remnant of an interrupted prune is removed

- **WHEN** a named digest's ECR manifest and every S3 object version under its derived prefix are positively absent, the repository exists, and its Metadata_Index record names the derived bucket and prefix as an archived bundle whose `archivedAt` plus the configured retention period has passed
- **THEN** `cairn prune` SHALL treat the digest as an **index remnant**, SHALL delete its ControlClaims rows and then its Metadata_Index record with audit records, and SHALL refuse the digest when any of those conditions does not hold

#### Scenario: [RETAIN-010.05] A referrer of the pruned bundle that holds its own evidence is refused

- **WHEN** an expired bundle's referrers include a manifest that has S3 object versions under its own derived prefix or its own Metadata_Index record
- **THEN** `cairn prune` SHALL refuse the expired bundle, report that referrer's digest as the reason, and delete no part of either

---

### Requirement: [RETAIN-011] Prune audit record

For every named item, `cairn prune` SHALL write a structured audit record to the stack-owned prune audit log group before each delete step and after the item's outcome. The record SHALL name the caller identity as returned by STS, the digest, the Prune_Candidate class, the eligibility or refusal reason, the step, and the outcome. IF the audit write fails, THEN `cairn prune` SHALL NOT perform the next delete step. The audit log group SHALL never expire its events.

#### Scenario: [RETAIN-011.01] Every named item is audited in the stack-owned log group

- **WHEN** `cairn prune` deletes one named item, refuses a second, and fails on a third
- **THEN** the prune audit log group SHALL hold a record for each item naming the STS caller identity, the digest, the class, the reason, and the outcome

#### Scenario: [RETAIN-011.02] Audit write failure stops deletion

- **WHEN** `cairn prune` cannot write the audit record that precedes a delete step
- **THEN** it SHALL NOT perform that delete step, SHALL report the audit failure, and SHALL exit non-zero

---

### Requirement: [RETAIN-006] No automatic deletion of artifacts

THE Provenance_System SHALL NOT delete or expire an Artifact_Bundle, an ECR manifest, an ECR referrer, or an S3 archive object version automatically:

1. No artifact ECR repository SHALL carry a lifecycle rule; an untagged manifest, including every referrer, SHALL be retained regardless of age.
2. The S3 archive bucket SHALL carry no lifecycle expiration action of any kind. Storage-class transitions under [RETAIN-001](#requirement-retain-001-s3-lifecycle-tiering) remain permitted.
3. No scheduled job, event rule, or pipeline stage SHALL issue a delete against an artifact ECR repository or the archive bucket.

#### Scenario: [RETAIN-006.01] Synthesized stack carries no expiry rule

- **WHEN** the stack is synthesized
- **THEN** no artifact ECR repository SHALL carry a lifecycle policy, and the archive bucket's lifecycle configuration SHALL contain only storage-class transitions and no expiration action

#### Scenario: [RETAIN-006.02] Expired bundle remains until an operator prunes it

- **WHEN** an archived bundle's Retain_Until has passed and no operator has run `cairn prune` naming it
- **THEN** the bundle's ECR manifest, its referrers, its S3 object versions, and its Metadata_Index record SHALL all remain present

#### Scenario: [RETAIN-006.03] Referrers of a bundle are retained regardless of age

- **WHEN** a tagged Artifact_Bundle's untagged signature, WORM_Receipt, and Producer_Attestation_Referrer referrers have been in the repository for any length of time
- **THEN** every referrer SHALL remain discoverable through the OCI Referrers API, so `cairn verify` continues to find them

---

### Requirement: [RETAIN-013] Alarm on deletion and retention-configuration changes

THE Provenance_System SHALL raise an alarm on the deployment's notification topic, naming the caller principal and the affected resource, for every CloudTrail event that:

1. deletes an image from an artifact repository, changes an image's storage class, or deletes an artifact repository;
2. changes an artifact repository's lifecycle policy or repository policy;
3. changes the archive bucket's lifecycle or Object Lock configuration;
4. writes, labels, or deletes a retention SSM parameter, or updates the archive stage's function configuration;
5. stops, deletes, or reconfigures a CloudTrail trail or its event selectors.

#### Scenario: [RETAIN-013.01] Image deletion raises an alarm

- **WHEN** any principal deletes an image from an artifact repository, through `cairn prune` or the raw API
- **THEN** an alarm is published to the notification topic naming the caller principal and the image digest

#### Scenario: [RETAIN-013.02] Lifecycle or repository-policy change raises an alarm

- **WHEN** any principal puts a lifecycle policy or a repository policy on an artifact repository, or a lifecycle configuration on the archive bucket
- **THEN** an alarm is published to the notification topic naming the caller principal and the resource

#### Scenario: [RETAIN-013.03] Trail change raises an alarm

- **WHEN** any principal stops logging on, deletes, updates, or changes the event selectors of a CloudTrail trail in the deployment's account
- **THEN** an alarm is published to the notification topic naming the caller principal and the trail

---
