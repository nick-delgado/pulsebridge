/**
 * Helpers for this directory's unit tests only (imported by *.test.ts, never by the page).
 * Every value is SYNTHETIC and hand-made; no real reading is ever used.
 */
import type { Observation } from '../api/fhir';

export interface WindowSpec {
  /** Window start, an R4 dateTime with its offset, for example "2026-01-01T09:05:00-05:00". */
  start: string;
  /** Window end; defaults to five minutes after start, in the same offset. */
  end?: string;
  avg: number;
  min?: number;
  max?: number;
  status?: Observation['status'];
  id?: string;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** Adds minutes to an R4 dateTime and writes it back in the same offset. */
export function plusMinutes(dateTime: string, minutes: number): string {
  const offset = /(Z|[+-]\d{2}:\d{2})$/.exec(dateTime)?.[1] ?? 'Z';
  const offMin =
    offset === 'Z'
      ? 0
      : (offset.startsWith('-') ? -1 : 1) *
        (Number(offset.slice(1, 3)) * 60 + Number(offset.slice(4)));
  const wall = new Date(Date.parse(dateTime) + (minutes + offMin) * 60_000);
  return (
    `${String(wall.getUTCFullYear())}-${pad(wall.getUTCMonth() + 1)}-${pad(wall.getUTCDate())}` +
    `T${pad(wall.getUTCHours())}:${pad(wall.getUTCMinutes())}:${pad(wall.getUTCSeconds())}${offset}`
  );
}

const perMinute = (value: number) => ({
  value,
  unit: 'beats/minute',
  system: 'http://unitsofmeasure.org',
  code: '/min',
});

/** A heart-rate Observation with the element structure of T06's example. */
export function hrObservation(spec: WindowSpec): Observation {
  const end = spec.end ?? plusMinutes(spec.start, 5);
  return {
    resourceType: 'Observation',
    id: spec.id ?? `hr-${String(Date.parse(spec.start))}`,
    status: spec.status ?? 'final',
    category: [
      {
        coding: [
          {
            system: 'http://terminology.hl7.org/CodeSystem/observation-category',
            code: 'vital-signs',
          },
        ],
      },
    ],
    code: { coding: [{ system: 'http://loinc.org', code: '8867-4', display: 'Heart rate' }] },
    effectivePeriod: { start: spec.start, end },
    valueQuantity: perMinute(spec.avg),
    device: { reference: 'Device/test-watch' },
    component: [
      ...(spec.min === undefined
        ? []
        : [
            {
              code: { coding: [{ system: 'http://loinc.org', code: '96935-2' }] },
              valueQuantity: perMinute(spec.min),
            },
          ]),
      ...(spec.max === undefined
        ? []
        : [
            {
              code: { coding: [{ system: 'http://loinc.org', code: '55426-1' }] },
              valueQuantity: perMinute(spec.max),
            },
          ]),
    ],
  };
}
