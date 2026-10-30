# platform-constraints Specification

## Purpose
Requirements that constrain implementation choices:

1. AWS-only operation — no third-party SaaS in the core path;
1. ECR as the OCI registry — no self-hosted registries;
1. AWS Signer — no self-managed PKI;
1. S3 Object Lock Compliance mode when `WORM_Enabled=true` — Governance mode is never an acceptable substitute;
1. multi-account deployment topology support;
1. Inheritable compliance with at least FedRAMP Moderate, PCI, HIPAA, ISO 27k;
1. data residency in the deployment's designated region — no cross-region replication without explicit operator action.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [PLATFORM-001] AWS-Only Operation

THE Provenance_System SHALL run entirely within AWS. All compute, storage, signing, key management, networking, and observability dependencies of the Provenance_System SHALL be AWS-native services; no component of the system may depend on non-AWS infrastructure for core ingestion, signing, archival, metadata indexing, or retrieval operations.

This foundational platform constraint justifies the choice of ECR, AWS Signer, S3 Object Lock, and VPC endpoints — alternative implementations on non-AWS platforms are out of scope.

The AWS-only constraint binds the Provenance_System itself — the Custody and Operations axes — and SHALL NOT be interpreted to bind the parties that produce or consume evidence. Custody (the customer's AWS account that holds the evidence) and Operations (the principal that runs the substrate) are AWS-resident; Production and Consumption are location-agnostic. An Artifact_Producer or an evidence consumer MAY run on any cloud, on-premises, or in a hybrid environment and SHALL interact with the Provenance_System through a federated AWS principal (for example IAM Roles Anywhere or an OIDC web-identity role).

#### Scenario: [PLATFORM-001.01] Deployment provisioning rejects non-AWS targets

- **WHEN** an operator attempts to provision an Provenance_System deployment targeting infrastructure outside AWS
- **THEN** the Provenance_System provisioning tooling rejects the request and no deployment is created

#### Scenario: [PLATFORM-001.02] Core data planes resolve to AWS service endpoints

- **WHEN** the Provenance_System performs an ingestion, signing, archival, indexing, or retrieval operation
- **THEN** every external dependency invoked resolves to an AWS-native service endpoint (ECR, S3, DynamoDB, AWS Signer, KMS, EventBridge, Lambda, CloudWatch) and no third-party SaaS endpoint is contacted

#### Scenario: [PLATFORM-001.03] Producer outside AWS pushes through a federated AWS principal

- **WHEN** an Artifact_Producer running on another cloud, on-premises, or in a hybrid environment authenticates through a federated AWS principal and pushes an Artifact_Bundle
- **THEN** the push is accepted on the same terms as an in-AWS producer and the AWS-only constraint does not reject the producer for running outside AWS

### Requirement: [PLATFORM-002] Uniform AWS Resource Naming Convention

THE Provenance_System SHALL derive every AWS resource physical name from a single naming helper (`nameFor`) producing the form `{appName}-{resource}-{qualifier}`, where:

- `appName` is a configurable per-deployment identifier (default `cairnproofs{token}`) whose `{token}` is a 3-character uniquifier drawn from an unambiguous alphabet (excluding `o 0 i l 1`), **generated once at first deploy and frozen in committed configuration** — never regenerated per synthesis;
- `resource` is the AWS resource's logical label (e.g. `validate-bundle`, `artifact-archive`);
- `qualifier` is the immutable `{regionCode}-{stage}` Deployment_Qualifier.

The deployment **Stack** name SHALL be `{appName}-{qualifier}` — two segments, no `{resource}` — because it names the deployment container, not a resource within it. THE Provenance_System SHALL compose the ECR repository namespace and every reference derived from it — the Notation trust-policy registry scope and the pipeline IAM ARN patterns — from the SAME single helper, so the resource-name structure has exactly one definition point.

THE Provenance_System SHALL NOT emit a CloudFormation Export for any resource name; cross-stack and cross-system value sharing uses SSM Parameter Store.

#### Scenario: [PLATFORM-002.01] Two deployments in one account are collision-free and distinguishable

- **WHEN** two Provenance_System deployments are provisioned in the same AWS account, region, and stage
- **THEN** their resource names differ by the frozen per-deployment `{appName}` token
- **AND** no two physical resource names collide

#### Scenario: [PLATFORM-002.02] The appName token is frozen, not regenerated per synth

- **WHEN** `cdk synth` runs repeatedly for an already-deployed stack
- **THEN** the `{appName}` token is read unchanged from committed configuration
- **AND** CloudFormation update targeting is preserved (no resource replacement from a shifted name)

#### Scenario: [PLATFORM-002.03] Derived references compose from the single helper

- **WHEN** the ECR repository namespace prefix (the `{appName}-artifact-{qualifier}` form the `nameFor` helper composes) is re-templated
- **THEN** the change is made in the one naming helper
- **AND** the Notation registry scope and the IAM ARN patterns follow with no independent edits

### Requirement: [PLATFORM-003] ECR as the OCI Registry

THE Provenance_System SHALL use Amazon ECR as the OCI registry for all Artifact_Bundle storage and SHALL NOT use any self-hosted or third-party OCI registry implementation.

This constraint applies to every Provenance_System deployment regardless of which operator runs it (in-house, MSP, or vendor-managed). It precludes registry choices such as Harbor, Quay, Docker Registry, or self-managed distribution implementations.

#### Scenario: [PLATFORM-003.01] OCI registry operations target ECR

- **WHEN** the Provenance_System performs any OCI registry operation (manifest push, blob upload, referrer query, tag list, signature attachment)
- **THEN** the operation targets an Amazon ECR endpoint and no self-hosted or third-party registry is contacted

#### Scenario: [PLATFORM-003.02] Deployment configuration rejects non-ECR registry endpoints

- **WHEN** an operator attempts to configure the Provenance_System with a non-ECR OCI registry endpoint
- **THEN** the configuration is rejected at provisioning time and the deployment is not created

### Requirement: [PLATFORM-004] AWS Signer for Signing Operations

THE Provenance_System SHALL use AWS Signer for all cryptographic signing operations on Artifact_Bundles and SHALL NOT operate a self-managed PKI, self-hosted signing service, or alternative cloud-provider signing service.

Signing key custody, signing profile management, and signature generation all flow through AWS Signer; the Provenance_System does not hold or generate signing private keys outside the AWS Signer / KMS HSM boundary.

#### Scenario: [PLATFORM-004.01] Signing operations invoke AWS Signer

- **WHEN** the Provenance_System signs an Artifact_Bundle
- **THEN** the signature is produced by AWS Signer using a configured signing profile and no self-managed signing service is invoked

#### Scenario: [PLATFORM-004.02] Self-managed PKI configuration is rejected

- **WHEN** an operator attempts to configure the Provenance_System with a self-managed PKI or non-AWS-Signer signing backend
- **THEN** the configuration is rejected at provisioning time

### Requirement: [PLATFORM-005] S3 Object Lock Compliance Mode When WORM_Enabled

WHILE operating with `WORM_Enabled=true`, THE Provenance_System SHALL use S3 Object Lock Compliance mode (not Governance mode). Governance mode is never an acceptable choice — when WORM is enabled at all, it MUST be Compliance mode.

The choice between Compliance and Governance mode SHALL NOT be exposed as a configuration option to operators or customers; the Provenance_System provisioning tooling SHALL hard-code Compliance mode for any deployment with `WORM_Enabled=true`. When `WORM_Enabled=false`, S3 Object Lock is not configured on the archive bucket at all and this requirement does not apply.

#### Scenario: [PLATFORM-005.01] WORM bucket is provisioned with Compliance mode

- **WHILE** provisioning an Provenance_System deployment with `WORM_Enabled=true`
- **WHEN** the archive S3 bucket is created
- **THEN** the bucket has Object Lock enabled in Compliance mode and not in Governance mode

#### Scenario: [PLATFORM-005.02] Governance mode configuration is rejected

- **WHEN** an operator or configuration source attempts to specify Object Lock Governance mode for the archive bucket while `WORM_Enabled=true`
- **THEN** the Provenance_System provisioning tooling rejects the configuration and the deployment is not created

#### Scenario: [PLATFORM-005.03] WORM_Enabled=false deployments do not configure Object Lock

- **WHILE** provisioning an Provenance_System deployment with `WORM_Enabled=false`
- **WHEN** the archive S3 bucket is created
- **THEN** the bucket is created without S3 Object Lock and the Compliance-vs-Governance choice is moot

### Requirement: [PLATFORM-006] Maximum File Size 10 GB

THE Provenance_System SHALL reject any individual file that exceeds 10 GB and SHALL return an error indicating the maximum allowed size. The 10 GB limit is a conservative product-level bound chosen to constrain ingestion latency and storage cost; it sits well below ECR's per-layer hard limit of 52 GB. Enforcement mechanics (the advisory descriptor-size check, the authoritative byte-true check, and the client-side pre-flight) are owned by the artifact-ingestion *File Size Constraint* Requirement.

#### Scenario: [PLATFORM-006.01] Push of a file exceeding 10 GB is rejected with size error

- **WHEN** an Artifact_Producer attempts to push an individual file larger than 10 GB
- **THEN** the Provenance_System rejects the push and returns an error indicating the maximum allowed file size of 10 GB, and no manifest, signature, or metadata record is created

### Requirement: [PLATFORM-007] Data Residency in Designated Region

THE Provenance_System SHALL NOT store or process evidence data outside the AWS region designated at Cairn initialization time, in support of customer data sovereignty and regulatory residency requirements.

The designated region is set at Cairn-instance provisioning and is immutable for the lifetime of that instance. All ECR repositories, S3 buckets, DynamoDB tables, KMS keys, AWS Signer profiles, Lambda functions, EventBridge buses, and CloudWatch resources used to store or process evidence SHALL be regional resources in the designated region.

#### Scenario: [PLATFORM-007.01] All evidence-bearing resources are regional in the designated region

- **WHEN** the Provenance_System is provisioned for a designated region
- **THEN** every resource that stores or processes evidence (ECR repositories, S3 buckets, DynamoDB tables, KMS keys, AWS Signer profiles, Lambda functions, EventBridge buses, CloudWatch log groups and metrics) is created in that designated region and no evidence data is replicated, processed, or accessible outside that region

#### Scenario: [PLATFORM-007.02] Cross-region replication of evidence data is not configured

- **WHEN** an operator attempts to configure cross-region replication, cross-region backup, or any other mechanism that would cause evidence data to leave the designated region
- **THEN** the Provenance_System provisioning tooling rejects the configuration and the deployment retains data residency within the designated region

