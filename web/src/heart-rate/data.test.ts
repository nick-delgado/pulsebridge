import { describe, expect, it } from 'vitest';
import { shareableObservation } from '../state/selection';
import type { Observation } from '../api/fhir';
import {
  chartSeries,
  describePoint,
  nearestPoint,
  prepareReadings,
  selectionFor,
  statusText,
  toPoint,
  unshareableReason,
  windowStats,
  type HeartRatePoint,
} from './data';
import { hrObservation, plusMinutes } from './test-support';

const T0 = '2026-01-01T09:00:00-05:00';
const at = (minutes: number) => plusMinutes(T0, minutes);

function point(obs: ReturnType<typeof hrObservation>): HeartRatePoint {
  const p = toPoint(obs);
  if (typeof p === 'string') throw new Error(`not a point: ${p}`);
  return p;
}

describe('one Observation becomes one point', () => {
  it('reads the average from valueQuantity and min/max from the components by LOINC code', () => {
    const obs = hrObservation({ start: T0, avg: 68.2, min: 64, max: 75, id: 'w1' });
    // Put the components in the "wrong" order: they are found by code, not by position.
    obs.component = [...(obs.component ?? [])].reverse();
    const p = point(obs);
    expect(p).toMatchObject({ id: 'w1', average: 68.2, minimum: 64, maximum: 75 });
    expect(p.startMs).toBe(Date.parse('2026-01-01T14:00:00Z'));
    expect(p.endMs - p.startMs).toBe(5 * 60_000);
    expect(p.midMs).toBe(Date.parse('2026-01-01T14:02:30Z'));
    expect(p.offsetMinutes).toBe(-300);
    expect(p.observation).toBe(obs);
  });

  it('calls only preliminary and registered windows provisional; shares only final ones', () => {
    const at0 = (status: Observation['status']) =>
      point(hrObservation({ start: T0, avg: 60, status }));
    expect(at0('final')).toMatchObject({ provisional: false, shareable: true });
    expect(at0('preliminary')).toMatchObject({ provisional: true, shareable: false });
    expect(at0('registered')).toMatchObject({ provisional: true, shareable: false });
    for (const status of ['amended', 'corrected', 'unknown'] as const) {
      expect(at0(status)).toMatchObject({ provisional: false, shareable: false });
      expect(statusText(at0(status))).toBe(`status ${status}: not shared (only final is)`);
      expect(unshareableReason(at0(status))).toBe(
        `this window's FHIR status is "${status}", not "final". Only final readings are shared.`,
      );
    }
    expect(statusText(at0('preliminary'))).toBe('provisional (status preliminary)');
    expect(statusText(at0('final'))).toBe('final');
  });

  it('requires UCUM /min on the value and on any min/max component', () => {
    const bare = hrObservation({ start: T0, avg: 60 });
    bare.valueQuantity = { value: 60 };
    expect(toPoint(bare)).toBe('unreadable');
    const unitOnly = hrObservation({ start: T0, avg: 60 });
    unitOnly.valueQuantity = { value: 60, unit: 'beats/minute' };
    expect(toPoint(unitOnly)).toBe('unreadable');
    const badComponent = hrObservation({ start: T0, avg: 60, min: 55, max: 70 });
    const minComp = badComponent.component?.[0];
    if (minComp)
      minComp.valueQuantity = { value: 55, system: 'http://unitsofmeasure.org', code: 'mm[Hg]' };
    expect(toPoint(badComponent)).toBe('unreadable');
    // No components at all is fine: the band then shows the average only.
    expect(typeof toPoint(hrObservation({ start: T0, avg: 60 }))).toBe('object');
  });

  it('treats contradictory numbers as unreadable instead of repairing them', () => {
    expect(toPoint(hrObservation({ start: T0, avg: 60, min: 70, max: 65 }))).toBe('unreadable');
    expect(toPoint(hrObservation({ start: T0, avg: 80, min: 55, max: 70 }))).toBe('unreadable');
    expect(toPoint(hrObservation({ start: T0, avg: 50, min: 55, max: 70 }))).toBe('unreadable');
    expect(toPoint(hrObservation({ start: T0, avg: 50, min: 55 }))).toBe('unreadable');
    expect(toPoint(hrObservation({ start: T0, avg: 80, max: 70 }))).toBe('unreadable');
    expect(typeof toPoint(hrObservation({ start: T0, avg: 55, min: 55, max: 55 }))).toBe('object');
    const r = prepareReadings([
      hrObservation({ start: at(0), avg: 60, min: 70, max: 65 }),
      hrObservation({ start: at(5), avg: 60, min: 55, max: 65 }),
    ]);
    expect(r.unreadable).toBe(1);
    expect(r.points).toHaveLength(1);
  });

  it('keeps one window per start time: the most recently updated', () => {
    const older = {
      ...hrObservation({ start: at(0), avg: 60, id: 'old' }),
      meta: { lastUpdated: '2026-01-01T15:00:00Z' },
    };
    const newer = {
      ...hrObservation({ start: at(0), avg: 61, id: 'new' }),
      meta: { lastUpdated: '2026-01-01T16:00:00Z' },
    };
    const other = hrObservation({ start: at(5), avg: 62, id: 'other' });
    for (const order of [
      [older, newer, other],
      [newer, older, other],
    ]) {
      const r = prepareReadings(order);
      expect(r.points.map((p) => p.id)).toEqual(['new', 'other']);
      expect(r.duplicates).toBe(1);
    }
    // No lastUpdated on either: the later one in the list wins.
    const a = hrObservation({ start: at(0), avg: 60, id: 'a' });
    const b = hrObservation({ start: at(0), avg: 61, id: 'b' });
    expect(prepareReadings([a, b]).points.map((p) => p.id)).toEqual(['b']);
  });

  it('leaves out withdrawn results and refuses what it cannot draw correctly', () => {
    expect(toPoint(hrObservation({ start: T0, avg: 60, status: 'entered-in-error' }))).toBe(
      'withdrawn',
    );
    expect(toPoint(hrObservation({ start: T0, avg: 60, status: 'cancelled' }))).toBe('withdrawn');
    const wrongUnit = hrObservation({ start: T0, avg: 60 });
    wrongUnit.valueQuantity = { value: 60, system: 'http://unitsofmeasure.org', code: 'mm[Hg]' };
    expect(toPoint(wrongUnit)).toBe('unreadable');
    expect(
      toPoint(hrObservation({ start: '2026-01-01T09:00:00', end: '2026-01-01T09:05:00', avg: 60 })),
    ).toBe('unreadable');
    const noValue = hrObservation({ start: T0, avg: 60 });
    delete noValue.valueQuantity;
    expect(toPoint(noValue)).toBe('unreadable');
  });

  it('counts what it left out and sorts the rest oldest first', () => {
    const r = prepareReadings([
      hrObservation({ start: at(10), avg: 62 }),
      hrObservation({ start: at(0), avg: 60 }),
      hrObservation({ start: at(5), avg: 61, status: 'entered-in-error' }),
      { ...hrObservation({ start: at(15), avg: 63 }), effectivePeriod: {} },
    ]);
    expect(r.points.map((p) => p.average)).toEqual([60, 62]);
    expect(r.withdrawn).toBe(1);
    expect(r.unreadable).toBe(1);
  });
});

