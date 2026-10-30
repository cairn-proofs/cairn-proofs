import { Duration } from 'aws-cdk-lib';
import { Platform, SigningProfile as L2SigningProfile } from 'aws-cdk-lib/aws-signer';
import { StringParameter } from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { Naming } from '../naming';

export interface SigningProps {
  readonly naming: Naming;
  /** Deployment stage (e.g. `alpha`) — used for the SSM parameter path. */
  readonly stage: string;
}

/**
 * AWS Signer signing profile + SSM parameter.
 *
 * Signing_Profile:
 * - Platform: `Notation-OCI-SHA384-ECDSA` — the AWS Signer platform that
 *   matches Notation's OCI signing protocol.
 * - Validity: 5 years.
 * - Permissions to invoke (`signer:SignPayload`) are granted to the
 *   sign-bundle Lambda's role in the merge pass.
 *
 * SSM parameter `/{appName}/{stage}/signing/timestamp-provider`:
 * - Initialized to `system-clock` (signing [SIGN-009]).
 *
 * The Notation trust policy is rendered INLINE by `artifact-store-stack.ts`
 * and published to SSM `/{appName}/{stage}/notation/trust-policy`; it pins
 * this profile's versioned ARN as a trusted identity at substitution time.
 *
 * Reviewer sign-off profile (`artifact-verifiability`, signing [SIGN-003]):
 * a SECOND profile `{appName}_artifact_reviewer_signoff_*`
 * mirrors the pipeline profile so an independent human Attesting Reviewer can
 * attach a Notation signature referrer that verifies from the canonical ECR
 * artifact alone. (The signer is the Attesting Reviewer — Quinn — explicitly
 * distinct from the read-only auditors who never sign.) It is admitted by the same
 * strict trust policy, pinned by its own version ARN. The IAM grant to sign
 * with it is a DISTINCT identity (see iam-roles.ts) and is never composed into
 * the read-only auditor role.
 */
export class Signing extends Construct {
  readonly signingProfile: L2SigningProfile;
  readonly reviewerSignoffProfile: L2SigningProfile;
  readonly timestampProviderParam: StringParameter;

  constructor(scope: Construct, id: string, props: SigningProps) {
    super(scope, id);

    const { naming, stage } = props;

    // Signing profile names are constrained to /^[a-zA-Z0-9_]{2,64}$/ —
    // underscores only, no hyphens. `signerNameFor` underscores the whole
    // `{appName}_{resource}_{qualifier}` form.
    const profileName = naming.signerNameFor('artifact_pipeline_signing');
    const reviewerProfileName = naming.signerNameFor('artifact_reviewer_signoff');

    this.signingProfile = new L2SigningProfile(this, 'ArtifactPipelineProfile', {
      platform: Platform.NOTATION_OCI_SHA384_ECDSA,
      signatureValidity: Duration.days(365 * 5),
      signingProfileName: profileName,
    });

    // Reviewer sign-off profile — mirrors the pipeline profile's platform and
    // validity so a reviewer sign-off is just another OCI signature referrer
    // (stock `notation sign` against this profile).
    this.reviewerSignoffProfile = new L2SigningProfile(this, 'ArtifactReviewerSignoffProfile', {
      platform: Platform.NOTATION_OCI_SHA384_ECDSA,
      signatureValidity: Duration.days(365 * 5),
      signingProfileName: reviewerProfileName,
    });

    this.timestampProviderParam = new StringParameter(this, 'TimestampProviderParam', {
      parameterName: naming.ssmPath(stage, 'signing/timestamp-provider'),
      stringValue: 'system-clock',
      description:
        'TimestampProvider for the signing pipeline: `system-clock`. ' +
        '`rfc3161` adoption would require a commercial TSA procurement first.',
    });
  }

  get signingProfileArn(): string {
    return this.signingProfile.signingProfileArn;
  }

  get signingProfileVersionArn(): string {
    return this.signingProfile.signingProfileVersionArn;
  }

  get reviewerSignoffProfileArn(): string {
    return this.reviewerSignoffProfile.signingProfileArn;
  }

  get reviewerSignoffProfileVersionArn(): string {
    return this.reviewerSignoffProfile.signingProfileVersionArn;
  }
}
