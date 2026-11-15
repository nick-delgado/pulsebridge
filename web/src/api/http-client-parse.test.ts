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
  const outcome = (code: string, field: string, text: string) => ({
    resourceType: 'OperationOutcome',
    issue: [{ severity: 'error', code, diagnostics: text, expression: [field] }],
  });

  it.each([
    [400, 'invalid-body', 'invalid'],
    [413, 'too-large', 'too-long'],
    [422, 'invalid-input', 'invalid'],
    [500, 'server-error', 'structure'],
    [504, 'no-answer', 'timeout'],
  ] as const)('%i becomes CuffSendError %s', async (status, reason, code) => {
    const error: unknown = await client(json(status, outcome(code, 'body', 'refused')))
      .submitCuffReading(READING)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CuffSendError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status, reason, message: 'refused' });
  });

  it('429 carries Retry-After as seconds', async () => {
    const error: unknown = await client(
      json(429, outcome('throttled', 'body', 'too many sends; try again later'), {
        'Retry-After': '7',
      }),
    )
      .submitCuffReading(READING)
      .catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 429, reason: 'rate-limited', retryAfterSeconds: 7 });
  });

  it('a 422 names every wrong field and joins every diagnostic', async () => {
    const body = {
      resourceType: 'OperationOutcome',
      issue: [
        {
          severity: 'error',
          code: 'invalid',
          diagnostics: 'systolic: 1 to 400.',
          expression: ['systolic'],
        },
        {
          severity: 'error',
          code: 'invalid',
          diagnostics: 'diastolic: 1 to 400.',
          expression: ['diastolic'],
        },
      ],
    };
    const error = (await client(json(422, body))
      .submitCuffReading(READING)
      .catch((e: unknown) => e)) as CuffSendError;
    expect(error.fields).toEqual(['systolic', 'diastolic']);
    expect(error.message).toBe('systolic: 1 to 400. diastolic: 1 to 400.');
  });

  it('another status stays a plain ApiError', async () => {
    const error: unknown = await client(json(502, {}))
      .submitCuffReading(READING)
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(CuffSendError);
  });

  it('only the cuff route is typed: another route keeps a plain ApiError for 422', async () => {
    const error: unknown = await client(json(422, outcome('invalid', 'x', 'no')))
      .share({ observations: [] })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(CuffSendError);
  });
});
