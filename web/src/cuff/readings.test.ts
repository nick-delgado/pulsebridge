import { describe, expect, it } from 'vitest';
import { ApiError, CuffSendError } from '../api/errors';
import type { OperationOutcome } from '../api/fhir';
import {
  applicationPhrase,
  clockTime,
  commitAckPhrase,
  fieldMessages,
  isPending,
  newReading,
  readingSummary,
  sendFailureText,
  unshareableReason,
  whatWasSent,
  withTrace,
} from './readings';
import { AA, AE, attemptTrace, bpObservation, noAck, sendResult } from './test-support';

const built = { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null } as const;
const sample = { transport: 'mllp', sendMalformed: 'unparseable_no_msh' } as const;

describe('newReading', () => {
  it('an accepted, recorded send starts pending and is polled', () => {
    const r = newReading('cuff-row-1', built, sendResult());
    expect(r.application).toEqual({ outcome: 'pending', reason: null });
    expect(r.lookup).toBe('polling');
    expect(r.recorded).toBe(true);
    expect(isPending(r)).toBe(true);
  });

  it('a rejected send has no application-level outcome', () => {
    const r = newReading('k', sample, sendResult({ ack: AE }));
    expect(r.application).toBeNull();
    expect(r.lookup).toBe('not-stored');
    expect(isPending(r)).toBe(false);
  });

  it('attemptId null (AuditEvent not built) is not recorded and not polled', () => {
    const r = newReading(
      'k',
      built,
      sendResult({ attemptId: null, audit: { recorded: false, detail: 'could not build' } }),
    );
    expect(r.recorded).toBe(false);
    expect(r.lookup).toBe('not-recorded');
    expect(isPending(r)).toBe(false);
  });

  it('an id with a failed write is not recorded either (nothing is stored under it)', () => {
    const r = newReading(
      'k',
      built,
      sendResult({ audit: { recorded: false, detail: 'HTTP 503' } }),
    );
    expect(r.recorded).toBe(false);
  });
});

describe('withTrace', () => {
  const r = newReading('k', built, sendResult());

  it('settles the outcome and takes the Observation id and status from the trace', () => {
    const next = withTrace(
      r,
      attemptTrace(
        sendResult(),
        { outcome: 'accepted', reason: null },
        { observation: bpObservation('bp-9') },
      ),
    );
    expect(next.application).toEqual({ outcome: 'accepted', reason: null });
    expect(next.lookup).toBe('settled');
    expect(next.observationId).toBe('bp-9');
    expect(next.observationStatus).toBe('final');
  });

  it('never moves a settled row back to pending (an older answer arriving late)', () => {
    const settled = withTrace(
      r,
      attemptTrace(sendResult(), { outcome: 'error', reason: 'out-of-range' }),
    );
    const late = withTrace(
      settled,
      attemptTrace(sendResult(), { outcome: 'pending', reason: null }),
    );
    expect(late).toBe(settled);
    expect(late.application).toEqual({ outcome: 'error', reason: 'out-of-range' });
  });
});

describe('words for the two acknowledgment levels', () => {
  it('AA and AE, with MSA-3 as the reason', () => {
    expect(commitAckPhrase(AA)).toEqual({
      label: 'AA, stored',
      detail: 'Application Accept: the HL7v2 store parsed the message and stored it.',
      tone: 'ok',
    });
    expect(commitAckPhrase(AE)).toEqual({
      label: 'AE, rejected',
      detail:
        'Application Error: the HL7v2 store could not process the message and did not store it. Reason (MSA-3): Invalid MSH header',
      tone: 'error',
    });
    expect(
      commitAckPhrase({ received: true, code: 'AR', controlId: null, reason: null, raw: null })
        .label,
    ).toBe('AR, rejected');
  });

  it.each([
    ['timeout', 'timed out: nothing came back before the simulator stopped waiting'],
    ['connection-closed', 'connection closed without a reply'],
    ['api-error', 'the ingest API answered with an error and no HL7v2 acknowledgment (HTTP 503)'],
    ['unreadable-reply', 'a reply came back, but it is not a readable HL7v2 ACK'],
  ] as const)('no acknowledgment: %s', (why, words) => {
    const p = commitAckPhrase(noAck(why));
    expect(p.label).toBe('No acknowledgment');
    expect(p.detail).toBe(
      `No acknowledgment received: ${words}. It is not known whether the store kept the message.`,
    );
  });

  it('application-level outcome: pending, accepted, error with the reason in words', () => {
    const r = newReading('k', built, sendResult());
    expect(applicationPhrase(r).label).toBe('Pending');
    expect(applicationPhrase({ ...r, lookup: 'gave-up' }).detail).toContain('stopped checking');
    expect(
      applicationPhrase({ ...r, application: { outcome: 'accepted', reason: null } }).label,
    ).toBe('Accepted');
    expect(
      applicationPhrase({ ...r, application: { outcome: 'error', reason: 'missing-obx-14' } })
        .detail,
    ).toBe(
      'Error: An OBX-14 (date/time of the observation) is empty. (reason code missing-obx-14)',
    );
    expect(applicationPhrase(newReading('k', sample, sendResult({ ack: AE }))).detail).toBe(
      'Not stored, so the mapper never saw it: there is no application-level outcome.',
    );
    const unrecorded = newReading(
      'k',
      built,
      sendResult({ attemptId: null, audit: { recorded: false, detail: 'x' } }),
    );
    expect(applicationPhrase(unrecorded).label).toBe('Not known');
  });
});

