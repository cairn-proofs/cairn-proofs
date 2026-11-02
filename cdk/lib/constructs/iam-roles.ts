import { IKey } from 'aws-cdk-lib/aws-kms';
import { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { IRule } from 'aws-cdk-lib/aws-events';
import { ILogGroup } from 'aws-cdk-lib/aws-logs';
import { IBucket } from 'aws-cdk-lib/aws-s3';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import {
  ArnPrincipal,
  CompositePrincipal,
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  PrincipalBase,
  PrincipalPolicyFragment,
  Role,
} from 'aws-cdk-lib/aws-iam';
import { Duration, Stack } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { Naming } from '../naming';

/**
 * Trust conditions every pruner assume-role call must satisfy
 * (access-control [ACCESS-007]): MFA present (a `Bool` match, never
 * `BoolIfExists`), and a source identity equal to the caller's own IAM user
 * name, so every pruner session is attributable to one person in CloudTrail.
 */
export const PRUNER_TRUST_CONDITIONS = {
  Bool: { 'aws:MultiFactorAuthPresent': 'true' },
  StringEquals: { 'sts:SourceIdentity': '${aws:username}' },
};

/**
 * Trust principal for the pruner role. It admits only the designated principal
 * ARNs, and it grants `sts:SetSourceIdentity` alongside `sts:AssumeRole` in one
 * conditioned statement, which the stock CDK principals cannot express.
 */
class DesignatedPrunerPrincipals extends PrincipalBase {
  constructor(private readonly arns: string[]) {
    super();
  }

  get policyFragment(): PrincipalPolicyFragment {
    return new PrincipalPolicyFragment({ AWS: this.arns }, PRUNER_TRUST_CONDITIONS);
  }

  addToAssumeRolePolicy(doc: PolicyDocument): void {
    doc.addStatements(
      new PolicyStatement({
        actions: ['sts:AssumeRole', 'sts:SetSourceIdentity'],
        principals: this.arns.map((arn) => new ArnPrincipal(arn)),
        conditions: PRUNER_TRUST_CONDITIONS,
      }),
    );
  }

  toString(): string {
    return `DesignatedPrunerPrincipals(${this.arns.join(',')})`;
  }

  dedupeString(): string | undefined {
    return undefined;
  }
}

export interface IamPolicyTemplatesProps {
  readonly naming: Naming;
  /** Opaque org id — leading segment of the project repository path. */
  readonly orgId: string;
  /** Opaque tenant id — middle segment of the project repository path. */
  readonly tenantId: string;
  /** Opaque project id — trailing segment of the project repository path. */
  readonly projectId: string;
  readonly archiveBucket: IBucket;
  readonly metadataTable: ITable;
  readonly kmsKey: IKey;
  /**
   * Versioned ARN of the `{appName}_artifact_reviewer_signoff` AWS Signer
   * profile (`artifact-verifiability`, signing [SIGN-003]). The reviewer sign-off
   * grant is scoped to this profile and lives on a DISTINCT policy, never
   * composed into the read-only auditor policy (Threat #4).
   */
  readonly reviewerSignoffProfileArn: string;
  /**
   * The deployment stage (e.g. `alpha`) — used to derive the SSM parameter
   * name of the rendered Notation trust policy so the write-protect grant can
   * scope `ssm:PutParameter` to exactly that parameter (Threat #9).
   */
  readonly stage: string;
  /** ControlClaims table — the pruner deletes a pruned bundle's rows here. */
  readonly controlsTable: ITable;
  /** Stack-owned prune audit log group ([RETAIN-011]); the pruner may only append. */
  readonly pruneAuditLogGroup: ILogGroup;
  /** SSM path of the retention-period parameter ([RETAIN-007]). */
  readonly retentionParamName: string;
  /** SSM path of the unarchived-manifest-window parameter ([RETAIN-007], [RETAIN-008]). */
  readonly windowParamName: string;
  /** Physical name of the archive-to-s3 Lambda (Operator DENY on its configuration). */
  readonly archiveFunctionName: string;
  /** The [RETAIN-013] alarm rules; the Operator may not change or disable them. */
  readonly retentionAlarmRules: IRule[];
  /** The notification topic the [RETAIN-013] rules publish to. */
  readonly alarmTopic: ITopic;
  /**
   * IAM user ARNs, in the deployment account, that may assume the pruner role
   * ([ACCESS-007]). Must be non-empty.
   */
  readonly prunerPrincipalArns: string[];
  /**
   * IAM user or role ARNs, in the deployment account, that may assume the
   * non-production integration-test cleanup role ([ACCESS-010] item 1). Must
   * be non-empty when `isProd` is false.
   */
  readonly integCleanupPrincipalArns: string[];
  /**
   * Non-production gate: the integration-test cleanup role is synthesized only
   * when this is `false` ([ACCESS-010] item 1).
   */
  readonly isProd: boolean;
}

/**
 * Reusable IAM policy templates:
 *
 * - `producerPolicy`: ECR push scope for an Artifact_Producer (any service
 *   pushing into the project's repositories). Attached to a producer IAM
 *   role or user; not provisioned to a specific principal here. The producer
 *   identity is the IAM principal whose userIdentity flows into the
 *   `authenticatedPushPrincipal` field per [PIPELINE-010].
 *
 * - `auditorPolicy`: read-only ECR pull + S3 read + DynamoDB query against
 *   the metadata table. Attached to a downstream auditor or verifier role.
 *
 * - `aliasMoverPolicy`: the dedicated alias-movement boundary
 *   (access-control *Alias Movement Authorization Distinct
 *   from Push and Sign*). Grants the ECR permissions a `cairn alias set/move`
 *   needs to repoint an existing manifest's alias tag (`ecr:PutImage` to write
 *   the alias tag, `ecr:BatchGetImage` to read the target manifest for the
 *   filed-digest precondition). It is a SEPARATE policy from `producerPolicy`
 *   and from the signing principal — holding producer or signer permissions does
 *   NOT confer alias movement. A producer that can push evidence cannot, by that
 *   fact alone, move `nist-800-53-latest`.
 *
 *   IAM limitation (NOT a CDK gap): ECR `ecr:PutImage` cannot be conditioned on
 *   the tag string — there is no IAM condition key that distinguishes a write to
 *   an `_als`/`_chn` alias tag from a write to a canonical `_ref` tag. So "only
 *   this policy may write alias-namespace tags" is enforced at the
 *   principal/role granularity (this distinct policy attached to a distinct
 *   alias-mover role) PLUS the `cairn alias` wrapper's filed-digest + reserved-
 *   namespace grammar checks — NOT by an IAM tag condition. The IAM scoping (a
 *   separate role) is the load-bearing control; the wrapper is client-side.
 *
 * Per-Lambda execution roles are NOT provisioned here — they live next to
 * each Lambda construct in the merge pass so each role can scope to only
 * the resources its function actually needs (least privilege per Lambda).
 * These templates are the shared building blocks (e.g., the project-scoped
 * ECR push statement can be reused by the producer-role template and any
 * future cross-account producer role).
 */
export class IamPolicyTemplates extends Construct {
  readonly producerPolicy: ManagedPolicy;
  readonly auditorPolicy: ManagedPolicy;
  readonly aliasMoverPolicy: ManagedPolicy;
  readonly operatorPolicy: ManagedPolicy;
  readonly reviewerSignoffPolicy: ManagedPolicy;
  readonly trustPolicyWriteProtectPolicy: ManagedPolicy;
  /** Per-project pruner role ([ACCESS-007], [ACCESS-008]). */
  readonly prunerRole: Role;
  /**
   * Non-production integration-test cleanup role ([ACCESS-010] item 1).
   * `undefined` on a production (`isProd=true`) stack.
   */
  readonly integrationCleanupRole?: Role;

  constructor(scope: Construct, id: string, props: IamPolicyTemplatesProps) {
    super(scope, id);

    const {
      naming,
      orgId,
      tenantId,
      projectId,
      archiveBucket,
      metadataTable,
      controlsTable,
      kmsKey,
      reviewerSignoffProfileArn,
      stage,
      pruneAuditLogGroup,
      retentionParamName,
      windowParamName,
      archiveFunctionName,
      retentionAlarmRules,
      alarmTopic,
      prunerPrincipalArns,
      integCleanupPrincipalArns,
      isProd,
    } = props;
    if (prunerPrincipalArns.length === 0) {
      throw new Error(
        'prunerPrincipalArns must name at least one designated pruner principal ([ACCESS-007]).',
      );
    }
    const account = Stack.of(this).account;
    const region = Stack.of(this).region;
    const partition = Stack.of(this).partition;
    const artifactNamespace = naming.artifactNamespace;

    // Single repository per project, addressed through the uniform three-segment
    // logical tenant hierarchy {org-id}/{tenant-id}/{project-id}
    // (access-control [ACCESS-002]):
    //   arn:aws:ecr:<region>:<acct>:repository/{appName}-artifact-<qualifier>/{org}/{tenant}/{project}
    // The repository path derives from the SAME naming.artifactNamespace as the
    // ECR repo construct and the Notation trust scope — IAM stays
    // repo-path-scoped, NEVER widened to `repository/*` or a bare `{appName}`
    // prefix. Push is scoped to the BARE project-repo ARN: a project
    // maps to a single repository with no child repository-path segment, so
    // there is no `/*` sub-repository form to grant ([ACCESS-002]).
    const projectRepoArn = `arn:aws:ecr:${region}:${account}:repository/${artifactNamespace}/${orgId}/${tenantId}/${projectId}`;

    // ── Producer policy ───────────────────────────────────────────────────
    this.producerPolicy = new ManagedPolicy(this, 'ProducerPolicy', {
      managedPolicyName: naming.nameFor('producer'),
      description: `ECR push scope for ${projectId} producers (Cairn Proofs)`,
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'EcrAuthToken',
            effect: Effect.ALLOW,
            actions: ['ecr:GetAuthorizationToken'],
            resources: ['*'],
          }),
          new PolicyStatement({
            // A producer submission is TWO OCI objects — the
            // content manifest AND a Producer_Attestation_Referrer (artifactType
            // `application/vnd.cairnproofs.producer-attestation.v1+json`, subject =
            // content digest) carrying the producer claim. The referrer is an
            // ordinary OCI manifest `PutImage` into the SAME project repo as the
            // content manifest, so this push grant authorizes both objects;
            // there is no separate IAM action for "push a referrer".
            sid: 'EcrPushToProjectRepos',
            effect: Effect.ALLOW,
            actions: [
              'ecr:BatchCheckLayerAvailability',
              'ecr:CompleteLayerUpload',
              'ecr:InitiateLayerUpload',
              'ecr:UploadLayerPart',
              'ecr:PutImage',
              'ecr:DescribeRepositories',
              'ecr:DescribeImages',
            ],
            // Bare project-repo ARN only — a project is a single repository with
            // no child repository-path segment, so there is no `/*`
            // sub-repository form to grant ([ACCESS-002]).
            resources: [projectRepoArn],
          }),
          new PolicyStatement({
            sid: 'KmsEncryptForEcrPush',
            effect: Effect.ALLOW,
            actions: ['kms:Encrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
            resources: [kmsKey.keyArn],
            conditions: {
              StringEquals: { 'kms:ViaService': `ecr.${region}.amazonaws.com` },
            },
          }),
        ],
      }),
    });

    // ── Alias-mover policy ─────────────────────────────────────────
    // Dedicated alias-movement boundary, distinct from producer push and from
    // the signing principal. Attach to the alias-mover role the technical
    // operator (Sam/Priya) assumes to run `cairn alias set/move` on Nadia's
    // behalf. NOT attached to a specific principal here.
    //
    // Grants exactly the ECR surface the alias rebind needs:
    //  - ecr:PutImage      — write/repoint the `_als`/`_chn` alias tag onto an
    //                        already-filed manifest.
    //  - ecr:BatchGetImage — read the target manifest by digest for the
    //                        filed-digest precondition the wrapper enforces.
    //  - ecr:GetAuthorizationToken — registry auth (the `oras tag` /
    //                        batch-get-image → put-image idiom needs a token).
    //
    // Scoped to the bare project repo ARN (no `/*` sub-repository form). See the
    // IAM-limitation note in the class doc: PutImage cannot be tag-conditioned,
    // so alias-namespace scoping is a role-granularity control, not an IAM tag
    // condition. KMS encrypt is required because the repo is CMK-encrypted and
    // the alias tag write goes through ECR's KMS path.
    this.aliasMoverPolicy = new ManagedPolicy(this, 'AliasMoverPolicy', {
      managedPolicyName: naming.nameFor('alias-mover'),
      description: `Alias set/move boundary for ${projectId} (distinct from push and sign; Cairn Proofs)`,
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'EcrAuthToken',
            effect: Effect.ALLOW,
            actions: ['ecr:GetAuthorizationToken'],
            resources: ['*'],
          }),
          new PolicyStatement({
            sid: 'EcrAliasMoveOnProjectRepos',
            effect: Effect.ALLOW,
            actions: ['ecr:PutImage', 'ecr:BatchGetImage'],
            resources: [projectRepoArn],
          }),
          new PolicyStatement({
            sid: 'KmsForAliasMove',
            effect: Effect.ALLOW,
            actions: ['kms:Encrypt', 'kms:GenerateDataKey', 'kms:Decrypt', 'kms:DescribeKey'],
            resources: [kmsKey.keyArn],
            conditions: {
              StringEquals: { 'kms:ViaService': `ecr.${region}.amazonaws.com` },
            },
          }),
        ],
      }),
    });

    // ── Auditor policy ────────────────────────────────────────────────────
    this.auditorPolicy = new ManagedPolicy(this, 'AuditorPolicy', {
      managedPolicyName: naming.nameFor('auditor'),
      description: 'Read-only auditor scope: ECR pull, S3 read, DynamoDB query',
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'EcrAuthToken',
            effect: Effect.ALLOW,
            actions: ['ecr:GetAuthorizationToken'],
            resources: ['*'],
          }),
          new PolicyStatement({
            sid: 'EcrPullAllProjectRepos',
            effect: Effect.ALLOW,
            actions: [
              'ecr:BatchCheckLayerAvailability',
              'ecr:BatchGetImage',
              'ecr:GetDownloadUrlForLayer',
              'ecr:DescribeRepositories',
              'ecr:DescribeImages',
              'ecr:ListImages',
            ],
            resources: [`arn:aws:ecr:${region}:${account}:repository/${artifactNamespace}/*`],
          }),
          new PolicyStatement({
            sid: 'S3ReadArchive',
            effect: Effect.ALLOW,
            actions: ['s3:GetObject', 's3:GetObjectVersion', 's3:ListBucket'],
            resources: [archiveBucket.bucketArn, `${archiveBucket.bucketArn}/*`],
          }),
          new PolicyStatement({
            // Read-only query surface (access-control [ACCESS-003]). Tenant
            // isolation is DynamoDB-native: the tenant-id is the partition key,
            // scoped exact-match by `dynamodb:LeadingKeys` (NOT a partition-key
            // prefix), and project scope is a sort-key `begins_with` within the
            // auditor's tenant partition. `dynamodb:Scan` is intentionally
            // ABSENT so no unscoped table sweep can reach records outside the
            // caller's `LeadingKeys`-bound tenant. The `/index/*` resource
            // covers any future GSI — and because every GSI on a tenant-PK plane
            // leads with `{tenant-id}` as its partition key ([META-001] item 4),
            // the same `LeadingKeys` exact-match binds a GSI query, so an auditor
            // cannot escape its tenant via an alternate index ([ACCESS-003.03]).
            sid: 'DdbQueryMetadata',
            effect: Effect.ALLOW,
            actions: [
              'dynamodb:Query',
              'dynamodb:GetItem',
              'dynamodb:BatchGetItem',
              'dynamodb:DescribeTable',
            ],
            resources: [metadataTable.tableArn, `${metadataTable.tableArn}/index/*`],
          }),
          new PolicyStatement({
            sid: 'KmsDecryptForReads',
            effect: Effect.ALLOW,
            actions: ['kms:Decrypt', 'kms:DescribeKey'],
            resources: [kmsKey.keyArn],
            conditions: {
              StringEquals: {
                'kms:ViaService': [
                  `ecr.${region}.amazonaws.com`,
                  `s3.${region}.amazonaws.com`,
                  `dynamodb.${region}.amazonaws.com`,
                ],
              },
            },
          }),
        ],
      }),
    });

    // ── Operator policy (access-control [ACCESS-006]) ─────────────────────────
    // The Operator is the control-plane principal that deploys and runs the
    // substrate. It is UNTRUSTED FOR INTEGRITY by construction: artifact
    // integrity is anchored in the data plane (the Manifest_Digest, the
    // signature, the WORM_Receipt), never conferred by the control plane, so no
    // Operator permission can forge or move an artifact's verification verdict.
    //
    // The policy is two halves:
    //  (1) ALLOW — the management/operational surface to provision and run the
    //      ECR, S3, DynamoDB, EventBridge, Lambda, KMS, and Signer substrate
    //      (describe/list/get + config + observability). It deliberately does
    //      NOT include `ecr:PutImage` (so the Operator cannot write or move an
    //      alias tag — operating does NOT confer the alias-mover grant of
    //      [ACCESS-005], satisfying [ACCESS-006.01]) and NO signing action.
    //  (2) DENY — an explicit guardrail bounding the delete/DoS/read blast
    //      radius and re-affirming the integrity boundary even
    //      if a broader policy were ever attached alongside: no manifest/alias
    //      write, no image/repo/table deletion, no archive-object mutate/delete,
    //      no data-plane item write/delete, no signing, no evidence-plaintext
    //      `kms:Decrypt`, and no key disable/destroy. On the alpha
    //      (`WORM_Enabled=false`) the Object Lock delete-backstop is inactive,
    //      so this least-privilege bound is the operative control on delete;
    //      production (`WORM_Enabled=true`) adds Object Lock behind
    //      it.
    const operatorEcrNamespaceArn = `arn:${partition}:ecr:${region}:${account}:repository/${artifactNamespace}/*`;
    const retentionParamArn = `arn:${partition}:ssm:${region}:${account}:parameter${retentionParamName}`;
    const windowParamArn = `arn:${partition}:ssm:${region}:${account}:parameter${windowParamName}`;
    const archiveFunctionArn = `arn:${partition}:lambda:${region}:${account}:function:${archiveFunctionName}`;
    // Every CloudWatch alarm this deployment owns carries the `{appName}-`
    // physical-name prefix (Naming.nameFor).
    const deploymentAlarmArnPattern = `arn:${partition}:cloudwatch:${region}:${account}:alarm:${naming.appName}-*`;
    this.operatorPolicy = new ManagedPolicy(this, 'OperatorPolicy', {
      managedPolicyName: naming.nameFor('operator'),
      description:
        'Least-privilege control-plane operator: deploy/operate the substrate but untrusted for integrity — no manifest/alias write, no sign, bounded delete/DoS/read (access-control [ACCESS-006]; Cairn Proofs)',
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            // Cross-platform read-only operational visibility (describe/list/get
            // + observability). Resource-level scoping is not available for many
            // of these list/describe actions, so they are `*`-scoped reads — a
            // read action confers no integrity power.
            sid: 'OperatorReadOnlyVisibility',
            effect: Effect.ALLOW,
            actions: [
              'ecr:Describe*',
              'ecr:List*',
              'ecr:GetLifecyclePolicy',
              'ecr:GetRepositoryPolicy',
              'ecr:GetAuthorizationToken',
              's3:GetBucket*',
              's3:GetEncryptionConfiguration',
              's3:GetLifecycleConfiguration',
              's3:ListBucket',
              's3:ListAllMyBuckets',
              'dynamodb:DescribeTable',
              'dynamodb:ListTables',
              'lambda:GetFunction',
              'lambda:GetFunctionConfiguration',
              'lambda:ListFunctions',
              'events:DescribeEventBus',
              'events:DescribeRule',
              'events:ListRules',
              'events:ListTargetsByRule',
              'sqs:GetQueueAttributes',
              'sqs:ListQueues',
              'sns:GetTopicAttributes',
              'sns:ListTopics',
              'signer:GetSigningProfile',
              'signer:ListSigningProfiles',
              'kms:DescribeKey',
              'kms:GetKeyPolicy',
              'kms:GetKeyRotationStatus',
              'kms:ListAliases',
              'kms:ListKeys',
              'cloudwatch:Describe*',
              'cloudwatch:Get*',
              'cloudwatch:List*',
              'logs:Describe*',
              'logs:Get*',
              'logs:FilterLogEvents',
              'cloudformation:DescribeStacks',
              'cloudformation:DescribeStackEvents',
              'cloudformation:DescribeStackResources',
              'cloudformation:GetTemplate',
              'cloudformation:ListStackResources',
            ],
            resources: ['*'],
          }),
          new PolicyStatement({
            // Substrate management mutations needed to OPERATE (never to write
            // evidence). Scoped to this deployment's ECR namespace. Lifecycle
            // and repository-policy changes are NOT here: they are retention
            // controls, DENIED below ([ACCESS-009] item 1).
            sid: 'OperatorEcrSubstrateManagement',
            effect: Effect.ALLOW,
            actions: [
              'ecr:PutImageScanningConfiguration',
              'ecr:TagResource',
              'ecr:UntagResource',
            ],
            resources: [operatorEcrNamespaceArn],
          }),
          new PolicyStatement({
            // Operate the pipeline substrate: update Lambda configuration and
            // EventBridge wiring. No code/data-plane write — these are control-
            // plane operational knobs.
            sid: 'OperatorPipelineSubstrateManagement',
            effect: Effect.ALLOW,
            actions: [
              'lambda:UpdateFunctionConfiguration',
              'events:PutRule',
              'events:PutTargets',
              'events:EnableRule',
              'events:DisableRule',
              'cloudwatch:PutMetricAlarm',
              'cloudwatch:EnableAlarmActions',
              'cloudwatch:DisableAlarmActions',
            ],
            resources: ['*'],
          }),
          new PolicyStatement({
            // Integrity + blast-radius guardrail. An explicit
            // DENY is the load-bearing bound: even with a broader policy
            // attached, the Operator can never write or move a manifest/alias,
            // sign, delete evidence, mutate data-plane items, read evidence
            // plaintext, or disable/destroy the CMK that protects all data.
            sid: 'OperatorIntegrityAndBlastRadiusGuardrail',
            effect: Effect.DENY,
            actions: [
              // No manifest register / alias move — keeps the Operator outside
              // the integrity path and off the alias-mover boundary ([ACCESS-006.01]).
              'ecr:PutImage',
              'ecr:BatchDeleteImage',
              'ecr:DeleteRepository',
              'ecr:DeleteRepositoryPolicy',
              // No signing — the Operator cannot mint a verification verdict.
              'signer:SignPayload',
              'signer:StartSigningJob',
              'signer:PutSigningProfile',
              // Bounded archive delete/forge — WORM backstops this only
              // when WORM_Enabled=true; the deny is the operative bound on the alpha.
              's3:DeleteObject',
              's3:DeleteObjectVersion',
              's3:PutObject',
              's3:PutObjectRetention',
              's3:BypassGovernanceRetention',
              's3:PutBucketPolicy',
              's3:DeleteBucket',
              // Bounded data-plane tamper/delete.
              'dynamodb:PutItem',
              'dynamodb:UpdateItem',
              'dynamodb:DeleteItem',
              'dynamodb:DeleteTable',
              // Bounded read: no evidence-plaintext decrypt; bounded DoS:
              // no key disable / scheduled deletion.
              'kms:Decrypt',
              'kms:DisableKey',
              'kms:DisableKeyRotation',
              'kms:ScheduleKeyDeletion',
            ],
            resources: ['*'],
          }),
          // ── Retention-configuration guardrail ([ACCESS-009]) ──────────────
          // Object Lock honors whatever date the archive stage writes, and the
          // pruner trusts the receipt the archive stage writes, so every knob
          // that sets, shortens, or hides retention is out of the Operator's
          // reach. Only the stack deployment changes them, and each change
          // raises a [RETAIN-013] alarm.
          new PolicyStatement({
            // [ACCESS-009] item 1: lifecycle and repository policy.
            sid: 'OperatorDenyEcrRetentionConfig',
            effect: Effect.DENY,
            actions: [
              'ecr:PutLifecyclePolicy',
              'ecr:DeleteLifecyclePolicy',
              'ecr:SetRepositoryPolicy',
              'ecr:DeleteRepositoryPolicy',
            ],
            resources: [operatorEcrNamespaceArn],
          }),
          new PolicyStatement({
            // [ACCESS-009] item 2: archive bucket lifecycle, Object Lock, legal hold.
            sid: 'OperatorDenyArchiveRetentionConfig',
            effect: Effect.DENY,
            actions: [
              's3:PutLifecycleConfiguration',
              's3:PutBucketObjectLockConfiguration',
              's3:PutObjectLegalHold',
            ],
            resources: [archiveBucket.bucketArn, archiveBucket.arnForObjects('*')],
          }),
          new PolicyStatement({
            // [ACCESS-009] item 3: the retention-period and
            // unarchived-manifest-window parameters.
            sid: 'OperatorDenyRetentionParameterWrite',
            effect: Effect.DENY,
            actions: ['ssm:PutParameter', 'ssm:LabelParameterVersion', 'ssm:DeleteParameter'],
            resources: [retentionParamArn, windowParamArn],
          }),
          new PolicyStatement({
            // [ACCESS-009] item 4: the archive stage's configuration (it carries
            // the retention-parameter name).
            sid: 'OperatorDenyArchiveStageConfig',
            effect: Effect.DENY,
            actions: ['lambda:UpdateFunctionConfiguration'],
            resources: [archiveFunctionArn],
          }),
          new PolicyStatement({
            // [ACCESS-009] item 5: the [RETAIN-013] alarm rules.
            sid: 'OperatorDenyRetentionAlarmRules',
            effect: Effect.DENY,
            actions: [
              'events:PutRule',
              'events:PutTargets',
              'events:RemoveTargets',
              'events:DisableRule',
              'events:DeleteRule',
            ],
            resources: retentionAlarmRules.map((r) => r.ruleArn),
          }),
          new PolicyStatement({
            // [ACCESS-009] item 5: this deployment's CloudWatch alarms.
            sid: 'OperatorDenyDeploymentAlarms',
            effect: Effect.DENY,
            actions: [
              'cloudwatch:PutMetricAlarm',
              'cloudwatch:DisableAlarmActions',
              'cloudwatch:DeleteAlarms',
            ],
            resources: [deploymentAlarmArnPattern],
          }),
          new PolicyStatement({
            // [ACCESS-009] item 5: the notification topic.
            sid: 'OperatorDenyAlarmTopicChanges',
            effect: Effect.DENY,
            actions: [
              'sns:Unsubscribe',
              'sns:SetTopicAttributes',
              'sns:DeleteTopic',
              'sns:RemovePermission',
            ],
            resources: [alarmTopic.topicArn],
          }),
        ],
      }),
    });

    // ── Reviewer sign-off policy (artifact-verifiability, signing [SIGN-003]) ────
    // The grant to create a reviewer sign-off signature with the
    // `{appName}_artifact_reviewer_signoff` Signer profile. This is a DISTINCT,
    // sign-only identity — it is NOT attached to or composed into the read-only
    // `auditorPolicy` above (Threat #4: a read-only auditor principal must not,
    // by that fact alone, be able to sign). The signer is the Attesting Reviewer
    // (Quinn), distinct from the read-only auditors. Attach this to the
    // dedicated attesting-reviewer role only. Scoped to exactly the reviewer
    // profile ARN (never `*`). `signer:DescribeSigningJob` lets the signer poll
    // the job it started; no read-evidence (ECR/S3/DDB) actions appear here.
    this.reviewerSignoffPolicy = new ManagedPolicy(this, 'ReviewerSignoffPolicy', {
      managedPolicyName: naming.nameFor('reviewer-signoff'),
      description:
        'Distinct sign-only grant for artifact-reviewer-signoff (NOT attached to the read-only auditor role; Cairn Proofs)',
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'SignerReviewerSignoff',
            effect: Effect.ALLOW,
            actions: ['signer:SignPayload', 'signer:StartSigningJob', 'signer:DescribeSigningJob'],
            resources: [reviewerSignoffProfileArn],
          }),
        ],
      }),
    });

    // ── Trust-policy SSM write-protect (Threat #9) ────────────────────────────
    // The rendered Notation trust policy lives at SSM
    // `/{appName}/{stage}/notation/trust-policy` and is the operator-shipped
    // verification policy. A silent substitution of that parameter could admit
    // an attacker-controlled identity, so write access (`ssm:PutParameter`,
    // plus tag/label mutations that re-point the active version) is restricted
    // to the deploy/admin path. This policy is an explicit DENY scoped to the
    // trust-policy parameter ARN — attach it (boundary or direct) to every
    // non-deploy principal so the parameter cannot be silently substituted.
    // The deploy/admin role is exempted by NOT carrying this deny; CDK/CFN
    // writes the parameter through the CloudFormation execution role on deploy.
    const trustPolicyParamArn = `arn:${partition}:ssm:${region}:${account}:parameter${naming.ssmPath(stage, 'notation/trust-policy')}`;
    this.trustPolicyWriteProtectPolicy = new ManagedPolicy(this, 'TrustPolicyWriteProtectPolicy', {
      managedPolicyName: naming.nameFor('trust-policy-write-protect'),
      description:
        'Denies write/relabel of the Notation trust-policy SSM parameter to non-deploy principals (Threat #9; Cairn Proofs)',
      document: new PolicyDocument({
        statements: [
          new PolicyStatement({
            sid: 'DenyTrustPolicySubstitution',
            effect: Effect.DENY,
            actions: ['ssm:PutParameter', 'ssm:LabelParameterVersion', 'ssm:DeleteParameter'],
            resources: [trustPolicyParamArn],
          }),
        ],
      }),
    });

    // ── Pruner role (access-control [ACCESS-007], [ACCESS-008]) ───────────────
    // The only sanctioned grant for `cairn prune` deletions, scoped to this
    // stack's single project. The trust policy admits only the designated
    // principals, with MFA and a caller-bound source identity, for at most one
    // hour. IAM cannot condition a delete on retention state, so the pruner's
    // eligibility checks run client-side; the bound on raw-API misuse is this
    // narrow trust, project scope, the [ACCESS-010] repository policy, and the
    // [RETAIN-013] alarm on every image delete.
    const projectPrefix = `${orgId}/${tenantId}/${projectId}`;
    const receiptCopyArn = archiveBucket.arnForObjects(`${projectPrefix}/*/worm-receipt.json`);
    const ssmDeploymentParamsArn = `arn:${partition}:ssm:${region}:${account}:parameter/${naming.appName}/${stage}/*`;
    const ssmAppNameParamArn = `arn:${partition}:ssm:${region}:${account}:parameter/cairnproofs/${naming.qualifier}/app-name`;
    this.prunerRole = new Role(this, 'PrunerRole', {
      roleName: naming.nameFor('pruner'),
      description: `cairn prune deletion boundary for ${projectId} (MFA + SourceIdentity, 1h; Cairn Proofs)`,
      assumedBy: new DesignatedPrunerPrincipals(prunerPrincipalArns),
      maxSessionDuration: Duration.hours(1),
    });
    const prunerStatements: PolicyStatement[] = [
      new PolicyStatement({
        // Classification reads + the one delete on the project repository.
        sid: 'PrunerEcrProjectRepo',
        effect: Effect.ALLOW,
        actions: [
          'ecr:DescribeRepositories',
          'ecr:DescribeImages',
          'ecr:ListImages',
          'ecr:BatchGetImage',
          'ecr:BatchDeleteImage',
        ],
        resources: [projectRepoArn],
      }),
      new PolicyStatement({
        // Version listing and Object Lock reads, bounded to the project prefix.
        sid: 'PrunerS3ListProjectPrefix',
        effect: Effect.ALLOW,
        actions: ['s3:ListBucket', 's3:ListBucketVersions'],
        resources: [archiveBucket.bucketArn],
        conditions: { StringLike: { 's3:prefix': [`${projectPrefix}/*`] } },
      }),
      new PolicyStatement({
        // WORM detection: a positive Object Lock configuration read.
        sid: 'PrunerS3ObjectLockConfigRead',
        effect: Effect.ALLOW,
        actions: ['s3:GetBucketObjectLockConfiguration'],
        resources: [archiveBucket.bucketArn],
      }),
      new PolicyStatement({
        // The authoritative S3 receipt copy ([RETAIN-012]) — the only object
        // body the pruner reads.
        sid: 'PrunerS3ReceiptCopyRead',
        effect: Effect.ALLOW,
        actions: ['s3:GetObject', 's3:GetObjectVersion'],
        resources: [receiptCopyArn],
      }),
      new PolicyStatement({
        // Retention + legal-hold reads, and the per-version delete, under the
        // project prefix only.
        sid: 'PrunerS3ProjectObjectVersions',
        effect: Effect.ALLOW,
        actions: ['s3:GetObjectRetention', 's3:GetObjectLegalHold', 's3:DeleteObjectVersion'],
        resources: [archiveBucket.arnForObjects(`${projectPrefix}/*`)],
      }),
      new PolicyStatement({
        // Index existence check (strongly consistent GetItem), ControlClaims
        // row lookup, and the final row deletes — tenant partition only.
        sid: 'PrunerDdbTenantRows',
        effect: Effect.ALLOW,
        actions: ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:DeleteItem'],
        resources: [metadataTable.tableArn, controlsTable.tableArn],
        conditions: { 'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': [tenantId] } },
      }),
      new PolicyStatement({
        // Append-only audit trail ([RETAIN-011], [ACCESS-008]).
        sid: 'PrunerAuditAppend',
        effect: Effect.ALLOW,
        actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [pruneAuditLogGroup.logGroupArn, `${pruneAuditLogGroup.logGroupArn}:log-stream:*`],
      }),
      new PolicyStatement({
        // Deployment discovery: appName, table names, retention settings, the
        // audit log group name, and this role's own ARN.
        sid: 'PrunerDeploymentParamRead',
        effect: Effect.ALLOW,
        actions: ['ssm:GetParameter'],
        resources: [ssmDeploymentParamsArn, ssmAppNameParamArn],
      }),
      new PolicyStatement({
        // Decrypt the S3 receipt copy only. With S3 Bucket Keys off, the S3
        // encryption context is the object ARN, so no evidence blob decrypts.
        // With Bucket Keys on, the context is the bucket ARN and this denies
        // every decrypt (fails safe).
        sid: 'PrunerKmsDecryptReceiptCopy',
        effect: Effect.ALLOW,
        actions: ['kms:Decrypt'],
        resources: [kmsKey.keyArn],
        conditions: {
          StringEquals: { 'kms:ViaService': `s3.${region}.amazonaws.com` },
          StringLike: {
            'kms:EncryptionContext:aws:s3:arn': receiptCopyArn,
          },
        },
      }),
      new PolicyStatement({
        // Decrypt for the DynamoDB reads and deletes on the two index tables.
        sid: 'PrunerKmsDecryptIndexTables',
        effect: Effect.ALLOW,
        actions: ['kms:Decrypt'],
        resources: [kmsKey.keyArn],
        conditions: {
          StringEquals: {
            'kms:ViaService': `dynamodb.${region}.amazonaws.com`,
            'kms:EncryptionContext:aws:dynamodb:tableName': [
              metadataTable.tableName,
              controlsTable.tableName,
            ],
          },
        },
      }),
      new PolicyStatement({
        // [ACCESS-008]: the pruner can never shorten, bypass, or hide retention,
        // nor erase its own audit trail — even if a broader policy is attached.
        sid: 'PrunerCannotAlterRetention',
        effect: Effect.DENY,
        actions: [
          's3:BypassGovernanceRetention',
          's3:PutObjectRetention',
          's3:PutObjectLegalHold',
          's3:PutLifecycleConfiguration',
          's3:PutBucketObjectLockConfiguration',
          'ecr:PutLifecyclePolicy',
          'ecr:DeleteLifecyclePolicy',
          'ecr:SetRepositoryPolicy',
          'ecr:DeleteRepositoryPolicy',
          'ssm:PutParameter',
          'ssm:LabelParameterVersion',
          'ssm:DeleteParameter',
          'logs:DeleteLogGroup',
          'logs:DeleteLogStream',
          'logs:PutRetentionPolicy',
          'logs:DeleteRetentionPolicy',
        ],
        resources: ['*'],
      }),
    ];
    for (const s of prunerStatements) {
      this.prunerRole.addToPrincipalPolicy(s);
    }

    // ── Integration-test cleanup role (non-production only, [ACCESS-010]) ────
    // Automation cannot satisfy the pruner's MFA condition, so the integration
    // harness deletes its own test images through this role. Its trust names
    // only the designated cleanup principals (by default the designated
    // pruners), never the account root, so an account admin's blanket
    // sts:AssumeRole grant does not reach it. A production stack never
    // synthesizes it.
    if (!isProd) {
      if (integCleanupPrincipalArns.length === 0) {
        throw new Error(
          'integCleanupPrincipalArns must name at least one principal ([ACCESS-010] item 1).',
        );
      }
      this.integrationCleanupRole = new Role(this, 'IntegrationCleanupRole', {
        roleName: naming.nameFor('integ-cleanup'),
        description: 'Non-production integration-test image cleanup (Cairn Proofs)',
        assumedBy: new CompositePrincipal(
          ...integCleanupPrincipalArns.map((arn) => new ArnPrincipal(arn)),
        ),
        maxSessionDuration: Duration.hours(1),
      });
      this.integrationCleanupRole.addToPrincipalPolicy(
        new PolicyStatement({
          sid: 'IntegrationCleanupEcrProjectRepo',
          effect: Effect.ALLOW,
          actions: ['ecr:DescribeImages', 'ecr:BatchGetImage', 'ecr:BatchDeleteImage'],
          resources: [projectRepoArn],
        }),
      );
    }
  }
}
