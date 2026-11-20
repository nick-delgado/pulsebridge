/**
 * The mock messages log, status and read route behave as fhir-proxy as built
 * (services/fhir-proxy/README.md, "Contract amendments"; messages.py, status.py, app.py).
 * Synthetic data only.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { MessagesLogEntry, MessagesLogQuery, SendAttemptEntry } from './contract';
import { MESSAGES_DEFAULT_COUNT, MESSAGES_MAX_COUNT } from './contract';
import { ApiError, proxyReason } from './errors';
import { LOINC } from './fhir';
import { MockClient, type MockClientOptions } from './mock-client';

const NOW = Date.parse('2026-03-10T15:02:00Z');

async function client(options: Partial<MockClientOptions> = {}) {
  const c = new MockClient({
    onUnauthorized: () => undefined,
    now: () => NOW,
    latencyMs: 0,
    rateLimit: null,
    mappingDelayMs: 0,
    ...options,
  });
  await c.startSession('mock-recaptcha-test');
  return { c };
}

/** Every row of the log, following nextCursor until it is null (bounded). */
async function allRows(c: MockClient, query: MessagesLogQuery = {}) {
  const rows: MessagesLogEntry[] = [];
  const pages: number[] = [];
  let cursor: string | undefined;
  for (let i = 0; i < 20; i++) {
    const page = await c.listMessages({ ...query, ...(cursor ? { cursor } : {}) });
    rows.push(...page.entries);
    pages.push(page.entries.length);
    if (page.nextCursor === null) return { rows, pages };
    cursor = page.nextCursor;
  }
  throw new Error('more than 20 pages');
}

const sends = (rows: MessagesLogEntry[]) =>
  rows.filter((r): r is SendAttemptEntry => r.kind === 'cuff-send');
const attempts = (rows: MessagesLogEntry[]) =>
  sends(rows).filter((r) => r.source === 'audit-event');

/** The time a row sorts on (amendment 17). */
function sortTime(r: MessagesLogEntry): string | null {
  if (r.kind === 'cuff-send') return r.source === 'store-only' ? r.recordedAt : r.sentAt;
  return r.recordedAt;
}

