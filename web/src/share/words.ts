/**
 * The share area's words, as pure functions (words.test.ts): what a confirm says will be sent
 * and left out, each trace step's result, and what did and did not happen when a share fails.
 *
 * The share sequence (T15; docs/spikes/s1.md) talks to OpenEMR's FHIR API with a registered
 * OAuth2 client: a client-credentials token obtained with a signed assertion (`private_key_jwt`,
 * no password, no user), then plain FHIR requests. OpenEMR has no Bundle endpoint and ignores
 * conditional create, so the proxy searches for the Patient before creating it, and an Encounter
 * is needed because OpenEMR files a vital sign as part of an encounter.
 */
import type { ShareResult, ShareStep } from '../api/contract';
import type { ShareError } from '../api/errors';
import type { Observation } from '../api/fhir';

const STEP_TITLE: Readonly<Record<string, string>> = {
  token: 'Access token',
  'patient-search': 'Find the Patient',
  'patient-create': 'Create the Patient',
  'encounter-search': 'Find an Encounter for the reading’s day',
  'encounter-create': 'Create the Encounter',
  'observation-create': 'Send the reading',
  'patient-read': 'Read back the Patient',
  'encounter-read': 'Read back the Encounter',
  'observation-read': 'Read back the reading',
};

export function stepTitle(step: ShareStep): string {
  return STEP_TITLE[step.step] ?? step.label;
}

export type Tone = 'ok' | 'warn' | 'error' | 'neutral';

/** A step's result in words, and a tone (always paired with the words). */
export function stepResult(step: ShareStep): { text: string; tone: Tone } {
  const s = step.step;
  switch (step.result) {
    case 'found':
      if (s === 'patient-search')
        return { text: 'Patient already existed: not created again', tone: 'ok' };
      if (s === 'encounter-search') return { text: 'Encounter found: reused', tone: 'ok' };
      return { text: 'Found', tone: 'ok' };
    case 'not-found':
      if (s === 'patient-search')
        return { text: 'No Patient yet: it is created next', tone: 'neutral' };
      if (s === 'encounter-search')
        return { text: 'None for that day: one is created next', tone: 'neutral' };
      return { text: 'Not found', tone: 'neutral' };
    case 'already-shared':
      return {
        text: 'Already in OpenEMR: refused as a repeat, nothing new written',
        tone: 'ok',
      };
    case 'failed':
      return {
        text:
          step.response.status === 0
            ? 'Failed: no answer'
            : `Failed (HTTP ${String(step.response.status)})`,
        tone: 'error',
      };
    case 'unknown':
      return { text: 'Result not recognised by this page', tone: 'warn' };
    case 'ok':
      if (s === 'token') return { text: 'Token issued', tone: 'ok' };
      if (s.endsWith('-create')) return { text: 'Created', tone: 'ok' };
      if (s.endsWith('-read')) return { text: 'Read back', tone: 'ok' };
      return { text: 'Done', tone: 'ok' };
  }
}

/** What the trace's "[not shown]" means. */
export const NOT_SHOWN_NOTE =
  'Values shown as "[not shown]" were removed by the proxy before the trace left the server: the signed assertion and the access token. As the proxy’s trace helper is written today it also hides every element named "code" or "token", so FHIR codes in these bodies show as "[not shown]"; the read-back below shows them in full.';

/** The sentences of the confirm step. */
export const CONFIRM_SENT = [
  'The reading itself: its code, value or values, time, category and status (final), as one FHIR Observation.',
  'Linked to the clinic’s own copy of the Patient and to an Encounter for the reading’s day, which the proxy finds and reuses, or creates.',
  'The Patient, only if the clinic does not have it yet: its patient identifier (PT), official name, gender and birth date.',
] as const;

export const CONFIRM_LEFT_OUT =
  'Left out: the device (the watch or the cuff), the performer (it names the Patient in this record), the reading’s identifiers, this store’s own id and metadata, and the Patient’s Google Health user id. Nothing that points back into this record goes to the clinic.';

export const REPEAT_NOTE =
  'Sharing it again creates no duplicate: the proxy finds the Patient before creating one, reuses the Encounter for that day, and OpenEMR refuses a repeated reading, which the page reports as "already shared".';

