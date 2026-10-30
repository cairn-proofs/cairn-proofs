import { CfnDeletionPolicy, CfnResource, IAspect, RemovalPolicy } from 'aws-cdk-lib';
import { CfnRepository } from 'aws-cdk-lib/aws-ecr';
import { IConstruct } from 'constructs';

/**
 * Teardown-readiness gate for the stack's stateful resources,
 * keyed on `isProd`.
 *
 * - `isProd=false` (non-production, e.g. alpha): the deployment is
 *   disposable — stateful resources are DESTROY so `cdk destroy` tears the
 *   stack down cleanly with no orphaned (still-billing) resources. The owning
 *   constructs pair this with `autoDeleteObjects: true` (S3) and
 *   `emptyOnDelete: true` (ECR); without the auto-empty, a non-empty
 *   bucket/repo still blocks deletion even under DESTROY.
 * - `isProd=true` (production): stateful resources stay RETAIN and the
 *   auto-empty knobs stay off so a stack-level `cdk destroy` can never
 *   irreversibly drop production data.
 *
 * Teardown readiness is DECOUPLED from `WORM_Enabled`: S3 Object Lock is an
 * orthogonal data-immutability knob (see `archive.ts`), while `isProd` alone
 * decides whether a stack RETAINs or sheds its stateful resources on destroy.
 * The two stay coherent via the stack-level invariant that `WORM_Enabled`
 * implies `isProd` (an Object-Lock bucket cannot be auto-emptied), so a WORM
 * stack is always a RETAIN stack — Object Lock then rejects auto-delete on the
 * archive bucket regardless, as defense-in-depth behind this gate.
 *
 * The {@link ProductionTeardownGuard} aspect fails synth if a
 * future edit ever makes a stateful resource deletable on an `isProd=true`
 * stack.
 */
export function statefulRemovalPolicy(isProd: boolean): RemovalPolicy {
  return isProd ? RemovalPolicy.RETAIN : RemovalPolicy.DESTROY;
}

/**
 * CloudFormation resource types this stack treats as stateful (data-bearing).
 * Every resource of these types must be RETAIN on an `isProd=true` stack.
 */
const STATEFUL_RESOURCE_TYPES: ReadonlySet<string> = new Set([
  'AWS::S3::Bucket',
  'AWS::ECR::Repository',
  'AWS::DynamoDB::Table',
  'AWS::KMS::Key',
  'AWS::SQS::Queue',
  'AWS::Logs::LogGroup',
]);

/**
 * Synth-time enforcement of the teardown-readiness gate: on an `isProd=true`
 * stack, THROWS — failing synth — if any
 * stateful resource is deletable (`DeletionPolicy`/`UpdateReplacePolicy` other
 * than `Retain`), if any S3 auto-delete custom resource is present, or if the
 * ECR repository sets `EmptyOnDelete`.
 *
 * Applied to the whole stack via `Aspects.of(stack).add(...)`, so it guards
 * every current and future stateful construct — the gate cannot silently
 * invert under refactoring. No-op when `isProd=false`.
 */
export class ProductionTeardownGuard implements IAspect {
  constructor(private readonly isProd: boolean) {}

  visit(node: IConstruct): void {
    if (!this.isProd || !(node instanceof CfnResource)) {
      return;
    }

    if (node.cfnResourceType === 'Custom::S3AutoDeleteObjects') {
      throw new Error(
        `isProd=true forbids S3 autoDeleteObjects, but '${node.node.path}' ` +
          'synthesizes an auto-delete custom resource. Auto-emptying a ' +
          'production archive deployment would destroy retained data.',
      );
    }

    if (node instanceof CfnRepository && node.emptyOnDelete === true) {
      throw new Error(
        `isProd=true forbids ECR emptyOnDelete, but '${node.node.path}' ` +
          'sets it. Auto-emptying the evidence repository on a production ' +
          'stack would destroy artifacts.',
      );
    }

    if (STATEFUL_RESOURCE_TYPES.has(node.cfnResourceType)) {
      const { deletionPolicy, updateReplacePolicy } = node.cfnOptions;
      if (
        deletionPolicy !== CfnDeletionPolicy.RETAIN ||
        updateReplacePolicy !== CfnDeletionPolicy.RETAIN
      ) {
        throw new Error(
          `isProd=true requires every stateful resource to be RETAIN, ` +
            `but '${node.node.path}' (${node.cfnResourceType}) has ` +
            `DeletionPolicy=${deletionPolicy ?? 'unset (Delete)'} / ` +
            `UpdateReplacePolicy=${updateReplacePolicy ?? 'unset (Delete)'}. ` +
            'A deletable production stack risks irreversible destruction of ' +
            'retained production data.',
        );
      }
    }
  }
}
