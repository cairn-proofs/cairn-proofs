# encryption Specification

## Purpose
Encryption and isolation posture across all data planes. Defines:

1. TLS 1.2+ for everything in transit.
2. AWS KMS customer-managed key encryption at rest for ECR, S3, and DynamoDB.
3. Signing-key custody within KMS HSM boundaries.
4. Integrity verification on archive — digest verify-and-abort; the IntegrityMismatch alarm branch is pending metric-alarm alignment.

Customer_CMK support and VPC endpoints are deferred.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [ENCRYPT-001] TLS 1.2+ for All Data in Transit

THE Provenance_System SHALL encrypt all data in transit using TLS 1.2 or higher for all client-to-service and service-to-service communication, including connections to ECR, S3, KMS, DynamoDB, Signer, CloudWatch Logs, EventBridge, and STS.

No call within the Provenance_System data planes or control planes — service-to-service or client-to-service — may use an unencrypted channel. Any call that negotiates a TLS version below 1.2 MUST be rejected at the client or endpoint layer.

#### Scenario: [ENCRYPT-001.01] Producer push over TLS 1.2 or higher

- **WHEN** a producer pushes an Artifact_Bundle to ECR or any downstream pipeline component initiates a call to S3, KMS, DynamoDB, Signer, CloudWatch Logs, EventBridge, or STS
- **THEN** the connection negotiates TLS 1.2 or higher and any attempt to use a lower TLS version is refused

#### Scenario: [ENCRYPT-001.02] Downgrade attempt is refused

- **WHEN** a client attempts to connect to any Provenance_System-managed endpoint using TLS 1.1 or lower
- **THEN** the endpoint refuses the connection and the request is not processed

### Requirement: [ENCRYPT-002] Encryption at Rest with KMS CMK

THE Provenance_System SHALL encrypt all data at rest using AWS KMS customer-managed keys for ECR, S3, and DynamoDB.

AWS-managed keys and SSE-S3-only configurations are unacceptable substitutes. Every data plane that persists artifacts or artifacts metadata MUST be configured with a CMK owned by the Provenance_System account.

#### Scenario: [ENCRYPT-002.01] ECR, S3, and DynamoDB configured with CMKs

- **WHEN** any AWS resource that supports AWS KMS customer-managed keys (ECR repository, S3 bucket, DynamoDB, etc.) is provisioned by the Provenance_System
- **THEN** the resource is configured to use an AWS KMS customer-managed key for at-rest encryption, and the key ARN is recorded in the deployment configuration

### Requirement: [ENCRYPT-003] Signing Key Custody in KMS HSM

THE Provenance_System SHALL ensure signing key material never leaves AWS KMS HSM boundaries.

Signing operations MUST use AWS Signer / KMS APIs that operate on the key inside the HSM. The raw private key material MUST NOT be exportable, cached, or transmitted to any compute outside the KMS HSM boundary.

#### Scenario: [ENCRYPT-003.01] Signing happens inside the HSM

- **WHEN** the Processing_Pipeline or a reviewer signs an Artifact_Bundle via AWS Signer
- **THEN** the signing operation is performed inside AWS KMS HSM, the private key material is never exposed to the calling Lambda or to any caller, and no signing key is materialized in any process memory, log, or storage outside KMS

### Requirement: [ENCRYPT-004] Integrity-Mismatch Alarming on Archive

WHEN the Processing_Pipeline archives an Artifact_Bundle to S3 WORM, THE Provenance_System SHALL compare the SHA-256 digest of each archived blob against the corresponding ECR manifest digest, and SHALL emit an IntegrityMismatch CloudWatch alarm if any digest does not match.

Digest verification on archive is the last line of defense against silent corruption or tampering between ECR and S3; a mismatch MUST raise an alarm rather than be retried away.

#### Scenario: [ENCRYPT-004.01] Mismatched archived blob raises IntegrityMismatch alarm

- **WHEN** the Processing_Pipeline writes an Artifact_Bundle's blobs to S3 WORM and the SHA-256 digest of any archived blob does not equal the corresponding ECR manifest digest
- **THEN** the Provenance_System emits an IntegrityMismatch CloudWatch alarm identifying the bundle and the offending blob, and the bundle is not marked archived in the Metadata_Index

#### Scenario: [ENCRYPT-004.02] Digest verification correctness

- **WHEN** the digest verification function is invoked for any pair of blob content and expected digest — either during archive integrity checks or any other digest verification site
- **THEN** the function returns `true` if and only if the SHA-256 of the blob content equals the expected digest, and on failure the returned error includes both the computed digest and the expected digest

