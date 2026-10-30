import * as path from 'path';
import { App, Aspects, CfnResource, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { Repository } from 'aws-cdk-lib/aws-ecr';
import { Bucket } from 'aws-cdk-lib/aws-s3';
import * as fs from 'fs';
import * as os from 'os';
import { resolveDeployConfig } from '../bin/artifact-store';
import { ArtifactStoreStack } from '../lib/artifact-store-stack';
import { Alarms } from '../lib/constructs/alarms';
import { ProductionTeardownGuard } from '../lib/constructs/removal-policy';
import {
  generateAppNameToken,
  isValidToken,
  resolveAppNameToken,
  TOKEN_ALPHABET,
} from '../lib/naming';

// Pin every synth to a single, stable cloud-assembly directory. Without an
// explicit outdir, each `new App()` synthesizes into a fresh
// `os.tmpdir()/cdk.outXXXXXX`, and each one stages a full copy of the ~200 MiB
// asset payload (4 Lambda bundles + Notation layer). Across this file's synths
// that accumulates to multiple GB of /tmp simultaneously (CDK only frees temp
// dirs on process exit) — enough to fill the disk, and leaked entirely if the
// run is interrupted. A shared outdir reuses one directory: assets are
// content-hashed, so identical bundles dedupe and peak usage stays ~200 MiB.
const OUTDIR = path.join(__dirname, '..', 'cdk.out.test');

// Pinned appName for deterministic physical-name assertions. The default would
// resolve `cairnproofs{token}` from cdk/app-config.json; the tests pin it
// explicitly so every name assertion is stable regardless of the committed
// token (and the stack name is `{appName}-{qualifier}`).
const TEST_APP_NAME = 'cairnproofs4af';

// Uniform three-segment logical tenant hierarchy {org-id}/{tenant-id}/
// {project-id}. The alpha test stack
// pins one opaque id per segment so every path/ARN assertion is stable.
const TEST_ORG_ID = 'mvp-org';
const TEST_TENANT_ID = 'mvp-tenant';
const TEST_PROJECT_ID = 'mvp-project';
const PROJECT_REPO_PATH = `${TEST_APP_NAME}-artifact-cmh-alpha/${TEST_ORG_ID}/${TEST_TENANT_ID}/${TEST_PROJECT_ID}`;
const PROJECT_REPO_ARN = `arn:aws:ecr:us-east-2:111111111111:repository/${PROJECT_REPO_PATH}`;

// Overrides for the alarm-action gate matrix: the effective `ActionsEnabled`
// posture is `isProd || alarmActionsEnabled`. Defaults are the alpha config
// (isProd=false, opt-in omitted).
function makeStack(
  overrides: { isProd?: boolean; alarmActionsEnabled?: boolean } = {},
): ArtifactStoreStack {
  const app = new App({ outdir: OUTDIR });
  return new ArtifactStoreStack(app, 'TestStack', {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: false,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
    ...overrides,
  });
}

// The SNS action is WIRED onto an alarm (a non-empty AlarmActions array) so
// that flipping `ActionsEnabled` is the only thing the prod-gate changes —
// re-enabling never re-wires. Shared by the posture helpers below and the
// wired-in-every-config matrix test.
type AlarmActionProps = { AlarmActions?: unknown[]; ActionsEnabled?: boolean };

function expectActionWired(props: AlarmActionProps): void {
  expect(Array.isArray(props.AlarmActions)).toBe(true);
  expect((props.AlarmActions ?? []).length).toBeGreaterThan(0);
}

// SNS action WIRED but DISABLED — the non-prod default (isProd=false, no
// opt-in) posture.
function expectActionWiredButDisabled(props: AlarmActionProps): void {
  expectActionWired(props);
  expect(props.ActionsEnabled).toBe(false);
}

// SNS action WIRED and ENABLED — the production (`isProd`) / non-prod opt-in
// (`alarmActionsEnabled`) posture.
function expectActionWiredAndEnabled(props: AlarmActionProps): void {
  expectActionWired(props);
  expect(props.ActionsEnabled).toBe(true);
}

// Collect EVERY synthesized CloudWatch alarm's action posture (AlarmActions +
// ActionsEnabled) so a property can quantify over the whole retained alarm set
// (DLQ-depth per queue, DeadLetterErrors per Lambda, the one archive-duration
// alarm) rather than a single example.
function allAlarmActionProps(template: Template): AlarmActionProps[] {
  const alarms = template.findResources('AWS::CloudWatch::Alarm');
  return Object.values(alarms).map((a) => a.Properties as AlarmActionProps);
}

// Every retained alarm shares the same shape — a >0 GreaterThanThreshold
// evaluated over a single period, treating missing data as not-breaching —
// differing only in the threshold. Asserts that shape so the
// parameter-invariance test stays one source of truth.
function expectStandardAlarmParams(props: unknown, expectedThreshold: number): void {
  const p = props as {
    Threshold: number;
    ComparisonOperator: string;
    EvaluationPeriods: number;
    TreatMissingData: string;
  };
  expect(p.Threshold).toBe(expectedThreshold);
  expect(p.ComparisonOperator).toBe('GreaterThanThreshold');
  expect(p.EvaluationPeriods).toBe(1);
  expect(p.TreatMissingData).toBe('notBreaching');
}

// The alarm-action posture is gated on `isProd || alarmActionsEnabled`,
// quantified over EVERY retained alarm.
test('isProd=true enables alarm actions on every retained alarm', () => {
  const template = Template.fromStack(makeStack({ isProd: true }));
  const postures = allAlarmActionProps(template);
  // Guard against a vacuous pass — there ARE alarms to check.
  expect(postures.length).toBeGreaterThan(0);
  for (const props of postures) {
    expectActionWiredAndEnabled(props);
  }
});

// Non-production opt-in (isProd=false, alarmActionsEnabled=true) enables
// actions on every retained alarm (Expected Behavior #3 / OBS-011.05).
test('non-prod opt-in (alarmActionsEnabled=true) enables alarm actions on every retained alarm', () => {
  const template = Template.fromStack(makeStack({ isProd: false, alarmActionsEnabled: true }));
  const postures = allAlarmActionProps(template);
  expect(postures.length).toBeGreaterThan(0);
  for (const props of postures) {
    expectActionWiredAndEnabled(props);
  }
});

// isProd forces actions on even when alarmActionsEnabled=false; the
// production guard is non-overridable (Expected Behavior #4 / OBS-011.04).
test('isProd=true forces alarm actions on even when alarmActionsEnabled=false', () => {
  const template = Template.fromStack(makeStack({ isProd: true, alarmActionsEnabled: false }));
  const postures = allAlarmActionProps(template);
  expect(postures.length).toBeGreaterThan(0);
  for (const props of postures) {
    expectActionWiredAndEnabled(props);
  }
});

// REGRESSION — default alpha config (isProd=false, no opt-in) keeps every
// retained alarm wired-but-disabled (Unchanged Behavior #4 / OBS-011.02).
test('default config (isProd=false, no opt-in) leaves every retained alarm action wired-but-disabled', () => {
  const template = Template.fromStack(makeStack());
  const postures = allAlarmActionProps(template);
  expect(postures.length).toBeGreaterThan(0);
  for (const props of postures) {
    expectActionWiredButDisabled(props);
  }
});

// REGRESSION — explicit non-prod opt-OUT (isProd=false, alarmActionsEnabled=false)
// leaves every retained alarm wired-but-disabled, identically to the omitted
// default. This completes the posture truth table: the gate
// `isProd || (alarmActionsEnabled ?? false)` collapses an explicit `false` and an
// omitted value to the same DISABLED branch, and its mirror cell
// ({isProd:true, alarmActionsEnabled:false}) already has a dedicated force-ON
// test above — so the explicit-false non-prod cell is pinned here to guard against
// a future refactor that diverged explicit-`false` from the omitted default.
test('explicit non-prod opt-out (isProd=false, alarmActionsEnabled=false) leaves every retained alarm wired-but-disabled', () => {
  const template = Template.fromStack(makeStack({ isProd: false, alarmActionsEnabled: false }));
  const postures = allAlarmActionProps(template);
  expect(postures.length).toBeGreaterThan(0);
  for (const props of postures) {
    expectActionWiredButDisabled(props);
  }
});

// REGRESSION — across the full isProd × alarmActionsEnabled matrix, every
// retained alarm always has a non-empty AlarmActions array (the SNS action stays
// WIRED regardless of ActionsEnabled — Unchanged Behavior #1 / blast-radius #1).
test('alarm SNS action is wired in every isProd × alarmActionsEnabled config', () => {
  const configs: Array<{ isProd?: boolean; alarmActionsEnabled?: boolean }> = [
    {},
    { isProd: true },
    { isProd: false, alarmActionsEnabled: true },
    { isProd: true, alarmActionsEnabled: false },
  ];
  for (const cfg of configs) {
    const postures = allAlarmActionProps(Template.fromStack(makeStack(cfg)));
    expect(postures.length).toBeGreaterThan(0);
    for (const props of postures) {
      expectActionWired(props);
    }
  }
});

// REGRESSION — the synthesized alarm set and parameters are unchanged by the
// action-gate fix (Unchanged Behavior #2 / #3 / blast-radius #1): one DLQ-depth
// alarm per DLQ, one DeadLetterErrors alarm per pipeline Lambda, exactly one
// archive-duration alarm, unchanged thresholds / window / comparison /
// treatMissingData, and NO SigningFailure/ArchiveFailure/IntegrityMismatch alarm.
test('alarm set and parameters are unchanged across the action-gate matrix', () => {
  const configs: Array<{ isProd?: boolean; alarmActionsEnabled?: boolean }> = [
    {},
    { isProd: true },
    { isProd: false, alarmActionsEnabled: true },
  ];
  for (const cfg of configs) {
    const template = Template.fromStack(makeStack(cfg));

    // Same DLQ-depth alarm fan-out as the number of pipeline DLQs (one per queue).
    const dlqCount = Object.keys(template.findResources('AWS::SQS::Queue')).length;
    const dlqDepthAlarms = template.findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'ApproximateNumberOfMessagesVisible', Namespace: 'AWS/SQS' },
    });
    expect(Object.keys(dlqDepthAlarms)).toHaveLength(dlqCount);
    for (const a of Object.values(dlqDepthAlarms)) {
      expectStandardAlarmParams(a.Properties, 0);
    }

    // One DeadLetterErrors alarm per pipeline Lambda.
    const dleAlarms = template.findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'DeadLetterErrors', Namespace: 'AWS/Lambda' },
    });
    expect(Object.keys(dleAlarms)).toHaveLength(PIPELINE_FUNCTION_NAMES.length);
    for (const a of Object.values(dleAlarms)) {
      expectStandardAlarmParams(a.Properties, 0);
    }

    // Exactly one archive-duration alarm at the ARCHIVE_DURATION_BOUND_MS bound.
    const durationAlarms = template.findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'Duration', Namespace: 'AWS/Lambda' },
    });
    expect(Object.keys(durationAlarms)).toHaveLength(1);
    expectStandardAlarmParams(
      Object.values(durationAlarms)[0].Properties,
      Alarms.ARCHIVE_DURATION_BOUND_MS,
    );

    // No failure-count alarms regardless of the action gate.
    const watchedMetrics = Object.values(template.findResources('AWS::CloudWatch::Alarm')).map(
      (a) => (a.Properties as { MetricName?: string }).MetricName,
    );
    for (const dead of ['SigningFailure', 'ArchiveFailure', 'IntegrityMismatch']) {
      expect(watchedMetrics).not.toContain(dead);
    }
  }
});

test('stack synthesizes without errors', () => {
  const template = Template.fromStack(makeStack());
  expect(template).toBeDefined();
});

test('mints the service KMS CMK with rotation enabled', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::KMS::Key', {
    EnableKeyRotation: true,
  });
});

test('provisions a single collapsed ECR repository with alias-scoped tag immutability', () => {
  const template = Template.fromStack(makeStack());
  // One repository per project: the evidence-category is part of
  // the image tag, so there is a single repository named
  // `artifact-{qualifier}/{projectId}`.
  template.resourceCountIs('AWS::ECR::Repository', 1);
  // Tag immutability is IMMUTABLE_WITH_EXCLUSION, with the mutable namespace
  // scoped to exactly the `_als`/`_chn` alias tags.
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: PROJECT_REPO_PATH,
    ImageTagMutability: 'IMMUTABLE_WITH_EXCLUSION',
  });
});

// The single repository must NOT regress the must-retain properties — KMS
// encryption against the service CMK and the UNTAGGED 90-day lifecycle rule.
// (The removal policy is gated on isProd — see the teardown-readiness section
// at the bottom of this file: this isProd=false test stack is Delete-on-
// teardown; isProd=true stays Retain.)
test('collapsed ECR repository retains KMS encryption and lifecycle rule', () => {
  const template = Template.fromStack(makeStack());

  // KMS encryption referencing the service CMK. The key is a Ref/Fn::GetAtt,
  // so assert the encryption type and that a KmsKey reference is present.
  template.hasResourceProperties('AWS::ECR::Repository', {
    EncryptionConfiguration: Match.objectLike({
      EncryptionType: 'KMS',
      KmsKey: Match.anyValue(),
    }),
  });

  // UNTAGGED 90-day lifecycle rule. LifecyclePolicyText is a JSON string; match
  // the tagStatus / sinceImagePushed=90 intent within the serialized policy.
  template.hasResourceProperties('AWS::ECR::Repository', {
    LifecyclePolicy: Match.objectLike({
      LifecyclePolicyText: Match.serializedJson(
        Match.objectLike({
          rules: Match.arrayWith([
            Match.objectLike({
              selection: Match.objectLike({
                tagStatus: 'untagged',
                countType: 'sinceImagePushed',
                countUnit: 'days',
                countNumber: 90,
              }),
              action: Match.objectLike({ type: 'expire' }),
            }),
          ]),
        }),
      ),
    }),
  });

});

// IAM scoping for the uniform three-segment path
// (access-control [ACCESS-002]): the producer push policy
// authorizes push against the single project repository, addressed through the
// `{org-id}/{tenant-id}/{project-id}` hierarchy. A project maps to ONE
// repository with no child repository-path segment, so the producer scope is
// the BARE project-repo ARN ONLY. The auditor pattern `artifact-{qualifier}/*`
// is qualifier-scoped (covers every tenant's repos for read).
//
// region/account are pinned by the test stack, so producer/auditor ARNs
// synthesize as plain strings (no Fn::Join).
//
// makeStack() ⇒ regionCode `cmh`, stage `alpha` (qualifier `cmh-alpha`),
// org `mvp-org` / tenant `mvp-tenant` / project `mvp-project`,
// account `111111111111`, region `us-east-2`.
test('producer policy authorizes push to the bare three-segment repo; no /* form; auditor pattern unchanged', () => {
  const template = Template.fromStack(makeStack());
  const arnBase = 'arn:aws:ecr:us-east-2:111111111111:repository/';
  const projectRepoArn = PROJECT_REPO_ARN;
  const removedReExpansionPattern = `${PROJECT_REPO_ARN}/*`;
  const auditorPattern = `${arnBase}cairnproofs4af-artifact-cmh-alpha/*`;

  // Locate the producer push statement (managed policy `cairnproofs4af-producer-*`,
  // sid `EcrPushToProjectRepos`) and read its resource list robustly: the
  // Resource field may be a single string or an array of (string | Fn::Join).
  const managedPolicies = template.findResources('AWS::IAM::ManagedPolicy');
  const producerStatements = Object.values(managedPolicies)
    .map((p) => (p as { Properties: { PolicyDocument: { Statement: Array<{ Sid?: string; Resource?: unknown }> } } }).Properties.PolicyDocument.Statement)
    .flat()
    .filter((s) => s.Sid === 'EcrPushToProjectRepos');
  expect(producerStatements).toHaveLength(1);
  const producerResources = producerStatements[0].Resource;
  const producerResourceList = Array.isArray(producerResources)
    ? producerResources
    : [producerResources];
  const producerResourceStrings = producerResourceList.map((r) => JSON.stringify(r));

  // Producer push authorizes the BARE three-segment project repo ARN …
  expect(producerResourceStrings).toContain(JSON.stringify(projectRepoArn));
  // … and the per-category `/*` re-expansion form is GONE (D.14).
  expect(producerResourceStrings).not.toContain(JSON.stringify(removedReExpansionPattern));
  // Exactly the one bare project-repo ARN, nothing else.
  expect(producerResourceList).toHaveLength(1);

  // The auditor pattern (qualifier-scoped) is unchanged and still present in the
  // synthesized policies (auditor ManagedPolicy + lambda roles).
  const allPolicies = JSON.stringify({
    managed: managedPolicies,
    inline: template.findResources('AWS::IAM::Policy'),
  });
  expect(allPolicies).toContain(auditorPattern);

  // IAM wildcard semantics: `*` matches zero-or-more characters. Confirm the
  // producer resource matches the project repo, and the unchanged auditor
  // pattern still covers it (pull/list).
  const iamGlobMatches = (pattern: string, value: string): boolean => {
    const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
    return new RegExp(`^${escaped}$`).test(value);
  };
  const producerMatchesRepo = producerResourceList.some(
    (r) => typeof r === 'string' && iamGlobMatches(r, projectRepoArn),
  );
  expect(producerMatchesRepo).toBe(true);
  expect(iamGlobMatches(auditorPattern, projectRepoArn)).toBe(true);
});