/**
 * The Observation as the page expects the proxy to send it (T06, T15): no `id`, `meta`,
 * `identifier`, `device` or `performer`; `subject` and `encounter` become OpenEMR's. The ids are
 * OpenEMR's own and not known yet, so they are shown as placeholders. The trace shows what was
 * actually sent.
 */
export function expectedShared(obs: Observation): Observation {
  const copy: Observation = structuredClone(obs);
  delete copy.id;
  delete copy.meta;
  delete copy.identifier;
  delete copy.device;
  delete copy.performer;
  copy.subject = { reference: 'Patient/<the clinic’s id for the Patient>' };
  copy.encounter = { reference: 'Encounter/<the clinic’s id for the Encounter>' };
  return copy;
}

/** The headline of a share that ended with the reading in OpenEMR. */
export function resultHeadline(result: ShareResult): string {
  const patientFound = result.steps.some(
    (s) => s.step === 'patient-search' && s.result === 'found',
  );
  const readBackFailed = result.readBack.observation.stored === null;
  const lead =
    result.outcome === 'shared'
      ? 'Shared: OpenEMR stored the reading.'
      : 'Already shared: OpenEMR already held this reading, so nothing new was written.';
  const patient = patientFound
    ? ' The Patient already existed there and was not created again.'
    : ' The Patient was created there for this first share.';
  const back = readBackFailed
    ? ' Reading it back failed, so what OpenEMR stored cannot be shown.'
    : ' What it stored is shown under "As stored in OpenEMR".';
  return `${lead}${patient}${back}`;
}

/** What happened, step by step, before a share stopped: the sentences of a failure. */
export function failureSentences(error: ShareError): string[] {
  const steps = error.steps;
  if (error.status === 0) {
    return [
      'The connection to the server failed, so it is not known whether anything was sent to the clinic.',
      'Sharing again is safe: a repeat creates no second Patient or reading.',
    ];
  }
  if (steps.length === 0) {
    const why: Record<string, string> = {
      'not-found': 'the reading was not found in the record (it may have been removed).',
      'invalid-body':
        'the request was not in the form the server expects. This is a fault in the page.',
      'share-not-configured': 'sharing with the clinic is not configured on the server.',
    };
    const reason = why[error.reason] ?? `${error.message.trim().replace(/[.\s]+$/, '')}.`;
    return [`Not shared: ${reason}`, 'Nothing was sent to the clinic.'];
  }
  const ok = (name: string) => steps.some((s) => s.step === name && s.result === 'ok');
  const last = steps.at(-1);
  const out = [
    `The share stopped at step ${String(steps.length)} (${last ? stepTitle(last) : 'unknown'}): ${error.message.trim().replace(/[.\s]+$/, '')}.`,
  ];
  const done: string[] = [];
  if (ok('token')) done.push('an access token was issued');
  if (ok('patient-create')) done.push('a Patient was created in OpenEMR');
  if (steps.some((s) => s.step === 'patient-search' && s.result === 'found')) {
    done.push('the Patient was found in OpenEMR');
  }
  if (ok('encounter-create')) done.push('an Encounter was created in OpenEMR');
  if (steps.some((s) => s.step === 'encounter-search' && s.result === 'found')) {
    done.push('an existing Encounter was found');
  }
  if (done.length > 0) out.push(`Done before it stopped: ${done.join('; ')}.`);
  const observationStep = steps.find((s) => s.step === 'observation-create');
  if (observationStep === undefined) {
    out.push('Not done: the reading was not sent to OpenEMR.');
  } else if (observationStep.result === 'ok' || observationStep.result === 'already-shared') {
    out.push('The reading is in OpenEMR; only reading it back did not finish.');
  } else if (observationStep.response.status === 0) {
    out.push(
      'It is not known whether OpenEMR stored the reading: its answer did not arrive. Sharing again is safe: a repeat in the same Encounter is refused and reported as already shared.',
    );
  } else {
    out.push('Not done: OpenEMR refused the reading, so it was not stored.');
  }
  return out;
}
