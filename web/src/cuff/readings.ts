/**
 * The readings list as data: one `Reading` per send made in this session, and the words the page
 * shows for it. Pure functions, no DOM (readings.test.ts checks them with literal values).
 *
 * Two acknowledgment levels (D7), always kept apart:
 *   - the COMMIT-LEVEL ACKNOWLEDGMENT is the HL7v2 ACK the store returned for the send (MSA-1
 *     `AA` stored, `AE` rejected with MSA-3 as the reason), or no acknowledgment at all. It is in
 *     the send result, so it is known at once.
 *   - the APPLICATION-LEVEL OUTCOME is what the mapper (v2-to-fhir) made of a stored message:
 *     `accepted` (an Observation was written) or `error` with a reason code. It arrives seconds
 *     later, as labels on the stored message, so it starts as `pending` and the panel polls the
 *     attempt's trace until it settles (web/README.md, "Waiting for pending").
 * The names are modelled on HL7's enhanced acknowledgment mode (MSH-15 accept, MSH-16
 * application acknowledgment); the store itself only speaks the original mode.
 */
import type {
  AckCode,
  ApplicationOutcome,
  CommitAck,
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  NoAckReason,
} from '../api/contract';
import { ApiError, CuffSendError } from '../api/errors';
import { transportName } from './form';
import { reasonText } from './reasons';

/** How far the panel got in learning the application-level outcome. */
export type OutcomeLookup =
  /** Not stored (rejected or no acknowledgment): there is nothing for the mapper to do. */
  | 'not-stored'
  /** The attempt was not recorded, so it has no trace to ask. */
  | 'not-recorded'
  /** Polling the trace while the outcome is pending. */
  | 'polling'
  /** Polling reached its bound with the outcome still pending (web/README.md: a real state). */
  | 'gave-up'
  /** The trace lookup answered 404: the attempt is not (or no longer) in the audit store. */
  | 'missing'
  /** The outcome is accepted or error. */
  | 'settled';

export interface Reading {
  /** Local key for this row (unique on the page, `cuff-` prefixed so it can be an element id). */
  key: string;
  request: CuffReadingRequest;
  /** The send result exactly as returned. */
  result: CuffSendResult;
  /**
   * Whether the attempt can be looked up: its AuditEvent was recorded (D12). False when
   * `attemptId` is null (it could not be built) or `audit.recorded` is false (the write failed,
   * so nothing is stored under the id). Such a row is listed but has no trace.
   */
  recorded: boolean;
  /** Null when the message was never stored, so it has no application-level outcome. */
  application: ApplicationOutcome | null;
  lookup: OutcomeLookup;
  /** The resulting Observation's id, once the trace names one. */
  observationId: string | null;
  /** That Observation's `status` as the trace returned it (only `final` is shared). */
  observationStatus: string | null;
}

export function newReading(
  key: string,
  request: CuffReadingRequest,
  result: CuffSendResult,
): Reading {
  const recorded = result.attemptId !== null && result.audit.recorded;
  const stored = result.result === 'accepted';
  let lookup: OutcomeLookup = 'not-stored';
  if (stored) lookup = recorded ? 'polling' : 'not-recorded';
  return {
    key,
    request,
    result,
    recorded,
    application: stored ? { outcome: 'pending', reason: null } : null,
    lookup,
    observationId: null,
    observationStatus: null,
  };
}

/** True while the outcome may still change, so the row should be polled. */
export function isPending(r: Reading): boolean {
  return r.recorded && r.application?.outcome === 'pending';
}

/**
 * The reading updated from a trace answer. Monotonic: an answer that still says `pending` never
 * moves a settled row back (an older request can answer after a newer one).
 */
