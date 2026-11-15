import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  CallOptions,
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  MalformedSample,
  PulseBridgeApi,
} from '../api/contract';
import { ApiError, CuffSendError, UnauthorizedError } from '../api/errors';
import type { OperationOutcome } from '../api/fhir';
import { createDataChangeBus, type DataChangeKind } from '../state/data-changed';
import { createSelectionStore, shareableObservation } from '../state/selection';
import { createCuff, RECHECK_REASON } from './panel';
import {
  AE,
  attemptTrace,
  bpObservation,
  deferred,
  noAck,
  SAMPLES,
  sendResult,
  type Deferred,
} from './test-support';

/** A client whose every answer the test controls, recording the signal of each call. */
function fakeClient() {
  const sends: {
    request: CuffReadingRequest;
    signal: AbortSignal;
    answer: Deferred<CuffSendResult>;
  }[] = [];
  const traces: { id: string; signal: AbortSignal; answer: Deferred<CuffAttemptTrace> }[] = [];
  const sampleCalls: Deferred<MalformedSample[]>[] = [];
  const client = {
    mode: 'mock',
    submitCuffReading(request: CuffReadingRequest, options: CallOptions = {}) {
      if (!options.signal) throw new Error('send without a signal');
      const answer = deferred<CuffSendResult>();
      sends.push({ request: structuredClone(request), signal: options.signal, answer });
      return answer.promise;
    },
    getCuffAttemptTrace(id: string, options: CallOptions = {}) {
      if (!options.signal) throw new Error('trace without a signal');
      const answer = deferred<CuffAttemptTrace>();
      traces.push({ id, signal: options.signal, answer });
      return answer.promise;
    },
    listMalformedSamples(options: CallOptions = {}) {
      if (!options.signal) throw new Error('samples without a signal');
      const answer = deferred<MalformedSample[]>();
      sampleCalls.push(answer);
      return answer.promise;
    },
  } as unknown as PulseBridgeApi;
  return { client, sends, traces, sampleCalls };
}

/** Lets resolved promises run their callbacks (works with real and fake timers). */
async function flush() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

let teardown: AbortController | null = null;
afterEach(() => {
  teardown?.abort();
  vi.useRealTimers();
  document.body.replaceChildren();
});

