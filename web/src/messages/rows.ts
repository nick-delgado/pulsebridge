/**
 * The messages log as words: what each row of `GET /messages` says, as plain data with no DOM
 * (rows.test.ts checks it with literal rows in the proxy's shapes).
 *
 * Four kinds of row (services/fhir-proxy/README.md, "How the joins work"):
 *   - a cuff SEND ATTEMPT, read from its AuditEvent in the audit FHIR store (D12, D15): the
 *     commit-level acknowledgment the store gave, and, once the proxy has confirmed the stored
 *     message (`messageLookup`), the application-level outcome the mapper recorded (D7);
 *   - a STORE-ONLY message: in the HL7v2 store, but no send attempt names it (its AuditEvent was
 *     not written, or it was sent straight to the adapter). It has no trace and is never
 *     selected; `possibleDuplicateOf` is a hint at one attempt, never a confirmed link;
 *   - an UNREADABLE send record: an AuditEvent that is ours but could not be read;
 *   - a FHIR TRANSACTION: one ingest Bundle, derived from the Provenances it wrote.
 *
 * The acknowledgment words are the cuff side's own (`commitAckPhrase`, `ACK_MODE_NOTE`), imported
 * rather than copied, so the log and the cuff trace can never describe an ACK differently.
 */
import type {
  FhirTransactionEntry,
  MessagesLogEntry,
  SendAttemptEntry,
  UnreadableSendAttemptEntry,
} from '../api/contract';
import { reasonText } from '../api/reasons';
import { transportName } from '../cuff/form';
import { commitAckPhrase, type Phrase } from '../cuff/readings';

export const COMMIT_LEVEL = 'Commit-level acknowledgment (from the store)';
export const APPLICATION_LEVEL = 'Application-level outcome (from the mapper)';

/** What the log lists, and what it cannot list (the summary under the strip's title). */
export const LOG_SCOPE =
  'Every cuff send the simulator recorded (accepted, rejected or with no acknowledgment), messages in the HL7v2 store that no send record names, and every FHIR transaction written for this patient, newest first by send time. A send whose record could not be written ("not recorded" in the cuff panel) has no AuditEvent, so it is not listed as a send; if the store kept its message, that message appears as a stored message with no send record.';

export type RowKind = 'attempt' | 'store-only' | 'unreadable' | 'transaction';

export interface Fact {
  term: string;
  label: string;
  detail: string;
  tone: Phrase['tone'];
}

/**
 * A stable key per row, so a refresh updates rows in place (focus and scroll survive). An
 * unreadable row may have the id `""` (amendment 6), so its time and problem are part of its key.
 */
export function rowKey(e: MessagesLogEntry): string {
  if (e.kind === 'cuff-send') return `${e.source}|${e.id}`;
  if (e.kind === 'fhir-transaction') return `transaction|${e.id}`;
  return `unreadable|${e.id}|${e.recordedAt ?? ''}|${e.id === '' ? e.problem : ''}`;
}

export function rowKind(e: MessagesLogEntry): RowKind {
  if (e.kind === 'cuff-send') return e.source === 'store-only' ? 'store-only' : 'attempt';
  return e.kind === 'fhir-transaction' ? 'transaction' : 'unreadable';
}

/**
 * The instant a row sorts on (amendment 17): an attempt's send time, a store-only message's
 * createTime (`recordedAt`), a transaction's `recordedAt`; null for an undated unreadable row.
 */
export function sortTime(e: MessagesLogEntry): string | null {
  if (e.kind === 'cuff-send') return e.source === 'store-only' ? e.recordedAt : e.sentAt;
  return e.recordedAt;
}

/** "10 Mar, 14:17:05" in `timeZone` (the browser's own when undefined); the ISO text if unreadable. */
export function logTime(iso: string | null, timeZone?: string): string {
  if (iso === null) return 'time unknown';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    ...(timeZone ? { timeZone } : {}),
  }).format(ms);
}

const WRITER: Readonly<Record<FhirTransactionEntry['writer'], string>> = {
  'health-sync': 'health-sync (the watch’s heart-rate sync)',
  'v2-to-fhir': 'v2-to-fhir (the HL7v2 mapper, for a cuff reading)',
  seed: 'the synthetic-data seeder',
  unknown: 'a writer the proxy does not recognise',
};

export function writerText(writer: FhirTransactionEntry['writer']): string {
  return WRITER[writer];
}

