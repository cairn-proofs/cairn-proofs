# access-control Specification

## Purpose
IAM governs every operation: push, pull, sign, verify, and query. This capability defines:

1. Producer and auditor IAM scopes over the uniform `{org-id}/{tenant-id}/{project-id}` hierarchy — producer push scoped to its project repository path, auditor read scoped tenant-exact via `dynamodb:LeadingKeys` (exact-match, never a partition-key prefix).
2. Producer project scoping via repository-path-bound IAM resources.
3. The least-privilege **Operator** principal — the control plane that runs the substrate, untrusted for integrity by construction: no auto alias-mover grant, and a bounded delete/DoS/read blast radius.
4. CloudTrail logging of all access events.
5. The dedicated alias-movement authorization boundary.

The WORM deny-delete bucket policy is deferred.

*Domain terms (`Capitalized_Underscore`) used below are defined in [GLOSSARY.md](../../GLOSSARY.md), which names the governing requirement for each.*
## Requirements
### Requirement: [ACCESS-001] IAM-Enforced Access Control on All Operations

THE Provenance_System SHALL enforce IAM-based access control on all evidence operations including push, pull, sign, verify, and query. IF a principal attempts an operation they are not authorized for, THEN THE Provenance_System SHALL deny the request and return an error indicating insufficient permissions.

No operation against ECR, S3, DynamoDB, or AWS Signer bypasses IAM. Service-internal credentials are never shared across principals, and no anonymous data-plane endpoints exist.

#### Scenario: [ACCESS-001.01] Unauthorized push is denied with insufficient-permissions error

- **WHEN** a principal lacking ECR push permissions on a target repository attempts to push an Artifact_Bundle
- **THEN** the Provenance_System denies the request and returns an error indicating insufficient permissions, and no manifest, signature, or metadata record is created

#### Scenario: [ACCESS-001.02] Unauthorized query is denied with insufficient-permissions error

- **WHEN** a principal lacking DynamoDB query permissions on the Metadata_Index attempts to issue a query
- **THEN** the Provenance_System denies the request and returns an error indicating insufficient permissions, and no index records are returned

### Requirement: [ACCESS-003] Read-Only Auditor Access Scope

THE Provenance_System SHALL support read-only auditor access that includes ECR pull and verify operations, S3 archive object read operations, and DynamoDB Metadata_Index query operations, but excludes push and sign operations, scopeable to all projects within a tenant or a specific project.

An auditor role MUST be expressible at either the tenant scope (all projects within one `{tenant-id}`) or the project scope (a single `{tenant-id}/{project-id}`) without custom per-deployment policy authoring. THE Provenance_System SHALL enforce that scope through DynamoDB's native key primitives:

1. The `{tenant-id}` is the Metadata_Index partition key; tenant scope SHALL be enforced by `dynamodb:LeadingKeys`, which matches the partition key by exact value, not by prefix.
2. The `{project-id}` is carried in the sort key; project scope SHALL be enforced by a sort-key `begins_with` condition within the auditor's tenant partition.
3. THE Provenance_System SHALL deny an unscoped `Scan` of the Metadata_Index, so no auditor reaches records outside their `LeadingKeys`-bound tenant partition.

#### Scenario: [ACCESS-003.01] Auditor can pull, verify, and query but cannot push or sign

- **WHEN** a principal bound to the auditor role attempts each of: ECR pull, signature verification, S3 archive object GET, Metadata_Index query, ECR push, and Notation sign
- **THEN** the pull, verify, GET, and query operations succeed within the auditor's scope, and the push and sign operations are denied with insufficient-permissions errors

#### Scenario: [ACCESS-003.02] Project-scoped auditor cannot query another project in the same tenant

- **WHEN** an auditor principal scoped to a single `{tenant-id}/{project-id}` issues a Metadata_Index query for a different project within the same `{tenant-id}` partition
- **THEN** the Provenance_System denies the query via the sort-key `begins_with` condition bounding the auditor to its `{project-id}`, and no records are returned

#### Scenario: [ACCESS-003.03] Tenant partition is exact-match scoped and unscoped Scan is denied

