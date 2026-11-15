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
    expect(t).toContain('Newest watch data: 7 min ago');
    expect(t).toMatch(/Observations: 8,?412 watch · 6 cuff/);
    expect(t).toContain('FHIR store: Reachable');
    expect(t).toContain('OpenEMR: Reachable');
  });

  it('says "Reconnect needed" with the reason when health-sync reports it', () => {
    const s = status({ state: 'reconnect-needed', reason: 'refresh-token-invalid' });
    expect(values(s)[0]).toBe('Watch sync: Reconnect needed');
    expect(text(s)).toContain('refresh-token-invalid');
    expect(text(s)).not.toContain('Newest watch data');
  });

  it('says "Not connected" for not-configured', () => {
    const s = status({ state: 'not-configured', reason: 'secrets-missing:refresh-token' });
    expect(values(s)[0]).toBe('Watch sync: Not connected');
    expect(text(s)).toContain('secrets-missing:refresh-token');
  });

  it('keeps the last sync time when the check is unknown, and says the check did not finish', () => {
    const s = status({ state: 'unknown', reason: 'token-endpoint-unavailable' });
    expect(values(s)[0]).toBe('Newest watch data: 7 min ago');
    expect(text(s)).toContain('could not finish');
    expect(strip(s).querySelector('.status-item')?.className).toContain('status-item--neutral');
    // A connected one is "ok", and says nothing about a check.
    expect(strip(status()).querySelector('.status-item')?.className).toContain('status-item--ok');
    expect(text(status())).not.toContain('could not finish');
  });

  it('says "None yet" when no watch data has been written', () => {
    expect(values(status({ lastSyncAt: null }))[0]).toBe('Watch data: None yet');
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
    expect(values(s)[0]).toBe('Newest watch data: 7 min ago');
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

describe('status strip: the proxy as built (T19, amendments 9, 10, 11, 13, 15)', () => {
  it('a null count is "unknown", never 0; a real 0 stays 0', () => {
    const unknown = status({}, { observationCounts: { watch: null, cuff: 0 } });
    expect(values(unknown)).toContain('Observations: unknown watch · 0 cuff');
    expect(text(unknown)).toContain(
      'A count is unknown: the FHIR store could not be asked in time.',
    );
  });

  it('OpenEMR "not-checked" is "Not checked", neutral, not "Unreachable"', () => {
    const s = status(
      {},
      { openemr: { reachable: false, checkedAt: '2026-03-10T12:00:00Z', detail: 'not-checked' } },
    );
    expect(values(s)).toContain('OpenEMR: Not checked');
    expect(text(s)).not.toContain('OpenEMR: Unreachable');
    const item = [...strip(s).querySelectorAll('.status-item')].find((li) =>
      li.textContent.includes('OpenEMR'),
    );
    expect(item?.className).toContain('status-item--neutral');
    // A real failure still says Unreachable, with its code.
    const down = status(
      {},
      { openemr: { reachable: false, checkedAt: '2026-03-10T12:00:00Z', detail: 'error' } },
    );
    expect(values(down)).toContain('OpenEMR: Unreachable');
  });

  it("health-sync's failure reasons in words, with the code", () => {
    const s = status({ state: 'unknown', reason: 'health-sync-timeout' });
    expect(text(s)).toContain('(health-sync did not answer in time: health-sync-timeout)');
    // Its own reasons stay codes.
    expect(text(status({ state: 'reconnect-needed', reason: 'refresh-token-invalid' }))).toContain(
      '(refresh-token-invalid)',
    );
  });

  it('lastSyncAt with milliseconds reads like any instant', () => {
    expect(text(status({ lastSyncAt: '2026-03-10T11:53:00.000Z' }))).toContain(
      'Newest watch data: 7 min ago',
    );
  });

  it('not configured: one clear item, no counts', () => {
    const st = statusStrip();
    st.render({ kind: 'not-configured' });
    expect(st.root.textContent).toContain('Service: Not configured');
    expect(st.root.textContent).toContain('You are still logged in');
  });
});
