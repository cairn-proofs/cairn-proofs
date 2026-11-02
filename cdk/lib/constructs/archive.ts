import { Duration } from 'aws-cdk-lib';
import { IKey } from 'aws-cdk-lib/aws-kms';
import {
  Bucket,
  BucketEncryption,
  BlockPublicAccess,
  ObjectOwnership,
  StorageClass,
  TransitionDefaultMinimumObjectSize,
} from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { Naming } from '../naming';
import { statefulRemovalPolicy } from './removal-policy';

export interface ArchiveProps {
  readonly naming: Naming;
  /**
   * When true, the bucket is created with S3 Object Lock in Compliance mode
   * — irreversible. With WORM_Enabled=false, the branch still runs the archive
   * workflow but does NOT enable Object Lock.
   * Object Lock only — teardown is gated separately by `isProd` (always true
   * when this is true, per the stack-level invariant).
   */
  readonly wormEnabled: boolean;
  /**
   * Gates teardown-readiness (see `statefulRemovalPolicy`): false ⇒ DESTROY +
   * autoDeleteObjects (disposable non-production deployment); true ⇒ RETAIN,
   * no auto-delete.
   */
  readonly isProd: boolean;
  readonly kmsKey: IKey;
  /**
   * Expiration of the access-log bucket's objects, in days. The stack passes
   * the retention period so the S3 access logs cover a bundle's whole
   * retention and its prune (access-control [ACCESS-004], M10).
   */
  readonly accessLogRetentionDays: number;
}

/**
 * S3 archive bucket (`worm-archive` [WORM-001], [WORM-002]).
 *
 * With `WORM_Enabled=false`, the bucket has versioning, KMS-CMK
 * encryption, public access blocked, and access logging — but no Object
 * Lock. The lifecycle tiering ([RETAIN-001], default 90d → Glacier IR → 3yr →
 * Deep Archive) applies regardless of WORM_Enabled.
 *
 * Per-object keys follow the single canonical shape
 * `{org-id}/{tenant-id}/{project-id}/{manifest-digest}/` ([WORM-002], design
 * D.1) — exactly one shape, all three logical-hierarchy segments mandatory,
 * `{manifest-digest}` the sole identity anchor. The bucket itself is
 * key-agnostic: that prefix is constructed at write time by the
 * `archive_to_s3` Lambda (the runtime owner of [WORM-002] key construction),
 * not here, so this construct provisions only the bucket and its retention /
 * encryption / access-logging posture.
 *
 * Access logs are written to a dedicated logging bucket whose objects expire
 * after `accessLogRetentionDays` (the deployment's retention period), so the
 * access record outlives every bundle it describes.
 *
 * The archive bucket has transitions only and no expiration rule of any kind
 * ([RETAIN-006]). S3 Bucket Keys stay OFF: the pruner's receipt-copy
 * `kms:Decrypt` grant is conditioned on the object-ARN encryption context,
 * which S3 uses only when Bucket Keys are disabled.
 */
export class Archive extends Construct {
  readonly bucket: Bucket;
  readonly accessLogBucket: Bucket;

  constructor(scope: Construct, id: string, props: ArchiveProps) {
    super(scope, id);

    const { naming, wormEnabled, isProd, kmsKey, accessLogRetentionDays } = props;

    // S3 bucket names are GLOBALLY unique. The frozen per-deployment appName
    // token (drawn from the unambiguous alphabet) supplies that uniqueness — two
    // deployments get distinct `{appName}-artifact-archive[-logs]-{qualifier}`
    // names with no account-id suffix needed.
    this.accessLogBucket = new Bucket(this, 'AccessLogBucket', {
      bucketName: naming.nameFor('artifact-archive-logs'),
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      // Access logs from S3 are written by the S3 log-delivery group — must
      // own the bucket and disable ACLs are incompatible. Keep ACLs enabled
      // by setting ObjectOwnership to BUCKET_OWNER_PREFERRED.
      objectOwnership: ObjectOwnership.BUCKET_OWNER_PREFERRED,
      lifecycleRules: [
        {
          expiration: Duration.days(accessLogRetentionDays),
        },
      ],
      removalPolicy: statefulRemovalPolicy(isProd),
      autoDeleteObjects: !isProd,
    });

    this.bucket = new Bucket(this, 'ArchiveBucket', {
      bucketName: naming.nameFor('artifact-archive'),
      versioned: true,
      objectLockEnabled: wormEnabled,
      encryption: BucketEncryption.KMS,
      encryptionKey: kmsKey,
      bucketKeyEnabled: false,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: this.accessLogBucket,
      serverAccessLogsPrefix: 'access/',
      removalPolicy: statefulRemovalPolicy(isProd),
      autoDeleteObjects: !isProd,
      // Receipt copies (under 128 KB) must never transition, so `cairn prune` can always read them.
      transitionDefaultMinimumObjectSize: TransitionDefaultMinimumObjectSize.ALL_STORAGE_CLASSES_128_K,
      lifecycleRules: [
        {
          id: 'standard-to-glacier-ir-then-deep-archive',
          enabled: true,
          transitions: [
            {
              storageClass: StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: Duration.days(90),
            },
            {
              storageClass: StorageClass.DEEP_ARCHIVE,
              transitionAfter: Duration.days(365 * 3),
            },
          ],
          noncurrentVersionTransitions: [
            {
              storageClass: StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: Duration.days(90),
            },
          ],
        },
      ],
    });
  }
}