- **WHEN** an auditor whose `dynamodb:LeadingKeys` binds queries to partition-key value `{tenant-id}` = T issues a query naming a different tenant partition T', or attempts an unscoped `Scan` of the Metadata_Index
- **THEN** the Provenance_System denies both: `dynamodb:LeadingKeys` matches the partition key by exact value, so T ≠ T' returns no records, and the unscoped `Scan` is denied — so no auditor reaches records outside their tenant partition. The same exact-match scoping binds a query against any Global Secondary Index, because every GSI on a tenant-PK plane leads with `{tenant-id}` as its partition key (see metadata-index *Queryable artifact index by core dimensions*), so an auditor cannot escape the tenant partition via an alternate index

### Requirement: [ACCESS-004] CloudTrail Logging of All Access Events

THE Provenance_System SHALL log all access events including push, pull, sign, verify, and delete attempts to CloudTrail.

CloudTrail coverage MUST include both successful and denied attempts so that unauthorized-access patterns are visible to security operations.

#### Scenario: [ACCESS-004.01] Successful and denied operations both appear in CloudTrail

- **WHEN** a principal performs a successful ECR pull and a separate denied ECR push attempt against the Provenance_System
- **THEN** CloudTrail records both events, including principal ARN, operation, target resource ARN, and outcome (success or AccessDenied)

### Requirement: [ACCESS-005] Dedicated Alias-Movement Authorization Boundary

THE Provenance_System SHALL provide a **dedicated, least-privilege IAM authorization** (a distinct alias-mover role/policy) as the sanctioned grant for setting or moving a mutable alias tag (the `_als` and `_chn` reserved forms). This dedicated grant SHALL NOT be attached to, or embedded in, the evidence-producer push role or the signing role; possessing producer or signer permissions SHALL NOT, by itself, attach the alias-mover grant.

A consumer SHALL NOT need to trust that the registry enforced the alias-movement boundary. ECR cannot condition `ecr:PutImage` on the image-tag string, so a principal already holding repo-scoped `ecr:PutImage` (e.g. an evidence producer) can write an alias tag out-of-band — outside the dedicated role and outside the `cairn alias` wrapper. THE Provenance_System therefore SHALL NOT rest the integrity guarantee against a misdirected alias on the IAM boundary alone.

The guarantee SHALL instead rest on digest-anchored verification. An alias reached via the retrieval/verification paths resolves to its underlying Manifest_Digest and is verified against that digest, its signature, and its WORM_Receipt (see artifact-ingestion *Alias Tags Do Not Carry the Verification Guarantee*). A consumer who verifies is therefore never deceived by an alias an unauthorized principal repointed: at worst the alias names a different validated artifact, and an alias pointed at unvalidated content fails verification.

THE `cairn alias` operator surface SHALL surface a registry authorization denial legibly, directing the operator at the dedicated alias-mover grant, and SHALL NOT itself re-implement the authorization check.

#### Scenario: [ACCESS-005.01] Alias movement is a dedicated grant, not embedded in push or sign

- **WHEN** the IAM policies are provisioned
- **THEN** the alias-mover grant is a distinct policy/role that is not attached to or embedded in the evidence-producer push role or the signing role, so neither push nor sign permission carries the alias-mover grant by itself

#### Scenario: [ACCESS-005.02] Authorized alias movement via the dedicated grant succeeds

- **WHEN** a principal holding the dedicated alias-mover grant sets or moves an alias to an already-filed digest through the `cairn alias` surface
- **THEN** the operation is permitted

#### Scenario: [ACCESS-005.03] Registry authorization denial is surfaced legibly

- **WHEN** a principal lacking the alias-mover grant attempts a set or move through the `cairn alias` surface and the registry denies the `ecr:PutImage`
- **THEN** the wrapper surfaces the denial as an insufficient-permissions error directing the operator at the dedicated alias-mover grant, and no alias tag is created or repointed by that call

#### Scenario: [ACCESS-005.04] Digest-anchored verification backstops an out-of-band alias repoint

