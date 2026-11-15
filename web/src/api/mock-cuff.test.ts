/**
 * The mock's cuff route against cuff-sim's documented contract (services/cuff-sim/README.md):
 * limits, refusals, the result fields, the derived MLLP message name, the rate limit, and an
 * AuditEvent that could not be recorded. Synthetic values only.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import samplesManifest from '../../../libs/pulsebridge_hl7v2/samples/samples.json';
import { isDerivedMessageName, type CuffReadingRequest } from './contract';
import { CuffSendError } from './errors';
import { MockClient, type MockClientOptions } from './mock-client';
import { MOCK_HL7V2_STORE } from './mock-data';
import { Session } from './session';

const NOW = Date.parse('2026-03-10T15:02:00Z');
const SAMPLES = (
  samplesManifest as { samples: { name: string; class: string; mllp_framing: string }[] }
).samples;

let now: number;
async function client(options: Partial<MockClientOptions> = {}): Promise<MockClient> {
  const session = new Session(sessionStorage, () => now);
  const c = new MockClient({
    session,
    onUnauthorized: () => undefined,
    now: () => now,
    latencyMs: 0,
    rateLimit: null,
    ...options,
  });
  await c.login('x');
  return c;
}

/** A built reading by default; `over` may make it a malformed send. */
function reading(over: Partial<CuffReadingRequest> = {}): CuffReadingRequest {
  const base = { systolic: 120, diastolic: 80, transport: 'mllp' as const, sendMalformed: null };
  return { ...base, ...over };
}

async function refusal(c: MockClient, request: unknown): Promise<CuffSendError> {
  const error = await c.submitCuffReading(request as CuffReadingRequest).catch((e: unknown) => e);
  if (!(error instanceof CuffSendError))
    throw new Error(`expected a CuffSendError: ${String(error)}`);
  return error;
}

/** The URL-safe base64 (padded) SHA-256 of a string, computed here independently. */
async function sha256UrlSafe(text: string): Promise<string> {
  const bytes = new Uint8Array(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)),
  );
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_');
}

beforeEach(() => {
  sessionStorage.clear();
  now = NOW;
});

describe('mock cuff route: the service limits', () => {
  it('accepts the edges, 1 and 400 mm[Hg], with systolic above diastolic', async () => {
    const c = await client();
    await expect(
      c.submitCuffReading(reading({ systolic: 400, diastolic: 1 })),
    ).resolves.toMatchObject({ result: 'accepted' });
    await expect(
      c.submitCuffReading(reading({ systolic: 2, diastolic: 1 })),
    ).resolves.toMatchObject({
      result: 'accepted',
    });
  });

  it('refuses 0, 401, non-integers and systolic not above diastolic with 422 naming the field', async () => {
    const c = await client();
    const before = (await c.listMessages({ kind: 'cuff-send' })).entries.length;
    // Values as a caller outside TypeScript could send them (the proxy forwards JSON).
    const cases: [Record<string, unknown>, string[]][] = [
      [{ systolic: 401 }, ['systolic']],
      [{ diastolic: 0 }, ['diastolic']],
      [{ systolic: 120.5 }, ['systolic']],
      [{ systolic: '120' }, ['systolic']],
      [{ systolic: 80, diastolic: 80 }, ['systolic']],
    ];
    for (const [over, fields] of cases) {
      const e = await refusal(c, { ...reading(), ...over });
      expect([e.status, e.reason, e.fields], JSON.stringify(over)).toEqual([
        422,
        'invalid-input',
        fields,
      ]);
      expect(e.outcome?.issue[0]?.code).toBe('invalid');
    }
    // Nothing was sent, so nothing was recorded.
    expect((await c.listMessages({ kind: 'cuff-send' })).entries).toHaveLength(before);
  });

  it('requires both values for a built reading', async () => {
    const c = await client();
    const e = await refusal(c, { transport: 'mllp', sendMalformed: null });
    expect(e.fields).toEqual(['systolic', 'diastolic']);
  });

  it('lets a malformed send leave the values out, but still checks them if present', async () => {
    const c = await client();
    const sent = await c.submitCuffReading({
      transport: 'mllp',
      sendMalformed: 'unparseable_no_msh',
    });
    expect(sent.sample).toBe('unparseable_no_msh');
    const e = await refusal(c, {
      transport: 'mllp',
      sendMalformed: 'unparseable_no_msh',
      systolic: 999,
    });
    expect(e.fields).toEqual(['systolic']);
  });

  it('refuses an unknown sample, or one not for this transport, on sendMalformed', async () => {
    const c = await client();
    expect((await refusal(c, reading({ sendMalformed: 'no_such_sample' }))).fields).toEqual([
      'sendMalformed',
    ]);
    expect(
      (await refusal(c, reading({ transport: 'https', sendMalformed: 'noack_missing_end_block' })))
        .fields,
    ).toEqual(['sendMalformed']);
  });

  it('stores a reading outside the mapper range with AA, then maps it to error out-of-range', async () => {
    const c = await client();
    const sent = await c.submitCuffReading(reading({ systolic: 350, diastolic: 80 }));
    expect(sent.result).toBe('accepted');
    now += 5000;
    const trace = await c.getCuffAttemptTrace(sent.attemptId ?? '');
    expect(trace.attempt.application).toEqual({ outcome: 'error', reason: 'out-of-range' });
    expect(trace.observation).toBeNull();
  });
});

