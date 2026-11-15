/**
 * The HTTP client against fhir-proxy AS BUILT (services/fhir-proxy/README.md, "Contract
 * amendments", 1 to 21), and against the share route specified for T15. Every wire body below
 * is written literally in the shape the proxy's own code builds it (messages.py `attempt_row`,
 * `store_only_row`, `unreadable_row`, `groups_from_provenances`, `page`; status.py; app.py;
 * errors.py `Problem.outcome`; extensions.py `trace_step`). All values are synthetic.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import type { SendAttemptEntry } from './contract';
import { SHARE_URL_HIDDEN } from './contract';
import { ApiError, isServiceNotConfigured, proxyReason, ShareError } from './errors';
import { HttpClient } from './http-client';
import { Session } from './session';

const ORIGIN = 'https://dashboard.example.test';
const STORE = 'projects/p/locations/l/datasets/d/hl7V2Stores/s';

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** An OperationOutcome exactly as errors.py `Problem.outcome` writes one. */
function problem(code: string, reason: string, text: string, expression?: string[]) {
  return {
    resourceType: 'OperationOutcome',
    issue: [
      {
        severity: 'error',
        code,
        details: { coding: [{ system: 'urn:pulsebridge:fhir-proxy:reason', code: reason }] },
        diagnostics: text,
        ...(expression ? { expression } : {}),
      },
    ],
  };
}

let session: Session;
let requests: { url: string; init: RequestInit }[];
beforeEach(() => {
  sessionStorage.clear();
  session = new Session(sessionStorage);
  session.save({ token: 't', expiresAt: new Date(Date.now() + 3_600_000).toISOString() });
  requests = [];
});

