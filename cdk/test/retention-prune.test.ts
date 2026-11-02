import * as path from 'path';
import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import {
  INTEG_CLEANUP_PRINCIPALS_ENV,
  PRUNER_PRINCIPALS_ENV,
  resolveIntegCleanupPrincipalArns,
  resolvePrunerPrincipalArns,
} from '../bin/artifact-store';
import { ArtifactStoreStack, ArtifactStoreStackProps } from '../lib/artifact-store-stack';

// Retention, prune, and deletion-alarm surface (change retention-expiry-and-prune).
// A distinct cloud-assembly directory from artifact-store.test.ts: jest runs the
// two files in parallel workers, and a shared outdir would race.
const OUTDIR = path.join(__dirname, '..', 'cdk.out.test-retention');

const ACCOUNT = '111111111111';
const REGION = 'us-east-2';
const APP_NAME = 'cairnproofs4af';
const PRUNERS = ['arn:aws:iam::111111111111:user/dana', 'arn:aws:iam::111111111111:user/robin'];
const PROJECT_PREFIX = 'mvp-org/mvp-tenant/mvp-project';
const PROJECT_REPO_ARN = `arn:aws:ecr:${REGION}:${ACCOUNT}:repository/${APP_NAME}-artifact-cmh-alpha/${PROJECT_PREFIX}`;
const ARCHIVE_BUCKET = `${APP_NAME}-artifact-archive-cmh-alpha`;
const RETENTION_PARAM = `/${APP_NAME}/alpha/retention/period-days`;
const WINDOW_PARAM = `/${APP_NAME}/alpha/retention/unarchived-manifest-window-days`;

function makeStack(overrides: Partial<ArtifactStoreStackProps> = {}): ArtifactStoreStack {
  const app = new App({ outdir: OUTDIR });
  return new ArtifactStoreStack(app, 'RetentionStack', {
    env: { account: ACCOUNT, region: REGION },
    appName: APP_NAME,
    regionCode: 'cmh',
    stage: 'alpha',
    orgId: 'mvp-org',
    tenantId: 'mvp-tenant',
    projectId: 'mvp-project',
    isProd: false,
    wormEnabled: false,
    notificationEmail: 'test@example.com',
    prunerPrincipalArns: PRUNERS,
    ...overrides,
  });
}

type Statement = {
  Sid?: string;
  Effect: string;
  Action: string | string[];
  Resource?: unknown;
  Principal?: unknown;
  NotPrincipal?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
};

const actionsOf = (s: Statement): string[] => (Array.isArray(s.Action) ? s.Action : [s.Action]);

// Synthesize the two stacks once; every read-only assertion shares them.
let alpha: Template;
let prod: Template;
beforeAll(() => {
  alpha = Template.fromStack(makeStack());
  prod = Template.fromStack(makeStack({ isProd: true }));
});

/** Every statement of every inline policy attached to a role whose logical id starts with `prefix`. */
function roleStatements(template: Template, prefix: string): Statement[] {
  const out: Statement[] = [];
  for (const r of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const props = r.Properties as {
      PolicyDocument: { Statement: Statement[] };
      Roles?: Array<{ Ref?: string }>;
    };
    if ((props.Roles ?? []).some((x) => (x.Ref ?? '').startsWith(prefix))) {
      out.push(...props.PolicyDocument.Statement);
    }
  }
  return out;
}

function roleByName(template: Template, name: string): { Properties: Record<string, unknown> } {
  const roles = Object.values(template.findResources('AWS::IAM::Role')).filter(
    (r) => (r.Properties as { RoleName?: string }).RoleName === name,
  );
  expect(roles).toHaveLength(1);
  return roles[0] as { Properties: Record<string, unknown> };
}

function operatorStatements(template: Template): Statement[] {
  const managed = Object.values(template.findResources('AWS::IAM::ManagedPolicy')).filter(
    (p) => (p.Properties as { ManagedPolicyName?: string }).ManagedPolicyName === `${APP_NAME}-operator-cmh-alpha`,
  );
  expect(managed).toHaveLength(1);
  return (managed[0].Properties as { PolicyDocument: { Statement: Statement[] } }).PolicyDocument.Statement;
}

/** Every string literal anywhere inside `value`. */
function stringsIn(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(stringsIn);
  if (value && typeof value === 'object') return Object.values(value).flatMap(stringsIn);
  return [];
}

function repositoryPolicyText(template: Template): unknown {
  const repos = Object.values(template.findResources('AWS::ECR::Repository'));
  expect(repos).toHaveLength(1);
  return (repos[0].Properties as { RepositoryPolicyText: unknown }).RepositoryPolicyText;
}

function repositoryPolicy(template: Template): Statement[] {
  const repos = Object.values(template.findResources('AWS::ECR::Repository'));
  expect(repos).toHaveLength(1);
  const props = repos[0].Properties as { RepositoryPolicyText: { Statement: Statement[] } };
  return props.RepositoryPolicyText.Statement;
}

// ── [RETAIN-007] retention settings ───────────────────────────────────────────

