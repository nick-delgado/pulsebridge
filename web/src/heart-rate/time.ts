/**
 * Times on the heart-rate chart: the reading's OWN local time, as recorded, never the viewer's
 * browser zone.
 *
 * Why: this is a personal health record. "My heart rate was 110 at 07:30" means 07:30 where the
 * wearer was, whichever zone the person looking at the chart is in. health-sync (T10, through
 * T06's builder) writes each window's `effectivePeriod` in the user's zone, for example
 * `2026-01-01T09:05:00-05:00`, so the offset is part of the data and we read it from there.
 *
 * How: every time is kept as a UTC instant (milliseconds) plus the UTC offset its Observation
 * carried. The x axis is linear in instants, so a daylight-saving change can neither stack two
 * hours on top of each other nor leave a hole. Only the LABELS use local wall-clock time, and a
 * tick after the offset changes also names the new offset ("01:00" twice in the autumn would
 * otherwise look like a duplicate). Formatting uses the UTC getters on a shifted Date, so the
 * result does not depend on the browser's zone or locale.
 */

export const MINUTE_MS = 60_000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

/**
 * An R4 `dateTime` with a time part. R4 says: "If hours and minutes are specified, a time zone
 * SHALL be populated", so a value without `Z` or `±hh:mm` is not valid and we do not guess one.
 * https://hl7.org/fhir/R4/datatypes.html#dateTime
 */
const FHIR_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/;

export interface RecordedInstant {
  /** The UTC instant, milliseconds since the epoch. */
  ms: number;
  /** The UTC offset the value was written with, in minutes (New York in winter: -300). */
  offsetMinutes: number;
}

/** Parses an R4 dateTime that has a time and a zone; null for anything else. */
export function parseFhirDateTime(value: string | undefined): RecordedInstant | null {
  if (value === undefined) return null;
  const match = FHIR_DATETIME.exec(value);
  if (!match) return null;
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  if (match[1] === 'Z') return { ms, offsetMinutes: 0 };
  const sign = match[2] === '-' ? -1 : 1;
  const offsetMinutes = sign * (Number(match[3]) * 60 + Number(match[4]));
  return { ms, offsetMinutes };
}

