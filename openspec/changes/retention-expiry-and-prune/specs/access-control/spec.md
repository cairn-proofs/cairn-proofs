## MODIFIED Requirements

### Requirement: [ACCESS-004] CloudTrail Logging of All Access Events

THE Provenance_System SHALL log all access events including push, pull, sign, verify, and delete attempts to CloudTrail.

CloudTrail coverage MUST include both successful and denied attempts so that unauthorized-access patterns are visible to security operations. Coverage SHALL include data events for object-version deletes on the archive bucket and for item deletes on the Metadata_Index and ControlClaims tables, and deployment SHALL fail its CloudTrail preflight when that data-event coverage is absent.

#### Scenario: [ACCESS-004.01] Successful and denied operations both appear in CloudTrail

- **WHEN** a principal performs a successful ECR pull and a separate denied ECR push attempt against the Provenance_System
- **THEN** CloudTrail records both events, including principal ARN, operation, target resource ARN, and outcome (success or AccessDenied)

#### Scenario: [ACCESS-004.02] Archive and index deletes are recorded as data events

- **WHEN** a principal deletes an S3 archive object version or a Metadata_Index item
- **THEN** CloudTrail records the delete as a data event naming the principal ARN, the object key and version or the item key, and the outcome

#### Scenario: [ACCESS-004.03] Deployment preflight requires delete data-event coverage

- **WHEN** an operator deploys a stack whose CloudTrail coverage lacks data events for archive-bucket object deletes or index item deletes
- **THEN** the deployment preflight fails and names the missing data-event coverage

## ADDED Requirements

### Requirement: [ACCESS-007] Dedicated Pruner Authorization Boundary

THE Provenance_System SHALL provide a dedicated **pruner** IAM role, scoped per project, as the only sanctioned grant for `cairn prune` deletions. Its trust policy SHALL name only designated IAM users, SHALL require `aws:MultiFactorAuthPresent` to be true (never "if exists"), and SHALL require an `sts:SourceIdentity` equal to the caller's IAM user name, with a maximum session of one hour. `cairn prune` SHALL refuse to delete unless its STS caller identity is a session of that role. Its deletes SHALL be limited to `ecr:BatchDeleteImage` on the project repository, `s3:DeleteObjectVersion` under the project's archive prefix, and `dynamodb:DeleteItem` on the Metadata_Index and ControlClaims under the tenant's `dynamodb:LeadingKeys`.

#### Scenario: [ACCESS-007.01] Pruner role is distinct and not assumable by other roles

- **WHEN** the IAM roles are provisioned
- **THEN** the pruner role is distinct, and its trust policy does not admit the Operator, producer, signing, alias-mover, auditor, or any pipeline Lambda execution role

#### Scenario: [ACCESS-007.02] Pipeline roles cannot delete artifacts or index records

- **WHEN** any pipeline Lambda execution role attempts `ecr:BatchDeleteImage` on an artifact repository, `s3:DeleteObjectVersion` on the archive bucket, or `dynamodb:DeleteItem` or `dynamodb:BatchWriteItem` on the Metadata_Index or ControlClaims
- **THEN** the attempt is denied with an insufficient-permissions error

#### Scenario: [ACCESS-007.03] Pruner session without MFA is refused

- **WHEN** a designated principal attempts to assume the pruner role without MFA or without setting a source identity
- **THEN** the assume-role call is denied

#### Scenario: [ACCESS-007.05] Only IAM users can be designated pruners

- **WHEN** a deployment is synthesized with a designated pruner principal that is a role, or an IAM user in another account
- **THEN** synthesis fails and names the rejected principal

#### Scenario: [ACCESS-007.06] cairn prune obtains the pruner session itself

- **WHEN** a designated IAM user runs `cairn prune` with digests and with credentials that are not already a pruner session
- **THEN** `cairn prune` prompts for an MFA code and calls `sts:AssumeRole` on the pruner role with that code and a source identity equal to the user name, and refuses to delete if the call fails

#### Scenario: [ACCESS-007.04] Pruner cannot delete outside its project

- **WHEN** a pruner session for project X attempts to delete an image in project Y's repository or an S3 object version under project Y's archive prefix
- **THEN** the attempt is denied with an insufficient-permissions error

### Requirement: [ACCESS-008] Pruner Cannot Alter Retention

The pruner role SHALL NOT include `s3:BypassGovernanceRetention`, `s3:PutObjectRetention`, `s3:PutObjectLegalHold`, or any permission that changes an ECR lifecycle policy, the archive bucket's lifecycle configuration, or a retention SSM parameter. The pruner role SHALL be able to append to the prune audit log group and SHALL NOT be able to delete it, delete its streams, or change its retention.

#### Scenario: [ACCESS-008.01] Pruner grant cannot shorten or bypass retention

- **WHEN** a principal holding only the pruner role attempts `s3:PutObjectRetention`, `s3:PutObjectLegalHold`, a governance-retention bypass, or a change to an ECR lifecycle policy
- **THEN** each attempt is denied with an insufficient-permissions error