describe('chart series: gaps and provisional windows', () => {
  it('breaks the line where windows are missing, with a null row that cannot be selected', () => {
    const points = prepareReadings([
      hrObservation({ start: at(0), avg: 60, min: 58, max: 63 }),
      hrObservation({ start: at(5), avg: 61, min: 59, max: 64 }),
      hrObservation({ start: at(10), avg: 62, min: 60, max: 65 }),
      // 09:15 to 09:45 missing: the watch was off.
      hrObservation({ start: at(45), avg: 70, min: 66, max: 74 }),
    ]).points;
    const s = chartSeries(points);
    expect(s.x).toEqual([
      Date.parse('2026-01-01T14:02:30Z'),
      Date.parse('2026-01-01T14:07:30Z'),
      Date.parse('2026-01-01T14:12:30Z'),
      Date.parse('2026-01-01T14:15:00Z'), // end of the last window before the gap
      Date.parse('2026-01-01T14:47:30Z'),
    ]);
    expect(s.averageSettled).toEqual([60, 61, 62, null, 70]);
    expect(s.minimum).toEqual([58, 59, 60, null, 66]);
    expect(s.maximum).toEqual([63, 64, 65, null, 74]);
    expect(s.pointOfRow).toEqual([0, 1, 2, -1, 3]);
    expect(s.gaps).toBe(1);
  });

  it('draws adjacent windows as one line, with no gap row', () => {
    const s = chartSeries(
      prepareReadings([
        hrObservation({ start: at(0), avg: 60 }),
        hrObservation({ start: at(5), avg: 61 }),
      ]).points,
    );
    expect(s.gaps).toBe(0);
    expect(s.x).toHaveLength(2);
  });

  it('puts non-final windows on the provisional series, joined to the last final point', () => {
    const s = chartSeries(
      prepareReadings([
        hrObservation({ start: at(0), avg: 60 }),
        hrObservation({ start: at(5), avg: 61 }),
        hrObservation({ start: at(10), avg: 66, status: 'preliminary' }),
        hrObservation({ start: at(15), avg: 67, status: 'preliminary' }),
      ]).points,
    );
    expect(s.averageSettled).toEqual([60, 61, null, null]);
    expect(s.averageProvisional).toEqual([null, 61, 66, 67]);
  });

  it('uses the average for the band where a window has no min/max components', () => {
    const s = chartSeries(prepareReadings([hrObservation({ start: at(0), avg: 60 })]).points);
    expect(s.minimum).toEqual([60]);
    expect(s.maximum).toEqual([60]);
  });
});

