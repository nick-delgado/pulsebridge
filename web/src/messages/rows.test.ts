import { describe, expect, it } from 'vitest';
import {
  APPLICATION_LEVEL,
  applicationFact,
  attemptUnshareableReason,
  COMMIT_LEVEL,
  isPendingRow,
  isProblemRow,
  LOG_SCOPE,
  logTime,
  resourceCount,
  rowFacts,
  rowKey,
  rowTitle,
  sortTime,
  writerText,
} from './rows';
import { attempt, noAckRow, rejected, storeOnly, transaction, unreadable } from './test-support';

const fact = (facts: ReturnType<typeof rowFacts>, term: string) =>
  facts.find((f) => f.term === term);

describe('the two acknowledgment levels of a v2 row (D7)', () => {
  it('accepted: AA stored, and the mapper accepted it', () => {
    const facts = rowFacts(attempt());
    expect(fact(facts, COMMIT_LEVEL)).toMatchObject({ label: 'AA, stored', tone: 'ok' });
    expect(fact(facts, APPLICATION_LEVEL)).toMatchObject({ label: 'Accepted', tone: 'ok' });
  });

  it('AE: rejected with MSA-3, and no application-level outcome', () => {
    const facts = rowFacts(rejected());
    expect(fact(facts, COMMIT_LEVEL)?.label).toBe('AE, rejected');
    expect(fact(facts, COMMIT_LEVEL)?.detail).toContain('Reason (MSA-3): Invalid MSH header');
    expect(fact(facts, APPLICATION_LEVEL)).toMatchObject({
      label: 'None',
      detail: 'Not stored, so the mapper never saw it: there is no application-level outcome.',
    });
  });

  it('no acknowledgment: says so, and that no outcome is known', () => {
    const facts = rowFacts(noAckRow());
    expect(fact(facts, COMMIT_LEVEL)?.label).toBe('No acknowledgment');
    expect(fact(facts, APPLICATION_LEVEL)?.detail).toContain(
      'it is not known whether the store kept',
    );
  });

  it('pending: stored, not labelled yet', () => {
    const row = attempt({ application: { outcome: 'pending', reason: null }, observation: null });
    expect(applicationFact(row)).toMatchObject({ label: 'Pending', tone: 'warn' });
    expect(isPendingRow(row)).toBe(true);
  });

  it('error: the reason code as readable text, from the shared reasons module', () => {
    const row = attempt({ application: { outcome: 'error', reason: 'missing-obx-14' } });
    expect(applicationFact(row).detail).toBe(
      'Error: An OBX-14 (date/time of the observation) is empty. (reason code missing-obx-14)',
    );
    expect(isProblemRow(row)).toBe(true);
  });

  it('lookup not-found or failed: "not found" / "could not be checked", never "pending"', () => {
    const nf = applicationFact(
      attempt({ application: null, messageLookup: 'not-found', observation: null }),
    );
    expect(nf.label).toBe('Not found');
    expect(nf.detail).toContain(
      'the HL7v2 store has no message under the name recorded for this send',
    );
    const failed = applicationFact(
      attempt({ application: null, messageLookup: 'failed', observation: null }),
    );
    expect(failed.label).toBe('Could not be checked');
    expect(isPendingRow(attempt({ application: null, messageLookup: 'failed' }))).toBe(false);
  });
});