describe('[RETAIN-007] retention settings and bounds', () => {
  test.each([0, 36136, 1.5, Number.NaN])(
    '[RETAIN-007.01] retention period %p fails synth naming the bounds',
    (days) => {
      expect(() => makeStack({ retentionPeriodDays: days })).toThrow(/1 to 36135/);
    },
  );

  test.each([0, 366, 2.5])(
    '[RETAIN-007.02] unarchived-manifest window %p fails synth naming the bounds',
    (days) => {
      expect(() => makeStack({ unarchivedManifestWindowDays: days })).toThrow(/1 to 365/);
    },
  );

  test('the bounds themselves synthesize (1 and 36135 days, 1 and 365 window)', () => {
    expect(() => makeStack({ retentionPeriodDays: 1, unarchivedManifestWindowDays: 365 })).not.toThrow();
    expect(() => makeStack({ retentionPeriodDays: 36135, unarchivedManifestWindowDays: 1 })).not.toThrow();
  });

  test('default synth publishes the 2555-day period and the 30-day window to SSM', () => {
    alpha.hasResourceProperties('AWS::SSM::Parameter', { Name: RETENTION_PARAM, Value: '2555' });
    alpha.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `/${APP_NAME}/alpha/retention/unarchived-manifest-window-days`,
      Value: '30',
    });
  });

  test('an explicit retention period is published verbatim', () => {
    const t = Template.fromStack(makeStack({ retentionPeriodDays: 10, unarchivedManifestWindowDays: 7 }));
    t.hasResourceProperties('AWS::SSM::Parameter', { Name: RETENTION_PARAM, Value: '10' });
  });

  test('archive-to-s3 reads the period by name and holds ssm:GetParameter on it only', () => {
    alpha.hasResourceProperties('AWS::Lambda::Function', {
      FunctionName: `${APP_NAME}-archive-to-s3-cmh-alpha`,
      Environment: {
        Variables: Match.objectLike({ RETENTION_PERIOD_PARAM: RETENTION_PARAM }),
      },
    });
    const fns = Object.values(alpha.findResources('AWS::Lambda::Function'));
    for (const fn of fns) {
      const vars = ((fn.Properties as { Environment?: { Variables?: Record<string, unknown> } })
        .Environment?.Variables ?? {}) as Record<string, unknown>;
      expect(vars).not.toHaveProperty('WORM_RETENTION_YEARS');
    }
    const ssm = roleStatements(alpha, 'LambdaFunctionsArchiveToS3ServiceRole').filter((s) =>
      actionsOf(s).some((a) => a.startsWith('ssm:')),
    );
    expect(ssm).toHaveLength(1);
    expect(actionsOf(ssm[0])).toEqual(['ssm:GetParameter']);
    expect(JSON.stringify(ssm[0].Resource)).toContain(`parameter${RETENTION_PARAM}`);
  });
});

// ── [RETAIN-006] no automatic deletion ───────────────────────────────────────

describe('[RETAIN-006] no automatic deletion', () => {
  test('[RETAIN-006.01] no repository lifecycle policy; archive lifecycle is transitions only', () => {
    for (const t of [alpha, prod]) {
      t.allResources('AWS::ECR::Repository', { LifecyclePolicy: Match.absent() });
      const buckets = Object.values(t.findResources('AWS::S3::Bucket')).filter(
        (b) => (b.Properties as { BucketName?: string }).BucketName === ARCHIVE_BUCKET,
      );
      expect(buckets).toHaveLength(1);
      const rules = (buckets[0].Properties as {
        LifecycleConfiguration: { Rules: Array<Record<string, unknown>> };
      }).LifecycleConfiguration.Rules;
      expect(rules.length).toBeGreaterThan(0);
      for (const rule of rules) {
        for (const key of [
          'ExpirationInDays',
          'ExpirationDate',
          'ExpiredObjectDeleteMarker',
          'NoncurrentVersionExpiration',
          'NoncurrentVersionExpirationInDays',
          'AbortIncompleteMultipartUpload',
        ]) {
          expect(rule).not.toHaveProperty(key);
        }
        expect(rule.Transitions).toBeDefined();
      }
    }
  });

  test('archive bucket keeps objects under 128 KB (receipt copies) out of every transition', () => {
    for (const t of [alpha, prod]) {
      t.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: ARCHIVE_BUCKET,
        LifecycleConfiguration: Match.objectLike({
          TransitionDefaultMinimumObjectSize: 'all_storage_classes_128K',
        }),
      });
    }
  });

  test('archive bucket does not enable S3 Bucket Keys (pruner decrypt condition relies on it)', () => {
    const bucket = Object.values(alpha.findResources('AWS::S3::Bucket')).find(
      (b) => (b.Properties as { BucketName?: string }).BucketName === ARCHIVE_BUCKET,
    )!;
    const sse = (bucket.Properties as {
      BucketEncryption: { ServerSideEncryptionConfiguration: Array<{ BucketKeyEnabled?: boolean }> };
    }).BucketEncryption.ServerSideEncryptionConfiguration;
    for (const rule of sse) {
      expect(rule.BucketKeyEnabled).not.toBe(true);
    }
  });

  test('access-log bucket keeps logs for at least the retention period (M10)', () => {
    const t = Template.fromStack(makeStack({ retentionPeriodDays: 4000 }));
    t.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `${APP_NAME}-artifact-archive-logs-cmh-alpha`,
      LifecycleConfiguration: { Rules: [Match.objectLike({ ExpirationInDays: 4000 })] },
    });
  });
});

// ── [ACCESS-010] stack-owned repository policy ───────────────────────────────

