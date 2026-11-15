import { describe, expect, it } from 'vitest';
import type { ShareResult, ShareStep } from '../api/contract';
import { ShareError } from '../api/errors';
import type { Observation, OperationOutcome } from '../api/fhir';
import { UNREADABLE_SHARE } from '../api/parse';
import {
  expectedShared,
  failureSentences,
  NOT_SHOWN_NOTE,
  REPEAT_NOTE,
  resultHeadline,
  SAFE_AGAIN,
  stepResult,
  stepTitle,
} from './words';

function step(name: string, result: ShareStep['result'], status = 200): ShareStep {
  return {
    step: name,
    label: `label ${name}`,
    request: { method: 'GET', url: '/x', query: null, body: null },
    response: { status, body: null },
    result,
    durationMs: 1,
  };
}

const OO: OperationOutcome = {
  resourceType: 'OperationOutcome',
  issue: [{ severity: 'error', code: 'invalid', diagnostics: 'x' }],
};

describe('repeat and credential notes (F5, F6)', () => {
  it('the repeat note does not promise a refusal for a heart rate', () => {
    expect(REPEAT_NOTE).toContain('does not create a second Patient');
    expect(REPEAT_NOTE).toContain('for a heart-rate reading that refusal has not been verified');
  });

  it('the credential note is neutral', () => {
    expect(NOT_SHOWN_NOTE).toBe('Credentials are replaced by "[not shown]".');
  });
});

describe('step words', () => {
  it.each([
    ['token', 'ok', 'Token issued'],
    ['patient-search', 'found', 'Patient already existed: not created again'],
    ['patient-search', 'not-found', 'No Patient yet: it is created next'],
    ['encounter-search', 'found', 'Encounter found: reused'],
    ['encounter-search', 'not-found', 'None for that day: one is created next'],
    ['patient-create', 'ok', 'Created'],
    [
      'observation-create',
      'already-shared',
      'Already in OpenEMR: refused as a repeat, nothing new written',
    ],
    ['observation-read', 'ok', 'Read back'],
    ['observation-create', 'unknown', 'Result not recognised by this page'],
  ] as const)('%s %s: %s', (name, result, text) => {
    expect(stepResult(step(name, result)).text).toBe(text);
  });

  it('a failed step names its status, or "no answer"', () => {
    expect(stepResult(step('encounter-create', 'failed', 400))).toEqual({
      text: 'Failed (HTTP 400)',
      tone: 'error',
    });
    expect(stepResult(step('observation-create', 'failed', 0)).text).toBe('Failed: no answer');
  });

  it('an unknown step name falls back to the proxy label', () => {
    expect(stepTitle(step('something-new', 'ok'))).toBe('label something-new');
    expect(stepTitle(step('encounter-create', 'ok'))).toBe('Create the Encounter');
  });
});