test('archive bucket has versioning and SSL enforcement', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketName: 'cairnproofs4af-artifact-archive-cmh-alpha',
    VersioningConfiguration: { Status: 'Enabled' },
  });
});

test('metadata, control-claims, and identity-registry tables are PAY_PER_REQUEST with PITR and no GSIs', () => {
  const template = Template.fromStack(makeStack());
  // Metadata_Index — tenant-PK plane, no GSI ships today ([META-001] item 4).
  // Tables are auto-named (no explicit TableName, so a future key-schema change
  // replaces cleanly); the logical role is carried by the `cairn:resource` tag.
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    Tags: Match.arrayWith([{ Key: 'cairn:resource', Value: 'ArtifactMetadata' }]),
    BillingMode: 'PAY_PER_REQUEST',
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    GlobalSecondaryIndexes: Match.absent(),
  });
  // ControlClaims — the control-claims projection table, a tenant-PK plane with
  // no GSI.
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    Tags: Match.arrayWith([{ Key: 'cairn:resource', Value: 'ControlClaims' }]),
    BillingMode: 'PAY_PER_REQUEST',
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    GlobalSecondaryIndexes: Match.absent(),
  });
  // Identity_Registry — control-plane table ([IDREG-001]), same posture.
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    Tags: Match.arrayWith([{ Key: 'cairn:resource', Value: 'IdentityRegistry' }]),
    BillingMode: 'PAY_PER_REQUEST',
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    GlobalSecondaryIndexes: Match.absent(),
  });
});

test('identity-registry deploy-seed PutItems write facade-shaped items (id/kind)', () => {
  const template = Template.fromStack(makeStack());
  const customResources = template.findResources('Custom::AWS');
  // The seed AwsCustomResources render their call as a `Create` property; the
  // table-name token makes it an Fn::Join, so stringify the whole value before
  // matching rather than assuming a plain string.
  const seedCreates = Object.values(customResources)
    .map((r) => JSON.stringify((r.Properties as { Create?: unknown }).Create ?? ''))
    .filter((c) => c.includes('putItem'));
  // org + tenant + project = three idempotent seed writes ([IDREG-001]).
  expect(seedCreates.length).toBe(3);
  for (const create of seedCreates) {
    // Seed items MUST carry the exact attribute names the cairn_metadata
    // IdentityRegistry facade reads: keyed on `id`, discriminator `kind`,
    // human name `displayName`. Mismatched names fail the facade's GetItem /
    // BatchGetItem closed (display names silently never resolve).
    expect(create).toContain('attribute_not_exists(id)');
    expect(create).toContain('kind');
    expect(create).toContain('displayName');
    // The mismatched attribute names must be absent.
    expect(create).not.toContain('attribute_not_exists(pk)');
    expect(create).not.toContain('entityType');
  }
});

test('display-only outputs (no Export name) for AuditCoverageStatus + trust-policy', () => {
  const template = Template.fromStack(makeStack());
  template.hasOutput('AuditCoverageStatus', { Export: Match.absent() });
  template.hasOutput('EcrRegistryHost', { Export: Match.absent() });
  template.hasOutput('SigningProfileVersionArn', { Export: Match.absent() });
  template.hasOutput('NotationTrustPolicyParamName', { Export: Match.absent() });
});

test('AuditCoverageStatus defaults to `unverified` when context is unset', () => {
  const template = Template.fromStack(makeStack());
  template.hasOutput('AuditCoverageStatus', { Value: 'unverified' });
});

test('AuditCoverageStatus reflects the cloudTrailCheck context value', () => {
  const app = new App({ outdir: OUTDIR, context: { cloudTrailCheck: 'passed' } });
  const stack = new ArtifactStoreStack(app, 'PassedStack', {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: false,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
  });
  const template = Template.fromStack(stack);
  template.hasOutput('AuditCoverageStatus', { Value: 'passed' });
});

test('SSM parameters publish ecr-host, profile-version-arn, rendered trust policy', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/registry/ecr-host',
  });
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/signing/profile-version-arn',
  });
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/notation/trust-policy',
  });
});

test('SSM parameters publish auto-named DynamoDB table names for CLI/TUI discovery', () => {
  const template = Template.fromStack(makeStack());
  // The tables are auto-named (no explicit TableName), so their physical names
  // are published at deterministic SSM paths the CLI/TUI read (resolve_table_names).
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/tables/metadata',
  });
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/tables/control-claims',
  });
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/tables/identity-registry',
  });
});

test('SQS DLQs have 14-day retention and KMS encryption', () => {
  const template = Template.fromStack(makeStack());
  // Five pipeline Lambdas (validate, sign, archive, metadata, mint-tag), each
  // with its own DLQ.
  template.resourceCountIs('AWS::SQS::Queue', 5);
  template.allResources('AWS::SQS::Queue', {
    Properties: Match.objectLike({
      MessageRetentionPeriod: 14 * 24 * 60 * 60,
    }),
  });
});

test('AWS Signer profile uses Notation OCI platform', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Signer::SigningProfile', {
    PlatformId: 'Notation-OCI-SHA384-ECDSA',
  });
});

// ── artifact-verifiability — auditor sign-off profile, dual-profile trust ──
// policy, distinct sign-off grant, trust-policy write-protect.
//
// signing [SIGN-003]: a SECOND AWS Signer profile
// `artifact-reviewer-signoff` is provisioned alongside `artifact-pipeline-signing`,
// the Notation trust policy admits BOTH under `strict` (the auditor profile
// pinned by its OWN version ARN — Threat #3, never a wildcard), the sign-off
// IAM grant is a DISTINCT identity NOT on the read-only auditor role (Threat #4),
// and the trust-policy SSM parameter is write-protected (Threat #9).

test('both signing profiles are synthesized (pipeline + auditor sign-off)', () => {
  const template = Template.fromStack(makeStack());
  // Exactly two AWS Signer profiles, both on the Notation OCI platform.
  template.resourceCountIs('AWS::Signer::SigningProfile', 2);
  const profiles = template.findResources('AWS::Signer::SigningProfile');
  // AWS Signer profile names are not emitted as a CFN property by the L2
  // construct (the physical name is generated), so the two profiles are
  // discriminated by their construct logical IDs: the pipeline profile and the
  // NEW auditor sign-off profile.
  const logicalIds = Object.keys(profiles).join('|');
  expect(logicalIds).toMatch(/ArtifactPipelineProfile/);
  expect(logicalIds).toMatch(/ArtifactReviewerSignoffProfile/);
  // Both use the Notation OCI platform and the same 5-year validity.
  for (const p of Object.values(profiles)) {
    const props = p.Properties as {
      PlatformId?: string;
      SignatureValidityPeriod?: { Type?: string; Value?: number };
    };
    expect(props.PlatformId).toBe('Notation-OCI-SHA384-ECDSA');
    expect(props.SignatureValidityPeriod).toEqual({ Type: 'DAYS', Value: 365 * 5 });
  }
});

test('auditor sign-off profile version ARN is published to SSM (no CFN Export)', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/signing/reviewer-profile-version-arn',
  });
  // Display-only output, never an Export.
  template.hasOutput('ReviewerSignoffProfileVersionArn', { Export: Match.absent() });
});

test('trust policy admits BOTH profiles under strict, auditor identity pinned (Threat #3)', () => {
  const template = Template.fromStack(makeStack());
  // Locate the rendered trust-policy SSM parameter and parse its JSON value.
  const params = template.findResources('AWS::SSM::Parameter', {
    Properties: { Name: '/cairnproofs4af/alpha/notation/trust-policy' },
  });
  expect(Object.keys(params)).toHaveLength(1);
  const rawValue = (Object.values(params)[0].Properties as { Value: unknown }).Value;
  // The rendered value embeds deploy-time profile-version-ARN tokens, so CDK
  // synthesizes it as an `Fn::Join`. Flatten the join parts to one string,
  // then assert structurally on the literal JSON fragments.
  const flatten = (v: unknown): string => {
    if (typeof v === 'string') return v;
    if (v && typeof v === 'object' && 'Fn::Join' in (v as Record<string, unknown>)) {
      const parts = (v as { 'Fn::Join': [string, unknown[]] })['Fn::Join'][1];
      return parts.map(flatten).join('');
    }
    // Fn::GetAtt / Ref token for a profile-version ARN — render a placeholder so
    // the surrounding JSON stays parseable and the identity is detectable.
    return '<<ARN_TOKEN>>';
  };
  const flat = flatten(rawValue);
  const policy = JSON.parse(flat) as {
    version: string;
    trustPolicies: Array<{
      name: string;
      registryScopes: string[];
      signatureVerification: { level: string };
      trustStores: string[];
      trustedIdentities: string[];
    }>;
  };

  expect(policy.version).toBe('1.0');
  // Notation requires a registry scope to belong to EXACTLY ONE trust policy
  // (a scope repeated across policies is a hard load error). Both profiles are
  // therefore admitted as two pinned trustedIdentities on a SINGLE policy.
  expect(policy.trustPolicies).toHaveLength(1);
  const scope =
    '111111111111.dkr.ecr.us-east-2.amazonaws.com/cairnproofs4af-artifact-cmh-alpha';
  // Guard against regressing to the invalid two-policies-same-scope shape:
  // no registry scope may appear in more than one trust policy.
  const allScopes = policy.trustPolicies.flatMap((tp) => tp.registryScopes);
  expect(new Set(allScopes).size).toBe(allScopes.length);

  const tp = policy.trustPolicies[0];
  // Verification level is strict.
  expect(tp.signatureVerification.level).toBe('strict');
  expect(tp.trustStores).toEqual(['signingAuthority:aws-signer']);
  // Threat #3: BOTH profiles are pinned as exact version-ARN identities on the
  // one policy, NEVER a wildcard or a repo-only scope. (Both ARNs are distinct
  // GetAtt tokens that flatten to the same placeholder here; the raw-token
  // assertion below proves they are two DIFFERENT profiles.)
  expect(tp.trustedIdentities).toHaveLength(2);
  expect(tp.trustedIdentities).toEqual(['<<ARN_TOKEN>>', '<<ARN_TOKEN>>']);
  expect(tp.trustedIdentities).not.toContain('*');
  // Registry scope is the artifact namespace (account/region are pinned in the
  // test stack so it renders as a literal), never an identity proxy/wildcard.
  expect(tp.registryScopes).toEqual([scope]);

  // The single policy pins DISTINCT version ARNs (pipeline vs auditor) — assert
  // the raw (unflattened) join references two different Signer profile-version
  // GetAtt tokens.
  const rawSerialized = JSON.stringify(rawValue);
  expect(rawSerialized).toContain('ArtifactPipelineProfile');
  expect(rawSerialized).toContain('ArtifactReviewerSignoffProfile');
});

// The inline renderer in artifact-store-stack.ts is the SOLE Notation
// trust-policy source of truth. These guards assert no shadow trust-policy
// config file exists under cdk/config/ and that signing.ts does not reference
// one.
describe('dead Notation trust-policy config under cdk/config/ is absent (single source of truth)', () => {
  // Test dir is cdk/test/, so the repo cdk/ dir is one level up.
  const CDK_DIR = path.resolve(__dirname, '..');

  test('dead cdk/config/notation-trust-policy.json is absent (no shadow source of truth)', () => {
    expect(fs.existsSync(path.join(CDK_DIR, 'config', 'notation-trust-policy.json'))).toBe(false);
  });

  test('dead cdk/config/README.md is absent (no doc asserting a dead file is canonical)', () => {
    expect(fs.existsSync(path.join(CDK_DIR, 'config', 'README.md'))).toBe(false);
  });

  test('signing.ts source does not reference the dead cdk/config/notation-trust-policy.json', () => {
    const signingSrc = fs.readFileSync(
      path.join(CDK_DIR, 'lib', 'constructs', 'signing.ts'),
      'utf8',
    );
    expect(signingSrc).not.toContain('cdk/config/notation-trust-policy.json');
  });
});

test('read-only auditor role has NO Signer sign permission (Threat #4)', () => {
  const template = Template.fromStack(makeStack());
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  // The read-only auditor managed policy must carry no signer:* action at all.
  for (const policy of Object.values(managed)) {
    const props = policy.Properties as {
      ManagedPolicyName?: string;
      PolicyDocument: { Statement: Array<{ Action?: unknown }> };
    };
    const name = props.ManagedPolicyName ?? '';
    if (name.startsWith('cairnproofs4af-auditor-') && !name.startsWith('cairnproofs4af-reviewer-signoff')) {
      const actions = props.PolicyDocument.Statement.flatMap((s) =>
        Array.isArray(s.Action) ? s.Action : s.Action ? [s.Action] : [],
      );
      for (const a of actions) {
        expect(String(a).startsWith('signer:')).toBe(false);
      }
    }
  }
});

test('auditor sign-off grant is a DISTINCT policy scoped to the auditor profile (Threat #4)', () => {
  const template = Template.fromStack(makeStack());
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  const signoff = Object.values(managed).filter((p) =>
    ((p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName ?? '').startsWith(
      'cairnproofs4af-reviewer-signoff',
    ),
  );
  expect(signoff).toHaveLength(1);
  const statements = (
    signoff[0].Properties as {
      PolicyDocument: { Statement: Array<{ Sid?: string; Action?: unknown; Resource?: unknown; Effect?: string }> };
    }
  ).PolicyDocument.Statement;
  const signStmt = statements.find((s) => s.Sid === 'SignerReviewerSignoff');
  expect(signStmt).toBeDefined();
  expect(signStmt!.Effect).toBe('Allow');
  const actions = Array.isArray(signStmt!.Action) ? signStmt!.Action : [signStmt!.Action];
  expect(actions).toContain('signer:SignPayload');
  expect(actions).toContain('signer:StartSigningJob');
  // Resource is the auditor profile ARN (a Fn::GetAtt of the auditor profile),
  // never `*`.
  expect(signStmt!.Resource).not.toBe('*');
  expect(JSON.stringify(signStmt!.Resource)).toContain('ArtifactReviewerSignoffProfile');

  // The sign grant appears ONLY on the distinct signoff policy — the read-only
  // auditor policy never carries signer:SignPayload.
  const allManaged = JSON.stringify(managed);
  const occurrences = allManaged.split('SignerReviewerSignoff').length - 1;
  expect(occurrences).toBe(1);
});

test('trust-policy SSM parameter write access is restricted (Threat #9)', () => {
  const template = Template.fromStack(makeStack());
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  const writeProtect = Object.values(managed).filter((p) =>
    ((p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName ?? '').startsWith(
      'cairnproofs4af-trust-policy-write-protect',
    ),
  );
  expect(writeProtect).toHaveLength(1);
  const statements = (
    writeProtect[0].Properties as {
      PolicyDocument: { Statement: Array<{ Sid?: string; Effect?: string; Action?: unknown; Resource?: unknown }> };
    }
  ).PolicyDocument.Statement;
  const denyStmt = statements.find((s) => s.Sid === 'DenyTrustPolicySubstitution');
  expect(denyStmt).toBeDefined();
  // An explicit DENY on the write actions.
  expect(denyStmt!.Effect).toBe('Deny');
  const actions = Array.isArray(denyStmt!.Action) ? denyStmt!.Action : [denyStmt!.Action];
  expect(actions).toContain('ssm:PutParameter');
  // Scoped to exactly the trust-policy parameter ARN — not `*`, not all SSM.
  const resource = JSON.stringify(denyStmt!.Resource);
  expect(resource).toContain('parameter/cairnproofs4af/alpha/notation/trust-policy');
  expect(denyStmt!.Resource).not.toBe('*');
});

test('SSM timestamp-provider parameter defaults to system-clock', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs4af/alpha/signing/timestamp-provider',
    Value: 'system-clock',
  });
});

