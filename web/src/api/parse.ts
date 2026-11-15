/**
 * Turning the proxy's JSON (the WIRE shape) into the types in contract.ts (the PARSED shape),
 * where the two differ or where a value from another system must not be trusted blindly.
 *
 * The rule throughout: a value the page does not recognise must never turn into a stronger claim
 * than the server made. An unknown connection state becomes `unknown` (not `connected`), an
 * unknown trace source kind becomes `unknown` (neither `original` nor `reconstructed`), and an
 * unknown message-name source is treated as derived, not as confirmed by the store.
 *
 * Pure functions, no network; http-client.ts calls them and http-client.test.ts tests them
 * through the client.
 */
import type {
  AckCode,
  CommitAck,
  CuffSendResult,
  NoAckReason,
  Transport,
  GoogleHealthConnection,
  GoogleHealthRollUpWindow,
  GoogleHealthSource,
  GoogleHealthSourceKind,
  MessageNameSource,
  MllpFraming,
  Reachability,
  SendResultKind,
  StatusSummary,
  WatchSyncNote,
  WatchSyncStatus,
} from './contract';
import { ApiError } from './errors';

const CONNECTION_STATES: readonly GoogleHealthConnection[] = [
  'connected',
  'reconnect-needed',
  'not-configured',
  'unknown',
];
const NOTES: readonly WatchSyncNote[] = ['backfill-needed', 'refresh-token-rotated'];
const SOURCE_KINDS: readonly GoogleHealthSourceKind[] = ['reconstructed', 'original'];
const RESULTS: readonly SendResultKind[] = ['accepted', 'rejected', 'no-acknowledgment'];

/** T31's mapping: AA/CA accepted, any other MSA-1 rejected, no readable ACK no-acknowledgment. */
function resultFromAck(ack: unknown): SendResultKind {
  if (!isObject(ack) || ack.received !== true) return 'no-acknowledgment';
  return ack.code === 'AA' || ack.code === 'CA' ? 'accepted' : 'rejected';
}

const FRAMINGS: readonly MllpFraming[] = ['standard', 'missing_end_block', 'missing_final_cr'];

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function oneOf<T extends string>(allowed: readonly T[], value: unknown): T | null {
  return allowed.find((a) => a === value) ?? null;
}

/** `watchSync` from `GET /status`, with every field that comes from health-sync checked. */
export function parseWatchSync(wire: unknown): WatchSyncStatus {
  const w = isObject(wire) ? wire : {};
  const stranded = w.strandedPreliminary;
  return {
    // The proxy sends `state`; health-sync's own name for it, `googleHealth`, is read when
    // `state` is absent, so a /status body passed through unchanged also works.
    state: oneOf(CONNECTION_STATES, 'state' in w ? w.state : w.googleHealth) ?? 'unknown',
    reason: stringOrNull(w.reason),
    notes: Array.isArray(w.notes)
      ? [...new Set(w.notes.flatMap((n) => oneOf(NOTES, n) ?? []))]
      : [],
    // A count is a whole number of windows; anything else is "not known" (null), never zero.
    strandedPreliminary:
      typeof stranded === 'number' && Number.isInteger(stranded) && stranded >= 0 ? stranded : null,
    checkedAt: stringOrNull(w.checkedAt) ?? '',
    lastSyncAt: stringOrNull(w.lastSyncAt),
  };
}

function count(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 0;
}

function reachability(value: unknown): Reachability {
  const r = isObject(value) ? value : {};
  return {
    // Only an explicit true is "reachable"; a missing or odd value is not a claim of health.
    reachable: r.reachable === true,
    checkedAt: stringOrNull(r.checkedAt) ?? '',
    detail: stringOrNull(r.detail) ?? (r.reachable === true ? null : 'not reported'),
  };
}

/**
 * `GET /status`. Never throws on an odd body (for example JSON `null`): every part is read from
 * a checked object, so a missing part reads as "not known" (`unknown`, unreachable, 0).
 */
export function parseStatus(wire: unknown): StatusSummary {
  const body = isObject(wire) ? wire : {};
  const counts = isObject(body.observationCounts) ? body.observationCounts : {};
  return {
    watchSync: parseWatchSync(body.watchSync),
    observationCounts: { watch: count(counts.watch), cuff: count(counts.cuff) },
    fhirStore: reachability(body.fhirStore),
    openemr: reachability(body.openemr),
  };
}