async function mount(options: { maxReadings?: number; samples?: boolean } = {}) {
  const api = fakeClient();
  const selection = createSelectionStore();
  const bus = createDataChangeBus();
  const notified: DataChangeKind[] = [];
  const controller = new AbortController();
  teardown = controller;
  const slots = { panel: document.createElement('div'), trace: document.createElement('div') };
  document.body.append(slots.panel, slots.trace);
  createCuff(
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
    {
      now: () => Date.now(),
      timeZone: 'UTC',
      poll: { isVisible: () => true },
      ...(options.maxReadings ? { maxReadings: options.maxReadings } : {}),
    },
  );
  if (options.samples !== false) {
    api.sampleCalls[0]?.resolve(SAMPLES);
    await flush();
  }
  const q = (selector: string, root: ParentNode = slots.panel): HTMLElement => {
    const found = root.querySelector<HTMLElement>(selector);
    if (!found) throw new Error(`nothing matches ${selector}`);
    return found;
  };
  const input = (id: string) => q(`#${id}`) as HTMLInputElement;
  const sendButton = () => q('#cuff-send') as HTMLButtonElement;
  const set = (id: string, value: string) => {
    const i = input(id);
    i.value = value;
    i.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const check = (id: string, on = true) => {
    const i = input(id);
    i.checked = on;
    i.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const submit = () => {
    (q('#cuff-form') as HTMLFormElement).dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    );
  };
  const rows = () => [...slots.panel.querySelectorAll<HTMLLIElement>('.cuff-reading')];
  const message = () => q('#cuff-form-message').textContent;
  /** Sends a built reading and answers it with `result`. */
  const sendAndAnswer = async (result: CuffSendResult) => {
    submit();
    api.sends.at(-1)?.answer.resolve(result);
    await flush();
  };
  return {
    ...api,
    selection,
    bus,
    notified,
    controller,
    slots,
    input,
    sendButton,
    set,
    check,
    submit,
    rows,
    message,
    sendAndAnswer,
    q,
  };
}

const outcome = (issues: [string, string][]): OperationOutcome => ({
  resourceType: 'OperationOutcome',
  issue: issues.map(([f, d]) => ({
    severity: 'error',
    code: 'invalid',
    diagnostics: d,
    expression: [f],
  })),
});
const refusal = (
  status: number,
  issues: [string, string][] = [['body', 'refused']],
  retry: number | null = null,
) =>
  CuffSendError.from(
    new ApiError(status, `The server answered HTTP ${status}.`, outcome(issues), retry),
  );

describe('the request sent for each form state', () => {
  it('the default form sends 120/80 over MLLP as a built reading', async () => {
    const m = await mount();
    m.submit();
    expect(m.sends.map((s) => s.request)).toStrictEqual([
      { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null },
    ]);
  });

  it('HTTPS and other values', async () => {
    const m = await mount();
    m.set('cuff-systolic', '321');
    m.set('cuff-diastolic', '1');
    m.check('cuff-transport-https');
    m.submit();
    expect(m.sends[0]?.request).toStrictEqual({
      systolic: 321,
      diastolic: 1,
      transport: 'https',
      sendMalformed: null,
    });
  });

  it('a malformed sample sends no pressures, and the pressure inputs are disabled', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    expect((m.q('.cuff-pressures') as HTMLFieldSetElement).disabled).toBe(true);
    expect((m.q('#cuff-samples') as HTMLFieldSetElement).hidden).toBe(false);
    expect(m.sendButton().textContent).toBe('Send malformed sample');
    m.check('cuff-sample-unmappable_missing_obx14');
    m.submit();
    expect(m.sends[0]?.request).toStrictEqual({
      transport: 'mllp',
      sendMalformed: 'unmappable_missing_obx14',
    });
  });

  it('lists the samples by class with their descriptions', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    const groups = [...m.slots.panel.querySelectorAll('.cuff-sample-group')];
    expect(groups.map((g) => g.querySelector('legend')?.textContent)).toEqual([
      'Structurally invalid',
      'Unparseable',
      'No acknowledgment (MLLP only)',
      'Parses but cannot be mapped',
    ]);
    expect(m.q('#cuff-sample-unparseable_no_msh-help').textContent).toBe(
      'The MSH segment is missing.',
    );
  });
});

describe('validation before sending', () => {
  it.each([
    ['0', '80', 'systolic', "Must be from 1 to 400 mm[Hg] (the simulator's limit)."],
    ['401', '80', 'systolic', "Must be from 1 to 400 mm[Hg] (the simulator's limit)."],
    ['120.5', '80', 'systolic', 'Enter a whole number of mm[Hg], with no decimals or units.'],
    ['80', '80', 'systolic', 'Must be greater than the diastolic value.'],
    ['120', '0', 'diastolic', "Must be from 1 to 400 mm[Hg] (the simulator's limit)."],
  ])('%s/%s is not sent; the error sits next to %s', async (s, d, field, text) => {
    const m = await mount();
    m.set('cuff-systolic', s);
    m.set('cuff-diastolic', d);
    m.submit();
    expect(m.sends).toHaveLength(0);
    expect(m.q(`#cuff-${field}-error`).textContent).toBe(text);
    expect(m.input(`cuff-${field}`).getAttribute('aria-invalid')).toBe('true');
    expect(m.input(`cuff-${field}`).getAttribute('aria-describedby')).toContain(
      `cuff-${field}-error`,
    );
    expect(document.activeElement).toBe(m.input(`cuff-${field}`));
    expect(m.message()).toBe('Not sent: correct the marked fields.');
  });

  it('1 and 400 are accepted', async () => {
    const m = await mount();
    m.set('cuff-systolic', '400');
    m.set('cuff-diastolic', '1');
    m.submit();
    expect(m.sends).toHaveLength(1);
  });

  it('says a value outside the mapper range will be sent, then rejected', async () => {
    const m = await mount();
    m.set('cuff-systolic', '320');
    expect(m.q('#cuff-systolic-note').textContent).toContain(
      'rejected by the mapper with "out-of-range"',
    );
    m.set('cuff-systolic', '120');
    expect(m.q('#cuff-systolic-note').textContent).toBe('');
    expect(m.q('#cuff-limits').textContent).toContain('accepts whole numbers from 1 to 400 mm[Hg]');
  });

  it('a malformed send without a sample is not sent', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    m.submit();
    expect(m.sends).toHaveLength(0);
    expect(m.q('#cuff-sample-error').textContent).toBe('Choose a sample to send.');
  });
});