/** The row's title: what kind of record it is. */
export function rowTitle(e: MessagesLogEntry): string {
  switch (rowKind(e)) {
    case 'attempt':
      return 'Cuff send';
    case 'store-only':
      return 'Stored message, no send record';
    case 'unreadable':
      return 'Unreadable send record';
    case 'transaction':
      return 'FHIR transaction';
  }
}

/**
 * The application-level outcome of a cuff row in words (D7). Only a row whose stored message the
 * proxy confirmed has one; the others say why there is none, plainly, never "pending".
 */
export function applicationFact(e: SendAttemptEntry): Fact {
  const term = APPLICATION_LEVEL;
  const a = e.application;
  if (a === null) {
    if (e.messageLookup === 'not-found') {
      return {
        term,
        label: 'Not found',
        detail:
          'Not known: the HL7v2 store has no message under the name recorded for this send (the simulator derives it for an MLLP send, and it is unconfirmed), so its outcome cannot be looked up.',
        tone: 'warn',
      };
    }
    if (e.messageLookup === 'failed') {
      return {
        term,
        label: 'Could not be checked',
        detail:
          'Not known: the proxy could not ask the HL7v2 store in time. Reloading the log may confirm it.',
        tone: 'warn',
      };
    }
    if (!e.commitAck.received) {
      return {
        term,
        label: 'None',
        detail:
          'No acknowledgment came back, so it is not known whether the store kept the message: there is no outcome to show.',
        tone: 'neutral',
      };
    }
    return {
      term,
      label: 'None',
      detail: 'Not stored, so the mapper never saw it: there is no application-level outcome.',
      tone: 'neutral',
    };
  }
  if (a.outcome === 'accepted') {
    return {
      term,
      label: 'Accepted',
      detail: 'Accepted: the mapper wrote a FHIR Observation for this message.',
      tone: 'ok',
    };
  }
  if (a.outcome === 'error') {
    return {
      term,
      label: 'Error',
      detail: `Error: ${reasonText(a.reason)}${a.reason ? ` (reason code ${a.reason})` : ''}`,
      tone: 'error',
    };
  }
  return {
    term,
    label: 'Pending',
    detail:
      'Pending: stored, but the mapper has not labelled it yet. It may still be running, or it failed and the message waits to be replayed.',
    tone: 'warn',
  };
}

/** The commit-level acknowledgment in words: the cuff side's phrase, plus what a store-only row means. */
export function commitFact(e: SendAttemptEntry): Fact {
  const phrase = commitAckPhrase(e.commitAck);
  if (e.source === 'store-only') {
    return {
      term: COMMIT_LEVEL,
      ...phrase,
      detail:
        'AA, stored: the message is in the HL7v2 store, so the store accepted it. No send record holds the acknowledgment itself.',
    };
  }
  return { term: COMMIT_LEVEL, ...phrase };
}

/** The stored message's name and where it came from. */
function messageFact(e: SendAttemptEntry): Fact {
  const term = 'Stored message';
  if (e.messageName !== null) {
    return {
      term,
      label: e.source === 'store-only' ? 'Listed from the store' : 'Name returned by the store',
      detail: e.messageName,
      tone: 'neutral',
    };
  }
  if (e.derivedMessageName !== null) {
    const check =
      e.messageLookup === 'confirmed'
        ? 'The store confirmed it.'
        : e.messageLookup === 'not-found'
          ? 'The store does not have it.'
          : 'It could not be checked.';
    return {
      term,
      label:
        e.messageLookup === 'confirmed'
          ? 'Name derived by the simulator, confirmed by the store'
          : 'Name derived by the simulator (unconfirmed)',
      detail: `${e.derivedMessageName} (from the SHA-256 of the bytes sent, because an MLLP acknowledgment carries no name). ${check}`,
      tone: e.messageLookup === 'confirmed' ? 'neutral' : 'warn',
    };
  }
  return { term, label: 'None', detail: 'The store did not report storing it.', tone: 'neutral' };
}

