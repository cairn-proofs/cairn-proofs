import { EventBus as EbEventBus, EventField, Rule, RuleTargetInput } from 'aws-cdk-lib/aws-events';
import { SnsTopic } from 'aws-cdk-lib/aws-events-targets';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { Naming } from '../naming';

export interface EventBusProps {
  readonly naming: Naming;
  readonly alarmTopic: ITopic;
  /** Physical name of the S3 archive bucket ([RETAIN-013] item 3). */
  readonly archiveBucketName: string;
  /** SSM path of the retention-period parameter ([RETAIN-013] item 4). */
  readonly retentionParamName: string;
  /** SSM path of the unarchived-manifest-window parameter ([RETAIN-013] item 4). */
  readonly windowParamName: string;
  /** Physical name of the archive-to-s3 Lambda ([RETAIN-013] item 4). */
  readonly archiveFunctionName: string;
}

/**
 * The SNS message every [RETAIN-013] rule publishes. It names the caller
 * principal and the request parameters, which carry the affected resource
 * (repository and image ids, bucket, parameter, function, or trail).
 */
export const RETENTION_ALARM_MESSAGE = RuleTargetInput.fromText(
  `[RETAIN-013] ${EventField.fromPath('$.detail.eventName')} by ` +
    `${EventField.fromPath('$.detail.userIdentity.arn')} in account ` +
    `${EventField.fromPath('$.account')} (${EventField.fromPath('$.region')}); ` +
    `request: ${EventField.fromPath('$.detail.requestParameters')}; ` +
    `errorCode: ${EventField.fromPath('$.detail.errorCode')}`,
);

/**
 * Custom EventBridge bus + routing rules for the validate → sign → archive →
 * metadata pipeline (`processing-pipeline` [PIPELINE-002], [PIPELINE-003], [PIPELINE-004]).
 *
 * Rules are declared here without Lambda targets — the merge-pass Lambda
 * constructs call `addTarget(new LambdaFunction(fn))` on each rule. The
 * SNS-alarm rules for permanent-failure event types ARE wired here because
 * the SNS topic is already available.
 *
 * Event pattern source ("dev.cairnproofs.artifact"): all events emitted by
 * the four Lambdas carry `source: "dev.cairnproofs.artifact"` and a
 * `detailType` matching the event name (e.g., `artifact.validated`). The
 * ECR-PUSH source path uses AWS's native event source (`aws.ecr`).
 */
export class EventBus extends Construct {
  readonly bus: EbEventBus;

  // Ingress: ECR push triggers validate-bundle.
  readonly ecrPushRule: Rule;

  // Pipeline progression rules — Lambda targets attached in merge pass.
  readonly artifactValidatedRule: Rule;
  readonly artifactSignedRule: Rule;
  readonly artifactArchivedRule: Rule;
  // update-metadata subscribes to ALL artifact.* events ([PIPELINE-004]); a single
  // rule with a glob-like detailType pattern keeps the target set tidy.
  readonly artifactLifecycleRule: Rule;

  // Permanent-failure routing → SNS alarm.
  readonly validationFailedRule: Rule;
  readonly signingFailedRule: Rule;
  readonly archiveFailedRule: Rule;

  // [RETAIN-013] deletion and retention-configuration alarms (CloudTrail on
  // the default bus → SNS). The Operator may not change them ([ACCESS-009]).
  readonly retentionAlarmRules: Rule[];

