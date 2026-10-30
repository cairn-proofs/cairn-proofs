import { Duration } from 'aws-cdk-lib';
import { IKey } from 'aws-cdk-lib/aws-kms';
import { Queue, QueueEncryption } from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

export interface DeadLetterQueuesProps {
  readonly naming: Naming;
  readonly kmsKey: IKey;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
}

/**
 * Per-Lambda SQS dead-letter queues (`processing-pipeline` [PIPELINE-005]).
 *
 * 14-day message retention so on-call has a full retention window to triage
 * permanent failures before SQS expires them. KMS-CMK encryption via the
 * service key. The Lambda constructs in the merge pass wire each function's
 * `deadLetterQueue` prop to the matching queue below.
 */
export class DeadLetterQueues extends Construct {
  readonly validateBundle: Queue;
  readonly signBundle: Queue;
  readonly archiveToS3: Queue;
  readonly updateMetadata: Queue;
  readonly mintTag: Queue;

  constructor(scope: Construct, id: string, props: DeadLetterQueuesProps) {
    super(scope, id);

    const { naming } = props;
    const make = (logicalId: string, name: string): Queue =>
      new Queue(this, logicalId, {
        queueName: name,
        retentionPeriod: Duration.days(14),
        encryption: QueueEncryption.KMS,
        encryptionMasterKey: props.kmsKey,
        enforceSSL: true,
        removalPolicy: statefulRemovalPolicy(props.isProd),
      });

    this.validateBundle = make('ValidateBundleDlq', naming.nameFor('validate-bundle-dlq'));
    this.signBundle = make('SignBundleDlq', naming.nameFor('sign-bundle-dlq'));
    this.archiveToS3 = make('ArchiveToS3Dlq', naming.nameFor('archive-to-s3-dlq'));
    this.updateMetadata = make('UpdateMetadataDlq', naming.nameFor('update-metadata-dlq'));
    this.mintTag = make('MintTagDlq', naming.nameFor('mint-tag-dlq'));
  }

  all(): Queue[] {
    return [
      this.validateBundle,
      this.signBundle,
      this.archiveToS3,
      this.updateMetadata,
      this.mintTag,
    ];
  }
}