describe('samples and transports', () => {
  it('an MLLP-only sample is disabled over HTTPS, and unchosen with a reason when switching', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    m.check('cuff-sample-noack_missing_end_block');
    m.check('cuff-transport-https');
    expect(m.input('cuff-sample-noack_missing_end_block').disabled).toBe(true);
    expect(m.input('cuff-sample-noack_missing_end_block').checked).toBe(false);
    expect(m.input('cuff-sample-unparseable_no_msh').disabled).toBe(false);
    expect(m.q('#cuff-sample-error').textContent).toBe(
      'noack_missing_end_block can only be sent over MLLP; choose another sample or switch back.',
    );
    m.check('cuff-transport-mllp');
    expect(m.input('cuff-sample-noack_missing_end_block').disabled).toBe(false);
  });

  it('a failed sample load offers a retry', async () => {
    const m = await mount({ samples: false });
    m.sampleCalls[0]?.reject(new ApiError(503, 'Service unavailable'));
    await flush();
    expect(m.q('#cuff-samples [role="alert"]').textContent).toBe(
      'Could not load the samples: Service unavailable',
    );
    (m.q('#cuff-samples button') as HTMLButtonElement).click();
    m.sampleCalls[1]?.resolve(SAMPLES);
    await flush();
    expect(m.slots.panel.querySelectorAll('input[name="cuff-sample"]')).toHaveLength(4);
  });
});

describe('busy state and double submit', () => {
  it('one send at a time: a second submit while sending sends nothing', async () => {
    const m = await mount();
    m.submit();
    m.submit();
    expect(m.sends).toHaveLength(1);
    expect(m.sendButton().disabled).toBe(true);
    expect(m.sendButton().textContent).toBe('Sending…');
    expect(m.q('#cuff-form').getAttribute('aria-busy')).toBe('true');
    m.sends[0]?.answer.resolve(sendResult({ ack: AE }));
    await flush();
    expect(m.sendButton().disabled).toBe(false);
    expect(m.sendButton().textContent).toBe('Take reading');
    m.submit();
    expect(m.sends).toHaveLength(2);
  });
});

