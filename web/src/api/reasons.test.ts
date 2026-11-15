/**
 * Drift tests: the page's copies of values that live in Python must match their sources.
 *   - every reason code and sentence in services/v2-to-fhir/v2_to_fhir/reasons.py;
 *   - the mapper's plausible ranges, SYSTOLIC_RANGE and DIASTOLIC_RANGE in
 *     libs/pulsebridge_fhir/pulsebridge_fhir/inputs.py, against `MAPPER_PLAUSIBLE_RANGE` in
 *     contract.ts (and so against the out-of-range sentence, which is built from them).
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { MAPPER_PLAUSIBLE_RANGE } from './contract';
import { REASON_SENTENCES, reasonText } from './reasons';

/** A repository file by its path from the repository root. */
function repoFile(path: string): string {
  // A path string: jsdom replaces the global URL class, which Node's fs does not accept; Vite
  // serves files outside its root under `/@fs/<absolute path>`.
  // The base goes through a variable: Vite rewrites `new URL(<template>, import.meta.url)` into
  // an asset glob of the parent directories, which it then refuses to serve.
  const base = import.meta.url;
  const absolute = decodeURIComponent(new URL(`../../../${path}`, base).pathname);
  return readFileSync(absolute.replace(/^\/@fs\//, '/'), 'utf8');
}

const reasonsPy = repoFile('services/v2-to-fhir/v2_to_fhir/reasons.py');
const inputsPy = repoFile('libs/pulsebridge_fhir/pulsebridge_fhir/inputs.py');

/** `NAME = (low, high)` in inputs.py. */
function pythonRange(name: string): { min: number; max: number } {
  const m = new RegExp(`^${name} = \\((\\d+), (\\d+)\\)`, 'm').exec(inputsPy);
  if (!m) throw new Error(`${name} not found in inputs.py`);
  return { min: Number(m[1]), max: Number(m[2]) };
}

/** `NAME = "code"` lines of the Reason enum. */
function enumCodes(py: string): Map<string, string> {
  const block = /class Reason\(StrEnum\):([\s\S]*?)\n\n\n/.exec(py)?.[1] ?? '';
  return new Map(
    [...block.matchAll(/^\s+([A-Z0-9_]+) = "([a-z0-9-]+)"/gm)].map((m) => [m[1] ?? '', m[2] ?? '']),
  );
}

/**
 * Each `Reason.NAME: "..." "..."` entry of SENTENCES, its string pieces joined, with `{}` filled
 * from the PYTHON ranges, as reasons.py formats OUT_OF_RANGE.
 */
function sentences(py: string): Map<string, string> {
  const block = /SENTENCES: dict\[Reason, str\] = \{([\s\S]*?)\n\}/.exec(py)?.[1] ?? '';
  const out = new Map<string, string>();
  const parts = block.split(/\n\s+Reason\./).slice(1);
  const s = pythonRange('SYSTOLIC_RANGE');
  const d = pythonRange('DIASTOLIC_RANGE');
  for (const part of parts) {
    const name = /^([A-Z0-9_]+):/.exec(part)?.[1] ?? '';
    let text = [...part.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1] ?? '').join('');
    for (const n of [s.min, s.max, d.min, d.max]) text = text.replace('{}', String(n));
    out.set(name, text);
  }
  return out;
}

describe("MAPPER_PLAUSIBLE_RANGE matches the mapper's ranges in inputs.py", () => {
  it('systolic and diastolic, both ends', () => {
    expect(MAPPER_PLAUSIBLE_RANGE).toEqual({
      systolic: pythonRange('SYSTOLIC_RANGE'),
      diastolic: pythonRange('DIASTOLIC_RANGE'),
    });
  });
});

describe("reason sentences match v2-to-fhir's reasons.py", () => {
  const codes = enumCodes(reasonsPy);
  const texts = sentences(reasonsPy);

  it('reads a sensible number of codes from the source', () => {
    expect(codes.size).toBeGreaterThan(30);
    expect(texts.size).toBe(codes.size);
  });

  it('has the same sentence for every code, and no extra code', () => {
    const expected = Object.fromEntries([...codes].map(([name, code]) => [code, texts.get(name)]));
    expect(REASON_SENTENCES).toEqual(expected);
  });

  it('shows an unknown code as itself, and says when there is none', () => {
    expect(reasonText('brand-new-code')).toBe(
      'The mapper gave the reason code "brand-new-code", which this page does not know.',
    );
    expect(reasonText(null)).toBe('No reason was given.');
    expect(reasonText('out-of-range')).toBe(
      'A value is outside the plausible range (systolic 50 to 300, diastolic 20 to 200 mm[Hg]).',
    );
  });
});
