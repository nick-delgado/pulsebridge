/**
 * From stored FHIR Observations to what the chart draws, and back.
 *
 * One heart-rate Observation is one five-minute roll-up window (D8; T06's builder, see
 * libs/pulsebridge_fhir/examples/observation-heart-rate.json):
 *   - `effectivePeriod` start/end: the window, in the user's UTC offset; `end` is exclusive.
 *   - `valueQuantity`: the window's AVERAGE, UCUM `/min` (beats per minute).
 *   - `component` LOINC 96935-2 / 55426-1: the window's minimum / maximum, also `/min`.
 *   - `status`: `final`, or `preliminary` while the window may still gain late samples (T10).
 *
 * Each readable Observation becomes exactly one `HeartRatePoint`, and the chart's keyboard
 * cursor, clicks and the selection all work on the index into `points`, so a selected point
 * always maps back to one stored Observation (`points[i].observation`). The extra rows the chart
 * needs to draw gaps are separate (`ChartSeries.x` with null values) and never selectable.
 */
import { LOINC, UCUM_SYSTEM, type Observation, type Quantity } from '../api/fhir';
import type { Selection } from '../state/selection';
import {
  formatClock,
  formatDay,
  formatWindow,
  offsetSegments,
  parseFhirDateTime,
  type OffsetSegment,
} from './time';

/** LOINC codes of the minimum and maximum components (libs/pulsebridge_fhir/README.md). */
export const COMPONENT_LOINC = { minimum: '96935-2', maximum: '55426-1' } as const;

export interface HeartRatePoint {
  observation: Observation;
  /** FHIR store id; what the selection and the trace call use. */
  id: string;
  startMs: number;
  endMs: number;
  /** Where the point is drawn: the middle of the window it averages. */
  midMs: number;
  /** UTC offset the window was recorded in (minutes). */
  offsetMinutes: number;
  average: number;
  /** Null when the Observation has no minimum / maximum component. */
  minimum: number | null;
  maximum: number | null;
  status: Observation['status'];
  /**
   * True for `preliminary` and `registered`: the window is still filling (T10), so it is drawn
   * dashed and labelled "provisional". Other non-final statuses (`amended`, `corrected`,
   * `unknown`) are not called provisional, but they are not shareable either (see `shareable`).
   */
  provisional: boolean;
  /** Only `final` windows are shared (T06/T15). */
  shareable: boolean;
}

export interface PreparedReadings {
  /** Oldest first. */
  points: HeartRatePoint[];
  /**
   * Observations that could not be drawn: no id, no valid period, a value or component that is
   * not UCUM `/min`, or numbers that contradict each other (see `toPoint`).
   */
  unreadable: number;
  /** `entered-in-error` or `cancelled`: R4 says these are not results, so they are left out. */
  withdrawn: number;
  /** Extra Observations for a window start already seen; the newest (`meta.lastUpdated`) is kept. */
  duplicates: number;
  segments: OffsetSegment[];
}

/** An empty set of readings (before the first load, or after an error). */
export function noReadings(): PreparedReadings {
  return {
    points: [],
    unreadable: 0,
    withdrawn: 0,
    duplicates: 0,
    segments: [],
  };
}

/**
 * The value of a Quantity in beats per minute, or null. It must say so in UCUM: `system`
 * http://unitsofmeasure.org and `code` `/min`, as T06 writes it. A bare number, a display-only
 * `unit`, or any other unit is refused rather than guessed at or drawn on the wrong scale.
 */
function perMinute(q: Quantity | undefined): number | null {
  if (!q || typeof q.value !== 'number' || !Number.isFinite(q.value)) return null;
  if (q.system !== UCUM_SYSTEM || q.code !== '/min') return null;
  return q.value;
}

/**
 * The value of the component with this LOINC code: a number, `absent` when there is no such
 * component (allowed: the band then shows the average only), or `invalid` when it is there but
 * not a UCUM `/min` value (the whole Observation is then unreadable).
 */
function component(obs: Observation, loinc: string): number | 'absent' | 'invalid' {
  const c = obs.component?.find((comp) => comp.code.coding?.some((cd) => cd.code === loinc));
  if (!c) return 'absent';
  return perMinute(c.valueQuantity) ?? 'invalid';
}

