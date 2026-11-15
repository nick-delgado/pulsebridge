/**
 * The cuff form as data: what the user entered, whether it may be sent, and the exact request
 * body. Pure functions, no DOM, so the rules are tested with literal values (form.test.ts).
 *
 * The bounds are the cuff simulator's own (`CUFF_SERVICE_LIMITS`, services/cuff-sim/README.md):
 * whole numbers from 1 to 400 mm[Hg], systolic strictly above diastolic. The page checks the same
 * rules first so the user gets an answer at once, but the service checks again and its answer
 * (a 422 naming the field) is shown the same way.
 *
 * The mapper's narrower "plausible" range (`MAPPER_PLAUSIBLE_RANGE`, T12) is deliberately NOT a
 * limit here: a reading between the two is sent, stored with AA, and then gets the
 * application-level outcome `error` / `out-of-range`. That is one of the D7 layers the demo shows.
 */
import {
  CUFF_SERVICE_LIMITS,
  MAPPER_PLAUSIBLE_RANGE,
  type CuffReadingRequest,
  type MalformedClass,
  type MalformedSample,
  type Transport,
} from '../api/contract';

/** The form exactly as the user left it: raw text from the two inputs. */
export interface CuffFormState {
  systolic: string;
  diastolic: string;
  transport: Transport;
  sendMalformed: boolean;
  /** The chosen sample name, or '' when none is chosen. */
  sample: string;
}

/** Fields an error can be attached to: the request's own field names (cuff-sim's `expression`). */
export type CuffField = 'systolic' | 'diastolic' | 'transport' | 'sendMalformed' | 'body';

export type FieldErrors = Partial<Record<CuffField, string>>;

export type FormCheck =
  { ok: true; request: CuffReadingRequest } | { ok: false; errors: FieldErrors };

const { min, max } = CUFF_SERVICE_LIMITS;

/**
 * One pressure input. Only plain digits are accepted, so `120.5`, `1e2`, `+120`, `120 mmHg` and
 * an empty field never become a number by accident; the service wants a JSON integer.
 */
export function parsePressure(raw: string): { value: number } | { error: string } {
  const text = raw.trim();
  if (text === '') return { error: `Enter a whole number from ${min} to ${max}.` };
  if (!/^\d+$/.test(text))
    return { error: 'Enter a whole number of mm[Hg], with no decimals or units.' };
  const value = Number(text);
  if (value < min || value > max) {
    return { error: `Must be from ${min} to ${max} mm[Hg] (the simulator's limit).` };
  }
  return { value };
}

/** True when the sample may be sent over this transport (bad-framing samples are MLLP only). */
export function sampleAvailable(sample: MalformedSample, transport: Transport): boolean {
  return sample.transports.includes(transport);
}

/**
 * Checks the form and builds the request body.
 * - A built reading sends both pressures and `sendMalformed: null`.
 * - "Send malformed" sends only the transport and the sample name: a sample is fixed bytes, so
 *   the pressures would be ignored, and leaving them out means a half-typed value cannot cause a
 *   refusal (the service still checks them when present).
 */
export function checkForm(state: CuffFormState, samples: readonly MalformedSample[]): FormCheck {
  const errors: FieldErrors = {};
  if (state.sendMalformed) {
    const sample = samples.find((s) => s.name === state.sample);
    if (state.sample === '' || !sample) errors.sendMalformed = 'Choose a sample to send.';
    else if (!sampleAvailable(sample, state.transport)) {
      errors.sendMalformed = `This sample can only be sent over ${sample.transports.map(transportName).join(' or ')}.`;
    }
    if (Object.keys(errors).length > 0) return { ok: false, errors };
    return { ok: true, request: { transport: state.transport, sendMalformed: state.sample } };
  }

  const systolic = parsePressure(state.systolic);
  const diastolic = parsePressure(state.diastolic);
  if ('error' in systolic) errors.systolic = systolic.error;
  if ('error' in diastolic) errors.diastolic = diastolic.error;
  if ('value' in systolic && 'value' in diastolic && systolic.value <= diastolic.value) {
    errors.systolic = 'Must be greater than the diastolic value.';
  }
  if ('error' in systolic || 'error' in diastolic || errors.systolic !== undefined) {
    return { ok: false, errors };
  }
  return {
    ok: true,
    request: {
      systolic: systolic.value,
      diastolic: diastolic.value,
      transport: state.transport,
      sendMalformed: null,
    },
  };
}

/**
 * A note (not an error) for a valid value outside the mapper's plausible range: it will be sent
 * and stored, then the mapper will refuse it. Null when the value is plausible or not valid.
 */
export function plausibilityNote(field: 'systolic' | 'diastolic', raw: string): string | null {
  const parsed = parsePressure(raw);
  if (!('value' in parsed)) return null;
  const range = MAPPER_PLAUSIBLE_RANGE[field];
  if (parsed.value >= range.min && parsed.value <= range.max) return null;
  return `Outside the mapper's plausible ${field} range (${range.min} to ${range.max}): it will be sent and stored, then rejected by the mapper with "out-of-range".`;
}

export function transportName(t: Transport): string {
  return t === 'mllp' ? 'MLLP' : 'HTTPS';
}

/** The order the picker lists the classes in, and what each one demonstrates (D7). */
export const CLASS_ORDER: readonly MalformedClass[] = [
  'structurally_invalid',
  'unparseable',
  'no_acknowledgment',
  'parses_but_cannot_map',
];

export const CLASS_INFO: Readonly<Record<MalformedClass, { title: string; layer: string }>> = {
  structurally_invalid: {
    title: 'Structurally invalid',
    layer:
      'A required field is missing or has the wrong type. Normally hl7apy validation in the simulator refuses it before sending; "send malformed" skips that check, so it reaches the store (AA) and the mapper records an error.',
  },
  unparseable: {
    title: 'Unparseable',
    layer:
      'The HL7v2 store cannot read it (no MSH, wrong terminators or separators), so it answers AE: a commit-level rejection. Nothing is stored.',
  },
  no_acknowledgment: {
    title: 'No acknowledgment (MLLP only)',
    layer:
      'The MLLP frame is broken, so the adapter keeps waiting for the end of the message and never answers: "no acknowledgment", after the simulator times out.',
  },
  parses_but_cannot_map: {
    title: 'Parses but cannot be mapped',
    layer:
      'The store accepts it (AA), then the mapper (v2-to-fhir) cannot turn it into a FHIR Observation and records an application-level error with a reason.',
  },
};

/**
 * The samples grouped by class, in `CLASS_ORDER`; a class with no sample is left out. A sample
 * whose class this page does not know (a newer service) is listed under `other`, never dropped.
 */
export function samplesByClass(
  samples: readonly MalformedSample[],
): { cls: MalformedClass | 'other'; samples: MalformedSample[] }[] {
  const known = new Set<string>(CLASS_ORDER);
  const groups: { cls: MalformedClass | 'other'; samples: MalformedSample[] }[] = CLASS_ORDER.map(
    (cls) => ({ cls, samples: samples.filter((s) => s.class === cls) }),
  );
  groups.push({ cls: 'other', samples: samples.filter((s) => !known.has(s.class)) });
  return groups.filter((g) => g.samples.length > 0);
}

export function classTitle(cls: MalformedClass | 'other'): string {
  return cls === 'other' ? 'Other' : CLASS_INFO[cls].title;
}