describe('mock cuff route: the result fields', () => {
  it('over MLLP: standard framing, and a DERIVED message name from the bytes sent', async () => {
    const c = await client();
    const sent = await c.submitCuffReading(reading());
    expect(sent.framing).toBe('standard');
    expect(sent.result).toBe('accepted');
    expect(sent.messageNameSource).toBe('derived-sha256-unverified');
    expect(isDerivedMessageName(sent)).toBe(true);
    expect(sent.messageName).toBe(
      `${MOCK_HL7V2_STORE}/messages/${await sha256UrlSafe(sent.messageSent)}`,
    );
    // The derived name is not in the permanent record (cuff-sim keeps it out of the AuditEvent).
    const trace = await c.getCuffAttemptTrace(sent.attemptId ?? '');
    expect(trace.attempt.messageName).toBeNull();
  });

  it('over HTTPS: no framing, and the name from the ingest response', async () => {
    const c = await client();
    const sent = await c.submitCuffReading(reading({ transport: 'https' }));
    expect(sent.framing).toBeNull();
    expect(sent.messageNameSource).toBe('ingest-response');
    expect(isDerivedMessageName(sent)).toBe(false);
    expect(sent.messageName).toMatch(/\/messages\//);
  });

  it('no name for a rejected or unacknowledged send; result follows the ACK', async () => {
    const c = await client();
    const rejected = await c.submitCuffReading(reading({ sendMalformed: 'unparseable_no_msh' }));
    expect([rejected.result, rejected.messageName, rejected.messageNameSource]).toEqual([
      'rejected',
      null,
      null,
    ]);
    const noAck = await c.submitCuffReading(reading({ sendMalformed: 'noack_missing_end_block' }));
    expect([noAck.result, noAck.messageName, noAck.messageNameSource]).toEqual([
      'no-acknowledgment',
      null,
      null,
    ]);
  });

  it("reports each sample's own MLLP framing, as T07 defines it", async () => {
    const c = await client();
    for (const s of SAMPLES.filter((x) => x.class === 'no_acknowledgment')) {
      const sent = await c.submitCuffReading(reading({ sendMalformed: s.name }));
      expect(sent.framing, s.name).toBe(s.mllp_framing);
    }
    expect(SAMPLES.some((x) => x.mllp_framing !== 'standard')).toBe(true);
  });
});

describe('mock cuff route: AuditEvent not recorded', () => {
  it('build failure: the send happened, attemptId is null, and nothing is in the log', async () => {
    const c = await client({ auditFailure: 'build' });
    const before = (await c.listMessages({ kind: 'cuff-send' })).entries.length;
    const sent = await c.submitCuffReading(reading());
    expect(sent.attemptId).toBeNull();
    expect(sent.audit).toMatchObject({ recorded: false });
    expect(sent.ack).toMatchObject({ received: true, code: 'AA' });
    expect((await c.listMessages({ kind: 'cuff-send' })).entries).toHaveLength(before);
  });

  it('write failure: an id exists, but no AuditEvent is stored under it', async () => {
    const c = await client({ auditFailure: 'write' });
    const sent = await c.submitCuffReading(reading());
    expect(sent.attemptId).not.toBeNull();
    expect(sent.audit.recorded).toBe(false);
    expect(!sent.audit.recorded && sent.audit.detail).toMatch(/^write-failed/);
    await expect(c.getCuffAttemptTrace(sent.attemptId ?? '')).rejects.toMatchObject({
      status: 404,
    });
  });
});

describe('mock cuff route: rate limit', () => {
  it('allows the burst, then answers 429 with Retry-After, and refills over time', async () => {
    const c = await client({ rateLimit: { burst: 3, perMinute: 6 } });
    for (let i = 0; i < 3; i++) await c.submitCuffReading(reading());
    const e = await refusal(c, reading());
    expect([e.status, e.reason]).toEqual([429, 'rate-limited']);
    // One token per 10 s at 6 a minute.
    expect(e.retryAfterSeconds).toBe(10);
    now += 10_000;
    await expect(c.submitCuffReading(reading())).resolves.toMatchObject({ result: 'accepted' });
  });

  it('checks the input first: a refused body does not use a send', async () => {
    const c = await client({ rateLimit: { burst: 1, perMinute: 6 } });
    await refusal(c, reading({ systolic: 0 }));
    await expect(c.submitCuffReading(reading())).resolves.toMatchObject({ result: 'accepted' });
  });

  it("is on by default, with cuff-sim's defaults", async () => {
    const c = new MockClient({
      session: new Session(sessionStorage, () => now),
      onUnauthorized: () => undefined,
      now: () => now,
      latencyMs: 0,
    });
    await c.login('x');
    for (let i = 0; i < 3; i++) await c.submitCuffReading(reading());
    expect((await refusal(c, reading())).status).toBe(429);
  });
});
