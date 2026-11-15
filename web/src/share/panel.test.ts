import { afterEach, describe, expect, it } from 'vitest';
import type { CallOptions, PulseBridgeApi, ShareRequest, ShareResult } from '../api/contract';
import { ApiError, ShareError, UnauthorizedError } from '../api/errors';
import type { Observation, OperationOutcome } from '../api/fhir';
import { UNREADABLE_SHARE } from '../api/parse';
import { deferred, type Deferred } from '../cuff/test-support';
import { createDataChangeBus, type DataChangeKind } from '../state/data-changed';
import { createSelectionStore, type Selection } from '../state/selection';
import { createShare } from './panel';

const OO: OperationOutcome = {
  resourceType: 'OperationOutcome',
  issue: [{ severity: 'error', code: 'invalid', diagnostics: 'x' }],
};

const HOSTILE = `<img src=x onerror="alert(1)">${'Z'.repeat(600)}`;

function fakeClient() {
  const reads: { id: string; signal: AbortSignal; answer: Deferred<Observation> }[] = [];
  const shares: { request: ShareRequest; signal: AbortSignal; answer: Deferred<ShareResult> }[] =
    [];
  const client = {
    mode: 'mock',
    getObservation(id: string, options: CallOptions = {}) {
      if (!options.signal) throw new Error('read without a signal');
      const answer = deferred<Observation>();
      reads.push({ id, signal: options.signal, answer });
      return answer.promise;
    },
    share(request: ShareRequest, options: CallOptions = {}) {
      if (!options.signal) throw new Error('share without a signal');
      const answer = deferred<ShareResult>();
      shares.push({ request: structuredClone(request), signal: options.signal, answer });
      return answer.promise;
    },
  } as unknown as PulseBridgeApi;
  return { client, reads, shares };
}

async function flush() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

let teardown: AbortController | null = null;
afterEach(() => {
  teardown?.abort();
  document.body.replaceChildren();
});

function mount() {
  const api = fakeClient();
  const selection = createSelectionStore();
  const bus = createDataChangeBus();
  const notified: DataChangeKind[] = [];
  const controller = new AbortController();
  teardown = controller;
  const slots = {
    action: document.createElement('div'),
    trace: document.createElement('div'),
    readback: document.createElement('div'),
  };
  document.body.append(slots.action, slots.trace, slots.readback);
  createShare(
    {
      client: api.client,
      selection,
      slots,
      signal: controller.signal,
      refreshStatus: () => undefined,
      notifyDataChanged: (kind) => {
        notified.push(kind);
        bus.notify(kind);
      },
      onDataChanged: (listener, signal) => {
        bus.subscribe(listener, signal);
      },
    },
    { now: () => Date.parse('2026-03-10T14:30:00Z'), timeZone: 'UTC' },
  );
  const byId = (id: string): HTMLElement | null => document.getElementById(id);
  const shareButton = () => byId('share-button') as HTMLButtonElement | null;
  const send = () => byId('share-send') as HTMLButtonElement | null;
  return { ...api, selection, notified, controller, slots, byId, shareButton, send };
}

const SELECTED: Selection = {
  source: 'cuff',
  attemptId: 'att-1',
  observationId: 'bp-1',
  summary: 'BP 120/80 mm[Hg] at 14:17:00, MLLP',
  unshareableReason: null,
};

const OBS: Observation = {
  resourceType: 'Observation',
  id: 'bp-1',
  meta: { profile: ['p'] },
  identifier: [{ system: 'urn:pulsebridge:observation', value: 'k' }],
  status: 'final',
  code: { coding: [{ system: 'http://loinc.org', code: '85354-9' }] },
  subject: { reference: 'Patient/ours' },
  performer: [{ reference: 'Patient/ours' }],
  device: { reference: 'Device/cuff' },
  effectiveDateTime: '2026-03-10T14:17:00Z',
};

function result(over: Partial<ShareResult> = {}): ShareResult {
  const sent: Observation = {
    resourceType: 'Observation',
    status: 'final',
    code: OBS.code,
    subject: { reference: 'Patient/emr-p' },
    encounter: { reference: 'Encounter/emr-e' },
    performer: [{ reference: 'Patient/emr-p' }],
    effectiveDateTime: '2026-03-10T14:17:00Z',
  };
  const step = (name: string, res: ShareResult['steps'][number]['result'], status = 200) => ({
    step: name,
    // The proxy's label is text from the server too (F9).
    label: `label ${name} ${HOSTILE}`,
    request: {
      method: 'POST',
      url: `/apis/default/fhir/${name}`,
      query: name.endsWith('-search') ? { identifier: 'SYN-1', date: ['2026-03-10'] } : null,
      body: { client_assertion: '[not shown]' },
    },
    response: { status, body: { note: HOSTILE } },
    result: res,
    durationMs: 12,
  });
  return {
    outcome: 'shared',
    steps: [
      step('token', 'ok'),
      step('patient-search', 'found'),
      step('encounter-search', 'found'),
      step('observation-create', 'ok', 201),
      step('observation-read', 'ok'),
    ],
    readBack: {
      patient: { sent: null, stored: { resourceType: 'Patient', id: 'emr-p' } },
      encounter: {
        sent: null,
        stored: { resourceType: 'Encounter', id: 'emr-e', status: 'finished' },
      },
      observation: {
        sent,
        stored: {
          ...structuredClone(sent),
          id: 'emr-o',
          effectiveDateTime: '2026-03-10T14:17:00+00:00',
        },
      },
    },
    ...over,
  };
}

