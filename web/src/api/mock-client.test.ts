import { beforeEach, describe, expect, it, vi } from 'vitest';
// The real shapes the mock must imitate: T06's example resources (synthetic, in the repo).
import heartRateExample from '../../../libs/pulsebridge_fhir/examples/observation-heart-rate.json';
import bloodPressureExample from '../../../libs/pulsebridge_fhir/examples/observation-blood-pressure.json';
import type { SendAttemptEntry } from './contract';
import { ApiError, CuffSendError, LoginError, UnauthorizedError } from './errors';
import { LOINC, type Observation } from './fhir';
import { MockClient } from './mock-client';
import { mockControlId, mockOruR01 } from './mock-data';
import samplesManifest from '../../../libs/pulsebridge_hl7v2/samples/samples.json';
import { Session } from './session';

const EXAMPLES: Record<string, Observation> = {
  'observation-heart-rate.json': heartRateExample as Observation,
  'observation-blood-pressure.json': bloodPressureExample as Observation,
};
const example = (name: string): Observation => {
  const found = EXAMPLES[name];
  if (!found) throw new Error(`no example ${name}`);
  return found;
};

interface SourceSample {
  name: string;
  segments: string[];
  segment_terminator: string;
  terminate_last_segment: boolean;
}
const SAMPLES = (samplesManifest as { samples: SourceSample[] }).samples;

const NOW = Date.parse('2026-03-10T15:02:00Z');
const DAY = 24 * 60 * 60 * 1000;

/** Element names present in an object, recursively, as dotted paths (arrays use their first item). */
function shape(value: unknown, prefix = ''): string[] {
  if (Array.isArray(value)) return shape(value[0], prefix);
  if (typeof value !== 'object' || value === null) return [prefix];
  return Object.entries(value).flatMap(([k, v]) => shape(v, prefix ? `${prefix}.${k}` : k));
}

