import { describe, expect, it } from 'vitest';
import { createSyncWatch } from './sync-watch';

describe('sync watch', () => {
  it('is quiet on the first load, and when the time is unchanged', () => {
    const w = createSyncWatch();
    expect(w.changed('2026-03-10T10:02:00Z')).toBe(false);
    expect(w.changed('2026-03-10T10:02:00Z')).toBe(false);
  });

  it('reports a changed time once', () => {
    const w = createSyncWatch();
    w.changed('2026-03-10T10:02:00Z');
    expect(w.changed('2026-03-10T10:17:00Z')).toBe(true);
    expect(w.changed('2026-03-10T10:17:00Z')).toBe(false);
  });

  it('is quiet when the time becomes null, and reports the next real time', () => {
    const w = createSyncWatch();
    w.changed('2026-03-10T10:02:00Z');
    expect(w.changed(null)).toBe(false);
    expect(w.changed('2026-03-10T10:17:00Z')).toBe(true);
  });

  it('is quiet when the first load has no sync, then reports the first one', () => {
    const w = createSyncWatch();
    expect(w.changed(null)).toBe(false);
    expect(w.changed('2026-03-10T10:02:00Z')).toBe(true);
  });

  it('compares instants: the same moment in another notation is not a change', () => {
    const w = createSyncWatch();
    w.changed('2026-03-10T10:02:00Z');
    expect(w.changed('2026-03-10T10:02:00.000+00:00')).toBe(false);
    expect(w.changed('2026-03-10T05:02:00-05:00')).toBe(false);
  });
});