/**
 * One Observation to a point; 'withdrawn' or 'unreadable' when it cannot be drawn.
 *
 * Contradictory numbers make an Observation unreadable rather than "repaired": a minimum above
 * the maximum, or an average outside minimum..maximum, cannot all be true, and the page cannot
 * know which one is wrong. Counting it under "could not be shown" keeps the problem visible
 * instead of drawing a band that hides it.
 */
export function toPoint(obs: Observation): HeartRatePoint | 'withdrawn' | 'unreadable' {
  // R4 observation-status: entered-in-error and cancelled are not results to display.
  if (obs.status === 'entered-in-error' || obs.status === 'cancelled') return 'withdrawn';
  const start = parseFhirDateTime(obs.effectivePeriod?.start);
  const end = parseFhirDateTime(obs.effectivePeriod?.end);
  const average = perMinute(obs.valueQuantity);
  if (!obs.id || !start || !end || end.ms <= start.ms || average === null) return 'unreadable';
  const min = component(obs, COMPONENT_LOINC.minimum);
  const max = component(obs, COMPONENT_LOINC.maximum);
  if (min === 'invalid' || max === 'invalid') return 'unreadable';
  const minimum = min === 'absent' ? null : min;
  const maximum = max === 'absent' ? null : max;
  if (minimum !== null && (minimum > average || (maximum !== null && minimum > maximum)))
    return 'unreadable';
  if (maximum !== null && maximum < average) return 'unreadable';
  return {
    observation: obs,
    id: obs.id,
    startMs: start.ms,
    endMs: end.ms,
    midMs: (start.ms + end.ms) / 2,
    offsetMinutes: start.offsetMinutes,
    average,
    minimum,
    maximum,
    status: obs.status,
    // R4 observation-status: "registered" (no result yet) and "preliminary" (may be incomplete).
    provisional: obs.status === 'preliminary' || obs.status === 'registered',
    shareable: obs.status === 'final',
  };
}

/** `meta.lastUpdated` as milliseconds, or -Infinity when absent or not a date. */
function lastUpdated(obs: Observation): number {
  const t = Date.parse(obs.meta?.lastUpdated ?? '');
  return Number.isFinite(t) ? t : -Infinity;
}

/**
 * Observations to points, oldest first. Writes are idempotent (one Observation per window start,
 * T10), so two for the same start mean something went wrong upstream; the one updated last
 * (`meta.lastUpdated`, the later one in the list on a tie) is kept and the rest are counted.
 */
export function prepareReadings(observations: readonly Observation[]): PreparedReadings {
  const byStart = new Map<number, HeartRatePoint>();
  let unreadable = 0;
  let withdrawn = 0;
  let duplicates = 0;
  for (const obs of observations) {
    const p = toPoint(obs);
    if (p === 'unreadable') unreadable += 1;
    else if (p === 'withdrawn') withdrawn += 1;
    else {
      const seen = byStart.get(p.startMs);
      if (seen) {
        duplicates += 1;
        if (lastUpdated(p.observation) < lastUpdated(seen.observation)) continue;
      }
      byStart.set(p.startMs, p);
    }
  }
  const points = [...byStart.values()].sort((a, b) => a.startMs - b.startMs);
  const segments = offsetSegments(
    points.map((p) => ({ ms: p.startMs, offsetMinutes: p.offsetMinutes })),
  );
  return { points, unreadable, withdrawn, duplicates, segments };
}

/**
 * The aligned arrays uPlot draws (one x array, one y array per series, same length).
 * Rows are the points in order, plus one row with all-null values wherever the next window does
 * not start where the previous one ended: the watch was off (or charging), so the line breaks
 * there instead of drawing a straight line across hours with no data.
 */
export interface ChartSeries {
  x: number[];
  /** Average of windows that are not provisional (solid line); null elsewhere. */
  averageSettled: (number | null)[];
  /** Average of provisional windows, plus the point just before them so the dashed line joins. */
  averageProvisional: (number | null)[];
  minimum: (number | null)[];
  maximum: (number | null)[];
  /** The point index of each row, or -1 for a gap row. */
  pointOfRow: number[];
  /** Number of gaps (places the line breaks). */
  gaps: number;
}