describe('[ACCESS-010] stack-owned repository policy', () => {
  const bySid = (stmts: Statement[], sid: string) => {
    const s = stmts.find((x) => x.Sid === sid);
    expect(s).toBeDefined();
    return s!;
  };

  const CFN_EXEC_ROLE = `cdk-hnb659fds-cfn-exec-role-${ACCOUNT}-${REGION}`;

  test('[ACCESS-010.01] BatchDeleteImage is denied to all but the pruner and (non-prod) cleanup and teardown roles', () => {
    const stmt = bySid(repositoryPolicy(alpha), 'DenyImageDeleteExceptPruner');
    expect(stmt.Effect).toBe('Deny');
    expect(stmt.Principal).toEqual({ AWS: '*' });
    expect(actionsOf(stmt)).toEqual(['ecr:BatchDeleteImage']);
    const except = stmt.Condition!.ArnNotEquals['aws:PrincipalArn'] as unknown[];
    expect(except).toHaveLength(3);
    const text = JSON.stringify(except);
    expect(text).toContain('PrunerRole');
    expect(text).toContain('IntegrationCleanupRole');
    // emptyOnDelete teardown deletes the remaining images as the deployment role.
    expect(text).toContain(CFN_EXEC_ROLE);
  });

  test('isProd=true synthesizes no cleanup-role exception, role, or SSM parameter', () => {
    const stmt = bySid(repositoryPolicy(prod), 'DenyImageDeleteExceptPruner');
    const except = JSON.stringify(stmt.Condition!.ArnNotEquals['aws:PrincipalArn']);
    expect(except).toContain('PrunerRole');
    expect(except).not.toContain('IntegrationCleanupRole');
    // A production repository is retained, so the deployment role never deletes images.
    expect(except).not.toContain(CFN_EXEC_ROLE);
    const names = Object.values(prod.findResources('AWS::IAM::Role')).map(
      (r) => (r.Properties as { RoleName?: string }).RoleName,
    );
    expect(names).not.toContain(`${APP_NAME}-integ-cleanup-cmh-alpha`);
    prod.resourcePropertiesCountIs(
      'AWS::SSM::Parameter',
      { Name: `/${APP_NAME}/alpha/integration/cleanup-role-arn` },
      0,
    );
  });

  test('[ACCESS-010.02] lifecycle and repository-policy changes are denied to all but the deployment role', () => {
    const stmt = bySid(repositoryPolicy(alpha), 'DenyRepositoryChangesExceptDeployment');
    expect(stmt.Effect).toBe('Deny');
    expect(actionsOf(stmt).sort()).toEqual(
      [
        'ecr:DeleteLifecyclePolicy',
        'ecr:DeleteRepository',
        'ecr:DeleteRepositoryPolicy',
        'ecr:PutLifecyclePolicy',
        'ecr:SetRepositoryPolicy',
      ].sort(),
    );
    // The exception is the synthesizer's CloudFormation execution role (M37),
    // with the partition resolved through the AWS::Partition pseudo-parameter.
    expect(stmt.Condition!.ArnNotEquals['aws:PrincipalArn']).toEqual({
      'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, `:iam::${ACCOUNT}:role/${CFN_EXEC_ROLE}`]],
    });
  });

  test('no literal ${AWS::...} placeholder survives anywhere in the repository policy', () => {
    // CloudFormation substitutes placeholders only inside Fn::Sub. A literal one
    // inside RepositoryPolicyText would make an aws:PrincipalArn exception match
    // no principal, locking the repository policy for everyone.
    for (const t of [alpha, prod]) {
      const policy = repositoryPolicyText(t);
      expect(JSON.stringify(policy)).not.toContain('Fn::Sub');
      for (const s of stringsIn(policy)) {
        expect(s).not.toContain('${AWS::');
      }
    }
  });

  test('[ACCESS-010.04] archive storage class is denied to every principal, with no exception', () => {
    const stmt = bySid(repositoryPolicy(alpha), 'DenyArchiveStorageClass');
    expect(stmt.Effect).toBe('Deny');
    expect(actionsOf(stmt)).toEqual(['ecr:UpdateImageStorageClass']);
    expect(stmt.Condition).toBeUndefined();
    // Repository deletion is in the deployment-only deny (asserted above).
    expect(actionsOf(bySid(repositoryPolicy(alpha), 'DenyRepositoryChangesExceptDeployment'))).toContain(
      'ecr:DeleteRepository',
    );
  });

  test('[ACCESS-010.03] the policy has no Allow and never uses NotPrincipal', () => {
    for (const t of [alpha, prod]) {
      const stmts = repositoryPolicy(t);
      expect(stmts).toHaveLength(3);
      for (const s of stmts) {
        expect(s.Effect).toBe('Deny');
        expect(s.NotPrincipal).toBeUndefined();
        if (s.Condition) {
          expect(Object.keys(s.Condition)).toEqual(['ArnNotEquals']);
          expect(Object.keys(s.Condition.ArnNotEquals)).toEqual(['aws:PrincipalArn']);
        }
      }
    }
  });
});

// ── [ACCESS-007] / [ACCESS-008] pruner role ──────────────────────────────────

