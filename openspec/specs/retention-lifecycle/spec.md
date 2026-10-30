# retention-lifecycle Specification

## Purpose
Retention policy and lifecycle management for evidence over its full storage lifetime. Defines:

1. The S3 lifecycle tier schedule — Standard → Glacier Instant Retrieval at 90 days → Glacier Deep Archive at 1095 days (3 years).
2. Metadata_Index record retention for the full retention period.

Per-kind retention configurability, the unarchived-manifest cleanup policy, the retention-expiry workflow, and LTA re-timestamping are deferred.

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

THE Metadata_Index SHALL retain records for the full configured retention period of each artifact bundle, even after the corresponding ECR copy has been lifecycle-expired.

#### Scenario: [RETAIN-002.01] Metadata record survives ECR cleanup

- **WHEN** an ECR manifest is removed by ECR lifecycle policy after its `WORM_Receipt` is confirmed
- **THEN** the corresponding `Metadata_Index` record SHALL remain queryable and SHALL continue to reference the S3 archive object until that object's retention period expires

---
