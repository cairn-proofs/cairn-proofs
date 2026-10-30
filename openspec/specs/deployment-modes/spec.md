# deployment-modes Specification

## Purpose
The deployment topology — custody distinct from operations. Defines:

1. The Customer_Hosted deployment mode — the Provenance_System always runs in the customer's own AWS account; Custody (account ownership) is the customer's, never a vendor's.
2. The custody/operations split — Custody is fixed to the customer while Operations (who runs the substrate) is agnostic: an in-house, MSP, or vendor-managed Operator MAY deploy into the customer's account without changing the trust model ("vendor-operated, customer-custodied" is an expected delivery model).
3. The uniform `{org-id}/{tenant-id}/{project-id}` logical hierarchy — every deployment is provisioned under exactly three mandatory, opaque path segments; the `{org-id}` segment is present from day one.
4. Region pinning — a deployment is bound to one AWS region for its lifetime, set at provisioning by the `{regionCode}-{stage}` Deployment_Qualifier.
5. In-region residency of all artifact storage resources.
6. Remote-region and off-AWS (federated) producer push.
7. The `WORM_Enabled` deployment-time parameter.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [DEPLOY-001] Customer_Hosted deployment mode

The **Provenance_System** is the system — the ECR operational layer, S3 WORM archive, DynamoDB Metadata_Index, and the EventBridge/Lambda pipeline. THE Provenance_System SHALL support a **Customer_Hosted** deployment mode — the only supported deployment mode — in which **custody** and **operations** are distinct, independent concerns:

1. **Custody is fixed in the customer's own AWS account.** Every Customer_Hosted Provenance_System instance SHALL be deployed entirely within the customer's own AWS account as a single-account instance, and the data plane — stored artifacts, KMS customer-managed key material, and the CloudTrail access record — SHALL never leave that account's trust boundary. No evidence content or key material SHALL be held in a vendor-owned account. (This is the account-ownership guarantee; in-region residency of the storage resources is governed by [DEPLOY-003].)

2. **Operations is operator-agnostic.** Any operator — the customer's own platform or SRE team, a managed-service provider, a systems integrator, or a vendor-managed offering — MAY deploy, run, and maintain a Customer_Hosted instance by operating within the customer's account, without moving the custody boundary or changing the trust model. "Vendor-operated, customer-custodied" is an expected delivery model: who operates the instance is a delivery and commercial decision, never a trust decision, because artifact integrity is anchored in the data plane (Manifest_Digest, signature, WORM_Receipt) and is never conferred by whoever operates the control plane.

The custody guarantee fixes the meaning of a customer-managed key: any **Customer_CMK** a customer supplies is an in-account key, used without any cross-account grant, because custody — including key material — never leaves the customer's own account. The Customer_CMK concept itself is defined in [GLOSSARY.md](../../GLOSSARY.md); this requirement specifies no Customer_CMK provisioning behavior.

Within a Customer_Hosted instance, every tenant SHALL belong to exactly one org, identified by a mandatory, immutable, opaque `{org-id}` that is always present as the leading segment of the logical tenant hierarchy (whose full path grammar is governed by [artifact-ingestion](../artifact-ingestion/spec.md)). The `{org-id}` SHALL be allocated once and SHALL NOT be reused or renamed, and it SHALL be present for every tenant from initial provisioning — there is no implicit, shared, or omitted org segment. This requirement governs only the presence of the `{org-id}` segment and each tenant's assignment to exactly one org; org-scoped operations grouping (org-scoped roles and org-spanning queries) is outside its scope.

#### Scenario: [DEPLOY-001.01] Customer_Hosted single-account deployment
- **WHEN** a customer provisions a Customer_Hosted Provenance_System instance
- **THEN** the full Provenance_System stack (ECR, S3 WORM, DynamoDB Metadata_Index, EventBridge pipeline, AWS Signer profiles, KMS keys) is deployed entirely within the customer's designated AWS account with no service-account dependency, and no evidence content or key material is held in any vendor-owned account

#### Scenario: [DEPLOY-001.02] Vendor-operated, customer-custodied delivery
- **WHEN** a managed-service provider or vendor-managed offering operates a Customer_Hosted instance on the customer's behalf
- **THEN** the operator deploys and runs the instance within the customer's own AWS account, custody and the entire data plane remain in the customer's account, and the operator's operational access confers no ability to forge, tamper with, or undetectably alter a stored artifact

#### Scenario: [DEPLOY-001.03] Every tenant belongs to exactly one org via a mandatory org-id segment
- **WHEN** a tenant is established within a Customer_Hosted Provenance_System instance
- **THEN** that tenant is assigned exactly one org identified by a mandatory, immutable, opaque `{org-id}` present from initial provisioning as the leading segment of the tenant hierarchy, and that `{org-id}` is allocated once and is never reused or renamed

### Requirement: [DEPLOY-002] Region pinning at Cairn initialization

The **Deployment_Qualifier** is the immutable `{regionCode}-{stage}` suffix applied across all Cairn resource names. THE Provenance_System SHALL be deployed exclusively in the AWS region where the parent Cairn instance is initialized, as determined by the `Deployment_Qualifier` `{regionCode}-{stage}` established at initialization time. The deployment region SHALL be immutable for the lifetime of the Provenance_System instance; changing the deployment region requires provisioning a new Provenance_System instance in the target region.

