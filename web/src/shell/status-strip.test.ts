import { describe, expect, it } from 'vitest';
import type { StatusSummary } from '../api/contract';
import { relativeTime, statusStrip } from './status-strip';

const NOW = Date.parse('2026-03-10T12:00:00Z');

function status(overrides: Partial<StatusSummary> = {}): StatusSummary {
  const ok = { reachable: true, checkedAt: '2026-03-10T12:00:00Z', detail: null };
  return {
    watchSync: { state: 'ok', lastSyncAt: '2026-03-10T11:53:00Z', detail: null },
    observationCounts: { watch: 8412, cuff: 6 },
    fhirStore: ok,
    openemr: ok,
    ...overrides,
  };
}

function text(s: StatusSummary): string {
  const strip = statusStrip();
  strip.render({ kind: 'ready', status: s, now: NOW });
  return strip.root.textContent;
}

describe('status strip', () => {
  it('shows last sync, counts per source and reachability', () => {
    const t = text(status());
    expect(t).toContain('Last sync: 7 min ago');
    expect(t).toMatch(/Observations: 8,?412 watch · 6 cuff/);
    expect(t).toContain('FHIR store: Reachable');
    expect(t).toContain('OpenEMR: Reachable');
  });

  it('says "Reconnect needed" when the Google token has expired', () => {
    const t = text(
      status({
        watchSync: { state: 'reconnect-needed', lastSyncAt: '2026-03-03T00:00:00Z', detail: null },
      }),
    );
    expect(t).toContain('Reconnect needed');
    expect(t).not.toContain('Last sync');
  });

  it('says which system is unreachable, in words', () => {
    const t = text(status({ openemr: { reachable: false, checkedAt: '', detail: 'VM stopped' } }));
    expect(t).toContain('OpenEMR: Unreachable');
    expect(t).toContain('VM stopped');
  });

  it('formats relative times', () => {
    expect(relativeTime('2026-03-10T11:59:40Z', NOW)).toBe('just now');
    expect(relativeTime('2026-03-10T09:00:00Z', NOW)).toBe('3 h ago');
    expect(relativeTime('2026-03-07T12:00:00Z', NOW)).toBe('3 d ago');
  });
});
