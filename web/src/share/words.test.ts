import { describe, expect, it } from 'vitest';
import type { ShareResult, ShareStep } from '../api/contract';
import { ShareError } from '../api/errors';
import type { Observation } from '../api/fhir';
import { expectedShared, failureSentences, resultHeadline, stepResult, stepTitle } from './words';

function step(name: string, result: ShareStep['result'], status = 200): ShareStep {
  return {
    step: name,
    label: `label ${name}`,
    request: { method: 'GET', url: '/x', body: null },
    response: { status, body: null },
    result,
    durationMs: 1,
  };
}

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
      null,
      [],
    );
    expect(failureSentences(e)).toEqual([
      'Not shared: Only final Observations are shared; this one is amended.',
      'Nothing was sent to the clinic.',
    ]);
    const nf = new ShareError('not-found', 404, 'x', null, []);
    expect(failureSentences(nf)[0]).toContain('the reading was not found');
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
    expect(failureSentences(lost)[0]).toContain('it is not known whether anything was sent');
  });
});