describe('refusals', () => {
  it('422: each field’s diagnostics next to it, and nothing was sent', async () => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(
      refusal(422, [
        ['systolic', 'must be greater than diastolic'],
        ['diastolic', 'is required'],
      ]),
    );
    await flush();
    expect(m.q('#cuff-systolic-error').textContent).toBe(
      'The simulator says: must be greater than diastolic',
    );
    expect(m.q('#cuff-diastolic-error').textContent).toBe('The simulator says: is required');
    expect(m.message()).toBe(
      'The simulator refused the reading (HTTP 422): systolic: must be greater than diastolic; diastolic: is required. Nothing was sent.',
    );
    expect(m.q('#cuff-form-message').getAttribute('role')).toBe('alert');
    expect(m.rows()).toHaveLength(0);
    expect(m.notified).toEqual([]);
  });

  it('hostile diagnostics are shown as text, never markup', async () => {
    const m = await mount();
    m.submit();
    const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
    m.sends[0]?.answer.reject(refusal(422, [['systolic', evil]]));
    await flush();
    expect(m.slots.panel.querySelectorAll('img, script')).toHaveLength(0);
    expect(m.q('#cuff-systolic-error').textContent).toBe(`The simulator says: ${evil}`);
    expect(m.message()).toContain(evil);
  });

  it('422 on sendMalformed is shown by the sample picker', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    m.check('cuff-sample-unparseable_no_msh');
    m.submit();
    m.sends[0]?.answer.reject(
      refusal(422, [['sendMalformed', 'this sample cannot be sent over https']]),
    );
    await flush();
    expect(m.q('#cuff-sample-error').textContent).toBe(
      'The simulator says: this sample cannot be sent over https',
    );
  });

  it('429: a visible countdown from Retry-After keeps the button disabled, then re-enables it', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-03-01T10:00:00Z') });
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(refusal(429, [['body', 'too many sends; try again later']], 10));
    await flush();
    expect(m.message()).toContain('Too many readings in a short time');
    expect(m.q('#cuff-countdown').textContent).toBe('You can send again in 10 s.');
    expect(m.sendButton().disabled).toBe(true);
    m.submit();
    expect(m.sends).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.q('#cuff-countdown').textContent).toBe('You can send again in 7 s.');
    await vi.advanceTimersByTimeAsync(6000);
    expect(m.q('#cuff-countdown').textContent).toBe('You can send again in 1 s.');
    expect(m.sendButton().disabled).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(m.q('#cuff-countdown').textContent).toBe('You can send again now.');
    expect(m.sendButton().disabled).toBe(false);
    m.submit();
    expect(m.sends).toHaveLength(2);
  });

  it('429 without Retry-After asks to wait but does not lock the button', async () => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(refusal(429, [['body', 'x']], null));
    await flush();
    expect(m.q('#cuff-countdown').textContent).toBe('Wait a little before sending again.');
    expect(m.sendButton().disabled).toBe(false);
  });

  it('504 no-answer: "may have been sent", and the others are told data may have changed', async () => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(refusal(504));
    await flush();
    expect(m.message()).toContain('The reading may have been sent, but it was not recorded');
    expect(m.notified).toEqual(['cuff-send']);
    expect(m.rows()).toHaveLength(0);
  });

  it.each([
    [500, 'The simulator failed before sending (HTTP 500)'],
    [400, 'The simulator could not read the request (HTTP 400)'],
    [413, 'The request was too large for the simulator (HTTP 413)'],
  ])('%d: nothing was sent', async (status, words) => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(refusal(status));
    await flush();
    expect(m.message()).toContain(words);
    expect(m.message()).toContain('Nothing was sent.');
    expect(m.notified).toEqual([]);
  });

  it('a plain network failure says it is not known whether it was sent', async () => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(new ApiError(0, 'Failed to fetch'));
    await flush();
    expect(m.message()).toBe(
      'The connection failed (Failed to fetch), so it is not known whether the reading was sent. Check the messages log before sending again.',
    );
  });

  it('401 is left to the shell: no message is written over the form', async () => {
    const m = await mount();
    m.submit();
    m.sends[0]?.answer.reject(new UnauthorizedError());
    await flush();
    expect(m.message()).toBe('Sending…');
  });
});