describe('[ACCESS-007] / [ACCESS-008] pruner role', () => {
  const PRUNER_ROLE = `${APP_NAME}-pruner-cmh-alpha`;
  const pruner = () => roleStatements(alpha, 'IamPolicyTemplatesPrunerRole');
  const allows = () => pruner().filter((s) => s.Effect === 'Allow');

  test('[ACCESS-007.01] trust admits exactly the designated principals; one-hour sessions', () => {
    const role = roleByName(alpha, PRUNER_ROLE);
    expect(role.Properties.MaxSessionDuration).toBe(3600);
    const trust = (role.Properties.AssumeRolePolicyDocument as { Statement: Statement[] }).Statement;
    expect(trust).toHaveLength(1);
    expect(trust[0].Effect).toBe('Allow');
    expect(trust[0].Principal).toEqual({ AWS: PRUNERS });
    expect(actionsOf(trust[0]).sort()).toEqual(['sts:AssumeRole', 'sts:SetSourceIdentity']);
  });

  test('[ACCESS-007.03] trust requires MFA (Bool, never IfExists) and a caller-bound source identity', () => {
    const role = roleByName(alpha, PRUNER_ROLE);
    const trust = (role.Properties.AssumeRolePolicyDocument as { Statement: Statement[] }).Statement[0];
    expect(trust.Condition).toEqual({
      Bool: { 'aws:MultiFactorAuthPresent': 'true' },
      StringEquals: { 'sts:SourceIdentity': '${aws:username}' },
    });
  });

  test('[ACCESS-007.04] every grant is scoped to this project (repo, prefix, tenant rows)', () => {
    const allow = allows();
    const ecr = allow.filter((s) => actionsOf(s).some((a) => a.startsWith('ecr:')));
    expect(ecr).toHaveLength(1);
    expect(ecr[0].Resource).toBe(PROJECT_REPO_ARN);
    expect(actionsOf(ecr[0])).toContain('ecr:BatchDeleteImage');

    const s3 = allow.filter((s) => actionsOf(s).some((a) => a.startsWith('s3:')));
    for (const s of s3) {
      const res = JSON.stringify(s.Resource);
      if (actionsOf(s).some((a) => a.startsWith('s3:List'))) {
        expect(s.Condition).toEqual({ StringLike: { 's3:prefix': [`${PROJECT_PREFIX}/*`] } });
      } else if (!actionsOf(s).includes('s3:GetBucketObjectLockConfiguration')) {
        expect(res).toContain(`/${PROJECT_PREFIX}/*`);
      }
    }
    const del = s3.find((s) => actionsOf(s).includes('s3:DeleteObjectVersion'))!;
    expect(JSON.stringify(del.Resource)).toContain(`/${PROJECT_PREFIX}/*`);

    const ddb = allow.filter((s) => actionsOf(s).some((a) => a.startsWith('dynamodb:')));
    expect(ddb).toHaveLength(1);
    expect(actionsOf(ddb[0]).sort()).toEqual(['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:Query']);
    expect(ddb[0].Condition).toEqual({
      'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['mvp-tenant'] },
    });
  });

  test('kms:Decrypt is exactly two narrow statements: the S3 receipt copy and the two index tables', () => {
    const kms = pruner().filter((s) => actionsOf(s).some((a) => a.startsWith('kms:')));
    expect(kms).toHaveLength(2);
    for (const s of kms) {
      expect(s.Effect).toBe('Allow');
      expect(actionsOf(s)).toEqual(['kms:Decrypt']);
    }
    const viaS3 = kms.find(
      (s) => s.Condition!.StringEquals['kms:ViaService'] === `s3.${REGION}.amazonaws.com`,
    )!;
    expect(viaS3).toBeDefined();
    expect(viaS3.Condition!.StringLike['kms:EncryptionContext:aws:s3:arn']).toEqual(
      expect.objectContaining({}),
    );
    expect(JSON.stringify(viaS3.Condition!.StringLike['kms:EncryptionContext:aws:s3:arn'])).toContain(
      `/${PROJECT_PREFIX}/*/worm-receipt.json`,
    );
    const viaDdb = kms.find(
      (s) => s.Condition!.StringEquals['kms:ViaService'] === `dynamodb.${REGION}.amazonaws.com`,
    )!;
    expect(viaDdb).toBeDefined();
    expect(Object.keys(viaDdb.Condition!)).toEqual(['StringEquals']);
    const tables = viaDdb.Condition!.StringEquals['kms:EncryptionContext:aws:dynamodb:tableName'];
    expect(Array.isArray(tables)).toBe(true);
    expect(JSON.stringify(tables)).toContain('MetadataTable');
    expect(JSON.stringify(tables)).toContain('ControlClaimsTable');
    // No decrypt via ECR, and no unconditioned decrypt.
    expect(JSON.stringify(kms)).not.toContain('ecr.');
  });

  test('the only object body the pruner may read is the S3 receipt copy', () => {
    const reads = allows().filter((s) => actionsOf(s).includes('s3:GetObject'));
    expect(reads).toHaveLength(1);
    expect(JSON.stringify(reads[0].Resource)).toContain(`/${PROJECT_PREFIX}/*/worm-receipt.json`);
  });

  const RETENTION_ALTERING = [
    's3:BypassGovernanceRetention',
    's3:PutObjectRetention',
    's3:PutObjectLegalHold',
    's3:PutLifecycleConfiguration',
    'ecr:PutLifecyclePolicy',
    'ecr:DeleteLifecyclePolicy',
    'ssm:PutParameter',
  ];
  const AUDIT_ERASING = ['logs:DeleteLogGroup', 'logs:DeleteLogStream', 'logs:PutRetentionPolicy'];

  test('[ACCESS-008.01] no retention-altering grant; an explicit deny backs it', () => {
    const allowed = allows().flatMap(actionsOf);
    for (const a of RETENTION_ALTERING) {
      expect(allowed).not.toContain(a);
    }
    expect(allowed.some((a) => a.endsWith('*'))).toBe(false);
    const deny = pruner().find((s) => s.Sid === 'PrunerCannotAlterRetention')!;
    expect(deny.Effect).toBe('Deny');
    for (const a of RETENTION_ALTERING) {
      expect(actionsOf(deny)).toContain(a);
    }
  });

  test('[ACCESS-008.03] append-only audit access; cannot delete the group, streams, or retention', () => {
    const logs = allows().filter((s) => actionsOf(s).some((a) => a.startsWith('logs:')));
    expect(logs).toHaveLength(1);
    expect(actionsOf(logs[0]).sort()).toEqual(['logs:CreateLogStream', 'logs:PutLogEvents']);
    expect(JSON.stringify(logs[0].Resource)).toContain('PruneAudit');
    const deny = pruner().find((s) => s.Sid === 'PrunerCannotAlterRetention')!;
    for (const a of AUDIT_ERASING) {
      expect(actionsOf(deny)).toContain(a);
    }
  });

  test('pruner role ARN and audit log group name are published for the CLI', () => {
    alpha.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `/${APP_NAME}/alpha/prune/pruner-role-arn`,
      Value: Match.objectLike({ 'Fn::GetAtt': [Match.stringLikeRegexp('PrunerRole'), 'Arn'] }),
    });
    alpha.hasResourceProperties('AWS::SSM::Parameter', {
      Name: `/${APP_NAME}/alpha/prune/audit-log-group`,
      Value: Match.anyValue(),
    });
  });

  const cleanupTrust = (t: Template): Statement[] =>
    (roleByName(t, `${APP_NAME}-integ-cleanup-cmh-alpha`).Properties.AssumeRolePolicyDocument as {
      Statement: Statement[];
    }).Statement;

  test('cleanup role (non-prod) trusts only named principals, never the account root', () => {
    const principals = (trust: Statement[]): unknown[] =>
      trust.flatMap((s) => {
        expect(s.Effect).toBe('Allow');
        expect(actionsOf(s)).toEqual(['sts:AssumeRole']);
        expect(s.Condition).toBeUndefined();
        const aws = (s.Principal as { AWS: unknown }).AWS;
        return Array.isArray(aws) ? aws : [aws];
      });
    // Default: the designated pruners.
    const trusted = principals(cleanupTrust(alpha));
    expect(trusted.sort()).toEqual([...PRUNERS].sort());
    expect(JSON.stringify(trusted)).not.toContain(':root');
    expect(JSON.stringify(trusted)).not.toContain('"*"');
    // Explicit cleanup principals replace the default.
    const ci = 'arn:aws:iam::111111111111:role/integ-runner';
    const t = Template.fromStack(makeStack({ integCleanupPrincipalArns: [ci] }));
    expect(principals(cleanupTrust(t))).toEqual([ci]);
  });

  test('cleanup role refuses an empty principal list', () => {
    expect(() => makeStack({ integCleanupPrincipalArns: [] })).toThrow(/at least one principal/);
  });

  test('cleanup role holds ECR image deletes on the project repository only', () => {
    const stmts = roleStatements(alpha, 'IamPolicyTemplatesIntegrationCleanupRole');
    expect(stmts).toHaveLength(1);
    expect(stmts[0].Resource).toBe(PROJECT_REPO_ARN);
    expect(actionsOf(stmts[0])).toContain('ecr:BatchDeleteImage');
  });
});

