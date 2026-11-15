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
import type {
  CommitAck,
  GoogleHealthRollUpWindow,
  MalformedClass,
  MalformedSample,
  MllpFraming,
  SendAttemptEntry,
  Transport,
} from './contract';
import {
  LOINC,
  LOINC_SYSTEM,
  UCUM_SYSTEM,
  type AuditEvent,
  type CodeableConcept,
  type Device,
  type Observation,
  type Patient,
  type Provenance,
  type Quantity,
} from './fhir';
import mockSamples from './mock-samples.json';
import acceptedExample from './t31-examples/auditevent-send-accepted.json';
import noAckExample from './t31-examples/auditevent-send-no-ack.json';
import rejectedExample from './t31-examples/auditevent-send-rejected.json';
import unreadableExample from './t31-examples/auditevent-send-unreadable-reply.json';

export const MOCK_PATIENT_REF = 'Patient/mock-patient';
export const MOCK_WATCH_REF = 'Device/mock-watch';
export const MOCK_CUFF_REF = 'Device/mock-cuff';

/** Five-minute roll-up windows (D8). */
export const WINDOW_MS = 5 * 60 * 1000;
/** Windows that end within this long before a sync run are still `preliminary` (T10's cutoff). */
export const PRELIMINARY_MS = 30 * 60 * 1000;

/**
 * The mock's health-sync schedule: a run every 15 minutes (brief, "Ingestion, wearable"), at
 * minute 2, 17, 32 and 47 of each hour. Data reaches the store only at a run, so the mock's
 * heart-rate windows, their `preliminary`/`final` status and the status strip's last sync time
 * all move together, once per run. That is what lets the status strip see a new sync during a
 * session and announce it (`notifyDataChanged('sync')`).
 */
export const SYNC_INTERVAL_MS = 15 * 60 * 1000;
export const SYNC_OFFSET_MS = 2 * 60 * 1000;

/** The time of the most recent mock sync run at or before `nowMs`. */
export function lastSyncRun(nowMs: number): number {
  return (
    Math.floor((nowMs - SYNC_OFFSET_MS) / SYNC_INTERVAL_MS) * SYNC_INTERVAL_MS + SYNC_OFFSET_MS
  );
}

// ---------------------------------------------------------------------------------------------
// Mock data sets: the zone the heart-rate timestamps are written in.

/**
 * A mock data set, chosen with `?mock-data=<id>` on the mock page (index.ts):
 * - `utc` (default): every `effectivePeriod` in UTC (`...Z`), so the page shows "UTC+00:00".
 * - `dst-new-york`: every `effectivePeriod` written in the FIXED IANA zone America/New_York,
 *   with the UTC offset in force at that instant, the way health-sync writes the user's zone
 *   through T06's `fhir_datetime` (`2026-11-01T01:30:00-04:00`, then `...-05:00` after the
 *   autumn change). The zone is fixed by the data set, never the browser's, so the page must show
 *   New York times whatever zone the viewer is in. Any range that contains a New York clock change
 *   crosses it (24 h: just after one; 30 d: four weeks after one; the browser test pins the page's
 *   clock to 2026-11-01 12:00 EST, eleven hours after the autumn change).
 */
export const MOCK_DATA_SETS = {
  utc: { id: 'utc', timeZone: null, label: 'UTC timestamps', hostile: false },
  'dst-new-york': {
    id: 'dst-new-york',
    timeZone: 'America/New_York',
    label: 'America/New_York timestamps, across daylight-saving changes',
    hostile: false,
  },
  // UTC data plus markup in every text that comes from another system (see HOSTILE_MARKUP), so a
  // browser test can prove the page shows it as text under the real Content-Security-Policy.
  hostile: {
    id: 'hostile',
    timeZone: null,
    label: 'Markup in text from other systems (MSA-3, sample text, diagnostics)',
    hostile: true,
  },
} as const;

/**
 * The markup the `hostile` data set puts into text from other systems: an MSA-3 reason (and so
 * the raw ACK), each sample's description, a malformed sample's segment text (an extra NTE
 * segment) and the diagnostics of a refusal. Rendered as markup it would run script or load an
 * image, which the CSP would report; rendered as text it is just characters. It ends with one
 * long unbroken token, which must wrap rather than widen the page.
 */
export const HOSTILE_MARKUP =
  `<img src="x" onerror="alert('pulsebridge-hostile-mock')">` +
  `<script>alert('pulsebridge-hostile-mock')</script> ` +
  // One 612-character token with no break opportunity, to prove long text wraps on a phone.
  'PULSEBRIDGEHOSTILE'.repeat(34);