export function withTrace(r: Reading, trace: CuffAttemptTrace): Reading {
  const application = trace.attempt.application;
  if (r.application && r.application.outcome !== 'pending' && application?.outcome === 'pending') {
    return r;
  }
  const fromRef = trace.attempt.observation?.replace(/^Observation\//, '') ?? null;
  const observationId = trace.observation?.id ?? fromRef;
  return {
    ...r,
    application: application ?? r.application,
    lookup: application && application.outcome !== 'pending' ? 'settled' : r.lookup,
    observationId,
    observationStatus: trace.observation?.status ?? null,
  };
}

// -- words -------------------------------------------------------------------------------------

/**
 * MSA-1, the acknowledgment code (HL7 table 0008). The store sends AA or AE only (spike S4); the
 * others are listed so an unexpected code still reads as something.
 */
export const ACK_CODE_MEANING: Readonly<Record<AckCode, string>> = {
  AA: 'Application Accept: the HL7v2 store parsed the message and stored it.',
  AE: 'Application Error: the HL7v2 store could not process the message and did not store it.',
  AR: 'Application Reject: the message was refused outright (the store does not normally send this).',
  CA: 'Commit Accept (enhanced mode): the receiver has safely stored the message.',
  CE: 'Commit Error (enhanced mode): the receiver could not store the message.',
  CR: 'Commit Reject (enhanced mode): the receiver refused the message.',
};

export const NO_ACK_REASON: Readonly<Record<NoAckReason, string>> = {
  timeout: 'timed out: nothing came back before the simulator stopped waiting',
  'connection-closed': 'connection closed without a reply',
  'api-error': 'the ingest API answered with an error and no HL7v2 acknowledgment',
  'unreadable-reply': 'a reply came back, but it is not a readable HL7v2 ACK',
};

export type Tone = 'ok' | 'warn' | 'error' | 'neutral';

export interface Phrase {
  /** Short words for the list. */
  label: string;
  /** A sentence that explains it. */
  detail: string;
  tone: Tone;
}

/** The commit-level acknowledgment in words. */
export function commitAckPhrase(ack: CommitAck): Phrase {
  if (!ack.received) {
    const status =
      ack.why === 'api-error' && ack.httpStatus !== null ? ` (HTTP ${ack.httpStatus})` : '';
    return {
      label: 'No acknowledgment',
      detail: `No acknowledgment received: ${NO_ACK_REASON[ack.why]}${status}. It is not known whether the store kept the message.`,
      tone: 'warn',
    };
  }
  const meaning = ACK_CODE_MEANING[ack.code];
  const accepted = ack.code === 'AA' || ack.code === 'CA';
  return {
    label: accepted ? `${ack.code}, stored` : `${ack.code}, rejected`,
    detail: ack.reason ? `${meaning} Reason (MSA-3): ${ack.reason}` : meaning,
    tone: accepted ? 'ok' : 'error',
  };
}

/** The application-level outcome in words, for any row. */
export function applicationPhrase(r: Reading): Phrase {
  if (r.application === null) {
    if (!r.result.ack.received) {
      return {
        label: 'None',
        detail:
          'No acknowledgment came back, so the page cannot tell whether the mapper ever saw this message.',
        tone: 'neutral',
      };
    }
    return {
      label: 'None',
      detail: 'Not stored, so the mapper never saw it: there is no application-level outcome.',
      tone: 'neutral',
    };
  }
  if (r.application.outcome === 'accepted') {
    return {
      label: 'Accepted',
      detail: 'Accepted: the mapper wrote a FHIR Observation for this reading.',
      tone: 'ok',
    };
  }
  if (r.application.outcome === 'error') {
    return {
      label: 'Error',
      detail: `Error: ${reasonText(r.application.reason)}${r.application.reason ? ` (reason code ${r.application.reason})` : ''}`,
      tone: 'error',
    };
  }
  // pending
  const waiting: Record<OutcomeLookup, string> = {
    polling: 'Pending: stored, waiting for the mapper (checking every few seconds).',
    'gave-up':
      'Pending: still not mapped after 2 minutes, so the page stopped checking. The mapper may have failed, or the message is waiting to be replayed.',
    missing:
      'Pending when last known: the send-attempt record could not be found, so the outcome cannot be looked up.',
    'not-recorded':
      'Not known: this attempt was not recorded, so its outcome cannot be looked up here.',
    'not-stored': 'Pending.',
    settled: 'Pending.',
  };
  return {
    label: r.lookup === 'not-recorded' ? 'Not known' : 'Pending',
    detail: waiting[r.lookup],
    tone: 'warn',
  };
}

/** What was sent: the pressures, or the sample's name. */
export function whatWasSent(r: Reading): string {
  if (r.request.sendMalformed !== null) return `Malformed sample ${r.request.sendMalformed}`;
  return `BP ${r.request.systolic}/${r.request.diastolic} mm[Hg]`;
}

/**
 * Clock time of a send, `HH:MM:SS`, in `timeZone` (the browser's own when undefined). The page
 * lists this session's sends, so the viewer's own clock is the natural one; the trace shows the
 * full ISO instant.
 */
export function clockTime(iso: string, timeZone?: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    ...(timeZone ? { timeZone } : {}),
  }).format(ms);
}

