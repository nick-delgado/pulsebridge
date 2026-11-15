/**
 * The mock share walks the sequence specified for T15 (contract.ts `ShareStepName`; spike S1),
 * redacts its trace as T14's `extensions.trace_step` does, and reaches every failure the page
 * must show. Synthetic data only.
 */
import { describe, expect, it } from 'vitest';
import { SHARE_FHIR_STEP_SECRETS, SHARE_TOKEN_STEP_SECRETS } from './contract';
import { ShareError } from './errors';
import { LOINC, type Observation } from './fhir';
import { MockClient, redact, type MockClientOptions } from './mock-client';
import { Session } from './session';

const NOW = Date.parse('2026-03-10T15:02:00Z');
const DAY = 86_400_000;

async function setup(options: Partial<MockClientOptions> = {}) {
  sessionStorage.clear();
  const c = new MockClient({
    session: new Session(sessionStorage, () => NOW),
    onUnauthorized: () => undefined,
    now: () => NOW,
    latencyMs: 0,
    ...options,
  });
  await c.login('x');
  const range = { start: new Date(NOW - DAY), end: new Date(NOW) };
  const hr = await c.searchObservations({ code: LOINC.heartRate, range });
  const final = hr.filter((o) => o.status === 'final');
  const bp = await c.searchObservations({ code: LOINC.bloodPressurePanel, range });
  return { c, final, preliminary: hr.filter((o) => o.status === 'preliminary'), bp };
}

const idOf = (o: Observation | undefined) => o?.id ?? '';

async function shareError(p: Promise<unknown>): Promise<ShareError> {
  const e: unknown = await p.catch((x: unknown) => x);
  if (!(e instanceof ShareError)) throw new Error(`expected a ShareError, got ${String(e)}`);
  return e;
}