/**
 * In the `hostile` data set a built reading with this systolic value is refused with a 422 whose
 * diagnostics hold `HOSTILE_MARKUP`: a refusal the form's own checks cannot catch.
 */
export const HOSTILE_REFUSED_SYSTOLIC = 333;

/** The audit failure named by `?mock-audit=build|write` (MockClientOptions.auditFailure), or null. */
export function mockAuditFailureFromSearch(search: string): 'build' | 'write' | null {
  const value = new URLSearchParams(search).get('mock-audit');
  return value === 'build' || value === 'write' ? value : null;
}

/**
 * The switches for states the built mock page cannot otherwise reach (browser tests only; mock
 * builds only: this module is not in an HTTP build, which scripts/check-bundle.mjs proves
 * through the markers this module holds):
 * - `?mock-lookup=not-found|failed`: the HL7v2 store does not have (or cannot be asked for) the
 *   message name of ANY accepted send, as when cuff-sim's derived MLLP name is wrong
 *   (`messageLookup`, amendment 1). With `not-found`, an MLLP message nobody else claims is
 *   listed as a store-only row with a `possibleDuplicateOf` hint (amendment 14).
 * - `?mock-log=partial`: every messages-log page says `partial: true` (amendment 16).
 * - `?mock-status=openemr-not-checked|counts-unknown|health-sync-timeout|not-configured`: the
 *   status the proxy sends before T15 installs its OpenEMR probe (amendment 10), when the store
 *   could not be counted (null counts, amendment 15), when health-sync did not answer (amendment
 *   9), or when its password secret is missing, so every route but login answers 503 (amendment
 *   11).
 * - `?mock-share=<MockShareFailure>`: a share that stops or is refused at a given point.
 */
export type MockLookupFailure = 'not-found' | 'failed';
export type MockStatusMode =
  'openemr-not-checked' | 'counts-unknown' | 'health-sync-timeout' | 'not-configured';
export type MockShareFailure =
  /** 422 before OpenEMR is called: the stored Patient has no birth date. */
  | 'patient-incomplete'
  /** 503: the share is not configured on the server. */
  | 'not-configured'
  /** 502 at the first step: the token request gets no answer. */
  | 'openemr-unreachable'
  /** 502 part-way: OpenEMR refuses to create the Encounter. */
  | 'encounter-refused'
  /** 504 part-way: the Observation POST gets no answer in time. */
  | 'observation-timeout'
  /** 200, but reading the Observation back fails. */
  | 'read-back';

export interface MockSwitches {
  lookupFailure: MockLookupFailure | null;
  partialLog: boolean;
  statusMode: MockStatusMode | null;
  shareFailure: MockShareFailure | null;
}

const LOOKUP_FAILURES: readonly MockLookupFailure[] = ['not-found', 'failed'];
const STATUS_MODES: readonly MockStatusMode[] = [
  'openemr-not-checked',
  'counts-unknown',
  'health-sync-timeout',
  'not-configured',
];
const SHARE_FAILURES: readonly MockShareFailure[] = [
  'patient-incomplete',
  'not-configured',
  'openemr-unreachable',
  'encounter-refused',
  'observation-timeout',
  'read-back',
];

/** The switches named in a page URL's query string; each is off unless spelled exactly. */
export function mockSwitchesFromSearch(search: string): MockSwitches {
  const q = new URLSearchParams(search);
  const pick = <T extends string>(allowed: readonly T[], name: string): T | null =>
    allowed.find((v) => v === q.get(name)) ?? null;
  return {
    lookupFailure: pick(LOOKUP_FAILURES, 'mock-lookup'),
    partialLog: q.get('mock-log') === 'partial',
    statusMode: pick(STATUS_MODES, 'mock-status'),
    shareFailure: pick(SHARE_FAILURES, 'mock-share'),
  };
}

export type MockDataSetId = keyof typeof MOCK_DATA_SETS;
export type MockDataSet = (typeof MOCK_DATA_SETS)[MockDataSetId];

/** The data set named by a page URL's query string (`?mock-data=dst-new-york`); `utc` otherwise. */
export function mockDataSetFromSearch(search: string): MockDataSet {
  const id = new URLSearchParams(search).get('mock-data');
  return id !== null && Object.hasOwn(MOCK_DATA_SETS, id)
    ? MOCK_DATA_SETS[id as MockDataSetId]
    : MOCK_DATA_SETS.utc;
}

const zoneFormats = new Map<string, Intl.DateTimeFormat>();