describe('the readings list', () => {
  const rowText = (row: HTMLElement | undefined) => row?.textContent ?? '';

  it('starts empty, with a message', async () => {
    const m = await mount();
    expect(m.slots.panel.textContent).toContain('No readings sent in this session yet.');
  });

  it('AA: time, value, transport, the two levels labelled separately, pending', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult());
    const [row] = m.rows();
    expect(row?.querySelector('time')?.textContent).toBe('09:15:30');
    expect(row?.querySelector('.cuff-reading__what')?.textContent).toBe('BP 120/80 mm[Hg]');
    expect(row?.querySelector('.cuff-reading__transport')?.textContent).toBe('MLLP');
    const terms = [...(row?.querySelectorAll('dt') ?? [])].map((d) => d.textContent);
    expect(terms).toEqual([
      'Commit-level acknowledgment (from the store)',
      'Application-level outcome (from the mapper)',
    ]);
    const labels = [...(row?.querySelectorAll('.cuff-reading__label') ?? [])].map(
      (d) => d.textContent,
    );
    expect(labels).toEqual(['AA, stored', 'Pending']);
    expect(m.notified).toEqual(['cuff-send']);
    expect(m.message()).toBe(
      'Sent over MLLP. Commit-level acknowledgment: AA, stored. Waiting for the mapper…',
    );
  });

  it('AE: rejected with the reason, no application outcome', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult({ ack: AE }));
    const labels = [...(m.rows()[0]?.querySelectorAll('.cuff-reading__label') ?? [])].map(
      (d) => d.textContent,
    );
    expect(labels).toEqual(['AE, rejected', 'None']);
    expect(rowText(m.rows()[0])).toContain('Reason (MSA-3): Invalid MSH header');
    expect(m.notified).toEqual(['cuff-send']);
  });

  it.each([
    ['timeout', 'timed out'],
    ['connection-closed', 'connection closed without a reply'],
    ['api-error', '(HTTP 503)'],
    ['unreadable-reply', 'not a readable HL7v2 ACK'],
  ] as const)('no acknowledgment, %s', async (why, words) => {
    const m = await mount();
    await m.sendAndAnswer(sendResult({ ack: noAck(why) }));
    expect(m.rows()[0]?.querySelector('.cuff-reading__label')?.textContent).toBe(
      'No acknowledgment',
    );
    expect(rowText(m.rows()[0])).toContain(words);
    expect(m.notified).toEqual(['cuff-send']);
  });

  it.each([
    ['build', null, 'the attempt could not be expressed as an AuditEvent'],
    ['write', 'att-w', 'the audit store write failed (HTTP 503)'],
  ])(
    'audit failure (%s): listed, marked not recorded, no trace, not selected, not polled',
    async (_kind, id, detail) => {
      vi.useFakeTimers();
      const m = await mount();
      await m.sendAndAnswer(sendResult({ attemptId: id, audit: { recorded: false, detail } }));
      const row = m.rows()[0];
      expect(rowText(row)).toContain(`Not recorded: ${detail}. This attempt has no trace to open`);
      expect(row?.querySelector('.cuff-reading__show')).toBeNull();
      expect(row?.querySelectorAll('.cuff-reading__label')[1]?.textContent).toBe('Not known');
      expect(m.selection.get()).toBeNull();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(m.traces).toHaveLength(0);
    },
  );

  it('newest first, with a malformed sample shown by name', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult({ ack: AE }));
    m.check('cuff-malformed');
    m.check('cuff-sample-unparseable_no_msh');
    await m.sendAndAnswer(
      sendResult({ ack: AE, attemptId: 'att-2', sample: 'unparseable_no_msh' }),
    );
    expect(m.rows().map((r) => r.querySelector('.cuff-reading__what')?.textContent)).toEqual([
      'Malformed sample unparseable_no_msh',
      'BP 120/80 mm[Hg]',
    ]);
  });

  it('keeps the newest rows; a dropped row stops polling and its selection is cleared', async () => {
    vi.useFakeTimers();
    const m = await mount({ maxReadings: 2 });
    await m.sendAndAnswer(sendResult({ attemptId: 'a1', sentAt: '2026-03-01T09:00:01Z' }));
    await m.sendAndAnswer(sendResult({ attemptId: 'a2', sentAt: '2026-03-01T09:00:02Z' }));
    // Select a1 again (a2 was auto-selected), then let both rows poll once.
    m.rows()[1]?.querySelector<HTMLButtonElement>('.cuff-reading__show')?.click();
    expect(m.selection.get()).toMatchObject({ attemptId: 'a1' });
    await vi.advanceTimersByTimeAsync(3000);
    const a1Calls = () => m.traces.filter((t) => t.id === 'a1');
    expect(a1Calls().length).toBeGreaterThanOrEqual(2);

    await m.sendAndAnswer(sendResult({ attemptId: 'a3', sentAt: '2026-03-01T09:00:03Z' }));
    expect(m.rows().map((r) => r.querySelector('time')?.textContent)).toEqual([
      '09:00:03',
      '09:00:02',
    ]);
    expect(a1Calls().every((t) => t.signal.aborted)).toBe(true);
    // The dropped row's selection is cleared and its trace emptied.
    expect(m.selection.get()).toBeNull();
    expect(m.slots.trace.textContent).toContain('Select a reading to see its trace');
    const before = a1Calls().length;
    await vi.advanceTimersByTimeAsync(9000);
    expect(a1Calls().length).toBe(before);
  });
});