function client(...responses: Response[]): HttpClient {
  return new HttpClient({
    baseUrl: '/api',
    session,
    onUnauthorized: () => undefined,
    origin: ORIGIN,
    fetch: (input, init) => {
      requests.push({ url: (input as URL).toString(), init: init ?? {} });
      return Promise.resolve(responses.shift() ?? json(200, {}));
    },
  });
}

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  const e: unknown = await p.catch((x: unknown) => x);
  if (!(e instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(e)}`);
  return e;
}

// ---- literal rows, as messages.py writes them -------------------------------------------------

/** attempt_row: an accepted MLLP attempt whose derived name the store confirmed, mapped. */
const CONFIRMED = {
  kind: 'cuff-send',
  id: 'att-0001',
  sentAt: '2026-03-10T14:17:00.000Z',
  recordedAt: '2026-03-10T14:17:01.000Z',
  transport: 'mllp',
  controlId: 'C0FFEE0000000000000A',
  result: 'accepted',
  outcome: '0',
  sample: null,
  commitAck: {
    received: true,
    code: 'AA',
    controlId: null,
    reason: null,
    raw: 'MSH|^~\\&|||||20260310141701||ACK|A1|P|2.5.1\rMSA|AA|C0FFEE0000000000000A',
  },
  application: { outcome: 'accepted', reason: null },
  messageName: null,
  derivedMessageName: `${STORE}/messages/abc=`,
  messageLookup: 'confirmed',
  observation: 'Observation/bp-1',
  source: 'audit-event',
};

/** attempt_row with lookup not-found: the proxy sends no outcome and no Observation. */
const NOT_FOUND = {
  ...CONFIRMED,
  id: 'att-0002',
  sentAt: '2026-03-10T14:15:00.000Z',
  recordedAt: '2026-03-10T14:15:01.000Z',
  application: null,
  messageLookup: 'not-found',
  observation: null,
};

/** store_only_row with the hint. */
const STORE_ONLY = {
  kind: 'cuff-send',
  id: `${STORE}/messages/real=`,
  sentAt: '2026-03-10T14:15:00.000Z',
  recordedAt: '2026-03-10T14:15:02.000Z',
  transport: null,
  controlId: 'C0FFEE0000000000000A',
  result: 'accepted',
  outcome: '0',
  sample: null,
  commitAck: { received: true, code: 'AA', controlId: null, reason: null, raw: null },
  application: { outcome: 'accepted', reason: null },
  messageName: `${STORE}/messages/real=`,
  derivedMessageName: null,
  messageLookup: 'confirmed',
  observation: 'Observation/bp-2',
  source: 'store-only',
  possibleDuplicateOf: 'att-0002',
};

/** unreadable_row of an AuditEvent with no id (amendment 6). */
const UNREADABLE = {
  kind: 'unreadable-send-attempt',
  id: '',
  recordedAt: null,
  problem: 'the AuditEvent has no recorded time',
};

/** groups_from_provenances: the seeder's Bundle, httpStatus null. */
const SEED_TX = {
  kind: 'fhir-transaction',
  id: 'tx-0001',
  recordedAt: '2026-03-10T13:00:00.000Z',
  writer: 'seed',
  httpStatus: null,
  resources: ['Observation/hr-1', 'Provenance/p-1'],
};

describe('GET /messages: rows as the proxy builds them (amendments 1, 2, 5, 6, 14, 16)', () => {
  it('reads every row kind with its new fields', async () => {
    const page = await client(
      json(200, {
        entries: [CONFIRMED, NOT_FOUND, STORE_ONLY, SEED_TX, UNREADABLE],
        nextCursor: 'eyJ2IjoxfQ',
        unreadable: 1,
        partial: false,
      }),
    ).listMessages({ kind: 'cuff-send', outcome: 'accepted', count: 25, cursor: 'abc' });
    expect(requests[0]?.url).toBe(
      `${ORIGIN}/api/messages?kind=cuff-send&outcome=accepted&count=25&cursor=abc`,
    );
    expect(page.partial).toBe(false);
    expect(page.nextCursor).toBe('eyJ2IjoxfQ');
    const [a, b, c, d, e] = page.entries as [
      SendAttemptEntry,
      SendAttemptEntry,
      SendAttemptEntry,
      unknown,
      unknown,
    ];
    expect(a).toMatchObject({ messageLookup: 'confirmed', possibleDuplicateOf: null });
    expect(a.application?.outcome).toBe('accepted');
    expect(b).toMatchObject({ messageLookup: 'not-found', application: null, observation: null });
    expect(c).toMatchObject({
      source: 'store-only',
      messageLookup: 'confirmed',
      transport: null,
      possibleDuplicateOf: 'att-0002',
    });
    expect(d).toMatchObject({ kind: 'fhir-transaction', writer: 'seed', httpStatus: null });
    expect(e).toEqual(UNREADABLE);
  });

  it('only a confirmed lookup carries an outcome and an Observation, whatever the body says', async () => {
    const page = await client(
      json(200, {
        entries: [
          { ...CONFIRMED, messageLookup: 'failed' },
          { ...CONFIRMED, id: 'x2', messageLookup: undefined },
          { ...CONFIRMED, id: 'x3', messageLookup: 'something-new' },
        ],
        nextCursor: null,
        unreadable: 0,
        partial: true,
      }),
    ).listMessages();
    for (const row of page.entries as SendAttemptEntry[]) {
      expect(row).toMatchObject({ messageLookup: 'failed', application: null, observation: null });
    }
  });

  it('possibleDuplicateOf is kept only on a store-only row', async () => {
    const page = await client(
      json(200, {
        entries: [{ ...CONFIRMED, possibleDuplicateOf: 'att-9' }],
        nextCursor: null,
        unreadable: 0,
        partial: false,
      }),
    ).listMessages();
    expect((page.entries[0] as SendAttemptEntry).possibleDuplicateOf).toBeNull();
  });

  it('an unknown writer is "unknown"; a rejected row has no lookup', async () => {
    const page = await client(
      json(200, {
        entries: [
          { ...SEED_TX, writer: 'PulseBridge something' },
          {
            ...CONFIRMED,
            id: 'r1',
            result: 'rejected',
            outcome: '4',
            commitAck: { received: true, code: 'AE', controlId: null, reason: 'Bad', raw: null },
            application: null,
            derivedMessageName: null,
            messageLookup: null,
            observation: null,
          },
        ],
        nextCursor: null,
        unreadable: 0,
        partial: false,
      }),
    ).listMessages();
    expect(page.entries[0]).toMatchObject({ writer: 'unknown' });
    expect(page.entries[1]).toMatchObject({ result: 'rejected', messageLookup: null });
  });

  it('a page that does not say partial: false is partial; an unreadable row makes it partial', async () => {
    const missing = await client(
      json(200, { entries: [], nextCursor: null, unreadable: 0 }),
    ).listMessages();
    expect(missing.partial).toBe(true);
    const dropped = await client(
      json(200, {
        entries: [CONFIRMED, { kind: 'new-kind', id: 'n' }, 'not an object'],
        nextCursor: null,
        unreadable: 0,
        partial: false,
      }),
    ).listMessages();
    expect(dropped.entries).toHaveLength(1);
    expect(dropped.partial).toBe(true);
  });

  it('a short page with a cursor is returned as it is (the log follows it)', async () => {
    const page = await client(
      json(200, { entries: [], nextCursor: 'more', unreadable: 0, partial: false }),
    ).listMessages({ outcome: 'error' });
    expect(page).toEqual({ entries: [], nextCursor: 'more', unreadable: 0, partial: false });
  });

  it('a cursor error is a 400 invalid-cursor (amendment 21)', async () => {
    const e = await rejection(
      client(
        json(
          400,
          problem(
            'invalid',
            'invalid-cursor',
            'The cursor is not valid; load the log from the start.',
            ['cursor'],
          ),
        ),
      ).listMessages({ cursor: 'zzz' }),
    );
    expect([e.status, proxyReason(e.outcome)]).toEqual([400, 'invalid-cursor']);
    expect(e.message).toBe('The cursor is not valid; load the log from the start.');
  });
});

describe('GET /status (amendments 9, 10, 13, 15)', () => {
  const body = {
    watchSync: {
      state: 'unknown',
      reason: 'health-sync-timeout',
      notes: [],
      strandedPreliminary: null,
      checkedAt: '2026-01-02T12:00:00Z',
      lastSyncAt: '2026-01-02T12:00:01.000Z',
    },
    observationCounts: { watch: null, cuff: 0 },
    fhirStore: { reachable: false, checkedAt: '2026-01-02T12:00:00.000Z', detail: 'timeout' },
    openemr: { reachable: false, checkedAt: '2026-01-02T12:00:00.000Z', detail: 'not-checked' },
  };

  it('keeps null counts as null and 0 as 0, the reason code, and milliseconds', async () => {
    const s = await client(json(200, body)).getStatus();
    expect(s.observationCounts).toEqual({ watch: null, cuff: 0 });
    expect(s.watchSync).toMatchObject({ state: 'unknown', reason: 'health-sync-timeout' });
    expect(s.watchSync.lastSyncAt).toBe('2026-01-02T12:00:01.000Z');
    expect(s.openemr).toMatchObject({ reachable: false, detail: 'not-checked' });
  });

  it('a 503 login-not-configured is a "service not configured" error and keeps the session', async () => {
    const e = await rejection(
      client(
        json(
          503,
          problem(
            'transient',
            'login-not-configured',
            'Reviewer login is not configured on the server.',
          ),
        ),
      ).getStatus(),
    );
    expect(isServiceNotConfigured(e)).toBe(true);
    expect(session.token()).toBe('t');
    // Another 503 is not that.
    const other = await rejection(
      client(json(503, problem('transient', 'upstream-unavailable', 'x'))).getStatus(),
    );
    expect(isServiceNotConfigured(other)).toBe(false);
  });
});

describe('search and read (amendments 7, 8, 20)', () => {
  it('follows a path-absolute next link and ignores fullUrl; no total needed', async () => {
    const entry = (id: string) => ({
      fullUrl: `https://dashboard.example.test/api/fhir/Observation/${id}`,
      resource: { resourceType: 'Observation', id, status: 'final', code: {} },
      search: { mode: 'match' },
    });
    const observations = await client(
      json(200, {
        resourceType: 'Bundle',
        type: 'searchset',
        link: [
          { relation: 'self', url: '/api/fhir/Observation?code=http%3A%2F%2Floinc.org%7C8867-4' },
          { relation: 'next', url: '/api/fhir/Observation?code=x&_page_token=p2' },
        ],
        entry: [entry('a')],
      }),
      json(200, {
        resourceType: 'Bundle',
        type: 'searchset',
        link: [{ relation: 'self', url: '/api/fhir/Observation?code=x&_page_token=p2' }],
        entry: [entry('b')],
      }),
    ).searchObservations({ code: '8867-4', range: { start: new Date(0), end: new Date(1000) } });
    expect(observations.map((o) => o.id)).toEqual(['a', 'b']);
    expect(requests[1]?.url).toBe(`${ORIGIN}/api/fhir/Observation?code=x&_page_token=p2`);
  });

  it('a refused search is a 400 with the reason', async () => {
    const e = await rejection(
      client(
        json(400, problem('not-supported', 'unsupported-value', 'code must be ...', ['code'])),
      ).searchObservations({ code: '8867-4', range: { start: new Date(0), end: new Date(1) } }),
    );
    expect([e.status, e.outcome?.issue[0]?.code]).toEqual([400, 'not-supported']);
  });

  it('GET /fhir/Observation/{id}: the Observation, or 404; another resource is refused', async () => {
    const obs = { resourceType: 'Observation', id: 'bp-1', status: 'final', code: {} };
    expect((await client(json(200, obs)).getObservation('bp-1')).id).toBe('bp-1');
    expect(requests[0]?.url).toBe(`${ORIGIN}/api/fhir/Observation/bp-1`);
    const e = await rejection(
      client(
        json(404, problem('not-found', 'not-found', 'The Observation was not found.')),
      ).getObservation('x'),
    );
    expect(e.status).toBe(404);
    const wrong = await rejection(
      client(json(200, { ...obs, id: 'other' })).getObservation('bp-1'),
    );
    expect(wrong.status).toBe(502);
  });
});

