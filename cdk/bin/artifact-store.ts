#!/usr/bin/env node
import 'source-map-support/register';
import { App } from 'aws-cdk-lib';
import { ArtifactStoreStack } from '../lib/artifact-store-stack';
import { Naming } from '../lib/naming';
import { resolveRegionCode } from '../lib/region-codes';

/**
 * The resolved deployment target. Account and region come from the operator
 * environment (the gitignored `.env`, loaded onto PATH via mise — see
 * `.env.example`); `regionCode` is DERIVED from the region via the committed
 * `region-codes` registry so the two can never drift.
 */
export interface DeployConfig {
  readonly account: string;
  readonly region: string;
  readonly regionCode: string;
  readonly stage: string;
  readonly qualifier: string;
}

/**
 * Read a required deploy-target env var, trimming whitespace. THROWS a clear
 * fail-fast error (never a silent fallback) when it is unset or empty. `noun`
 * is the human phrase used in the message (e.g. `target account`, `region`).
 */
function requireDeployEnv(env: NodeJS.ProcessEnv, name: string, noun: string): string {
  const value = (env[name] ?? '').trim();
  if (!value) {
    throw new Error(
      `${name} is unset or empty. Set the deployment ${noun} in your gitignored ` +
        `.env (copy .env.example); mise loads it onto PATH. Refusing to deploy ` +
        `without an explicit ${noun} — no silent fallback.`,
    );
  }
  return value;
}

/**
 * Resolve the deployment account/region from the operator environment and
 * derive the regionCode from the committed registry.
 *
 * FAILS FAST (throws a clear Error) on (a) an unset/empty `CAIRN_ALPHA_ACCOUNT`,
 * (b) an unset/empty `CAIRN_ALPHA_REGION`, and (c) a region with no entry in the
 * committed region-code registry (via `resolveRegionCode`). It NEVER silently
 * falls back to a default account/region/code — a missing or unregistered value
 * is an operator error that must surface loudly at synth/deploy time.
 *
 * @param env process environment to read (overridable for tests).
 */
export function resolveDeployConfig(env: NodeJS.ProcessEnv = process.env): DeployConfig {
  const account = requireDeployEnv(env, 'CAIRN_ALPHA_ACCOUNT', 'target account');
  const region = requireDeployEnv(env, 'CAIRN_ALPHA_REGION', 'region');

  // resolveRegionCode throws a clear fail-fast error for an unregistered region
  // (case c) — naming how to register it. Never catch-and-fallback.
  const regionCode = resolveRegionCode(region);
  const stage = 'alpha';
  const qualifier = `${regionCode}-${stage}`;

  return { account, region, regionCode, stage, qualifier };
}

/**
 * Construct the CDK app and synthesize the Provenance_System stack against the
 * env-sourced deployment target.
 */
export function synthApp(): App {
  const app = new App();
  const { account, region, regionCode, stage, qualifier } = resolveDeployConfig();

  // Resolve (or, on first deploy, generate-and-freeze) the appName from the
  // committed cdk/app-config.json token. The stack id IS the stack name —
  // `{appName}-{qualifier}` (the 2-segment naming exception).
  const naming = Naming.fromProps({ qualifier });

  new ArtifactStoreStack(app, naming.stackName(), {
    env: {
      account,
      region,
    },
    description:
      'Cairn Proofs — Customer_Hosted, single account, alpha stage, isProd=false, WORM_Enabled=false',
    appName: naming.appName,
    regionCode,
    stage,
    // Uniform three-segment logical tenant hierarchy {org-id}/{tenant-id}/
    // {project-id}. The alpha is
    // provisioned for a single org/tenant/project; each is a stable, opaque,
    // allocate-once id (NOT a display name — display names live only in the
    // Identity_Registry, [IDREG-001]). Overridable via `cdk --context`.
    orgId: app.node.tryGetContext('orgId') ?? 'mvp-org',
    tenantId: app.node.tryGetContext('tenantId') ?? 'mvp-tenant',
    projectId: app.node.tryGetContext('projectId') ?? 'mvp-project',
    // Non-production alpha: stateful resources are DESTROY + auto-empty so
    // `cdk destroy` orphans nothing. Production deployments set isProd=true
    // (RETAIN); WORM_Enabled=true additionally requires isProd=true.
    isProd: false,
    wormEnabled: false,
    notificationEmail: app.node.tryGetContext('notificationEmail') ?? '',
  });

  app.synth();
  return app;
}

// Only synthesize when invoked as the CDK entrypoint (cdk synth/deploy), NOT
// when imported by a test that exercises `resolveDeployConfig` in isolation.
if (require.main === module) {
  synthApp();
}
