/**
 * Committed region → airport-code registry.
 *
 * `regionCode` is load-bearing: it flows `qualifier = ${regionCode}-${stage}`
 * into every physical resource name via {@link Naming}, into the Lambda env, and
 * into IAM ARN / Notation trust-policy scopes. Deriving the code from the region
 * via this single committed map — rather than carrying a second operator-settable
 * literal — means the code can never silently drift from the region it tracks.
 *
 * Grow on demand: add a region → 3-char code entry as new deployment regions are
 * onboarded. An absent region is a fail-fast at synth/deploy time
 * ({@link resolveRegionCode}), never a silent fallback.
 */
export const REGION_CODES: Readonly<Record<string, string>> = {
  'us-east-1': 'iad',
  'us-east-2': 'cmh',
  'us-west-1': 'sfo',
  'us-west-2': 'pdx',
};

/**
 * Resolve a region's committed airport code, or THROW a clear fail-fast error
 * naming how to register an absent region. Callers must not catch-and-fallback:
 * an unregistered region is an operator error that must surface loudly at
 * synth/deploy time.
 *
 * @param region the AWS region (e.g. `us-east-2`)
 * @returns the registered 3-char airport code (e.g. `cmh`)
 * @throws if `region` has no entry in {@link REGION_CODES}
 */
export function resolveRegionCode(region: string): string {
  const code = REGION_CODES[region];
  if (code === undefined) {
    const known = Object.keys(REGION_CODES).sort().join(', ');
    throw new Error(
      `No region code registered for region '${region}'. ` +
        `Register it by adding a '${region}' → '<airport-code>' entry to ` +
        `REGION_CODES in cdk/lib/region-codes.ts. Known regions: ${known}.`,
    );
  }
  return code;
}