describe('mock share: the sequence', () => {
  it('first share: token, Patient not found and created, Encounter created, Observation, three reads', async () => {
    const { c, final } = await setup();
    const r = await c.share({ observationId: idOf(final[0]) });
    expect(r.outcome).toBe('shared');
    expect(r.steps.map((s) => `${s.step}:${s.result}`)).toEqual([
      'token:ok',
      'patient-search:not-found',
      'patient-create:ok',
      'encounter-search:not-found',
      'encounter-create:ok',
      'observation-create:ok',
      'patient-read:ok',
      'encounter-read:ok',
      'observation-read:ok',
    ]);
    for (const s of r.steps) expect(s.request.url).toMatch(/^\/[^/?]/);
    expect(r.readBack.patient.sent).not.toBeNull();
    expect(r.readBack.encounter.sent).not.toBeNull();
  });

  it('the same reading again: Patient found, Encounter reused, refused as a repeat: already shared', async () => {
    const { c, final } = await setup();
    const first = await c.share({ observationId: idOf(final[0]) });
    const again = await c.share({ observationId: idOf(final[0]) });
    expect(again.outcome).toBe('already-shared');
    expect(again.steps.map((s) => `${s.step}:${s.result}`)).toEqual([
      // The first share's token is still valid: reused, no token request (as the proxy).
      'token:reused',
      'patient-search:found',
      'encounter-search:found',
      'observation-create:already-shared',
      'patient-read:ok',
      'encounter-read:ok',
      'observation-read:ok',
    ]);
    // No Patient or Encounter sent again; the stored reading is the one OpenEMR already held.
    expect(again.readBack.patient.sent).toBeNull();
    expect(again.readBack.encounter.sent).toBeNull();
    expect(again.readBack.observation.stored?.id).toBe(first.readBack.observation.stored?.id);
    expect(again.steps.find((s) => s.step === 'observation-create')?.response.status).toBe(400);
  });

  it('another reading of the same day reuses the Encounter and is shared', async () => {
    const { c, final } = await setup();
    await c.share({ observationId: idOf(final[0]) });
    const other = await c.share({ observationId: idOf(final[1]) });
    expect(other.outcome).toBe('shared');
    expect(other.steps.find((s) => s.step === 'encounter-search')?.result).toBe('found');
    expect(other.steps.some((s) => s.step === 'encounter-create')).toBe(false);
  });

  it('the Observation sent has no reference to our store (T06): no id, meta, identifier, device, performer', async () => {
    const { c, bp } = await setup();
    const obs = bp[0];
    expect(obs).toHaveProperty('performer');
    expect(obs).toHaveProperty('device');
    const r = await c.share({ observationId: idOf(obs) });
    const sent = r.readBack.observation.sent as unknown as Record<string, unknown>;
    for (const element of ['id', 'meta', 'identifier', 'device', 'performer']) {
      expect(sent, element).not.toHaveProperty(element);
    }
    expect(r.readBack.observation.sent?.subject?.reference).toMatch(/^Patient\/(?!mock-patient)/);
    expect(r.readBack.observation.sent?.encounter?.reference).toMatch(/^Encounter\//);
    // The Patient sent is patient_for_sharing's: four elements only.
    expect(Object.keys(r.readBack.patient.sent ?? {}).sort()).toEqual([
      'birthDate',
      'gender',
      'identifier',
      'name',
      'resourceType',
    ]);
  });

  it('OpenEMR stores something different from what was sent (a heart rate loses its components)', async () => {
    const { c, final } = await setup();
    const r = await c.share({ observationId: idOf(final[0]) });
    expect(r.readBack.observation.sent).toHaveProperty('component');
    expect(r.readBack.observation.stored).not.toHaveProperty('component');
    expect(r.readBack.observation.stored).toHaveProperty('meta');
  });
});

describe('mock share: the trace hides credentials as trace_step does', () => {
  it('no assertion or access token in any step; the token step shows "[not shown]"', async () => {
    const { c, final } = await setup();
    const r = await c.share({ observationId: idOf(final[0]) });
    const text = JSON.stringify(r.steps);
    expect(text).not.toContain('mock-assertion');
    expect(text).not.toContain('mock-access-token');
    const token = r.steps[0];
    expect(token?.request.body).toMatchObject({ client_assertion: '[not shown]' });
    expect(token?.response.body).toMatchObject({ access_token: '[not shown]' });
  });

  it('redact: at any depth, without regard to case, only the names it is given', () => {
    expect(
      redact({ a: [{ Client_Assertion: 'x', code: 'y', nested: { TOKEN: 'z', keep: 1 } }] }, [
        'client_assertion',
        'token',
      ]),
    ).toEqual({
      a: [
        { Client_Assertion: '[not shown]', code: 'y', nested: { TOKEN: '[not shown]', keep: 1 } },
      ],
    });
  });

  it('the final rule: FHIR codes stay visible in FHIR steps; the token step hides OAuth codes and tokens', async () => {
    const { c, final } = await setup();
    const r = await c.share({ observationId: idOf(final[0]) });
    const create = r.steps.find((s) => s.step === 'observation-create');
    const body = create?.request.body as { code?: { coding?: { code?: string }[] } };
    expect(body.code?.coding?.[0]?.code).toBe('8867-4');
    expect(JSON.stringify(r.steps.slice(1))).not.toContain('[not shown]');
    // In the token step even a generic `code` or `token` is hidden.
    expect(SHARE_TOKEN_STEP_SECRETS).toContain('code');
    expect(SHARE_FHIR_STEP_SECRETS).not.toContain('code');
    expect(SHARE_FHIR_STEP_SECRETS).not.toContain('token');
  });

  it('search steps carry their parameters in query; the url stays a path and the body null', async () => {
    const { c, final } = await setup();
    const r = await c.share({ observationId: idOf(final[0]) });
    const patient = r.steps.find((s) => s.step === 'patient-search');
    expect(patient?.request).toMatchObject({
      url: '/apis/default/fhir/Patient',
      query: { identifier: 'SYN-000123' },
      body: null,
    });
    const encounter = r.steps.find((s) => s.step === 'encounter-search');
    // The UTC calendar day of the reading's start (the day rule in web/README.md).
    const day = new Date(Date.parse(final[0]?.effectivePeriod?.start ?? ''))
      .toISOString()
      .slice(0, 10);
    expect(encounter?.request.query).toMatchObject({ date: day });
    expect(r.steps.find((s) => s.step === 'observation-create')?.request.query).toBeNull();
  });
});

describe('mock share: refusals and failures', () => {
  it('a preliminary reading is a 422 observation-not-final, before OpenEMR is called', async () => {
    const { c, preliminary } = await setup();
    const e = await shareError(c.share({ observationId: idOf(preliminary.at(-1)) }));
    expect([e.status, e.reason, e.steps]).toEqual([422, 'observation-not-final', []]);
  });

  it('an unknown reading is a 404; a body with more than the id is a 400', async () => {
    const { c } = await setup();
    expect((await shareError(c.share({ observationId: 'nothing' }))).reason).toBe('not-found');
    const extra = { observationId: 'x', patient: 'Patient/1' } as unknown as {
      observationId: string;
    };
    expect((await shareError(c.share(extra))).status).toBe(400);
  });

  it.each([
    ['patient-incomplete', 422, 'patient-incomplete', []],
    ['not-configured', 503, 'share-not-configured', []],
    ['openemr-unreachable', 502, 'openemr-unreachable', ['token']],
    [
      'encounter-refused',
      502,
      'openemr-refused',
      ['token', 'patient-search', 'patient-create', 'encounter-search', 'encounter-create'],
    ],
    [
      'observation-timeout',
      504,
      'openemr-timeout',
      [
        'token',
        'patient-search',
        'patient-create',
        'encounter-search',
        'encounter-create',
        'observation-create',
      ],
    ],
  ] as const)(
    '?mock-share=%s: %s %s with the steps so far',
    async (failure, status, reason, steps) => {
      const { c, final } = await setup({ shareFailure: failure });
      const e = await shareError(c.share({ observationId: idOf(final[0]) }));
      expect([e.status, e.reason]).toEqual([status, reason]);
      expect(e.steps.map((s) => s.step)).toEqual(steps);
      if (steps.length > 0) expect(e.steps.at(-1)?.result).toBe('failed');
    },
  );

  it('?mock-share=hosting-timeout: an HTML-like 504 with no steps after the clinic was changed', async () => {
    const { c, final } = await setup({ shareFailure: 'hosting-timeout' });
    const e = await shareError(c.share({ observationId: idOf(final[0]) }));
    expect([e.status, e.outcome, e.steps, e.nothingSent]).toEqual([504, null, [], false]);
  });

  it('?mock-share=read-back: shared, but the Observation read-back failed', async () => {
    const { c, final } = await setup({ shareFailure: 'read-back' });
    const r = await c.share({ observationId: idOf(final[0]) });
    expect(r.outcome).toBe('shared');
    expect(r.readBack.observation.stored).toBeNull();
    expect(r.steps.at(-1)).toMatchObject({ step: 'observation-read', result: 'failed' });
  });
});

describe('mock share as T15 built it', () => {
  it('a heart-rate window is sent at its start, without a period', async () => {
    const { c, final } = await setup();
    const window = final.find((o) => o.effectivePeriod !== undefined);
    if (!window) throw new Error('no heart-rate window in the mock data');
    const result = await c.share({ observationId: idOf(window) });
    const sent = result.readBack.observation.sent;
    expect(sent?.effectiveDateTime).toBe(window.effectivePeriod?.start);
    expect(sent?.effectivePeriod).toBeUndefined();
  });
});
