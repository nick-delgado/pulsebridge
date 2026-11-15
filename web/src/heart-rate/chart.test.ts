import { describe, expect, it } from 'vitest';
import { pointAtPosition } from './chart';
import { prepareReadings } from './data';
import { hrObservation, plusMinutes } from './test-support';

const T0 = '2026-01-01T09:00:00-05:00';
const START = Date.parse('2026-01-01T14:00:00Z');

describe('which window a click at a position selects', () => {
  // Four windows, 09:00 to 09:20 local, then a gap to 10:00.
  const points = prepareReadings([
    hrObservation({ start: T0, avg: 60, id: 'a' }),
    hrObservation({ start: plusMinutes(T0, 5), avg: 61, id: 'b' }),
    hrObservation({ start: plusMinutes(T0, 10), avg: 62, id: 'c' }),
    hrObservation({ start: plusMinutes(T0, 60), avg: 63, id: 'd' }),
  ]).points;
  // A plot 650 px wide showing 14:00Z to 15:05Z: one pixel is six seconds.
  const posToVal = (left: number) => START + left * 6000;
  const idAt = (left: number) => {
    const i = pointAtPosition(points, posToVal, left);
    return i === null ? null : points[i]?.id;
  };

  it('picks the window whose middle is under the pointer', () => {
    expect(idAt(25)).toBe('a'); // 14:02:30, a's middle
    expect(idAt(75)).toBe('b'); // 14:07:30, b's middle
    expect(idAt(125)).toBe('c'); // 14:12:30, c's middle
    expect(idAt(625)).toBe('d'); // 15:02:30, d's middle
  });

  it('picks the nearer window at the plot edges and inside a gap', () => {
    expect(idAt(0)).toBe('a'); // the left edge
    expect(idAt(650)).toBe('d'); // the right edge
    expect(idAt(200)).toBe('c'); // 14:20, in the gap, nearer c (14:12:30) than d (15:02:30)
    expect(idAt(400)).toBe('d'); // 14:40, nearer d
  });

  it('selects nothing when there are no windows', () => {
    expect(pointAtPosition([], posToVal, 10)).toBeNull();
  });
});
