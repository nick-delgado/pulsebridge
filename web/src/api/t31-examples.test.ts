import { describe, expect, it } from 'vitest';

// The copies kept under ./t31-examples/ and the files T31's generator writes in libs/. Vite
// refuses `?raw` reads outside web/, so the files are compared parsed, by their JSON text with
// key order kept: any changed value, element or order fails, only whitespace could not.
const copies = import.meta.glob<unknown>('./t31-examples/*.json', {
  import: 'default',
  eager: true,
});
const library = import.meta.glob<unknown>(
  '../../../libs/pulsebridge_fhir/examples/auditevent-send-*.json',
  { import: 'default', eager: true },
);

const byName = (files: Record<string, unknown>): Record<string, string> =>
  Object.fromEntries(
    Object.entries(files).map(([path, json]) => [
      path.split('/').at(-1) ?? path,
      JSON.stringify(json),
    ]),
  );

describe('T31 example copies against libs/pulsebridge_fhir/examples', () => {
  it('holds a copy of every send-attempt AuditEvent example, and nothing else', () => {
    expect(Object.keys(byName(copies)).sort()).toEqual(Object.keys(byName(library)).sort());
    expect(Object.keys(byName(copies))).toHaveLength(5);
  });

  it.each(Object.keys(byName(library)).sort())('%s is the same as the library file', (name) => {
    expect(byName(copies)[name]).toBe(byName(library)[name]);
  });
});
