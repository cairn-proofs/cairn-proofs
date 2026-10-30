import { Duration } from 'aws-cdk-lib';
import {
  Alarm,
  ComparisonOperator,
  Metric,
  Statistic,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { SnsAction } from 'aws-cdk-lib/aws-cloudwatch-actions';
import { IFunction } from 'aws-cdk-lib/aws-lambda';
import { ITopic } from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { DeadLetterQueues } from './dead-letter-queues';

export interface AlarmsProps {
  readonly naming: Naming;
  readonly alarmTopic: ITopic;
  readonly deadLetterQueues: DeadLetterQueues;
  /**
   * Pipeline Lambdas (validate-bundle, sign-bundle, archive-to-s3,
   * update-metadata) — used to provision per-function `DeadLetterErrors`
   * alarms keyed on `FunctionName`. Surfaces a DLQ-write IAM/KMS regression
   * the existing `DLQ depth` alarm cannot see (depth stays at 0 when
   * delivery silently fails). Per `observability` spec
   * "DeadLetterErrors alarm per pipeline Lambda" Requirement.
   */
  readonly pipelineLambdas: IFunction[];
  /**
   * The `archive-to-s3` Lambda specifically — wired separately (rather than
   * filtered out of `pipelineLambdas` by node id) so the duration-envelope
   * alarm has a typed handle on the one function whose archival DURATION is
   * the load-bearing risk under the raised content-layer bound.
   */
  readonly archiveFunction: IFunction;
  /**
   * Whether the SNS actions of the pipeline-failure alarms are enabled. The
   * stack passes `isProd || alarmActionsEnabled` (see `artifact-store-stack.ts`),
   * so production always enables them and a non-production deployment may opt in.
   * When false, the alarms still evaluate and stay console-visible but publish
   * no SNS notification.
   */
  readonly actionsEnabled: boolean;
}

/**
 * CloudWatch alarms for the pipeline-failure surface (`observability` spec
 * [OBS-011]).
 *
 * Retained alarm groups (each watches an AWS-managed metric with known
 * dimensions, so all are design-independent and console-visible):
 * - DLQ depth > 0, one alarm per dead-letter queue.
 * - `AWS/Lambda::DeadLetterErrors` > 0, one alarm per pipeline Lambda — covers
 *   a failed async-invoke DLQ write (IAM/KMS denial, missing queue) that the
 *   DLQ-depth alarm cannot see, because depth stays at 0 when the write fails.
 * - The `archive-to-s3` per-invocation duration envelope.
 *
 * The three custom-metric failure alarms (`SigningFailure`, `ArchiveFailure`,
 * `IntegrityMismatch`) and the validation skip-rate dashboard are NOT
 * provisioned; they watched metric tuples no handler emits.
 *
 * SNS-action posture: an alarm's actions are enabled when the construct
 * receives `actionsEnabled=true`. The stack computes that as
 * `isProd || alarmActionsEnabled` — production always enables notification and
 * a non-production deployment may opt in via `alarmActionsEnabled`. When
 * `actionsEnabled` is false the alarms keep evaluating and stay console-visible
 * but publish no SNS notification; enabling is purely the flag, never a re-wire.
 *
 * Archive duration envelope:
 * raising the max content-layer count 100→300 means `archive-to-s3` does up to
 * 300 serial fetch→verify_digest→PutObject hops within one invocation (layers
 * are processed one at a time, so the raise stresses DURATION, not peak memory)
 * against its 10-minute Lambda timeout. The `archiveDurationAlarm` below fires
 * when the function's per-invocation Duration crosses the documented operating
 * bound of 8 minutes (480000 ms ≈ 80% of the timeout) — the early-warning line
 * that the largest legitimate bundles are approaching the timeout ceiling.
 */
export class Alarms extends Construct {
  /** Documented operating bound for `archive-to-s3` per-invocation duration:
   * 8 minutes, ≈80% of the 10-minute Lambda timeout. */
  static readonly ARCHIVE_DURATION_BOUND_MS = Duration.minutes(8).toMilliseconds();

  readonly dlqDepthAlarms: Alarm[];
  readonly dlqDeliveryErrorAlarms: Alarm[];
  readonly archiveDurationAlarm: Alarm;

  constructor(scope: Construct, id: string, props: AlarmsProps) {
    super(scope, id);

    const { naming, alarmTopic, deadLetterQueues, pipelineLambdas, archiveFunction, actionsEnabled } =
      props;
    const alarmAction = new SnsAction(alarmTopic);

    // ── DLQ depth — one alarm per queue ────────────────────────────────────
    this.dlqDepthAlarms = deadLetterQueues.all().map((q) => {
      const alarm = new Alarm(this, `DlqDepthAlarm-${q.node.id}`, {
        alarmName: naming.nameFor(`dlq-depth-${q.node.id}`),
        alarmDescription: `DLQ ${q.queueName} has messages — pipeline permanent failure`,
        metric: q.metricApproximateNumberOfMessagesVisible({
          period: Duration.minutes(1),
          statistic: Statistic.MAXIMUM,
        }),
        threshold: 0,
        comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: TreatMissingData.NOT_BREACHING,
        actionsEnabled,
      });
      alarm.addAlarmAction(alarmAction);
      return alarm;
    });

    // ── DLQ delivery health — one alarm per Lambda ────────────────────────
    // `AWS/Lambda::DeadLetterErrors` increments when Lambda gives up trying
    // to write a failed async invocation to its function-level DLQ (IAM
    // missing on the role, KMS key policy denying, queue missing, etc.).
    // The DLQ-depth alarm above cannot see this regression — depth stays
    // at 0 when delivery itself fails. See
    // `runbooks/troubleshooting.md#dlq-delivery-health` for response.
    this.dlqDeliveryErrorAlarms = pipelineLambdas.map((fn) => {
      // `fn.functionName` is a CDK token at synth time, so string `.replace`
      // calls don't match the literal qualifier/prefix and the resolved name
      // ends up doubly-decorated. The construct id (e.g. `ValidateEvidence`)
      // is a synth-time literal — convert to kebab-case for the alarm name.
      const shortName = fn.node.id
        .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
        .toLowerCase();
      const metric = new Metric({
        namespace: 'AWS/Lambda',
        metricName: 'DeadLetterErrors',
        dimensionsMap: { FunctionName: fn.functionName },
        statistic: Statistic.SUM,
        period: Duration.minutes(5),
      });
      const alarm = new Alarm(this, `DlqDeliveryErrorAlarm-${fn.node.id}`, {
        alarmName: naming.nameFor(`${shortName}-dlq-delivery-errors`),
        alarmDescription: `Lambda ${fn.functionName} failed to deliver async-invoke failures to its DLQ — see runbooks/troubleshooting.md#dlq-delivery-health`,
        metric,
        threshold: 0,
        comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: TreatMissingData.NOT_BREACHING,
        actionsEnabled,
      });
      alarm.addAlarmAction(alarmAction);
      return alarm;
    });

    // ── archive-to-s3 duration envelope ───────────────────────────────────
    // Operating bound: at the raised 300-content-layer ceiling, archive-to-s3
    // does up to 300 serial fetch→verify_digest→PutObject hops in one
    // invocation against its 10-minute timeout. We alarm on per-invocation
    // MAXIMUM Duration crossing 8 minutes (≈80% of the timeout) — the
    // early-warning line that the largest bundles are nearing the ceiling, well
    // before the timeout itself trips and dumps the invocation to the DLQ.
    const archiveDurationMetric = archiveFunction.metricDuration({
      statistic: Statistic.MAXIMUM,
      period: Duration.minutes(5),
    });
    this.archiveDurationAlarm = new Alarm(this, 'ArchiveDurationAlarm', {
      alarmName: naming.nameFor('archive-to-s3-duration'),
      alarmDescription:
        `${archiveFunction.functionName} per-invocation duration crossed the ` +
        `${Alarms.ARCHIVE_DURATION_BOUND_MS} ms operating bound (8 min ≈ 80% of ` +
        `the 10-min timeout). At the 300-content-layer ceiling archival does up ` +
        `to 300 serial fetch→verify→PutObject hops in one invocation; this fires ` +
        `before the timeout trips.`,
      metric: archiveDurationMetric,
      threshold: Alarms.ARCHIVE_DURATION_BOUND_MS,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: TreatMissingData.NOT_BREACHING,
      actionsEnabled,
    });
    this.archiveDurationAlarm.addAlarmAction(alarmAction);
  }
}