describe('GET /trace/cuff-attempts/{id} (amendments 1, 12)', () => {
  it('the attempt is checked like a log row', async () => {
    const trace = await client(
      json(200, {
        attempt: NOT_FOUND,
        messageSent: null,
        resources: { resourceType: 'Bundle', type: 'collection', entry: [] },
      }),
    ).getCuffAttemptTrace('att-0002');
    expect(trace.attempt).toMatchObject({ messageLookup: 'not-found', application: null });
    expect(trace.messageSent).toBeNull();
  });

  it('an unreadable AuditEvent answers 422, a final answer the page can tell apart', async () => {
    const e = await rejection(
      client(
        json(
          422,
          problem(
            'processing',
            'unreadable-send-attempt',
            "This send attempt's AuditEvent could not be read; it is listed in the messages log as unreadable.",
          ),
        ),
      ).getCuffAttemptTrace('att-x'),
    );
    expect([e.status, proxyReason(e.outcome)]).toEqual([422, 'unreadable-send-attempt']);
  });

  it('a trace without a readable attempt is a 502, never a guessed row', async () => {
    const e = await rejection(
      client(json(200, { attempt: null, messageSent: null, resources: {} })).getCuffAttemptTrace(
        'a',
      ),
    );
    expect(e.status).toBe(502);
  });
});

