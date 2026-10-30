import { REGION_CODES, resolveRegionCode } from '../lib/region-codes';

// D4 / M4: every region in the committed
// registry resolves to its airport code, and an unregistered region fails fast
// (never a silent fallback) with a message naming how to register it.

describe('resolveRegionCode', () => {
  test.each([
    ['us-east-1', 'iad'],
    ['us-east-2', 'cmh'],
    ['us-west-1', 'sfo'],
    ['us-west-2', 'pdx'],
  ])('maps known region %s to code %s', (region, expected) => {
    expect(resolveRegionCode(region)).toBe(expected);
    // The map and the resolver agree.
    expect(REGION_CODES[region]).toBe(expected);
  });

  test('resolves every registry entry without throwing', () => {
    for (const [region, code] of Object.entries(REGION_CODES)) {
      expect(resolveRegionCode(region)).toBe(code);
    }
  });

  test('throws a clear fail-fast error for an unregistered region', () => {
    expect(() => resolveRegionCode('eu-west-1')).toThrow(
      /No region code registered for region 'eu-west-1'/,
    );
  });

  test('the fail-fast error names how to register the absent region', () => {
    expect(() => resolveRegionCode('ap-south-1')).toThrow(
      /REGION_CODES in cdk\/lib\/region-codes\.ts/,
    );
  });

  test('never silently falls back for an empty region', () => {
    expect(() => resolveRegionCode('')).toThrow(/No region code registered/);
  });
});
