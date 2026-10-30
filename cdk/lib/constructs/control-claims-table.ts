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
import { RESOURCE_ROLE_TAG } from './metadata-table';
import { statefulRemovalPolicy } from './removal-policy';

export interface ControlClaimsTableProps {
  readonly naming: Naming;
  readonly kmsKey: IKey;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY
   * (disposable non-production deployment); true ⇒ RETAIN.
   */
  readonly isProd: boolean;
}

/**
 * ControlClaims table — the Control Claims relationship projection
 * (`metadata-index` [META-001]). A bundle *cites*
 * controls, which is a producer-claim fact.
 *
 * One row per `(tenant-id, catalogOrProfileUuid, controlId, manifestDigest)`.
 * The auditor query *"list every bundle citing AC-2 against profile X within my
 * tenant"* is a single tenant-scoped Query on this table.
 *
 * Key shape (the physical tenant-PK plane schema, [META-001]):
 *   PK (`pk`): `{tenant-id}` — the partition-key isolation boundary, scoped
 *              exact-match via `dynamodb:LeadingKeys`.
 *   SK (`sk`): `{catalogOrProfileUuid}#{controlId}#{manifest-digest}` — so a
 *              heavily-cited control is a sort-key band rather than a hot
 *              partition key, addressed via `begins_with`.
 *
 * The `catalogOrProfileUuid` SK segment prevents the OSCAL token-id name
 * clash: "ac-2" in NIST 800-53 rev5 is a different token from "ac-2" in another
 * catalog (OSCAL TokenDatatype IDs are unique only within the containing
 * document).
 *
 * NO Local Secondary Index sits on this tenant-PK plane; alternate access paths
 * are GSIs only, and any GSI MUST lead with `{tenant-id}` as its partition key
 * ([META-001] item 4). No GSI ships today.
 *
 * On-demand billing for unpredictable load. PITR enabled. KMS-CMK encryption
 * per [ENCRYPT-002].
 */
export class ControlClaimsTable extends Construct {
  readonly table: Table;

  constructor(scope: Construct, id: string, props: ControlClaimsTableProps) {
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
    // Auto-named; logical role carried by the tag, physical name published to
    // SSM by the stack (see RESOURCE_ROLE_TAG / artifact-store-stack.ts).
    Tags.of(this.table).add(RESOURCE_ROLE_TAG, 'ControlClaims');
  }
}