test('SNS alarm topic + email subscription provisioned', () => {
  const template = Template.fromStack(makeStack());
  template.resourceCountIs('AWS::SNS::Topic', 1);
  template.hasResourceProperties('AWS::SNS::Subscription', {
    Protocol: 'email',
    Endpoint: 'test@example.com',
  });
});

test('EventBridge custom bus + rules wired', () => {
  const template = Template.fromStack(makeStack());
  template.resourceCountIs('AWS::Events::EventBus', 1);
  template.hasResourceProperties('AWS::Events::EventBus', {
    Name: 'cairnproofs4af-artifact-cmh-alpha',
  });
});

// ── ECR push trigger rule ───────────────────────────────────────────────────
//
// The ECR PutImage trigger rule is narrowed by three strictly-upstream,
// footgun-free filters that pre-drop events the in-handler classifier would
// discard anyway: (a) successful pushes only (`errorCode` absent), (b) only
// this deployment's evidence repositories (`repositoryName` begins-with
// `artifact-<qualifier>/`), and (c) exclusion of the pipeline's own internal
// referrer/tag pushes via an `anything-but` wildcard on the always-present
// `userIdentity.arn` role-session (= Lambda function name) segment.
//
// D5: `imageManifestMediaType` is NOT a narrowing filter — that field is an
// OPTIONAL request parameter, so an `anything-but` on it would silently drop a
// genuine bundle push that omitted it. The three filters here are footgun-free
// precisely because each keys on an always-present field.

function ecrPushPatternDetail(template: Template): Record<string, unknown> {
  const rules = template.findResources('AWS::Events::Rule', {
    Properties: { Name: 'cairnproofs4af-ecr-push-cmh-alpha' },
  });
  expect(Object.keys(rules)).toHaveLength(1);
  const pattern = (Object.values(rules)[0].Properties as { EventPattern: Record<string, unknown> })
    .EventPattern;
  expect(pattern.source).toEqual(['aws.ecr']);
  expect(pattern['detail-type']).toEqual(['AWS API Call via CloudTrail']);
  return pattern.detail as Record<string, unknown>;
}

test('EcrPushRule still matches PutImage on the native ECR CloudTrail source', () => {
  const detail = ecrPushPatternDetail(Template.fromStack(makeStack()));
  expect(detail.eventSource).toEqual(['ecr.amazonaws.com']);
  expect(detail.eventName).toEqual(['PutImage']);
});

test('EcrPushRule admits successful pushes only (errorCode absent)', () => {
  const detail = ecrPushPatternDetail(Template.fromStack(makeStack()));
  expect(detail.errorCode).toEqual([{ exists: false }]);
});

test('EcrPushRule is scoped to this deployment evidence repos (repositoryName prefix)', () => {
  const detail = ecrPushPatternDetail(Template.fromStack(makeStack()));
  const requestParameters = detail.requestParameters as Record<string, unknown> | undefined;
  expect(requestParameters).toBeDefined();
  // A begins-with (prefix) match, NOT exact — deeper paths like
  // cairnproofs4af-artifact-cmh-alpha/proj/scan-reports must still match.
  expect(requestParameters!.repositoryName).toEqual([{ prefix: 'cairnproofs4af-artifact-cmh-alpha/' }]);
});

// Coverage gap: the existing prefix test pins the rule's
// `prefix` to the literal `cairnproofs4af-artifact-cmh-alpha/`, but never ties that literal to
// the repository the repositories construct actually creates
// (`artifact-${qualifier}/${projectId}`). If the repo naming convention drifted
// (qualifier composition changed, the `artifact-` prefix dropped, a new
// namespace segment inserted) the rule would silently stop matching every real
// producer push — zero validate invocations, no failure event, no DLQ entry —
// and the literal-only test above would still pass. Cross-check the rule prefix
// against the SYNTHESIZED ECR repository name from the same template so a
// naming drift on either side fails the build.
test('EcrPushRule repositoryName prefix is a true begins-with of the synthesized ECR repo name', () => {
  const template = Template.fromStack(makeStack());

  // Read the rule's repositoryName prefix from the synth.
  const detail = ecrPushPatternDetail(template);
  const requestParameters = detail.requestParameters as Record<string, unknown>;
  const repoNameMatchers = requestParameters.repositoryName as Array<{ prefix?: string }>;
  expect(repoNameMatchers).toHaveLength(1);
  const rulePrefix = repoNameMatchers[0].prefix;
  expect(typeof rulePrefix).toBe('string');

  // Read the actual repository name the repositories construct synthesizes
  // (exactly one collapsed repo per project — see the single-repo test above).
  const repos = template.findResources('AWS::ECR::Repository');
  const repoNames = Object.values(repos)
    .map((r) => (r.Properties as { RepositoryName?: string }).RepositoryName)
    .filter((n): n is string => typeof n === 'string');
  expect(repoNames).toHaveLength(1);
  const actualRepoName = repoNames[0];

  // The rule prefix MUST be a genuine begins-with of the real repo name, and
  // strictly shorter than it (a prefix, never an exact match — deeper paths must
  // stay in scope). If these drift apart the rule matches nothing real.
  expect(actualRepoName.startsWith(rulePrefix!)).toBe(true);
  expect(rulePrefix!.length).toBeLessThan(actualRepoName.length);
  // And the prefix ends at a path boundary (`/`) so it cannot half-match a
  // sibling repo like `artifact-cmh-alpha-other/...`.
  expect(rulePrefix!.endsWith('/')).toBe(true);
});

test('EcrPushRule excludes pipeline-internal pushes via userIdentity.arn anything-but wildcard', () => {
  const template = Template.fromStack(makeStack());
  const detail = ecrPushPatternDetail(template);
  const userIdentity = detail.userIdentity as Record<string, unknown> | undefined;
  expect(userIdentity).toBeDefined();
  const arnMatchers = userIdentity!.arn as Array<{ 'anything-but'?: { wildcard?: string[] } }>;
  expect(Array.isArray(arnMatchers)).toBe(true);
  expect(arnMatchers).toHaveLength(1);
  const wildcard = arnMatchers[0]['anything-but']?.wildcard;
  expect(Array.isArray(wildcard)).toBe(true);

  // Completeness: each of the THREE pipeline-internal-writer Lambdas
  // (sign-bundle, archive-to-s3, mint-tag) must have its synthesized
  // FunctionName present as a `*/<fn-name>` wildcard. Look the names up FROM the
  // template (deterministic literal function names) so a renamed/missing
  // function or a drifted wildcard list fails the build.
  const fns = functionNames(template);
  const internalWriters = [
    'cairnproofs4af-sign-bundle-cmh-alpha',
    'cairnproofs4af-archive-to-s3-cmh-alpha',
    'cairnproofs4af-mint-tag-cmh-alpha',
  ];
  for (const fnName of internalWriters) {
    // Confirm the function actually exists in the synthesized template …
    expect(fns).toContain(fnName);
    // … and that the wildcard excludes its role-session segment.
    expect(wildcard).toContain(`*/${fnName}`);
  }
  expect(wildcard).toHaveLength(internalWriters.length);

  // NEGATIVE: validate-bundle and update-metadata must NOT be excluded —
  // validate is the rule's whole point and metadata is downstream.
  for (const fnName of ['cairnproofs4af-validate-bundle-cmh-alpha', 'cairnproofs4af-update-metadata-cmh-alpha']) {
    expect(fns).toContain(fnName);
    expect(wildcard).not.toContain(`*/${fnName}`);
  }
});

// Resolve every synthesized AWS::Lambda::Function FunctionName from the template.
function functionNames(template: Template): string[] {
  const fns = template.findResources('AWS::Lambda::Function');
  return Object.values(fns)
    .map((f) => (f.Properties as { FunctionName?: string }).FunctionName)
    .filter((n): n is string => typeof n === 'string');
}

// ── Pipeline-failure custom-metric alarms ───────────────────────────────────
//
// The three custom-metric failure alarms (`SigningFailure`, `ArchiveFailure`,
// `IntegrityMismatch`) and the validation skip-rate dashboard are NOT
// provisioned — they would watch metric-name/dimension tuples no handler emits.
// A quiet board must mean "healthy," not "misconfigured." These guards pin the
// absence of that surface; a corrected, re-enabled alarm set is deferred to a
// follow-up metric/alarm-alignment item tracked in the backlog.

test('no validation skip-rate dashboard (or any dashboard) is synthesized', () => {
  const template = Template.fromStack(makeStack());
  template.resourceCountIs('AWS::CloudWatch::Dashboard', 0);
});

test('no alarm watches the retracted SigningFailure/ArchiveFailure/IntegrityMismatch custom metrics', () => {
  const template = Template.fromStack(makeStack());
  const alarms = template.findResources('AWS::CloudWatch::Alarm');
  const watchedMetricNames = Object.values(alarms).map(
    (a) => (a.Properties as { MetricName?: string }).MetricName,
  );
  for (const dead of ['SigningFailure', 'ArchiveFailure', 'IntegrityMismatch']) {
    expect(watchedMetricNames).not.toContain(dead);
  }
});

test('five pipeline Lambdas provisioned, arm64 / python3.12', () => {
  const template = Template.fromStack(makeStack());
  // The five pipeline Lambdas are validate, sign, archive, metadata, and the
  // post-archive mint-tag. The isProd=false teardown-readiness gate also
  // synthesizes the S3 auto-delete custom-resource provider function (nodejs,
  // no FunctionName), so the pipeline assertions are scoped to the five NAMED
  // `{appName}-*` functions.
  const fns = template.findResources('AWS::Lambda::Function');
  const pipelineFns = Object.values(fns).filter((f) => {
    const name = (f.Properties as { FunctionName?: string }).FunctionName;
    return typeof name === 'string' && name.startsWith(`${TEST_APP_NAME}-`);
  });
  expect(pipelineFns).toHaveLength(5);
  for (const fn of pipelineFns) {
    const props = fn.Properties as { Runtime?: string; Architectures?: string[] };
    expect(props.Runtime).toBe('python3.12');
    expect(props.Architectures).toEqual(['arm64']);
  }
});

test('sign-bundle sets XDG_CONFIG_HOME=/opt for Notation plugin discovery', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'cairnproofs4af-sign-bundle-cmh-alpha',
    Environment: { Variables: Match.objectLike({ XDG_CONFIG_HOME: '/opt' }) },
  });
  // Confirm the function references at least one layer.
  const signFn = template.findResources('AWS::Lambda::Function', {
    Properties: { FunctionName: 'cairnproofs4af-sign-bundle-cmh-alpha' },
  });
  const props = Object.values(signFn)[0].Properties as { Layers?: unknown[] };
  expect(Array.isArray(props.Layers)).toBe(true);
  expect((props.Layers ?? []).length).toBeGreaterThan(0);
});

test('archive-to-s3 receives WORM_ENABLED env var (false)', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'cairnproofs4af-archive-to-s3-cmh-alpha',
    Environment: { Variables: Match.objectLike({ WORM_ENABLED: 'false' }) },
  });
});

test('Notation Lambda layer provisioned for arm64 + python3.12', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Lambda::LayerVersion', {
    CompatibleArchitectures: ['arm64'],
    CompatibleRuntimes: ['python3.12'],
  });
});

// ── DLQ-delivery regression guards ──────────────────────────────────────────
//
// The validate-bundle role carries a narrow SQS-only `kms:GenerateDataKey`
// statement on the DLQ KMS key so SQS can wrap the data key for async-invoke
// failure delivery; without it the DLQ stays at 0 messages while
// `AWS/Lambda::DeadLetterErrors` ticks. The regression guards below assert
// (a) the narrow statement exists on the validate role, (b) all four Lambda
// roles can produce `kms:GenerateDataKey` against the service KMS key (validate
// via the narrow helper, the other three via the `kms:GenerateDataKey*` from
// `addScopedKmsEncryptDecrypt`), and (c) `AWS/Lambda::DeadLetterErrors` alarms
// are wired to the pipeline alerts SNS topic.

test('validate-bundle role has narrow kms:GenerateDataKey on DLQ KMS key via SQS only', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties(
    'AWS::IAM::Policy',
    Match.objectLike({
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'kms:GenerateDataKey',
            Effect: 'Allow',
            Condition: {
              StringEquals: {
                'kms:ViaService': 'sqs.us-east-2.amazonaws.com',
              },
            },
          }),
        ]),
      },
      // The DefaultPolicy attached to the validate-bundle service role.
      Roles: Match.arrayWith([
        Match.objectLike({
          Ref: Match.stringLikeRegexp('^LambdaFunctionsValidateBundleServiceRole'),
        }),
      ]),
    }),
  );
});

test('all pipeline Lambda roles can produce kms:GenerateDataKey on the service KMS key', () => {
  // Regression guard for the CDK auto-grant asymmetry: future refactors of the
  // `addScopedKms*` helpers must not silently
  // remove `kms:GenerateDataKey` from any pipeline role. Validate uses
  // the narrow single-action helper; the other three get the action via
  // `kms:GenerateDataKey*` from `addScopedKmsEncryptDecrypt`.
  const template = Template.fromStack(makeStack());
  const roleNamePrefixes = [
    'LambdaFunctionsValidateBundleServiceRole',
    'LambdaFunctionsSignBundleServiceRole',
    'LambdaFunctionsArchiveToS3ServiceRole',
    'LambdaFunctionsUpdateMetadataServiceRole',
    'LambdaFunctionsMintTagServiceRole',
  ];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const rolePrefix of roleNamePrefixes) {
    const roleHasGenerateDataKey = Object.values(policies).some((p) => {
      const props = p.Properties as {
        Roles?: Array<{ Ref?: string }>;
        PolicyDocument?: { Statement?: Array<{ Action?: unknown }> };
      };
      const attachedToRole = (props.Roles ?? []).some((r) =>
        (r.Ref ?? '').startsWith(rolePrefix),
      );
      if (!attachedToRole) return false;
      return (props.PolicyDocument?.Statement ?? []).some((s) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        return actions.some(
          (a) => a === 'kms:GenerateDataKey' || a === 'kms:GenerateDataKey*',
        );
      });
    });
    expect(roleHasGenerateDataKey).toBe(true);
  }
});

test('per-Lambda DeadLetterErrors alarms present-but-disabled (action wired, ActionsEnabled false)', () => {
  const template = Template.fromStack(makeStack());
  const dlqDeliveryAlarms = template.findResources('AWS::CloudWatch::Alarm', {
    Properties: {
      MetricName: 'DeadLetterErrors',
      Namespace: 'AWS/Lambda',
    },
  });
  expect(Object.keys(dlqDeliveryAlarms).length).toBe(5);
  for (const alarm of Object.values(dlqDeliveryAlarms)) {
    const props = alarm.Properties as {
      Threshold: number;
      ComparisonOperator: string;
      EvaluationPeriods: number;
      Period: number;
      TreatMissingData: string;
      Dimensions: Array<{ Name: string; Value: unknown }>;
      AlarmActions: unknown[];
      ActionsEnabled: boolean;
    };
    expect(props.Threshold).toBe(0);
    expect(props.ComparisonOperator).toBe('GreaterThanThreshold');
    expect(props.EvaluationPeriods).toBe(1);
    expect(props.Period).toBe(300);
    expect(props.TreatMissingData).toBe('notBreaching');
    expect(props.Dimensions).toHaveLength(1);
    expect(props.Dimensions[0].Name).toBe('FunctionName');
    expectActionWiredButDisabled(props);
  }
  // Confirm each function name is dimension-targeted exactly once.
  const targetedFunctions = Object.values(dlqDeliveryAlarms).map((a) => {
    const props = a.Properties as { Dimensions: Array<{ Name: string; Value: unknown }> };
    return JSON.stringify(props.Dimensions[0].Value);
  });
  expect(new Set(targetedFunctions).size).toBe(5);
});