/** Selects, clicks Share, answers the read with `obs`. */
async function toConfirm(m: ReturnType<typeof mount>, obs: Observation = OBS) {
  m.selection.set(SELECTED);
  m.shareButton()?.click();
  expect(m.reads).toHaveLength(1);
  expect(m.reads[0]?.id).toBe('bp-1');
  m.reads[0]?.answer.resolve(obs);
  await flush();
}

describe('checking and confirming', () => {
  it('reads the Observation first; the confirm says what is sent and what is left out', async () => {
    const m = mount();
    m.selection.set(SELECTED);
    m.shareButton()?.click();
    expect(m.shareButton()?.disabled).toBe(true);
    expect(m.slots.action.getAttribute('aria-busy')).toBe('true');
    expect(m.byId('share-status')?.textContent).toContain('Checking BP 120/80');
    m.reads[0]?.answer.resolve(OBS);
    await flush();
    const confirm = m.byId('share-confirm');
    expect(confirm?.hidden).toBe(false);
    const text = confirm?.textContent ?? '';
    expect(text).toContain('Send this reading to the clinic?');
    expect(text).toContain('its patient identifier (PT), official name, gender and birth date');
    expect(text).toContain('Left out: the device (the watch or the cuff), the performer');
    expect(text).toContain('Google Health user id');
    // The preview leaves out device, performer, identifier, meta and our id.
    const preview = confirm?.querySelector('code')?.textContent ?? '';
    for (const gone of [
      '"device"',
      '"performer"',
      '"identifier"',
      '"meta"',
      '"bp-1"',
      'Patient/ours',
    ]) {
      expect(preview).not.toContain(gone);
    }
    expect(document.activeElement?.id).toBe('share-confirm-heading');
    expect(m.shares).toHaveLength(0);
  });

  it('a reading that is not final is refused before anything is sent', async () => {
    const m = mount();
    await toConfirm(m, { ...OBS, status: 'amended' });
    expect(m.byId('share-confirm')?.hidden).toBe(true);
    expect(m.byId('share-alert')?.textContent).toContain("this reading's status is amended");
    expect(m.byId('share-alert')?.textContent).toContain('Nothing was sent to the clinic.');
    expect(m.shares).toHaveLength(0);
    expect(m.shareButton()?.disabled).toBe(false);
  });

  it('a reading not found: says so, sends nothing', async () => {
    const m = mount();
    m.selection.set(SELECTED);
    m.shareButton()?.click();
    m.reads[0]?.answer.reject(new ApiError(404, 'The Observation was not found.'));
    await flush();
    expect(m.byId('share-alert')?.textContent).toContain('the reading was not found in the record');
  });

  it('Cancel closes the confirm, sends nothing, and returns focus to the button', async () => {
    const m = mount();
    await toConfirm(m);
    (m.byId('share-cancel') as HTMLButtonElement | null)?.click();
    expect(m.byId('share-confirm')?.hidden).toBe(true);
    expect(m.byId('share-status')?.textContent).toBe('Cancelled: nothing was sent.');
    expect(document.activeElement).toBe(m.shareButton());
    expect(m.shares).toHaveLength(0);
  });

  it('the selection changing while confirming closes it; while checking, the read is aborted', async () => {
    const m = mount();
    await toConfirm(m);
    m.selection.set({ ...SELECTED, attemptId: 'att-2', observationId: 'bp-2' });
    expect(m.byId('share-confirm')?.hidden).toBe(true);
    expect(m.byId('share-status')?.textContent).toBe('The selection changed, so nothing was sent.');
    m.shareButton()?.click();
    m.selection.set(null);
    expect(m.reads[1]?.signal.aborted).toBe(true);
    m.reads[1]?.answer.resolve(OBS);
    await flush();
    expect(m.byId('share-confirm')?.hidden).toBe(true);
  });
});