/** The selection's summary, shown by the share area: "BP 121/79 mm[Hg] at 10:03:05, MLLP". */
export function readingSummary(r: Reading, timeZone?: string): string {
  return `${whatWasSent(r)} at ${clockTime(r.result.sentAt, timeZone)}, ${transportName(r.result.transport)}`;
}

/**
 * Why the selected reading may not be shared, or null when it may: only an accepted reading
 * whose Observation exists and is `final` (T06/T15 share only final Observations).
 */
export function unshareableReason(r: Reading): string | null {
  if (!r.result.ack.received) {
    return 'no acknowledgment came back, so nothing is known to be stored and there is no Observation.';
  }
  if (r.result.result !== 'accepted' || r.application === null) {
    return 'the HL7v2 store rejected this message, so it was never stored and there is no Observation.';
  }
  if (r.application.outcome === 'pending') {
    return 'not mapped to FHIR yet (application-level outcome pending), so there is no Observation to share.';
  }
  if (r.application.outcome === 'error') {
    return `the mapper could not turn it into an Observation: ${reasonText(r.application.reason)}`;
  }
  if (r.observationId === null) {
    return 'the mapper accepted it, but its Observation could not be found in the FHIR store.';
  }
  if (r.observationStatus !== 'final') {
    return `its Observation has status ${r.observationStatus ?? 'unknown'}, not final; only final readings are shared.`;
  }
  return null;
}

// -- refusals ----------------------------------------------------------------------------------

export interface SendFailureText {
  /** The message shown above the button. */
  message: string;
  /** Whether the reading may have been sent (so the user should check the log before retrying). */
  maybeSent: boolean;
}

/**
 * Words for a failed `submitCuffReading`. Field errors (`fields`) are shown next to their fields
 * by the panel; this is the message for the form as a whole. 401 and aborts are not passed here.
 */
export function sendFailureText(error: unknown): SendFailureText {
  if (error instanceof CuffSendError) {
    switch (error.reason) {
      case 'invalid-input':
        return {
          message: `The simulator refused the reading (HTTP 422): ${error.message}. Nothing was sent.`,
          maybeSent: false,
        };
      case 'rate-limited':
        return {
          message:
            'Too many readings in a short time: the simulator allows a few in a row, then about one every 10 seconds. Nothing was sent.',
          maybeSent: false,
        };
      case 'invalid-body':
        return {
          message: `The simulator could not read the request (HTTP 400): ${error.message}. Nothing was sent. This is a fault in the page.`,
          maybeSent: false,
        };
      case 'too-large':
        return {
          message: 'The request was too large for the simulator (HTTP 413). Nothing was sent.',
          maybeSent: false,
        };
      case 'server-error':
        return {
          message: `The simulator failed before sending (HTTP 500): ${error.message}. Nothing was sent.`,
          maybeSent: false,
        };
      case 'no-answer':
        return {
          message:
            'No answer in time (HTTP 504): the request was cut off on the way. The reading may have been sent, but it was not recorded, so it may not appear in the messages log. Wait a moment before sending again.',
          maybeSent: true,
        };
    }
  }
  if (error instanceof ApiError) {
    if (error.status === 0) {
      return {
        message: `The connection failed (${error.message}), so it is not known whether the reading was sent. Check the messages log before sending again.`,
        maybeSent: true,
      };
    }
    return {
      message: `The server answered HTTP ${error.status}: ${error.message}. It is not known whether the reading was sent.`,
      maybeSent: true,
    };
  }
  return {
    message: `Could not send: ${error instanceof Error ? error.message : 'unknown error'}. It is not known whether the reading was sent.`,
    maybeSent: true,
  };
}

/** Each named field's own diagnostics from a refusal, joined: { systolic: "must be ..." }. */
export function fieldMessages(error: CuffSendError): Partial<Record<string, string>> {
  const out: Record<string, string[]> = {};
  for (const issue of error.outcome?.issue ?? []) {
    for (const field of issue.expression ?? []) {
      (out[field] ??= []).push(issue.diagnostics?.trim() || 'is not valid');
    }
  }
  return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.join('; ')]));
}
