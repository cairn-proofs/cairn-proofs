import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

export interface LogGroupsProps {
  readonly naming: Naming;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
}

/**
 * Per-Lambda CloudWatch log groups.
 *
 * Pre-created so the Lambda constructs (merge pass) can reference them by
 * name AND so retention is explicit at stack-define time rather than
 * inheriting Lambda's auto-create default of "never expire" (which silently
 * burns budget).
 *
 * 30-day retention — long enough to triage incidents over a sprint, short
 * enough to bound storage.
 */
export class LogGroups extends Construct {
  readonly validateBundle: LogGroup;
  readonly signBundle: LogGroup;
  readonly archiveToS3: LogGroup;
  readonly updateMetadata: LogGroup;
  readonly mintTag: LogGroup;

  constructor(scope: Construct, id: string, props: LogGroupsProps) {
    super(scope, id);

    const { naming } = props;
    const make = (logicalId: string, fnName: string): LogGroup =>
      new LogGroup(this, logicalId, {
        logGroupName: `/aws/lambda/${fnName}`,
        retention: RetentionDays.ONE_MONTH,
        removalPolicy: statefulRemovalPolicy(props.isProd),
      });

    this.validateBundle = make('ValidateBundle', naming.nameFor('validate-bundle'));
    this.signBundle = make('SignBundle', naming.nameFor('sign-bundle'));
    this.archiveToS3 = make('ArchiveToS3', naming.nameFor('archive-to-s3'));
    this.updateMetadata = make('UpdateMetadata', naming.nameFor('update-metadata'));
    this.mintTag = make('MintTag', naming.nameFor('mint-tag'));
  }
}
