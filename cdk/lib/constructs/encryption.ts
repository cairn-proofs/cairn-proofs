import { Key } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

export interface EncryptionProps {
  readonly naming: Naming;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
}

/**
 * Service-owned KMS Customer Managed Key for ECR / S3 / DynamoDB / SQS / SNS
 * encryption (encryption [ENCRYPT-002]). Service-CMK path; Customer_CMK
 * override is a deferred option.
 *
 * Per-resource encryption configuration lives in each owning construct; this
 * file only mints the key. RemovalPolicy is RETAIN on a production stack
 * (`isProd=true`) so the key survives stack destroy — its absence would render
 * every encrypted ECR image, S3 object, and DynamoDB row unreadable. On a
 * disposable non-production stack (`isProd=false`) the key is DESTROY: KMS
 * still imposes its pending-deletion window (not billed while pending; the
 * alias frees immediately) rather than deleting instantly.
 */
export class Encryption extends Construct {
  readonly serviceKey: Key;

  constructor(scope: Construct, id: string, props: EncryptionProps) {
    super(scope, id);

    const { naming } = props;
    this.serviceKey = new Key(this, 'ServiceKey', {
      alias: naming.kmsAliasFor('service'),
      description: `Cairn Proofs service-owned CMK (${naming.qualifier})`,
      enableKeyRotation: true,
      removalPolicy: statefulRemovalPolicy(props.isProd),
    });
  }
}