- **WHEN** a principal with repo-scoped `ecr:PutImage` but without the dedicated alias-mover grant repoints an alias out-of-band (bypassing the wrapper), and a consumer later verifies an artifact reached via that alias
- **THEN** verification is anchored on the resolved Manifest_Digest, its signature, and its WORM_Receipt — so the consumer is not deceived about integrity: an alias pointed at unvalidated content fails verification, and an alias pointed at a different validated artifact is presented as a mutable label over that digest, never as the verified identity

### Requirement: [ACCESS-002] Producer Scoping by Project Within a Tenant

THE Provenance_System SHALL support scoping Artifact_Producer access by project within the logical tenant hierarchy, restricting ECR push permissions to the project repository path `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}`.

The path is a single logical hierarchy of three mandatory segments — `{org-id}`, `{tenant-id}`, and `{project-id}`:

1. All three segments SHALL be present; none is optional, so exactly one path shape exists.
2. Each segment SHALL be a stable, opaque, immutable, allocate-once identifier.
3. `{tenant-id}` is a logical **Tenant**, NOT an AWS account; the producing identity is recorded as per-artifact provenance (the signed claim, the CloudTrail push principal, and the signature) and SHALL NOT appear as a path segment.

THE Provenance_System SHALL grant producer ECR push permission scoped to the bare project repository ARN: a project maps to a single repository with no child repository-path segment, so there is no `/*` sub-repository form to grant.

Producer IAM scoping SHALL be enforced by ECR resource-ARN prefix across the `{org-id}`, `{tenant-id}`, and `{project-id}` segments, stopping a producer authorized for one project from pushing to another project's repositories.

#### Scenario: [ACCESS-002.01] Producer scoped to project X cannot push to project Y

- **WHEN** an Artifact_Producer principal whose IAM policy scopes ECR push to the project repository `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{X}` attempts to push a bundle to the repository `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{Y}`
- **THEN** the Provenance_System denies the push via IAM resource scoping and no manifest is created in project Y's namespace

### Requirement: [ACCESS-006] Least-Privilege Operator Principal

THE Provenance_System SHALL model the **Operator** — the control-plane principal that deploys and runs the substrate — as a first-class, least-privilege IAM principal that is **untrusted for integrity by construction**. Artifact integrity is anchored in the data plane (the Manifest_Digest, the signature, and the WORM_Receipt) and SHALL NOT be conferred by the control plane; no Operator permission can forge or alter an artifact's verification verdict.

1. The Operator SHALL hold only the permissions required to deploy and operate the substrate (provisioning and managing the ECR, S3, DynamoDB, EventBridge, Lambda, KMS, and AWS Signer resources).
2. The Operator SHALL NOT carry the dedicated alias-mover grant by virtue of operating; the alias-mover grant remains the distinct principal of [ACCESS-005](#requirement-access-005-dedicated-alias-movement-authorization-boundary).
3. The Operator's blast radius SHALL be bounded: WORM Object Lock backstops object deletion where `WORM_Enabled=true`, and least-privilege bounds denial-of-service and read exposure.

A verifying consumer's verdict SHALL be independent of Operator access: because verification re-derives authenticity, integrity, and the WORM verdict from the content-addressed bytes, their signatures, and the WORM_Receipt (see [artifact-verifiability](../../specs/artifact-verifiability/spec.md)), no Operator action can move a verdict a consumer re-derives.

#### Scenario: [ACCESS-006.01] Operator can deploy and operate but cannot move an alias by operating

- **WHEN** a principal bound to the Operator role deploys the stack and performs routine operational actions, then attempts to set or move a mutable alias tag without holding the dedicated alias-mover grant
- **THEN** the deploy and operational actions succeed within the Operator's least-privilege scope, and the alias set/move is denied with an insufficient-permissions error, because operating does not attach the alias-mover grant of [ACCESS-005](#requirement-access-005-dedicated-alias-movement-authorization-boundary)

#### Scenario: [ACCESS-006.02] A verifying consumer's verdict is unaffected by Operator access

- **WHEN** a principal holding full Operator access operates the substrate and a consumer later verifies an artifact reached through the store
- **THEN** the consumer's verdict is re-derived from the resolved Manifest_Digest, its signature, and its WORM_Receipt independent of the control plane, so no Operator action alters whether the artifact verifies

