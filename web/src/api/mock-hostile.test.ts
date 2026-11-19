/**
 * The mock's test switches for browser tests: the `hostile` data set (markup in text from other
 * systems) and `?mock-audit=build|write`. Mock builds only; the bundle check proves neither is in
 * the HTTP build. Synthetic values only.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import type { CuffReadingRequest } from './contract';
import { CuffSendError } from './errors';
import { MockClient } from './mock-client';
import {
  HOSTILE_MARKUP,
  MOCK_DATA_SETS,
  MOCK_SAMPLES,
  mockAuditFailureFromSearch,
  mockDataSetFromSearch,
} from './mock-data';

const NOW = Date.parse('2026-03-10T15:02:00Z');

async function client(hostile: boolean): Promise<MockClient> {
  const c = new MockClient({
    onUnauthorized: () => undefined,
    now: () => NOW,
    latencyMs: 0,
    rateLimit: null,
    dataSet: hostile ? MOCK_DATA_SETS.hostile : MOCK_DATA_SETS.utc,
  });
  await c.startSession('mock-recaptcha-test');
  return c;
}

const sample = (name: string, transport: 'mllp' | 'https' = 'mllp'): CuffReadingRequest => ({
  transport,
  sendMalformed: name,
});

beforeEach(() => {});

describe('?mock-audit= and ?mock-data=hostile', () => {
  it('reads the audit switch, and nothing else', () => {
    expect(mockAuditFailureFromSearch('?mock-audit=build')).toBe('build');
    expect(mockAuditFailureFromSearch('?mock-data=hostile&mock-audit=write')).toBe('write');
    expect(mockAuditFailureFromSearch('?mock-audit=everything')).toBeNull();
    expect(mockAuditFailureFromSearch('')).toBeNull();
    expect(mockDataSetFromSearch('?mock-data=hostile').hostile).toBe(true);
    expect(mockDataSetFromSearch('?mock-data=utc').hostile).toBe(false);
  });
});

describe('the hostile data set', () => {
  it('puts markup into each sample description', async () => {
    const samples = await (await client(true)).listMalformedSamples();
    expect(samples.length).toBe(MOCK_SAMPLES.length);
    for (const s of samples) expect(s.label.endsWith(` ${HOSTILE_MARKUP}`), s.name).toBe(true);
  });

  it('puts markup into an AE reason (MSA-3) and the raw ACK', async () => {
    const r = await (await client(true)).submitCuffReading(sample('unparseable_no_msh'));
    expect(r.ack.received && r.ack.reason).toBe(`Invalid MSH header ${HOSTILE_MARKUP}`);
    expect(r.ack.received && r.ack.raw).toContain(HOSTILE_MARKUP);
  });

  it("adds an NTE segment with markup to a sample, with the sample's own terminator", async () => {
    const c = await client(true);
    const cr = await c.submitCuffReading(sample('unmappable_missing_obx14'));
    expect(cr.messageSent.endsWith(`\rNTE|1||${HOSTILE_MARKUP}\r`)).toBe(true);
    const lf = await c.submitCuffReading(sample('unparseable_lf_terminators'));
    expect(lf.messageSent.endsWith(`\nNTE|1||${HOSTILE_MARKUP}\n`)).toBe(true);
  });

  it('refuses systolic 333 with markup in the diagnostics', async () => {
    const error = await (
      await client(true)
    )
      .submitCuffReading({ systolic: 333, diastolic: 80, transport: 'mllp', sendMalformed: null })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CuffSendError);
    expect((error as CuffSendError).status).toBe(422);
    expect((error as CuffSendError).fields).toEqual(['systolic']);
    expect((error as CuffSendError).message).toBe(
      `systolic: is refused by the hostile data set ${HOSTILE_MARKUP}`,
    );
  });

  it('changes nothing in the default data set', async () => {
    const c = await client(false);
    expect((await c.listMalformedSamples()).some((s) => s.label.includes('<'))).toBe(false);
    const r = await c.submitCuffReading(sample('unparseable_no_msh'));
    expect(r.ack.received && r.ack.reason).toBe('Invalid MSH header');
    expect(r.messageSent).not.toContain('NTE|');
    await expect(
      c.submitCuffReading({ systolic: 333, diastolic: 80, transport: 'mllp', sendMalformed: null }),
    ).resolves.toMatchObject({ result: 'accepted' });
  });
});