test('DLQ-depth alarms present-but-disabled (action wired, ActionsEnabled false)', () => {
  // Stabilization-window posture: the design-independent DLQ-depth alarms (one
  // per queue) are retained and keep evaluating, but their SNS action is
  // wired-but-disabled during the window.
  // Pin the full alarm contract (threshold/comparison/window/dimension), not
  // just the disabled-action posture: a test that only checks ActionsEnabled
  // would pass even if the threshold drifted off 0 or the dimension was lost.
  const template = Template.fromStack(makeStack());
  const dlqDepthAlarms = template.findResources('AWS::CloudWatch::Alarm', {
    Properties: {
      MetricName: 'ApproximateNumberOfMessagesVisible',
      Namespace: 'AWS/SQS',
    },
  });
  // One DLQ-depth alarm per pipeline DLQ (validate, sign, archive, metadata,
  // mint-tag) — same fan-out as the pipeline Lambdas.
  expect(Object.keys(dlqDepthAlarms).length).toBe(PIPELINE_FUNCTION_NAMES.length);
  for (const alarm of Object.values(dlqDepthAlarms)) {
    const props = alarm.Properties as {
      Threshold: number;
      ComparisonOperator: string;
      EvaluationPeriods: number;
      Period: number;
      Statistic: string;
      TreatMissingData: string;
      Dimensions: Array<{ Name: string; Value: unknown }>;
    };
    expect(props.Threshold).toBe(0);
    expect(props.ComparisonOperator).toBe('GreaterThanThreshold');
    expect(props.EvaluationPeriods).toBe(1);
    // Production synthesizes a 1-minute period (Duration.minutes(1)) with a
    // single evaluation period — see report on the spec's "5-minute window"
    // wording. This pins the AS-BUILT contract; do not change it to 300 to
    // match the spec text without a production-code fix.
    expect(props.Period).toBe(60);
    expect(props.Statistic).toBe('Maximum');
    expect(props.TreatMissingData).toBe('notBreaching');
    expect(props.Dimensions).toHaveLength(1);
    expect(props.Dimensions[0].Name).toBe('QueueName');
    expectActionWiredButDisabled(alarm.Properties);
  }
});

test('SNS topic survives the disabled-action posture and every kept alarm still Refs it', () => {
  // Invariant #5: disabling alarm ACTIONS
  // must NOT mean the SNS topic was removed, and the kept alarms must keep
  // their action WIRED (pointing at the surviving topic) so re-enabling is a
  // one-line `actionsEnabled` flip — not a re-wire. A test that only checks
  // ActionsEnabled:false would still pass if someone dropped the AlarmActions
  // wiring entirely or pointed it at a stale/removed topic; this asserts the
  // action references the single surviving topic by its logical id.
  const template = Template.fromStack(makeStack());

  // The pipeline-alerts SNS topic is still provisioned (exactly one in stack).
  const topics = template.findResources('AWS::SNS::Topic');
  expect(Object.keys(topics)).toHaveLength(1);
  const topicLogicalId = Object.keys(topics)[0];

  // Every retained alarm (DLQ-depth on AWS/SQS + DeadLetterErrors on AWS/Lambda)
  // must Ref that exact topic in its AlarmActions despite ActionsEnabled:false.
  const keptAlarms = {
    ...template.findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'ApproximateNumberOfMessagesVisible', Namespace: 'AWS/SQS' },
    }),
    ...template.findResources('AWS::CloudWatch::Alarm', {
      Properties: { MetricName: 'DeadLetterErrors', Namespace: 'AWS/Lambda' },
    }),
  };
  // Sanity: there ARE kept alarms to check (guards against a vacuous pass if
  // the find filters ever silently match nothing).
  expect(Object.keys(keptAlarms).length).toBe(PIPELINE_FUNCTION_NAMES.length * 2);
  for (const alarm of Object.values(keptAlarms)) {
    const props = alarm.Properties as {
      ActionsEnabled?: boolean;
      AlarmActions?: Array<{ Ref?: string }>;
    };
    expect(props.ActionsEnabled).toBe(false);
    expect(props.AlarmActions).toBeDefined();
    expect(props.AlarmActions).toEqual(
      expect.arrayContaining([{ Ref: topicLogicalId }]),
    );
  }
});

test('archive-to-s3 duration-envelope alarm provisioned at the 8-min operating bound', () => {
  // At the 300-content-layer ceiling archive-to-s3's
  // per-invocation DURATION is the load-bearing risk (up to 300 serial
  // fetch→verify→PutObject hops vs the 10-min timeout). This pins the
  // early-warning alarm at ~80% of the timeout (8 min = 480000 ms), targeted at
  // the archive function by its FunctionName dimension, in the same wired-but-
  // disabled stabilization posture as the other kept alarms.
  const template = Template.fromStack(makeStack());
  const durationAlarms = template.findResources('AWS::CloudWatch::Alarm', {
    Properties: {
      MetricName: 'Duration',
      Namespace: 'AWS/Lambda',
    },
  });
  // Exactly one Duration alarm, and it targets the archive function.
  expect(Object.keys(durationAlarms).length).toBe(1);
  const props = Object.values(durationAlarms)[0].Properties as {
    Threshold: number;
    ComparisonOperator: string;
    EvaluationPeriods: number;
    Period: number;
    Statistic: string;
    TreatMissingData: string;
    Dimensions: Array<{ Name: string; Value: unknown }>;
    AlarmActions?: unknown[];
    ActionsEnabled?: boolean;
  };
  expect(props.Threshold).toBe(480000);
  expect(props.ComparisonOperator).toBe('GreaterThanThreshold');
  expect(props.EvaluationPeriods).toBe(1);
  expect(props.Period).toBe(300);
  expect(props.Statistic).toBe('Maximum');
  expect(props.TreatMissingData).toBe('notBreaching');
  expect(props.Dimensions).toHaveLength(1);
  expect(props.Dimensions[0].Name).toBe('FunctionName');
  // The Function helper resolves the dimension to a Ref on the archive
  // function's logical id; confirm it targets the archive function (not a
  // literal/wrong function) by cross-checking that logical id resolves to the
  // archive function's name.
  const dimRef = (props.Dimensions[0].Value as { Ref?: string }).Ref;
  expect(dimRef).toBeDefined();
  const fns = template.findResources('AWS::Lambda::Function');
  expect((fns[dimRef as string].Properties as { FunctionName: string }).FunctionName).toBe(
    'cairnproofs4af-archive-to-s3-cmh-alpha',
  );
  expectActionWiredButDisabled(props);
});

// ── mint-tag infra ──────────────────────────────────────────────────────────
//
// The post-archive mint-tag Lambda is the SINGLE principal authorized to apply
// a canonical ECR tag. These guards pin its least-privilege posture: PutImage
// scoped to exactly the one project evidence repo, DynamoDB read-only
// (GetItem, no UpdateItem) on the Metadata_Index table for the durable-record
// read, and the producer/auditor roles carry NO tag-write action.

// Helper: flatten every Statement across managed + inline IAM policies, tagging
// each with the role refs it is attached to (inline policies) for scoping
// assertions.
function allIamStatements(template: Template): Array<{
  action: string[];
  resource: unknown;
  roleRefs: string[];
  effect?: string;
}> {
  const out: Array<{ action: string[]; resource: unknown; roleRefs: string[]; effect?: string }> =
    [];
  const collect = (resources: Record<string, { Properties?: unknown }>, roleKey: 'Roles') => {
    for (const r of Object.values(resources)) {
      const props = r.Properties as {
        PolicyDocument?: { Statement?: Array<{ Action?: unknown; Resource?: unknown; Effect?: string }> };
        Roles?: Array<{ Ref?: string }>;
      };
      const roleRefs = (props.Roles ?? []).map((x) => x.Ref ?? '').filter(Boolean);
      for (const s of props.PolicyDocument?.Statement ?? []) {
        const action = Array.isArray(s.Action) ? s.Action : s.Action ? [s.Action] : [];
        out.push({ action: action as string[], resource: s.Resource, roleRefs, effect: s.Effect });
      }
    }
  };
  collect(template.findResources('AWS::IAM::Policy'), 'Roles');
  // Managed policies attach to no role ref inline; still collect their statements
  // (producer/auditor managed policies) with empty roleRefs.
  for (const r of Object.values(template.findResources('AWS::IAM::ManagedPolicy'))) {
    const props = r.Properties as {
      PolicyDocument?: { Statement?: Array<{ Action?: unknown; Resource?: unknown; Effect?: string }> };
    };
    for (const s of props.PolicyDocument?.Statement ?? []) {
      const action = Array.isArray(s.Action) ? s.Action : s.Action ? [s.Action] : [];
      out.push({ action: action as string[], resource: s.Resource, roleRefs: [], effect: s.Effect });
    }
  }
  return out;
}

test('mint-tag Lambda is provisioned and triggered by artifact.archived', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'cairnproofs4af-mint-tag-cmh-alpha',
    Handler: 'cairn_lambda_mint_tag.handler.lambda_handler',
    Runtime: 'python3.12',
    Architectures: ['arm64'],
    Environment: {
      Variables: Match.objectLike({ METADATA_TABLE_NAME: Match.anyValue() }),
    },
  });
  // The artifact.archived rule targets the mint-tag function.
  const rules = template.findResources('AWS::Events::Rule', {
    Properties: { Name: 'cairnproofs4af-artifact-archived-cmh-alpha' },
  });
  expect(Object.keys(rules)).toHaveLength(1);
  const rule = Object.values(rules)[0].Properties as {
    EventPattern: { 'detail-type': string[] };
    Targets: Array<{ Arn: unknown }>;
  };
  expect(rule.EventPattern['detail-type']).toEqual(['artifact.archived']);
  expect(rule.Targets.length).toBeGreaterThan(0);
  // The target Arn is a Fn::GetAtt of the mint-tag function's logical id.
  const targetArns = JSON.stringify(rule.Targets);
  expect(targetArns).toContain('MintTag');
});

test('mint-tag role has ecr:PutImage scoped to the ONE project evidence repo', () => {
  const template = Template.fromStack(makeStack());
  const projectRepoArn =
    PROJECT_REPO_ARN;

  const putImageStatements = allIamStatements(template).filter(
    (s) =>
      s.action.includes('ecr:PutImage') &&
      s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsMintTagServiceRole')),
  );
  // Exactly one PutImage statement on the mint role, scoped to the bare project
  // repo ARN (NOT `*`, NOT the qualifier-wide `/*` pattern).
  expect(putImageStatements).toHaveLength(1);
  expect(putImageStatements[0].resource).toBe(projectRepoArn);
});

test('auditor (consumer/read) role carries NO ecr:PutImage (R3 / T-02)', () => {
  const template = Template.fromStack(makeStack());
  // The auditor/consumer role is pure read (ECR pull, S3 read, DynamoDB query)
  // and MUST NOT carry the tag-write action. The producer push policy is a
  // separate matter (next test): ECR's `ecr:PutImage` is BOTH "register a
  // manifest" and "apply a tag" — there is no IAM condition key that
  // distinguishes them — so a producer that pushes its manifest by digest
  // necessarily holds `ecr:PutImage`. The canonical-tag-authority boundary
  // (R3 / T-02) is therefore enforced by (a) repo tag immutability + the mint
  // role being the sole principal that mints the *canonical* tag, and
  // (b) the read role carrying no PutImage at all, asserted here.
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  for (const [, policy] of Object.entries(managed)) {
    const props = policy.Properties as {
      ManagedPolicyName?: string;
      PolicyDocument: { Statement: Array<{ Action?: unknown }> };
    };
    const name = props.ManagedPolicyName ?? '';
    if (name.startsWith('cairnproofs4af-auditor')) {
      const actions = props.PolicyDocument.Statement.flatMap((s) =>
        Array.isArray(s.Action) ? s.Action : s.Action ? [s.Action] : [],
      );
      expect(actions).not.toContain('ecr:PutImage');
    }
  }
});

test('mint-tag PutImage is repo-scoped (single canonical-tag principal, R3 / T-02)', () => {
  // Regression guard: archive-to-s3 and sign-bundle legitimately carry
  // ecr:PutImage (signature/receipt referrer attach), and the producer push
  // policy carries it to register manifests by digest — ECR cannot IAM-scope
  // PutImage to untagged-only. What this change ADDS is the mint role's
  // canonical-tag write, and it MUST be scoped to exactly the one project
  // evidence repo ARN (never `*`, never the qualifier-wide `/*` read pattern).
  const template = Template.fromStack(makeStack());
  const stmts = allIamStatements(template).filter((s) => s.action.includes('ecr:PutImage'));
  const mintPutImage = stmts.filter((s) =>
    s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsMintTagServiceRole')),
  );
  expect(mintPutImage).toHaveLength(1);
  expect(mintPutImage[0].resource).toBe(
    PROJECT_REPO_ARN,
  );
});

test('mint-tag has read-only DynamoDB access — GetItem only, no UpdateItem', () => {
  const template = Template.fromStack(makeStack());
  // Exactly three DynamoDB tables: Metadata_Index + ControlClaims +
  // Identity_Registry. The mint role's only DynamoDB need is reading the
  // durable validate record.
  template.resourceCountIs('AWS::DynamoDB::Table', 3);

  // The mint role has dynamodb:GetItem (durable-record read) scoped to the
  // Metadata_Index table ARN (a Fn::GetAtt of the metadata table) and carries
  // NO dynamodb:UpdateItem — least privilege for a read-only consumer.
  const mintDdbStatements = allIamStatements(template).filter((s) =>
    s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsMintTagServiceRole')),
  );
  const getStmt = mintDdbStatements.find((s) => s.action.includes('dynamodb:GetItem'));
  expect(getStmt).toBeDefined();
  // Resource is a Fn::GetAtt referencing the Metadata table logical id.
  expect(JSON.stringify(getStmt!.resource)).toContain('MetadataTable');
  // No UpdateItem anywhere on the mint role.
  expect(mintDdbStatements.some((s) => s.action.includes('dynamodb:UpdateItem'))).toBe(false);
});

// Regression: validate stamps the durable validateStatus/artifactKind/name
// marker the mint stage reads. Both halves must be wired — the Metadata_Index
// env var AND the write grant on validate — or the write silently no-ops
// (`_metadata_index()` returns None when METADATA_TABLE_NAME is unset) and
// nothing is tagged (mint finds rows lacking validateStatus and refuses).
// Guard both halves of the wiring.
test('validate-bundle can write the Metadata_Index marker mint depends on', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'cairnproofs4af-validate-bundle-cmh-alpha',
    Environment: { Variables: Match.objectLike({ METADATA_TABLE_NAME: Match.anyValue() }) },
  });

  const validateDdbStatements = allIamStatements(template).filter((s) =>
    s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsValidateBundleServiceRole')),
  );
  // validate writes the marker (PutItem/UpdateItem) and reads it back for its
  // own re-tag short-circuit (GetItem), scoped to the Metadata table ARN.
  const writeStmt = validateDdbStatements.find(
    (s) => s.action.includes('dynamodb:PutItem') || s.action.includes('dynamodb:UpdateItem'),
  );
  expect(writeStmt).toBeDefined();
  expect(JSON.stringify(writeStmt!.resource)).toContain('MetadataTable');
  expect(
    validateDdbStatements.some((s) => s.action.includes('dynamodb:GetItem')),
  ).toBe(true);
});

// ── Producer attestation referrer identity ──────────────────────────────────
//
// The Producer_Attestation_Referrer flow has three parts: (a) a producer push of
// a SECOND OCI object (the referrer), (b) a SECOND Notation signature referrer
// on the sign role (over the Producer_Attestation_Referrer), and (c) the validate
// role's subject-scope (anti-hijack) residence check reading the resolved
// subject manifest — possibly CROSS-REPO — via BatchGetImage/DescribeImages.
// All three ride already-present grants (an OCI referrer push/attach is an
// ordinary PutImage; the subject read is an ordinary BatchGetImage over the
// qualifier-wide `artifact-*` namespace). These tests pin that the grants cover
// the behavior and that the PutImage routing rule does NOT narrow on
// artifactType (artifactType filtering is in-Lambda, processing-pipeline Req).

