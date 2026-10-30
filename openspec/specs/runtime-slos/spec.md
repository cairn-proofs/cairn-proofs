# runtime-slos Specification

## Purpose
The runtime service-level properties the system commits to under normal regional operating conditions:

1. End-to-end pipeline completion within 10 minutes for 100 MB bundles.
2. Capacity for at least 100,000 bundles per account and 1 PB of WORM storage.
3. OCI 1.1 Referrers API conformance.
4. 11-nines S3 durability.
5. DLQ-based regional-disruption recovery.

Measured latency/throughput/availability SLOs with breach alarming are deferred.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [SLO-001] End-to-End Processing_Pipeline Duration

WHEN an Artifact_Bundle up to 100 MB is ingested, THE Processing_Pipeline SHALL complete all processing stages — validation, signing, S3 archival with `WORM_Receipt` attachment (with Object Lock retention applied when `WORM_Enabled=true`), and metadata indexing — within 10 minutes end-to-end. Duration MUST be measured from the ECR PUSH event to the final metadata update.

#### Scenario: [SLO-001.01] 100 MB bundle reaches metadata within 10 minutes

- **WHEN** an Artifact_Bundle up to 100 MB is successfully pushed to ECR
- **THEN** validation, signing, archival, and metadata indexing all complete within 10 minutes measured from the ECR PUSH event to the final Metadata_Index update

#### Scenario: [SLO-001.02] End-to-end duration budget exceeded

- **WHEN** the elapsed time from ECR PUSH event to final Metadata_Index update for a bundle up to 100 MB exceeds 10 minutes
- **THEN** the breach is recorded against the end-to-end pipeline SLO and surfaces through the observability capability

### Requirement: [SLO-002] Bundle and Repository Capacity per AWS Account

THE Provenance_System SHALL support, per AWS account, without requiring changes to the data model, key structure, component topology, or deployment architecture:

1. up to 10,000 ECR repositories, one per project, so the per-account project ceiling equals tenants × projects-per-tenant — approximately 100 tenants per account at the 100-projects-per-tenant soft default of [SLO-003](#requirement-slo-003-project-and-per-project-bundle-capacity) before an ECR repository-quota increase; and
2. at least 10,000,000,000 concurrently stored Artifact_Bundles, the product of the 10,000-project ceiling and the per-project bundle capacity of [SLO-003](#requirement-slo-003-project-and-per-project-bundle-capacity).

#### Scenario: [SLO-002.01] Account at scale capacity

- **WHEN** an AWS account holds up to 10,000 ECR repositories (one per project, across all tenants), each project at the per-project bundle capacity of [SLO-003](#requirement-slo-003-project-and-per-project-bundle-capacity)
- **THEN** all ingestion, signing, archival, and query operations continue to meet their published SLOs without requiring data-model, key-structure, topology, or deployment changes

### Requirement: [SLO-003] Project and Per-Project Bundle Capacity

THE Metadata_Index SHALL, while continuing to satisfy the indexed-query latency target, support:

1. at least 100 distinct projects per tenant, as a soft default that MAY be raised; and
2. at least 1,000,000 Artifact_Bundles per project.

#### Scenario: [SLO-003.01] Hundred projects per tenant with a million bundles each

- **WHEN** the Metadata_Index holds 100 distinct projects for a tenant and each project contains 1,000,000 Artifact_Bundles
- **THEN** indexed queries by project, control, or date continue to satisfy the p99 latency target defined for the Metadata_Index

### Requirement: [SLO-004] S3 WORM Archive Storage Capacity

THE S3 WORM archive SHALL scale to at least 1 PB of total storage per tenant without requiring changes to the data model, key structure, component topology, or deployment architecture.

#### Scenario: [SLO-004.01] One petabyte archive

- **WHEN** the S3 WORM archive for a tenant reaches 1 PB of total stored data
- **THEN** archival, retrieval, and lifecycle operations continue to function without modifications to the data model, key structure, component topology, or deployment architecture

### Requirement: [SLO-005] OCI 1.1 Referrers API Conformance

THE Provenance_System SHALL use ECR's OCI 1.1 Referrers API for signatures, WORM_Receipts, and in-toto attestations, and SHALL NOT use Docker-specific APIs or features that are incompatible with the OCI 1.1 specification.

#### Scenario: [SLO-005.01] Referrer attachment uses OCI 1.1 API

- **WHEN** a signature, WORM_Receipt, or in-toto attestation is attached to an Artifact_Bundle
- **THEN** the attachment is made using ECR's OCI 1.1 Referrers API and the resulting referrer linkage is discoverable via that same API

#### Scenario: [SLO-005.02] Docker-specific incompatibility rejected

- **WHEN** an Provenance_System component would otherwise rely on a Docker-specific API or feature that is incompatible with the OCI 1.1 specification
- **THEN** that path is not used and an OCI 1.1-compliant alternative is used instead

### Requirement: [SLO-006] S3 WORM Archive Durability

THE S3 WORM archive SHALL maintain 99.999999999% (11 nines) annual durability by using S3 Object Lock in Compliance mode with versioning enabled. This durability target applies whenever `WORM_Enabled=true`; non-production deployments running with `WORM_Enabled=false` inherit S3's standard durability for the bucket configuration in use.

#### Scenario: [SLO-006.01] WORM_Enabled archive configured for 11 nines

- **WHEN** a deployment is provisioned with `WORM_Enabled=true`
- **THEN** the S3 archive bucket is configured with Object Lock in Compliance mode and versioning enabled to deliver 99.999999999% annual durability

### Requirement: [SLO-007] Regional Disruption Recovery via Dead Letter Queue

IF a regional service disruption affects the Processing_Pipeline, THEN THE Provenance_System SHALL resume processing queued events from the dead letter queue within 4 hours of service restoration, without data loss, preserving all events received prior to the disruption.

#### Scenario: [SLO-007.01] DLQ drain begins within four hours of restoration

- **WHEN** a regional service disruption affecting the Processing_Pipeline is restored
- **THEN** processing of queued events from the dead letter queue resumes within 4 hours of service restoration and preserves all events received prior to the disruption with no data loss