describe('MockClient', () => {
  let session: Session;
  let onUnauthorized: ReturnType<typeof vi.fn<() => void>>;
  let now: number;
  let client: MockClient;

  beforeEach(async () => {
    sessionStorage.clear();
    now = NOW;
    // The session checks token expiry against the same fixed clock the mock uses.
    session = new Session(sessionStorage, () => now);
    onUnauthorized = vi.fn<() => void>();
    client = new MockClient({ session, onUnauthorized, now: () => now, latencyMs: 0 });
    await client.login('any password');
  });

  describe('login', () => {
    it('accepts any non-empty password and stores a token in sessionStorage', () => {
      expect(client.isLoggedIn()).toBe(true);
      expect(session.token()).toMatch(/^mock-session-/);
    });

    it('rejects an empty password', async () => {
      client.logout();
      await expect(client.login('')).rejects.toBeInstanceOf(LoginError);
      expect(client.isLoggedIn()).toBe(false);
    });

    it('answers like a 401 when logged out: clears the session and calls onUnauthorized', async () => {
      client.logout();
      await expect(client.getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).toHaveBeenCalledOnce();
    });
  });

  describe('heart-rate Observations', () => {
    it('have exactly the element structure of the T06 heart-rate example', async () => {
      const [obs] = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(NOW - DAY), end: new Date(NOW) },
      });
      const reference = example('observation-heart-rate.json');
      // `id` is assigned by the FHIR store, so the stored resource has it and the example does not.
      expect(
        shape(obs)
          .filter((p) => p !== 'id')
          .sort(),
      ).toEqual(shape(reference).sort());
      // Same codes and units as the example: LOINC 8867-4, UCUM /min, vital-signs category,
      // the same min/max component codes.
      expect(obs?.code).toEqual(reference.code);
      expect(obs?.category).toEqual(reference.category);
      expect(obs?.valueQuantity?.code).toBe('/min');
      expect(obs?.valueQuantity?.system).toBe('http://unitsofmeasure.org');
      expect(obs?.component?.map((c) => c.code)).toEqual(reference.component?.map((c) => c.code));
      expect(obs?.device?.reference).toMatch(/^Device\//);
      // T06 (T33): self-measured, the performer is the subject Patient.
      expect(obs?.performer).toEqual([obs?.subject]);
    });

    it('are five-minute roll-ups (D8): about 288 a day, with min <= mean <= max', async () => {
      const day = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(NOW - DAY), end: new Date(NOW) },
      });
      // 288 windows a day minus the synthetic charging gap.
      expect(day.length).toBeGreaterThan(270);
      expect(day.length).toBeLessThanOrEqual(288);
      for (const o of day) {
        const start = Date.parse(o.effectivePeriod?.start ?? '');
        const end = Date.parse(o.effectivePeriod?.end ?? '');
        expect(end - start).toBe(5 * 60 * 1000);
        const [min, max] = (o.component ?? []).map((c) => c.valueQuantity?.value ?? NaN);
        const mean = o.valueQuantity?.value ?? NaN;
        expect(min).toBeLessThanOrEqual(mean);
        expect(mean).toBeLessThanOrEqual(max ?? NaN);
        expect(end).toBeLessThanOrEqual(NOW);
      }
      // Oldest first, and the newest windows are still preliminary (T10's cutoff).
      expect(day.map((o) => o.effectivePeriod?.start)).toEqual(
        [...day].map((o) => o.effectivePeriod?.start).sort(),
      );
      expect(day.at(-1)?.status).toBe('preliminary');
      expect(day[0]?.status).toBe('final');
    });

    it('follow the gt/lt Period rule: a window ending exactly at the start is left out', async () => {
      const start = Date.parse('2026-03-09T00:00:00Z'); // aligned to a window boundary
      const end = start + 30 * 60 * 1000;
      const windows = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(start), end: new Date(end) },
      });
      expect(windows.map((o) => o.effectivePeriod?.start)).toEqual(
        [0, 5, 10, 15, 20, 25].map((m) => new Date(start + m * 60_000).toISOString()),
      );
      // An unaligned start includes the window it falls inside (its period overlaps).
      const inside = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(start + 60_000), end: new Date(end) },
      });
      expect(inside[0]?.effectivePeriod?.start).toBe(new Date(start).toISOString());
    });

    it('are deterministic: the same window always has the same value', async () => {
      const range = { start: new Date(NOW - DAY), end: new Date(NOW) };
      const a = await client.searchObservations({ code: LOINC.heartRate, range });
      const b = await client.searchObservations({ code: LOINC.heartRate, range });
      expect(a).toEqual(b);
    });

    it('give a trace with the Google Health roll-up window the Observation came from', async () => {
      const [obs] = await client.searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date(NOW - DAY), end: new Date(NOW) },
      });
      const trace = await client.getHeartRateTrace(obs?.id ?? '');
      expect(trace.observation).toEqual(obs);
      // The proxy will rebuild the window from the stored reading and say so (open question 2).
      expect(trace.source?.kind).toBe('reconstructed');
      expect(trace.source?.window.startTime).toBe(obs?.effectivePeriod?.start);
      expect(trace.source?.window.heartRate.beatsPerMinuteAvg).toBe(obs?.valueQuantity?.value);
      expect(trace.provenance?.target[0]?.reference).toBe(`Observation/${obs?.id ?? ''}`);
    });
  });

  describe('blood-pressure Observations', () => {
    it('have exactly the element structure of the T06 blood-pressure example', async () => {
      const [bp] = await client.searchObservations({
        code: LOINC.bloodPressurePanel,
        range: { start: new Date(NOW - DAY), end: new Date(NOW) },
      });
      const reference = example('observation-blood-pressure.json');
      expect(
        shape(bp)
          .filter((p) => p !== 'id')
          .sort(),
      ).toEqual(shape(reference).sort());
      expect(bp?.code).toEqual(reference.code);
      expect(bp?.performer).toEqual([bp?.subject]);
      expect(bp?.component?.map((c) => [c.code, c.valueQuantity?.code])).toEqual(
        reference.component?.map((c) => [c.code, 'mm[Hg]']),
      );
    });
  });

  describe('messages log', () => {
    it('covers every outcome the log must show (D7, D12)', async () => {
      const { entries } = await client.listMessages({ kind: 'cuff-send' });
      const sends = entries as SendAttemptEntry[];
      const ack = (e: SendAttemptEntry) => (e.commitAck.received ? e.commitAck.code : 'none');

      expect(sends.some((e) => ack(e) === 'AA' && e.application?.outcome === 'accepted')).toBe(
        true,
      );
      expect(sends.some((e) => ack(e) === 'AA' && e.application?.outcome === 'pending')).toBe(true);
      const mappingError = sends.find((e) => e.application?.outcome === 'error');
      expect(mappingError?.application?.reason).toBe('missing-obx-14');
      const nack = sends.find((e) => ack(e) === 'AE');
      expect(nack?.commitAck.received && nack.commitAck.reason).toBe('Invalid MSH header');
      expect(nack?.application).toBeNull();
      const whys = sends.flatMap((e) => (e.commitAck.received ? [] : [e.commitAck.why]));
      expect(whys).toEqual(expect.arrayContaining(['timeout', 'api-error', 'unreadable-reply']));
      // T31's closed set of no-acknowledgment reasons, and an HTTP status only with api-error.
      for (const e of sends) {
        if (e.commitAck.received) continue;
        expect(['timeout', 'connection-closed', 'api-error', 'unreadable-reply']).toContain(
          e.commitAck.why,
        );
        expect(e.commitAck.httpStatus !== null).toBe(e.commitAck.why === 'api-error');
      }
      // result and outcome follow T31's mapping.
      for (const e of sends) {
        const expected = !e.commitAck.received
          ? ['no-acknowledgment', '8']
          : e.commitAck.code === 'AA'
            ? ['accepted', '0']
            : ['rejected', '4'];
        expect([e.result, e.outcome]).toEqual(expected);
      }
      expect(new Set(sends.map((e) => e.transport))).toEqual(new Set(['mllp', 'https']));
    });

    it('lists newest first, mixes in FHIR transactions, and filters by outcome', async () => {
      const all = await client.listMessages();
      const times = all.entries.map((e) => e.recordedAt ?? '');
      expect(times).toEqual([...times].sort().reverse());
      expect(all.entries.some((e) => e.kind === 'fhir-transaction')).toBe(true);
      // An unreadable AuditEvent is a visible row and counted, never dropped.
      const unreadable = all.entries.filter((e) => e.kind === 'unreadable-send-attempt');
      expect(unreadable).toHaveLength(1);
      expect(all.unreadable).toBe(1);
      expect(unreadable[0]?.kind === 'unreadable-send-attempt' && unreadable[0].problem).toMatch(
        /format version/,
      );

      const errors = await client.listMessages({ outcome: 'error' });
      expect(errors.entries.length).toBeGreaterThan(0);
      for (const e of errors.entries) {
        expect(e.kind === 'cuff-send' && e.application?.outcome).toBe('error');
      }
    });
  });

  describe('cuff readings', () => {
    it('rejects values outside bounds with 422 and an OperationOutcome; nothing is logged', async () => {
      const before = (await client.listMessages({ kind: 'cuff-send' })).entries.length;
      const error = (await client
        .submitCuffReading({ systolic: 401, diastolic: 80, transport: 'mllp', sendMalformed: null })
        .catch((e: unknown) => e)) as CuffSendError;
      expect(error).toBeInstanceOf(CuffSendError);
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(422);
      expect(error.reason).toBe('invalid-input');
      expect(error.fields).toEqual(['systolic']);
      expect(error.outcome?.resourceType).toBe('OperationOutcome');
      expect((await client.listMessages({ kind: 'cuff-send' })).entries).toHaveLength(before);
    });

    it('returns AA, then the stored message goes from pending to accepted with an Observation', async () => {
      const result = await client.submitCuffReading({
        systolic: 131,
        diastolic: 84,
        transport: 'https',
        sendMalformed: null,
      });
      expect(result.ack.received && result.ack.code).toBe('AA');
      expect(result.messageSent).toContain('OBX|1|NM|8480-6');
      expect(result.messageName).not.toBeNull();

      let trace = await client.getCuffAttemptTrace(result.attemptId ?? '');
      expect(trace.attempt.application?.outcome).toBe('pending');
      expect(trace.observation).toBeNull();

      now += 5000;
      trace = await client.getCuffAttemptTrace(result.attemptId ?? '');
      expect(trace.attempt.application?.outcome).toBe('accepted');
      expect(trace.observation?.component?.[0]?.valueQuantity?.value).toBe(131);
    });

    it('sends a malformed sample only over a transport it applies to', async () => {
      const samples = await client.listMalformedSamples();
      const framing = samples.find((s) => s.class === 'no_acknowledgment');
      expect(framing?.transports).toEqual(['mllp']);
      await expect(
        client.submitCuffReading({
          systolic: 120,
          diastolic: 80,
          transport: 'https',
          sendMalformed: framing?.name ?? '',
        }),
      ).rejects.toBeInstanceOf(ApiError);
      const sent = await client.submitCuffReading({
        systolic: 120,
        diastolic: 80,
        transport: 'mllp',
        sendMalformed: framing?.name ?? '',
      });
      expect(sent.ack).toMatchObject({ received: false, why: 'timeout' });
    });

    it('shows a malformed sample exactly as T07 defines it, not a message built from the inputs', async () => {
      for (const name of ['unparseable_lf_terminators', 'unmappable_non_numeric_obx5']) {
        const source = SAMPLES.find((x) => x.name === name);
        if (!source) throw new Error(`no source sample ${name}`);
        const sent = await client.submitCuffReading({
          systolic: 140,
          diastolic: 90,
          transport: 'mllp',
          sendMalformed: name,
        });
        const expected =
          source.segments.join(source.segment_terminator) +
          (source.terminate_last_segment ? source.segment_terminator : '');
        expect(sent.messageSent).toBe(expected);
        expect(sent.messageSent).not.toContain('||140|');
        // The sample's own MSH-10, as T31 would record it.
        expect(sent.controlId).toBe(source.segments[0]?.split('|')[9]);
      }
    });

    it('a sample with no MSH has no control ID', async () => {
      const sent = await client.submitCuffReading({
        systolic: 120,
        diastolic: 80,
        transport: 'https',
        sendMalformed: 'unparseable_no_msh',
      });
      expect(sent.controlId).toBeNull();
      expect(sent.ack).toMatchObject({ received: true, code: 'AE', reason: 'Invalid MSH header' });
    });

    it('rejects with an AbortError, sending nothing, when the signal is already aborted', async () => {
      const before = (await client.listMessages({ kind: 'cuff-send' })).entries.length;
      const controller = new AbortController();
      controller.abort();
      await expect(
        client.submitCuffReading(
          { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null },
          { signal: controller.signal },
        ),
      ).rejects.toMatchObject({ name: 'AbortError' });
      expect((await client.listMessages({ kind: 'cuff-send' })).entries).toHaveLength(before);
    });
  });

  describe('built message (mirrors T07)', () => {
    it("is byte for byte T07's valid_bp_reading sample for the same inputs", () => {
      const source = SAMPLES.find((x) => x.name === 'valid_bp_reading');
      if (!source) throw new Error('no valid sample');
      const expected = source.segments.join('\r') + '\r';
      expect(mockOruR01('SYN-VALID-0001', Date.parse('2026-11-13T20:53:29Z'), 120, 80)).toBe(
        expected,
      );
    });

    it('uses a 20-character upper-case hex control ID like T07', () => {
      expect(mockControlId('any seed')).toMatch(/^[0-9A-F]{20}$/);
    });
  });

  describe('share', () => {
    it('shares a final Observation, then reports the same one as already shared', async () => {
      const range = { start: new Date(NOW - DAY), end: new Date(NOW) };
      const [obs] = await client.searchObservations({ code: LOINC.heartRate, range });
      const ref = `Observation/${obs?.id ?? ''}`;

      const first = await client.share({ observations: [ref] });
      expect(first.outcome).toBe('shared');
      expect(first.readBack.observations).toHaveLength(1);
      expect(first.readBack.patient?.name?.[0]?.use).toBe('official');
      // The trace never carries the assertion or the access token (T15).
      expect(JSON.stringify(first.steps)).not.toMatch(/eyJ|mock-session/);

      const second = await client.share({ observations: [ref] });
      expect(second.outcome).toBe('already-shared');
    });

    it('sends the Observation to OpenEMR without device, performer, meta, identifier or id (T06)', async () => {
      const range = { start: new Date(NOW - DAY), end: new Date(NOW) };
      const [obs] = await client.searchObservations({ code: LOINC.heartRate, range });
      const result = await client.share({ observations: [`Observation/${obs?.id ?? ''}`] });
      const sent = result.steps.find((st) => st.step === 'observation-create')?.request
        .body as Record<string, unknown>;
      expect(obs).toHaveProperty('performer');
      // performer names our Patient; OpenEMR does not keep it (spike S1), so it is dropped.
      for (const element of ['device', 'performer', 'meta', 'identifier', 'id']) {
        expect(sent, element).not.toHaveProperty(element);
      }
      expect(sent).toHaveProperty('subject');
      expect(sent).toHaveProperty('encounter');
    });

    it('refuses a preliminary Observation', async () => {
      const range = { start: new Date(NOW - DAY), end: new Date(NOW) };
      const day = await client.searchObservations({ code: LOINC.heartRate, range });
      const latest = day.at(-1);
      expect(latest?.status).toBe('preliminary');
      await expect(
        client.share({ observations: [`Observation/${latest?.id ?? ''}`] }),
      ).rejects.toMatchObject({ status: 422 });
    });
  });
});