/**
 * An instant as an R4 dateTime in `timeZone` with its UTC offset, seconds precision, as Python's
 * `datetime.astimezone(ZoneInfo(zone)).isoformat()` writes it (T06's `fhir_datetime`): for
 * example `2026-11-01T01:30:00-04:00`, and `+00:00` (not `Z`) for a zero offset. With no zone,
 * UTC in JavaScript's own form (`toISOString`, `...000Z`), the default data set's format.
 * Uses Intl's tz database, so the result does not depend on the zone this code runs in.
 */
export function formatInZone(ms: number, timeZone: string | null): string {
  if (timeZone === null) return new Date(ms).toISOString();
  let format = zoneFormats.get(timeZone);
  if (!format) {
    format = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      timeZoneName: 'longOffset',
    });
    zoneFormats.set(timeZone, format);
  }
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    format.formatToParts(ms).find((p) => p.type === type)?.value ?? '';
  // longOffset is "GMT-04:00", or plain "GMT" for a zero offset.
  const gmt = part('timeZoneName').replace('GMT', '');
  const offset = gmt === '' ? '+00:00' : gmt;
  return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}${offset}`;
}

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

/**
 * Heart-rate Observation for one roll-up window, shaped like T06's example. `syncMs` is the sync
 * run that wrote it (its status follows T10's cutoff at that run); `timeZone` is the zone its
 * `effectivePeriod` is written in (null: UTC).
 */
export function heartRateObservation(
  window: GoogleHealthRollUpWindow,
  syncMs: number,
  timeZone: string | null = null,
): Observation {
  const startMs = Date.parse(window.startTime);
  const endMs = Date.parse(window.endTime);
  return {
    resourceType: 'Observation',
    id: `${HR_ID_PREFIX}${startMs}`,
    meta: { profile: ['http://hl7.org/fhir/StructureDefinition/heartrate'] },
    identifier: [
      { use: 'official', system: 'urn:pulsebridge:observation', value: pseudoUuid(`hr${startMs}`) },
    ],
    status: endMs > syncMs - PRELIMINARY_MS ? 'preliminary' : 'final',
    category: [VITAL_SIGNS],
    code: loinc(LOINC.heartRate, 'Heart rate', 'Heart rate'),
    subject: { reference: MOCK_PATIENT_REF },
    // T06 writes the window in the user's zone, so the offset is part of the data.
    effectivePeriod: { start: formatInZone(startMs, timeZone), end: formatInZone(endMs, timeZone) },
    // T06 (T33): self-measured, so the performer is the Patient, as in `subject`.
    performer: [{ reference: MOCK_PATIENT_REF }],
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

/**
 * All windows stored by the sync run at `syncMs` (complete windows ending by then) whose
 * effectivePeriod overlaps the open interval (startMs, endMs), oldest first. This is the R4
 * search rule for `date=gt<start>&date=lt<end>` on a Period: `gt` matches when the period ends
 * after start, `lt` when it starts before end. A window that ends exactly at start is therefore
 * excluded, so an aligned 24 h range gives 288.
 */
export function heartRateObservations(
  startMs: number,
  endMs: number,
  syncMs: number,
  timeZone: string | null = null,
): Observation[] {
  const out: Observation[] = [];
  // First window whose end (t + WINDOW_MS) is after startMs.
  const first = Math.floor(startMs / WINDOW_MS) * WINDOW_MS;
  for (let t = first; t < endMs && t + WINDOW_MS <= syncMs; t += WINDOW_MS) {
    if (t + WINDOW_MS <= startMs) continue;
    const window = syntheticRollUp(t);
    if (window) out.push(heartRateObservation(window, syncMs, timeZone));
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
    performer: [{ reference: MOCK_PATIENT_REF }],
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
 * The malformed samples "send malformed" offers: a generated copy of T07's manifest
 * (libs/pulsebridge_hl7v2/samples/samples.json, via scripts/sync-mock-samples.mjs), with each
 * sample's exact payload and the outcome that file expects at each layer.
 */
export interface MockSampleBehaviour extends MalformedSample {
  /** The payload exactly as T07 defines it (its own segment terminator). */
  er7: string;
  /** How T07 frames it over MLLP (`mllp_framing`); cuff-sim reports it as `framing`. */
  mllpFraming: MllpFraming;
  /** The sample's own MSH-10, or null when it has none (for example no MSH segment). */
  controlId: string | null;
  /** MSA-1 the store replies with, or null for no acknowledgment. */
  commitAck: 'AA' | 'AE' | null;
  commitReason: string | null;
  application: 'accepted' | 'error' | null;
  mappingReason: string | null;
}

interface ManifestSample {
  name: string;
  class: string;
  description: string;
  transports: string[];
  mllpFraming: string;
  controlId: string | null;
  er7: string;
  expected: {
    commit_ack: string | null;
    commit_ack_reason: string | null;
    no_ack: string | null;
    application_outcome: string | null;
    mapping_reason: string | null;
  };
}

export const MOCK_SAMPLES: MockSampleBehaviour[] = (
  mockSamples as { samples: ManifestSample[] }
).samples.map((s) => ({
  name: s.name,
  label: s.description,
  class: s.class as MalformedClass,
  transports: s.transports as Transport[],
  er7: s.er7,
  mllpFraming: s.mllpFraming as MllpFraming,
  controlId: s.controlId,
  commitAck: s.expected.commit_ack as 'AA' | 'AE' | null,
  commitReason: s.expected.commit_ack_reason,
  application: s.expected.application_outcome as 'accepted' | 'error' | null,
  mappingReason: s.expected.mapping_reason,
}));

/** HL7v2 timestamp (DTM, YYYYMMDDHHMMSS+ZZZZ) in UTC. */
export function hl7Time(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}+0000`;
}