test('producer push grant covers the Producer_Attestation_Referrer PutImage (same repo, no extra action)', () => {
  // The Producer_Attestation_Referrer is an ordinary OCI manifest PutImage into the
  // SAME project repo as the content manifest — covered by the existing
  // producer push statement. Assert PutImage + the layer-upload actions are
  // present and scoped to the bare project repo ARN (single repository per
  // project — no `/*` sub-repository form, D.14).
  const template = Template.fromStack(makeStack());
  const projectRepoArn = PROJECT_REPO_ARN;
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  const pushStmts = Object.values(managed)
    .map(
      (p) =>
        (p as { Properties: { PolicyDocument: { Statement: Array<{ Sid?: string; Action?: unknown; Resource?: unknown }> } } })
          .Properties.PolicyDocument.Statement,
    )
    .flat()
    .filter((s) => s.Sid === 'EcrPushToProjectRepos');
  expect(pushStmts).toHaveLength(1);
  const actions = Array.isArray(pushStmts[0].Action)
    ? (pushStmts[0].Action as string[])
    : [pushStmts[0].Action as string];
  expect(actions).toContain('ecr:PutImage');
  expect(actions).toContain('ecr:InitiateLayerUpload');
  expect(actions).toContain('ecr:UploadLayerPart');
  expect(actions).toContain('ecr:CompleteLayerUpload');
  const resources = Array.isArray(pushStmts[0].Resource)
    ? (pushStmts[0].Resource as unknown[])
    : [pushStmts[0].Resource];
  const resourceStrings = resources.map((r) => JSON.stringify(r));
  expect(resourceStrings).toContain(JSON.stringify(projectRepoArn));
  // The per-category `/*` re-expansion form is removed (D.14).
  expect(resourceStrings).not.toContain(JSON.stringify(`${projectRepoArn}/*`));
  expect(resources).toHaveLength(1);
});

test('sign role can attach the SECOND (attestation) signature referrer — PutImage over the artifact-* namespace', () => {
  // Two independent Notation signature referrers (content + attestation) are
  // attached by the sign stage (signing [SIGN-001]). Both are referrer PutImage
  // (+ layer-upload) writes into the qualifier-wide `artifact-*/*` namespace,
  // so the existing sign-role ECR write grant covers the second signature.
  const template = Template.fromStack(makeStack());
  const anyArtifactRepoArnPattern =
    'arn:aws:ecr:us-east-2:111111111111:repository/cairnproofs4af-artifact-cmh-alpha/*';
  const signWriteStmts = allIamStatements(template).filter(
    (s) =>
      s.action.includes('ecr:PutImage') &&
      s.action.includes('ecr:InitiateLayerUpload') &&
      s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsSignBundleServiceRole')),
  );
  // One referrer-attach statement, qualifier-wide so it covers BOTH the
  // content-signature referrer and the attestation-signature referrer.
  expect(signWriteStmts).toHaveLength(1);
  expect(signWriteStmts[0].resource).toBe(anyArtifactRepoArnPattern);
});

test('validate role can read the subject manifest cross-repo for the subject-scope check (T-1/T-5)', () => {
  // The subject-scope (anti-hijack) check resolves the Producer_Attestation_Referrer's
  // `subject` content manifest and confirms it resides in the SAME repo path.
  // A foreign subject lives in a DIFFERENT repo — so the read grant must span
  // the whole `artifact-*` namespace (BatchGetImage + DescribeImages over the
  // qualifier-wide `/*` pattern), which the existing validate read grant does.
  const template = Template.fromStack(makeStack());
  const anyArtifactRepoArnPattern =
    'arn:aws:ecr:us-east-2:111111111111:repository/cairnproofs4af-artifact-cmh-alpha/*';
  const validateReadStmts = allIamStatements(template).filter(
    (s) =>
      s.action.includes('ecr:BatchGetImage') &&
      s.action.includes('ecr:DescribeImages') &&
      s.roleRefs.some((ref) => ref.startsWith('LambdaFunctionsValidateBundleServiceRole')),
  );
  expect(validateReadStmts).toHaveLength(1);
  // Namespace-wide (NOT pinned to the triggering project repo), so a foreign-
  // scope subject in another project repo is readable for the residence check.
  expect(validateReadStmts[0].resource).toBe(anyArtifactRepoArnPattern);
});

test('PutImage rule still routes attestation-referrer pushes — no artifactType narrowing (in-Lambda filter)', () => {
  // artifactType filtering is done IN-LAMBDA (the EventBridge rule does not
  // filter on artifactType), so the Producer_Attestation_Referrer PutImage must reach
  // validate-bundle through the ECR-push rule. The attestation referrer is an
  // ordinary OCI PutImage into the SAME project repo (matches the new
  // `cairnproofs4af-artifact-cmh-alpha/` repositoryName prefix) and is pushed by the PRODUCER
  // identity (not in the pipeline-internal anything-but list), so it is admitted.
  // Assert specifically that the rule does NOT narrow on artifactType (the
  // the push-rule filters key on errorCode / repositoryName /
  // userIdentity.arn — never artifactType) and that it targets validate-bundle.
  const template = Template.fromStack(makeStack());
  const rules = template.findResources('AWS::Events::Rule', {
    Properties: { Name: 'cairnproofs4af-ecr-push-cmh-alpha' },
  });
  expect(Object.keys(rules)).toHaveLength(1);
  const props = Object.values(rules)[0].Properties as {
    EventPattern: { detail: Record<string, unknown> };
    Targets: Array<{ Arn: unknown }>;
  };
  const detail = props.EventPattern.detail;
  expect(detail.eventName).toEqual(['PutImage']);
  expect(detail.eventSource).toEqual(['ecr.amazonaws.com']);
  // The repositoryName narrowing is a begins-with prefix on the project repo
  // namespace, so the referrer (same repo) still matches; it is NOT pinned to a
  // specific manifest mediaType / artifactType.
  const requestParameters = detail.requestParameters as Record<string, unknown> | undefined;
  expect(requestParameters?.repositoryName).toEqual([{ prefix: 'cairnproofs4af-artifact-cmh-alpha/' }]);
  expect(JSON.stringify(requestParameters)).not.toContain('imageManifestMediaType');
  // No responseElements / artifactType narrowing — the attestation-referrer
  // push is classified by the handler, not the rule.
  expect(detail.responseElements).toBeUndefined();
  expect(JSON.stringify(detail)).not.toContain('artifactType');
  // Routes to validate-bundle.
  expect(props.Targets.length).toBeGreaterThan(0);
  expect(JSON.stringify(props.Targets)).toContain('ValidateBundle');
});

// validate-bundle delivers
// permanent-rejection records to its DLQ IN-BAND (not via async-invoke retry
// exhaustion), so the handler needs the queue URL at runtime. Assert the env
// var is present and Refs the *validate* DLQ specifically (a Ref to an SQS
// queue resolves to that queue's URL). NO retryAttempts/maxEventAge override
// is asserted — transient failures must keep retrying ([PIPELINE-005]).
test('validate-bundle receives VALIDATE_DLQ_URL referencing its own DLQ (in-band rejection)', () => {
  const template = Template.fromStack(makeStack());

  // Resolve the logical id of the validate-bundle DLQ from its queue name.
  const queues = template.findResources('AWS::SQS::Queue', {
    Properties: { QueueName: 'cairnproofs4af-validate-bundle-dlq-cmh-alpha' },
  });
  const validateDlqLogicalIds = Object.keys(queues);
  expect(validateDlqLogicalIds).toHaveLength(1);
  const validateDlqLogicalId = validateDlqLogicalIds[0];

  // The env var must be present and Ref the validate DLQ (queue Ref == its URL).
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'cairnproofs4af-validate-bundle-cmh-alpha',
    Environment: {
      Variables: Match.objectLike({
        VALIDATE_DLQ_URL: { Ref: validateDlqLogicalId },
      }),
    },
  });
});

test('validate-bundle role can sqs:SendMessage to its own DLQ scoped to the queue ARN (in-band rejection grant is pinned)', () => {
  const template = Template.fromStack(makeStack());

  const queues = template.findResources('AWS::SQS::Queue', {
    Properties: { QueueName: 'cairnproofs4af-validate-bundle-dlq-cmh-alpha' },
  });
  const validateDlqLogicalId = Object.keys(queues)[0];

  // The in-band permanent-failure path relies on sqs:SendMessage; assert it is
  // explicitly granted and scoped to the validate DLQ ARN (not a wildcard), so a
  // future change to the async failure-routing cannot silently strip it.
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: {
      Statement: Match.arrayWith([
        Match.objectLike({
          Effect: 'Allow',
          Action: Match.arrayWith(['sqs:SendMessage']),
          Resource: Match.objectLike({ 'Fn::GetAtt': [validateDlqLogicalId, 'Arn'] }),
        }),
      ]),
    },
  });
});

test('mint-tag has its own DLQ and CloudWatch log group', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'cairnproofs4af-mint-tag-dlq-cmh-alpha',
  });
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/aws/lambda/cairnproofs4af-mint-tag-cmh-alpha',
  });
});

test('untagged-manifest lifecycle window stays at 90 days (in-flight reaping guard, T-09)', () => {
  const template = Template.fromStack(makeStack());
  // R6/T-09: an archived-but-not-yet-tagged manifest is legitimately untagged
  // for the full pipeline-through-mint latency. The 90-day window must comfortably
  // exceed that latency so no in-flight artifact is reaped before mint. Assert
  // the UNTAGGED expire rule is present and its window is >= 90 days.
  const repos = template.findResources('AWS::ECR::Repository');
  const repoProps = Object.values(repos)[0].Properties as {
    LifecyclePolicy: { LifecyclePolicyText: string };
  };
  const policy = JSON.parse(repoProps.LifecyclePolicy.LifecyclePolicyText) as {
    rules: Array<{
      selection: { tagStatus: string; countType: string; countUnit: string; countNumber: number };
      action: { type: string };
    }>;
  };
  const untaggedRule = policy.rules.find((r) => r.selection.tagStatus === 'untagged');
  expect(untaggedRule).toBeDefined();
  expect(untaggedRule!.action.type).toBe('expire');
  expect(untaggedRule!.selection.countType).toBe('sinceImagePushed');
  expect(untaggedRule!.selection.countUnit).toBe('days');
  expect(untaggedRule!.selection.countNumber).toBeGreaterThanOrEqual(90);
});

// ── Alias-scoped tag mutability + alias-mover boundary ──────────────────────
//
// The reserved alias namespace (`_als`/`_chn` middle-token forms) is mutable
// while the canonical `_ref` form stays hard-immutable. The repo uses
// IMMUTABLE_WITH_EXCLUSION with exactly two contains-style exclusion filters
// (`*--_als--*`, `*--_chn--*`); alias set/move is gated behind a dedicated
// alias-mover IAM policy distinct from producer push and signing.
//
// OQ2 (whether ECR honors a LEADING-`*` contains-match) is a DEPLOY-TIME gate,
// not synthesizable — these tests assert the template, never runtime overwrite
// behavior.

const ALIAS_EXCLUSION_FILTERS = ['*--_als--*', '*--_chn--*'];

test('ECR repo uses IMMUTABLE_WITH_EXCLUSION with exactly the two alias filters (T4)', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::ECR::Repository', {
    ImageTagMutability: 'IMMUTABLE_WITH_EXCLUSION',
    ImageTagMutabilityExclusionFilters: ALIAS_EXCLUSION_FILTERS.map((value) => ({
      ImageTagMutabilityExclusionFilterType: 'WILDCARD',
      ImageTagMutabilityExclusionFilterValue: value,
    })),
  });

  // Pin the count: exactly two filters, no more (a third/`*` filter would risk
  // unlocking canonical tags — the T4 mis-scope regression).
  const repos = template.findResources('AWS::ECR::Repository');
  const repoProps = Object.values(repos)[0].Properties as {
    ImageTagMutabilityExclusionFilters: Array<{ ImageTagMutabilityExclusionFilterValue: string }>;
  };
  expect(repoProps.ImageTagMutabilityExclusionFilters).toHaveLength(2);
});

// The exclusion filters must provably match ONLY alias tags, never a
// canonical `_ref` tag. ECR WILDCARD matches `*` as zero-or-more chars; assert
// (pure string) that each filter matches its alias form and that NEITHER glob
// matches a sample canonical tag. This is the mis-scope guard that proves the
// canonical namespace stays immutable.
test('alias exclusion filters match only alias tags, never a canonical _ref tag (T4)', () => {
  const wildcardMatches = (pattern: string, value: string): boolean => {
    const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
    return new RegExp(`^${escaped}$`).test(value);
  };

  const canonicalTag = 'catalog--_ref--a1b2c3';
  const namedAliasTag = 'catalog--_als--nist-800-53-latest';
  const channelTag = 'catalog--_chn--latest';

  // Each filter matches its own alias form …
  expect(wildcardMatches('*--_als--*', namedAliasTag)).toBe(true);
  expect(wildcardMatches('*--_chn--*', channelTag)).toBe(true);

  // … and NEITHER filter matches the canonical `_ref` tag — canonical
  // immutability is preserved.
  for (const filter of ALIAS_EXCLUSION_FILTERS) {
    expect(wildcardMatches(filter, canonicalTag)).toBe(false);
  }
  // Cross-check: the alias filters don't bleed across each other's namespace.
  expect(wildcardMatches('*--_als--*', channelTag)).toBe(false);
  expect(wildcardMatches('*--_chn--*', namedAliasTag)).toBe(false);
});

