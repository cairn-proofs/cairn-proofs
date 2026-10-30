import { Duration } from 'aws-cdk-lib';
import {
  ImageTagMutabilityExclusionFilter,
  Repository,
  TagMutability,
  TagStatus,
} from 'aws-cdk-lib/aws-ecr';
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
}

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
 * Lifecycle rule ([INGEST-027]): remove untagged manifests after 90 days. The
 * WORM_Receipt gating ("only after the receipt referrer is attached") is
 * enforced at the producer-pipeline layer rather than via ECR lifecycle policy
 * — ECR lifecycle has no native way to inspect referrers. The pipeline relies
 * on archive-to-s3 emitting the receipt before any untagged manifest can age
 * into the lifecycle window.
 *
 * In-flight (archived-but-not-yet-tagged) safety: a manifest is
 * pushed UNTAGGED and only receives its
 * canonical tag at the pipeline TAIL (the post-archive mint-tag Lambda). So a
 * bundle is legitimately untagged for the full validate → sign → archive →
 * mint latency (seconds to a few minutes in practice; minutes even under
 * retry/DLQ re-drive). The 90-day window comfortably exceeds that latency by
 * orders of magnitude, so an in-flight artifact can never age into the
 * lifecycle window and be reaped before mint applies its tag. Do NOT shorten
 * this window below the worst-case end-to-end-through-mint latency.
 */
export class Repositories extends Construct {
  readonly repository: Repository;

  constructor(scope: Construct, id: string, props: RepositoriesProps) {
    super(scope, id);

    const { naming, orgId, tenantId, projectId, kmsKey, isProd } = props;

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

    this.repository.addLifecycleRule({
      description:
        'Remove untagged manifests after 90 days. WORM_Receipt presence is enforced upstream.',
      tagStatus: TagStatus.UNTAGGED,
      maxImageAge: Duration.days(90),
    });
  }
}