describe('polling the application-level outcome', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse('2026-03-01T09:15:31Z') });
  });

  /** Sends an accepted reading and answers the panel's own trace call with "pending". */
  async function sentPending() {
    const m = await mount();
    await m.sendAndAnswer(sendResult());
    // pick() loaded the trace for the auto-selected new row.
    expect(m.traces).toHaveLength(1);
    m.traces[0]?.answer.resolve(attemptTrace(sendResult(), { outcome: 'pending', reason: null }));
    await flush();
    return m;
  }

  it('pending → accepted: the row, the selection (shareable) and the trace follow', async () => {
    const m = await sentPending();
    expect(shareableObservation(m.selection.get())).toBeNull();
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.traces).toHaveLength(2);
    m.traces[1]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1') },
      ),
    );
    await flush();
    expect(m.rows()[0]?.querySelectorAll('.cuff-reading__label')[1]?.textContent).toBe('Accepted');
    expect(shareableObservation(m.selection.get())).toBe('Observation/bp-1');
    expect(m.slots.trace.textContent).toContain('"id": "bp-1"');
    // Settled: no more polling.
    await vi.advanceTimersByTimeAsync(9000);
    expect(m.traces).toHaveLength(2);
  });

  it('pending → error: the reason in words, and the selection says why it cannot be shared', async () => {
    const m = await sentPending();
    await vi.advanceTimersByTimeAsync(3000);
    m.traces[1]?.answer.resolve(
      attemptTrace(sendResult(), { outcome: 'error', reason: 'out-of-range' }),
    );
    await flush();
    expect(m.rows()[0]?.textContent).toContain(
      'Error: A value is outside the plausible range (systolic 50 to 300, diastolic 20 to 200 mm[Hg]). (reason code out-of-range)',
    );
    expect(m.selection.get()?.unshareableReason).toContain(
      'the mapper could not turn it into an Observation',
    );
    expect(shareableObservation(m.selection.get())).toBeNull();
  });

  it('accepted but the Observation is not final: selected as not shareable', async () => {
    const m = await sentPending();
    await vi.advanceTimersByTimeAsync(3000);
    m.traces[1]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-2', 'preliminary') },
      ),
    );
    await flush();
    expect(m.selection.get()).toMatchObject({
      observationId: 'bp-2',
      unshareableReason:
        'its Observation has status preliminary, not final; only final readings are shared.',
    });
  });

  it('is bounded: after 2 minutes it stops, says so, and offers "Check again"', async () => {
    const m = await sentPending();
    for (let i = 0; i < 45; i++) {
      await vi.advanceTimersByTimeAsync(3000);
      for (const t of m.traces)
        t.answer.resolve(attemptTrace(sendResult(), { outcome: 'pending', reason: null }));
      await flush();
    }
    const calls = m.traces.length;
    expect(calls).toBeGreaterThan(30);
    expect(calls).toBeLessThanOrEqual(42);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(m.traces.length).toBe(calls);
    const row = m.rows()[0];
    expect(row?.textContent).toContain(
      'still not mapped after 2 minutes, so the page stopped checking',
    );
    const again = row?.querySelector<HTMLButtonElement>('.cuff-reading__again');
    expect(again?.hidden).toBe(false);
    again?.click();
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.traces.length).toBe(calls + 1);
  });

  it('a passing failure is retried at the next tick; a 404 stops it', async () => {
    const m = await sentPending();
    await vi.advanceTimersByTimeAsync(3000);
    m.traces[1]?.answer.reject(new ApiError(0, 'offline'));
    await flush();
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.traces).toHaveLength(3);
    m.traces[2]?.answer.reject(new ApiError(404, 'not found'));
    await flush();
    await vi.advanceTimersByTimeAsync(9000);
    expect(m.traces).toHaveLength(3);
    expect(m.rows()[0]?.textContent).toContain('the send-attempt record could not be found');
  });

  it('stops on logout, and a poll answer arriving after logout changes nothing', async () => {
    const m = await sentPending();
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.traces).toHaveLength(2);
    const pending = m.traces[1];
    m.controller.abort();
    expect(pending?.signal.aborted).toBe(true);
    pending?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1') },
      ),
    );
    await flush();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(m.traces).toHaveLength(2);
    expect(m.rows()[0]?.querySelectorAll('.cuff-reading__label')[1]?.textContent).toBe('Pending');
  });

  it('an older "pending" answer after the row settled does not move it back', async () => {
    const m = await sentPending();
    m.rows()[0]?.querySelector<HTMLButtonElement>('.cuff-reading__show')?.click(); // a new panel load
    await vi.advanceTimersByTimeAsync(3000);
    // Poll (traces[2]) answers first with the outcome, then the panel load (traces[1]) with pending.
    m.traces[2]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1') },
      ),
    );
    await flush();
    m.traces[1]?.answer.resolve(attemptTrace(sendResult(), { outcome: 'pending', reason: null }));
    await flush();
    expect(m.rows()[0]?.querySelectorAll('.cuff-reading__label')[1]?.textContent).toBe('Accepted');
    expect(shareableObservation(m.selection.get())).toBe('Observation/bp-1');
  });
});