describe('what the confirm expects to be sent (T06)', () => {
  it('drops id, meta, identifier, device and performer; subject and encounter are the clinic’s', () => {
    const obs: Observation = {
      resourceType: 'Observation',
      id: 'bp-1',
      meta: { profile: ['p'] },
      identifier: [{ value: 'k' }],
      status: 'final',
      code: {},
      subject: { reference: 'Patient/ours' },
      performer: [{ reference: 'Patient/ours' }],
      device: { reference: 'Device/cuff' },
      effectiveDateTime: 't',
    };
    const out = expectedShared(obs);
    for (const k of ['id', 'meta', 'identifier', 'device', 'performer'])
      expect(out).not.toHaveProperty(k);
    expect(out.subject?.reference).not.toContain('ours');
    expect(out.encounter?.reference).toMatch(/^Encounter\//);
    expect(out.effectiveDateTime).toBe('t');
    expect(obs.id).toBe('bp-1'); // the original is not changed
  });
});

describe('result and failure words', () => {
  const result = (over: Partial<ShareResult>): ShareResult => ({
    outcome: 'shared',
    steps: [step('patient-search', 'not-found')],
    readBack: {
      patient: { sent: null, stored: null },
      encounter: { sent: null, stored: null },
      observation: {
        sent: null,
        stored: { resourceType: 'Observation', status: 'final', code: {} },
      },
    },
    ...over,
  });

  it('shared, first time; already shared with the Patient found; read-back failed', () => {
    expect(resultHeadline(result({}))).toBe(
      'Shared: OpenEMR stored the reading. The Patient was created there for this first share. What it stored is shown under "As stored in OpenEMR".',
    );
    const again = result({ outcome: 'already-shared', steps: [step('patient-search', 'found')] });
    expect(resultHeadline(again)).toContain(
      'Already shared: OpenEMR already held this reading, so nothing new was written.',
    );
    expect(resultHeadline(again)).toContain(
      'The Patient already existed there and was not created again.',
    );
    const noBack = result({});
    noBack.readBack.observation.stored = null;
    expect(resultHeadline(noBack)).toContain('Reading it back failed');
  });

  it('refused before OpenEMR: nothing was sent', () => {
    const e = new ShareError(
      'observation-not-final',
      422,
      'Only final Observations are shared; this one is amended.',
      OO,
      [],
    );
    expect(failureSentences(e)).toEqual([
      'Not shared: Only final Observations are shared; this one is amended.',
      'Nothing was sent to the clinic.',
    ]);
    const nf = new ShareError('not-found', 404, 'x', OO, []);
    expect(failureSentences(nf)[0]).toContain('the reading was not found');
  });

  // F1: only the proxy's own refusals (400, 404, 422, 503 with its OperationOutcome) say that
  // nothing was sent. Everything else may come after the clinic was changed.
  it.each([
    [400, OO, true],
    [404, OO, true],
    [422, OO, true],
    [503, OO, true],
    [503, null, false], // an HTML 503 from the hosting layer
    [500, OO, false],
    [504, OO, false], // the proxy's own budget running out, no steps envelope
    [504, null, false], // Firebase Hosting's HTML 504
    [502, null, false],
    [0, null, false], // no connection
  ] as const)('HTTP %s (OperationOutcome: %s): nothing sent is %s', (status, outcome, nothing) => {
    const e = new ShareError('other', status, 'The server answered.', outcome, []);
    expect(e.nothingSent).toBe(nothing);
    const lines = failureSentences(e).join(' ');
    if (nothing) {
      expect(lines).toContain('Nothing was sent to the clinic.');
    } else {
      expect(lines).not.toContain('Nothing was sent');
      expect(lines).toContain('it is not known what reached the clinic');
      expect(lines).toContain(SAFE_AGAIN);
    }
  });

  it('an unreadable 200 says the answer could not be read, never that nothing was sent', () => {
    const e = new ShareError('other', 502, UNREADABLE_SHARE, null, []);
    expect(failureSentences(e)).toEqual([
      'The share answered, but its answer could not be read, so it is not known what reached the clinic: the Patient, the Encounter or the reading may already have been created in OpenEMR.',
      SAFE_AGAIN,
    ]);
  });

  it('an HTML 504 names the status; a bare OperationOutcome 504 quotes the proxy', () => {
    const html = new ShareError('other', 504, 'The server answered HTTP 504.', null, []);
    expect(failureSentences(html)[0]).toContain(
      'The server answered HTTP 504 without saying what happened',
    );
    const budget = new ShareError('other', 504, 'The request ran out of time.', OO, []);
    expect(failureSentences(budget)[0]).toContain(
      'The request ran out of time, so it is not known',
    );
  });

  it('stopped part-way before the reading: what was done, and that the reading was not sent', () => {
    const e = new ShareError(
      'openemr-refused',
      502,
      'OpenEMR refused to create the Encounter.',
      null,
      [
        step('token', 'ok'),
        step('patient-search', 'not-found'),
        step('patient-create', 'ok', 201),
        step('encounter-search', 'not-found'),
        step('encounter-create', 'failed', 400),
      ],
    );
    expect(failureSentences(e)).toEqual([
      'The share stopped at step 5 (Create the Encounter): OpenEMR refused to create the Encounter.',
      'Done before it stopped: an access token was issued; a Patient was created in OpenEMR.',
      'Not done: the reading was not sent to OpenEMR.',
    ]);
  });

  it('timed out on the reading itself: not known whether it was stored, and a retry is safe', () => {
    const e = new ShareError('openemr-timeout', 504, 'No answer.', null, [
      step('token', 'ok'),
      step('patient-search', 'found'),
      step('encounter-search', 'found'),
      step('observation-create', 'failed', 0),
    ]);
    const lines = failureSentences(e);
    expect(lines[1]).toBe(
      'Done before it stopped: an access token was issued; the Patient was found in OpenEMR; an existing Encounter was found.',
    );
    expect(lines[2]).toContain('It is not known whether OpenEMR stored the reading');
    expect(lines[2]).toContain('Sharing again is safe');
  });

  it('refused reading, and a lost connection', () => {
    const refused = new ShareError('openemr-refused', 502, 'x', null, [
      step('observation-create', 'failed', 400),
    ]);
    expect(failureSentences(refused).at(-1)).toBe(
      'Not done: OpenEMR refused the reading, so it was not stored.',
    );
    const lost = new ShareError('other', 0, 'x', null, []);
    expect(failureSentences(lost)[0]).toContain(
      'The connection to the server failed, so it is not known what reached the clinic',
    );
  });
});