describe('sending', () => {
  it('sends only the id; double submit sends once; busy while sending', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.send()?.click();
    expect(m.shares).toHaveLength(1);
    expect(m.shares[0]?.request).toEqual({ observationId: 'bp-1' });
    expect(m.send()?.disabled).toBe(true);
    expect(m.shareButton()?.disabled).toBe(true);
    expect(m.shareButton()?.textContent).toBe('Sharing…');
    m.shareButton()?.click();
    expect(m.reads).toHaveLength(1);
  });

  it('success: the steps in order with their status, the read-back with differences, notify, re-check', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.resolve(result());
    await flush();
    const steps = [...m.slots.trace.querySelectorAll('.share-step')];
    expect(steps.map((s) => s.querySelector('.share-step__result')?.textContent)).toEqual([
      'Token issued',
      'Patient already existed: not created again',
      'Encounter found: reused',
      'Created',
      'Read back',
    ]);
    expect(steps[3]?.textContent).toContain('POST /apis/default/fhir/observation-create → 201');
    expect(m.byId('share-status')?.textContent).toContain('Shared: OpenEMR stored the reading.');
    const rb = m.slots.readback.textContent;
    expect(rb).toContain(
      'effectiveDateTime changed: sent "2026-03-10T14:17:00Z", stored "2026-03-10T14:17:00+00:00".',
    );
    expect(rb).toContain('id was added by OpenEMR');
    expect(rb).toContain('Not sent: the Patient already existed in OpenEMR');
    expect(rb).toContain('Not sent: an Encounter for the reading’s day already existed');
    expect(m.notified).toEqual(['share']);
    expect(m.byId('share-confirm')?.hidden).toBe(true);
    expect(m.byId('share-hint')?.textContent).toBe(
      'Selected: BP 120/80 mm[Hg] at 14:17:00, MLLP. Shared at 14:30:00 in this session.',
    );
    expect(document.activeElement?.id).toBe('share-trace-summary');
    // A search step shows its parameters (F5: request.query), never in the url.
    expect(steps[1]?.querySelector('.share-step__query')?.textContent).toBe(
      'Search parameters: identifier=SYN-1 & date=2026-03-10',
    );
    expect(steps[0]?.querySelector('.share-step__query')).toBeNull();
    // OpenEMR's text and the proxy's labels are text.
    expect(m.slots.trace.querySelector('img')).toBeNull();
    expect(steps[0]?.querySelector('.share-hint')?.textContent).toContain('<img src=x onerror=');
    expect(m.slots.trace.textContent).toContain('<img src=x onerror=');
  });

  it('a repeat: the confirm says it was shared and that no second Patient is created; the result says already shared', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.resolve(result());
    await flush();
    m.shareButton()?.click();
    m.reads[1]?.answer.resolve(OBS);
    await flush();
    expect(m.byId('share-confirm')?.textContent).toContain(
      'You already shared this reading at 14:30:00.',
    );
    expect(m.byId('share-confirm')?.textContent).toContain('does not create a second Patient');
    expect(m.byId('share-confirm')?.textContent).toContain('has not been verified');
    m.send()?.click();
    m.shares[1]?.answer.resolve(result({ outcome: 'already-shared' }));
    await flush();
    expect(m.byId('share-status')?.textContent).toContain(
      'Already shared: OpenEMR already held this reading',
    );
    expect(m.slots.readback.textContent).toContain('OpenEMR refused this copy as a repeat');
  });

  it('the selection changing while sending does not cancel the share; its result says what it was for', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.selection.set({
      source: 'watch',
      observationId: 'hr-1',
      summary: 'HR 61',
      unshareableReason: null,
    });
    expect(m.shares[0]?.signal.aborted).toBe(false);
    m.shares[0]?.answer.resolve(result());
    await flush();
    expect(m.byId('share-status')?.textContent).toContain(
      'This result is for BP 120/80 mm[Hg] at 14:17:00, MLLP; the selection changed while it was sent.',
    );
    expect(m.byId('share-hint')?.textContent).toBe('Selected: HR 61');
  });

  it('logout while sending: aborted, and the late answer changes nothing', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.controller.abort();
    expect(m.shares[0]?.signal.aborted).toBe(true);
    m.shares[0]?.answer.resolve(result());
    await flush();
    expect(m.slots.trace.querySelector('.share-step')).toBeNull();
    expect(m.notified).toEqual([]);
  });

  it('a 401 is left to the shell', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.reject(new UnauthorizedError());
    await flush();
    expect(m.byId('share-alert')?.textContent).toBe('');
  });
});