describe('late answers are ignored', () => {
  it('send, then the user selects something else: the new row does not take the selection', async () => {
    const m = await mount();
    m.submit();
    const watch = {
      source: 'watch' as const,
      observationId: 'hr-1',
      summary: 'Heart rate 60',
      unshareableReason: null,
    };
    m.selection.set(watch);
    m.sends[0]?.answer.resolve(sendResult());
    await flush();
    expect(m.rows()).toHaveLength(1);
    expect(m.selection.get()).toBe(watch);
    // Only the poll will ask for its trace; the panel did not.
    expect(m.traces).toHaveLength(0);
    expect(m.rows()[0]?.classList.contains('cuff-reading--selected')).toBe(false);
  });

  it('a trace answer for a reading no longer selected does not render', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult({ attemptId: 'a1' }));
    await m.sendAndAnswer(sendResult({ attemptId: 'a2', transport: 'https', framing: null }));
    // a2 was auto-selected (traces[1]); now pick a1 again (traces[2]).
    const a1Button = m.rows()[1]?.querySelector<HTMLButtonElement>('.cuff-reading__show');
    a1Button?.click();
    expect(m.traces.map((t) => t.id)).toEqual(['a1', 'a2', 'a1']);
    expect(m.traces[1]?.signal.aborted).toBe(true);
    m.traces[1]?.answer.resolve(
      attemptTrace(sendResult({ attemptId: 'a2', transport: 'https', framing: null }), {
        outcome: 'pending',
        reason: null,
      }),
    );
    await flush();
    expect(m.slots.trace.textContent).toContain(
      'Loading the trace for BP 120/80 mm[Hg] at 09:15:30, MLLP',
    );
    expect(m.slots.trace.textContent).not.toContain('HTTPS');
    expect(m.selection.get()).toMatchObject({ attemptId: 'a1' });
  });

  it('send, then logout: the answer adds no row and notifies nothing', async () => {
    const m = await mount();
    m.submit();
    m.controller.abort();
    expect(m.sends[0]?.signal.aborted).toBe(true);
    m.sends[0]?.answer.resolve(sendResult());
    await flush();
    expect(m.rows()).toHaveLength(0);
    expect(m.notified).toEqual([]);
    expect(m.traces).toHaveLength(0);
  });

  it('the trace panel: a selection moving to the watch side drops our highlight and trace', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult());
    const traceCall = m.traces[0];
    m.selection.set({
      source: 'watch',
      observationId: 'hr-1',
      summary: 'HR',
      unshareableReason: null,
    });
    expect(traceCall?.signal.aborted).toBe(true);
    traceCall?.answer.resolve(attemptTrace(sendResult(), { outcome: 'pending', reason: null }));
    await flush();
    expect(m.slots.trace.textContent).toContain('Select a reading to see its trace');
    expect(m.rows()[0]?.classList.contains('cuff-reading--selected')).toBe(false);
    expect(m.rows()[0]?.querySelector('.cuff-reading__show')?.getAttribute('aria-pressed')).toBe(
      'false',
    );
  });
});

