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
  AuthConfig,
  ApplicationOutcome,
  CommitAck,
  CuffSendResult,
  FhirTransactionEntry,
  MessageLookup,
  MessagesLogEntry,
  MessagesLogPage,
  NoAckReason,
  SendAttemptEntry,
  SentAndStored,
  ShareResult,
  ShareQuery,
  ShareStep,
  ShareStepResult,
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
import { SHARE_URL_HIDDEN } from './contract';
import { ApiError } from './errors';
import type { Encounter, Observation, Patient, Resource } from './fhir';

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

/** A count is a whole number; anything else (null included) is "not known", never zero. */
function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
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
 * a checked object, so a missing part reads as "not known" (`unknown`, unreachable, a null
 * count: amendment 15, never 0).
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

// ---------------------------------------------------------------------------------------------
// The messages log (GET /messages) and the cuff trace's `attempt` row

const LOOKUPS: readonly Exclude<MessageLookup, null>[] = ['confirmed', 'not-found', 'failed'];
const OUTCOMES: readonly ApplicationOutcome['outcome'][] = ['accepted', 'error', 'pending'];
const WRITERS: readonly FhirTransactionEntry['writer'][] = [
  'health-sync',
  'v2-to-fhir',
  'seed',
  'unknown',
];
const ROW_OUTCOMES: readonly SendAttemptEntry['outcome'][] = ['0', '4', '8'];

function parseApplication(wire: unknown): ApplicationOutcome | null {
  if (!isObject(wire)) return null;
  // An outcome the page does not know is "not mapped yet", never a stronger claim.
  const outcome = oneOf(OUTCOMES, wire.outcome) ?? 'pending';
  return { outcome, reason: outcome === 'error' ? stringOrNull(wire.reason) : null };
}

/** A log row's commit-level acknowledgment; one that cannot be read is "no acknowledgment". */
function parseRowAck(wire: unknown): CommitAck {
  try {
    return parseCommitAck(wire);
  } catch {
    return {
      received: false,
      why: 'unreadable-reply',
      httpStatus: null,
      detail: 'The proxy sent an acknowledgment this page could not read.',
      replyPrefixBase64: null,
    };
  }
}

/**
 * One `cuff-send` row (an attempt, or a store-only message), with every value the page builds a
 * claim on checked. The rules (amendments 1, 5 and 14):
 * - `messageLookup` is one of the three codes or null; an ACCEPTED audit-event row whose lookup
 *   is missing or unknown is treated as `failed` (not confirmed);
 * - only a `confirmed` lookup carries an application-level outcome and an Observation: for any
 *   other lookup both are null, whatever the body said;
 * - `possibleDuplicateOf` is kept only on a store-only row;
 * - a row without an id or a send time cannot be shown and is dropped (null).
 */
export function parseSendAttemptEntry(wire: unknown): SendAttemptEntry | null {
  if (!isObject(wire) || wire.kind !== 'cuff-send') return null;
  const id = stringOrNull(wire.id);
  const sentAt = stringOrNull(wire.sentAt);
  if (id === null || id === '' || sentAt === null) return null;
  const commitAck = parseRowAck(wire.commitAck);
  const source = wire.source === 'store-only' ? 'store-only' : 'audit-event';
  const result = oneOf(RESULTS, wire.result) ?? resultFromAck(commitAck);
  let messageLookup: MessageLookup = oneOf(LOOKUPS, wire.messageLookup);
  if (messageLookup === null && result === 'accepted') messageLookup = 'failed';
  if (source === 'store-only') messageLookup = 'confirmed';
  const confirmed = messageLookup === 'confirmed';
  const observation = stringOrNull(wire.observation);
  return {
    kind: 'cuff-send',
    id,
    sentAt,
    recordedAt: stringOrNull(wire.recordedAt) ?? sentAt,
    transport: oneOf(TRANSPORTS, wire.transport),
    controlId: stringOrNull(wire.controlId),
    result,
    outcome:
      oneOf(ROW_OUTCOMES, wire.outcome) ??
      (result === 'accepted' ? '0' : result === 'rejected' ? '4' : '8'),
    sample: stringOrNull(wire.sample),
    commitAck,
    application: confirmed ? parseApplication(wire.application) : null,
    messageName: stringOrNull(wire.messageName),
    derivedMessageName: stringOrNull(wire.derivedMessageName),
    messageLookup,
    observation: confirmed && observation?.startsWith('Observation/') ? observation : null,
    source,
    possibleDuplicateOf: source === 'store-only' ? stringOrNull(wire.possibleDuplicateOf) : null,
  };
}