async function errorOf(p: Promise<unknown>): Promise<ApiError> {
  const e: unknown = await p.catch((x: unknown) => x);
  if (!(e instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(e)}`);
  return e;
}

describe('mock messages log: rows (amendments 1, 2, 5, 6, 14, 18)', () => {
  let rows: MessagesLogEntry[];
  beforeEach(async () => {
    const { c } = await client();
    rows = (await allRows(c)).rows;
  });

  it('every commit-level and application-level state the log must show (D7, D12)', () => {
    const ack = (e: SendAttemptEntry) => (e.commitAck.received ? e.commitAck.code : 'none');
    const a = attempts(rows);
    expect(a.some((e) => ack(e) === 'AA' && e.application?.outcome === 'accepted')).toBe(true);
    expect(a.some((e) => ack(e) === 'AA' && e.application?.outcome === 'pending')).toBe(true);
    expect(a.find((e) => e.application?.outcome === 'error')?.application?.reason).toBe(
      'missing-obx-14',
    );
    const nack = a.find((e) => ack(e) === 'AE');
    expect(nack?.commitAck.received && nack.commitAck.reason).toBe('Invalid MSH header');
    expect(nack).toMatchObject({ application: null, messageLookup: null });
    const whys = a.flatMap((e) => (e.commitAck.received ? [] : [e.commitAck.why]));
    expect(whys).toEqual(expect.arrayContaining(['timeout', 'api-error', 'unreadable-reply']));
    for (const e of a.filter((x) => x.result !== 'accepted')) expect(e.messageLookup).toBeNull();
    // As messages.py `commit_ack`: a row never carries MSA-2 (only the send result does).
    for (const e of a) if (e.commitAck.received) expect(e.commitAck.controlId).toBeNull();
  });

  it('an accepted MLLP send whose derived name the store does not have: no outcome, no Observation', () => {
    const notFound = attempts(rows).filter((e) => e.messageLookup === 'not-found');
    expect(notFound).toHaveLength(1);
    expect(notFound[0]).toMatchObject({
      result: 'accepted',
      transport: 'mllp',
      application: null,
      observation: null,
      possibleDuplicateOf: null,
    });
    expect(notFound[0]?.derivedMessageName).toMatch(/\/messages\//);
  });

  it('store-only rows: one hints at that attempt, one (sent to the adapter) has no hint', () => {
    const storeOnly = sends(rows).filter((e) => e.source === 'store-only');
    const attempt = attempts(rows).find((e) => e.messageLookup === 'not-found');
    const hinted = storeOnly.find((e) => e.possibleDuplicateOf !== null);
    expect(hinted).toMatchObject({
      possibleDuplicateOf: attempt?.id,
      controlId: attempt?.controlId,
      messageLookup: 'confirmed',
      transport: null,
      result: 'accepted',
    });
    // The message keeps its OWN outcome; the attempt gets none (the hint moves nothing).
    expect(hinted?.application?.outcome).toBe('accepted');
    expect(hinted?.observation).toMatch(/^Observation\//);
    expect(hinted?.id).toBe(hinted?.messageName);
    const plain = storeOnly.find((e) => e.controlId === 'ADAPTER-0001');
    expect(plain).toMatchObject({ possibleDuplicateOf: null, transport: null });
    // Only attempt rows from the audit store have an attempt id; store-only ids are names.
    for (const s of storeOnly) expect(s.id).toContain('/messages/');
  });

  it('unreadable rows: one with an id, one with id "" and no time, which sorts last', () => {
    const unreadable = rows.filter((r) => r.kind === 'unreadable-send-attempt');
    expect(unreadable.map((u) => u.id)).toEqual(['mock-unreadable-0001', '']);
    expect(rows.at(-1)).toMatchObject({
      kind: 'unreadable-send-attempt',
      id: '',
      recordedAt: null,
    });
  });

  it('transaction rows: four writers, httpStatus always null, only Observations and Provenances', () => {
    const tx = rows.filter((r) => r.kind === 'fhir-transaction');
    expect(new Set(tx.map((t) => t.writer))).toEqual(
      new Set(['health-sync', 'v2-to-fhir', 'seed', 'unknown']),
    );
    for (const t of tx) {
      expect(t.httpStatus).toBeNull();
      for (const ref of t.resources) expect(ref).toMatch(/^(Observation|Provenance)\//);
    }
  });

  it('newest first by send time, store-only rows by createTime, undated last (amendment 17)', () => {
    const dated = rows.filter((r) => sortTime(r) !== null);
    const times = dated.map((r) => Date.parse(sortTime(r) ?? ''));
    expect(times).toEqual([...times].sort((x, y) => y - x));
    expect(rows.slice(dated.length).every((r) => sortTime(r) === null)).toBe(true);
    // A row whose recordedAt is later than its sentAt still sorts by sentAt: the timeout seed
    // is recorded 10 s after it was sent.
    const late = attempts(rows).find((e) => !e.commitAck.received && e.commitAck.why === 'timeout');
    expect(Date.parse(late?.recordedAt ?? '') - Date.parse(late?.sentAt ?? '')).toBe(10_000);
  });
});

describe('mock messages log: paging (amendments 3, 4, 19, 21)', () => {
  it('pages of 25 by default, capped at 50; count below 1 is a 400', async () => {
    const { c } = await client();
    expect(MESSAGES_DEFAULT_COUNT).toBe(25);
    expect(MESSAGES_MAX_COUNT).toBe(50);
    const first = await c.listMessages();
    expect(first.entries).toHaveLength(25);
    expect(first.nextCursor).not.toBeNull();
    const big = await c.listMessages({ count: 500 });
    expect(big.entries.length).toBeLessThanOrEqual(50);
    const e = await errorOf(c.listMessages({ count: 0 }));
    expect([e.status, proxyReason(e.outcome)]).toEqual([400, 'unsupported-value']);
  });

  it('a filtered page can be short or empty while nextCursor is set; following it finds the rest', async () => {
    const { c } = await client();
    const { rows, pages } = await allRows(c, { outcome: 'error', count: 5 });
    expect(pages.some((n) => n < 5)).toBe(true);
    expect(pages.some((n) => n === 0)).toBe(true);
    expect(rows.length).toBeGreaterThan(0);
    for (const r of rows) expect(r.kind === 'cuff-send' && r.application?.outcome).toBe('error');
  });

  it('the outcome filter matches only rows WITH that outcome: never transactions or null outcomes', async () => {
    const { c } = await client();
    for (const outcome of ['accepted', 'error', 'pending'] as const) {
      const { rows } = await allRows(c, { outcome, count: 50 });
      for (const r of rows) {
        expect(r.kind).toBe('cuff-send');
        expect(r.kind === 'cuff-send' && r.application?.outcome).toBe(outcome);
      }
    }
    // The not-found attempt is pending as far as the mapper goes, but matches no filter.
    const pending = (await allRows(c, { outcome: 'pending', count: 50 })).rows;
    expect(sends(pending).some((r) => r.messageLookup === 'not-found')).toBe(false);
  });

  it('kind filters: each page holds only that kind', async () => {
    const { c } = await client();
    for (const kind of ['cuff-send', 'unreadable-send-attempt', 'fhir-transaction'] as const) {
      const { rows } = await allRows(c, { kind, count: 10 });
      expect(rows.length, kind).toBeGreaterThan(0);
      for (const r of rows) expect(r.kind).toBe(kind);
    }
  });

  it('a cursor the proxy did not write is a 400 invalid-cursor', async () => {
    const { c } = await client();
    for (const cursor of ['25', 'not a cursor', btoa('{"v":2,"o":1}')]) {
      const e = await errorOf(c.listMessages({ cursor }));
      expect([e.status, proxyReason(e.outcome)], cursor).toEqual([400, 'invalid-cursor']);
    }
  });
});

describe('mock messages log: lookups and partial pages (amendments 1, 16)', () => {
  it('?mock-log=partial: every page says partial', async () => {
    const { c } = await client({ partialLog: true });
    expect((await c.listMessages()).partial).toBe(true);
    const { c: plain } = await client();
    expect((await plain.listMessages()).partial).toBe(false);
  });

  it('?mock-lookup=failed: accepted rows are failed, with no outcome, and the page is partial', async () => {
    const { c } = await client({ lookupFailure: 'failed' });
    const page = await c.listMessages({ count: 50 });
    expect(page.partial).toBe(true);
    const accepted = attempts(page.entries).filter((e) => e.result === 'accepted');
    expect(accepted.length).toBeGreaterThan(0);
    for (const e of accepted.filter((x) => x.messageLookup !== 'not-found')) {
      expect(e).toMatchObject({ messageLookup: 'failed', application: null, observation: null });
    }
  });

  it('?mock-lookup=not-found: a new MLLP send is not found; its message is store-only with the hint', async () => {
    const { c } = await client({ lookupFailure: 'not-found' });
    const sent = await c.submitCuffReading({
      systolic: 120,
      diastolic: 80,
      transport: 'mllp',
      sendMalformed: null,
    });
    const trace = await c.getCuffAttemptTrace(sent.attemptId ?? '');
    expect(trace.attempt).toMatchObject({ messageLookup: 'not-found', application: null });
    expect(trace.observation).toBeNull();
    // A built reading's bytes come only from the stored message, which was not found.
    expect(trace.messageSent).toBeNull();
    const page = await c.listMessages({ count: 5 });
    const hint = sends(page.entries).find((e) => e.possibleDuplicateOf === sent.attemptId);
    expect(hint).toMatchObject({ source: 'store-only', controlId: sent.controlId });
  });
});

describe('mock refusals (amendments 7, 11, 12)', () => {
  it('a refused search is a 400 not-supported, not a 422', async () => {
    const { c } = await client();
    const e = await errorOf(
      c.searchObservations({
        code: LOINC.systolic,
        range: { start: new Date(NOW - 1000), end: new Date(NOW) },
      }),
    );
    expect([e.status, e.outcome?.issue[0]?.code, proxyReason(e.outcome)]).toEqual([
      400,
      'not-supported',
      'unsupported-value',
    ]);
  });

  it('the trace of an unreadable AuditEvent is a final 422; a store-only name matches no route (404)', async () => {
    const { c } = await client();
    const e = await errorOf(c.getCuffAttemptTrace('mock-unreadable-0001'));
    expect([e.status, proxyReason(e.outcome)]).toEqual([422, 'unreadable-send-attempt']);
    const { rows } = await allRows(c, { kind: 'cuff-send', count: 50 });
    const storeOnly = sends(rows).find((r) => r.source === 'store-only');
    const bad = await errorOf(c.getCuffAttemptTrace(storeOnly?.id ?? ''));
    expect([bad.status, proxyReason(bad.outcome)]).toEqual([404, 'unknown-route']);
    const odd = await errorOf(c.getCuffAttemptTrace('..'));
    expect([odd.status, proxyReason(odd.outcome)]).toEqual([400, 'invalid-id']);
  });

  it('?mock-status=not-configured: every protected route is 503 session-not-configured; the session stays', async () => {
    const onUnauthorized = vi.fn<() => void>();
    const { c } = await client({ statusMode: 'not-configured', onUnauthorized });
    for (const call of [
      () => c.getStatus(),
      () => c.listMessages(),
      () => c.getObservation('x1'),
      () => c.listMalformedSamples(),
    ]) {
      const e = await errorOf(call());
      expect([e.status, proxyReason(e.outcome)]).toEqual([503, 'session-not-configured']);
    }
    // The start-up probe gets the same 503 (T43: with a cookie), and nothing ended the session.
    const probe = await errorOf(c.checkSession());
    expect([probe.status, proxyReason(probe.outcome)]).toEqual([503, 'session-not-configured']);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe('mock status (amendments 9, 10, 13, 15)', () => {
  it('lastSyncAt is UTC with milliseconds; counts are numbers when the store answered', async () => {
    const { c } = await client();
    const s = await c.getStatus();
    expect(s.watchSync.lastSyncAt).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
    expect(typeof s.observationCounts.watch).toBe('number');
  });

  it('counts-unknown: null counts (never 0), store unreachable, no lastSyncAt', async () => {
    const { c } = await client({ statusMode: 'counts-unknown' });
    const s = await c.getStatus();
    expect(s.observationCounts).toEqual({ watch: null, cuff: null });
    expect(s.fhirStore).toMatchObject({ reachable: false, detail: 'timeout' });
    expect(s.watchSync.lastSyncAt).toBeNull();
  });

  it('openemr-not-checked and health-sync-timeout', async () => {
    const { c } = await client({ statusMode: 'openemr-not-checked' });
    expect((await c.getStatus()).openemr).toMatchObject({
      reachable: false,
      detail: 'not-checked',
    });
    const { c: hs } = await client({ statusMode: 'health-sync-timeout' });
    expect((await hs.getStatus()).watchSync).toMatchObject({
      state: 'unknown',
      reason: 'health-sync-timeout',
    });
  });
});

describe('mock read route GET /fhir/Observation/{id} (amendment 20)', () => {
  it('reads a stored Observation; an unknown id is 404, a bad id 400', async () => {
    const { c } = await client();
    const range = { start: new Date(NOW - 86_400_000), end: new Date(NOW) };
    const [hr] = await c.searchObservations({ code: LOINC.heartRate, range });
    expect((await c.getObservation(hr?.id ?? '')).id).toBe(hr?.id);
    const [bp] = await c.searchObservations({ code: LOINC.bloodPressurePanel, range });
    expect((await c.getObservation(bp?.id ?? '')).code.coding?.[0]?.code).toBe('85354-9');
    const missing = await errorOf(c.getObservation('nothing-here'));
    expect([missing.status, proxyReason(missing.outcome)]).toEqual([404, 'not-found']);
    expect((await errorOf(c.getObservation('../x'))).status).toBe(400);
  });
});