  constructor(scope: Construct, id: string, props: EventBusProps) {
    super(scope, id);

    const {
      naming,
      alarmTopic,
      archiveBucketName,
      retentionParamName,
      windowParamName,
      archiveFunctionName,
    } = props;
    const retentionSettingParams = [retentionParamName, windowParamName];

    // Bus name is `{appName}-artifact-{qualifier}`.
    this.bus = new EbEventBus(this, 'Bus', {
      eventBusName: naming.nameFor('artifact'),
    });

    // ECR PutImage via CloudTrail — note we subscribe to the CloudTrail
    // delivery, NOT the ECR-native "ECR Image Action" event. CloudTrail's
    // payload carries `userIdentity` (spec [PIPELINE-010] — authenticatedPushPrincipal)
    // and the full request/response shape the validate Lambda parses; the
    // ECR-native event omits these fields. These events arrive on the DEFAULT
    // bus, declared by NOT setting `eventBus` on the rule constructor; targets
    // are added at merge time.
    //
    // Three strictly-upstream filters
    // pre-drop PutImage events that the in-handler classifier would discard
    // anyway, saving a validate-bundle invocation each. Every filter keys on a
    // field that is PRESENT on a genuine, successful, authenticated bundle push,
    // so none can silently drop a real bundle:
    //   (a) `errorCode: [{ exists: false }]` — admit SUCCESSFUL pushes only. A
    //       failed PutImage carries an `errorCode` (and no responseElements /
    //       digest), so an `exists:false` match drops only the failures.
    //   (b) `requestParameters.repositoryName: [{ prefix: '{appName}-artifact-<qual>/' }]`
    //       — admit pushes into THIS deployment's artifact repositories only.
    //       `repositoryName` is present on every successful authenticated
    //       PutImage; a `prefix` (begins-with, NOT exact) match keeps deeper
    //       paths like `{appName}-artifact-cmh-alpha/proj/scan-reports` in scope.
    //   (c) `userIdentity.arn: [{ 'anything-but': { wildcard: [...] } }]` —
    //       EXCLUDE the pipeline's own internal pushes (signature/receipt/tag
    //       referrers written by sign-bundle, archive-to-s3, mint-tag) so the
    //       rule does not re-trigger validate on the pipeline's own writes.
    //       We match `userIdentity.arn` (ALWAYS present on every principal —
    //       IAMUser AND AssumedRole) and NOT `sessionContext.sessionIssuer`
    //       (ABSENT on an IAMUser producer like `alice`, where an `anything-but`
    //       on a missing field would no-match → DROP the producer's push). The
    //       wildcards anchor on the role-SESSION segment (`*/<fn-name>`), which
    //       equals the Lambda FUNCTION NAME and carries the full untruncated
    //       `${qualifier}` — NOT the CDK-default execution-role-name segment
    //       (random-suffixed AND truncates `cmh-alpha`→`cmh-al`, which would
    //       clash with a sibling deployment). The function names are templated
    //       from `${qualifier}` below so the rule and the deployed function
    //       names cannot drift.
    //
    // The in-handler classifier (classify_pipeline_internal) remains the
    // AUTHORITATIVE and sole SEMANTIC filter; this rule pattern is a
    // strictly-upstream fail-safe cost optimization, never a correctness gate.
    //
    // NOTE: we deliberately do NOT narrow on
    // `requestParameters.imageManifestMediaType` to pre-drop OCI image indexes.
    // EventBridge `anything-but` requires the field to be PRESENT, but
    // `imageManifestMediaType` is an OPTIONAL `PutImage` request parameter
    // (ECR infers it from the manifest body when the client omits it — see the
    // `.get(...)` fallback in archive_to_s3/ecr_client.py). A present-and-not-
    // equal pattern would silently drop a genuine bundle push that omitted the
    // field, with no validation, no failure event, and no DLQ entry — which is
    // exactly why the three filters above each key on an always-present field
    // instead.
    this.ecrPushRule = new Rule(this, 'EcrPushRule', {
      ruleName: naming.nameFor('ecr-push'),
      description: 'ECR PutImage (CloudTrail) → validate-bundle',
      eventPattern: {
        source: ['aws.ecr'],
        detailType: ['AWS API Call via CloudTrail'],
        detail: {
          eventSource: ['ecr.amazonaws.com'],
          eventName: ['PutImage'],
          // (a) successful pushes only — a failed PutImage carries an errorCode.
          errorCode: [{ exists: false }],
          // (b) only this deployment's artifact repositories (begins-with). The
          // prefix derives from the SAME naming.artifactNamespace as the repo
          // path / IAM ARNs / trust scope.
          requestParameters: {
            repositoryName: [{ prefix: `${naming.artifactNamespace}/` }],
          },
          // (c) exclude the pipeline's own internal referrer/tag pushes, keyed
          // on the always-present userIdentity.arn role-session (= function
          // name) segment. validate-bundle and update-metadata are NOT in
          // this list — they must never be excluded.
          userIdentity: {
            arn: [
              {
                'anything-but': {
                  wildcard: [
                    `*/${naming.nameFor('sign-bundle')}`,
                    `*/${naming.nameFor('archive-to-s3')}`,
                    `*/${naming.nameFor('mint-tag')}`,
                  ],
                },
              },
            ],
          },
        },
      },
    });

    const cairnSource = ['dev.cairnproofs.artifact'];

    this.artifactValidatedRule = new Rule(this, 'ArtifactValidatedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-validated'),
      description: 'artifact.validated → sign-bundle',
      eventPattern: { source: cairnSource, detailType: ['artifact.validated'] },
    });

    this.artifactSignedRule = new Rule(this, 'ArtifactSignedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-signed'),
      description: 'artifact.signed → archive-to-s3',
      eventPattern: { source: cairnSource, detailType: ['artifact.signed'] },
    });

    this.artifactArchivedRule = new Rule(this, 'ArtifactArchivedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-archived'),
      description: 'artifact.archived → mint-tag (pipeline tail: canonical tag minting)',
      eventPattern: { source: cairnSource, detailType: ['artifact.archived'] },
    });

    this.artifactLifecycleRule = new Rule(this, 'ArtifactLifecycleRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-lifecycle'),
      description: 'all artifact.* events → update-metadata (fan-in)',
      eventPattern: {
        source: cairnSource,
        detailType: [
          'artifact.validated',
          'artifact.signed',
          'artifact.signing-failed',
          'artifact.archived',
          'artifact.archive-failed',
        ],
        // `artifact.validation-failed` intentionally NOT routed here — the
        // bundle never received a Metadata_Index record, so update-metadata
        // has nothing to write. It flows to the alarm rule below.
      },
    });

    this.validationFailedRule = new Rule(this, 'ValidationFailedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-validation-failed'),
      description: 'artifact.validation-failed → SNS alarm',
      eventPattern: {
        source: cairnSource,
        detailType: ['artifact.validation-failed'],
      },
      targets: [new SnsTopic(alarmTopic)],
    });

    this.signingFailedRule = new Rule(this, 'SigningFailedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-signing-failed'),
      description: 'artifact.signing-failed → SNS alarm',
      eventPattern: {
        source: cairnSource,
        detailType: ['artifact.signing-failed'],
      },
      targets: [new SnsTopic(alarmTopic)],
    });

    this.archiveFailedRule = new Rule(this, 'ArchiveFailedRule', {
      eventBus: this.bus,
      ruleName: naming.nameFor('artifact-archive-failed'),
      description: 'artifact.archive-failed → SNS alarm',
      eventPattern: {
        source: cairnSource,
        detailType: ['artifact.archive-failed'],
      },
      targets: [new SnsTopic(alarmTopic)],
    });

    // ── [RETAIN-013] deletion + retention-configuration alarms ────────────
    // CloudTrail management events on the DEFAULT bus. No errorCode filter:
    // a denied attempt is as alarm-worthy as a successful one. Every rule
    // publishes RETENTION_ALARM_MESSAGE (caller principal + resource).
    const cloudTrailRule = (
      id: string,
      resource: string,
      description: string,
      detail: Record<string, unknown>,
    ): Rule =>
      new Rule(this, id, {
        ruleName: naming.nameFor(`retention-alarm-${resource}`),
        description,
        eventPattern: {
          detailType: ['AWS API Call via CloudTrail'],
          detail,
        },
        targets: [new SnsTopic(alarmTopic, { message: RETENTION_ALARM_MESSAGE })],
      });
    const artifactRepos = { repositoryName: [{ prefix: `${naming.artifactNamespace}/` }] };
    this.retentionAlarmRules = [
      // Item 1: image delete, archive storage class, repository delete.
      cloudTrailRule('RetentionAlarmEcrImageDelete', 'ecr-delete', '[RETAIN-013] item 1: artifact image or repository deletion', {
        eventSource: ['ecr.amazonaws.com'],
        eventName: ['BatchDeleteImage', 'UpdateImageStorageClass', 'DeleteRepository'],
        requestParameters: artifactRepos,
      }),
      // Item 2: lifecycle policy or repository policy change.
      cloudTrailRule('RetentionAlarmEcrPolicy', 'ecr-policy', '[RETAIN-013] item 2: artifact repository lifecycle or repository policy change', {
        eventSource: ['ecr.amazonaws.com'],
        eventName: [
          'PutLifecyclePolicy',
          'DeleteLifecyclePolicy',
          'SetRepositoryPolicy',
          'DeleteRepositoryPolicy',
        ],
        requestParameters: artifactRepos,
      }),
      // Item 3: archive bucket lifecycle or Object Lock configuration.
      cloudTrailRule('RetentionAlarmArchiveBucket', 's3-config', '[RETAIN-013] item 3: archive bucket lifecycle or Object Lock change', {
        eventSource: ['s3.amazonaws.com'],
        eventName: [
          'PutBucketLifecycle',
          'PutBucketLifecycleConfiguration',
          'DeleteBucketLifecycle',
          'PutObjectLockConfiguration',
          'PutBucketObjectLockConfiguration',
        ],
        requestParameters: { bucketName: [archiveBucketName] },
      }),
      // Item 4a: retention-period or unarchived-manifest-window parameter
      // write, label, or delete.
      cloudTrailRule('RetentionAlarmRetentionParam', 'ssm-param', '[RETAIN-013] item 4: retention setting parameter change', {
        eventSource: ['ssm.amazonaws.com'],
        eventName: [
          'PutParameter',
          'LabelParameterVersion',
          'UnlabelParameterVersion',
          'DeleteParameter',
        ],
        requestParameters: { name: retentionSettingParams },
      }),
      // Item 4b: the batch delete names its targets in `names`, not `name`.
      cloudTrailRule('RetentionAlarmRetentionParamBatch', 'ssm-params', '[RETAIN-013] item 4: retention setting parameter batch delete', {
        eventSource: ['ssm.amazonaws.com'],
        eventName: ['DeleteParameters'],
        requestParameters: { names: retentionSettingParams },
      }),
      // Item 4c: archive-stage configuration. CloudTrail records the API with a
      // version suffix (`UpdateFunctionConfiguration20150331v2`), so match the
      // prefix; functionName is the name or the ARN.
      cloudTrailRule('RetentionAlarmArchiveStageConfig', 'lambda-config', '[RETAIN-013] item 4: archive stage function configuration change', {
        eventSource: ['lambda.amazonaws.com'],
        eventName: [{ prefix: 'UpdateFunctionConfiguration' }],
        requestParameters: {
          functionName: [archiveFunctionName, { suffix: `:function:${archiveFunctionName}` }],
        },
      }),
      // Item 5: any trail stop, delete, update, or event-selector change.
      cloudTrailRule('RetentionAlarmTrail', 'cloudtrail', '[RETAIN-013] item 5: CloudTrail trail stopped, deleted, or reconfigured', {
        eventSource: ['cloudtrail.amazonaws.com'],
        eventName: ['StopLogging', 'DeleteTrail', 'UpdateTrail', 'PutEventSelectors'],
      }),
    ];
  }
}