describe('other row facts', () => {
  it('derived name labelled as derived and unconfirmed, with what the store said', () => {
    const facts = rowFacts(attempt());
    expect(fact(facts, 'Stored message')?.label).toBe(
      'Name derived by the simulator (unconfirmed)',
    );
    expect(fact(facts, 'Stored message')?.detail).toContain('The store confirmed it.');
    const nf = rowFacts(attempt({ messageLookup: 'not-found', application: null }));
    expect(fact(nf, 'Stored message')?.detail).toContain('The store does not have it.');
  });

  it('transport and control ID; a store-only row says its transport is not known', () => {
    expect(fact(rowFacts(attempt()), 'Transport')?.label).toBe('MLLP');
    expect(fact(rowFacts(attempt({ transport: 'https' })), 'Transport')?.label).toBe('HTTPS');
    const so = rowFacts(storeOnly());
    expect(fact(so, 'Transport')?.label).toBe('Not known');
    expect(fact(so, 'Control ID (MSH-10)')?.label).toBe('C0FFEE0000000000000A');
    expect(fact(so, COMMIT_LEVEL)?.detail).toContain('No send record holds the acknowledgment');
    expect(fact(rowFacts(attempt({ controlId: null })), 'Control ID (MSH-10)')?.label).toBe('None');
  });

  it('a malformed sample is named', () => {
    expect(
      fact(rowFacts(attempt({ sample: 'unparseable_no_msh' })), 'Malformed sample')?.label,
    ).toBe('unparseable_no_msh');
  });

  it('transaction rows: writer in words, an unrecorded HTTP status, resource counts', () => {
    const facts = rowFacts(transaction({ writer: 'seed' }));
    expect(fact(facts, 'Written by')?.label).toBe('the synthetic-data seeder');
    expect(fact(facts, 'HTTP status')?.label).toBe('Not recorded');
    expect(fact(facts, 'Resources')?.label).toBe('2 Observations, 2 Provenances');
    expect(fact(rowFacts(transaction({ httpStatus: 200 })), 'HTTP status')?.label).toBe('200');
    expect(writerText('unknown')).toBe('a writer the proxy does not recognise');
    expect(resourceCount(['Observation/a'])).toBe('1 Observation');
  });

  it('unreadable rows: the problem, and "None" for an empty id', () => {
    const facts = rowFacts(unreadable({ id: '' }));
    expect(fact(facts, 'Problem')?.detail).toBe('written in format version 2');
    expect(fact(facts, 'AuditEvent id')?.label).toBe('None');
    expect(isProblemRow(unreadable())).toBe(true);
  });

  it('titles', () => {
    expect([attempt(), storeOnly(), unreadable(), transaction()].map(rowTitle)).toEqual([
      'Cuff send',
      'Stored message, no send record',
      'Unreadable send record',
      'FHIR transaction',
    ]);
  });

  it('the scope says not-recorded sends are not listed as sends, and why', () => {
    expect(LOG_SCOPE).toContain('has no AuditEvent, so it is not listed as a send');
  });
});

describe('keys, sort times and clock text', () => {
  it('keys are stable and distinct; an unreadable row with id "" is keyed by time and problem', () => {
    expect(rowKey(attempt())).toBe('audit-event|att-1');
    expect(rowKey(storeOnly())).not.toBe(rowKey(attempt({ id: storeOnly().id })));
    expect(rowKey(unreadable({ id: '', problem: 'a' }))).not.toBe(
      rowKey(unreadable({ id: '', problem: 'b' })),
    );
  });

  it('sorts attempts by send time, store-only by createTime (recordedAt)', () => {
    expect(sortTime(attempt())).toBe('2026-03-10T14:17:00.000Z');
    expect(sortTime(storeOnly())).toBe('2026-03-10T14:15:02.000Z');
    expect(sortTime(unreadable({ recordedAt: null }))).toBeNull();
  });

  it('formats in the given zone', () => {
    expect(logTime('2026-03-10T14:17:05.000Z', 'UTC')).toBe('10 Mar, 14:17:05');
    expect(logTime('2026-03-10T14:17:05.000Z', 'Asia/Kolkata')).toBe('10 Mar, 19:47:05');
    expect(logTime(null)).toBe('time unknown');
  });
});

describe('selecting an attempt from the log', () => {
  it('a mapped attempt with an Observation may be shared (the share area checks it is final)', () => {
    expect(attemptUnshareableReason(attempt())).toBeNull();
  });

  it.each([
    [rejected(), 'rejected'],
    [noAckRow(), 'no acknowledgment'],
    [
      attempt({ messageLookup: 'not-found', application: null, observation: null }),
      'no message under',
    ],
    [
      attempt({ messageLookup: 'failed', application: null, observation: null }),
      'could not be checked',
    ],
    [
      attempt({ application: { outcome: 'pending', reason: null }, observation: null }),
      'not mapped',
    ],
    [attempt({ application: { outcome: 'error', reason: 'out-of-range' } }), 'could not turn it'],
    [attempt({ observation: null }), 'could not be found'],
  ])('otherwise says why (%#)', (row, words) => {
    expect(attemptUnshareableReason(row)).toContain(words);
  });
});