/** The facts of one row, in the order they are shown. */
export function rowFacts(e: MessagesLogEntry): Fact[] {
  if (e.kind === 'cuff-send') {
    const facts: Fact[] = [
      commitFact(e),
      applicationFact(e),
      {
        term: 'Transport',
        label: e.transport === null ? 'Not known' : transportName(e.transport),
        detail:
          e.transport === null
            ? 'The MLLP adapter cannot label a message, so the transport of a message with no send record is not known (probably MLLP).'
            : '',
        tone: 'neutral',
      },
      {
        term: 'Control ID (MSH-10)',
        label: e.controlId ?? 'None',
        detail: e.controlId === null ? 'The message has no readable MSH-10.' : '',
        tone: 'neutral',
      },
      messageFact(e),
      {
        term: 'Resulting Observation',
        label: e.observation ?? 'None',
        detail: '',
        tone: 'neutral',
      },
    ];
    if (e.sample !== null) {
      facts.splice(2, 0, {
        term: 'Malformed sample',
        label: e.sample,
        detail: '',
        tone: 'neutral',
      });
    }
    return facts;
  }
  if (e.kind === 'unreadable-send-attempt') return unreadableFacts(e);
  return [
    { term: 'Written by', label: writerText(e.writer), detail: '', tone: 'neutral' },
    {
      term: 'HTTP status',
      label: e.httpStatus === null ? 'Not recorded' : String(e.httpStatus),
      detail:
        e.httpStatus === null
          ? 'Nothing records the executeBundle status; the Provenance it wrote shows the transaction committed (a transaction is all or nothing).'
          : '',
      tone: 'neutral',
    },
    {
      term: 'Resources',
      label: resourceCount(e.resources),
      detail: '',
      tone: 'neutral',
    },
  ];
}

function unreadableFacts(e: UnreadableSendAttemptEntry): Fact[] {
  return [
    {
      term: 'Problem',
      label: 'Could not be read',
      detail: e.problem,
      tone: 'error',
    },
    {
      term: 'AuditEvent id',
      label: e.id === '' ? 'None' : e.id,
      detail: e.id === '' ? 'The AuditEvent has no id.' : '',
      tone: 'neutral',
    },
  ];
}

/** "1 row", "3 rows": a count with its noun, singular for exactly one. */
export function plural(n: number, noun: string, nouns = `${noun}s`): string {
  return `${String(n)} ${n === 1 ? noun : nouns}`;
}

/** "3 Observations, 3 Provenances". */
export function resourceCount(resources: string[]): string {
  const counts = new Map<string, number>();
  for (const r of resources) {
    const type = r.split('/')[0] ?? r;
    counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  if (counts.size === 0) return 'None listed';
  return [...counts].map(([type, n]) => plural(n, type)).join(', ');
}

/** True for a row the log should keep checking: stored, confirmed, and not labelled yet. */
export function isPendingRow(e: MessagesLogEntry): boolean {
  return e.kind === 'cuff-send' && e.application?.outcome === 'pending';
}

/** True for a row that shows a problem: a rejection, no ACK, a mapper error, a lookup that failed. */
export function isProblemRow(e: MessagesLogEntry): boolean {
  if (e.kind === 'unreadable-send-attempt') return true;
  if (e.kind !== 'cuff-send') return false;
  return (
    e.result !== 'accepted' ||
    e.application?.outcome === 'error' ||
    e.messageLookup === 'not-found' ||
    e.messageLookup === 'failed'
  );
}

/** The selection summary for an attempt row, for the share area: "Cuff send at 10 Mar, 14:17:05, MLLP". */
export function attemptSummary(e: SendAttemptEntry, timeZone?: string): string {
  const how = e.transport === null ? '' : `, ${transportName(e.transport)}`;
  return `Cuff send at ${logTime(e.sentAt, timeZone)}${how} (from the messages log)`;
}

/**
 * Why an attempt row's reading may not be shared, or null when it has an Observation (the share
 * area then reads that Observation and checks it is `final` before sending anything).
 */
export function attemptUnshareableReason(e: SendAttemptEntry): string | null {
  if (!e.commitAck.received) {
    return 'no acknowledgment came back, so nothing is known to be stored and there is no Observation.';
  }
  if (e.result !== 'accepted') {
    return 'the HL7v2 store rejected this message, so it was never stored and there is no Observation.';
  }
  if (e.messageLookup === 'not-found') {
    return 'the HL7v2 store has no message under this send’s name, so its Observation is not known.';
  }
  if (e.messageLookup === 'failed') {
    return 'its stored message could not be checked, so its Observation is not known.';
  }
  if (e.application?.outcome === 'pending') {
    return 'not mapped to FHIR yet (application-level outcome pending), so there is no Observation to share.';
  }
  if (e.application?.outcome === 'error') {
    return `the mapper could not turn it into an Observation: ${reasonText(e.application.reason)}`;
  }
  if (e.observation === null) {
    return 'the mapper accepted it, but its Observation could not be found in the FHIR store.';
  }
  return null;
}