/** A new MSH-10 control ID in T07's format: 20 upper-case hex characters (the 2.5.1 maximum). */
export function mockControlId(seed: string): string {
  return pseudoUuid(seed).replace(/-/g, '').slice(0, 20).toUpperCase();
}

/**
 * A display copy of the ORU^R01 T07's builder produces for a reading (build_bp_oru_r01 with its
 * default endpoints): MSH-3 CUFFSIM, MSH-5 PHR, OBR-3 filler number = control ID, OBX-6 as a CE
 * `mm[Hg]^mm[Hg]^UCUM`, every segment ending in "\r". Mock mode only: the real message is built
 * and validated with hl7apy by T07/T13. A test compares it with T07's `valid_bp_reading` sample.
 */
export function mockOruR01(
  controlId: string,
  ms: number,
  systolic: number,
  diastolic: number,
): string {
  const ts = hl7Time(ms);
  const unit = 'mm[Hg]^mm[Hg]^UCUM';
  return (
    [
      `MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|${ts}||ORU^R01^ORU_R01|${controlId}|P|2.5.1`,
      'PID|1||SYN-000123^^^PULSEBRIDGE^MR||TESTPATIENT^SYNTHIA',
      `OBR|1||${controlId}^CUFFSIM|85354-9^Blood pressure panel with all children optional^LN|||${ts}`,
      `OBX|1|NM|8480-6^Systolic blood pressure^LN||${systolic}|${unit}|||||F|||${ts}`,
      `OBX|2|NM|8462-4^Diastolic blood pressure^LN||${diastolic}|${unit}|||||F|||${ts}`,
    ].join('\r') + '\r'
  );
}

/**
 * cuff-sim's DERIVED message name for an accepted MLLP send (`derived_message_name` in
 * services/cuff-sim/cuff_sim/message.py): `<store>/messages/<id>`, where the id is the URL-safe
 * base64, with `=` padding, of the SHA-256 of the exact message bytes (spike S5's observation,
 * unverified for MLLP). The ACK does not carry the name, so this is computed, not returned.
 */
export async function derivedMessageName(store: string, payload: string): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(payload)),
  );
  const b64 = btoa(String.fromCharCode(...digest))
    .replaceAll('+', '-')
    .replaceAll('/', '_');
  return `${store}/messages/${b64}`;
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

// ---------------------------------------------------------------------------------------------
// Send-attempt AuditEvents (D12), in T31's final model.
//
// Built from four of T31's five generated examples (copied into ./t31-examples/ from
// libs/pulsebridge_fhir/examples/auditevent-send-*.json) as templates; only values change.
// mock-audit.test.ts compares the result with those files element by element. An accepted MLLP
// send is the accepted template without `what` and with the `derived-message-name` detail, which
// is exactly auditevent-send-accepted-mllp.json (the test compares the mock with it too).

/** Placeholder HL7v2 store name for mock mode (the real one is configuration). */
export const MOCK_HL7V2_STORE =
  'projects/mock-project/locations/mock-region/datasets/mock-dataset/hl7V2Stores/mock-store';
/** The synthetic patient's identifier (urn:pulsebridge:patient), as in T07's samples. */
export const MOCK_PATIENT_IDENTIFIER = 'SYN-000123';

const TEMPLATES = {
  accepted: acceptedExample,
  rejected: rejectedExample,
  noAck: noAckExample,
  unreadable: unreadableExample,
} as const;

