import {
  ImageTagMutabilityExclusionFilter,
  Repository,
  TagMutability,
} from 'aws-cdk-lib/aws-ecr';
import { AnyPrincipal, Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { IKey } from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

/**
 * ECR tag-mutability exclusion filters scoping the mutable namespace to exactly
 * the reserved alias tags (spec *ECR Tag Immutability Enabled*).
 *
 * Alias tags are MIDDLE-token: `{artifact-kind}--_als--{name}` (named alias) and
 * `{artifact-kind}--_chn--{channel}` (channel pointer). The canonical
 * `{artifact-kind}--_ref--{digest}` form stays hard-immutable.
 *
 * ECR allows at most 5 exclusion filters per repo (pattern
 * `^[0-9a-zA-Z._*-]{1,128}$`). Per-kind prefix filters are infeasible (15
 * artifact-kinds), so this is the MIDDLE-TOKEN PRIMARY path: two contains-style
 * filters, one per reserved alias token. Both depend on ECR's WILDCARD engine
 * evaluating a LEADING `*` as a contains-match.
 *
 * OQ2 is a DEPLOY-TIME empirical gate, NOT a code/synth gate: whether ECR
 * actually honors the leading-`*` (contains) wildcard is confirmed at deploy
 * (provision a throwaway `IMMUTABLE_WITH_EXCLUSION` repo, confirm a `_als` tag
 * is overwritable while a `_ref` tag is not). If a deploy-time check shows ECR
 * rejects the leading wildcard, the FALLBACK is leading-token alias forms with
 * simple prefix filters `_als--*` / `_chn--*` plus a leading-token dispatch arm
 * in `parse_tag` — a separate follow-up. This construct implements the primary
 * only.
 */
const ALIAS_TAG_MUTABILITY_EXCLUSION_FILTERS = [
  ImageTagMutabilityExclusionFilter.wildcard('*--_als--*'),
  ImageTagMutabilityExclusionFilter.wildcard('*--_chn--*'),
];

export interface RepositoriesProps {
  readonly naming: Naming;
  /** Opaque organization id — leading segment of the logical tenant hierarchy. */
  readonly orgId: string;
  /** Opaque tenant id — middle segment of the logical tenant hierarchy (NOT an AWS account). */
  readonly tenantId: string;
  /** Opaque project id — trailing segment of the logical tenant hierarchy. */
  readonly projectId: string;
  readonly kmsKey: IKey;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY +
   * emptyOnDelete (disposable non-production deployment); true ⇒ RETAIN, no
   * auto-empty.
   */
  readonly isProd: boolean;
  /**
   * Principal ARNs the repository policy admits for `ecr:BatchDeleteImage`
   * ([ACCESS-010] item 1): the project's pruner role and, on a non-production
   * stack only, the integration-test cleanup role and the stack deployment
   * role (for the `emptyOnDelete` teardown). Each must be a resolvable ARN —
   * no literal `${AWS::...}` placeholder.
   */
  readonly imageDeleteExceptionArns: string[];
  /**
   * The stack deployment role (the synthesizer's CloudFormation execution
   * role) — the only principal admitted for repository deletion and lifecycle
   * / repository-policy changes ([ACCESS-010] item 3).
   */
  readonly deploymentRoleArn: string;
}

/** Actions only the stack deployment may perform on an artifact repository. */
export const DEPLOYMENT_ONLY_REPOSITORY_ACTIONS = [
  'ecr:DeleteRepository',
  'ecr:PutLifecyclePolicy',
  'ecr:DeleteLifecyclePolicy',
  'ecr:SetRepositoryPolicy',
  'ecr:DeleteRepositoryPolicy',
];

/**
 * Single ECR repository per project, addressed through the uniform three-segment
 * logical tenant hierarchy `{org-id}/{tenant-id}/{project-id}`
 * (access-control [ACCESS-002]).
 *
 * The repository path is
 * `{appName}-artifact-{regionCode}-{stage}/{org-id}/{tenant-id}/{project-id}` —
 * exactly one path shape, all three segments mandatory, each a stable, opaque,
 * immutable, allocate-once identifier. `{tenant-id}` is a LOGICAL tenant, never
 * an AWS account; the producing identity is per-artifact provenance (the signed
 * claim, the CloudTrail push principal, and the signature), never a path
 * segment. There is one repository per project and one path shape.
 *
 * The artifact-kind vocabulary lives only in Python
 * (`libs/oci/cairn_oci/categories.py` `ARTIFACT_KINDS`); CDK does not mirror
 * it.
 *
 * Tag immutability enabled ([INGEST-020]), in `IMMUTABLE_WITH_EXCLUSION` mode: the
 * canonical `_ref` tags are immutable while the reserved alias namespace
 * (`_als`/`_chn`) is mutable via the exclusion filters above.
 * KMS encryption with the customer-managed key ([ENCRYPT-002]).
 *
 * No lifecycle policy ([RETAIN-006]): every referrer the pipeline writes is
 * untagged, and an ECR lifecycle rule cannot see referrers or receipts, so no
 * rule can tell junk from evidence. Untagged manifests stay until an operator
 * prunes them with `cairn prune`.
 *
 * Stack-owned repository policy ([ACCESS-010]). Every exception is an
 * `aws:PrincipalArn` condition on a Deny, never `NotPrincipal`:
 * - `ecr:BatchDeleteImage` is denied to everyone except the pruner role (and,
 *   non-production only, the integration-test cleanup role and the stack
 *   deployment role, which the `emptyOnDelete` teardown runs as);
 * - `ecr:UpdateImageStorageClass` (the archive storage class) is denied to
 *   everyone;
 * - repository deletion and lifecycle / repository-policy changes are denied to
 *   everyone except the stack deployment role, so the deploy and the
 *   non-production `emptyOnDelete` teardown keep working.
 * The policy contains no Allow, so it grants nothing cross-account.
 */
export class Repositories extends Construct {
  readonly repository: Repository;

  constructor(scope: Construct, id: string, props: RepositoriesProps) {
    super(scope, id);

    const {
      naming,
      orgId,
      tenantId,
      projectId,
      kmsKey,
      isProd,
      imageDeleteExceptionArns,
      deploymentRoleArn,
    } = props;
    if (imageDeleteExceptionArns.length === 0) {
      throw new Error(
        'imageDeleteExceptionArns must name the pruner role ([ACCESS-010] item 1).',
      );
    }

    // Repo path = {artifactNamespace}/{org-id}/{tenant-id}/{project-id} =
    // {appName}-artifact-{qualifier}/{org-id}/{tenant-id}/{project-id}.
    // The namespace is the SINGLE composition point (naming.artifactNamespace);
    // the Notation trust scope and pipeline IAM ARNs derive from the same value
    // — no independent literal `artifact-${qualifier}` interpolation.
    const repoName = `${naming.artifactNamespace}/${orgId}/${tenantId}/${projectId}`;
    this.repository = new Repository(this, 'Repo', {
      repositoryName: repoName,
      imageTagMutability: TagMutability.IMMUTABLE_WITH_EXCLUSION,
      imageTagMutabilityExclusionFilters: ALIAS_TAG_MUTABILITY_EXCLUSION_FILTERS,
      encryptionKey: kmsKey,
      emptyOnDelete: !isProd,
      removalPolicy: statefulRemovalPolicy(isProd),
    });

    this.repository.addToResourcePolicy(
      new PolicyStatement({
        sid: 'DenyImageDeleteExceptPruner',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: ['ecr:BatchDeleteImage'],
        conditions: { ArnNotEquals: { 'aws:PrincipalArn': imageDeleteExceptionArns } },
      }),
    );
    this.repository.addToResourcePolicy(
      new PolicyStatement({
        sid: 'DenyArchiveStorageClass',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: ['ecr:UpdateImageStorageClass'],
      }),
    );
    this.repository.addToResourcePolicy(
      new PolicyStatement({
        sid: 'DenyRepositoryChangesExceptDeployment',
        effect: Effect.DENY,
        principals: [new AnyPrincipal()],
        actions: DEPLOYMENT_ONLY_REPOSITORY_ACTIONS,
        conditions: { ArnNotEquals: { 'aws:PrincipalArn': deploymentRoleArn } },
      }),
    );
  }
}