describe('designated pruner principals from the environment', () => {
  const env = (value: string | undefined) => ({ [PRUNER_PRINCIPALS_ENV]: value });

  test('parses a comma-separated list of in-account IAM user ARNs', () => {
    expect(
      resolvePrunerPrincipalArns(
        ACCOUNT,
        env(' arn:aws:iam::111111111111:user/dana , arn:aws:iam::111111111111:user/path/robin '),
      ),
    ).toEqual(['arn:aws:iam::111111111111:user/dana', 'arn:aws:iam::111111111111:user/path/robin']);
  });

  test.each([undefined, '', ' , '])('fails synth when the list is empty (%p)', (value) => {
    expect(() => resolvePrunerPrincipalArns(ACCOUNT, env(value))).toThrow(/unset or empty/);
  });

  test.each([
    '*',
    'arn:aws:iam::111111111111:root',
    'arn:aws:iam::222222222222:user/mallory',
    'arn:aws:sts::111111111111:assumed-role/ops/session',
    'lambda.amazonaws.com',
    // A role has no aws:username, so it can never satisfy the SourceIdentity
    // condition, and it could name the Operator or a pipeline role.
    'arn:aws:iam::111111111111:role/path/ops',
    `arn:aws:iam::111111111111:role/${APP_NAME}-operator-cmh-alpha`,
  ])('rejects %p, which is not an in-account IAM user ARN', (value) => {
    expect(() =>
      resolvePrunerPrincipalArns(ACCOUNT, env(`arn:aws:iam::111111111111:user/dana,${value}`)),
    ).toThrow(/not an IAM user ARN/);
  });

  test('the stack itself refuses an empty principal list', () => {
    expect(() => makeStack({ prunerPrincipalArns: [] })).toThrow(/at least one/);
  });
});