// ---- the share route specified for T15 ---------------------------------------------------------

/** One step as extensions.trace_step builds it. */
function step(name: string, method: string, url: string, status: number, result: string) {
  return {
    step: name,
    label: `label of ${name}`,
    request: { method, url, query: null, body: { client_assertion: '[not shown]' } },
    response: { status, body: { access_token: '[not shown]' } },
    result,
    durationMs: 42,
  };
}

const SENT = {
  resourceType: 'Observation',
  status: 'final',
  code: {},
  subject: { reference: 'Patient/p' },
};
const SHARED = {
  outcome: 'shared',
  steps: [
    step('token', 'POST', '/oauth2/default/token', 200, 'ok'),
    step('patient-search', 'GET', '/apis/default/fhir/Patient', 200, 'found'),
    step('encounter-search', 'GET', '/apis/default/fhir/Encounter', 200, 'not-found'),
    step('encounter-create', 'POST', '/apis/default/fhir/Encounter', 201, 'ok'),
    step('observation-create', 'POST', '/apis/default/fhir/Observation', 201, 'ok'),
  ],
  readBack: {
    patient: { sent: null, stored: { resourceType: 'Patient', id: 'p' } },
    encounter: {
      sent: { resourceType: 'Encounter', status: 'finished' },
      stored: { resourceType: 'Encounter', id: 'e', status: 'finished' },
    },
    observation: { sent: SENT, stored: { ...SENT, id: 'o' } },
  },
};

