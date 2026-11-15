/**
 * Helpers for this directory's unit tests only (imported by *.test.ts, never by the page).
 * Every value is SYNTHETIC and hand-made; no real reading is ever used.
 */
import type {
  ApplicationOutcome,
  CommitAck,
  CuffAttemptTrace,
  CuffSendResult,
  MalformedSample,
  SendAttemptEntry,
} from '../api/contract';
import type { Observation } from '../api/fhir';

export const SENT_AT = '2026-03-01T09:15:30.000Z';
export const STORE = 'projects/p/locations/l/datasets/d/hl7V2Stores/s';
export const BUILT_ER7 =
  'MSH|^~\\&|CUFFSIM|PULSEBRIDGE|PHR|PULSEBRIDGE|20260301091530+0000||ORU^R01^ORU_R01|C0FFEE0000000000000A|P|2.5.1\r' +
  'PID|1||SYN-000123^^^SYN||Synthetic^Demo\r' +
  'OBR|1||C0FFEE0000000000000A|85354-9^Blood pressure panel^LN\r' +
  'OBX|1|NM|8480-6^Systolic^LN||120|mm[Hg]^mm[Hg]^UCUM|||||F|||20260301091530+0000\r' +
  'OBX|2|NM|8462-4^Diastolic^LN||80|mm[Hg]^mm[Hg]^UCUM|||||F|||20260301091530+0000\r';

export const AA: CommitAck = {
  received: true,
  code: 'AA',
  controlId: 'C0FFEE0000000000000A',
  reason: null,
  raw: 'MSH|^~\\&|||||20260301091531+0000||ACK|A1|P|2.5.1\rMSA|AA|C0FFEE0000000000000A',
};

export const AE: CommitAck = {
  received: true,
  code: 'AE',
  controlId: null,
  reason: 'Invalid MSH header',
  raw: 'MSH|^~\\&|||||20260301091531+0000||ACK|A2|P|2.5.1\rMSA|AE||Invalid MSH header',
};

export function noAck(
  why: 'timeout' | 'connection-closed' | 'api-error' | 'unreadable-reply',
): CommitAck {
  return {
    received: false,
    why,
    httpStatus: why === 'api-error' ? 503 : null,
    detail: `synthetic ${why} detail`,
    replyPrefixBase64: why === 'unreadable-reply' ? '77u/TVNIfA==' : null,
  };
}

/** A send result in cuff-sim's shape; accepted over MLLP by default. */
export function sendResult(overrides: Partial<CuffSendResult> = {}): CuffSendResult {
  const ack = overrides.ack ?? AA;
  const accepted = ack.received && (ack.code === 'AA' || ack.code === 'CA');
  return {
    attemptId: 'att-1',
    sentAt: SENT_AT,
    transport: 'mllp',
    controlId: 'C0FFEE0000000000000A',
    sample: null,
    messageSent: BUILT_ER7,
    framing: 'standard',
    messageName: accepted ? `${STORE}/messages/derivedAbc=` : null,
    messageNameSource: accepted ? 'derived-sha256-unverified' : null,
    result: !ack.received ? 'no-acknowledgment' : accepted ? 'accepted' : 'rejected',
    ack,
    audit: { recorded: true },
    ...overrides,
  };
}

export function bpObservation(id: string, status: Observation['status'] = 'final'): Observation {
  return {
    resourceType: 'Observation',
    id,
    status,
    code: { coding: [{ system: 'http://loinc.org', code: '85354-9' }] },
    effectiveDateTime: SENT_AT,
    component: [
      {
        code: { coding: [{ system: 'http://loinc.org', code: '8480-6' }] },
        valueQuantity: {
          value: 120,
          unit: 'mm[Hg]',
          system: 'http://unitsofmeasure.org',
          code: 'mm[Hg]',
        },
      },
      {
        code: { coding: [{ system: 'http://loinc.org', code: '8462-4' }] },
        valueQuantity: {
          value: 80,
          unit: 'mm[Hg]',
          system: 'http://unitsofmeasure.org',
          code: 'mm[Hg]',
        },
      },
    ],
  };
}

/** A trace answer for an attempt, with the given application-level outcome. */
export function attemptTrace(
  result: CuffSendResult,
  application: ApplicationOutcome | null,
  options: { observation?: Observation | null; messageSent?: string | null } = {},
): CuffAttemptTrace {
  const observation = options.observation ?? null;
  const entry: SendAttemptEntry = {
    kind: 'cuff-send',
    id: result.attemptId ?? 'none',
    sentAt: result.sentAt,
    recordedAt: result.sentAt,
    transport: result.transport,
    controlId: result.controlId,
    result: result.result,
    outcome: result.result === 'accepted' ? '0' : result.result === 'rejected' ? '4' : '8',
    sample: result.sample,
    commitAck: result.ack,
    application,
    messageName: result.messageNameSource === 'ingest-response' ? result.messageName : null,
    derivedMessageName:
      result.messageNameSource === 'derived-sha256-unverified' ? result.messageName : null,
    observation: observation?.id ? `Observation/${observation.id}` : null,
    source: 'audit-event',
  };
  return {
    attempt: entry,
    messageSent: options.messageSent === undefined ? result.messageSent : options.messageSent,
    auditEvent: { resourceType: 'AuditEvent', id: entry.id } as CuffAttemptTrace['auditEvent'],
    observation,
    provenance: observation
      ? ({ resourceType: 'Provenance', id: 'prov-1' } as CuffAttemptTrace['provenance'])
      : null,
  };
}

/** A hand-made sample list in cuff-sim's shape: one per class, plus an MLLP-only one. */
export const SAMPLES: MalformedSample[] = [
  {
    name: 'invalid_missing_pid3',
    label: 'PID-3 is empty.',
    class: 'structurally_invalid',
    transports: ['mllp', 'https'],
  },
  {
    name: 'unparseable_no_msh',
    label: 'The MSH segment is missing.',
    class: 'unparseable',
    transports: ['mllp', 'https'],
  },
  {
    name: 'noack_missing_end_block',
    label: 'The end bytes are never sent.',
    class: 'no_acknowledgment',
    transports: ['mllp'],
  },
  {
    name: 'unmappable_missing_obx14',
    label: 'OBX-14 is empty.',
    class: 'parses_but_cannot_map',
    transports: ['mllp', 'https'],
  },
];

export interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
