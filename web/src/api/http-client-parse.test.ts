/**
 * The HTTP client's conversion from the proxy's wire shapes into the contract's parsed shapes,
 * and the cuff route's typed errors. The bodies are what cuff-sim and health-sync document
 * (services/cuff-sim/README.md, services/health-sync/README.md); all values are synthetic.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import type { CuffReadingRequest } from './contract';
import { ApiError, CuffSendError } from './errors';
import { HttpClient } from './http-client';
import { Session } from './session';

const ORIGIN = 'https://dashboard.example.test';

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

let session: Session;
beforeEach(() => {
  sessionStorage.clear();
  session = new Session(sessionStorage);
  session.save({ token: 't', expiresAt: new Date(Date.now() + 3_600_000).toISOString() });
});

function client(...responses: Response[]): HttpClient {
  return new HttpClient({
    baseUrl: '/api',
    session,
    onUnauthorized: () => undefined,
    origin: ORIGIN,
    fetch: () => Promise.resolve(responses.shift() ?? json(200, {})),
  });
}

const READING: CuffReadingRequest = {
  systolic: 120,
  diastolic: 80,
  transport: 'mllp',
  sendMalformed: null,
};

const WINDOW = {
  startTime: '2026-01-01T14:05:00Z',
  endTime: '2026-01-01T14:10:00Z',
  heartRate: { beatsPerMinuteMin: 64, beatsPerMinuteAvg: 68.2, beatsPerMinuteMax: 75 },
};
const RESOURCES = {
  resourceType: 'Bundle',
  type: 'collection',
  entry: [
    { resource: { resourceType: 'Observation', id: 'hr-1', status: 'final', code: {} } },
    { resource: { resourceType: 'Device', id: 'd1' } },
  ],
};

describe('heart-rate trace: wire to parsed', () => {
  it('splits the Bundle by type and keeps the source kind and window', async () => {
    const trace = await client(
      json(200, { resources: RESOURCES, source: { kind: 'reconstructed', window: WINDOW } }),
    ).getHeartRateTrace('hr-1');
    expect(trace.observation.id).toBe('hr-1');
    expect(trace.device?.id).toBe('d1');
    expect(trace.provenance).toBeNull();
    expect(trace.source).toEqual({ kind: 'reconstructed', window: WINDOW });
  });

  it('keeps original', async () => {
    const trace = await client(
      json(200, { resources: RESOURCES, source: { kind: 'original', window: WINDOW } }),
    ).getHeartRateTrace('hr-1');
    expect(trace.source?.kind).toBe('original');
  });

  it('an unknown or missing kind is `unknown`, never a claim either way', async () => {
    for (const kind of ['raw', 'ORIGINAL', 42, undefined]) {
      const trace = await client(
        json(200, { resources: RESOURCES, source: { kind, window: WINDOW } }),
      ).getHeartRateTrace('hr-1');
      expect(trace.source, String(kind)).toEqual({ kind: 'unknown', window: WINDOW });
    }
  });

  it('a source that is not a roll-up window (or the old bare-window shape) is null', async () => {
    for (const source of [
      null,
      'x',
      { kind: 'original' },
      WINDOW,
      { kind: 'original', window: {} },
    ]) {
      const trace = await client(json(200, { resources: RESOURCES, source })).getHeartRateTrace(
        'hr-1',
      );
      expect(trace.source, JSON.stringify(source)).toBeNull();
    }
  });
});

describe('status: health-sync states', () => {
  const body = (watchSync: unknown) => ({
    watchSync,
    observationCounts: { watch: 1, cuff: 2 },
    fhirStore: { reachable: true, checkedAt: '2026-01-15T15:07:30Z', detail: null },
    openemr: { reachable: true, checkedAt: '2026-01-15T15:07:30Z', detail: null },
  });

  it('passes health-sync fields through as documented', async () => {
    const status = await client(
      json(
        200,
        body({
          state: 'connected',
          reason: null,
          notes: ['backfill-needed'],
          strandedPreliminary: 12,
          checkedAt: '2026-01-15T15:07:30Z',
          lastSyncAt: '2026-01-15T15:02:00Z',
        }),
      ),
    ).getStatus();
    expect(status.watchSync).toEqual({
      state: 'connected',
      reason: null,
      notes: ['backfill-needed'],
      strandedPreliminary: 12,
      checkedAt: '2026-01-15T15:07:30Z',
      lastSyncAt: '2026-01-15T15:02:00Z',
    });
    expect(status.observationCounts).toEqual({ watch: 1, cuff: 2 });
  });

  it('keeps each documented state', async () => {
    for (const state of ['connected', 'reconnect-needed', 'not-configured', 'unknown']) {
      const status = await client(json(200, body({ state }))).getStatus();
      expect(status.watchSync.state).toBe(state);
    }
  });

  it('reads an unknown state as `unknown`, drops unknown notes, and a bad count as null', async () => {
    const status = await client(
      json(
        200,
        body({ state: 'ok', notes: ['backfill-needed', 'new-note', 7], strandedPreliminary: '12' }),
      ),
    ).getStatus();
    expect(status.watchSync.state).toBe('unknown');
    expect(status.watchSync.notes).toEqual(['backfill-needed']);
    expect(status.watchSync.strandedPreliminary).toBeNull();
    expect(status.watchSync.lastSyncAt).toBeNull();
    for (const bad of [-1, 1.5, null]) {
      const s = await client(json(200, body({ strandedPreliminary: bad }))).getStatus();
      expect(s.watchSync.strandedPreliminary, String(bad)).toBeNull();
    }
  });
});

describe('status: odd bodies and health-sync names', () => {
  it('does not throw on a body that is not an object; reads as not known', async () => {
    for (const body of [null, [], 'x', 5]) {
      const status = await client(json(200, body)).getStatus();
      expect(status.watchSync.state, JSON.stringify(body)).toBe('unknown');
      expect(status.fhirStore.reachable).toBe(false);
      expect(status.openemr.reachable).toBe(false);
      expect(status.observationCounts).toEqual({ watch: 0, cuff: 0 });
    }
  });

  it("accepts health-sync's own name `googleHealth` when `state` is absent", async () => {
    const s = await client(
      json(200, { watchSync: { googleHealth: 'reconnect-needed' } }),
    ).getStatus();
    expect(s.watchSync.state).toBe('reconnect-needed');
    // `state` wins when both are there.
    const both = await client(
      json(200, { watchSync: { state: 'connected', googleHealth: 'reconnect-needed' } }),
    ).getStatus();
    expect(both.watchSync.state).toBe('connected');
  });

  it('only an explicit true is reachable', async () => {
    const s = await client(
      json(200, { fhirStore: { reachable: 'yes' }, openemr: { reachable: true, checkedAt: 't' } }),
    ).getStatus();
    expect(s.fhirStore.reachable).toBe(false);
    expect(s.openemr).toEqual({ reachable: true, checkedAt: 't', detail: null });
  });
});

describe('cuff send: result fields', () => {
  const RESULT = {
    attemptId: 'ae-1',
    sentAt: '2026-11-15T10:00:00.123Z',
    transport: 'mllp',
    controlId: 'C1',
    sample: null,
    messageSent: 'MSH|^~\\&|CUFFSIM\r',
    framing: 'standard',
    messageName: 'projects/p/locations/l/datasets/d/hl7V2Stores/s/messages/abc=',
    messageNameSource: 'derived-sha256-unverified',
    result: 'accepted',
    ack: { received: true, code: 'AA', controlId: 'C1', reason: null, raw: 'MSA|AA|C1' },
    audit: { recorded: true },
  };

  it('passes cuff-sim result fields through', async () => {
    const r = await client(json(200, RESULT)).submitCuffReading(READING);
    expect(r).toEqual(RESULT);
  });

  it('keeps a null attemptId (the AuditEvent could not be built)', async () => {
    const r = await client(
      json(200, { ...RESULT, attemptId: null, audit: { recorded: false, detail: 'x' } }),
    ).submitCuffReading(READING);
    expect(r.attemptId).toBeNull();
  });

  it('treats any name source other than the ingest response as derived', async () => {
    for (const source of ['something-new', null, undefined]) {
      const r = await client(json(200, { ...RESULT, messageNameSource: source })).submitCuffReading(
        READING,
      );
      expect(r.messageNameSource, String(source)).toBe('derived-sha256-unverified');
    }
    const ingest = await client(
      json(200, { ...RESULT, messageNameSource: 'ingest-response' }),
    ).submitCuffReading(READING);
    expect(ingest.messageNameSource).toBe('ingest-response');
    const none = await client(
      json(200, { ...RESULT, messageName: null, messageNameSource: 'ingest-response' }),
    ).submitCuffReading(READING);
    expect(none.messageNameSource).toBeNull();
  });

  it('works an unknown or missing result out from the ACK instead of passing it on', async () => {
    const cases: [unknown, unknown, string][] = [
      ['stored', RESULT.ack, 'accepted'],
      [undefined, { ...RESULT.ack, code: 'AE' }, 'rejected'],
      [42, { received: false, why: 'timeout' }, 'no-acknowledgment'],
      ['accepted?', undefined, 'no-acknowledgment'],
    ];
    for (const [result, ack, expected] of cases) {
      const r = await client(json(200, { ...RESULT, result, ack })).submitCuffReading(READING);
      expect(r.result, JSON.stringify([result, ack])).toBe(expected);
    }
    // A known value is kept as sent.
    const r = await client(json(200, { ...RESULT, result: 'rejected' })).submitCuffReading(READING);
    expect(r.result).toBe('rejected');
  });

  it('reads an unknown framing as null rather than inventing one', async () => {
    const r = await client(json(200, { ...RESULT, framing: 'weird' })).submitCuffReading(READING);
    expect(r.framing).toBeNull();
    const https = await client(
      json(200, { ...RESULT, transport: 'https', framing: null }),
    ).submitCuffReading(READING);
    expect(https.framing).toBeNull();
  });
});

describe('cuff send: refusals are typed', () => {
  /**
   * Bodies exactly as cuff-sim writes them: `_operation_outcome` in services/cuff-sim/cuff_sim/
   * app.py (one issue per field, `diagnostics` a fragment, the field in `expression`), with the
   * texts from request.py and app.py and the statuses its README and tests/test_app.py assert.
   */
  const fhir = (status: number, body: unknown, headers: Record<string, string> = {}) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/fhir+json', ...headers },
    });
  const oo = (code: string, issues: [string, string][]) => ({
    resourceType: 'OperationOutcome',
    issue: issues.map(([name, text]) => ({
      severity: 'error',
      code,
      diagnostics: text,
      expression: [name],
    })),
  });
  const refuse = async (response: Response): Promise<CuffSendError> => {
    const error: unknown = await client(response)
      .submitCuffReading(READING)
      .catch((e: unknown) => e);
    if (!(error instanceof CuffSendError)) throw new Error(`not a CuffSendError: ${String(error)}`);
    expect(error).toBeInstanceOf(ApiError);
    return error;
  };

  it('400: the body is not a JSON object', async () => {
    const e = await refuse(fhir(400, oo('invalid', [['body', 'must be a JSON object']])));
    expect([e.status, e.reason, e.message, e.fields]).toEqual([
      400,
      'invalid-body',
      'body: must be a JSON object',
      ['body'],
    ]);
  });

  it('413: over 16 KiB', async () => {
    const e = await refuse(
      fhir(413, oo('too-long', [['body', 'the body must be at most 16384 bytes']])),
    );
    expect([e.reason, e.message]).toEqual([
      'too-large',
      'body: the body must be at most 16384 bytes',
    ]);
  });

  it('422 with two fields: each fragment is put after its field, never run together', async () => {
    const e = await refuse(
      fhir(
        422,
        oo('invalid', [
          ['systolic', 'is required'],
          ['diastolic', 'is required'],
        ]),
      ),
    );
    expect(e.reason).toBe('invalid-input');
    expect(e.fields).toEqual(['systolic', 'diastolic']);
    expect(e.message).toBe('systolic: is required; diastolic: is required');
  });

  it('422 for bounds and order', async () => {
    const e = await refuse(
      fhir(
        422,
        oo('invalid', [
          ['diastolic', 'must be from 1 to 400 mm[Hg]'],
          ['systolic', 'must be greater than diastolic'],
        ]),
      ),
    );
    expect(e.message).toBe(
      'diastolic: must be from 1 to 400 mm[Hg]; systolic: must be greater than diastolic',
    );
  });

  it('429: rate-limited, with Retry-After in seconds', async () => {
    const e = await refuse(
      fhir(429, oo('throttled', [['body', 'too many sends; try again later']]), {
        'Retry-After': '10',
      }),
    );
    expect([e.reason, e.retryAfterSeconds, e.message]).toEqual([
      'rate-limited',
      10,
      'body: too many sends; try again later',
    ]);
  });

  it('500: a built message failed validation, or a bug', async () => {
    const validation = await refuse(
      fhir(
        500,
        oo('structure', [['body', 'the built message failed HL7v2 validation; nothing was sent']]),
      ),
    );
    expect([validation.reason, validation.outcome?.issue[0]?.code]).toEqual([
      'server-error',
      'structure',
    ]);
    const bug = await refuse(
      fhir(500, oo('exception', [['body', 'internal error; see the logs']])),
    );
    expect(bug.message).toBe('body: internal error; see the logs');
  });

  it('504 without a body: no-answer, with a generic message', async () => {
    const e = await refuse(new Response('upstream request timeout', { status: 504 }));
    expect([e.reason, e.message]).toEqual(['no-answer', 'The server answered HTTP 504.']);
  });

  it('an issue without an expression is its diagnostics alone', async () => {
    const body = {
      resourceType: 'OperationOutcome',
      issue: [{ severity: 'error', code: 'invalid', diagnostics: 'Something is wrong.' }],
    };
    expect((await refuse(fhir(422, body))).message).toBe('Something is wrong.');
  });

  it('another status stays a plain ApiError', async () => {
    const error: unknown = await client(json(502, {}))
      .submitCuffReading(READING)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(CuffSendError);
  });

  it('only the cuff route is typed: another route keeps a plain ApiError for 422', async () => {
    const error: unknown = await client(fhir(422, oo('invalid', [['x', 'no']])))
      .share({ observations: [] })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(CuffSendError);
  });
});

describe('Retry-After', () => {
  it.each([
    ['10', 10],
    [' 7 ', 7],
    ['0', 0],
    ['-5', null],
    ['1.5', null],
    ['10abc', null],
    ['Wed, 21 Oct 2026 07:28:00 GMT', null],
    ['', null],
  ])('"%s" is %s seconds', async (header, expected) => {
    const e: unknown = await client(json(429, {}, { 'Retry-After': header }))
      .submitCuffReading(READING)
      .catch((x: unknown) => x);
    expect((e as ApiError).retryAfterSeconds).toBe(expected);
  });

  it('applies to the login route too', async () => {
    const e: unknown = await client(json(429, {}, { 'Retry-After': '10abc' }))
      .login('pw')
      .catch((x: unknown) => x);
    expect(e).toMatchObject({ reason: 'rate-limited', retryAfterSeconds: null });
    const ok: unknown = await client(json(429, {}, { 'Retry-After': '30' }))
      .login('pw')
      .catch((x: unknown) => x);
    expect(ok).toMatchObject({ retryAfterSeconds: 30 });
  });
});
