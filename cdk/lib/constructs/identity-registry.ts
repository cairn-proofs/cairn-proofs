import { Stack, Tags } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { IKey } from 'aws-cdk-lib/aws-kms';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { RESOURCE_ROLE_TAG } from './metadata-table';
import { statefulRemovalPolicy } from './removal-policy';

export interface IdentityRegistryProps {
  readonly naming: Naming;
  readonly kmsKey: IKey;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
  /** Opaque org id seeded at deploy (the alpha's single org). */
  readonly orgId: string;
  /** Opaque tenant id seeded at deploy (the alpha's single tenant). */
  readonly tenantId: string;
  /** Opaque project id seeded at deploy (the alpha's single project). */
  readonly projectId: string;
  /** Human display name for the seeded org. Defaults to the opaque org id. */
  readonly orgDisplayName?: string;
  /** Human display name for the seeded tenant. Defaults to the opaque tenant id. */
  readonly tenantDisplayName?: string;
  /** Human display name for the seeded project. Defaults to the opaque project id. */
  readonly projectDisplayName?: string;
}

/**
 * Identity_Registry — the control-plane table that resolves opaque
 * `{org-id}`/`{tenant-id}`/`{project-id}` coordinates to their human display
 * names and records the tenant→org parentage (`identity-registry`
 * [IDREG-001]).
 *
 * This is canonical, MUTABLE control-plane state — encrypted at rest with the
 * service-owned CMK and IAM-gated identically to every other DynamoDB table.
 * It is DISTINCT from the rebuildable Metadata_Index projection: losing it costs
 * only display-name resolution, never artifact identity, discoverability, or
 * verifiability. Display names live ONLY here — never in the ECR/S3 path, the
 * signed claim, or an ECR resource tag (those are operator/console-visible).
 *
 * Key shape:
 *   PK (`id`): the opaque identity coordinate (org / tenant / project id). The
 *              ids are globally-unique opaque tokens, so the bare id is a
 *              sufficient key; the item carries `kind`, `displayName`, and
 *              the parentage attribute (`orgId` on a tenant, `tenantId` on a
 *              project) so a read-time join resolves a display name without
 *              denormalizing it onto Metadata_Index rows. The `id`/`kind`/
 *              `displayName`/`orgId` attribute names match the `cairn_metadata`
 *              `IdentityRegistry` facade that reads these seeded items.
 *
 * Alpha scope: seeded at deploy for the single org/tenant/project via the
 * deploy-time `AwsCustomResource` PutItems below ([IDREG-001]). Runtime
 * population (onboarding / provisioning) is deferred with the multi-tenant
 * provisioning path.
 *
 * On-demand billing. PITR enabled. KMS-CMK encryption per [ENCRYPT-002].
 */
export class IdentityRegistry extends Construct {
  readonly table: Table;

  constructor(scope: Construct, id: string, props: IdentityRegistryProps) {
    super(scope, id);

    const { kmsKey, isProd, orgId, tenantId, projectId } = props;

    this.table = new Table(this, 'Table', {
      partitionKey: { name: 'id', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: kmsKey,
      removalPolicy: statefulRemovalPolicy(isProd),
    });
    // Auto-named; logical role carried by the tag, physical name published to
    // SSM by the stack (see RESOURCE_ROLE_TAG / artifact-store-stack.ts).
    Tags.of(this.table).add(RESOURCE_ROLE_TAG, 'IdentityRegistry');

    // ── Deploy-time seed of the single org/tenant/project ([IDREG-001]) ──────
    // Each seed is a conditional PutItem so the custom resource is idempotent
    // across redeploys (a re-seed never clobbers an operator's later
    // display-name rename of the same id). DynamoDB PutItem cannot key a
    // CDK-token table name into an object key, so each item is its own
    // single-call AwsCustomResource (TableName is a string value, which CDK
    // tokens resolve cleanly).
    const seeds: Array<{ id: string; item: Record<string, unknown> }> = [
      {
        id: orgId,
        item: {
          id: { S: orgId },
          kind: { S: 'org' },
          displayName: { S: props.orgDisplayName ?? orgId },
        },
      },
      {
        id: tenantId,
        item: {
          id: { S: tenantId },
          kind: { S: 'tenant' },
          displayName: { S: props.tenantDisplayName ?? tenantId },
          // Parentage: every tenant resolves to exactly one org ([IDREG-001] item 2).
          orgId: { S: orgId },
        },
      },
      {
        id: projectId,
        item: {
          id: { S: projectId },
          kind: { S: 'project' },
          displayName: { S: props.projectDisplayName ?? projectId },
          tenantId: { S: tenantId },
        },
      },
    ];

    const seedPolicy = AwsCustomResourcePolicy.fromStatements([
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['dynamodb:PutItem'],
        resources: [this.table.tableArn],
      }),
      // The table is CMK-encrypted; the seed write goes through DynamoDB's KMS
      // path, so the custom-resource role needs envelope-key access mediated by
      // DynamoDB only.
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['kms:Decrypt', 'kms:GenerateDataKey', 'kms:DescribeKey'],
        resources: [kmsKey.keyArn],
        conditions: {
          StringEquals: {
            'kms:ViaService': `dynamodb.${Stack.of(this).region}.amazonaws.com`,
          },
        },
      }),
    ]);

    let previous: AwsCustomResource | undefined;
    for (const seed of seeds) {
      const seedResource = new AwsCustomResource(this, `Seed${seed.id.replace(/[^a-zA-Z0-9]/g, '')}`, {
        onCreate: {
          service: 'DynamoDB',
          action: 'putItem',
          parameters: {
            TableName: this.table.tableName,
            Item: seed.item,
            // Idempotent: only write when this id is not already present, so a
            // redeploy never overwrites a later operator rename of the seed.
            ConditionExpression: 'attribute_not_exists(id)',
          },
          physicalResourceId: PhysicalResourceId.of(`identity-registry-seed-${seed.id}`),
          // A failed condition (already seeded) is not an error.
          ignoreErrorCodesMatching: 'ConditionalCheckFailedException',
        },
        onUpdate: {
          service: 'DynamoDB',
          action: 'putItem',
          parameters: {
            TableName: this.table.tableName,
            Item: seed.item,
            ConditionExpression: 'attribute_not_exists(id)',
          },
          physicalResourceId: PhysicalResourceId.of(`identity-registry-seed-${seed.id}`),
          ignoreErrorCodesMatching: 'ConditionalCheckFailedException',
        },
        policy: seedPolicy,
      });
      // Serialize the seeds so the shared singleton provider issues them in a
      // deterministic order (org → tenant → project parentage).
      if (previous) {
        seedResource.node.addDependency(previous);
      }
      previous = seedResource;
    }
  }
}
