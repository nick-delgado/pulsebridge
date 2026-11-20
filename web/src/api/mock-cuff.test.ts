/**
 * The mock's cuff route against cuff-sim's documented contract (services/cuff-sim/README.md):
 * limits, refusals, the result fields, the derived MLLP message name, the rate limit, and an
 * AuditEvent that could not be recorded. Synthetic values only.
 */
import type { SendAttemptEntry } from './contract';
import { beforeEach, describe, expect, it } from 'vitest';
import samplesManifest from '../../../libs/pulsebridge_hl7v2/samples/samples.json';
import { isDerivedMessageName, type CuffReadingRequest } from './contract';
import { CuffSendError } from './errors';
import { MockClient, type MockClientOptions } from './mock-client';
import { MOCK_HL7V2_STORE } from './mock-data';

const NOW = Date.parse('2026-03-10T15:02:00Z');
const SAMPLES = (
  samplesManifest as { samples: { name: string; class: string; mllp_framing: string }[] }
).samples;

let now: number;
async function client(options: Partial<MockClientOptions> = {}): Promise<MockClient> {
  const c = new MockClient({
    onUnauthorized: () => undefined,
    now: () => now,
    latencyMs: 0,
    rateLimit: null,
    ...options,
  });
  await c.startSession('mock-recaptcha-test');
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

describe('mock cuff route: refusals exactly as cuff-sim writes them', () => {
  /** [expression, diagnostics] of each issue, in order. */
  const issues = (e: CuffSendError) =>
    (e.outcome?.issue ?? []).map((i) => [i.expression?.join(','), i.diagnostics, i.code]);

  it('uses request.py fragments, the field in expression, code invalid', async () => {
    const c = await client();
    const cases: [Record<string, unknown>, [string, string][]][] = [
      [{ systolic: 401 }, [['systolic', 'must be from 1 to 400 mm[Hg]']]],
      [{ diastolic: 0 }, [['diastolic', 'must be from 1 to 400 mm[Hg]']]],
      [{ systolic: 120.5 }, [['systolic', 'must be a whole number of mm[Hg]']]],
      [{ systolic: true }, [['systolic', 'must be a whole number of mm[Hg]']]],
      [{ systolic: 80, diastolic: 80 }, [['systolic', 'must be greater than diastolic']]],
      [{ transport: 'ftp' }, [['transport', 'must be "mllp" or "https"']]],
      [{ transport: null }, [['transport', 'must be "mllp" or "https"']]],
      [
        { sendMalformed: 'no_such_sample' },
        [['sendMalformed', 'must be the name of a malformed sample, or null']],
      ],
      [
        { transport: 'https', sendMalformed: 'noack_missing_end_block' },
        [['sendMalformed', 'this sample cannot be sent over https']],
      ],
    ];
    for (const [over, expected] of cases) {
      const e = await refusal(c, { ...reading(), ...over });
      expect(issues(e), JSON.stringify(over)).toEqual(expected.map(([f, t]) => [f, t, 'invalid']));
      expect(e.status).toBe(422);
    }
  });

  it('collects every issue before refusing, systolic-not-above included', async () => {
    const c = await client();
    const e = await refusal(c, {
      systolic: 80,
      diastolic: 90,
      transport: 'mllp',
      sendMalformed: null,
      extra: 1,
    });
    expect(issues(e)).toEqual([
      [
        'body',
        'only these fields are accepted: systolic, diastolic, transport, sendMalformed',
        'invalid',
      ],
      ['systolic', 'must be greater than diastolic', 'invalid'],
    ]);
    expect(e.message).toBe(
      'body: only these fields are accepted: systolic, diastolic, transport, sendMalformed; systolic: must be greater than diastolic',
    );
  });

  it('treats a null value as absent, so a built reading requires both', async () => {
    const c = await client();
    const e = await refusal(c, { systolic: null, diastolic: null, sendMalformed: null });
    expect(issues(e)).toEqual([
      ['systolic', 'is required', 'invalid'],
      ['diastolic', 'is required', 'invalid'],
    ]);
    expect(e.message).toBe('systolic: is required; diastolic: is required');
    // With a sample, null values are simply absent.
    await expect(
      c.submitCuffReading({ systolic: null, sendMalformed: 'unparseable_no_msh' } as never),
    ).resolves.toMatchObject({ sample: 'unparseable_no_msh' });
  });

  it('defaults an absent transport to mllp', async () => {
    const c = await client();
    const sent = await c.submitCuffReading({
      systolic: 120,
      diastolic: 80,
      sendMalformed: null,
    } as never);
    expect(sent.transport).toBe('mllp');
    expect(sent.framing).toBe('standard');
  });

  it('refuses a body that is not an object with 400', async () => {
    const c = await client();
    for (const body of [null, [], 'x']) {
      const e = await refusal(c, body);
      expect([e.status, e.reason, e.message]).toEqual([
        400,
        'invalid-body',
        'body: must be a JSON object',
      ]);
    }
  });

  it("rate limit: 429 with cuff-sim's text", async () => {
    const c = await client({ rateLimit: { burst: 1, perMinute: 6 } });
    await c.submitCuffReading(reading());
    const e = await refusal(c, reading());
    expect(issues(e)).toEqual([['body', 'too many sends; try again later', 'throttled']]);
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

  it('derives the same names as cuff-sim for fixed sample bytes', async () => {
    // Vectors computed by cuff-sim's derived_message_name over T07's manifest bytes. On a
    // deployment cuff-sim (T33) substitutes the configured patient into samples with a usable
    // PID, so a real name is over the bytes actually sent; these two samples are never changed.
    const c = await client();
    const vectors: [string, string][] = [
      ['unmappable_msh_only', 'wi1Blno9Nl-URg3wpycXvp6NuzBMZv5q3SL_idAs_l0='],
      ['invalid_missing_pid3', 'EuH8LonYjPPSSR8TOmtxfqoKCy69KUcDnjZt2LHnu2I='],
      // Manifest bytes only: on a deployment its PID carries the configured patient.
      ['unmappable_unknown_obx3_code', '0eF13P7qGXwhLzcUU4JK6C-47fFcIVDBAn8Tw60Q0J4='],
    ];
    for (const [name, id] of vectors) {
      const sent = await c.submitCuffReading({ transport: 'mllp', sendMalformed: name });
      expect(sent.result, name).toBe('accepted');
      expect(sent.messageName, name).toBe(`${MOCK_HL7V2_STORE}/messages/${id}`);
    }
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
  it('build failure: the send happened, attemptId is null, no attempt row, its message is store-only', async () => {
    const c = await client({ auditFailure: 'build' });
    const rows = async () =>
      (await c.listMessages({ kind: 'cuff-send', count: 50 })).entries as SendAttemptEntry[];
    const before = await rows();
    const sent = await c.submitCuffReading(reading());
    expect(sent.attemptId).toBeNull();
    expect(sent.audit).toMatchObject({ recorded: false });
    expect(sent.ack).toMatchObject({ received: true, code: 'AA' });
    const after = await rows();
    // No attempt row (no AuditEvent), but the stored message nobody claims is listed as a
    // store-only row (amendment 5), by its name, with no hint (no attempt qualifies).
    expect(after.filter((e) => e.source === 'audit-event')).toHaveLength(
      before.filter((e) => e.source === 'audit-event').length,
    );
    const added = after.filter((e) => !before.some((b) => b.id === e.id));
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({
      source: 'store-only',
      id: sent.messageName,
      transport: null,
      controlId: sent.controlId,
      messageLookup: 'confirmed',
      possibleDuplicateOf: null,
    });
  });

  it('write failure: an id exists, but no AuditEvent is stored under it', async () => {
    const c = await client({ auditFailure: 'write' });
    const sent = await c.submitCuffReading(reading());
    expect(sent.attemptId).not.toBeNull();
    expect(sent.audit.recorded).toBe(false);
    // cuff-sim's own sentence (audit.py), with the store's HTTP status.
    expect(!sent.audit.recorded && sent.audit.detail).toBe(
      'the audit store write failed (HTTP 503)',
    );
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
      onUnauthorized: () => undefined,
      now: () => now,
      latencyMs: 0,
    });
    await c.startSession('mock-recaptcha-test');
    for (let i = 0; i < 3; i++) await c.submitCuffReading(reading());
    expect((await refusal(c, reading())).status).toBe(429);
  });
});
