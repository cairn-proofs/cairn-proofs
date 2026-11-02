import * as path from 'path';
import { Duration } from 'aws-cdk-lib';
import { IEventBus, Rule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';
import { ITable } from 'aws-cdk-lib/aws-dynamodb';
import { PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { IKey } from 'aws-cdk-lib/aws-kms';
import {
  Architecture,
  Code,
  Function as LambdaFn,
  LayerVersion,
  Runtime,
  Tracing,
} from 'aws-cdk-lib/aws-lambda';
import { ILogGroup } from 'aws-cdk-lib/aws-logs';
import { IBucket } from 'aws-cdk-lib/aws-s3';
import { ISigningProfile } from 'aws-cdk-lib/aws-signer';
import { IStringParameter } from 'aws-cdk-lib/aws-ssm';
import { IQueue } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { DeadLetterQueues } from './dead-letter-queues';
import { EventBus as CairnEventBus } from './event-bus';
import { LogGroups } from './log-groups';

const REPO_ROOT = path.resolve(__dirname, '../../..');
const LAMBDA_BUNDLES = path.join(REPO_ROOT, 'lambda-bundles');
const NOTATION_LAYER_ZIP = path.join(
  REPO_ROOT,
  'lambda-layers/notation/build/notation-layer.zip',
);

export interface LambdaFunctionsProps {
  readonly naming: Naming;
  readonly stage: string;
  readonly regionCode: string;
  readonly orgId: string;
  readonly tenantId: string;
  readonly projectId: string;
  readonly wormEnabled: boolean;
  readonly eventBus: CairnEventBus;
  readonly deadLetterQueues: DeadLetterQueues;
  readonly logGroups: LogGroups;
  readonly archiveBucket: IBucket;
  readonly metadataTable: ITable;
  readonly controlsTable: ITable;
  readonly serviceKmsKey: IKey;
  readonly signingProfile: ISigningProfile;
  readonly timestampProviderParam: IStringParameter;
  /**
   * SSM path of the stack-written retention-period parameter ([RETAIN-007]).
   * archive-to-s3 reads it at runtime by name, with no label or version
   * selector, and fails the archive closed when it is invalid.
   */
  readonly retentionParamName: string;
}

/**
 * DynamoDB item actions the pipeline may use. None of them deletes: no
 * pipeline role holds `dynamodb:DeleteItem` or `dynamodb:BatchWriteItem`
 * (`BatchWriteItem` cannot be limited to puts), so only the pruner removes an
 * index row (access-control [ACCESS-007.02]).
 */
const DDB_READ_WRITE_ACTIONS = [
  'dynamodb:GetItem',
  'dynamodb:PutItem',
  'dynamodb:UpdateItem',
  'dynamodb:Query',
  'dynamodb:DescribeTable',
];
const DDB_WRITE_ACTIONS = ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DescribeTable'];

/**
 * The four pipeline Lambdas — wires Stream B handler code (lambda-bundles/*)
 * to Stream A infrastructure. Each function:
 *
 * - Python 3.12 / arm64 / 1024 MB / timeout per stage profile.
 * - Code from `lambda-bundles/<name>/` (built by `cdk/scripts/build-lambdas.sh`).
 * - Pre-created log group from the LogGroups construct (consistent retention).
 * - Pre-created SQS DLQ from the DeadLetterQueues construct, attached as
 *   `deadLetterQueue` for Lambda async-invoke failures AND as the
 *   EventBridge target DLQ for delivery failures.
 * - Env vars matching the handler-side os.environ.get() surface.
 * - Least-privilege role provisioned by helper grants (KMS, S3, DDB, etc.).
 *
 * sign-bundle additionally:
 * - Mounts the Notation Lambda layer (lambda-layers/notation/build/notation-layer.zip).
 * - Sets XDG_CONFIG_HOME=/opt so notation discovers the plugin (per
 *   runbooks/notation-layer.md).
 *
 * EventBridge target wiring is the second half of this constructor —
 * once each function exists, the matching rule from event-bus.ts gets
 * `addTarget(new LambdaFunction(fn, { deadLetterQueue }))`.
 */
export class LambdaFunctions extends Construct {
  readonly validateBundle: LambdaFn;
  readonly signBundle: LambdaFn;
  readonly archiveToS3: LambdaFn;
  readonly updateMetadata: LambdaFn;
  readonly mintTag: LambdaFn;
  readonly notationLayer: LayerVersion;

  constructor(scope: Construct, id: string, props: LambdaFunctionsProps) {
    super(scope, id);

    const {
      naming,
      stage,
      regionCode,
      wormEnabled,
      eventBus,
      deadLetterQueues,
      logGroups,
      archiveBucket,
      metadataTable,
      controlsTable,
      serviceKmsKey,
      signingProfile,
      timestampProviderParam,
      retentionParamName,
    } = props;

    // ── Shared scoped resource patterns ──────────────────────────────────
    // ECR repo ARN pattern for project-scoped Lambda permissions (scoped
    // instead of an overly broad resources:['*']).
    const stackAccount = props.eventBus.bus.stack.account;
    const stackRegion = props.eventBus.bus.stack.region;
    const stackPartition = props.eventBus.bus.stack.partition;
    // ECR ARN patterns derive from the SAME naming.artifactNamespace as the repo
    // path and the Notation trust scope. IAM stays repo-path-scoped —
    // `repository/{appName}-artifact-{qualifier}/...` — NEVER widened to
    // `repository/*` or a bare `{appName}` prefix.
    const artifactNamespace = naming.artifactNamespace;
    const anyArtifactRepoArnPattern = `arn:aws:ecr:${stackRegion}:${stackAccount}:repository/${artifactNamespace}/*`;
    // Project-scoped single artifact repo ARN, addressed through the uniform
    // three-segment logical tenant hierarchy {org-id}/{tenant-id}/{project-id}
    // The mint role's tag-write
    // (ecr:PutImage) is scoped to exactly this repo, not the qualifier-wide `/*`
    // pattern the pipeline read roles use.
    const projectArtifactRepoArn = `arn:aws:ecr:${stackRegion}:${stackAccount}:repository/${artifactNamespace}/${props.orgId}/${props.tenantId}/${props.projectId}`;

    // KMS condition: only allow Lambda role calls to the service CMK when
    // the call is mediated by an AWS service we use.
    // Lambda Console / direct kms:Decrypt outside these contexts is denied.
    const kmsViaServiceCondition = {
      StringEquals: {
        'kms:ViaService': [
          `s3.${stackRegion}.amazonaws.com`,
          `dynamodb.${stackRegion}.amazonaws.com`,
          `ecr.${stackRegion}.amazonaws.com`,
          `sqs.${stackRegion}.amazonaws.com`,
        ],
      },
    };
    const addScopedKmsDecrypt = (fn: LambdaFn) =>
      fn.addToRolePolicy(
        new PolicyStatement({
          actions: ['kms:Decrypt', 'kms:DescribeKey'],
          resources: [serviceKmsKey.keyArn],
          conditions: kmsViaServiceCondition,
        }),
      );
    const addScopedKmsEncryptDecrypt = (fn: LambdaFn) =>
      fn.addToRolePolicy(
        new PolicyStatement({
          actions: [
            'kms:Decrypt',
            'kms:Encrypt',
            'kms:ReEncrypt*',
            'kms:GenerateDataKey*',
            'kms:DescribeKey',
          ],
          resources: [serviceKmsKey.keyArn],
          conditions: kmsViaServiceCondition,
        }),
      );
    // Narrow `kms:GenerateDataKey` grant scoped to SQS only — for Lambda
    // roles that need to write to a KMS-encrypted DLQ but must NOT receive
    // a broader encrypt surface across S3/DDB/ECR. The CDK
    // `queue.grantSendMessages(role)` auto-grant from the `deadLetterQueue:`
    // prop does NOT materialize a KMS leg, so async-invoke failure delivery
    // to the DLQ silently drops on every retry (visible only as
    // `AWS/Lambda::DeadLetterErrors`, NOT in the Lambda's own error metric).
    // This stays scoped to a single action +
    // sqs.<region>.amazonaws.com — do not widen to the encrypt/reencrypt
    // surface that `addScopedKmsEncryptDecrypt` provides.
    const addScopedKmsGenerateDataKeyForSqs = (fn: LambdaFn) =>
      fn.addToRolePolicy(
        new PolicyStatement({
          actions: ['kms:GenerateDataKey'],
          resources: [serviceKmsKey.keyArn],
          conditions: {
            StringEquals: {
              'kms:ViaService': `sqs.${stackRegion}.amazonaws.com`,
            },
          },
        }),
      );

    // Explicit, delete-free DynamoDB grant on one table (base table + any GSI).
    const grantTableItems = (fn: LambdaFn, table: ITable, actions: string[]) =>
      fn.addToRolePolicy(
        new PolicyStatement({
          actions,
          resources: [table.tableArn, `${table.tableArn}/index/*`],
        }),
      );

    // ── Notation layer (Section 3 build output) ───────────────────────────
    this.notationLayer = new LayerVersion(this, 'NotationLayer', {
      layerVersionName: naming.nameFor('notation'),
      code: Code.fromAsset(NOTATION_LAYER_ZIP),
      compatibleArchitectures: [Architecture.ARM_64],
      compatibleRuntimes: [Runtime.PYTHON_3_12],
      description:
        'Notation CLI v1.3.2 + AWS Signer plugin v1.0.2292 (arm64). XDG_CONFIG_HOME=/opt',
    });

    // ── ADOT (AWS Distro for OpenTelemetry) COLLECTOR-ONLY arm64 layer ────
    // This is the AWS-managed ADOT *collector-only* layer: a Go binary that
    // runs the in-process OTLP→awsxray collector. It ships NO Python SDK and
    // NO auto-instrument wrapper — the application installs its own
    // TracerProvider in the observability bootstrap. Spans are produced by the
    // manual instrumentation and exported to X-Ray by the collector this layer
    // runs (config at /var/task/collector.yaml, see
    // OPENTELEMETRY_COLLECTOR_CONFIG_URI below).
    //
    // PINNED VERSION: aws-otel-collector-arm64-ver-0-117-0:1 (publisher account
    // 901920570463 — the public ADOT layer account in the commercial `aws`
    // partition, identical across commercial regions). Pinned (not "latest")
    // consistent with how the Notation layer is pinned, so a layer republish
    // can't silently change collector behavior. When bumping this pin, also
    // record the new version in runbooks/deploy-alpha.md.
    // The ARN partition is templated from the stack (`stackPartition`) so the
    // region segment stays partition-correct; NOTE the publisher ACCOUNT differs
    // in GovCloud/China, so a multi-partition deployment must also map the
    // account per partition (a Service_Hosted/multi-region follow-up).
    const ADOT_COLLECTOR_ARM64_LAYER_VERSION =
      'aws-otel-collector-arm64-ver-0-117-0:1';
    const adotLayer = LayerVersion.fromLayerVersionArn(
      this,
      'AdotLayer',
      `arn:${stackPartition}:lambda:${stackRegion}:901920570463:layer:${ADOT_COLLECTOR_ARM64_LAYER_VERSION}`,
    );

    const baseEnv = {
      STAGE: stage,
      REGION_CODE: regionCode,
      CAIRN_STAGE: stage,
      CAIRN_REGION_CODE: regionCode,
      EVENT_BUS_NAME: eventBus.bus.eventBusName,
      DEPLOYMENT_MODE: 'Customer_Hosted',
      // ── OTEL (collector-only layer + manual instrumentation) ────────────
      // NO AWS_LAMBDA_EXEC_WRAPPER: there is no auto-instrument wrapper. The
      // application installs its own TracerProvider in the observability
      // bootstrap; the ADOT collector-only
      // layer (above) runs the in-process collector that reads collector.yaml
      // and exports OTLP spans → awsxray. The vars below configure that
      // manual-instrumentation + collector topology.
      //
      // W3C propagation across pipeline-stage boundaries (EventBridge events
      // carry traceparent/baggage headers). `xray` is included so our
      // instrumentation also honors/emits the Lambda-injected X-Amzn-Trace-Id
      // header — keeping the span's trace ID equal to the X-Ray-service trace
      // ID, so a log line's `traceId` correlates to the X-Ray console trace
      // (not just span export).
      OTEL_PROPAGATORS: 'tracecontext,baggage,xray',
      OTEL_SERVICE_NAME: naming.nameFor('service'),
      // The collector config bundled into each Lambda asset (build-lambdas.sh
      // copies cdk/collector.yaml → /var/task/collector.yaml); the
      // collector-only layer reads it to stand up the OTLP→awsxray pipeline.
      OPENTELEMETRY_COLLECTOR_CONFIG_URI: '/var/task/collector.yaml',
    };

    // ── Metric emission (EMF — applies to all 4 Lambda roles below) ─────
    // Per openspec/specs/observability/spec.md "Metric Emission via EMF"
    // Requirement, NO `cloudwatch:PutMetricData` grant is added to any of
    // the pipeline Lambda execution roles. Each handler emits CloudWatch
    // metrics via EMF — `aws_lambda_powertools.metrics.Metrics` formats
    // an EMF-shaped JSON line, prints it to stdout, and CloudWatch Logs
    // auto-parses it server-side into a metric under the `Artifact/
    // {regionCode}-{stage}` namespace. The Logs path uses only the
    // AWS-managed basic Lambda execution role's `logs:PutLogEvents`
    // permission, which is granted by default. Adding PutMetricData here
    // would be unnecessary and would expand each role's blast radius
    // without operational benefit.

    // ── validate-bundle ───────────────────────────────────────────────────
    this.validateBundle = new LambdaFn(this, 'ValidateBundle', {
      functionName: naming.nameFor('validate-bundle'),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: 'cairn_lambda_validate_bundle.handler.lambda_handler',
      code: Code.fromAsset(path.join(LAMBDA_BUNDLES, 'validate_bundle')),
      memorySize: 1024,
      timeout: Duration.minutes(5),
      logGroup: logGroups.validateBundle,
      deadLetterQueueEnabled: true,
      deadLetterQueue: deadLetterQueues.validateBundle,
      tracing: Tracing.ACTIVE,
      layers: [adotLayer],
      environment: {
        ...baseEnv,
        ARCHIVE_BUCKET: archiveBucket.bucketName,
        METADATA_TABLE_NAME: metadataTable.tableName,
        VALIDATE_DLQ_URL: deadLetterQueues.validateBundle.queueUrl,
      },
    });
    eventBus.bus.grantPutEventsTo(this.validateBundle);
    // validate stamps the durable validateStatus/artifactKind/name marker the
    // mint stage reads (and reads it back for its own re-tag short-circuit).
    // Without this grant + env var, the write silently no-ops and nothing is
    // ever tagged.
    grantTableItems(this.validateBundle, metadataTable, DDB_READ_WRITE_ACTIONS);
    // ECR auth token must be * (AWS docs); other ECR reads scoped to the
    // qualifier's artifact-* repos.
    this.validateBundle.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    this.validateBundle.addToRolePolicy(
      new PolicyStatement({
        // ECR reads scoped to the qualifier-wide `artifact-*/*` namespace.
        // Two read paths use this grant:
        //  - reading the triggering manifest's body/layers/config
        //    (BatchGetImage / GetDownloadUrlForLayer), and
        //  - the subject-scope (anti-hijack) residence check:
        //    the Producer_Attestation_Referrer's resolved `subject` content manifest is
        //    read via BatchGetImage (+ DescribeImages) to confirm it resides in
        //    the SAME `{account-id}`/`{project-id}` repo path as the referrer.
        //    The subject may live in a DIFFERENT repo than the triggering one
        //    (that is precisely the hijack the check rejects), so the read grant
        //    MUST span the whole `artifact-*` namespace — the qualifier-wide
        //    `/*` pattern below already does. No new action is needed; the new
        //    check rides the existing BatchGetImage/DescribeImages coverage.
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
          'ecr:DescribeImages',
          'ecr:DescribeRepositories',
        ],
        resources: [anyArtifactRepoArnPattern],
      }),
    );
    addScopedKmsDecrypt(this.validateBundle);
    // The marker write encrypts under the table's CMK — DynamoDB-mediated only.
    this.validateBundle.addToRolePolicy(
      new PolicyStatement({
        actions: ['kms:Encrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*'],
        resources: [serviceKmsKey.keyArn],
        conditions: {
          StringEquals: { 'kms:ViaService': `dynamodb.${stackRegion}.amazonaws.com` },
        },
      }),
    );
    // DLQ-write path: validate-bundle uses the narrow `kms:GenerateDataKey`
    // helper above so async-invoke failures land on the SQS DLQ. The other
    // three Lambdas already have `kms:GenerateDataKey*` via
    // `addScopedKmsEncryptDecrypt` and don't need this second statement.
    addScopedKmsGenerateDataKeyForSqs(this.validateBundle);
    // Explicit `sqs:SendMessage` grant (scoped to the validate DLQ ARN) for the
    // IN-BAND rejection send: permanent validation failures write the rejection
    // record to the DLQ at classification time ([PIPELINE-006]), not via Lambda's
    // async-invoke dead-letter destination. The `deadLetterQueue:` prop above
    // already auto-grants the same action, so this is idempotent — its purpose
    // is to DECLARE the in-band path's dependency so a future change to the
    // async failure-routing (e.g. switching to an `onFailure` destination)
    // cannot silently strip the authorization the in-band send now relies on.
    deadLetterQueues.validateBundle.grantSendMessages(this.validateBundle);

    // ── sign-bundle ───────────────────────────────────────────────────────
    this.signBundle = new LambdaFn(this, 'SignBundle', {
      functionName: naming.nameFor('sign-bundle'),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: 'cairn_lambda_sign_bundle.handler.lambda_handler',
      code: Code.fromAsset(path.join(LAMBDA_BUNDLES, 'sign_bundle')),
      memorySize: 1024,
      timeout: Duration.minutes(3),
      logGroup: logGroups.signBundle,
      deadLetterQueueEnabled: true,
      deadLetterQueue: deadLetterQueues.signBundle,
      tracing: Tracing.ACTIVE,
      layers: [this.notationLayer, adotLayer],
      environment: {
        ...baseEnv,
        XDG_CONFIG_HOME: '/opt',
        // Notation reads credentials via `$HOME/.docker/config.json`. Lambda
        // doesn't set HOME by default; without it the CLI errors out before
        // initialization with `failed to get user home directory: $HOME is
        // not defined`. /tmp is Lambda's only writable path.
        HOME: '/tmp',
        SIGNER_PROFILE_ARN: signingProfile.signingProfileArn,
        METADATA_TABLE_NAME: metadataTable.tableName,
        // Full SSM parameter name (the CDK owns the /{appName}/{stage}/... scheme
        // and the matching grantRead below) — the Lambda reads this rather than
        // reconstructing the path, so code can't drift from the deployed name.
        TIMESTAMP_PROVIDER_PARAM: timestampProviderParam.parameterName,
      },
    });
    eventBus.bus.grantPutEventsTo(this.signBundle);
    this.signBundle.addToRolePolicy(
      new PolicyStatement({
        actions: [
          'signer:SignPayload',
          'signer:GetSigningProfile',
          // GetRevocationStatus per AWS Signer Notation plugin (security H-3).
          'signer:GetRevocationStatus',
        ],
        resources: [signingProfile.signingProfileArn],
      }),
    );
    this.signBundle.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    this.signBundle.addToRolePolicy(
      new PolicyStatement({
        // ECR reads — wide-scoped across all evidence repos so the Lambda
        // can call ListImageReferrers (IAM action: ecr:BatchGetImage)
        // against any project's manifest for the idempotency check.
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
          'ecr:DescribeImages',
        ],
        resources: [anyArtifactRepoArnPattern],
      }),
    );
    this.signBundle.addToRolePolicy(
      new PolicyStatement({
        // ECR writes (signature referrer attach). Scoped to all evidence
        // repos under this qualifier rather than a single projectId because
        // a stack instance may service multiple projects (the
        // integration-test harness mints per-test projectIds). Per-project IAM
        // tightening is handled by per-project stack deployments rather than
        // within-policy scoping.
        //
        // Two independent signatures (signing [SIGN-001]): the sign stage
        // attaches TWO Notation signature referrers — one whose `subject` is
        // the content manifest digest and a SECOND whose `subject` is the
        // Producer_Attestation_Referrer's digest. Both are ordinary referrer
        // `PutImage` (+ layer-upload) writes into the same `artifact-*`
        // namespace, so the existing grant covers both signatures.
        actions: [
          'ecr:InitiateLayerUpload',
          'ecr:UploadLayerPart',
          'ecr:CompleteLayerUpload',
          'ecr:PutImage',
        ],
        resources: [anyArtifactRepoArnPattern],
      }),
    );
    timestampProviderParam.grantRead(this.signBundle);
    grantTableItems(this.signBundle, metadataTable, DDB_WRITE_ACTIONS);
    addScopedKmsEncryptDecrypt(this.signBundle);

    // ── archive-to-s3 ─────────────────────────────────────────────────────
    this.archiveToS3 = new LambdaFn(this, 'ArchiveToS3', {
      functionName: naming.nameFor('archive-to-s3'),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: 'cairn_lambda_archive_to_s3.handler.lambda_handler',
      code: Code.fromAsset(path.join(LAMBDA_BUNDLES, 'archive_to_s3')),
      memorySize: 2048, // larger memory for blob streaming throughput
      timeout: Duration.minutes(10),
      logGroup: logGroups.archiveToS3,
      deadLetterQueueEnabled: true,
      deadLetterQueue: deadLetterQueues.archiveToS3,
      tracing: Tracing.ACTIVE,
      layers: [adotLayer],
      environment: {
        ...baseEnv,
        ARCHIVE_BUCKET: archiveBucket.bucketName,
        WORM_ENABLED: wormEnabled ? 'true' : 'false',
        RETENTION_PERIOD_PARAM: retentionParamName,
      },
    });
    eventBus.bus.grantPutEventsTo(this.archiveToS3);
    // Explicit object writes only: PutObject plus the per-object Object Lock
    // retention header. Never `grantPut`, which would add
    // s3:PutObjectLegalHold ([ACCESS-009] — no pipeline role sets or clears a
    // legal hold).
    this.archiveToS3.addToRolePolicy(
      new PolicyStatement({
        actions: ['s3:PutObject', 's3:PutObjectRetention'],
        resources: [archiveBucket.arnForObjects('*')],
      }),
    );
    // The retention period, read by exact name ([RETAIN-007]).
    this.archiveToS3.addToRolePolicy(
      new PolicyStatement({
        actions: ['ssm:GetParameter'],
        resources: [
          `arn:${stackPartition}:ssm:${stackRegion}:${stackAccount}:parameter${retentionParamName}`,
        ],
      }),
    );
    archiveBucket.grantRead(this.archiveToS3);
    this.archiveToS3.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    this.archiveToS3.addToRolePolicy(
      new PolicyStatement({
        // Read all evidence repos; write (referrer attach) scoped to the
        // project's repos (security H-1). ListImageReferrers itself maps
        // to the ecr:BatchGetImage action — no separate IAM action exists.
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:BatchGetImage',
          'ecr:GetDownloadUrlForLayer',
          'ecr:DescribeImages',
        ],
        resources: [anyArtifactRepoArnPattern],
      }),
    );
    this.archiveToS3.addToRolePolicy(
      new PolicyStatement({
        // Receipt-referrer writes scoped wide; see sign-bundle comment
        // above for rationale.
        actions: [
          'ecr:InitiateLayerUpload',
          'ecr:UploadLayerPart',
          'ecr:CompleteLayerUpload',
          'ecr:PutImage',
        ],
        resources: [anyArtifactRepoArnPattern],
      }),
    );
    addScopedKmsEncryptDecrypt(this.archiveToS3);

    // ── update-metadata ───────────────────────────────────────────────────
    this.updateMetadata = new LambdaFn(this, 'UpdateMetadata', {
      functionName: naming.nameFor('update-metadata'),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: 'cairn_lambda_update_metadata.handler.lambda_handler',
      code: Code.fromAsset(path.join(LAMBDA_BUNDLES, 'update_metadata')),
      memorySize: 512,
      timeout: Duration.minutes(2),
      logGroup: logGroups.updateMetadata,
      deadLetterQueueEnabled: true,
      deadLetterQueue: deadLetterQueues.updateMetadata,
      tracing: Tracing.ACTIVE,
      layers: [adotLayer],
      environment: {
        ...baseEnv,
        METADATA_TABLE_NAME: metadataTable.tableName,
        CONTROLS_TABLE_NAME: controlsTable.tableName,
      },
    });
    grantTableItems(this.updateMetadata, metadataTable, DDB_READ_WRITE_ACTIONS);
    grantTableItems(this.updateMetadata, controlsTable, DDB_READ_WRITE_ACTIONS);
    addScopedKmsEncryptDecrypt(this.updateMetadata);

    // ── mint-tag (pipeline TAIL — post-archive canonical tag minting) ─────
    // Triggered by `artifact.archived`. Reads the durable validate record
    // (artifact-kind + name) from the Metadata_Index table, then composes the
    // canonical ECR tag with the Manifest_Digest prefix and applies it
    // (PutImage-with-tag) on the
    // already-archived digest. The mint role is the SINGLE principal authorized
    // to write a canonical tag (ecr:PutImage scoped to the one project evidence
    // repo).
    this.mintTag = new LambdaFn(this, 'MintTag', {
      functionName: naming.nameFor('mint-tag'),
      runtime: Runtime.PYTHON_3_12,
      architecture: Architecture.ARM_64,
      handler: 'cairn_lambda_mint_tag.handler.lambda_handler',
      code: Code.fromAsset(path.join(LAMBDA_BUNDLES, 'mint_tag')),
      memorySize: 512,
      timeout: Duration.minutes(2),
      logGroup: logGroups.mintTag,
      deadLetterQueueEnabled: true,
      deadLetterQueue: deadLetterQueues.mintTag,
      tracing: Tracing.ACTIVE,
      layers: [adotLayer],
      environment: {
        ...baseEnv,
        METADATA_TABLE_NAME: metadataTable.tableName,
      },
    });
    eventBus.bus.grantPutEventsTo(this.mintTag);
    this.mintTag.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    // Read the existing manifest (idempotency check + PutImage body) — scoped
    // to the project's single evidence repo.
    this.mintTag.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:BatchGetImage', 'ecr:DescribeImages'],
        resources: [projectArtifactRepoArn],
      }),
    );
    // Tag-write (ecr:PutImage). The mint role is the ONLY principal that may
    // apply a canonical tag, scoped to exactly the one project evidence repo
    // ARN. The producer (push) and auditor (read) roles
    // deliberately carry NO ecr:PutImage.
    this.mintTag.addToRolePolicy(
      new PolicyStatement({
        actions: ['ecr:PutImage'],
        resources: [projectArtifactRepoArn],
      }),
    );
    // Read the durable validate record (artifact-kind + name) from the
    // Metadata_Index table — GetItem only (mint is a reader; no writes).
    this.mintTag.addToRolePolicy(
      new PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [metadataTable.tableArn],
      }),
    );
    addScopedKmsEncryptDecrypt(this.mintTag);

    // ── EventBridge target wiring ─────────────────────────────────────────
    const targetWithDlq = (fn: LambdaFn, dlq: IQueue) =>
      new LambdaFunction(fn, { deadLetterQueue: dlq });

    eventBus.ecrPushRule.addTarget(
      targetWithDlq(this.validateBundle, deadLetterQueues.validateBundle),
    );
    eventBus.artifactValidatedRule.addTarget(
      targetWithDlq(this.signBundle, deadLetterQueues.signBundle),
    );
    eventBus.artifactSignedRule.addTarget(
      targetWithDlq(this.archiveToS3, deadLetterQueues.archiveToS3),
    );
    eventBus.artifactLifecycleRule.addTarget(
      targetWithDlq(this.updateMetadata, deadLetterQueues.updateMetadata),
    );
    // Pipeline tail: artifact.archived → mint-tag (canonical tag minting).
    eventBus.artifactArchivedRule.addTarget(
      targetWithDlq(this.mintTag, deadLetterQueues.mintTag),
    );
  }

  /** All five pipeline Lambdas in declaration order. */
  all(): LambdaFn[] {
    return [
      this.validateBundle,
      this.signBundle,
      this.archiveToS3,
      this.updateMetadata,
      this.mintTag,
    ];
  }
}

// Construct-mode CDK forbids top-level `import` of IAM (cyclic). Re-export
// for callers that want to add custom policies to specific Lambdas later.
export { PolicyStatement } from 'aws-cdk-lib/aws-iam';