export function chartSeries(points: readonly HeartRatePoint[]): ChartSeries {
  const s: ChartSeries = {
    x: [],
    averageSettled: [],
    averageProvisional: [],
    minimum: [],
    maximum: [],
    pointOfRow: [],
    gaps: 0,
  };
  let previous: HeartRatePoint | undefined;
  points.forEach((p, i) => {
    if (previous && p.startMs > previous.endMs) {
      // Gap row at the end of the last window with data: every series is null here.
      s.x.push(previous.endMs);
      s.averageSettled.push(null);
      s.averageProvisional.push(null);
      s.minimum.push(null);
      s.maximum.push(null);
      s.pointOfRow.push(-1);
      s.gaps += 1;
    } else if (previous && p.provisional && !previous.provisional) {
      // Join the dashed provisional line to the last settled point before it.
      const last = s.averageProvisional.length - 1;
      s.averageProvisional[last] = previous.average;
    }
    s.x.push(p.midMs);
    s.averageSettled.push(p.provisional ? null : p.average);
    s.averageProvisional.push(p.provisional ? p.average : null);
    s.minimum.push(p.minimum ?? p.average);
    s.maximum.push(p.maximum ?? p.average);
    s.pointOfRow.push(i);
    previous = p;
  });
  return s;
}

export interface WindowStats {
  /** Windows with data in the range. */
  count: number;
  /** Of those, how many are provisional (`preliminary` or `registered`). */
  provisional: number;
  /** Lowest per-window minimum (the window's average where it has no minimum component). */
  minimum: number;
  /**
   * Arithmetic mean of the five-minute AVERAGES, each window counted once. It is not a
   * time-weighted or sample-weighted mean of every beat: a window with few samples counts as
   * much as a full one, and hours with no data do not count at all.
   */
  mean: number;
  /** Highest per-window maximum (the window's average where it has no maximum component). */
  maximum: number;
}

/** Statistics over exactly the points the chart shows; null when there are none. */
export function windowStats(points: readonly HeartRatePoint[]): WindowStats | null {
  if (points.length === 0) return null;
  let min = Infinity;
  let max = -Infinity;
  let sum = 0;
  let provisional = 0;
  for (const p of points) {
    min = Math.min(min, p.minimum ?? p.average);
    max = Math.max(max, p.maximum ?? p.average);
    sum += p.average;
    if (p.provisional) provisional += 1;
  }
  return {
    count: points.length,
    provisional,
    minimum: min,
    mean: sum / points.length,
    maximum: max,
  };
}

/** Index of the point whose middle is nearest to `ms`; -1 when there are none. */
export function nearestPoint(points: readonly HeartRatePoint[], ms: number): number {
  if (points.length === 0) return -1;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((points[mid]?.midMs ?? 0) < ms) lo = mid + 1;
    else hi = mid;
  }
  const before = points[lo - 1];
  const at = points[lo];
  if (before && at && ms - before.midMs <= at.midMs - ms) return lo - 1;
  return lo;
}

/** A value in beats per minute as text: whole numbers plain, others to one decimal. */
export function bpm(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** "final", "provisional (status preliminary)", or "status amended: not shared (only final is)". */
export function statusText(p: HeartRatePoint): string {
  if (p.shareable) return 'final';
  if (p.provisional) return `provisional (status ${p.status})`;
  return `status ${p.status}: not shared (only final is)`;
}

/** Everything about one window in one sentence, for the readout and screen readers. */
export function describePoint(p: HeartRatePoint): string {
  const range =
    p.minimum !== null && p.maximum !== null
      ? `, range ${bpm(p.minimum)} to ${bpm(p.maximum)}`
      : '';
  return `${formatWindow(p.startMs, p.endMs, p.offsetMinutes)}: average ${bpm(p.average)} beats/min${range}; ${statusText(p)}`;
}

/** Why a non-final window cannot be shared (shown next to the disabled Share button). */
export function unshareableReason(p: HeartRatePoint): string | null {
  if (p.shareable) return null;
  return p.provisional
    ? `this five-minute window is still provisional (FHIR status "${p.status}"): the watch may still upload samples for it, so it becomes final after a later sync. Only final readings are shared.`
    : `this window's FHIR status is "${p.status}", not "final". Only final readings are shared.`;
}

/** The page-wide selection for a chosen point. */
export function selectionFor(p: HeartRatePoint): Selection {
  return {
    source: 'watch',
    observationId: p.id,
    summary: `Heart rate ${bpm(p.average)} /min, ${formatDay(p.startMs, p.offsetMinutes)} ${formatClock(p.startMs, p.offsetMinutes)}–${formatClock(p.endMs, p.offsetMinutes)}`,
    unshareableReason: unshareableReason(p),
  };
}

/** The LOINC code this module charts (search parameter `code`). */
export const HEART_RATE_CODE = LOINC.heartRate;
