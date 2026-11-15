/**
 * Rows for this directory's tests, in the proxy's shapes (messages.py `attempt_row`,
 * `store_only_row`, `unreadable_row`, `groups_from_provenances`). Synthetic values only.
 */
import type {
  FhirTransactionEntry,
  SendAttemptEntry,
  UnreadableSendAttemptEntry,
} from '../api/contract';

export const STORE = 'projects/p/locations/l/datasets/d/hl7V2Stores/s';

export function attempt(over: Partial<SendAttemptEntry> = {}): SendAttemptEntry {
  return {
    kind: 'cuff-send',
    id: 'att-1',
    sentAt: '2026-03-10T14:17:00.000Z',
    recordedAt: '2026-03-10T14:17:01.000Z',
    transport: 'mllp',
    controlId: 'C0FFEE0000000000000A',
    result: 'accepted',
    outcome: '0',
    sample: null,
    commitAck: { received: true, code: 'AA', controlId: null, reason: null, raw: 'MSH|...' },
    application: { outcome: 'accepted', reason: null },
    messageName: null,
    derivedMessageName: `${STORE}/messages/abc=`,
    messageLookup: 'confirmed',
    observation: 'Observation/bp-1',
    source: 'audit-event',
    possibleDuplicateOf: null,
    ...over,
  };
}

export const rejected = (over: Partial<SendAttemptEntry> = {}) =>
  attempt({
    id: 'att-ae',
    result: 'rejected',
    outcome: '4',
    commitAck: {
      received: true,
      code: 'AE',
      controlId: null,
      reason: 'Invalid MSH header',
      raw: 'MSA|AE||Invalid MSH header',
    },
    application: null,
    derivedMessageName: null,
    messageLookup: null,
    observation: null,
    ...over,
  });

export const noAckRow = (over: Partial<SendAttemptEntry> = {}) =>
  attempt({
    id: 'att-noack',
    result: 'no-acknowledgment',
    outcome: '8',
    commitAck: {
      received: false,
      why: 'timeout',
      httpStatus: null,
      detail: 'No acknowledgment arrived before the deadline.',
      replyPrefixBase64: null,
    },
    application: null,
    derivedMessageName: null,
    messageLookup: null,
    observation: null,
    ...over,
  });

export function storeOnly(over: Partial<SendAttemptEntry> = {}): SendAttemptEntry {
  const name = `${STORE}/messages/real=`;
  return attempt({
    id: name,
    sentAt: '2026-03-10T14:15:00.000Z',
    recordedAt: '2026-03-10T14:15:02.000Z',
    transport: null,
    commitAck: { received: true, code: 'AA', controlId: null, reason: null, raw: null },
    messageName: name,
    derivedMessageName: null,
    source: 'store-only',
    observation: 'Observation/bp-2',
    ...over,
  });
}

export function unreadable(
  over: Partial<UnreadableSendAttemptEntry> = {},
): UnreadableSendAttemptEntry {
  return {
    kind: 'unreadable-send-attempt',
    id: 'aud-x',
    recordedAt: '2026-03-10T12:00:00.000Z',
    problem: 'written in format version 2',
    ...over,
  };
}

export function transaction(over: Partial<FhirTransactionEntry> = {}): FhirTransactionEntry {
  return {
    kind: 'fhir-transaction',
    id: 'tx-1',
    recordedAt: '2026-03-10T13:00:00.000Z',
    writer: 'health-sync',
    httpStatus: null,
    resources: ['Observation/hr-1', 'Observation/hr-2', 'Provenance/p-1', 'Provenance/p-2'],
    ...over,
  };
}