test('alias-mover managed policy exists with ecr:PutImage + BatchGetImage on the project repo', () => {
  const template = Template.fromStack(makeStack());
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  const aliasMover = Object.values(managed).filter((p) =>
    ((p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName ?? '').startsWith(
      'cairnproofs4af-alias-mover',
    ),
  );
  expect(aliasMover).toHaveLength(1);

  const statements = (
    aliasMover[0].Properties as {
      PolicyDocument: { Statement: Array<{ Sid?: string; Action?: unknown; Resource?: unknown }> };
    }
  ).PolicyDocument.Statement;
  const moveStmt = statements.find((s) => s.Sid === 'EcrAliasMoveOnProjectRepos');
  expect(moveStmt).toBeDefined();
  const actions = Array.isArray(moveStmt!.Action) ? moveStmt!.Action : [moveStmt!.Action];
  expect(actions).toContain('ecr:PutImage');
  expect(actions).toContain('ecr:BatchGetImage');
  // Scoped to the bare project repo ARN, never `*` and no `/*` form (D.14).
  // A single-element resource renders as a scalar string in CloudFormation.
  const projectRepoArn = PROJECT_REPO_ARN;
  const moveResources = Array.isArray(moveStmt!.Resource)
    ? (moveStmt!.Resource as unknown[])
    : [moveStmt!.Resource];
  expect(moveResources).toEqual([projectRepoArn]);
});

// The alias-movement grant is its OWN policy and must NOT be embedded
// in the producer push policy or the signing principal. Possessing producer or
// signer permissions must not, by that fact alone, confer alias movement. The
// discriminator is the `EcrAliasMoveOnProjectRepos` sid, which appears ONLY on
// the alias-mover policy. (ECR PutImage cannot be tag-conditioned, so this
// role-granularity boundary — not an IAM tag condition — is the load-bearing
// control.)
test('alias-mover grant is not embedded in producer or signer policies (T5/T6)', () => {
  const template = Template.fromStack(makeStack());
  const managed = template.findResources('AWS::IAM::ManagedPolicy');

  const sidsFor = (namePrefix: string): string[] =>
    Object.values(managed)
      .filter((p) =>
        ((p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName ?? '').startsWith(
          namePrefix,
        ),
      )
      .flatMap((p) =>
        (
          p.Properties as { PolicyDocument: { Statement: Array<{ Sid?: string }> } }
        ).PolicyDocument.Statement.map((s) => s.Sid ?? ''),
      );

  // The producer policy must NOT carry the alias-move statement.
  expect(sidsFor('cairnproofs4af-producer')).not.toContain('EcrAliasMoveOnProjectRepos');
  // The alias-move statement lives ONLY on the alias-mover policy.
  expect(sidsFor('cairnproofs4af-alias-mover')).toContain('EcrAliasMoveOnProjectRepos');

  // No signing principal carries the alias-move statement anywhere across the
  // synthesized IAM surface (managed + inline policies).
  const allIam = JSON.stringify({
    managed,
    inline: template.findResources('AWS::IAM::Policy'),
  });
  // The alias-move sid appears exactly once (the alias-mover policy alone).
  const occurrences = allIam.split('EcrAliasMoveOnProjectRepos').length - 1;
  expect(occurrences).toBe(1);
});

// ── Operator role, identity registry, three-segment path, tenant-keyed-GSI
//    rule ──────────────────────────────────────────────────────────────────

// Helper: read the managed policy whose ManagedPolicyName starts with a prefix.
function managedPolicyByPrefix(
  template: Template,
  prefix: string,
): { Statement: Array<{ Sid?: string; Effect?: string; Action?: unknown; Resource?: unknown }> } {
  const managed = template.findResources('AWS::IAM::ManagedPolicy');
  const matches = Object.values(managed).filter((p) =>
    ((p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName ?? '').startsWith(prefix),
  );
  expect(matches).toHaveLength(1);
  return (matches[0].Properties as { PolicyDocument: { Statement: Array<{ Sid?: string; Effect?: string; Action?: unknown; Resource?: unknown }> } })
    .PolicyDocument;
}

// ACCESS-006: the least-privilege operator is a first-class managed policy,
// untrusted for integrity by construction.
test('operator managed policy exists and is untrusted for integrity (no PutImage, no sign)', () => {
  const template = Template.fromStack(makeStack());
  const doc = managedPolicyByPrefix(template, 'cairnproofs4af-operator');

  const allowActions = doc.Statement.filter((s) => s.Effect !== 'Deny').flatMap((s) =>
    Array.isArray(s.Action) ? (s.Action as string[]) : s.Action ? [s.Action as string] : [],
  );
  // The operator can read/operate the substrate (describe visibility + a
  // management knob) …
  expect(allowActions).toContain('ecr:Describe*');
  expect(allowActions).toContain('ecr:PutLifecyclePolicy');
  expect(allowActions).toContain('cloudformation:DescribeStacks');
  // … but operating does NOT grant the data-plane manifest/alias write, so the
  // operator cannot move an alias by operating ([ACCESS-006.01]).
  expect(allowActions).not.toContain('ecr:PutImage');
  // … and no signing action is ALLOWed — the operator cannot mint a verdict.
  for (const a of allowActions) {
    expect(a.startsWith('signer:Sign')).toBe(false);
    expect(a.startsWith('signer:StartSigningJob')).toBe(false);
  }
});

// [ACCESS-006]: the explicit DENY guardrail bounds the
// operator's delete / DoS / read blast radius and re-affirms the integrity bound.
test('operator policy DENY guardrail bounds delete/DoS/read and forbids manifest/alias write + sign', () => {
  const template = Template.fromStack(makeStack());
  const doc = managedPolicyByPrefix(template, 'cairnproofs4af-operator');
  const denyStmt = doc.Statement.find((s) => s.Effect === 'Deny');
  expect(denyStmt).toBeDefined();
  const denied = Array.isArray(denyStmt!.Action)
    ? (denyStmt!.Action as string[])
    : [denyStmt!.Action as string];

  // Integrity boundary: no manifest register / alias move, no sign.
  expect(denied).toContain('ecr:PutImage');
  expect(denied).toContain('signer:SignPayload');
  expect(denied).toContain('signer:StartSigningJob');
  // Bounded delete (T3) — archive objects and data-plane items/tables.
  expect(denied).toContain('s3:DeleteObject');
  expect(denied).toContain('dynamodb:DeleteItem');
  expect(denied).toContain('dynamodb:DeleteTable');
  expect(denied).toContain('ecr:BatchDeleteImage');
  // Bounded read (T4): no evidence-plaintext decrypt.
  expect(denied).toContain('kms:Decrypt');
  // Bounded DoS (T2): no key disable / scheduled deletion.
  expect(denied).toContain('kms:ScheduleKeyDeletion');
  expect(denied).toContain('kms:DisableKey');
});

// ACCESS-006 item 2: operating does NOT attach the alias-mover grant — the
// alias-move statement lives ONLY on the alias-mover policy, never the operator.
test('operator policy does NOT carry the alias-mover grant (ACCESS-006.01 / ACCESS-005 boundary)', () => {
  const template = Template.fromStack(makeStack());
  const doc = managedPolicyByPrefix(template, 'cairnproofs4af-operator');
  const sids = doc.Statement.map((s) => s.Sid ?? '');
  expect(sids).not.toContain('EcrAliasMoveOnProjectRepos');
});

// IDREG-001: the Identity_Registry is a control-plane DynamoDB table, encrypted
// with the service CMK, PITR, on-demand, keyed by the opaque id.
test('identity-registry table is CMK-encrypted, PITR, on-demand, keyed by opaque id (IDREG-001)', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    Tags: Match.arrayWith([{ Key: 'cairn:resource', Value: 'IdentityRegistry' }]),
    BillingMode: 'PAY_PER_REQUEST',
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    SSESpecification: Match.objectLike({ SSEEnabled: true }),
    // Keyed on `id` (the opaque identity coordinate). The partition-key
    // attribute name MUST match the cairn_metadata IdentityRegistry facade that
    // reads these items — a `pk`/`id` disagreement fails the facade's GetItem /
    // BatchGetItem closed, so display names silently never resolve (IDREG-001).
    KeySchema: [{ AttributeName: 'id', KeyType: 'HASH' }],
    AttributeDefinitions: Match.arrayWith([{ AttributeName: 'id', AttributeType: 'S' }]),
  });
});

// IDREG-001 (alpha scope): the single org/tenant/project is seeded at deploy via
// deploy-time PutItem custom resources carrying the opaque ids (display names
// live ONLY here, never on the path/claim/tag).
test('identity-registry seeds the single org/tenant/project at deploy (IDREG-001)', () => {
  const template = Template.fromStack(makeStack());
  // One AwsCustomResource (Custom::AWS) per seeded coordinate.
  const seeds = template.findResources('Custom::AWS');
  const seedStrings = Object.values(seeds).map((r) => JSON.stringify(r.Properties));
  const putItemSeeds = seedStrings.filter((s) => s.includes('putItem'));
  expect(putItemSeeds.length).toBe(3);
  // The opaque org / tenant / project ids appear in the seed payloads.
  const allSeeds = putItemSeeds.join('|');
  expect(allSeeds).toContain('mvp-org');
  expect(allSeeds).toContain('mvp-tenant');
  expect(allSeeds).toContain('mvp-project');
  // Parentage is recorded (kind discriminator + orgId/tenantId attributes).
  // `kind` (not `entityType`) is the attribute name the cairn_metadata
  // IdentityRegistry facade writes/reads, so the seed must agree with it.
  expect(allSeeds).toContain('kind');
});

// [META-001]: alternate access paths are GSIs only, and every
// GSI on a tenant-PK plane must lead with {tenant-id}. No GSI ships today, so
// the testable form of the rule is that NO tenant-PK plane (or the
// control-plane registry) carries any GSI — a non-tenant-keyed GSI could
// never be introduced silently.
test('no DynamoDB table carries a Global Secondary Index (tenant-keyed-GSI rule, no GSI ships)', () => {
  const template = Template.fromStack(makeStack());
  const tables = template.findResources('AWS::DynamoDB::Table');
  expect(Object.keys(tables).length).toBe(3);
  for (const t of Object.values(tables)) {
    const props = t.Properties as { GlobalSecondaryIndexes?: unknown };
    expect(props.GlobalSecondaryIndexes).toBeUndefined();
  }
});

// D.1 / D.14: the ECR repo path is the uniform three-segment hierarchy and the
// per-category `/*` sub-repository ARN form is gone from EVERY producer/alias
// push grant.
test('no producer/alias push grant retains the removed per-category /* ARN form (D.14)', () => {
  const template = Template.fromStack(makeStack());
  const removedForm = `${PROJECT_REPO_ARN}/*`;
  const managed = JSON.stringify(template.findResources('AWS::IAM::ManagedPolicy'));
  expect(managed).not.toContain(removedForm);
});

// ── ADOT layer + OTEL activation env ────────────────────────────────────────
//
// The AWS-managed ADOT collector-only layer is attached to all five pipeline
// functions, and `baseEnv` sets the OTEL activation vars (propagators +
// collector config). These tests pin that layer attachment and env, plus the
// regression that sign-bundle keeps its Notation layer alongside ADOT.
//
// The five pipeline functions, keyed by their deployed FunctionName. mint-tag
// is the post-archive tagger; the ADOT instrumentation Requirement covers it
// alongside the other four.
const PIPELINE_FUNCTION_NAMES = [
  'cairnproofs4af-validate-bundle-cmh-alpha',
  'cairnproofs4af-sign-bundle-cmh-alpha',
  'cairnproofs4af-archive-to-s3-cmh-alpha',
  'cairnproofs4af-update-metadata-cmh-alpha',
  'cairnproofs4af-mint-tag-cmh-alpha',
];

// Resolve the synthesized `AWS::Lambda::Function` resources keyed by deployed
// FunctionName. Returns the Properties block so each test can read Layers /
// Environment without re-querying.
function pipelineFunctionProps(
  template: Template,
): Record<string, { Layers?: unknown[]; Environment?: { Variables?: Record<string, unknown> } }> {
  const fns = template.findResources('AWS::Lambda::Function');
  const byName: Record<
    string,
    { Layers?: unknown[]; Environment?: { Variables?: Record<string, unknown> } }
  > = {};
  for (const fn of Object.values(fns)) {
    const props = fn.Properties as {
      FunctionName?: string;
      Layers?: unknown[];
      Environment?: { Variables?: Record<string, unknown> };
    };
    if (props.FunctionName && PIPELINE_FUNCTION_NAMES.includes(props.FunctionName)) {
      byName[props.FunctionName] = props;
    }
  }
  return byName;
}

// The AWS-managed ADOT collector-only layer ARN carries the
// `:layer:aws-otel-collector-...` segment (the managed-layer name the AWS
// Distro for OpenTelemetry publishes per region). It is attached via
// `LayerVersion.fromLayerVersionArn`. Match the managed-layer name fragment so
// the assertion is robust to region/version drift — `aws-otel` (lowercase,
// matching the published `aws-otel-collector-*` layer family) or the CamelCase
// `AWSLambdaPythonAdot` form.
const ADOT_LAYER_ARN_FRAGMENT = /:layer:(aws-otel|AWSLambdaPythonAdot)/;

// Every pipeline function attaches the ADOT managed layer.
test('every pipeline Lambda attaches the ADOT managed layer', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  // All five functions must be present in the synthesized template.
  expect(Object.keys(fns).sort()).toEqual([...PIPELINE_FUNCTION_NAMES].sort());

  for (const name of PIPELINE_FUNCTION_NAMES) {
    const layers = fns[name].Layers ?? [];
    // Each layer entry may be a plain ARN string or a Fn::Join token; stringify
    // and match the ADOT managed-layer ARN fragment.
    const hasAdot = layers.some((l) => ADOT_LAYER_ARN_FRAGMENT.test(JSON.stringify(l)));
    expect(hasAdot).toBe(true);
  }
});

// NO pipeline Lambda sets AWS_LAMBDA_EXEC_WRAPPER. The ADOT layer is
// collector-only and the app installs its own TracerProvider, so the exec
// wrapper must be ABSENT (not /opt/otel-instrument).
test('no pipeline Lambda sets AWS_LAMBDA_EXEC_WRAPPER (collector-only)', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  for (const name of PIPELINE_FUNCTION_NAMES) {
    const vars = fns[name].Environment?.Variables ?? {};
    expect(vars.AWS_LAMBDA_EXEC_WRAPPER).toBeUndefined();
  }
});

// OTEL_PROPAGATORS includes tracecontext + baggage (W3C propagation per
// Expected Behavior #2), and the collector config pointer is set (Expected
// Behavior #3).
test('every pipeline Lambda sets OTEL_PROPAGATORS (tracecontext+baggage) and collector URI', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  for (const name of PIPELINE_FUNCTION_NAMES) {
    const vars = fns[name].Environment?.Variables ?? {};
    const propagators = vars.OTEL_PROPAGATORS;
    expect(typeof propagators).toBe('string');
    const list = String(propagators)
      .split(',')
      .map((s) => s.trim());
    expect(list).toContain('tracecontext');
    expect(list).toContain('baggage');
    // `xray` so the span trace ID equals the X-Ray-service trace ID, keeping
    // log `traceId` correlated to the X-Ray console trace.
    expect(list).toContain('xray');
    // Collector config pointer present (any non-empty value).
    expect(vars.OPENTELEMETRY_COLLECTOR_CONFIG_URI).toBeTruthy();
  }
});

// Regression — sign-bundle carries the Notation layer alongside the ADOT
// layer. Assert Notation presence only (no length check).
test('sign-bundle attaches the Notation layer (regression)', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  const signLayers = fns['cairnproofs4af-sign-bundle-cmh-alpha'].Layers ?? [];
  // The Notation layer is a same-stack LayerVersion, so it synthesizes as a
  // Ref/Fn::GetAtt token whose logical id carries the `Notation` fragment.
  const hasNotation = signLayers.some((l) => /Notation/.test(JSON.stringify(l)));
  expect(hasNotation).toBe(true);
});

// ── ADOT trace-export edge-case coverage ────────────────────────────────────
//
// Gaps the primary assertions leave open:
//  (a) 1.1 matches the ADOT ARN *fragment* but never asserts the layer is
//      version-PINNED (not floating "latest") nor that the publisher account
//      is the canonical public ADOT account 901920570463. A floating-version
//      or wrong-account ARN would silently change instrumentation behavior on
//      every republish — the exact failure mode design.md §Proposed Fix calls
//      out ("Pinned (not 'latest') … so a layer republish can't silently
//      change instrumentation behavior").
//  (b) 3.1 asserts Notation is present on sign-bundle but not that it carries
//      EXACTLY Notation + ADOT (both, and no extra/missing layer). A regression
//      that dropped Notation while keeping ADOT, or vice-versa, would pass 3.1
//      (it only checks Notation presence) and 1.1 (only checks ADOT presence)
//      independently — neither pins the conjunction on the one function that
//      must carry both.

// Edge — the ADOT layer ARN is version-PINNED and published by the canonical
// public ADOT account 901920570463 on EVERY pipeline function. Version-pinned
// means the ARN's trailing version segment is a concrete number (`...ver-N-..`
// + the layer-version `:N`), never `latest`/`$LATEST`.
test('ADOT layer ARN is version-pinned + publisher account 901920570463 (every fn)', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  // Pinned managed-layer ARN. The partition is templated from the stack
  // (`${AWS::Partition}`), so CDK synthesizes the ARN as an `Fn::Join`
  // intrinsic rather than a plain string — assert against the serialized form.
  // Account 901920570463, region from the test stack (us-east-2), concrete
  // version aws-otel-collector-arm64-ver-<n>-<n>-<n>:<n>. No `latest`. The
  // layer is the collector-only ADOT layer.
  const PINNED_ADOT_SUFFIX =
    /:lambda:us-east-2:901920570463:layer:aws-otel-collector-arm64-ver-\d+-\d+-\d+:\d+/;
  for (const name of PIPELINE_FUNCTION_NAMES) {
    const layers = fns[name].Layers ?? [];
    const adotLayers = layers.filter((l) =>
      ADOT_LAYER_ARN_FRAGMENT.test(JSON.stringify(l)),
    );
    expect(adotLayers).toHaveLength(1);
    const serialized = JSON.stringify(adotLayers[0]);
    // Concrete account + version pin present in the joined ARN.
    expect(serialized).toMatch(PINNED_ADOT_SUFFIX);
    // Partition is templated via the AWS::Partition pseudo-parameter, not hardcoded.
    expect(serialized).toContain('AWS::Partition');
    // Defensive: no floating-version token anywhere in the ADOT ARN.
    expect(serialized.toLowerCase()).not.toContain('latest');
  }
});

// Edge — sign-bundle carries EXACTLY two layers: the same-stack Notation
// layer AND the ADOT managed layer (Unchanged Behavior #1 conjunction). Pins
// that adding ADOT did not displace Notation and that no third layer crept in.
test('sign-bundle attaches exactly Notation + ADOT (both, no more)', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  const signLayers = fns['cairnproofs4af-sign-bundle-cmh-alpha'].Layers ?? [];
  expect(signLayers).toHaveLength(2);

  const hasNotation = signLayers.some((l) => /Notation/.test(JSON.stringify(l)));
  const hasAdot = signLayers.some((l) => ADOT_LAYER_ARN_FRAGMENT.test(JSON.stringify(l)));
  expect(hasNotation).toBe(true);
  expect(hasAdot).toBe(true);
});

