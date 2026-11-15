import { describe, expect, it } from 'vitest';
import {
  DAY_MS,
  HOUR_MS,
  formatClock,
  formatOffset,
  formatWindow,
  offsetAt,
  offsetSegments,
  parseFhirDateTime,
  tickStep,
  timeTicks,
} from './time';

describe('parseFhirDateTime', () => {
  it('keeps the instant and the offset the value was written in', () => {
    expect(parseFhirDateTime('2026-01-01T09:05:00-05:00')).toEqual({
      ms: Date.parse('2026-01-01T14:05:00Z'),
      offsetMinutes: -300,
    });
    expect(parseFhirDateTime('2026-01-01T14:05:00Z')?.offsetMinutes).toBe(0);
    expect(parseFhirDateTime('2026-01-01T19:35:00+05:30')?.offsetMinutes).toBe(330);
    expect(parseFhirDateTime('2026-01-01T14:05:00.250+00:00')?.ms).toBe(
      Date.parse('2026-01-01T14:05:00.250Z'),
    );
  });

  it('refuses a time with no zone (R4: a time SHALL have one), a bare date and junk', () => {
    expect(parseFhirDateTime('2026-01-01T09:05:00')).toBeNull();
    expect(parseFhirDateTime('2026-01-01')).toBeNull();
    expect(parseFhirDateTime('yesterday')).toBeNull();
    expect(parseFhirDateTime(undefined)).toBeNull();
  });
});

describe('formatting in the recorded zone, not the browser zone', () => {
  it('formats a window in its own offset', () => {
    const start = Date.parse('2026-01-01T14:05:00Z');
    expect(formatWindow(start, start + 5 * 60_000, -300)).toBe(
      'Thu 1 Jan 2026, 09:05–09:10 (UTC-05:00)',
    );
    expect(formatClock(start, 330)).toBe('19:35');
    expect(formatOffset(0)).toBe('UTC+00:00');
    expect(formatOffset(-570)).toBe('UTC-09:30');
  });
});

// America/New_York in 2026: summer time ends Sun 1 Nov at 02:00 EDT (06:00Z), when clocks go
// back to 01:00 EST; it starts Sun 8 Mar at 02:00 EST (07:00Z), when clocks jump to 03:00 EDT.
const AUTUMN = [
  { fromMs: Date.parse('2026-10-31T00:00:00-04:00'), offsetMinutes: -240 },
  { fromMs: Date.parse('2026-11-01T06:00:00Z'), offsetMinutes: -300 },
];
const SPRING = [
  { fromMs: Date.parse('2026-03-07T00:00:00-05:00'), offsetMinutes: -300 },
  { fromMs: Date.parse('2026-03-08T07:00:00Z'), offsetMinutes: -240 },
];

describe('offset segments', () => {
  it('collapses readings into one segment per offset, starting at the first reading with it', () => {
    const readings = [
      { ms: 1, offsetMinutes: -240 },
      { ms: 2, offsetMinutes: -240 },
      { ms: 3, offsetMinutes: -300 },
      { ms: 4, offsetMinutes: -300 },
    ];
    expect(offsetSegments(readings)).toEqual([
      { fromMs: 1, offsetMinutes: -240 },
      { fromMs: 3, offsetMinutes: -300 },
    ]);
    expect(offsetAt(offsetSegments(readings), 0)).toBe(-240);
    expect(offsetAt(offsetSegments(readings), 2.5)).toBe(-240);
    expect(offsetAt(offsetSegments(readings), 3)).toBe(-300);
  });
});

describe('axis ticks across a daylight-saving change', () => {
  it('autumn, hourly: 01:00 comes twice an hour apart, and the second names its offset', () => {
    const ticks = timeTicks(
      Date.parse('2026-11-01T03:00:00Z'),
      Date.parse('2026-11-01T09:00:00Z'),
      AUTUMN,
      10,
    );
    expect(ticks.map((t) => new Date(t.ms).toISOString().slice(11, 16))).toEqual([
      '03:00',
      '04:00',
      '05:00',
      '06:00',
      '07:00',
      '08:00',
      '09:00',
    ]);
    expect(ticks.map((t) => t.label)).toEqual([
      '23:00\n31 Oct',
      '00:00\n1 Nov',
      '01:00',
      '01:00\nUTC-05:00',
      '02:00',
      '03:00',
      '04:00',
    ]);
    expect(new Set(ticks.map((t) => t.label)).size).toBe(ticks.length);
  });

  it('spring, hourly: no 02:00 (it did not exist) and no hole on the axis', () => {
    const ticks = timeTicks(
      Date.parse('2026-03-08T05:00:00Z'),
      Date.parse('2026-03-08T09:00:00Z'),
      SPRING,
      10,
    );
    expect(ticks.map((t) => t.label)).toEqual([
      '00:00\n8 Mar',
      '01:00',
      '03:00\nUTC-04:00',
      '04:00',
      '05:00',
    ]);
    // Evenly spaced in real time: one hour between every pair of ticks.
    const gaps = ticks.slice(1).map((t, i) => t.ms - (ticks[i]?.ms ?? 0));
    expect(new Set(gaps)).toEqual(new Set([HOUR_MS]));
  });

  it('autumn, daily: one tick per local midnight, each day once, the long day 25 h', () => {
    const ticks = timeTicks(
      Date.parse('2026-10-29T12:00:00Z'),
      Date.parse('2026-11-04T12:00:00Z'),
      AUTUMN,
      7,
    );
    expect(ticks.map((t) => t.label)).toEqual([
      '30 Oct',
      '31 Oct',
      '1 Nov',
      '2 Nov\nUTC-05:00',
      '3 Nov',
      '4 Nov',
    ]);
    // Midnight 1 Nov (EDT) to midnight 2 Nov (EST) is 25 hours.
    expect((ticks[3]?.ms ?? 0) - (ticks[2]?.ms ?? 0)).toBe(25 * HOUR_MS);
    expect(new Date(ticks[3]?.ms ?? 0).toISOString()).toBe('2026-11-02T05:00:00.000Z');
  });

  it('picks the smallest step that fits the space', () => {
    expect(tickStep(0, DAY_MS, 8)).toBe(3 * HOUR_MS);
    expect(tickStep(0, DAY_MS, 4)).toBe(6 * HOUR_MS);
    expect(tickStep(0, 7 * DAY_MS, 7)).toBe(DAY_MS);
    expect(tickStep(0, 30 * DAY_MS, 6)).toBe(7 * DAY_MS);
  });
});