The deployment region and account SHALL be supplied to provisioning as operator-resolved configuration, not literals fixed in checked-in source. The `regionCode` segment of the `Deployment_Qualifier` SHALL be derived from the deployment region via a committed region-to-code registry, so the code cannot drift from the region it denotes. A deployment MAY target any AWS region present in the registry: there is no stage-to-region binding and no closed allowlist. A region absent from the registry SHALL be rejected at provisioning time with a message naming how to register it. Adding a region to the registry asserts it has been operationally validated, since the Provenance_System can deploy in any AWS region that supports ECR, AWS Signer, S3 Object Lock, KMS, EventBridge, Lambda, and DynamoDB.

This requirement governs only *which* region a deployment may be pinned to (any registered region) and how its code is derived. The confine-to-one-region guarantee and in-region residency of all artifact storage resources are unchanged and remain governed by [DEPLOY-003].

#### Scenario: [DEPLOY-002.01] Deployment region locked to Cairn initialization region
- **WHEN** a Cairn instance is initialized with `Deployment_Qualifier` `cmh-alpha`
- **THEN** the associated Provenance_System is provisioned exclusively in `us-east-2`, the deployment-region property is recorded immutably on the instance, and any attempt to mutate the deployment region post-provisioning is rejected — moving to a different region requires standing up a brand-new Provenance_System instance under a new Cairn initialization in the target region

#### Scenario: [DEPLOY-002.03] Region code derived from registry; unregistered region rejected
- **WHEN** an operator initializes a Cairn instance supplying a deployment region
- **THEN** if the region is present in the committed region-to-code registry, the `regionCode` segment of the `Deployment_Qualifier` is the registry's code for that region (e.g. `us-east-2` → `cmh`, `us-west-2` → `pdx`), and if the region is absent from the registry, provisioning is rejected with a message naming how to add the region to the registry — provisioning never proceeds with an unresolved or guessed region code

### Requirement: [DEPLOY-003] All artifact storage resources resident in deployment region

All artifact storage resources — ECR repositories, the S3 WORM archive bucket, and the DynamoDB Metadata_Index — SHALL reside in the Provenance_System's designated deployment region. No evidence content SHALL be stored in or replicated to any other AWS region unless an explicit operator-initiated cross-region replication action is performed. This guarantee provides customers explicit, verifiable control over the geographic location of their compliance data in support of data sovereignty and regulatory residency requirements.

#### Scenario: [DEPLOY-003.01] Storage resources colocated in deployment region
- **WHEN** an Provenance_System with `Deployment_Qualifier` `dub-prod` is inspected
- **THEN** every ECR repository, the S3 WORM archive bucket, and the DynamoDB Metadata_Index table are located in `eu-west-1`, and no automatic cross-region replication is configured on the WORM bucket or the Metadata_Index

#### Scenario: [DEPLOY-003.02] No silent cross-region replication
- **WHEN** an operator has not performed an explicit cross-region replication action against the WORM bucket
- **THEN** no evidence object versions are present in any region other than the designated deployment region

### Requirement: [DEPLOY-004] Remote-region producers push to designated deployment region

Evidence gathered from workloads operating in AWS regions other than the Provenance_System's designated deployment region SHALL be transmitted to and stored in the Provenance_System's designated deployment region. Producers operating in remote regions SHALL push evidence to the ECR endpoint of the designated Provenance_System deployment, not to a regional ECR endpoint local to the producer.

#### Scenario: [DEPLOY-004.01] Remote-region producer pushes to deployment-region ECR endpoint
- **WHEN** an Artifact_Producer running in `ap-southeast-2` produces evidence destined for an Provenance_System whose `Deployment_Qualifier` is `iad-prod`
- **THEN** the producer pushes the artifact bundle to the ECR endpoint in `us-east-1` (the deployment region), and the resulting object lands in the `us-east-1` S3 WORM archive bucket — not in any `ap-southeast-2` ECR or S3 resource

### Requirement: [DEPLOY-005] WORM_Enabled deployment-time parameter (cross-reference)

THE Provenance_System provisioning interface SHALL accept a `WORM_Enabled` boolean parameter at deployment time that controls whether the deployment's S3 WORM archive bucket is configured for Object Lock Compliance mode with per-object retention enforcement. The value of `WORM_Enabled` SHALL be irreversible for the lifetime of the deployment, because S3 Object Lock cannot be added to or removed from an existing bucket.

The `worm-archive` capability spec defines the full semantics of `WORM_Enabled`: bucket configuration, per-object retention headers, the conditional deny-delete bucket policy, archive workflow behavior, and the production-only requirement of `WORM_Enabled=true`. This requirement exists solely to record that the deployment-modes surface exposes the parameter and treats it as immutable post-provisioning.

#### Scenario: [DEPLOY-005.01] WORM_Enabled set at provisioning is immutable
- **WHEN** an Provenance_System instance is provisioned with `WORM_Enabled=true`
- **THEN** the value is recorded immutably on the deployment, any subsequent attempt to flip `WORM_Enabled` to `false` (or vice versa) on the existing deployment is rejected, and the deeper semantics of how `WORM_Enabled` shapes bucket configuration and the archive lifecycle are governed by the `worm-archive` capability