/** "UTC+00:00", "UTC-05:00", "UTC+05:30". */
export function formatOffset(offsetMinutes: number): string {
  const sign = offsetMinutes < 0 ? '-' : '+';
  const abs = Math.abs(offsetMinutes);
  return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** The wall clock at `ms` in a zone with this offset. */
function wall(ms: number, offsetMinutes: number): Date {
  return new Date(ms + offsetMinutes * MINUTE_MS);
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** "09:05" in the recorded zone. */
export function formatClock(ms: number, offsetMinutes: number): string {
  const d = wall(ms, offsetMinutes);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** "Thu 1 Jan" in the recorded zone. */
export function formatDay(ms: number, offsetMinutes: number): string {
  const d = wall(ms, offsetMinutes);
  return `${WEEKDAYS[d.getUTCDay()] ?? ''} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()] ?? ''}`;
}

/** "1 Jan" in the recorded zone (short, for a crowded axis). */
export function formatShortDay(ms: number, offsetMinutes: number): string {
  const d = wall(ms, offsetMinutes);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()] ?? ''}`;
}

/**
 * "Thu 1 Jan 2026, 09:05–09:10 (UTC-05:00)": a window with both ends in the offset of its start,
 * which the label names, so the window that ends at a clock change reads "01:55–02:00 (UTC-04:00)"
 * rather than the confusing "01:55–01:00".
 */
export function formatWindow(startMs: number, endMs: number, offsetMinutes: number): string {
  const year = wall(startMs, offsetMinutes).getUTCFullYear();
  return (
    `${formatDay(startMs, offsetMinutes)} ${year}, ` +
    `${formatClock(startMs, offsetMinutes)}–${formatClock(endMs, offsetMinutes)} ` +
    `(${formatOffset(offsetMinutes)})`
  );
}

/**
 * From `fromMs` on, readings carry `offsetMinutes`. A list of these, oldest first, describes
 * the zone over a range; a daylight-saving change adds a second segment.
 */
export interface OffsetSegment {
  fromMs: number;
  offsetMinutes: number;
}

/**
 * Collapses (instant, offset) pairs, oldest first, into segments. A segment starts at the first
 * reading that has the new offset; if the watch was off across the change, the switch is placed
 * at the first reading after it (the true moment is not in the data).
 */
export function offsetSegments(readings: readonly RecordedInstant[]): OffsetSegment[] {
  const out: OffsetSegment[] = [];
  for (const r of readings) {
    if (out.at(-1)?.offsetMinutes !== r.offsetMinutes) {
      out.push({ fromMs: r.ms, offsetMinutes: r.offsetMinutes });
    }
  }
  return out;
}

/** The offset in force at `ms`: the last segment starting at or before it, else the first. */
export function offsetAt(segments: readonly OffsetSegment[], ms: number): number {
  let offset = segments[0]?.offsetMinutes ?? 0;
  for (const s of segments) {
    if (s.fromMs <= ms) offset = s.offsetMinutes;
    else break;
  }
  return offset;
}

export interface TimeTick {
  ms: number;
  /** Text under the axis; "\n" separates lines. */
  label: string;
}

/** Tick spacings, smallest first. Multiples of a day fall on local midnight. */
const STEPS = [HOUR_MS, 2 * HOUR_MS, 3 * HOUR_MS, 6 * HOUR_MS, 12 * HOUR_MS, DAY_MS, 2 * DAY_MS];
const WEEK_STEP = 7 * DAY_MS;

/** The smallest step that keeps the number of ticks in [minMs, maxMs] at or under `maxTicks`. */
export function tickStep(minMs: number, maxMs: number, maxTicks: number): number {
  const span = Math.max(0, maxMs - minMs);
  return STEPS.find((s) => span / s <= Math.max(1, maxTicks)) ?? WEEK_STEP;
}

/**
 * Axis ticks for [minMs, maxMs], placed on round LOCAL times (every 3 h at 00:00, 03:00, …, or
 * every day at local midnight) in the zone of each offset segment.
 *
 * Across a daylight-saving change each segment is ticked in its own offset, so ticks never
 * collide and none is invented: in spring, hourly ticks read 01:00 then 03:00 (02:00 did not
 * exist); in autumn they read 01:00, then "01:00 UTC-05:00", an hour later. The first tick after a
 * change names the new offset so no two labels read the same.
 */
export function timeTicks(
  minMs: number,
  maxMs: number,
  segments: readonly OffsetSegment[],
  maxTicks: number,
): TimeTick[] {
  if (!(maxMs > minMs)) return [];
  const step = tickStep(minMs, maxMs, maxTicks);
  const segs: OffsetSegment[] =
    segments.length > 0 ? [...segments] : [{ fromMs: minMs, offsetMinutes: 0 }];
  const ticks: TimeTick[] = [];
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    if (!seg) continue;
    const next = segs[i + 1];
    const from = i === 0 ? minMs : Math.max(minMs, seg.fromMs);
    const until = next ? Math.min(maxMs, next.fromMs) : maxMs;
    if (from > until) continue;
    const off = seg.offsetMinutes * MINUTE_MS;
    // Round local times are multiples of `step` on the shifted (wall-clock) time line.
    let local = Math.ceil((from + off) / step) * step;
    let firstInSegment = true;
    for (; local - off <= until; local += step) {
      const ms = local - off;
      // The next segment owns its own start, so a tick exactly there is ticked once, by it.
      if (next && ms >= next.fromMs) break;
      ticks.push({
        ms,
        label: tickLabel(ms, seg.offsetMinutes, step, ticks.length === 0, i > 0 && firstInSegment),
      });
      firstInSegment = false;
    }
  }
  return ticks;
}

function tickLabel(
  ms: number,
  offsetMinutes: number,
  step: number,
  first: boolean,
  offsetChanged: boolean,
): string {
  let label: string;
  if (step >= DAY_MS) {
    label = formatShortDay(ms, offsetMinutes);
  } else {
    const clock = formatClock(ms, offsetMinutes);
    // Name the day on the first tick and at each local midnight.
    label = first || clock === '00:00' ? `${clock}\n${formatShortDay(ms, offsetMinutes)}` : clock;
  }
  return offsetChanged ? `${label}\n${formatOffset(offsetMinutes)}` : label;
}