describe('selection and later data changes', () => {
  async function selectedAccepted() {
    const m = await mount();
    await m.sendAndAnswer(sendResult());
    m.traces[0]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1') },
      ),
    );
    await flush();
    expect(shareableObservation(m.selection.get())).toBe('Observation/bp-1');
    return m;
  }

  it('the selection carries the attempt, the Observation and a summary', async () => {
    const m = await selectedAccepted();
    expect(m.selection.get()).toEqual({
      source: 'cuff',
      attemptId: 'att-1',
      observationId: 'bp-1',
      summary: 'BP 120/80 mm[Hg] at 09:15:30, MLLP',
      unshareableReason: null,
    });
    expect(m.rows()[0]?.querySelector('.cuff-reading__selected')?.hasAttribute('hidden')).toBe(
      false,
    );
  });

  it.each(['sync', 'share'] as const)(
    'after a %s change the selection is not shareable until re-checked, then follows the new data',
    async (kind) => {
      const m = await selectedAccepted();
      m.bus.notify(kind);
      expect(m.selection.get()?.unshareableReason).toBe(RECHECK_REASON);
      expect(shareableObservation(m.selection.get())).toBeNull();
      expect(m.traces).toHaveLength(2);
      m.traces[1]?.answer.resolve(
        attemptTrace(
          sendResult(),
          { outcome: 'accepted', reason: null },
          { observation: bpObservation('bp-1', 'amended') },
        ),
      );
      await flush();
      expect(m.selection.get()?.unshareableReason).toBe(
        'its Observation has status amended, not final; only final readings are shared.',
      );
    },
  );

  it('two re-checks overlap: the older answer, arriving last, is ignored', async () => {
    const m = await selectedAccepted();
    m.bus.notify('sync'); // traces[1]
    m.bus.notify('share'); // traces[2] replaces it
    expect(m.traces[1]?.signal.aborted).toBe(true);
    m.traces[2]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1', 'entered-in-error') },
      ),
    );
    await flush();
    // The older request answers last, with the older (final) copy: it must not make it shareable.
    m.traces[1]?.answer.resolve(
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-1') },
      ),
    );
    await flush();
    expect(shareableObservation(m.selection.get())).toBeNull();
    expect(m.selection.get()?.unshareableReason).toContain('status entered-in-error');
    expect(m.slots.trace.textContent).toContain('"status": "entered-in-error"');
  });

  it('a re-check that finds the attempt gone clears the selection and says so', async () => {
    const m = await selectedAccepted();
    m.bus.notify('sync');
    m.traces[1]?.answer.reject(new ApiError(404, 'gone'));
    await flush();
    expect(m.selection.get()).toBeNull();
    expect(m.slots.trace.textContent).toContain('no longer in the audit store');
  });

  it('a re-check that fails keeps it unshareable, with a retry', async () => {
    const m = await selectedAccepted();
    m.bus.notify('share');
    m.traces[1]?.answer.reject(new ApiError(502, 'Bad gateway'));
    await flush();
    expect(m.selection.get()?.unshareableReason).toBe('could not be checked: Bad gateway');
    expect(m.slots.trace.querySelector('[role="alert"]')?.textContent).toBe(
      'Could not load the trace: Bad gateway',
    );
    m.slots.trace.querySelector<HTMLButtonElement>('button')?.click();
    expect(m.traces).toHaveLength(3);
  });

  it('our own cuff-send announcement does not re-check (nothing behind the selection changed)', async () => {
    const m = await selectedAccepted();
    m.bus.notify('cuff-send');
    expect(m.traces).toHaveLength(1);
    expect(shareableObservation(m.selection.get())).toBe('Observation/bp-1');
  });

  it('a rejected reading is selected as not shareable', async () => {
    const m = await mount();
    await m.sendAndAnswer(sendResult({ ack: AE }));
    expect(m.selection.get()).toMatchObject({
      observationId: null,
      unshareableReason:
        'the HL7v2 store rejected this message, so it was never stored and there is no Observation.',
    });
  });
});

describe('element ids', () => {
  it('every id in the slots starts with cuff- (or ui- from the shared tabs) and is unique', async () => {
    const m = await mount();
    m.check('cuff-malformed');
    await m.sendAndAnswer(sendResult());
    m.traces[0]?.answer.resolve(attemptTrace(sendResult(), { outcome: 'pending', reason: null }));
    await flush();
    const ids = [...document.querySelectorAll('[id]')].map((e) => e.id);
    expect(ids.length).toBeGreaterThan(20);
    expect(ids.filter((id) => !/^(cuff-|ui-)/.test(id))).toEqual([]);
    expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
    // Every aria reference points at an element that exists.
    const refs = [
      ...document.querySelectorAll('[aria-describedby], [aria-labelledby], [aria-controls]'),
    ].flatMap((e) =>
      ['aria-describedby', 'aria-labelledby', 'aria-controls'].flatMap((a) =>
        (e.getAttribute(a) ?? '').split(/\s+/).filter(Boolean),
      ),
    );
    expect(refs.filter((id) => !document.getElementById(id))).toEqual([]);
  });
});