/** T31's result for a commit-level acknowledgment (its outcome mapping table). */
export function sendResult(ack: CommitAck): SendAttemptEntry['result'] {
  if (!ack.received) return 'no-acknowledgment';
  return ack.code === 'AA' || ack.code === 'CA' ? 'accepted' : 'rejected';
}

/** R4 audit-event-outcome for that result: 0 success, 4 minor failure, 8 serious failure. */
export function outcomeCode(result: SendAttemptEntry['result']): SendAttemptEntry['outcome'] {
  return result === 'accepted' ? '0' : result === 'rejected' ? '4' : '8';
}

// T31's fixed wording (libs/pulsebridge_fhir/pulsebridge_fhir/audit_event.py, _NO_ACK_TEXT).
const NO_ACK_WORDS: Record<string, string> = {
  timeout: 'the read timed out',
  'connection-closed': 'the connection was closed with no reply',
  'api-error': 'the API returned an error with no HL7 reply',
  'unreadable-reply': 'the reply could not be read as an HL7v2 ACK',
};

/** base64 of a string of byte-sized characters (an HL7v2 ACK is ASCII). */
function base64(text: string): string {
  return btoa(text);
}

/** The AuditEvent T31's builder would write for this attempt, shaped exactly like its examples. */
export function mockSendAuditEvent(entry: SendAttemptEntry): AuditEvent {
  const ack = entry.commitAck;
  const template = ack.received
    ? sendResult(ack) === 'accepted'
      ? TEMPLATES.accepted
      : TEMPLATES.rejected
    : ack.why === 'unreadable-reply'
      ? TEMPLATES.unreadable
      : TEMPLATES.noAck;
  const event = structuredClone(template) as unknown as AuditEvent;
  const result = sendResult(ack);

  event.id = entry.id;
  (event as AuditEvent & { period: { start: string; end: string } }).period = {
    start: entry.sentAt,
    end: entry.recordedAt,
  };
  event.recorded = entry.recordedAt;
  event.outcome = outcomeCode(result);
  // Assigned in place, so the element keeps the template's position; an accept has none.
  if (result === 'accepted') delete event.outcomeDesc;
  if (ack.received && result === 'rejected') {
    event.outcomeDesc = `Rejected by the receiver (MSA-1 ${ack.code})${ack.reason ? `: ${ack.reason}` : ''}`;
  } else if (!ack.received) {
    event.outcomeDesc = `No acknowledgment received: ${NO_ACK_WORDS[ack.why] ?? ack.why}`;
    if (ack.why === 'api-error' && ack.httpStatus !== null) {
      event.outcomeDesc += ` (HTTP ${String(ack.httpStatus)})`;
    }
  }

  const destination = event.agent[1]?.who?.identifier;
  if (destination) destination.value = MOCK_HL7V2_STORE;
  const patient = event.entity?.[0]?.what?.identifier;
  if (patient) patient.value = MOCK_PATIENT_IDENTIFIER;

  const message = event.entity?.[1];
  if (message) {
    // `what` only for a name the store returned; a derived one is a detail (T33, below).
    if (entry.messageName) {
      message.what = {
        identifier: {
          system: 'urn:pulsebridge:source-record:hl7v2-message',
          value: entry.messageName,
        },
      };
    } else {
      delete message.what;
    }
    // detail entries in T31's order; each only when it applies (its README's detail table).
    const detail: { type: string; valueString?: string; valueBase64Binary?: string }[] = [
      { type: 'format-version', valueString: '1' },
      { type: 'transport', valueString: entry.transport ?? 'mllp' },
    ];
    if (entry.controlId) detail.push({ type: 'MSH-10', valueString: entry.controlId });
    if (ack.received) {
      detail.push({ type: 'MSA-1', valueString: ack.code });
      if (ack.reason) detail.push({ type: 'MSA-3', valueString: ack.reason });
      if (ack.raw) detail.push({ type: 'ack-message', valueBase64Binary: base64(ack.raw) });
    } else {
      detail.push({ type: 'no-ack-reason', valueString: ack.why });
      if (ack.why === 'api-error' && ack.httpStatus !== null) {
        detail.push({ type: 'http-status', valueString: String(ack.httpStatus) });
      }
    }
    if (!ack.received && ack.replyPrefixBase64) {
      detail.push({ type: 'unreadable-reply-bytes', valueBase64Binary: ack.replyPrefixBase64 });
    }
    if (entry.sample) detail.push({ type: 'malformed-sample', valueString: entry.sample });
    if (entry.derivedMessageName) {
      detail.push({ type: 'derived-message-name', valueString: entry.derivedMessageName });
    }
    message.detail = detail;
  }
  return event;
}