describe('failures say what did and did not happen', () => {
  it('refused before OpenEMR (422): nothing sent, no steps, no notification', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.reject(
      new ShareError(
        'patient-incomplete',
        422,
        'The stored Patient is incomplete: missing birthDate.',
        OO,
        [],
      ),
    );
    await flush();
    expect(m.byId('share-alert')?.textContent).toContain(
      'Not shared: The stored Patient is incomplete: missing birthDate.',
    );
    expect(m.byId('share-alert')?.textContent).toContain('Nothing was sent to the clinic.');
    expect(m.slots.trace.textContent).toContain('No request reached OpenEMR.');
    expect(m.notified).toEqual([]);
    expect(m.shareButton()?.disabled).toBe(false);
  });

  it('a 504 part-way: the steps so far, what was done, and that it is not known whether it was stored', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    const steps = result().steps.slice(0, 3);
    const create = result().steps[3];
    if (!create) throw new Error('no step 4');
    steps.push({ ...create, result: 'failed', response: { status: 0, body: null } });
    m.shares[0]?.answer.reject(
      new ShareError('openemr-timeout', 504, 'OpenEMR did not answer in time.', null, steps),
    );
    await flush();
    expect(m.slots.trace.querySelectorAll('.share-step')).toHaveLength(4);
    expect(
      m.slots.trace.querySelector('.share-step:last-of-type .share-step__result')?.textContent,
    ).toBe('Failed: no answer');
    const alert = m.byId('share-alert')?.textContent ?? '';
    expect(alert).toContain(
      'The share stopped at step 4 (Send the reading): OpenEMR did not answer in time.',
    );
    expect(alert).toContain('It is not known whether OpenEMR stored the reading');
    expect(m.slots.readback.textContent).toContain('Nothing to read back');
    // Something happened at the clinic, so the others are told.
    expect(m.notified).toEqual(['share']);
  });

  it('503 not configured: says so, keeps the session', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.reject(new ShareError('share-not-configured', 503, 'x', OO, []));
    await flush();
    expect(m.byId('share-alert')?.textContent).toContain(
      'sharing with the clinic is not configured on the server',
    );
    expect(m.notified).toEqual([]);
  });

  // F1: anything but the proxy's own refusal before OpenEMR may come after the clinic was
  // changed: never "nothing was sent", the others are told, and sharing again is offered as safe.
  it.each([
    [
      'an HTML 504 from the hosting layer',
      new ShareError('other', 504, 'The server answered HTTP 504.', null, []),
    ],
    [
      'a bare OperationOutcome 504 (the budget ran out)',
      new ShareError('other', 504, 'The request ran out of time.', OO, []),
    ],
    ['a 500', new ShareError('other', 500, 'Internal error.', OO, [])],
    ['a lost connection', new ShareError('other', 0, 'The server could not be reached.', null, [])],
    ['an unreadable 200', new ShareError('other', 502, UNREADABLE_SHARE, null, [])],
  ])('%s: not known what reached the clinic, notified, no "nothing sent"', async (_name, error) => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.reject(error);
    await flush();
    const alert = m.byId('share-alert')?.textContent ?? '';
    expect(alert).toContain('it is not known what reached the clinic');
    expect(alert).toContain('Sharing again is safe');
    expect(alert).not.toContain('Nothing was sent');
    expect(m.slots.trace.textContent).toContain(
      'The server sent no trace, so it is not known which requests reached OpenEMR.',
    );
    expect(m.slots.trace.textContent).not.toContain('No request reached OpenEMR.');
    expect(m.notified).toEqual(['share']);
  });
});

describe('ids and references', () => {
  it('ids carry the share- prefix, are unique, and every ARIA reference resolves', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    m.shares[0]?.answer.resolve(result());
    await flush();
    const all = [m.slots.action, m.slots.trace, m.slots.readback].flatMap((s) => [
      ...s.querySelectorAll('[id]'),
    ]);
    const ids = all.map((e) => e.id);
    expect(ids.every((id) => id.startsWith('share-'))).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
    for (const s of [m.slots.action, m.slots.trace, m.slots.readback]) {
      for (const el of s.querySelectorAll(
        '[aria-labelledby], [aria-describedby], [aria-controls]',
      )) {
        for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-controls']) {
          for (const ref of (el.getAttribute(attr) ?? '').split(/\s+/).filter(Boolean)) {
            expect(document.getElementById(ref), `${attr}=${ref}`).not.toBeNull();
          }
        }
      }
    }
  });
});

describe('a reused token in the trace (T15)', () => {
  it('shows the token step as reused, with no "no answer" request line', async () => {
    const m = mount();
    await toConfirm(m);
    m.send()?.click();
    const shared = result();
    const token = shared.steps[0];
    if (!token) throw new Error('no token step');
    shared.steps[0] = {
      ...token,
      result: 'reused',
      request: { ...token.request, body: null },
      response: { status: 0, body: null },
    };
    m.shares[0]?.answer.resolve(shared);
    await flush();
    const first = m.slots.trace.querySelector('.share-step');
    expect(first?.querySelector('.share-step__result')?.textContent).toBe(
      'Token reused from the server’s memory: no request sent',
    );
    const line = first?.querySelector('.share-step__request')?.textContent ?? '';
    expect(line).toContain('not sent (token reused)');
    expect(line).not.toContain('no answer');
  });
});
