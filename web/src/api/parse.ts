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
  CuffSendResult,
  GoogleHealthConnection,
  GoogleHealthRollUpWindow,
  GoogleHealthSource,
  GoogleHealthSourceKind,
  MessageNameSource,
  MllpFraming,
  StatusSummary,
  WatchSyncNote,
  WatchSyncStatus,
} from './contract';

const CONNECTION_STATES: readonly GoogleHealthConnection[] = [
  'connected',
  'reconnect-needed',
  'not-configured',
  'unknown',
];
const NOTES: readonly WatchSyncNote[] = ['backfill-needed', 'refresh-token-rotated'];
const SOURCE_KINDS: readonly GoogleHealthSourceKind[] = ['reconstructed', 'original'];
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
    state: oneOf(CONNECTION_STATES, w.state) ?? 'unknown',
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

/** `GET /status`. The parts that are our own views (counts, reachability) pass through. */
export function parseStatus(wire: unknown): StatusSummary {
  const body = (isObject(wire) ? wire : {}) as unknown as StatusSummary;
  return { ...body, watchSync: parseWatchSync((wire as { watchSync?: unknown }).watchSync) };
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

/**
 * The 200 body of `POST /cuff/readings`. Only the fields whose meaning the page relies on for a
 * claim are normalised; the rest is cuff-sim's JSON as it is.
 */
export function parseCuffSendResult(wire: unknown): CuffSendResult {
  const body = (isObject(wire) ? wire : {}) as unknown as CuffSendResult & Record<string, unknown>;
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
    attemptId: stringOrNull(body.attemptId),
    framing: oneOf(FRAMINGS, body.framing),
    messageName,
    messageNameSource,
  };
}
