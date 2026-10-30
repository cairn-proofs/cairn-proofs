import { Topic } from 'aws-cdk-lib/aws-sns';
import { EmailSubscription } from 'aws-cdk-lib/aws-sns-subscriptions';
import { Construct } from 'constructs';
import { Naming } from '../naming';

export interface NotificationsProps {
  readonly naming: Naming;
  readonly notificationEmail: string;
}

/**
 * SNS topic for operator alarms + an email subscription stub.
 *
 * The subscription requires confirmation via the email's opt-in link the
 * first time the stack is deployed; until confirmed, alarms fire but the
 * email subscription returns "Pending confirmation". CloudWatch alarms
 * wire directly to this topic; the email subscriber is just the routing
 * endpoint.
 */
export class Notifications extends Construct {
  readonly alarmTopic: Topic;

  constructor(scope: Construct, id: string, props: NotificationsProps) {
    super(scope, id);

    const { naming } = props;
    this.alarmTopic = new Topic(this, 'AlarmTopic', {
      topicName: naming.nameFor('alarms'),
      displayName: `Cairn Proofs alarms (${naming.qualifier})`,
    });

    if (props.notificationEmail) {
      this.alarmTopic.addSubscription(new EmailSubscription(props.notificationEmail));
    }
  }
}
