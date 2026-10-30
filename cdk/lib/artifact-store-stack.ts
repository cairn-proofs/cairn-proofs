import { Aspects, CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import { StringParameter } from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { Naming } from './naming';
import { Encryption } from './constructs/encryption';
import { Repositories } from './constructs/repositories';
import { Archive } from './constructs/archive';
import { MetadataTable } from './constructs/metadata-table';
import { ControlClaimsTable } from './constructs/control-claims-table';
import { IdentityRegistry } from './constructs/identity-registry';
import { DeadLetterQueues } from './constructs/dead-letter-queues';
import { Notifications } from './constructs/notifications';
import { LogGroups } from './constructs/log-groups';
import { EventBus } from './constructs/event-bus';
import { Alarms } from './constructs/alarms';
import { Signing } from './constructs/signing';
import { IamPolicyTemplates } from './constructs/iam-roles';
import { LambdaFunctions } from './constructs/lambda-functions';
import { ProductionTeardownGuard } from './constructs/removal-policy';

export interface ArtifactStoreStackProps extends StackProps {
  /**
   * Application name — the leading segment of every AWS resource physical name
   * (`{appName}-{resource}-{qualifier}`, platform-constraints *Uniform AWS
   * Resource Naming Convention*). Defaults to `cairnproofs{token}` where the
   * token is the frozen per-deployment uniquifier from `cdk/app-config.json`.
   */
  readonly appName: string;
  /** Region code component of the Deployment_Qualifier (e.g., `cmh` for us-east-2). */
  readonly regionCode: string;
  /** Stage component of the Deployment_Qualifier (e.g., `alpha`). */
  readonly stage: string;
  /**
   * Opaque organization id — leading segment of the uniform logical tenant
   * hierarchy `{org-id}/{tenant-id}/{project-id}` baked into the ECR/S3 path.
   * Mandatory; a stable, opaque,
   * allocate-once identifier (NOT a display name).
   */
  readonly orgId: string;
  /**
   * Opaque tenant id — middle segment of the logical tenant hierarchy. A
   * LOGICAL tenant, NOT an AWS account; the producing identity is per-artifact
   * provenance, never a path segment.
   */
  readonly tenantId: string;
  /** Opaque project id — trailing segment of the logical tenant hierarchy. */
  readonly projectId: string;
  /**
   * Production-disposability gate for stateful resources. `false`
   * (non-production, e.g. alpha) ⇒ DESTROY + auto-empty so `cdk destroy` tears
   * the stack down cleanly with no orphaned resources; `true` (production) ⇒
   * RETAIN, no auto-empty. Decoupled from `wormEnabled` (see
   * `statefulRemovalPolicy`); MUST be `true` whenever `wormEnabled` is `true`.
   */
  readonly isProd: boolean;
  /**
   * Gates S3 Object Lock at archive bucket creation.
   * Irreversible after provisioning: S3 Object Lock cannot be added later.
   * Object Lock only — teardown/retention is gated separately by `isProd`,
   * which MUST be `true` whenever this is `true`.
   */
  readonly wormEnabled: boolean;
  /** Email subscribed to the alarm SNS topic. */
  readonly notificationEmail: string;
  /**
   * Opt-in to enable pipeline alarm SNS actions in a non-production deployment.
   * `isProd` forces alarm actions on regardless of this value, so the effective
   * posture is `isProd || alarmActionsEnabled`. Defaults to false.
   */
  readonly alarmActionsEnabled?: boolean;
}

export class ArtifactStoreStack extends Stack {
  readonly naming: Naming;
  readonly encryption: Encryption;
  readonly repositories: Repositories;
  readonly archive: Archive;
  readonly metadataTable: MetadataTable;
  readonly controlClaimsTable: ControlClaimsTable;
  readonly identityRegistry: IdentityRegistry;
  readonly deadLetterQueues: DeadLetterQueues;
  readonly notifications: Notifications;
  readonly logGroups: LogGroups;
  readonly eventBus: EventBus;
  readonly alarms: Alarms;
  readonly signing: Signing;
  readonly iamPolicyTemplates: IamPolicyTemplates;
  readonly lambdaFunctions: LambdaFunctions;

  constructor(scope: Construct, id: string, props: ArtifactStoreStackProps) {
    super(scope, id, props);

    const { appName, regionCode, stage, orgId, tenantId, projectId, isProd, wormEnabled, notificationEmail } =
      props;
    const qualifier = `${regionCode}-${stage}`;

    // WORM_Enabled (S3 Object Lock) requires production retention: an
    // Object-Lock bucket cannot be auto-emptied / DESTROYed on teardown, so it
    // must live on an isProd=true (RETAIN) stack. Reject the incoherent
    // combination at construction rather than synthesizing an archive
    // bucket whose auto-delete custom resource would fail against locked
    // objects. This invariant keeps the isProd teardown gate covering every
    // WORM-enabled deployment.
    if (wormEnabled && !isProd) {
      throw new Error(
        'WORM_Enabled=true requires isProd=true: S3 Object Lock is incompatible ' +
          'with the auto-empty/DESTROY teardown path used on non-production ' +
          'stacks. Set isProd=true for any WORM_Enabled deployment.',
      );
    }

    // Single composition point for every resource physical name: threaded to
    // every construct so the name STRUCTURE has exactly one definition.
    const naming = new Naming({ appName, qualifier });
    this.naming = naming;

    // Teardown-readiness guard: fail synth if any stateful
    // resource ever becomes deletable / auto-emptying while isProd=true.
    // Applied stack-wide so it also covers future stateful constructs.
    Aspects.of(this).add(new ProductionTeardownGuard(isProd));

    // KMS first — most downstream resources need it.
    this.encryption = new Encryption(this, 'Encryption', { naming, isProd });

    this.notifications = new Notifications(this, 'Notifications', {
      naming,
      notificationEmail,
    });

    this.repositories = new Repositories(this, 'Repositories', {
      naming,
      orgId,
      tenantId,
      projectId,
      isProd,
      kmsKey: this.encryption.serviceKey,
    });

    this.archive = new Archive(this, 'Archive', {
      naming,
      isProd,
      wormEnabled,
      kmsKey: this.encryption.serviceKey,
    });

    this.metadataTable = new MetadataTable(this, 'MetadataTable', {
      naming,
      isProd,
      kmsKey: this.encryption.serviceKey,
    });

    this.controlClaimsTable = new ControlClaimsTable(this, 'ControlClaimsTable', {
      naming,
      isProd,
      kmsKey: this.encryption.serviceKey,
    });

    // Control-plane Identity_Registry (opaque id → display name + tenant→org
    // parentage), seeded at deploy for the single org/tenant/project
    // (identity-registry [IDREG-001]). Encrypted with the service CMK and
    // IAM-gated like every other table; distinct from the rebuildable index.
    this.identityRegistry = new IdentityRegistry(this, 'IdentityRegistry', {
      naming,
      isProd,
      kmsKey: this.encryption.serviceKey,
      orgId,
      tenantId,
      projectId,
    });

    this.deadLetterQueues = new DeadLetterQueues(this, 'DeadLetterQueues', {
      naming,
      isProd,
      kmsKey: this.encryption.serviceKey,
    });

    this.logGroups = new LogGroups(this, 'LogGroups', { naming, isProd });

    this.eventBus = new EventBus(this, 'EventBus', {
      naming,
      alarmTopic: this.notifications.alarmTopic,
    });

    this.signing = new Signing(this, 'Signing', { naming, stage });

    this.iamPolicyTemplates = new IamPolicyTemplates(this, 'IamPolicyTemplates', {
      naming,
      orgId,
      tenantId,
      projectId,
      stage,
      archiveBucket: this.archive.bucket,
      metadataTable: this.metadataTable.table,
      kmsKey: this.encryption.serviceKey,
      reviewerSignoffProfileArn: this.signing.reviewerSignoffProfileArn,
    });

    // SSM Parameter Store — the canonical surface for cross-stack and
    // cross-system value sharing per the CDK conventions in AGENTS.md
    // (CFN Exports are prohibited because they create deletion ratchets).
    // The CfnOutputs below are display-only (no `exportName`) so they
    // remain queryable via `aws cloudformation describe-stacks` without
    // creating cross-stack import dependencies.
    const ecrRegistryHost = `${this.account}.dkr.ecr.${this.region}.amazonaws.com`;
    const profileVersionArn = this.signing.signingProfileVersionArn;
    const reviewerProfileVersionArn = this.signing.reviewerSignoffProfileVersionArn;

    // appName discovery for the CLI — published at a well-known path that does
    // NOT itself depend on the token (so the CLI can find it without already
    // knowing the appName). It carries the resolved appName as its value.
    // NOTE: two co-located deployments in the same account/region/stage would
    // CONTEND for this single path (last-writer-wins) — accepted, because
    // runtime isolation between co-located deployments is an explicit non-goal
    // (design.md Risks: "names deconflicted != pipelines isolated"; the runbook
    // warns operators). Token-deconfliction makes the resource NAMES distinct;
    // this discovery parameter is the one shared coordinate.
    new StringParameter(this, 'AppNameParam', {
      parameterName: `/cairnproofs/${qualifier}/app-name`,
      stringValue: appName,
      description:
        'Resolved appName for this deployment. The CLI reads this to derive the ' +
        'ECR push target namespace and other appName-prefixed resource names.',
    });

    new StringParameter(this, 'EcrRegistryHostParam', {
      parameterName: naming.ssmPath(stage, 'registry/ecr-host'),
      stringValue: ecrRegistryHost,
      description: 'ECR registry host for this deployment.',
    });

    // DynamoDB table-name discovery. The tables are auto-named (no explicit
    // `tableName`) so a future key-schema change replaces cleanly instead of
    // colliding on a fixed physical name; their resolved physical names are
    // published here at DETERMINISTIC parameter paths so out-of-stack consumers
    // (the `cairn` CLI and TUI) discover them without reconstructing the name.
    // The deterministic identifier is the SSM parameter NAME (which has no
    // replacement hazards — the value just re-points if the table is ever
    // replaced), not the table's physical name.
    new StringParameter(this, 'MetadataTableNameParam', {
      parameterName: naming.ssmPath(stage, 'tables/metadata'),
      stringValue: this.metadataTable.table.tableName,
      description: 'Physical name of the Metadata_Index DynamoDB table ([META-001]).',
    });
    new StringParameter(this, 'ControlClaimsTableNameParam', {
      parameterName: naming.ssmPath(stage, 'tables/control-claims'),
      stringValue: this.controlClaimsTable.table.tableName,
      description: 'Physical name of the ControlClaims DynamoDB table ([META-001]/[PCLAIM-001]).',
    });
    new StringParameter(this, 'IdentityRegistryTableNameParam', {
      parameterName: naming.ssmPath(stage, 'tables/identity-registry'),
      stringValue: this.identityRegistry.table.tableName,
      description: 'Physical name of the control-plane Identity_Registry DynamoDB table ([IDREG-001]).',
    });
    new StringParameter(this, 'SigningProfileVersionArnParam', {
      parameterName: naming.ssmPath(stage, 'signing/profile-version-arn'),
      stringValue: profileVersionArn,
      description:
        'Versioned ARN of the artifact-pipeline-signing AWS Signer profile. ' +
        'Notation trust-policy trustedIdentities compares against this exact value.',
    });
    new StringParameter(this, 'ReviewerSignoffProfileVersionArnParam', {
      parameterName: naming.ssmPath(stage, 'signing/reviewer-profile-version-arn'),
      stringValue: reviewerProfileVersionArn,
      description:
        'Versioned ARN of the artifact-reviewer-signoff AWS Signer profile. ' +
        'Notation trust-policy trustedIdentities pins this exact value as a ' +
        'distinct trusted identity (never a wildcard).',
    });

    // Pre-rendered Notation trust policy — single fetch + install on a
    // verifier (CLI, reviewer laptop): `aws ssm get-parameter --name
    // /{appName}/<stage>/notation/trust-policy --query Parameter.Value
    // --output text > ~/.config/notation/trustpolicy.json`. CDK token
    // substitution resolves profileVersionArn at deploy time.
    //
    // The registry scope is composed from the SAME naming.artifactNamespace as
    // the ECR repo path and the pipeline IAM ARN patterns — no
    // independent literal `artifact-${qualifier}` interpolation.
    const registryScopes = [`${ecrRegistryHost}/${naming.artifactNamespace}`];
    const renderedTrustPolicy = JSON.stringify(
      {
        version: '1.0',
        // A SINGLE strict trust policy for the artifact registry scope.
        // Notation requires every registry scope to be associated with exactly
        // ONE trust policy — a scope repeated across policies is a hard load
        // error ("registry scope ... is present in multiple trust policy
        // statements"). So the pipeline AND reviewer profiles (evidence-
        // verifiability, signing [SIGN-003]) are admitted as TWO pinned
        // trustedIdentities on ONE policy, never two policies that both claim
        // the artifact namespace. Both are exact profile version ARNs — never
        // a wildcard (`*`) and never a repo-only scope — so only signatures
        // from the two provisioned profiles are admitted (Threat #3: an
        // unadmitted/forged identity is rejected).
        trustPolicies: [
          {
            name: naming.nameFor('artifact'),
            registryScopes,
            signatureVerification: { level: 'strict' },
            trustStores: ['signingAuthority:aws-signer'],
            trustedIdentities: [profileVersionArn, reviewerProfileVersionArn],
          },
        ],
      },
      null,
      2,
    );
    new StringParameter(this, 'NotationTrustPolicyParam', {
      parameterName: naming.ssmPath(stage, 'notation/trust-policy'),
      stringValue: renderedTrustPolicy,
      description:
        'Rendered Notation trust policy (Notation v1.0 schema). Verifiers fetch and install at ~/.config/notation/trustpolicy.json.',
    });

    // Display-only outputs — visible via describe-stacks but NOT cross-stack
    // importable (no `exportName`). The SSM parameters above are the
    // load-bearing surface; outputs are for human inspection.
    new CfnOutput(this, 'AppName', {
      value: appName,
      description:
        'Resolved appName for this deployment (also at SSM /cairnproofs/' +
        qualifier +
        '/app-name).',
    });
    new CfnOutput(this, 'EcrRegistryHost', {
      value: ecrRegistryHost,
      description:
        'ECR registry host for this deployment (also at SSM ' +
        naming.ssmPath(stage, 'registry/ecr-host') +
        ').',
    });
    new CfnOutput(this, 'SigningProfileVersionArn', {
      value: profileVersionArn,
      description:
        'Versioned ARN of the artifact-pipeline-signing profile (also at SSM ' +
        naming.ssmPath(stage, 'signing/profile-version-arn') +
        ').',
    });
    new CfnOutput(this, 'ReviewerSignoffProfileVersionArn', {
      value: reviewerProfileVersionArn,
      description:
        'Versioned ARN of the artifact-reviewer-signoff profile (also at SSM ' +
        naming.ssmPath(stage, 'signing/reviewer-profile-version-arn') +
        ').',
    });
    new CfnOutput(this, 'NotationTrustPolicyParamName', {
      value: naming.ssmPath(stage, 'notation/trust-policy'),
      description:
        'SSM parameter holding the rendered Notation trust policy. Fetch with `aws ssm get-parameter` to install on a verifier.',
    });
    // AuditCoverageStatus — set by the pre-deploy CloudTrail preflight in
    // cdk/scripts/deploy.sh, read here via --context cloudTrailCheck=<value>.
    // Values:
    //   passed                    — preflight found an active trail with
    //                               management-event logging covering this
    //                               account; signing [SIGN-006] + access-control
    //                               [ACCESS-004] satisfied at deploy time.
    //   override-acknowledged     — preflight failed but the operator set
    //                               ALLOW_MISSING_CLOUDTRAIL=1 and
    //                               I_ACKNOWLEDGE_AUDIT_GAP=1; the deploy
    //                               proceeded with a documented audit gap.
    //                               This output exists so audit reviewers
    //                               can detect the override post-hoc via
    //                               `aws cloudformation describe-stacks`.
    //   unverified                — `cdk deploy` invoked directly without
    //                               going through deploy.sh, so the
    //                               preflight didn't run. Treat as a
    //                               compliance gap until re-deployed via
    //                               deploy.sh.
    const auditCoverageStatus =
      (this.node.tryGetContext('cloudTrailCheck') as string | undefined) ?? 'unverified';
    new CfnOutput(this, 'AuditCoverageStatus', {
      value: auditCoverageStatus,
      description:
        'CloudTrail preflight outcome from cdk/scripts/deploy.sh. Values: ' +
        'passed | override-acknowledged | unverified. ' +
        'override-acknowledged or unverified indicates a compliance gap against ' +
        'signing [SIGN-006] + access-control [ACCESS-004]; reconcile before production use.',
    });

    // Lambda constructs (merge of Section 4 infra + Section 5 handlers) —
    // wires Stream B handlers to Stream A infra via EventBridge targets.
    this.lambdaFunctions = new LambdaFunctions(this, 'LambdaFunctions', {
      naming,
      stage,
      regionCode,
      orgId,
      tenantId,
      projectId,
      wormEnabled,
      eventBus: this.eventBus,
      deadLetterQueues: this.deadLetterQueues,
      logGroups: this.logGroups,
      archiveBucket: this.archive.bucket,
      metadataTable: this.metadataTable.table,
      controlsTable: this.controlClaimsTable.table,
      serviceKmsKey: this.encryption.serviceKey,
      signingProfile: this.signing.signingProfile,
      timestampProviderParam: this.signing.timestampProviderParam,
    });

    // Alarms are constructed AFTER LambdaFunctions because the
    // `DeadLetterErrors` per-Lambda alarms reference each function's name as a
    // metric dimension. No upstream construct depends on Alarms.
    this.alarms = new Alarms(this, 'Alarms', {
      naming,
      alarmTopic: this.notifications.alarmTopic,
      deadLetterQueues: this.deadLetterQueues,
      pipelineLambdas: this.lambdaFunctions.all(),
      archiveFunction: this.lambdaFunctions.archiveToS3,
      // Production forces alarm actions on; a non-production deployment opts in
      // via alarmActionsEnabled. Effective posture: isProd || alarmActionsEnabled.
      actionsEnabled: props.isProd || (props.alarmActionsEnabled ?? false),
    });
  }
}
