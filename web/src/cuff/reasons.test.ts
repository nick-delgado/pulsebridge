import { describe, expect, it } from 'vitest';
import { MAPPER_PLAUSIBLE_RANGE } from '../api/contract';
import { REASON_SENTENCES, reasonText } from './reasons';

/** `NAME = "code"` lines of the Reason enum. */
function enumCodes(py: string): Map<string, string> {
  const block = /class Reason\(StrEnum\):([\s\S]*?)\n\n\n/.exec(py)?.[1] ?? '';
  return new Map(
    [...block.matchAll(/^\s+([A-Z0-9_]+) = "([a-z0-9-]+)"/gm)].map((m) => [m[1] ?? '', m[2] ?? '']),
  );
}

/** Each `Reason.NAME: "..." "..."` entry of SENTENCES, its string pieces joined. */
function sentences(py: string): Map<string, string> {
  const block = /SENTENCES: dict\[Reason, str\] = \{([\s\S]*?)\n\}/.exec(py)?.[1] ?? '';
  const out = new Map<string, string>();
  const parts = block.split(/\n\s+Reason\./).slice(1);
  const { systolic: s, diastolic: d } = MAPPER_PLAUSIBLE_RANGE;
  for (const part of parts) {
    const name = /^([A-Z0-9_]+):/.exec(part)?.[1] ?? '';
    let text = [...part.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1] ?? '').join('');
    // OUT_OF_RANGE is formatted with the ranges (pulsebridge_fhir SYSTOLIC_RANGE, DIASTOLIC_RANGE).
    for (const n of [s.min, s.max, d.min, d.max]) text = text.replace('{}', String(n));
    out.set(name, text);
  }
  return out;
}

/**
 * The literal source: v2-to-fhir's reason codes and the sentences it says the dashboard shows.
 * Read with Node's fs (Vite refuses `?raw` imports outside web/, and @types/node is not a
 * dependency, hence the small local type).
 */
const fsModule = 'node:fs';
// eslint-disable-next-line no-unsanitized/method -- a constant module name, in a unit test only
const fs = (await import(/* @vite-ignore */ fsModule)) as {
  readFileSync: (path: string, encoding: 'utf8') => string;
};
const source = fs.readFileSync(
  // A path string: jsdom replaces the global URL class, which Node's fs does not accept; Vite
  // serves files outside its root under `/@fs/<absolute path>`.
  decodeURIComponent(
    new URL('../../../services/v2-to-fhir/v2_to_fhir/reasons.py', import.meta.url).pathname,
  ).replace(/^\/@fs\//, '/'),
  'utf8',
);

describe("reason sentences match v2-to-fhir's reasons.py", () => {
  const codes = enumCodes(source);
  const texts = sentences(source);

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
