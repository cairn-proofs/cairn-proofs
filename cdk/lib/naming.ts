import { randomInt } from 'crypto';
import { existsSync, readFileSync, writeFileSync } from 'fs';
import * as path from 'path';

/**
 * Single composition point for every AWS resource physical name in the stack
 * (platform-constraints *Uniform AWS Resource Naming Convention*). One uniform
 * `{appName}-{resource}-{qualifier}` naming scheme.
 *
 * The payoff of centralizing is not only consistent prefixes — it is that the
 * name STRUCTURE has exactly one definition, so a later insertion (e.g. a
 * Service_Hosted `/{accountId}` tenant segment) is a one-place edit, not a
 * repo-wide sweep. The ECR namespace, the Notation trust-policy registry scope,
 * and the pipeline IAM ARN patterns all derive from {@link Naming.artifactNamespace}
 * — there is NO independent literal `artifact-${qualifier}`
 * interpolation anywhere in the stack.
 */

/**
 * Unambiguous token alphabet — excludes the visually-confusable `o 0 i l 1`
 * so a human reading a deployed resource name can transcribe the token without
 * ambiguity.
 */
export const TOKEN_ALPHABET = '23456789abcdefghjkmnpqrstuvwxyz';
const TOKEN_LENGTH = 3;

/** Committed config file holding the frozen appName token (NOT cdk.context.json,
 * which can be cleared — clearing it would regenerate the token and break
 * CloudFormation update targeting, T-06). */
const APP_CONFIG_PATH = path.resolve(__dirname, '..', 'app-config.json');

interface AppConfig {
  appNameToken?: string;
}

/**
 * A generated token is GUARANTEED to be a valid in-alphabet 3-char string
 * ("collision-checked" = the generation routine guarantees a well-formed token,
 * not a probabilistic one). Cross-deployment uniqueness is the per-account
 * operator's responsibility: each deployment commits its OWN `cdk/app-config.json`
 * separately, so two deployments cannot silently share a token unless an
 * operator copies the file. We do NOT call any AWS API at synth time (synth must
 * stay offline + deterministic), so uniqueness against a live account is not —
 * and per design is not meant to be — enforced here. Name-deconfliction is the
 * goal; runtime isolation between co-located deployments is an explicit non-goal
 * (see design.md Risks: "names deconflicted != pipelines isolated").
 */
export function generateAppNameToken(): string {
  let token = '';
  for (let i = 0; i < TOKEN_LENGTH; i++) {
    token += TOKEN_ALPHABET[randomInt(TOKEN_ALPHABET.length)];
  }
  return token;
}

/** A token is valid iff it is exactly {@link TOKEN_LENGTH} chars drawn from
 * {@link TOKEN_ALPHABET}. */
export function isValidToken(token: string): boolean {
  if (token.length !== TOKEN_LENGTH) {
    return false;
  }
  return [...token].every((c) => TOKEN_ALPHABET.includes(c));
}

/**
 * Generate-once-and-freeze: read the token from committed config if
 * present (verbatim — NEVER regenerated on a subsequent synth, or CloudFormation
 * would lose its update target and REPLACE every resource); otherwise generate
 * one, WRITE it back to the config file, and log loudly that it must be
 * committed.
 *
 * @param configPath override for tests; defaults to `cdk/app-config.json`.
 */
export function resolveAppNameToken(configPath: string = APP_CONFIG_PATH): string {
  const existing: AppConfig = existsSync(configPath)
    ? (JSON.parse(readFileSync(configPath, 'utf-8')) as AppConfig)
    : {};
  if (existing.appNameToken) {
    if (!isValidToken(existing.appNameToken)) {
      throw new Error(
        `app-config.json appNameToken '${existing.appNameToken}' is not a valid ` +
          `${TOKEN_LENGTH}-char token over the unambiguous alphabet '${TOKEN_ALPHABET}'. ` +
          'Fix or delete it (deleting regenerates a fresh token — only safe before first deploy).',
      );
    }
    return existing.appNameToken;
  }

  const token = generateAppNameToken();
  writeFileSync(configPath, JSON.stringify({ ...existing, appNameToken: token }, null, 2) + '\n');
  // eslint-disable-next-line no-console
  console.warn(
    `\n  *** generated appName token '${token}' — COMMIT cdk/app-config.json ***\n` +
      '  This token freezes the deployment\'s resource names. It must be committed and\n' +
      '  never regenerated, or `cdk deploy` would REPLACE every resource.\n',
  );
  return token;
}

export interface NamingProps {
  /**
   * Application name — the leading segment of every resource name. Defaults to
   * `cairnproofs{token}` where `{token}` is the frozen per-deployment uniquifier.
   */
  readonly appName: string;
  /** Deployment_Qualifier: `{regionCode}-{stage}` (e.g. `cmh-alpha`). */
  readonly qualifier: string;
}

/**
 * The single naming helper. Every construct receives one of these and calls
 * `nameFor()` (or a style variant) rather than hand-interpolating a prefix.
 */
export class Naming {
  readonly appName: string;
  readonly qualifier: string;

  constructor(props: NamingProps) {
    this.appName = props.appName;
    this.qualifier = props.qualifier;
  }

  /**
   * Build a Naming from a stack's resolved appName and the qualifier
   * components. The default appName is `cairnproofs{token}`.
   */
  static fromProps(opts: {
    qualifier: string;
    appName?: string;
    configPath?: string;
  }): Naming {
    const appName = opts.appName ?? `cairnproofs${resolveAppNameToken(opts.configPath)}`;
    return new Naming({ appName, qualifier: opts.qualifier });
  }

  /**
   * The canonical hyphenated physical name: `{appName}-{resource}-{qualifier}`.
   * e.g. `nameFor('validate-bundle')` ⇒ `cairnproofs4af-validate-bundle-cmh-alpha`.
   */
  nameFor(resource: string): string {
    return `${this.appName}-${resource}-${this.qualifier}`;
  }

  /**
   * The 2-segment STACK name `{appName}-{qualifier}` — the one documented
   * exception to the 3-segment scheme (it names the deployment container, not a
   * resource within it).
   */
  stackName(): string {
    return `${this.appName}-${this.qualifier}`;
  }

  /**
   * Underscore-only variant for AWS Signer profile names, which are constrained
   * to `/^[a-zA-Z0-9_]{2,64}$/` (no hyphens). The whole name — appName, resource,
   * and qualifier — is underscored: `{appName}_{resource}_{qualifier}`.
   * e.g. `signerNameFor('artifact_pipeline_signing')` ⇒
   * `cairnproofs4af_artifact_pipeline_signing_cmh_alpha`.
   */
  signerNameFor(resource: string): string {
    return `${this.appName}_${resource}_${this.qualifier}`.replace(/-/g, '_');
  }

  /**
   * KMS alias variant: `alias/{appName}-{resource}-{qualifier}`.
   */
  kmsAliasFor(resource: string): string {
    return `alias/${this.nameFor(resource)}`;
  }

  /**
   * SSM parameter path variant: `/{appName}/{stage}/{suffix}`. The stage (not
   * the full qualifier) is the path segment.
   */
  ssmPath(stage: string, suffix: string): string {
    return `/${this.appName}/${stage}/${suffix}`;
  }

  /**
   * The ECR repository namespace — the SINGLE composition point the Notation
   * trust-policy registry scope and the pipeline IAM ARN patterns both derive
   * from. `{appName}-artifact-{qualifier}`. The per-project repo path
   * is `{artifactNamespace}/{projectId}`.
   */
  get artifactNamespace(): string {
    return this.nameFor('artifact');
  }
}