function parseLogEntry(wire: unknown): MessagesLogEntry | null {
  if (!isObject(wire)) return null;
  if (wire.kind === 'cuff-send') return parseSendAttemptEntry(wire);
  if (wire.kind === 'unreadable-send-attempt') {
    return {
      kind: 'unreadable-send-attempt',
      // Amendment 6: "" when the AuditEvent had no id.
      id: stringOrNull(wire.id) ?? '',
      recordedAt: stringOrNull(wire.recordedAt),
      problem: stringOrNull(wire.problem) ?? 'The proxy gave no description of the problem.',
    };
  }
  if (wire.kind === 'fhir-transaction') {
    const id = stringOrNull(wire.id);
    const recordedAt = stringOrNull(wire.recordedAt);
    if (id === null || recordedAt === null) return null;
    return {
      kind: 'fhir-transaction',
      id,
      recordedAt,
      writer: oneOf(WRITERS, wire.writer) ?? 'unknown',
      httpStatus:
        typeof wire.httpStatus === 'number' && Number.isInteger(wire.httpStatus)
          ? wire.httpStatus
          : null,
      resources: Array.isArray(wire.resources)
        ? wire.resources.filter((r): r is string => typeof r === 'string')
        : [],
    };
  }
  return null; // a row kind this page does not know: not shown (counted in `dropped`)
}

/**
 * `GET /messages`. Rows the page cannot read are dropped rather than guessed, and a page that
 * dropped any is marked `partial` (it is then incomplete as far as the page is concerned).
 * `partial` is true unless the body says exactly `false`: a body that does not say is not a
 * claim that the page is complete.
 */
export function parseMessagesPage(wire: unknown): MessagesLogPage {
  if (!isObject(wire) || !Array.isArray(wire.entries)) {
    throw new ApiError(502, 'The messages log answered, but its page could not be read.');
  }
  const entries = wire.entries.map(parseLogEntry);
  const kept = entries.filter((e): e is MessagesLogEntry => e !== null);
  const unreadable = wire.unreadable;
  return {
    entries: kept,
    nextCursor: stringOrNull(wire.nextCursor),
    unreadable:
      typeof unreadable === 'number' && Number.isInteger(unreadable) && unreadable >= 0
        ? unreadable
        : kept.filter((e) => e.kind === 'unreadable-send-attempt').length,
    partial: wire.partial !== false || kept.length !== entries.length,
  };
}

// ---------------------------------------------------------------------------------------------
// The share (POST /share), specified for T15 in contract.ts and web/README.md

const STEP_RESULTS: readonly Exclude<ShareStepResult, 'unknown'>[] = [
  'ok',
  'reused',
  'found',
  'not-found',
  'already-shared',
  'failed',
];