describe('integration-test cleanup principals from the environment', () => {
  const env = (value: string | undefined) => ({ [INTEG_CLEANUP_PRINCIPALS_ENV]: value });

  test.each([undefined, '', ' , '])('defaults to the pruner principals when unset (%p)', (value) => {
    expect(resolveIntegCleanupPrincipalArns(ACCOUNT, PRUNERS, env(value))).toEqual(PRUNERS);
  });

  test('accepts in-account IAM user and role ARNs', () => {
    expect(
      resolveIntegCleanupPrincipalArns(
        ACCOUNT,
        PRUNERS,
        env('arn:aws:iam::111111111111:role/integ-runner, arn:aws:iam::111111111111:user/ci'),
      ),
    ).toEqual(['arn:aws:iam::111111111111:role/integ-runner', 'arn:aws:iam::111111111111:user/ci']);
  });

  test.each([
    '*',
    'arn:aws:iam::111111111111:root',
    'arn:aws:iam::222222222222:role/other-account',
    'arn:aws:sts::111111111111:assumed-role/ops/session',
  ])('rejects %p', (value) => {
    expect(() => resolveIntegCleanupPrincipalArns(ACCOUNT, PRUNERS, env(value))).toThrow(
      /not an IAM user or role ARN/,
    );
  });
});

// ── [ACCESS-009] Operator retention-configuration denials ────────────────────

describe('[ACCESS-009] Operator cannot change retention configuration', () => {
  const denied = (sid: string): Statement => {
    const s = operatorStatements(alpha).find((x) => x.Sid === sid);
    expect(s).toBeDefined();
    expect(s!.Effect).toBe('Deny');
    return s!;
  };
  const allowed = () =>
    operatorStatements(alpha)
      .filter((s) => s.Effect === 'Allow')
      .flatMap(actionsOf);

  test('[ACCESS-009.01] lifecycle on the repositories and the archive bucket is denied', () => {
    expect(allowed()).not.toContain('ecr:PutLifecyclePolicy');
    expect(actionsOf(denied('OperatorDenyEcrRetentionConfig'))).toEqual(
      expect.arrayContaining(['ecr:PutLifecyclePolicy', 'ecr:DeleteLifecyclePolicy']),
    );
    const s3 = denied('OperatorDenyArchiveRetentionConfig');
    expect(actionsOf(s3).sort()).toEqual(
      ['s3:PutBucketObjectLockConfiguration', 's3:PutLifecycleConfiguration', 's3:PutObjectLegalHold'].sort(),
    );
  });

  test('[ACCESS-009.02] retention parameter writes and archive-stage configuration are denied', () => {
    const ssm = denied('OperatorDenyRetentionParameterWrite');
    expect(actionsOf(ssm).sort()).toEqual(
      ['ssm:DeleteParameter', 'ssm:LabelParameterVersion', 'ssm:PutParameter'].sort(),
    );
    expect(JSON.stringify(ssm.Resource)).toContain(`parameter${RETENTION_PARAM}`);
    expect(JSON.stringify(ssm.Resource)).toContain(`parameter${WINDOW_PARAM}`);
    const fn = denied('OperatorDenyArchiveStageConfig');
    expect(actionsOf(fn)).toEqual(['lambda:UpdateFunctionConfiguration']);
    expect(JSON.stringify(fn.Resource)).toContain(`function:${APP_NAME}-archive-to-s3-cmh-alpha`);
  });

  test('[ACCESS-009.04] repository-policy set and delete are denied', () => {
    expect(allowed()).not.toContain('ecr:SetRepositoryPolicy');
    expect(actionsOf(denied('OperatorDenyEcrRetentionConfig'))).toEqual(
      expect.arrayContaining(['ecr:SetRepositoryPolicy', 'ecr:DeleteRepositoryPolicy']),
    );
  });

  test('[ACCESS-009.05] every resource on the alarm path is locked: rules, alarms, topic', () => {
    const rules = denied('OperatorDenyRetentionAlarmRules');
    expect(actionsOf(rules).sort()).toEqual(
      ['events:DeleteRule', 'events:DisableRule', 'events:PutRule', 'events:PutTargets', 'events:RemoveTargets'].sort(),
    );
    const ruleResources = rules.Resource as unknown[];
    expect(ruleResources).toHaveLength(7);
    for (const r of ruleResources) {
      expect(JSON.stringify(r)).toContain('RetentionAlarm');
    }
    const alarms = denied('OperatorDenyDeploymentAlarms');
    expect(actionsOf(alarms).sort()).toEqual(
      ['cloudwatch:DeleteAlarms', 'cloudwatch:DisableAlarmActions', 'cloudwatch:PutMetricAlarm'].sort(),
    );
    expect(JSON.stringify(alarms.Resource)).toContain(`alarm:${APP_NAME}-*`);
    const topic = denied('OperatorDenyAlarmTopicChanges');
    expect(actionsOf(topic).sort()).toEqual(
      ['sns:DeleteTopic', 'sns:RemovePermission', 'sns:SetTopicAttributes', 'sns:Unsubscribe'].sort(),
    );
    expect(JSON.stringify(topic.Resource)).toContain('AlarmTopic');
  });
});

// ── [ACCESS-007.02] / [ACCESS-009.03] pipeline grants ────────────────────────

