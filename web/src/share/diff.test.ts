import { describe, expect, it } from 'vitest';
import { differences, differenceSentences, MAX_DIFFERENCES } from './diff';

describe('sent versus stored', () => {
  it('nothing differs: no sentences', () => {
    const r = { resourceType: 'Patient', name: [{ family: 'Demo' }] };
    expect(differences(r, structuredClone(r))).toEqual([]);
  });

  it('dropped, added and changed, at their own paths', () => {
    const sent = {
      code: { coding: [{ code: '8867-4', display: 'Heart rate' }] },
      component: [{ v: 1 }],
      effectivePeriod: { start: 'a' },
      performer: [{ reference: 'Patient/x' }],
    };
    const stored = {
      code: { coding: [{ code: '8867-4', display: 'Pulse' }] },
      component: [],
      effectivePeriod: { start: 'a' },
      id: 'o1',
      meta: { profile: ['p'] },
    };
    expect(differences(sent, stored)).toEqual([
      { path: 'code.coding[0].display', change: 'changed', sent: 'Heart rate', stored: 'Pulse' },
      { path: 'component[0]', change: 'dropped', sent: { v: 1 }, stored: undefined },
      {
        path: 'performer',
        change: 'dropped',
        sent: [{ reference: 'Patient/x' }],
        stored: undefined,
      },
      { path: 'id', change: 'added', sent: undefined, stored: 'o1' },
      { path: 'meta', change: 'added', sent: undefined, stored: { profile: ['p'] } },
    ]);
  });

  it('in words', () => {
    expect(differenceSentences({ performer: 1, a: 'x' }, { a: 'y', id: 'o' })).toEqual([
      'performer was sent, but OpenEMR did not keep it (sent: 1).',
      'a changed: sent "x", stored "y".',
      'id was added by OpenEMR (stored: "o").',
    ]);
  });

  it('a type change is a change; long values are cut', () => {
    expect(differences({ a: 1 }, { a: '1' })).toHaveLength(1);
    const long = 'x'.repeat(500);
    expect(differenceSentences({ a: long }, { a: 'y' })[0]?.length).toBeLessThan(200);
  });

  it('bounded, with a count of the rest', () => {
    const sent = Object.fromEntries(Array.from({ length: 50 }, (_, i) => [`k${String(i)}`, i]));
    const out = differenceSentences(sent, {});
    expect(out).toHaveLength(MAX_DIFFERENCES + 1);
    expect(out.at(-1)).toBe('And 10 more.');
  });
});
