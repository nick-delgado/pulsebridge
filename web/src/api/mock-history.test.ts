import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './errors';
import { LOINC, type Observation } from './fhir';
import { MockClient } from './mock-client';
import { historyBoundary, inHistory, MOCK_HISTORY_DAYS } from './mock-data';

// T47: the mock shows only the proxy's window, the last HISTORY_DAYS days (7 by default). A fixed
// clock, never the time of day (T37).
const NOW = Date.parse('2026-03-10T15:02:00Z');
const DAY = 24 * 3600_000;
const BOUNDARY = NOW - 7 * DAY;

const at = (iso: string): Observation => ({
  resourceType: 'Observation',
  status: 'final',
  code: { coding: [] },
  effectiveDateTime: iso,
});
const period = (start: string, end: string): Observation => ({
  resourceType: 'Observation',
  status: 'final',
  code: { coding: [] },
  effectivePeriod: { start, end },
});

describe('the mock window rule', () => {
  it('is seven days, and a window that overlaps the boundary is inside', () => {
    expect(MOCK_HISTORY_DAYS).toBe(7);
    expect(historyBoundary(NOW)).toBe(BOUNDARY);
    const b = new Date(BOUNDARY).toISOString(); // 2026-03-03T15:02:00.000Z
    expect(inHistory(at(b), BOUNDARY)).toBe(true); // exactly at: inside
    expect(inHistory(at('2026-03-03T15:01:59.999Z'), BOUNDARY)).toBe(false); // just outside
    expect(inHistory(period('2026-03-03T15:00:00Z', '2026-03-03T15:05:00Z'), BOUNDARY)).toBe(true);
    expect(inHistory(period('2026-03-03T14:55:00Z', '2026-03-03T15:00:00Z'), BOUNDARY)).toBe(false);
  });
});

describe('MockClient shows only the last 7 days', () => {
  let now: number;
  let client: MockClient;

  beforeEach(async () => {
    now = NOW;
    client = new MockClient({ onUnauthorized: vi.fn(), now: () => now, latencyMs: 0 });
    await client.startSession('mock-recaptcha-test');
  });

  it('a search over 30 days returns only the windows from the last 7', async () => {
    const found = await client.searchObservations({
      code: LOINC.heartRate,
      range: { start: new Date(NOW - 30 * DAY), end: new Date(NOW) },
    });
    expect(found.length).toBeGreaterThan(1000);
    const ends = found.map((o) => Date.parse(o.effectivePeriod?.end ?? ''));
    expect(Math.min(...ends)).toBeGreaterThanOrEqual(BOUNDARY);
    // The oldest one shown overlaps the boundary or ends right after it.
    expect(Math.min(...ends)).toBeLessThanOrEqual(BOUNDARY + 5 * 60_000);
    const week = await client.searchObservations({
      code: LOINC.heartRate,
      range: { start: new Date(NOW - 7 * DAY), end: new Date(NOW) },
    });
    expect(week.length).toBe(found.length);
  });

  it('the status counts cover the same window', async () => {
    const status = await client.getStatus();
    const week = await client.searchObservations({
      code: LOINC.heartRate,
      range: { start: new Date(NOW - 30 * DAY), end: new Date(NOW) },
    });
    expect(status.observationCounts.watch).toBe(week.length);
  });

  it('a reading that has left the window answers 404, as a missing one', async () => {
    const [first] = await client.searchObservations({
      code: LOINC.heartRate,
      range: { start: new Date(NOW - DAY), end: new Date(NOW) },
    });
    const id = first?.id ?? '';
    await expect(client.getObservation(id)).resolves.toMatchObject({ id });
    await expect(client.getHeartRateTrace(id)).resolves.toBeTruthy();
    now = NOW + 8 * DAY; // eight days later: the reading is older than the window
    await client.startSession('mock-recaptcha-test');
    for (const call of [() => client.getObservation(id), () => client.getHeartRateTrace(id)]) {
      const error: unknown = await call().catch((e: unknown) => e);
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(404);
    }
    const shared: unknown = await client.share({ observationId: id }).catch((e: unknown) => e);
    expect((shared as ApiError).status).toBe(404);
  });

  it('the messages log lists nothing older than the window', async () => {
    const page = await client.listMessages({ count: 50 });
    expect(page.entries.length).toBeGreaterThan(0);
    const attempt = page.entries.find((e) => e.kind === 'cuff-send' && e.source === 'audit-event');
    const attemptId = attempt?.id ?? '';
    await expect(client.getCuffAttemptTrace(attemptId)).resolves.toBeTruthy();
    now = NOW + 8 * DAY; // every seeded row is now older than seven days
    await client.startSession('mock-recaptcha-test');
    const trace: unknown = await client.getCuffAttemptTrace(attemptId).catch((e: unknown) => e);
    expect((trace as ApiError).status).toBe(404);
    const later = await client.listMessages({ count: 50 });
    const times = later.entries.map((e) =>
      Date.parse(e.kind === 'cuff-send' ? e.sentAt : (e.recordedAt ?? '')),
    );
    expect(times.every((t) => t >= historyBoundary(now))).toBe(true);
  });
});