describe('pipeline roles cannot delete evidence or set legal holds', () => {
  const PIPELINE_ROLES = ['ValidateBundle', 'SignBundle', 'ArchiveToS3', 'UpdateMetadata', 'MintTag'];
  const FORBIDDEN = [
    'ecr:BatchDeleteImage',
    's3:DeleteObjectVersion',
    's3:DeleteObject',
    'dynamodb:DeleteItem',
    'dynamodb:BatchWriteItem',
    's3:PutObjectLegalHold',
  ];
  const covers = (granted: string, wanted: string): boolean => {
    if (granted === '*') return true;
    const re = new RegExp(`^${granted.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*')}$`);
    return re.test(wanted);
  };

  test.each(PIPELINE_ROLES)('[ACCESS-007.02] [ACCESS-009.03] %s holds no delete or legal-hold action', (fn) => {
    const stmts = roleStatements(alpha, `LambdaFunctions${fn}ServiceRole`).filter(
      (s) => s.Effect === 'Allow',
    );
    expect(stmts.length).toBeGreaterThan(0);
    for (const granted of stmts.flatMap(actionsOf)) {
      for (const wanted of FORBIDDEN) {
        expect({ granted, wanted, covered: covers(granted, wanted) }).toEqual({
          granted,
          wanted,
          covered: false,
        });
      }
    }
  });

  test('archive-to-s3 writes objects with PutObject + PutObjectRetention only', () => {
    const puts = roleStatements(alpha, 'LambdaFunctionsArchiveToS3ServiceRole')
      .flatMap(actionsOf)
      .filter((a) => a.startsWith('s3:Put'));
    expect(puts.sort()).toEqual(['s3:PutObject', 's3:PutObjectRetention']);
  });

  test('update-metadata keeps Get/Put/Update/Query on both index tables', () => {
    const ddb = roleStatements(alpha, 'LambdaFunctionsUpdateMetadataServiceRole').filter((s) =>
      actionsOf(s).some((a) => a.startsWith('dynamodb:')),
    );
    const res = JSON.stringify(ddb.map((s) => s.Resource));
    expect(res).toContain('MetadataTable');
    expect(res).toContain('ControlClaimsTable');
    for (const s of ddb) {
      expect(actionsOf(s)).toEqual(
        expect.arrayContaining(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query']),
      );
    }
  });
});

// ── [RETAIN-011] prune audit log group ───────────────────────────────────────

test('[RETAIN-011] prune audit log group never expires and is stack-owned', () => {
  const groups = Object.values(alpha.findResources('AWS::Logs::LogGroup')).filter(
    (g) => (g.Properties as { LogGroupName?: string }).LogGroupName === `/${APP_NAME}/cmh-alpha/prune-audit`,
  );
  expect(groups).toHaveLength(1);
  expect(groups[0].Properties).not.toHaveProperty('RetentionInDays');
  // Production retains it on stack deletion.
  const prodGroup = Object.values(prod.findResources('AWS::Logs::LogGroup')).find(
    (g) => (g.Properties as { LogGroupName?: string }).LogGroupName === `/${APP_NAME}/cmh-alpha/prune-audit`,
  )!;
  expect(prodGroup.DeletionPolicy).toBe('Retain');
});

// ── [RETAIN-013] deletion and retention-configuration alarms ──────────────────

describe('[RETAIN-013] alarms on deletion and retention-configuration changes', () => {
  const rule = (resource: string) => {
    const rules = Object.values(alpha.findResources('AWS::Events::Rule')).filter(
      (r) => (r.Properties as { Name?: string }).Name === `${APP_NAME}-retention-alarm-${resource}-cmh-alpha`,
    );
    expect(rules).toHaveLength(1);
    return rules[0].Properties as {
      EventBusName?: unknown;
      EventPattern: { detail: Record<string, unknown> };
      Targets: Array<{ Arn: unknown; InputTransformer?: { InputPathsMap: Record<string, string>; InputTemplate: string } }>;
    };
  };

  const expectAlarmTarget = (props: ReturnType<typeof rule>) => {
    // Default bus (CloudTrail delivery), SNS notification topic target.
    expect(props.EventBusName).toBeUndefined();
    expect(props.Targets).toHaveLength(1);
    expect(JSON.stringify(props.Targets[0].Arn)).toContain('AlarmTopic');
    const paths = Object.values(props.Targets[0].InputTransformer!.InputPathsMap);
    expect(paths).toContain('$.detail.userIdentity.arn');
    expect(paths).toContain('$.detail.requestParameters');
  };

  test('[RETAIN-013.01] image deletion, archive storage class, and repository deletion alarm', () => {
    const r = rule('ecr-delete');
    expect(r.EventPattern.detail).toEqual({
      eventSource: ['ecr.amazonaws.com'],
      eventName: ['BatchDeleteImage', 'UpdateImageStorageClass', 'DeleteRepository'],
      requestParameters: { repositoryName: [{ prefix: `${APP_NAME}-artifact-cmh-alpha/` }] },
    });
    expectAlarmTarget(r);
  });

  test('[RETAIN-013.02] lifecycle or repository-policy change, and bucket lifecycle, alarm', () => {
    const ecr = rule('ecr-policy');
    expect(ecr.EventPattern.detail.eventName).toEqual(
      expect.arrayContaining(['PutLifecyclePolicy', 'SetRepositoryPolicy', 'DeleteRepositoryPolicy']),
    );
    expectAlarmTarget(ecr);
    const s3 = rule('s3-config');
    expect(s3.EventPattern.detail.eventName).toEqual(
      expect.arrayContaining(['PutBucketLifecycle', 'PutObjectLockConfiguration']),
    );
    expect(JSON.stringify(s3.EventPattern.detail.requestParameters)).toContain('ArchiveArchiveBucket');
    expectAlarmTarget(s3);
    const ssm = rule('ssm-param');
    expect(ssm.EventPattern.detail.requestParameters).toEqual({
      name: [RETENTION_PARAM, WINDOW_PARAM],
    });
    expectAlarmTarget(ssm);
    expect(rule('ssm-params').EventPattern.detail.requestParameters).toEqual({
      names: [RETENTION_PARAM, WINDOW_PARAM],
    });
    const fn = rule('lambda-config');
    expect(fn.EventPattern.detail.eventName).toEqual([{ prefix: 'UpdateFunctionConfiguration' }]);
    expectAlarmTarget(fn);
  });

  test('[RETAIN-013.03] trail stop, delete, update, and event-selector change alarm', () => {
    const r = rule('cloudtrail');
    expect(r.EventPattern.detail).toEqual({
      eventSource: ['cloudtrail.amazonaws.com'],
      eventName: ['StopLogging', 'DeleteTrail', 'UpdateTrail', 'PutEventSelectors'],
    });
    expectAlarmTarget(r);
  });

  // Tester gap fill: the tests above use arrayContaining for several rules, so a
  // dropped event name (for example UnlabelParameterVersion or
  // DeleteBucketLifecycle) would go unnoticed. Pin each rule's full event set.
  test.each([
    ['ecr-policy', 'ecr.amazonaws.com', ['PutLifecyclePolicy', 'DeleteLifecyclePolicy', 'SetRepositoryPolicy', 'DeleteRepositoryPolicy']],
    [
      's3-config',
      's3.amazonaws.com',
      [
        'PutBucketLifecycle',
        'PutBucketLifecycleConfiguration',
        'DeleteBucketLifecycle',
        'PutObjectLockConfiguration',
        'PutBucketObjectLockConfiguration',
      ],
    ],
    ['ssm-param', 'ssm.amazonaws.com', ['PutParameter', 'LabelParameterVersion', 'UnlabelParameterVersion', 'DeleteParameter']],
    ['ssm-params', 'ssm.amazonaws.com', ['DeleteParameters']],
  ])('[RETAIN-013] %s rule matches exactly its event source and full event-name set', (resource, source, names) => {
    const r = rule(resource as string);
    expect(r.EventPattern.detail.eventSource).toEqual([source]);
    expect([...(r.EventPattern.detail.eventName as string[])].sort()).toEqual([...(names as string[])].sort());
    expectAlarmTarget(r);
  });

  test('[RETAIN-013] s3-config rule is scoped to the archive bucket by exact name', () => {
    const r = rule('s3-config');
    const bucketNames = (r.EventPattern.detail.requestParameters as { bucketName: unknown[] }).bucketName;
    expect(bucketNames).toHaveLength(1);
  });

  test('[RETAIN-013] lambda-config rule matches the archive stage by name and by ARN only', () => {
    const r = rule('lambda-config');
    expect(r.EventPattern.detail.eventSource).toEqual(['lambda.amazonaws.com']);
    const fn = `${APP_NAME}-archive-to-s3-cmh-alpha`;
    expect(r.EventPattern.detail.requestParameters).toEqual({
      functionName: [fn, { suffix: `:function:${fn}` }],
    });
  });

  test('[RETAIN-013] every retention alarm rule publishes a message naming caller, resource, and event', () => {
    const rules = Object.values(alpha.findResources('AWS::Events::Rule')).filter((r) =>
      ((r.Properties as { Name?: string }).Name ?? '').startsWith(`${APP_NAME}-retention-alarm-`),
    );
    // ecr-delete, ecr-policy, s3-config, ssm-param, ssm-params, lambda-config, cloudtrail
    expect(rules).toHaveLength(7);
    for (const r of rules) {
      const target = (
        r.Properties as {
          Targets: Array<{ InputTransformer: { InputPathsMap: Record<string, string>; InputTemplate: string } }>;
        }
      ).Targets[0];
      const paths = target.InputTransformer.InputPathsMap;
      expect(Object.values(paths)).toEqual(
        expect.arrayContaining([
          '$.detail.eventName',
          '$.detail.userIdentity.arn',
          '$.detail.requestParameters',
          '$.account',
        ]),
      );
      // Every mapped path is actually interpolated into the template.
      for (const key of Object.keys(paths)) {
        expect(target.InputTransformer.InputTemplate).toContain(`<${key}>`);
      }
    }
  });

  test('[RETAIN-013] production synthesizes the same seven alarm rules', () => {
    const names = Object.values(prod.findResources('AWS::Events::Rule'))
      .map((r) => (r.Properties as { Name?: string }).Name ?? '')
      .filter((n) => n.startsWith(`${APP_NAME}-retention-alarm-`))
      .sort();
    expect(names).toEqual(
      ['cloudtrail', 'ecr-delete', 'ecr-policy', 'lambda-config', 's3-config', 'ssm-param', 'ssm-params']
        .map((x) => `${APP_NAME}-retention-alarm-${x}-cmh-alpha`)
        .sort(),
    );
  });
});