function isRollUpWindow(value: unknown): value is GoogleHealthRollUpWindow {
  return (
    isObject(value) &&
    typeof value.startTime === 'string' &&
    typeof value.endTime === 'string' &&
    isObject(value.heartRate)
  );
}

/**
 * The `source` of `GET /trace/heart-rate/{id}`: `{"kind", "window"}` or null. A missing or
 * unrecognised `kind` is `unknown`; something that is not a roll-up window is null.
 */
export function parseHeartRateSource(wire: unknown): GoogleHealthSource | null {
  if (!isObject(wire) || !isRollUpWindow(wire.window)) return null;
  return { kind: oneOf(SOURCE_KINDS, wire.kind) ?? 'unknown', window: wire.window };
}

const ACK_CODES: readonly AckCode[] = ['AA', 'AE', 'AR', 'CA', 'CE', 'CR'];
const NO_ACK_REASONS: readonly NoAckReason[] = [
  'timeout',
  'connection-closed',
  'api-error',
  'unreadable-reply',
];
const TRANSPORTS: readonly Transport[] = ['mllp', 'https'];

/** A 200 from the cuff route whose body the page cannot read: a proxy-side fault, so 502. */
function unreadableResult(what: string): ApiError {
  return new ApiError(
    502,
    `The send answered, but its result could not be read (${what}). It is not known what the store said.`,
  );
}

/** `ack` exactly as cuff-sim documents it, or a thrown 502: the page never guesses an ACK. */
function parseCommitAck(wire: unknown): CommitAck {
  if (!isObject(wire)) throw unreadableResult('no ack');
  if (wire.received === true) {
    const code = oneOf(ACK_CODES, wire.code);
    if (code === null) throw unreadableResult('ack without a known MSA-1 code');
    return {
      received: true,
      code,
      controlId: stringOrNull(wire.controlId),
      reason: stringOrNull(wire.reason),
      raw: stringOrNull(wire.raw),
    };
  }
  if (wire.received === false) {
    const why = oneOf(NO_ACK_REASONS, wire.why);
    if (why === null) throw unreadableResult('no-acknowledgment without a known reason');
    return {
      received: false,
      why,
      httpStatus: typeof wire.httpStatus === 'number' ? wire.httpStatus : null,
      detail: stringOrNull(wire.detail) ?? '',
      replyPrefixBase64: stringOrNull(wire.replyPrefixBase64),
    };
  }
  throw unreadableResult('ack without received');
}

function parseAudit(wire: unknown): CuffSendResult['audit'] {
  if (isObject(wire) && wire.recorded === true) return { recorded: true };
  if (isObject(wire) && wire.recorded === false) {
    return { recorded: false, detail: stringOrNull(wire.detail) ?? 'no detail given' };
  }
  throw unreadableResult('no audit');
}

/**
 * The 200 body of `POST /cuff/readings`. The fields the page builds a claim on are checked:
 * `ack` and `audit` must have cuff-sim's documented shapes, and `transport`, `sentAt` and
 * `messageSent` must be there; otherwise this throws `ApiError(502)` rather than letting a
 * half-read result reach the page. `result`, `attemptId`, `framing` and the message name are
 * normalised as before; anything else is cuff-sim's JSON as it is.
 */
export function parseCuffSendResult(wire: unknown): CuffSendResult {
  if (!isObject(wire)) throw unreadableResult('not a JSON object');
  const body = wire as unknown as CuffSendResult & Record<string, unknown>;
  const ack = parseCommitAck(body.ack);
  const audit = parseAudit(body.audit);
  const transport = oneOf(TRANSPORTS, body.transport);
  if (transport === null) throw unreadableResult('no transport');
  if (typeof body.sentAt !== 'string') throw unreadableResult('no sentAt');
  if (typeof body.messageSent !== 'string') throw unreadableResult('no messageSent');
  const messageName = stringOrNull(body.messageName);
  // Only the store's own answer counts as confirmed; any other source of a name is "derived".
  const messageNameSource: MessageNameSource | null =
    messageName === null
      ? null
      : body.messageNameSource === 'ingest-response'
        ? 'ingest-response'
        : 'derived-sha256-unverified';
  return {
    ...body,
    ack,
    audit,
    transport,
    // An unknown `result` is not passed on: it is worked out from the ACK, as T31 does.
    result: oneOf(RESULTS, body.result) ?? resultFromAck(ack),
    attemptId: stringOrNull(body.attemptId),
    framing: oneOf(FRAMINGS, body.framing),
    messageName,
    messageNameSource,
  };
}
