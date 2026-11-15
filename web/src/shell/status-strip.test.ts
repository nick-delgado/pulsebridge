import { describe, expect, it } from 'vitest';
import type { StatusSummary, WatchSyncStatus } from '../api/contract';
import { relativeTime, statusStrip } from './status-strip';

const NOW = Date.parse('2026-03-10T12:00:00Z');

function status(sync: Partial<WatchSyncStatus> = {}, overrides: Partial<StatusSummary> = {}) {
  const ok = { reachable: true, checkedAt: '2026-03-10T12:00:00Z', detail: null };
  const summary: StatusSummary = {
    watchSync: {
      state: 'connected',
      reason: null,
      notes: [],
      strandedPreliminary: null,
      checkedAt: '2026-03-10T12:00:00Z',
      lastSyncAt: '2026-03-10T11:53:00Z',
      ...sync,
    },
    observationCounts: { watch: 8412, cuff: 6 },
    fhirStore: ok,
    openemr: ok,
    ...overrides,
  };
  return summary;
}

function strip(s: StatusSummary): HTMLElement {
  const st = statusStrip();
  st.render({ kind: 'ready', status: s, now: NOW });
  return st.root;
}
const text = (s: StatusSummary) => strip(s).textContent;
/** "Label: value" of each item, without the hidden detail. */
const values = (s: StatusSummary) =>
  [...strip(s).querySelectorAll('.status-item')].map(
    (li) =>
      `${li.querySelector('.status-item__label')?.textContent ?? ''}${li.querySelector('.status-item__value')?.textContent ?? ''}`,
  );

describe('status strip', () => {
  it('shows last sync, counts per source and reachability', () => {
    const t = text(status());
    expect(t).toContain('Last sync: 7 min ago');
    expect(t).toMatch(/Observations: 8,?412 watch · 6 cuff/);
    expect(t).toContain('FHIR store: Reachable');
    expect(t).toContain('OpenEMR: Reachable');
  });

  it('says "Reconnect needed" with the reason when health-sync reports it', () => {
    const s = status({ state: 'reconnect-needed', reason: 'refresh-token-invalid' });
    expect(values(s)[0]).toBe('Watch sync: Reconnect needed');
    expect(text(s)).toContain('refresh-token-invalid');
    expect(text(s)).not.toContain('Last sync');
  });

  it('says "Not connected" for not-configured', () => {
    const s = status({ state: 'not-configured', reason: 'secrets-missing:refresh-token' });
    expect(values(s)[0]).toBe('Watch sync: Not connected');
    expect(text(s)).toContain('secrets-missing:refresh-token');
  });

  it('keeps the last sync time when the check is unknown, and says the check did not finish', () => {
    const s = status({ state: 'unknown', reason: 'token-endpoint-unavailable' });
    expect(values(s)[0]).toBe('Last sync: 7 min ago');
    expect(text(s)).toContain('could not finish');
    expect(strip(s).querySelector('.status-item')?.className).toContain('status-item--neutral');
    // A connected one is "ok", and says nothing about a check.
    expect(strip(status()).querySelector('.status-item')?.className).toContain('status-item--ok');
    expect(text(status())).not.toContain('could not finish');
  });

  it('says "Never synced" when there has been no sync', () => {
    expect(values(status({ lastSyncAt: null }))[0]).toBe('Watch sync: Never synced');
  });

  it('shows the backfill-needed note with the stranded count, or without one when unknown', () => {
    const s = status({ notes: ['backfill-needed'], strandedPreliminary: 12 });
    expect(values(s)).toContain('Watch data: Backfill needed');
    expect(text(s)).toContain('12 windows from missed sync runs');
    const unknownCount = status({ notes: ['backfill-needed'], strandedPreliminary: null });
    expect(text(unknownCount)).toContain('Some windows from missed sync runs');
    expect(values(status())).not.toContain('Watch data: Backfill needed');
  });

  it('shows the refresh-token-rotated note as "Reconnect needed soon"', () => {
    const s = status({ notes: ['refresh-token-rotated'] });
    expect(values(s)).toContain('Watch sync: Reconnect needed soon');
    expect(values(s)[0]).toBe('Last sync: 7 min ago');
    expect(values(status())).not.toContain('Watch sync: Reconnect needed soon');
  });

  it('says which system is unreachable, in words', () => {
    const t = text(
      status({}, { openemr: { reachable: false, checkedAt: '', detail: 'VM stopped' } }),
    );
    expect(t).toContain('OpenEMR: Unreachable');
    expect(t).toContain('VM stopped');
  });

  it('formats relative times', () => {
    expect(relativeTime('2026-03-10T11:59:40Z', NOW)).toBe('just now');
    expect(relativeTime('2026-03-10T09:00:00Z', NOW)).toBe('3 h ago');
    expect(relativeTime('2026-03-07T12:00:00Z', NOW)).toBe('3 d ago');
  });
});