#### Scenario: [ACCESS-008.02] Pruner cannot delete a retained object version

- **WHEN** a principal holding the pruner role attempts `s3:DeleteObjectVersion` on an object version whose `ObjectLockRetainUntilDate` is in the future in a `WORM_Enabled=true` deployment
- **THEN** S3 Object Lock denies the delete and the object version remains intact

#### Scenario: [ACCESS-008.03] Pruner cannot erase the audit trail

- **WHEN** a principal holding the pruner role attempts to delete the prune audit log group, delete one of its streams, or change its retention
- **THEN** each attempt is denied with an insufficient-permissions error

### Requirement: [ACCESS-009] Operator Cannot Change Retention Configuration

The Operator principal SHALL be explicitly denied:

1. `ecr:PutLifecyclePolicy`, `ecr:SetRepositoryPolicy`, and `ecr:DeleteRepositoryPolicy` on the artifact repositories;
2. `s3:PutLifecycleConfiguration`, `s3:PutBucketObjectLockConfiguration`, and `s3:PutObjectLegalHold` on the archive bucket;
3. `ssm:PutParameter`, `ssm:LabelParameterVersion`, and `ssm:DeleteParameter` on both retention parameters;
4. `lambda:UpdateFunctionConfiguration` on the archive stage;
5. any change, disablement, or retargeting of the alarm rules, alarms, and notification topic of [RETAIN-013](../retention-lifecycle/spec.md#requirement-retain-013-alarm-on-deletion-and-retention-configuration-changes).

No pipeline role SHALL hold `s3:PutObjectLegalHold`.

#### Scenario: [ACCESS-009.01] Operator cannot add a lifecycle rule

- **WHEN** a principal holding the Operator policy attempts to put an ECR lifecycle policy on an artifact repository or a lifecycle configuration on the archive bucket
- **THEN** the attempt is denied with an insufficient-permissions error

#### Scenario: [ACCESS-009.02] Operator cannot shorten the retention period

- **WHEN** a principal holding the Operator policy attempts to write the retention-period or unarchived-manifest-window SSM parameter, or update the archive stage's function configuration
- **THEN** the attempt is denied with an insufficient-permissions error

#### Scenario: [ACCESS-009.03] No pipeline role can set or clear a legal hold

- **WHEN** the IAM policies are provisioned
- **THEN** no pipeline Lambda execution role holds `s3:PutObjectLegalHold`

#### Scenario: [ACCESS-009.04] Operator cannot change a repository policy

- **WHEN** a principal holding the Operator policy attempts to set or delete the repository policy of an artifact repository
- **THEN** the attempt is denied with an insufficient-permissions error

#### Scenario: [ACCESS-009.05] Operator cannot silence the deletion alarms

- **WHEN** a principal holding the Operator policy attempts to disable, delete, or retarget a [RETAIN-013](../retention-lifecycle/spec.md#requirement-retain-013-alarm-on-deletion-and-retention-configuration-changes) alarm rule, disable its alarm actions, or unsubscribe from its notification topic
- **THEN** the attempt is denied with an insufficient-permissions error

### Requirement: [ACCESS-010] Stack-Owned Artifact Repository Policy

Each artifact repository SHALL carry a stack-owned repository policy, with every exception expressed as an `aws:PrincipalArn` condition, that:

1. denies `ecr:BatchDeleteImage` to every principal except the project's pruner role and, only in a non-production deployment, the integration-test cleanup role and the stack deployment role;
2. denies `ecr:UpdateImageStorageClass` to every principal;
3. denies `ecr:DeleteRepository`, `ecr:PutLifecyclePolicy`, `ecr:DeleteLifecyclePolicy`, `ecr:SetRepositoryPolicy`, and `ecr:DeleteRepositoryPolicy` to every principal except the stack deployment role;
4. grants nothing cross-account.

#### Scenario: [ACCESS-010.01] Only the pruner can delete images

- **WHEN** any principal other than the project's pruner role, including one granted `ecr:BatchDeleteImage` by an identity policy, attempts to delete an image from an artifact repository
- **THEN** the repository policy denies the delete

#### Scenario: [ACCESS-010.02] Only the stack deployment can change lifecycle or repository policy

- **WHEN** any principal other than the stack deployment role attempts to put or delete a lifecycle policy or a repository policy on an artifact repository
- **THEN** the repository policy denies the attempt

#### Scenario: [ACCESS-010.04] No principal can archive or force-delete around the pruner

- **WHEN** any principal other than the stack deployment role attempts to delete an artifact repository, or any principal attempts to move an image to the ECR archive storage class
- **THEN** the repository policy denies the attempt

#### Scenario: [ACCESS-010.03] No cross-account grant

- **WHEN** the stack-owned repository policy is synthesized
- **THEN** it contains no Allow statement whose principal lies outside the deployment's account
