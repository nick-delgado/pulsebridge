/**
 * SYNTHETIC data for mock mode. Nothing here comes from a real person or a real watch: heart-rate
 * values come from a formula plus a hash of the window start time, and the patient is the made-up
 * `TESTPATIENT^SYNTHIA` (`SYN-000123`) used by libs/pulsebridge_hl7v2/samples.
 *
 * Shapes follow the real ones:
 * - heart-rate Observations: libs/pulsebridge_fhir/examples/observation-heart-rate.json (T06)
 * - blood-pressure panel Observations: .../observation-blood-pressure.json (T06)
 * - roll-up windows: docs/spikes/s3.md section 6 (Google Health `dataPoints:rollUp`)
 * - HL7v2 sample names and their expected outcomes: libs/pulsebridge_hl7v2/samples/samples.json
 */
import type { GoogleHealthRollUpWindow, MalformedSample } from './contract';
import {
  LOINC,
  LOINC_SYSTEM,
  UCUM_SYSTEM,
  type CodeableConcept,
  type Device,
  type Observation,
  type Patient,
  type Provenance,
  type Quantity,
} from './fhir';

export const MOCK_PATIENT_REF = 'Patient/mock-patient';
export const MOCK_WATCH_REF = 'Device/mock-watch';
export const MOCK_CUFF_REF = 'Device/mock-cuff';

/** Five-minute roll-up windows (D8). */
export const WINDOW_MS = 5 * 60 * 1000;
/** Windows that end within this long before "now" are still `preliminary` (T10's cutoff). */
export const PRELIMINARY_MS = 30 * 60 * 1000;

const HR_ID_PREFIX = 'mock-hr-';

/** vital-signs category: http://terminology.hl7.org/CodeSystem/observation-category */
export const VITAL_SIGNS: CodeableConcept = {
  coding: [
    {
      system: 'http://terminology.hl7.org/CodeSystem/observation-category',
      code: 'vital-signs',
      display: 'Vital Signs',
    },
  ],
  text: 'Vital Signs',
};

function loinc(code: string, display: string, text: string): CodeableConcept {
  return { coding: [{ system: LOINC_SYSTEM, code, display }], text };
}

/** UCUM `/min` for beats per minute. */
function perMinute(value: number): Quantity {
  return { value, unit: 'beats/minute', system: UCUM_SYSTEM, code: '/min' };
}

/** UCUM `mm[Hg]` for blood pressure. */
function mmHg(value: number): Quantity {
  return { value, unit: 'mmHg', system: UCUM_SYSTEM, code: 'mm[Hg]' };
}