describe('POST /share (the contract for T15)', () => {
  it('sends exactly {"observationId"} and reads the steps and the read-back', async () => {
    const result = await client(json(200, SHARED)).share({ observationId: 'bp-1' });
    expect(requests[0]?.url).toBe(`${ORIGIN}/api/share`);
    expect(requests[0]?.init.method).toBe('POST');
    expect(JSON.parse(requests[0]?.init.body as string)).toEqual({ observationId: 'bp-1' });
    expect(result.outcome).toBe('shared');
    expect(result.steps.map((s) => s.result)).toEqual(['ok', 'found', 'not-found', 'ok', 'ok']);
    expect(result.readBack.patient).toEqual({
      sent: null,
      stored: { resourceType: 'Patient', id: 'p' },
    });
    expect(result.readBack.observation.stored?.id).toBe('o');
  });

  it('a step url that is not a path is never shown; an unknown result is "unknown"', async () => {
    const result = await client(
      json(200, {
        ...SHARED,
        steps: [
          step('token', 'POST', 'https://openemr.internal/oauth2/default/token', 200, 'ok'),
          step('x', 'GET', '//host/path', 200, 'brand-new'),
          step('y', 'GET', '/apis/default/fhir/Patient?identifier=1', 200, 'ok'),
        ],
      }),
    ).share({ observationId: 'bp-1' });
    expect(result.steps.map((s) => s.request.url)).toEqual([
      SHARE_URL_HIDDEN,
      SHARE_URL_HIDDEN,
      SHARE_URL_HIDDEN,
    ]);
    expect(result.steps[1]?.result).toBe('unknown');
  });

  it('a 200 that does not say shared or already-shared is a 502, never reported as done', async () => {
    const e = await rejection(
      client(json(200, { ...SHARED, outcome: 'partly-shared' })).share({ observationId: 'x' }),
    );
    expect(e.status).toBe(502);
  });

  it('a read-back resource of the wrong type is null', async () => {
    const result = await client(
      json(200, {
        ...SHARED,
        readBack: {
          ...SHARED.readBack,
          patient: { sent: { resourceType: 'Observation' }, stored: 5 },
        },
      }),
    ).share({ observationId: 'x' });
    expect(result.readBack.patient).toEqual({ sent: null, stored: null });
  });

  it.each([
    [404, 'not-found'],
    [422, 'observation-not-final'],
    [422, 'patient-incomplete'],
    [503, 'share-not-configured'],
    [400, 'invalid-body'],
  ] as const)(
    '%s %s before OpenEMR: a ShareError with that reason and no steps',
    async (status, reason) => {
      const e = await rejection(
        client(json(status, problem('business-rule', reason, `text for ${reason}`))).share({
          observationId: 'x',
        }),
      );
      expect(e).toBeInstanceOf(ShareError);
      expect(e as ShareError).toMatchObject({
        status,
        reason,
        steps: [],
        message: `text for ${reason}`,
      });
    },
  );

  it('502/504 part-way: the envelope carries the OperationOutcome and the steps so far', async () => {
    const e = await rejection(
      client(
        json(504, {
          operationOutcome: problem(
            'timeout',
            'openemr-timeout',
            'OpenEMR did not answer in time.',
          ),
          steps: [
            step('token', 'POST', '/oauth2/default/token', 200, 'ok'),
            step('observation-create', 'POST', '/apis/default/fhir/Observation', 0, 'failed'),
          ],
        }),
      ).share({ observationId: 'x' }),
    );
    expect(e).toBeInstanceOf(ShareError);
    const share = e as ShareError;
    expect([share.status, share.reason, share.message]).toEqual([
      504,
      'openemr-timeout',
      'OpenEMR did not answer in time.',
    ]);
    expect(share.steps.map((s) => s.step)).toEqual(['token', 'observation-create']);
  });

  it('request.query: names to a value or values; anything else left out', async () => {
    const search = {
      ...step('encounter-search', 'GET', '/apis/default/fhir/Encounter', 200, 'found'),
      request: {
        method: 'GET',
        url: '/apis/default/fhir/Encounter',
        query: { patient: 'p1', date: ['2026-03-10'], odd: 5, mixed: ['a', 1] },
        body: null,
      },
    };
    const result = await client(json(200, { ...SHARED, steps: [search] })).share({
      observationId: 'x',
    });
    expect(result.steps[0]?.request).toEqual({
      method: 'GET',
      url: '/apis/default/fhir/Encounter',
      query: { patient: 'p1', date: ['2026-03-10'] },
      body: null,
    });
  });

  it('a 200 that is not JSON, or not readable, is a ShareError 502 that does not say nothing was sent', async () => {
    for (const response of [
      new Response('<html>oops</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
      json(200, { outcome: 'shared-ish' }),
    ]) {
      const e = await rejection(client(response).share({ observationId: 'x' }));
      expect(e).toBeInstanceOf(ShareError);
      expect([e.status, (e as ShareError).nothingSent]).toEqual([502, false]);
      expect(e.message).toContain('it is not known what reached the clinic');
    }
  });

  it("the hosting layer's HTML 504: a ShareError with no OperationOutcome, not nothing-sent", async () => {
    const e = await rejection(
      client(
        new Response('<!doctype html><title>504 Gateway Timeout</title>', {
          status: 504,
          headers: { 'Content-Type': 'text/html' },
        }),
      ).share({ observationId: 'x' }),
    );
    expect(e).toBeInstanceOf(ShareError);
    expect([e.status, e.outcome, (e as ShareError).steps, (e as ShareError).nothingSent]).toEqual([
      504,
      null,
      [],
      false,
    ]);
    // The proxy's own refusal before OpenEMR is the one case that says nothing was sent.
    const refused = await rejection(
      client(json(422, problem('business-rule', 'observation-not-final', 'x'))).share({
        observationId: 'x',
      }),
    );
    expect((refused as ShareError).nothingSent).toBe(true);
  });

  it('a network failure is a ShareError with status 0', async () => {
    const c = new HttpClient({
      baseUrl: '/api',
      session,
      onUnauthorized: () => undefined,
      origin: ORIGIN,
      fetch: () => Promise.reject(new TypeError('Failed to fetch')),
    });
    const e = await rejection(c.share({ observationId: 'x' }));
    expect(e).toBeInstanceOf(ShareError);
    expect(e.status).toBe(0);
  });
});