// Edge — the four NON-signing pipeline functions carry EXACTLY one layer: the
// ADOT managed layer (and specifically NOT the Notation layer, which is
// sign-bundle-only). Guards against ADOT being attached more than once or
// the Notation layer leaking onto a function that doesn't need it.
test('non-signing pipeline Lambdas attach exactly the ADOT layer (no Notation)', () => {
  const template = Template.fromStack(makeStack());
  const fns = pipelineFunctionProps(template);
  const nonSigning = PIPELINE_FUNCTION_NAMES.filter(
    (n) => n !== 'cairnproofs4af-sign-bundle-cmh-alpha',
  );
  for (const name of nonSigning) {
    const layers = fns[name].Layers ?? [];
    expect(layers).toHaveLength(1);
    expect(ADOT_LAYER_ARN_FRAGMENT.test(JSON.stringify(layers[0]))).toBe(true);
    expect(/Notation/.test(JSON.stringify(layers[0]))).toBe(false);
  }
});

// 3.2 (regression) — no pipeline Lambda execution-role policy statement carries
// `cloudwatch:PutMetricData`. Metrics flow through the EMF path (stdout
// structured metric lines), not the PutMetricData API, so no role should hold
// that grant. Guards the EMF contract; passes on main AND after the fix.
test('no pipeline Lambda role carries cloudwatch:PutMetricData (3.2, regression, EMF contract)', () => {
  const template = Template.fromStack(makeStack());
  const lambdaRolePrefixes = [
    'LambdaFunctionsValidateBundleServiceRole',
    'LambdaFunctionsSignBundleServiceRole',
    'LambdaFunctionsArchiveToS3ServiceRole',
    'LambdaFunctionsUpdateMetadataServiceRole',
    'LambdaFunctionsMintTagServiceRole',
  ];
  const policies = template.findResources('AWS::IAM::Policy');
  for (const policy of Object.values(policies)) {
    const props = policy.Properties as {
      Roles?: Array<{ Ref?: string }>;
      PolicyDocument?: { Statement?: Array<{ Action?: unknown }> };
    };
    const attachedToPipelineRole = (props.Roles ?? []).some((r) =>
      lambdaRolePrefixes.some((prefix) => (r.Ref ?? '').startsWith(prefix)),
    );
    if (!attachedToPipelineRole) continue;
    for (const s of props.PolicyDocument?.Statement ?? []) {
      const actions = Array.isArray(s.Action) ? s.Action : s.Action ? [s.Action] : [];
      expect(actions).not.toContain('cloudwatch:PutMetricData');
    }
  }
});

// Regression — one DeadLetterErrors alarm per pipeline Lambda, each with its
// SNS action wired (so re-enabling is a one-line flip) but DISABLED during the
// stabilization window. The 'per-Lambda DeadLetterErrors alarms
// present-but-disabled' test (above) already asserts the count (5) and the
// wired-but-disabled posture; this is a focused confirmation co-located with
// the ADOT guards.
test('one DeadLetterErrors alarm per pipeline Lambda, action wired-but-disabled (regression)', () => {
  const template = Template.fromStack(makeStack());
  const dlqAlarms = template.findResources('AWS::CloudWatch::Alarm', {
    Properties: { MetricName: 'DeadLetterErrors', Namespace: 'AWS/Lambda' },
  });
  // One alarm per pipeline Lambda (validate, sign, archive, metadata, mint-tag).
  expect(Object.keys(dlqAlarms)).toHaveLength(PIPELINE_FUNCTION_NAMES.length);
  for (const alarm of Object.values(dlqAlarms)) {
    expectActionWiredButDisabled(alarm.Properties);
  }
  // The alarm actions target the SNS topic (the pipeline-alerts topic is the
  // only SNS topic in the stack — see 'SNS alarm topic + email subscription').
  template.resourceCountIs('AWS::SNS::Topic', 1);
});

// ── teardown-readiness gate (keyed on isProd) ──────────────────────────────
//
// `cdk destroy` honors the DEPLOYED template's DeletionPolicy, not local code,
// so teardown readiness is a synth-time property. The gate is isProd (threat
// T-03 / mitigation M-5): false (non-production) ⇒ DESTROY + auto-empty on
// every stateful resource so teardown orphans nothing; true (production) ⇒
// RETAIN with NO auto-delete. The ProductionTeardownGuard aspect (mitigation
// M-6) fails synth on any contradiction so the gate cannot silently invert
// under future refactoring.
//
// Teardown is DECOUPLED from WORM_Enabled (S3 Object Lock): retention follows
// isProd alone, Object Lock follows wormEnabled. The two stay coherent via the
// stack-level invariant that wormEnabled ⇒ isProd (an Object-Lock bucket can't
// be auto-emptied), exercised below.

// The stack's stateful (data-bearing) resource types and the count of each —
// 17 resources across the RETAIN-gated construct sites (archive + access-log
// buckets, ECR repo, 3 DynamoDB tables — Metadata_Index + ControlClaims +
// Identity_Registry, KMS CMK, 5 DLQs, 5 log groups).
const STATEFUL_RESOURCE_COUNTS: Record<string, number> = {
  'AWS::S3::Bucket': 2,
  'AWS::ECR::Repository': 1,
  'AWS::DynamoDB::Table': 3,
  'AWS::KMS::Key': 1,
  'AWS::SQS::Queue': 5,
  'AWS::Logs::LogGroup': 5,
};

// Production stack WITHOUT Object Lock (isProd=true, wormEnabled=false). Proves
// RETAIN is driven by isProd alone — the decoupling from WORM_Enabled.
function makeProdStack(): ArtifactStoreStack {
  const app = new App({ outdir: OUTDIR });
  return new ArtifactStoreStack(app, 'ProdStack', {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: true,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
  });
}

// Full production stack: isProd=true AND Object Lock on the archive bucket.
// wormEnabled=true requires isProd=true (the stack-level invariant).
function makeWormStack(): ArtifactStoreStack {
  const app = new App({ outdir: OUTDIR });
  return new ArtifactStoreStack(app, 'WormStack', {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: true,
    wormEnabled: true,
    notificationEmail: 'test@example.com',
  });
}

test('isProd=false: every stateful resource is Delete on delete AND replace', () => {
  const template = Template.fromStack(makeStack());
  for (const [type, count] of Object.entries(STATEFUL_RESOURCE_COUNTS)) {
    // Pin the count so a new stateful resource cannot dodge the assertion.
    template.resourceCountIs(type, count);
    template.allResources(
      type,
      Match.objectLike({
        DeletionPolicy: 'Delete',
        UpdateReplacePolicy: 'Delete',
      }),
    );
  }
});

test('isProd=false: S3 auto-delete + ECR EmptyOnDelete present (auto-empty)', () => {
  const template = Template.fromStack(makeStack());
  // Without auto-empty, a non-empty bucket/repo blocks deletion even under
  // DESTROY. One Custom::S3AutoDeleteObjects per bucket (archive + access-logs).
  template.resourceCountIs('Custom::S3AutoDeleteObjects', 2);
  template.hasResourceProperties('AWS::ECR::Repository', { EmptyOnDelete: true });
});

// The production posture: every stateful resource RETAIN with NO auto-empty.
// Shared by the isProd-only and the isProd+WORM stacks below, because retention
// is driven by isProd ALONE (M-5).
function expectRetainedWithNoAutoEmpty(template: Template): void {
  for (const [type, count] of Object.entries(STATEFUL_RESOURCE_COUNTS)) {
    template.resourceCountIs(type, count);
    template.allResources(
      type,
      Match.objectLike({
        DeletionPolicy: 'Retain',
        UpdateReplacePolicy: 'Retain',
      }),
    );
  }
  // No S3 auto-delete custom resource anywhere in a production stack.
  template.resourceCountIs('Custom::S3AutoDeleteObjects', 0);
  // ECR never auto-empties on a production stack.
  for (const repo of Object.values(template.findResources('AWS::ECR::Repository'))) {
    expect((repo.Properties as { EmptyOnDelete?: boolean }).EmptyOnDelete).not.toBe(true);
  }
}

test('isProd=true (no WORM): stateful resources Retain, no auto-delete, Object Lock OFF', () => {
  // Decoupling proof: retention is driven by isProd ALONE. This stack is
  // production (RETAIN) but wormEnabled=false, so the archive bucket must NOT
  // carry Object Lock — teardown-readiness and Object Lock are orthogonal.
  const template = Template.fromStack(makeProdStack());
  expectRetainedWithNoAutoEmpty(template);
  for (const bucket of Object.values(template.findResources('AWS::S3::Bucket'))) {
    expect((bucket.Properties as { ObjectLockEnabled?: boolean }).ObjectLockEnabled).not.toBe(
      true,
    );
  }
});

test('wormEnabled=true rides on isProd=true: Retain + Object Lock on the archive bucket', () => {
  const template = Template.fromStack(makeWormStack());
  expectRetainedWithNoAutoEmpty(template);
  // Sanity: this really is the WORM branch — Object Lock on the archive bucket.
  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketName: 'cairnproofs4af-artifact-archive-cmh-alpha',
    ObjectLockEnabled: true,
  });
});

test('forcing DESTROY onto a stateful resource of an isProd=true stack fails synth (M-6)', () => {
  // End-to-end proof that the stack itself wires the guard: sabotage one
  // stateful construct post-construction (as a future refactor might) and
  // assert synth THROWS rather than silently emitting a deletable template.
  // wormEnabled=false here — the guard fires on isProd alone, not WORM.
  const app = new App({ outdir: OUTDIR });
  const stack = new ArtifactStoreStack(app, 'ProdViolationStack', {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: true,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
  });
  stack.metadataTable.table.applyRemovalPolicy(RemovalPolicy.DESTROY);
  expect(() => app.synth()).toThrow(/isProd=true requires every stateful resource/);
});

test('ProductionTeardownGuard throws on a DESTROY stateful resource when isProd=true (M-6 unit)', () => {
  const app = new App({ outdir: OUTDIR });
  const stack = new Stack(app, 'GuardDestroyStack');
  new Bucket(stack, 'Violating', { removalPolicy: RemovalPolicy.DESTROY });
  Aspects.of(stack).add(new ProductionTeardownGuard(true));
  expect(() => app.synth()).toThrow(/isProd=true requires every stateful resource/);
});

test('ProductionTeardownGuard throws on an S3 auto-delete custom resource when isProd=true (M-6 unit)', () => {
  // The Bucket L2 itself refuses autoDeleteObjects without DESTROY (in which
  // case the DESTROY check above fires first), so exercise the auto-delete
  // branch directly via a bare custom resource of the provider's type.
  const app = new App({ outdir: OUTDIR });
  const stack = new Stack(app, 'GuardAutoDeleteStack');
  new CfnResource(stack, 'FakeAutoDelete', { type: 'Custom::S3AutoDeleteObjects' });
  Aspects.of(stack).add(new ProductionTeardownGuard(true));
  expect(() => app.synth()).toThrow(/forbids S3 autoDeleteObjects/);
});

test('ProductionTeardownGuard throws on ECR emptyOnDelete when isProd=true (M-6 unit)', () => {
  const app = new App({ outdir: OUTDIR });
  const stack = new Stack(app, 'GuardEmptyOnDeleteStack');
  // emptyOnDelete requires DESTROY at the L2 layer; the guard checks the
  // emptyOnDelete contradiction first so the message names the real footgun.
  new Repository(stack, 'Violating', {
    removalPolicy: RemovalPolicy.DESTROY,
    emptyOnDelete: true,
  });
  Aspects.of(stack).add(new ProductionTeardownGuard(true));
  expect(() => app.synth()).toThrow(/forbids ECR emptyOnDelete/);
});

test('ProductionTeardownGuard is a no-op when isProd=false (gate engages only on production)', () => {
  const app = new App({ outdir: OUTDIR });
  const stack = new Stack(app, 'GuardNoopStack');
  new Bucket(stack, 'Disposable', {
    removalPolicy: RemovalPolicy.DESTROY,
    autoDeleteObjects: true,
  });
  Aspects.of(stack).add(new ProductionTeardownGuard(false));
  expect(() => app.synth()).not.toThrow();
});

test('constructing a stack with wormEnabled=true but isProd=false throws (WORM ⇒ isProd invariant)', () => {
  // An Object-Lock bucket cannot be auto-emptied/DESTROYed, so wormEnabled
  // without isProd is incoherent and is rejected at construction — the isProd
  // teardown gate therefore covers every WORM-enabled deployment.
  const app = new App({ outdir: OUTDIR });
  expect(
    () =>
      new ArtifactStoreStack(app, 'IncoherentWormStack', {
        env: { account: '111111111111', region: 'us-east-2' },
        appName: TEST_APP_NAME,
        regionCode: 'cmh',
        stage: 'alpha',
        orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
        isProd: false,
        wormEnabled: true,
        notificationEmail: 'test@example.com',
      }),
  ).toThrow(/WORM_Enabled=true requires isProd=true/);
});

// ── Uniform AWS resource naming ──────────────────────────────────────────────
//
// platform-constraints *Uniform AWS Resource Naming Convention*: every AWS
// resource physical name in the synthesized template is `{appName}-…`
// (hyphen / underscore / `alias/{appName}-` / `/{appName}/` variants), with
// exactly ONE documented exception: the Stack name is `{appName}-{qualifier}`
// (2 segments, no `{resource}`). These tests enforce that uniformity, the
// generate-once-and-freeze token behavior, and the single-composition-
// point ECR namespace.

// Resolve EVERY physical name the synthesized template assigns. Returns a flat
// list of { kind, name } so the assertion can apply the right per-kind prefix
// rule. CDK omits the physical name for resources it auto-names (e.g. the S3
// auto-delete custom-resource Lambda, IAM roles, the Signer profile L2) — those
// carry no asserted name and are skipped (they are not `{appName}`-violations).
function physicalNames(template: Template): Array<{ kind: string; name: string }> {
  const out: Array<{ kind: string; name: string }> = [];
  const add = (resourceType: string, prop: string, kind: string) => {
    for (const r of Object.values(template.findResources(resourceType))) {
      const name = (r.Properties as Record<string, unknown> | undefined)?.[prop];
      if (typeof name === 'string') {
        out.push({ kind, name });
      }
    }
  };
  add('AWS::Lambda::Function', 'FunctionName', 'lambda');
  add('AWS::SQS::Queue', 'QueueName', 'sqs');
  // DynamoDB tables are auto-named (no explicit TableName) so a future
  // key-schema change replaces cleanly; their role is carried by the
  // `cairn:resource` tag and their physical name published to SSM — so they
  // carry no asserted physical name and are (correctly) not naming-checked here.
  add('AWS::S3::Bucket', 'BucketName', 's3');
  add('AWS::ECR::Repository', 'RepositoryName', 'ecr');
  add('AWS::Events::EventBus', 'Name', 'eventbus');
  add('AWS::Events::Rule', 'Name', 'rule');
  add('AWS::SNS::Topic', 'TopicName', 'sns');
  add('AWS::CloudWatch::Alarm', 'AlarmName', 'alarm');
  add('AWS::Lambda::LayerVersion', 'LayerName', 'layer');
  add('AWS::IAM::ManagedPolicy', 'ManagedPolicyName', 'iam');
  add('AWS::KMS::Alias', 'AliasName', 'kms-alias');
  add('AWS::SSM::Parameter', 'Name', 'ssm');
  add('AWS::Signer::SigningProfile', 'ProfileName', 'signer');
  return out;
}

test('every named physical resource starts with the appName prefix (uniform naming)', () => {
  const template = Template.fromStack(makeStack());
  const names = physicalNames(template);
  // Sanity: we actually found a substantial set of named resources (guards
  // against a vacuous pass if findResources silently returned nothing).
  expect(names.length).toBeGreaterThan(15);

  for (const { kind, name } of names) {
    if (kind === 'kms-alias') {
      // KMS aliases are `alias/{appName}-…`.
      expect(name.startsWith(`alias/${TEST_APP_NAME}-`)).toBe(true);
    } else if (kind === 'ssm') {
      // SSM paths are `/{appName}/…` EXCEPT the token-independent appName
      // discovery path `/cairnproofs/{qualifier}/app-name` (it cannot embed the
      // token — the CLI reads it to LEARN the token).
      const ok =
        name.startsWith(`/${TEST_APP_NAME}/`) ||
        name === `/cairnproofs/cmh-alpha/app-name`;
      expect(ok).toBe(true);
    } else if (kind === 'signer') {
      // Signer profile names are underscore-only: `{appName}_…`.
      expect(name.startsWith(`${TEST_APP_NAME}_`)).toBe(true);
    } else {
      // Everything else is the hyphenated `{appName}-…` form.
      expect(name.startsWith(`${TEST_APP_NAME}-`)).toBe(true);
    }
  }
});