/** 32-bit FNV-1a hash: a cheap, deterministic source of noise from a number. */
export function hash32(input: number | string): number {
  let h = 0x811c9dc5;
  const s = String(input);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function unit(n: number, salt: string): number {
  return hash32(`${salt}:${n}`) / 0x100000000;
}

/**
 * The synthetic roll-up for the window starting at `startMs`, or null when the "watch was not
 * worn" (roll-ups leave such windows out, docs/spikes/s3.md). Same input, same output.
 */
export function syntheticRollUp(startMs: number): GoogleHealthRollUpWindow | null {
  const hour = new Date(startMs).getUTCHours() + new Date(startMs).getUTCMinutes() / 60;
  // Charging gap: about 40 minutes each day.
  if (hour >= 12 && hour < 12.67) return null;
  const asleep = hour < 6 || hour >= 23;
  const daytime = 64 + 9 * Math.sin(((hour - 9) / 24) * 2 * Math.PI);
  const base = asleep ? 55 : daytime;
  const activity = unit(startMs, 'act') < 0.04 ? 25 + 20 * unit(startMs, 'act2') : 0;
  const avg = Math.round((base + activity + 4 * (unit(startMs, 'n') - 0.5)) * 10) / 10;
  const min = Math.round(avg - 2 - 5 * unit(startMs, 'min'));
  const max = Math.round(avg + 3 + 10 * unit(startMs, 'max'));
  return {
    startTime: new Date(startMs).toISOString(),
    endTime: new Date(startMs + WINDOW_MS).toISOString(),
    heartRate: { beatsPerMinuteMin: min, beatsPerMinuteAvg: avg, beatsPerMinuteMax: max },
  };
}

/** A pseudo-identifier in UUID layout, derived from a seed (not a real UUID v5). */
export function pseudoUuid(seed: string): string {
  const hex = [0, 1, 2, 3]
    .map((i) => hash32(`${seed}#${i}`).toString(16).padStart(8, '0'))
    .join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

/** Heart-rate Observation for one roll-up window, shaped like T06's example. */
export function heartRateObservation(window: GoogleHealthRollUpWindow, nowMs: number): Observation {
  const startMs = Date.parse(window.startTime);
  const endMs = Date.parse(window.endTime);
  return {
    resourceType: 'Observation',
    id: `${HR_ID_PREFIX}${startMs}`,
    meta: { profile: ['http://hl7.org/fhir/StructureDefinition/heartrate'] },
    identifier: [
      { use: 'official', system: 'urn:pulsebridge:observation', value: pseudoUuid(`hr${startMs}`) },
    ],
    status: endMs > nowMs - PRELIMINARY_MS ? 'preliminary' : 'final',
    category: [VITAL_SIGNS],
    code: loinc(LOINC.heartRate, 'Heart rate', 'Heart rate'),
    subject: { reference: MOCK_PATIENT_REF },
    effectivePeriod: { start: window.startTime, end: window.endTime },
    valueQuantity: perMinute(window.heartRate.beatsPerMinuteAvg),
    method: {
      text: "Mean of the device's samples over effectivePeriod; components give the minimum and maximum",
    },
    device: { reference: MOCK_WATCH_REF },
    component: [
      {
        code: loinc(
          '96935-2',
          'Heart rate unspecified time minimum by Pedometer',
          'Heart rate minimum',
        ),
        valueQuantity: perMinute(window.heartRate.beatsPerMinuteMin),
      },
      {
        code: loinc(
          '55426-1',
          'Heart rate unspecified time maximum by Pedometer',
          'Heart rate maximum',
        ),
        valueQuantity: perMinute(window.heartRate.beatsPerMinuteMax),
      },
    ],
  };
}

/** Window start (ms) encoded in a mock heart-rate Observation id, or null if it is not one. */
export function windowStartFromId(id: string): number | null {
  if (!id.startsWith(HR_ID_PREFIX)) return null;
  const ms = Number(id.slice(HR_ID_PREFIX.length));
  return Number.isInteger(ms) && ms % WINDOW_MS === 0 ? ms : null;
}

/** All complete windows that start in [startMs, endMs) and end by nowMs, oldest first. */
export function heartRateObservations(
  startMs: number,
  endMs: number,
  nowMs: number,
): Observation[] {
  const out: Observation[] = [];
  const first = Math.ceil(startMs / WINDOW_MS) * WINDOW_MS;
  for (let t = first; t < endMs && t + WINDOW_MS <= nowMs; t += WINDOW_MS) {
    const window = syntheticRollUp(t);
    if (window) out.push(heartRateObservation(window, nowMs));
  }
  return out;
}

export function bloodPressureObservation(
  id: string,
  effective: string,
  systolic: number,
  diastolic: number,
): Observation {
  return {
    resourceType: 'Observation',
    id,
    meta: { profile: ['http://hl7.org/fhir/StructureDefinition/bp'] },
    identifier: [{ use: 'official', system: 'urn:pulsebridge:observation', value: pseudoUuid(id) }],
    status: 'final',
    category: [VITAL_SIGNS],
    code: loinc(
      LOINC.bloodPressurePanel,
      'Blood pressure panel with all children optional',
      'Blood pressure',
    ),
    subject: { reference: MOCK_PATIENT_REF },
    effectiveDateTime: effective,
    device: { reference: MOCK_CUFF_REF },
    component: [
      {
        code: loinc(LOINC.systolic, 'Systolic blood pressure', 'Systolic'),
        valueQuantity: mmHg(systolic),
      },
      {
        code: loinc(LOINC.diastolic, 'Diastolic blood pressure', 'Diastolic'),
        valueQuantity: mmHg(diastolic),
      },
    ],
  };
}

export function provenanceFor(
  target: string,
  recorded: string,
  source: 'watch' | 'cuff',
  message?: string,
): Provenance {
  return {
    resourceType: 'Provenance',
    id: `mock-prov-${hash32(target).toString(16)}`,
    target: [{ reference: target }],
    recorded,
    agent: [
      {
        type: {
          coding: [
            {
              system: 'http://terminology.hl7.org/CodeSystem/provenance-participant-type',
              code: 'device',
            },
          ],
        },
        who: { reference: source === 'watch' ? MOCK_WATCH_REF : MOCK_CUFF_REF },
      },
    ],
    ...(message ? { entity: [{ role: 'source', what: { display: message } }] } : {}),
  };
}

export const MOCK_WATCH: Device = {
  resourceType: 'Device',
  id: 'mock-watch',
  identifier: [{ system: 'urn:pulsebridge:device', value: pseudoUuid('watch') }],
  deviceName: [{ name: 'Synthetic watch (mock mode)', type: 'user-friendly-name' }],
};

/** The synthetic patient as the clinic would store it (OpenEMR read-back in mock mode). */
export function openemrPatient(id: string): Patient {
  return {
    resourceType: 'Patient',
    id,
    meta: { lastUpdated: '2026-01-01T00:00:00Z' },
    identifier: [
      {
        system: 'urn:pulsebridge:patient',
        value: 'SYN-000123',
        type: { coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v2-0203', code: 'PT' }] },
      },
    ],
    name: [{ use: 'official', family: 'Testpatient', given: ['Synthia'] }],
    gender: 'female',
    birthDate: '1980-01-01',
  };
}

/**
 * The malformed samples "send malformed" offers, named exactly as in
 * libs/pulsebridge_hl7v2/samples/samples.json, with the outcome that file expects at each layer.
 */
export interface MockSampleBehaviour extends MalformedSample {
  /** null: no acknowledgment (timeout). */
  commitAck: 'AA' | 'AE' | null;
  commitReason: string | null;
  application: 'accepted' | 'error' | null;
  mappingReason: string | null;
}

export const MOCK_SAMPLES: MockSampleBehaviour[] = [
  {
    name: 'invalid_missing_pid3',
    label: 'Missing patient identifier (PID-3)',
    class: 'structurally-invalid',
    transports: ['mllp', 'https'],
    commitAck: 'AA',
    commitReason: null,
    application: 'error',
    mappingReason: 'missing-patient-identifier',
  },
  {
    name: 'unparseable_no_msh',
    label: 'No MSH segment',
    class: 'unparseable',
    transports: ['mllp', 'https'],
    commitAck: 'AE',
    commitReason: 'Invalid MSH header',
    application: null,
    mappingReason: null,
  },
  {
    name: 'unparseable_lf_terminators',
    label: 'Line-feed segment terminators',
    class: 'unparseable',
    transports: ['mllp', 'https'],
    commitAck: 'AE',
    commitReason: 'invalid segment terminator, store expects: "\\r"',
    application: null,
    mappingReason: null,
  },
  {
    name: 'noack_missing_end_block',
    label: 'MLLP frame without its end block',
    class: 'no-acknowledgment',
    transports: ['mllp'],
    commitAck: null,
    commitReason: null,
    application: null,
    mappingReason: null,
  },
  {
    name: 'unmappable_unknown_obx3_code',
    label: 'Unknown OBX-3 code',
    class: 'parses-but-cannot-map',
    transports: ['mllp', 'https'],
    commitAck: 'AA',
    commitReason: null,
    application: 'error',
    mappingReason: 'unknown-obx-3-code',
  },
  {
    name: 'unmappable_missing_obx14',
    label: 'Missing OBX-14 (observation time)',
    class: 'parses-but-cannot-map',
    transports: ['mllp', 'https'],
    commitAck: 'AA',
    commitReason: null,
    application: 'error',
    mappingReason: 'missing-obx-14',
  },
  {
    name: 'unmappable_non_numeric_obx5',
    label: 'Non-numeric OBX-5 (value)',
    class: 'parses-but-cannot-map',
    transports: ['mllp', 'https'],
    commitAck: 'AA',
    commitReason: null,
    application: 'error',
    mappingReason: 'non-numeric-obx-5',
  },
];

/** HL7v2 timestamp (DTM, YYYYMMDDHHMMSS+ZZZZ) in UTC. */
export function hl7Time(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}+0000`;
}

/**
 * A display copy of the ORU^R01 the simulator would send (segments joined with "\r", as on the
 * wire). Mock mode only: the real message is built and validated with hl7apy by T07/T13.
 */
export function mockOruR01(
  controlId: string,
  ms: number,
  systolic: number,
  diastolic: number,
): string {
  const ts = hl7Time(ms);
  return [
    `MSH|^~\\&|PULSEBRIDGE-CUFF|PULSEBRIDGE|PULSEBRIDGE|PULSEBRIDGE|${ts}||ORU^R01^ORU_R01|${controlId}|P|2.5.1`,
    'PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA',
    `OBR|1|||85354-9^Blood pressure panel^LN|||${ts}`,
    `OBX|1|NM|8480-6^Systolic blood pressure^LN||${systolic}|mm[Hg]|||||F|||${ts}`,
    `OBX|2|NM|8462-4^Diastolic blood pressure^LN||${diastolic}|mm[Hg]|||||F|||${ts}`,
  ].join('\r');
}

/** A display copy of the store's ACK. Per spike S4 its type is `ACK` and MSA-2 is empty on AE. */
export function mockAck(
  code: 'AA' | 'AE',
  controlId: string | null,
  reason: string | null,
  ms: number,
): string {
  const ts = hl7Time(ms);
  const msa = code === 'AA' ? `MSA|AA|${controlId ?? ''}` : `MSA|AE||${reason ?? ''}`;
  return [`MSH|^~\\&|||||${ts}||ACK|${pseudoUuid(`ack${ms}`).slice(0, 8)}|P|2.5.1`, msa].join('\r');
}