describe('statistics for the range', () => {
  it('min of minimums, mean of averages, max of maximums, counts', () => {
    const stats = windowStats(
      prepareReadings([
        hrObservation({ start: at(0), avg: 60, min: 55, max: 66 }),
        hrObservation({ start: at(5), avg: 70, min: 65, max: 78 }),
        hrObservation({ start: at(10), avg: 80.5, min: 72, max: 90, status: 'preliminary' }),
      ]).points,
    );
    expect(stats).not.toBeNull();
    expect(stats?.count).toBe(3);
    expect(stats?.provisional).toBe(1);
    expect(stats?.minimum).toBe(55);
    expect(stats?.maximum).toBe(90);
    // (60 + 70 + 80.5) / 3: each window counts once.
    expect(stats?.mean).toBeCloseTo(70.1667, 4);
  });

  it('is not time-weighted: a gap does not change the mean', () => {
    const stats = windowStats(
      prepareReadings([
        hrObservation({ start: at(0), avg: 50 }),
        hrObservation({ start: at(600), avg: 90 }), // ten hours later
      ]).points,
    );
    expect(stats?.mean).toBe(70);
    expect(stats?.minimum).toBe(50);
    expect(stats?.maximum).toBe(90);
  });

  it('is null for an empty range', () => {
    expect(windowStats([])).toBeNull();
  });
});

describe('a daylight-saving change inside the range', () => {
  // New York, 1 Nov 2026: 01:55 EDT is followed by 01:00 EST (06:00Z).
  const readings = prepareReadings([
    hrObservation({ start: '2026-11-01T01:50:00-04:00', avg: 58 }),
    hrObservation({
      start: '2026-11-01T01:55:00-04:00',
      end: '2026-11-01T01:00:00-05:00',
      avg: 57,
    }),
    hrObservation({ start: '2026-11-01T01:00:00-05:00', avg: 56 }),
    hrObservation({ start: '2026-11-01T01:05:00-05:00', avg: 55 }),
  ]);

  it('keeps real time order and draws no gap at the change', () => {
    expect(readings.points.map((p) => p.average)).toEqual([58, 57, 56, 55]);
    const s = chartSeries(readings.points);
    expect(s.gaps).toBe(0);
    expect(s.x.slice(1).every((x, i) => x > (s.x[i] ?? Infinity))).toBe(true);
  });

  it('records both offsets and labels each window in its own one', () => {
    expect(readings.segments).toEqual([
      { fromMs: Date.parse('2026-11-01T05:50:00Z'), offsetMinutes: -240 },
      { fromMs: Date.parse('2026-11-01T06:00:00Z'), offsetMinutes: -300 },
    ]);
    const [, before, after] = readings.points;
    // Both ends are written in the start's offset, which the label names: 02:00 EDT is 01:00 EST.
    expect(before && describePoint(before)).toContain('Sun 1 Nov 2026, 01:55–02:00 (UTC-04:00)');
    expect(after && describePoint(after)).toContain('Sun 1 Nov 2026, 01:00–01:05 (UTC-05:00)');
  });
});

describe('a picked position maps back to one stored Observation', () => {
  const points = prepareReadings([
    hrObservation({ start: at(0), avg: 60, id: 'a' }),
    hrObservation({ start: at(5), avg: 61, id: 'b' }),
    hrObservation({ start: at(60), avg: 62, id: 'c' }),
  ]).points;

  it('finds the window whose middle is nearest', () => {
    const ids = (ms: number) => points[nearestPoint(points, ms)]?.observation.id;
    expect(ids(Date.parse('2026-01-01T14:02:30Z'))).toBe('a');
    expect(ids(Date.parse('2026-01-01T14:06:00Z'))).toBe('b');
    // In the gap, nearer to b's middle (14:07:30) than c's (15:02:30).
    expect(ids(Date.parse('2026-01-01T14:30:00Z'))).toBe('b');
    expect(ids(Date.parse('2026-01-01T14:40:00Z'))).toBe('c');
    expect(ids(Date.parse('2025-12-31T00:00:00Z'))).toBe('a');
    expect(ids(Date.parse('2027-01-01T00:00:00Z'))).toBe('c');
    expect(nearestPoint([], 0)).toBe(-1);
  });
});

describe('selection for a picked window', () => {
  it('a final window is shareable', () => {
    const sel = selectionFor(point(hrObservation({ start: T0, avg: 68.2, id: 'fin' })));
    expect(sel).toEqual({
      source: 'watch',
      observationId: 'fin',
      summary: 'Heart rate 68.2 /min, Thu 1 Jan 09:00–09:05',
      unshareableReason: null,
    });
    expect(shareableObservation(sel)).toBe('Observation/fin');
  });

  it('a preliminary window is not, and the reason says why', () => {
    const sel = selectionFor(
      point(hrObservation({ start: T0, avg: 70, id: 'pre', status: 'preliminary' })),
    );
    expect(sel.unshareableReason).toMatch(/provisional \(FHIR status "preliminary"\)/);
    expect(sel.unshareableReason).toMatch(/Only final readings are shared/);
    expect(shareableObservation(sel)).toBeNull();
  });
});