describe('unshareableReason: only an accepted, mapped, final reading may be shared', () => {
  const pending = newReading('k', built, sendResult());
  const accepted = withTrace(
    pending,
    attemptTrace(
      sendResult(),
      { outcome: 'accepted', reason: null },
      { observation: bpObservation('bp-1') },
    ),
  );

  it('null for an accepted reading with a final Observation', () => {
    expect(unshareableReason(accepted)).toBeNull();
  });

  it('a reason for pending, error, rejected, no acknowledgment and not final', () => {
    expect(unshareableReason(pending)).toContain('not mapped to FHIR yet');
    expect(
      unshareableReason({ ...pending, application: { outcome: 'error', reason: 'out-of-range' } }),
    ).toBe(
      'the mapper could not turn it into an Observation: A value is outside the plausible range (systolic 50 to 300, diastolic 20 to 200 mm[Hg]).',
    );
    expect(unshareableReason(newReading('k', sample, sendResult({ ack: AE })))).toContain(
      'rejected this message',
    );
    expect(
      unshareableReason(newReading('k', built, sendResult({ ack: noAck('timeout') }))),
    ).toContain('no acknowledgment came back');
    expect(unshareableReason({ ...accepted, observationStatus: 'preliminary' })).toBe(
      'its Observation has status preliminary, not final; only final readings are shared.',
    );
    expect(unshareableReason({ ...accepted, observationId: null })).toContain('could not be found');
  });
});

describe('what was sent and when', () => {
  it('pressures or the sample name', () => {
    expect(whatWasSent(newReading('k', built, sendResult()))).toBe('BP 120/80 mm[Hg]');
    expect(whatWasSent(newReading('k', sample, sendResult({ ack: AE })))).toBe(
      'Malformed sample unparseable_no_msh',
    );
  });

  it('clock time in the given zone, whatever the machine zone', () => {
    expect(clockTime('2026-03-01T09:15:30.000Z', 'UTC')).toBe('09:15:30');
    expect(clockTime('2026-03-01T09:15:30.000Z', 'Asia/Kolkata')).toBe('14:45:30');
    expect(clockTime('not a time', 'UTC')).toBe('not a time');
    expect(readingSummary(newReading('k', built, sendResult({ transport: 'https' })), 'UTC')).toBe(
      'BP 120/80 mm[Hg] at 09:15:30, HTTPS',
    );
  });
});

describe('sendFailureText for every refusal', () => {
  const outcome = (issues: [string, string][]): OperationOutcome => ({
    resourceType: 'OperationOutcome',
    issue: issues.map(([f, d]) => ({
      severity: 'error',
      code: 'invalid',
      diagnostics: d,
      expression: [f],
    })),
  });
  const cuff = (
    status: number,
    issues: [string, string][] = [['body', 'x']],
    retry: number | null = null,
  ) =>
    CuffSendError.from(
      new ApiError(status, `HTTP ${status}`, outcome(issues), retry),
    ) as CuffSendError;

  it.each([
    [400, 'could not read the request (HTTP 400)', false],
    [413, 'too large for the simulator (HTTP 413). Nothing was sent.', false],
    [422, 'refused the reading (HTTP 422)', false],
    [429, 'Too many readings in a short time', false],
    [500, 'failed before sending (HTTP 500)', false],
    [504, 'The reading may have been sent', true],
  ])('HTTP %d', (status, words, maybeSent) => {
    const text = sendFailureText(cuff(status));
    expect(text.message).toContain(words);
    expect(text.maybeSent).toBe(maybeSent);
    if (!maybeSent) expect(text.message).toContain('Nothing was sent');
  });

  it('a plain network failure and another status say it is not known', () => {
    expect(sendFailureText(new ApiError(0, 'Failed to fetch')).message).toBe(
      'The connection failed (Failed to fetch), so it is not known whether the reading was sent. Check the messages log before sending again.',
    );
    expect(sendFailureText(new ApiError(502, 'Bad gateway')).message).toContain('HTTP 502');
  });

  it('fieldMessages joins each field’s own diagnostics', () => {
    expect(
      fieldMessages(
        cuff(422, [
          ['systolic', 'is required'],
          ['systolic', 'must be greater than diastolic'],
          ['diastolic', 'must be from 1 to 400 mm[Hg]'],
        ]),
      ),
    ).toEqual({
      systolic: 'is required; must be greater than diastolic',
      diastolic: 'must be from 1 to 400 mm[Hg]',
    });
  });
});
