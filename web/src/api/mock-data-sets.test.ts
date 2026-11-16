/**
 * The mock's data sets (the zone heart-rate timestamps are written in) and its sync schedule.
 * The New York data set must carry the real offsets across a daylight-saving change, written the
 * way health-sync writes them (T06's `fhir_datetime`), whatever zone the tests run in.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { LOINC } from './fhir';
import { MockClient } from './mock-client';
import {
  formatInZone,
  heartRateWritesAt,
  lastHeartRateWrite,
  lastSyncRun,
  MOCK_DATA_SETS,
  mockDataSetFromSearch,
  SYNC_INTERVAL_MS,
} from './mock-data';
import { Session } from './session';

// US daylight saving ends on Sunday 1 November 2026 at 02:00 EDT (06:00 UTC): clocks go back
// to 01:00 EST. 01:30 local therefore happens twice, at 05:30 and 06:30 UTC.
const CHANGE = Date.parse('2026-11-01T06:00:00Z');

describe('formatInZone', () => {
  it('writes the offset in force at each instant, like Python isoformat in a ZoneInfo', () => {
    const ny = 'America/New_York';
    expect(formatInZone(Date.parse('2026-11-01T05:30:00Z'), ny)).toBe('2026-11-01T01:30:00-04:00');
    expect(formatInZone(Date.parse('2026-11-01T06:30:00Z'), ny)).toBe('2026-11-01T01:30:00-05:00');
    expect(formatInZone(Date.parse('2026-01-01T14:05:00Z'), ny)).toBe('2026-01-01T09:05:00-05:00');
    // Spring forward, 8 March 2026: 01:59 EST is followed by 03:00 EDT.
    expect(formatInZone(Date.parse('2026-03-08T07:00:00Z'), ny)).toBe('2026-03-08T03:00:00-04:00');
    expect(formatInZone(Date.parse('2026-03-08T12:00:00Z'), 'Etc/UTC')).toBe(
      '2026-03-08T12:00:00+00:00',
    );
    expect(formatInZone(Date.parse('2026-03-08T12:00:00Z'), null)).toBe('2026-03-08T12:00:00.000Z');
  });

  it('round-trips: the written time is the same instant', () => {
    for (let t = CHANGE - 3 * 3600_000; t <= CHANGE + 3 * 3600_000; t += 5 * 60_000) {
      expect(Date.parse(formatInZone(t, 'America/New_York'))).toBe(t);
    }
  });
});

describe('mock data sets', () => {
  it('are chosen with ?mock-data=, defaulting to UTC for anything unknown', () => {
    expect(mockDataSetFromSearch('?mock-data=dst-new-york').timeZone).toBe('America/New_York');
    expect(mockDataSetFromSearch('').id).toBe('utc');
    expect(mockDataSetFromSearch('?mock-data=toString').id).toBe('utc');
    expect(mockDataSetFromSearch('?mock-data=nowhere').id).toBe('utc');
  });

  describe('dst-new-york over the autumn change', () => {
    let client: MockClient;
    const now = Date.parse('2026-11-01T17:00:00Z'); // 12:00 EST

    beforeEach(async () => {
      sessionStorage.clear();
      client = new MockClient({
        session: new Session(sessionStorage, () => now),
        onUnauthorized: () => undefined,
        now: () => now,
        latencyMs: 0,
        dataSet: MOCK_DATA_SETS['dst-new-york'],
      });
      await client.login('x');
    });

    it('writes -04:00 before the change and -05:00 after it, for the same instants', async () => {
      const day = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(now - 24 * 3600_000), end: new Date(now) },
      });
      const starts = day.map((o) => o.effectivePeriod?.start ?? '');
      const offsets = [...new Set(starts.map((s) => s.slice(-6)))];
      expect(offsets).toEqual(['-04:00', '-05:00']);
      for (const o of day) {
        const start = o.effectivePeriod?.start ?? '';
        expect(start.endsWith(Date.parse(start) < CHANGE ? '-04:00' : '-05:00')).toBe(true);
        expect(start).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00-0[45]:00$/);
      }
      // 01:30 local twice: once in each offset.
      expect(starts).toEqual(
        expect.arrayContaining(['2026-11-01T01:30:00-04:00', '2026-11-01T01:30:00-05:00']),
      );
    });

    it('the trace returns the same Observation, and its source window in UTC as Google sends it', async () => {
      const [obs] = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(now - 3600_000), end: new Date(now) },
      });
      const trace = await client.getHeartRateTrace(obs?.id ?? '');
      expect(trace.observation.effectivePeriod).toEqual(obs?.effectivePeriod);
      expect(trace.source?.window.startTime).toMatch(/Z$/);
      expect(Date.parse(trace.source?.window.startTime ?? '')).toBe(
        Date.parse(obs?.effectivePeriod?.start ?? ''),
      );
    });
  });
});

describe('mock sync schedule', () => {
  it('runs every 15 minutes at minute 2', () => {
    expect(new Date(lastSyncRun(Date.parse('2026-03-10T15:16:59Z'))).toISOString()).toBe(
      '2026-03-10T15:02:00.000Z',
    );
    expect(new Date(lastSyncRun(Date.parse('2026-03-10T15:17:00Z'))).toISOString()).toBe(
      '2026-03-10T15:17:00.000Z',
    );
  });

  it('moves the data, its status and the last sync time together, once per run', async () => {
    sessionStorage.clear();
    let now = Date.parse('2026-03-10T15:03:00Z');
    const client = new MockClient({
      session: new Session(sessionStorage, () => now),
      onUnauthorized: () => undefined,
      now: () => now,
      latencyMs: 0,
    });
    await client.login('x');
    const range = () => ({ start: new Date(now - 3 * 3600_000), end: new Date(now) });
    const load = () => client.searchObservations({ code: LOINC.heartRate, range: range() });

    const before = await load();
    const statusBefore = await client.getStatus();
    expect(statusBefore.watchSync.lastSyncAt).toBe('2026-03-10T15:02:00.000Z');
    // The newest window ends at the run, not at "now".
    expect(before.at(-1)?.effectivePeriod?.end).toBe('2026-03-10T15:00:00.000Z');

    now += 10 * 60_000; // 15:13, no run yet: nothing changes
    expect((await client.getStatus()).watchSync.lastSyncAt).toBe(statusBefore.watchSync.lastSyncAt);
    expect((await load()).at(-1)?.id).toBe(before.at(-1)?.id);

    now = Date.parse('2026-03-10T15:02:00Z') + SYNC_INTERVAL_MS; // the 15:17 run
    const after = await load();
    expect((await client.getStatus()).watchSync.lastSyncAt).toBe('2026-03-10T15:17:00.000Z');
    expect(after.at(-1)?.effectivePeriod?.end).toBe('2026-03-10T15:15:00.000Z');
    // A window that was provisional at the 15:02 run is final after the 15:17 run (T10's cutoff).
    const id = before.find((o) => o.status === 'preliminary')?.id;
    expect(id).toBeDefined();
    expect(after.find((o) => o.id === id)?.status).toBe('final');
  });

  it('each window is written twice: new (preliminary) at one run, final at a later one', () => {
    // A day of runs on an ordinary day. Every window that ends well inside the day is written
    // exactly twice: first preliminary, then final. Nothing else is rewritten.
    const first = lastSyncRun(Date.parse('2026-06-10T00:10:00Z'));
    const writes = new Map<string, string[]>();
    for (let run = first; run < first + 24 * 3600_000; run += SYNC_INTERVAL_MS) {
      for (const o of heartRateWritesAt(run)) {
        writes.set(o.id ?? '', [...(writes.get(o.id ?? '') ?? []), o.status]);
      }
    }
    const middle = [...writes].filter(([id]) => {
      const start = Number(id.replace('mock-hr-', ''));
      return start > first + 3600_000 && start < first + 22 * 3600_000;
    });
    expect(middle.length).toBeGreaterThan(200);
    for (const [id, statuses] of middle) expect(statuses, id).toEqual(['preliminary', 'final']);
  });

  it('on the charger: a run with nothing to write writes nothing, and lastSyncAt stays put', async () => {
    // The daily gap leaves out the windows 12:00-12:45 UTC. At the 12:47 run nothing is new and
    // nothing is left to finalise (the 11:55 window became final at 12:32).
    const at = (t: string) => Date.parse(`2026-11-16T${t}:00Z`);
    expect(heartRateWritesAt(at('12:47'))).toEqual([]);
    expect(heartRateWritesAt(at('12:32')).map((o) => o.effectivePeriod?.start)).toEqual([
      '2026-11-16T11:45:00.000Z',
      '2026-11-16T11:50:00.000Z',
      '2026-11-16T11:55:00.000Z',
    ]);
    expect(lastHeartRateWrite(at('12:50'))).toBe(at('12:32'));
    // The first windows after the gap (12:45-13:00) arrive with the 13:02 run.
    expect(lastHeartRateWrite(at('13:05'))).toBe(at('13:02'));

    sessionStorage.clear();
    const now = at('12:50');
    const client = new MockClient({
      session: new Session(sessionStorage, () => now),
      onUnauthorized: () => undefined,
      now: () => now,
      latencyMs: 0,
    });
    await client.login('x');
    expect((await client.getStatus()).watchSync.lastSyncAt).toBe('2026-11-16T12:32:00.000Z');
    const page = await client.listMessages({ kind: 'fhir-transaction', count: 50 });
    const runs = page.entries.filter(
      (e) => e.kind === 'fhir-transaction' && e.writer === 'health-sync',
    );
    // The last eight runs are 11:02 to 12:47; the 12:47 run wrote no Provenance, so no row.
    expect(runs.map((r) => r.recordedAt)).toEqual(
      ['12:32', '12:17', '12:02', '11:47', '11:32', '11:17', '11:02'].map((t) =>
        new Date(at(t)).toISOString(),
      ),
    );
    for (const r of runs) {
      if (r.kind === 'fhir-transaction')
        expect(r.resources.length, r.recordedAt).toBeGreaterThan(0);
    }
  });
});