/** A path as `extensions.trace_step` requires: starts with one `/`, no scheme, host or query. */
function isPathOnly(url: unknown): url is string {
  return typeof url === 'string' && /^\/(?!\/)[^?#\s]*$/.test(url);
}

/** A search step's parameters: names to a string or strings; anything else is left out. */
function parseQuery(wire: unknown): ShareQuery | null {
  if (!isObject(wire)) return null;
  const out: ShareQuery = {};
  for (const [name, value] of Object.entries(wire)) {
    if (typeof value === 'string') out[name] = value;
    else if (Array.isArray(value) && value.every((v) => typeof v === 'string')) {
      out[name] = value;
    }
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** One trace step; null when it is not an object. Anything odd is shown as not known. */
function parseShareStep(wire: unknown): ShareStep | null {
  if (!isObject(wire)) return null;
  const request = isObject(wire.request) ? wire.request : {};
  const response = isObject(wire.response) ? wire.response : {};
  const status = response.status;
  const duration = wire.durationMs;
  return {
    step: stringOrNull(wire.step) ?? 'unknown',
    label: stringOrNull(wire.label) ?? 'A step the proxy did not describe',
    request: {
      method: stringOrNull(request.method) ?? '?',
      // Never a host: a url that is not a path is not shown at all.
      url: isPathOnly(request.url) ? request.url : SHARE_URL_HIDDEN,
      query: parseQuery(request.query),
      body: request.body ?? null,
    },
    response: {
      status: typeof status === 'number' && Number.isInteger(status) ? status : 0,
      body: response.body ?? null,
    },
    result: oneOf(STEP_RESULTS, wire.result) ?? 'unknown',
    durationMs: typeof duration === 'number' && duration >= 0 ? duration : 0,
  };
}

/** The `steps` array of a share answer or a share failure; [] when there is none. */
export function parseShareSteps(wire: unknown): ShareStep[] {
  return Array.isArray(wire)
    ? wire.map(parseShareStep).filter((s): s is ShareStep => s !== null)
    : [];
}

function resourceOf<T extends Resource>(wire: unknown, type: T['resourceType']): T | null {
  return isObject(wire) && wire.resourceType === type ? (wire as T) : null;
}

function sentAndStored<T extends Resource>(
  wire: unknown,
  type: T['resourceType'],
): SentAndStored<T> {
  const w = isObject(wire) ? wire : {};
  return { sent: resourceOf<T>(w.sent, type), stored: resourceOf<T>(w.stored, type) };
}

/**
 * A 200 from the share route that the page cannot read. The proxy may have done any part of
 * the share, so this never says that nothing was sent.
 */
export const UNREADABLE_SHARE =
  'The share answered, but its answer could not be read, so it is not known what reached the clinic.';

/**
 * The 200 body of `POST /share`. `outcome` must be `shared` or `already-shared` (a 200 means the
 * reading is in OpenEMR); anything else throws `ApiError(502)`, so the page never reports a
 * share it cannot read as done. Read-back resources of the wrong type are null.
 */
export function parseShareResult(wire: unknown): ShareResult {
  if (!isObject(wire)) throw new ApiError(502, UNREADABLE_SHARE);
  const outcome =
    wire.outcome === 'shared' || wire.outcome === 'already-shared' ? wire.outcome : null;
  if (outcome === null) throw new ApiError(502, UNREADABLE_SHARE);
  const readBack = isObject(wire.readBack) ? wire.readBack : {};
  return {
    outcome,
    steps: parseShareSteps(wire.steps),
    readBack: {
      patient: sentAndStored<Patient>(readBack.patient, 'Patient'),
      encounter: sentAndStored<Encounter>(readBack.encounter, 'Encounter'),
      observation: sentAndStored<Observation>(readBack.observation, 'Observation'),
    },
  };
}

/**
 * reCAPTCHA key ids are 40 characters of letters, digits, `-` and `_` today; the bounds are
 * looser so a longer id still works, while anything that could change the script URL's meaning
 * (`&`, `/`, `?`, spaces) is refused before it gets there.
 */
const SITE_KEY = /^[A-Za-z0-9_-]{20,100}$/;
/** reCAPTCHA action names may contain only letters, digits, `/` and `_` (Google's docs). */
const ACTION = /^[A-Za-z0-9/_]{1,100}$/;

/**
 * `GET /auth/config`'s body, `{"recaptcha_site_key", "recaptcha_action"}`, checked. Throws on any
 * other shape: a page that cannot load the bot check must say so, not guess a key.
 */
export function parseAuthConfig(body: unknown): AuthConfig {
  const b = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>;
  const key = b.recaptcha_site_key;
  const action = b.recaptcha_action;
  if (typeof key !== 'string' || !SITE_KEY.test(key)) {
    throw new Error('auth/config: recaptcha_site_key is missing or not a key id');
  }
  if (typeof action !== 'string' || !ACTION.test(action)) {
    throw new Error('auth/config: recaptcha_action is missing or not an action name');
  }
  return { recaptchaSiteKey: key, recaptchaAction: action };
}
