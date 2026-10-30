import { Tags } from 'aws-cdk-lib';
import {
  AttributeType,
  BillingMode,
  Table,
  TableEncryption,
} from 'aws-cdk-lib/aws-dynamodb';
import { IKey } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

/** Tag key labeling a DynamoDB table with its logical role. Tables are
 * auto-named (no `tableName`) so a key-schema change replaces cleanly instead
 * of colliding on a fixed physical name; the role label is this tag (console /
 * Tag Editor / `list-tags-of-resource`-discoverable) and the resolved physical
 * name is published to SSM for out-of-stack consumers. See
 * artifact-store-stack.ts. */
export const RESOURCE_ROLE_TAG = 'cairn:resource';

export interface MetadataTableProps {
  readonly naming: Naming;
  readonly kmsKey: IKey;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
}

/**
 * Metadata_Index DynamoDB table (`metadata-index` [META-001]).
 *
 * One row per Artifact_Bundle. Per-bundle state lives here; the
 * (catalog/profile, control) → bundle relationship lives in a separate
 * ControlClaims table (see ControlClaimsTable construct).
 *
 * Key shape (the physical tenant-PK plane schema, [META-001]):
 *   PK (`pk`): `{tenant-id}` — the partition-key isolation boundary, scoped
 *              exact-match via `dynamodb:LeadingKeys` (NEVER a partition-key
 *              prefix); enforced by the auditor IAM policy in `iam-roles.ts`.
 *   SK (`sk`): `{project-id}#{manifest-digest}` — the queryable sub-hierarchy,
 *              addressed via `begins_with` for project-scoped listing ([META-004]).
 *
 * The attribute names are the generic `pk`/`sk` strings; the tenant-id /
 * sub-hierarchy semantics are owned by the `libs/metadata` write/query layer.
 *
 * NO Local Secondary Index sits on this tenant-PK plane (an LSI would pin a
 * tenant's whole item collection to one partition under the 10 GB cap);
 * alternate access paths are Global Secondary Indexes only, and any GSI added
 * here MUST itself lead with `{tenant-id}` as its partition key so
 * `dynamodb:LeadingKeys` tenant scoping binds the GSI query exactly as it binds
 * the base table ([META-001] item 4). No GSI ships today.
 *
 * On-demand billing for unpredictable load. PITR enabled for accidental
 * deletion / corruption recovery. KMS-CMK encryption per [ENCRYPT-002].
 */
export class MetadataTable extends Construct {
  readonly table: Table;

  constructor(scope: Construct, id: string, props: MetadataTableProps) {
    super(scope, id);

    this.table = new Table(this, 'Table', {
      partitionKey: { name: 'pk', type: AttributeType.STRING },
      sortKey: { name: 'sk', type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: props.kmsKey,
      removalPolicy: statefulRemovalPolicy(props.isProd),
    });
    // Label the auto-named table with its logical role (no explicit `tableName`
    // — see RESOURCE_ROLE_TAG). The resolved physical name is published to SSM
    // by the stack for out-of-stack consumers (CLI/TUI).
    Tags.of(this.table).add(RESOURCE_ROLE_TAG, 'ArtifactMetadata');
  }
}