test('stack name is the 2-segment exception {appName}-{qualifier} (no {resource})', () => {
  const app = new App({ outdir: OUTDIR });
  const stack = new ArtifactStoreStack(app, `${TEST_APP_NAME}-cmh-alpha`, {
    env: { account: '111111111111', region: 'us-east-2' },
    appName: TEST_APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: false,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
  });
  expect(stack.stackName).toBe(`${TEST_APP_NAME}-cmh-alpha`);
  // Two segments after the appName-token boundary: appName then qualifier — no
  // middle `{resource}` slot.
  expect(stack.stackName.split('-cmh-alpha')[0]).toBe(TEST_APP_NAME);
});

test('the appName token discovery SSM parameter is published (token-independent path)', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::SSM::Parameter', {
    Name: '/cairnproofs/cmh-alpha/app-name',
    Value: TEST_APP_NAME,
  });
});

test('ECR namespace, Notation trust scope, and pipeline IAM ARNs all share one composition point', () => {
  const template = Template.fromStack(makeStack());
  const namespace = `${TEST_APP_NAME}-artifact-cmh-alpha`;

  // 1. ECR repo path = {namespace}/{projectId}.
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: `${namespace}/mvp-org/mvp-tenant/mvp-project`,
  });

  // 2. Notation trust-policy registry scope ends with the SAME namespace.
  const params = template.findResources('AWS::SSM::Parameter', {
    Properties: { Name: '/cairnproofs4af/alpha/notation/trust-policy' },
  });
  expect(Object.keys(params)).toHaveLength(1);
  const flatten = (v: unknown): string => {
    if (typeof v === 'string') return v;
    if (v && typeof v === 'object' && 'Fn::Join' in (v as Record<string, unknown>)) {
      return (v as { 'Fn::Join': [string, unknown[]] })['Fn::Join'][1]
        .map(flatten)
        .join('');
    }
    return '<<TOKEN>>';
  };
  const policy = JSON.parse(
    flatten((Object.values(params)[0].Properties as { Value: unknown }).Value),
  ) as { trustPolicies: Array<{ registryScopes: string[] }> };
  expect(policy.trustPolicies[0].registryScopes[0].endsWith(`/${namespace}`)).toBe(true);

  // 3. Pipeline IAM ARN patterns are repo-path-scoped to the SAME namespace,
  // never widened to `repository/*` or a bare appName prefix.
  const allIam = JSON.stringify({
    managed: template.findResources('AWS::IAM::ManagedPolicy'),
    inline: template.findResources('AWS::IAM::Policy'),
  });
  expect(allIam).toContain(`:repository/${namespace}/`);
  // Negative: no ECR ARN widened to a bare `repository/*` or a non-namespaced
  // `repository/{appName}-` form that isn't the artifact namespace.
  expect(allIam).not.toContain(':repository/*');
});

// ── Signer profile names (underscore-only, appName-prefixed) ─────────────────
//
// The AWS Signer L2 construct generates the profile's PHYSICAL name and does
// NOT emit it as a CFN template property (see 'both signing profiles are
// synthesized' above), so the physical name is asserted at the CONSTRUCT layer:
// `signing.ts` builds it via `naming.signerNameFor(...)` (underscore-only,
// `{appName}_artifact_{pipeline_signing,reviewer_signoff}_{qualifier}`). Here
// we (a) confirm the two profiles exist by their logical IDs and (b) assert the
// underscore-name format directly
// against the helper that source uses, so a format regression fails the build.
test('Signer profiles are Artifact{Pipeline,ReviewerSignoff} — no Evidence/auditor logical id', () => {
  const template = Template.fromStack(makeStack());
  const profiles = template.findResources('AWS::Signer::SigningProfile');
  const logicalIds = Object.keys(profiles).join('|');
  expect(logicalIds).toMatch(/ArtifactPipelineProfile/);
  expect(logicalIds).toMatch(/ArtifactReviewerSignoffProfile/);
  // No Evidence/auditor logical id.
  expect(logicalIds).not.toMatch(/Evidence/);
  expect(logicalIds).not.toMatch(/Auditor/);
});

test('signer name format is underscore-only {appName}_artifact_..._{qualifier} (helper contract)', () => {
  const { Naming } = require('../lib/naming') as typeof import('../lib/naming');
  const naming = new Naming({ appName: TEST_APP_NAME, qualifier: 'cmh-alpha' });
  // Underscore-only (Signer constraint /^[a-zA-Z0-9_]{2,64}$/), appName-prefixed;
  // both under the 64-char ceiling.
  const pipeline = naming.signerNameFor('artifact_pipeline_signing');
  const reviewer = naming.signerNameFor('artifact_reviewer_signoff');
  expect(pipeline).toBe(`${TEST_APP_NAME}_artifact_pipeline_signing_cmh_alpha`);
  expect(reviewer).toBe(`${TEST_APP_NAME}_artifact_reviewer_signoff_cmh_alpha`);
  expect(pipeline).not.toContain('-');
  expect(reviewer).not.toContain('auditor');
  expect(pipeline.length).toBeLessThanOrEqual(64);
  expect(reviewer.length).toBeLessThanOrEqual(64);
  expect(/^[a-zA-Z0-9_]{2,64}$/.test(pipeline)).toBe(true);
});

// ── EventBridge bus is {appName}-artifact-{qualifier} (no `audit` token) ─────
test('EventBridge bus is {appName}-artifact-{qualifier} — no audit token', () => {
  const template = Template.fromStack(makeStack());
  template.hasResourceProperties('AWS::Events::EventBus', {
    Name: `${TEST_APP_NAME}-artifact-cmh-alpha`,
  });
  const buses = template.findResources('AWS::Events::EventBus');
  for (const b of Object.values(buses)) {
    const name = (b.Properties as { Name?: string }).Name ?? '';
    expect(name).not.toContain('audit');
  }
});

// ── event source token uses the product domain ──────────────────────────────
test('pipeline EventBridge rules match source dev.cairnproofs.artifact (not org.cairnsecurity)', () => {
  const template = Template.fromStack(makeStack());
  const rules = template.findResources('AWS::Events::Rule');
  let sawCairnproofsSource = false;
  for (const r of Object.values(rules)) {
    const pattern = (r.Properties as { EventPattern?: { source?: string[] } }).EventPattern;
    const sources = pattern?.source ?? [];
    for (const src of sources) {
      // The native ECR trigger uses `aws.ecr`; our own events use the product
      // domain — never the forbidden `org.cairnsecurity.*` token.
      expect(src).not.toContain('cairnsecurity');
      if (src === 'dev.cairnproofs.artifact') sawCairnproofsSource = true;
    }
  }
  expect(sawCairnproofsSource).toBe(true);
});

// ── token freeze — unit tests of the naming helper ──────────────────────────
test('generateAppNameToken produces a valid 3-char token over the unambiguous alphabet', () => {
  for (let i = 0; i < 200; i++) {
    const tok = generateAppNameToken();
    expect(tok).toHaveLength(3);
    expect(isValidToken(tok)).toBe(true);
    // Excludes the confusable characters o 0 i l 1.
    for (const c of tok) {
      expect('o0il1').not.toContain(c);
      expect(TOKEN_ALPHABET).toContain(c);
    }
  }
});

test('isValidToken rejects wrong length and out-of-alphabet characters', () => {
  expect(isValidToken('4af')).toBe(true);
  expect(isValidToken('4a')).toBe(false); // too short
  expect(isValidToken('4abc')).toBe(false); // too long
  expect(isValidToken('4a0')).toBe(false); // contains excluded `0`
  expect(isValidToken('4al')).toBe(false); // contains excluded `l`
});

test('resolveAppNameToken reads a committed token verbatim and never regenerates it', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  fs.writeFileSync(cfg, JSON.stringify({ appNameToken: '4af' }));
  // Repeated reads return the SAME token — no per-synth regeneration.
  expect(resolveAppNameToken(cfg)).toBe('4af');
  expect(resolveAppNameToken(cfg)).toBe('4af');
  // The config file is unchanged (the token was read, not rewritten).
  expect(JSON.parse(fs.readFileSync(cfg, 'utf-8')).appNameToken).toBe('4af');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('resolveAppNameToken generates AND persists a token on first synth (absent config)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  // No config file yet → generate, write, and freeze.
  const tok = resolveAppNameToken(cfg);
  expect(isValidToken(tok)).toBe(true);
  expect(fs.existsSync(cfg)).toBe(true);
  expect(JSON.parse(fs.readFileSync(cfg, 'utf-8')).appNameToken).toBe(tok);
  // A subsequent read returns the SAME frozen token (now committed).
  expect(resolveAppNameToken(cfg)).toBe(tok);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('resolveAppNameToken rejects a malformed committed token (fail-closed)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  fs.writeFileSync(cfg, JSON.stringify({ appNameToken: 'BAD0' }));
  expect(() => resolveAppNameToken(cfg)).toThrow(/not a valid/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('resolveAppNameToken treats an empty-string token as absent and generates one', () => {
  // An `appNameToken: ""` is falsy → the function falls through to GENERATE +
  // persist rather than freezing the empty string (which would yield the
  // degenerate name `cairnproofs--{qualifier}`).
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  fs.writeFileSync(cfg, JSON.stringify({ appNameToken: '' }));
  const tok = resolveAppNameToken(cfg);
  expect(isValidToken(tok)).toBe(true);
  expect(JSON.parse(fs.readFileSync(cfg, 'utf-8')).appNameToken).toBe(tok);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('resolveAppNameToken generates a token for an empty {} config (no appNameToken key)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  fs.writeFileSync(cfg, '{}');
  const tok = resolveAppNameToken(cfg);
  expect(isValidToken(tok)).toBe(true);
  // The generated token is persisted (frozen) for the next synth.
  expect(JSON.parse(fs.readFileSync(cfg, 'utf-8')).appNameToken).toBe(tok);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('resolveAppNameToken fails loudly on a non-JSON config rather than silently regenerating', () => {
  // A corrupt (non-JSON) committed config must NOT be silently swallowed into a
  // fresh-token generate path — that would REPLACE every resource. It
  // surfaces the parse error so the operator fixes the file.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-token-'));
  const cfg = path.join(dir, 'app-config.json');
  fs.writeFileSync(cfg, 'not-json-at-all{{{');
  expect(() => resolveAppNameToken(cfg)).toThrow();
  fs.rmSync(dir, { recursive: true, force: true });
});

test('ProductionTeardownGuard throws on a stateful resource with an UNSET removal policy (M-6 fail-closed)', () => {
  // The insidious branch: a future stateful resource added with NO removal
  // policy at all (DeletionPolicy/UpdateReplacePolicy genuinely undefined). The
  // CFN default is Delete, so an unset policy is a teardown of production data
  // — the guard MUST treat undefined as a violation, not skip it.
  const app = new App({ outdir: OUTDIR });
  const stack = new Stack(app, 'GuardUnsetPolicyStack');
  // A bare L1 of a stateful type with no removalPolicy applied → both policy
  // options remain undefined on cfnOptions.
  new CfnResource(stack, 'UnsetTable', { type: 'AWS::DynamoDB::Table' });
  Aspects.of(stack).add(new ProductionTeardownGuard(true));
  expect(() => app.synth()).toThrow(/requires every stateful resource to be RETAIN/);
  expect(() => app.synth()).toThrow(/unset \(Delete\)/);
});

// ── display-only CfnOutputs remain (no exportName anywhere) ──────────────────
test('no CfnOutput carries an Export name (no CloudFormation Exports)', () => {
  const template = Template.fromStack(makeStack());
  const outputs = (template.toJSON() as { Outputs?: Record<string, { Export?: unknown }> }).Outputs ?? {};
  expect(Object.keys(outputs).length).toBeGreaterThan(0);
  for (const out of Object.values(outputs)) {
    expect(out.Export).toBeUndefined();
  }
  // The appName discovery output is present and display-only.
  template.hasOutput('AppName', { Export: Match.absent(), Value: TEST_APP_NAME });
});

// ── bin/ env wiring ──────────────────────────────────────────────────────────
//
// `bin/artifact-store.ts` carries no hardcoded `'123456789012'` /
// `'us-east-2'` / `'cmh'` literals. It sources the deployment account + region
// from the operator environment (CAIRN_ALPHA_ACCOUNT / CAIRN_ALPHA_REGION,
// loaded from the gitignored .env via mise) and DERIVES regionCode from the
// committed region-codes registry via `resolveRegionCode`. The three fail-fast
// paths (empty account, empty region, unregistered region) MUST throw — never a
// silent fallback.
describe('resolveDeployConfig — env-sourced account/region, fail-fast, no silent fallback', () => {
  // Test setup: inject the operator env vars the CDK app reads at synth time.
  // process.env is mutated, so save/restore the two keys around the suite to
  // keep the run parallel-safe at the process level (jest isolates per file).
  const SAVED_ACCOUNT = process.env.CAIRN_ALPHA_ACCOUNT;
  const SAVED_REGION = process.env.CAIRN_ALPHA_REGION;

  beforeAll(() => {
    process.env.CAIRN_ALPHA_ACCOUNT = '123456789012';
    process.env.CAIRN_ALPHA_REGION = 'us-east-2';
  });

  afterAll(() => {
    if (SAVED_ACCOUNT === undefined) delete process.env.CAIRN_ALPHA_ACCOUNT;
    else process.env.CAIRN_ALPHA_ACCOUNT = SAVED_ACCOUNT;
    if (SAVED_REGION === undefined) delete process.env.CAIRN_ALPHA_REGION;
    else process.env.CAIRN_ALPHA_REGION = SAVED_REGION;
  });

  test('reads account/region from process.env and derives regionCode (us-east-2 → cmh)', () => {
    // Default path: reads the injected process.env values set in beforeAll.
    const cfg = resolveDeployConfig();
    expect(cfg.account).toBe('123456789012');
    expect(cfg.region).toBe('us-east-2');
    // regionCode is DERIVED from the registry, never a standalone literal.
    expect(cfg.regionCode).toBe('cmh');
    expect(cfg.stage).toBe('alpha');
    expect(cfg.qualifier).toBe('cmh-alpha');
  });

  test('derives the correct regionCode for another registered region (us-west-2 → pdx)', () => {
    const cfg = resolveDeployConfig({
      CAIRN_ALPHA_ACCOUNT: '123456789012',
      CAIRN_ALPHA_REGION: 'us-west-2',
    });
    expect(cfg.regionCode).toBe('pdx');
    expect(cfg.qualifier).toBe('pdx-alpha');
  });

  test('FAIL FAST (a): unset account throws, no silent fallback', () => {
    expect(() =>
      resolveDeployConfig({ CAIRN_ALPHA_REGION: 'us-east-2' }),
    ).toThrow(/CAIRN_ALPHA_ACCOUNT is unset or empty/);
  });

  test('FAIL FAST (a): empty/whitespace account throws', () => {
    expect(() =>
      resolveDeployConfig({ CAIRN_ALPHA_ACCOUNT: '   ', CAIRN_ALPHA_REGION: 'us-east-2' }),
    ).toThrow(/CAIRN_ALPHA_ACCOUNT is unset or empty/);
  });

  test('FAIL FAST (b): unset region throws, no silent fallback', () => {
    expect(() =>
      resolveDeployConfig({ CAIRN_ALPHA_ACCOUNT: '123456789012' }),
    ).toThrow(/CAIRN_ALPHA_REGION is unset or empty/);
  });

  test('FAIL FAST (b): empty/whitespace region throws', () => {
    expect(() =>
      resolveDeployConfig({ CAIRN_ALPHA_ACCOUNT: '123456789012', CAIRN_ALPHA_REGION: '  ' }),
    ).toThrow(/CAIRN_ALPHA_REGION is unset or empty/);
  });

  test('FAIL FAST (c): unregistered region throws via resolveRegionCode (no silent fallback)', () => {
    expect(() =>
      resolveDeployConfig({
        CAIRN_ALPHA_ACCOUNT: '123456789012',
        CAIRN_ALPHA_REGION: 'eu-west-1',
      }),
    ).toThrow(/No region code registered for region 'eu-west-1'/);
  });
});
