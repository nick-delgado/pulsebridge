/**
 * The contract between the dashboard and fhir-proxy: every call the page makes, as one
 * TypeScript interface (`PulseBridgeApi`) with two implementations, `HttpClient` (the real proxy)
 * and `MockClient` (synthetic data, no network).
 *
 * The page calls exactly one backend, fhir-proxy (D10). The HTTP routes behind each method are
 * T16's proposal for T14 and T15 to implement; they are listed in web/README.md ("HTTP contract").
 * Where the data is FHIR the proxy returns FHIR (resources, Bundles, OperationOutcome); the
 * non-FHIR shapes below are views the proxy assembles (status, send results, the messages log).
 */
import type {
  AuditEvent,
  Device,
  Encounter,
  LoincCode,
  Observation,
  Patient,
  Provenance,
} from './fhir';

// ---------------------------------------------------------------------------------------------
// Login (D9, docs/spikes/s6.md)

export interface LoginResult {
  /** Opaque signed bearer token. The page never decodes it. */
  token: string;
  /** ISO 8601 instant; 8 hours after login by default. */
  expiresAt: string;
}

// ---------------------------------------------------------------------------------------------
// Status strip

export interface Reachability {
  reachable: boolean;
  /** When the proxy last checked, ISO 8601. */
  checkedAt: string;
  /** Short human-readable reason when not reachable. */
  detail: string | null;
}

/**
 * The Google Health connection state, exactly as health-sync's `GET /status` names it
 * (`googleHealth`, services/health-sync/README.md). health-sync answers HTTP 200 in every state:
 * - `connected`: a token refresh just succeeded;
 * - `reconnect-needed`: the refresh token expired or was revoked, the OAuth client was refused,
 *   or the scope was not granted (T10); the deployer must re-run the connect script (T11);
 * - `not-configured`: a Google Health secret is missing or unusable (for example no connect yet);
 * - `unknown`: the check could not finish (Google's token endpoint busy or unreachable). This is
 *   not an error of ours and not a disconnection; the last sync time still stands.
 * The HTTP client turns any other value into `unknown`, so a new state cannot claim "connected".
 */
export type GoogleHealthConnection =
  'connected' | 'reconnect-needed' | 'not-configured' | 'unknown';

/**
 * Non-fatal facts from health-sync's `/status` `notes` that the status strip shows:
 * - `backfill-needed`: `strandedPreliminary` windows are still `preliminary` but older than any
 *   future run's range (runs were missed); `health-sync-backfill` finalises them;
 * - `refresh-token-rotated`: Google issued a new refresh token that health-sync cannot store;
 *   reconnecting soon keeps the connection working.
 * Unknown note codes are dropped by the HTTP client (they would have no words to show).
 */
export type WatchSyncNote = 'backfill-needed' | 'refresh-token-rotated';

export interface WatchSyncStatus {
  /**
   * health-sync's `googleHealth`. The proxy should send it as `state`; the HTTP client also
   * reads `googleHealth` when `state` is absent, so health-sync's body can be passed through.
   */
  state: GoogleHealthConnection;
  /**
   * health-sync's `reason` code, for example `refresh-token-invalid` or
   * `secrets-missing:<names>`; null when connected. A code, not a sentence: shown as detail text.
   */
  reason: string | null;
  notes: WatchSyncNote[];
  /**
   * How many windows the most recent sync run on that health-sync instance found stranded as
   * `preliminary`. Null when no run has counted since the instance started (normal after a cold
   * start with scale to zero) or the count failed; null is "not known", never zero.
   */
  strandedPreliminary: number | null;
  /** When health-sync checked the connection (its `checkedAt`), ISO 8601. */
  checkedAt: string;
  /**
   * The time of the NEWEST HEART-RATE WRITE to the FHIR store, ISO 8601; null when there has
   * been none. It is not the time of the last sync run: health-sync writes only windows that
   * changed, so while the watch is off the wrist (or a run finds nothing new) this does not
   * move. The strip therefore labels it "Newest watch data". NOT part of health-sync's
   * `/status` (which is stateless): the proxy derives it from the store (README open question
   * 9). The field keeps its name so T17's code is unchanged; read it as "last data written".
   * The status strip announces a `sync` data change when it changes between two loads, which
   * is exactly when there is new data to re-fetch.
   */
  lastSyncAt: string | null;
}

export interface StatusSummary {
  watchSync: WatchSyncStatus;
  /** Observations in the FHIR store, per source. */
  observationCounts: { watch: number; cuff: number };
  fhirStore: Reachability;
  openemr: Reachability;
}

// ---------------------------------------------------------------------------------------------
// Observations

/**
 * A search range, sent as `date=gt<start>&date=lt<end>` (both bounds exclusive). With the R4
 * rules for a prefix on a Period, a heart-rate window matches when its effectivePeriod overlaps
 * (start, end): it ends after `start` and starts before `end`. So an aligned 24 h range returns
 * 288 five-minute windows, not 289 (with `ge`, the window ending exactly at `start` would match).
 * A blood-pressure reading (effectiveDateTime) matches when start < time < end.
 */
export interface TimeRange {
  start: Date;
  end: Date;
}

export interface ObservationQuery {
  /** LOINC code: 8867-4 heart rate, 85354-9 blood-pressure panel. */
  code: LoincCode;
  range: TimeRange;
}

/** The Google Health roll-up window a heart-rate Observation was mapped from (docs/spikes/s3.md). */
export interface GoogleHealthRollUpWindow {
  startTime: string;
  endTime: string;
  heartRate: { beatsPerMinuteMin: number; beatsPerMinuteAvg: number; beatsPerMinuteMax: number };
}

/**
 * Where the roll-up window in a trace came from:
 * - `reconstructed`: rebuilt by the proxy from the stored Observation (its effectivePeriod,
 *   average, minimum and maximum), in the shape the API returns. Today's only case: PulseBridge
 *   keeps no raw Google Health responses (README open question 2).
 * - `original`: the window exactly as the Google Health API returned it, if a later task stores
 *   it (for example as a `Binary` referenced from the Provenance).
 * - `unknown`: the proxy sent a value this page does not know (or none). The page must then make
 *   no claim either way. Only the HTTP client produces it; the proxy never sends it.
 */
export type GoogleHealthSourceKind = 'reconstructed' | 'original' | 'unknown';

/** The Google Health side of a heart-rate trace, as the page uses it (the PARSED shape). */
export interface GoogleHealthSource {
  kind: GoogleHealthSourceKind;
  window: GoogleHealthRollUpWindow;
}

/**
 * A heart-rate trace as the client returns it (the PARSED shape). On the wire the proxy sends
 * `{"resources": Bundle, "source": {"kind": "reconstructed" | "original", "window": {...}} | null}`
 * and the HTTP client converts it: the Bundle's entries become `observation`, `provenance` and
 * `device`, and an unrecognised `kind` becomes `unknown` (see web/README.md, "Wire shape and
 * parsed shape").
 */
export interface HeartRateTrace {
  observation: Observation;
  provenance: Provenance | null;
  device: Device | null;
  /** Null when the proxy cannot supply the source window, or sent one that is not a window. */
  source: GoogleHealthSource | null;
}

// ---------------------------------------------------------------------------------------------
// Cuff simulator (T13, forwarded by the proxy, D10)

export type Transport = 'mllp' | 'https';

/**
 * The limits the cuff simulator itself enforces (services/cuff-sim/README.md, "POST
 * /cuff/readings"), which are T07's builder limits: JSON integers, each 1 to 400 mm[Hg], and
 * systolic strictly above diastolic. Anything else is refused with 422 and nothing is sent.
 *
 * These are deliberately wide. The mapper (T12) accepts only systolic 50-300 and diastolic
 * 20-200 (`MAPPER_PLAUSIBLE_RANGE`): a reading between the two is sent, stored with `AA`, and
 * then gets the application-level outcome `error` with reason `out-of-range`, one of the D7
 * layers the demo can show. A form may offer narrower inputs, but must not call them the
 * service's limits.
 */
export const CUFF_SERVICE_LIMITS = {
  /** mm[Hg], inclusive, for both values. */
  min: 1,
  max: 400,
  systolicAboveDiastolic: true,
} as const;

/**
 * The mapper's plausibility bounds (T12, `pulsebridge_fhir.inputs` SYSTOLIC_RANGE and
 * DIASTOLIC_RANGE), inclusive, mm[Hg]. Information only: the cuff simulator does not enforce them.
 */
export const MAPPER_PLAUSIBLE_RANGE = {
  systolic: { min: 50, max: 300 },
  diastolic: { min: 20, max: 200 },
} as const;

/**
 * The malformed classes from the work plan's "Malformed message handling" table (D7), spelled
 * exactly as `classes` in libs/pulsebridge_hl7v2/samples/samples.json (T07).
 */
export type MalformedClass =
  'structurally_invalid' | 'unparseable' | 'no_acknowledgment' | 'parses_but_cannot_map';

/**
 * A synthetic sample from libs/pulsebridge_hl7v2/samples that "send malformed" can send: one row
 * of cuff-sim's `GET /cuff/samples`, `[{"name", "label", "class", "transports"}]`, which lists
 * every T07 sample except the `valid` class.
 */
export interface MalformedSample {
  /** Sample name exactly as in samples.json, for example `unparseable_no_msh`. */
  name: string;
  /** The manifest's `description`, a sentence saying what is wrong with the sample. */
  label: string;
  class: MalformedClass;
  /** Bad-framing samples exist only on MLLP. */
  transports: Transport[];
}

/**
 * The body of `POST /cuff/readings`, forwarded unchanged to the cuff simulator (D10).
 * - A built reading: `systolic` and `diastolic` are required integers within
 *   `CUFF_SERVICE_LIMITS`, and `sendMalformed` is null.
 * - A malformed sample: `sendMalformed` is the exact sample name from `listMalformedSamples`
 *   (one that lists this transport). `systolic` and `diastolic` may be left out; they are
 *   ignored, but the service still checks them if present.
 */
export type CuffReadingRequest =
  | { systolic: number; diastolic: number; transport: Transport; sendMalformed: null }
  | { systolic?: number; diastolic?: number; transport: Transport; sendMalformed: string };

/**
 * MSA-1 acknowledgment code (HL7 table 0008). AA/AE/AR are the original-mode codes the store
 * actually sends; CA/CE/CR are the enhanced-mode commit codes, included so a future ACK (or T31's
 * AuditEvent model) can be represented.
 */
export type AckCode = 'AA' | 'AE' | 'AR' | 'CA' | 'CE' | 'CR';

/**
 * Why no readable ACK came back: T31's closed set (`no-ack-reason` in the AuditEvent).
 * `api-error` is an HTTPS ingest error with an HTTP status 400-599 and no hl7Nack;
 * `unreadable-reply` means bytes came back that are not a readable HL7v2 ACK.
 */
export type NoAckReason = 'timeout' | 'connection-closed' | 'api-error' | 'unreadable-reply';

/**
 * The commit-level acknowledgment (D7): what the HL7v2 store (through the MLLP adapter, or the
 * HTTPS ingest response) said about storing the message. Either an ACK was received, or not.
 */
export type CommitAck =
  | {
      received: true;
      /** MSA-1. The store answers AA or AE; it never sends AR (spike S4). */
      code: AckCode;
      /**
       * MSA-2, the control ID acknowledged, when known: in a send result (T13). Null on a NACK
       * the store could not read, and in a log row built from an AuditEvent (T31 keeps MSH-10 of
       * the attempt, which is the row's `controlId`, not MSA-2).
       */
      controlId: string | null;
      /** MSA-3 text as T31 stores it (cleaned), for example "Invalid MSH header". */
      reason: string | null;
      /**
       * The ACK as received, DECODED for display: segments separated by "\r". Convenience only:
       * the AuditEvent stores it as `ack-message` valueBase64Binary, and the proxy decodes it.
       * It came from another system, so render it as text only. Null for a store-only row.
       */
      raw: string | null;
    }
  | {
      received: false;
      why: NoAckReason;
      /** HTTP status when `why` is `api-error` (400-599); otherwise null. */
      httpStatus: number | null;
      /** Short human-readable explanation. */
      detail: string;
      /**
       * For `unreadable-reply`: the first bytes of what came back, base64 as in the AuditEvent
       * (`unreadable-reply-bytes`), because they may not be text at all. Otherwise null.
       */
      replyPrefixBase64: string | null;
    };

/**
 * The application-level outcome (D7): what v2-to-fhir made of a stored message, read from the
 * `application_outcome` and `reason` labels. `pending` means stored but not labelled yet.
 */
export interface ApplicationOutcome {
  outcome: 'accepted' | 'error' | 'pending';
  /** Reason code such as `missing-obx-14`; null when accepted or pending. */
  reason: string | null;
}

/**
 * The MLLP framing a send used (cuff-sim's `framing`): `standard` (start block, message, end
 * block and carriage return), or a malformed sample's deliberately broken framing, sent as T07
 * defines it so the adapter's "no acknowledgment" path can be shown.
 */
export type MllpFraming = 'standard' | 'missing_end_block' | 'missing_final_cr';

/**
 * Where `messageName` came from (cuff-sim's `messageNameSource`):
 * - `ingest-response`: the HL7v2 store returned it (HTTPS `messages:ingest`, accepted). Confirmed.
 * - `derived-sha256-unverified`: over MLLP the ACK carries no name, so for an accepted send the
 *   simulator COMPUTES `<store>/messages/<urlsafe-base64(sha256(message bytes))>` (spike S5
 *   observed this rule on one HTTPS message). It is a derived name, not confirmed by the store:
 *   show it as such, never as the stored message's name (see `isDerivedMessageName`).
 * The HTTP client reads any other value as `derived-sha256-unverified`: only the store's own
 * answer is presented as confirmed.
 */
export type MessageNameSource = 'ingest-response' | 'derived-sha256-unverified';

/** T31's result for a send (cuff-sim's `result`): from the commit-level acknowledgment. */
export type SendResultKind = 'accepted' | 'rejected' | 'no-acknowledgment';

/**
 * The result of one send, in T13's single result shape for both transports
 * (services/cuff-sim/README.md, "The 200 result"). A 200 means a send happened, whatever the
 * store said; refusals before sending are `CuffSendError`s (errors.ts).
 */
export interface CuffSendResult {
  /**
   * Id of the AuditEvent recording this attempt (D12), used for trace lookups. Null only when
   * the AuditEvent could not even be built: there is then nothing to look up, and the attempt
   * is not in the messages log; show this result as it is.
   */
  attemptId: string | null;
  sentAt: string;
  transport: Transport;
  /** MSH-10 of the message sent; null for a sample hl7apy cannot read (no MSH, LF terminators). */
  controlId: string | null;
  /** The malformed sample sent, or null for a built reading. */
  sample: string | null;
  /** The message as sent, ER7 with the sample's own terminators ("\r" for a built reading). */
  messageSent: string;
  /** The MLLP framing used; null over HTTPS (no frame). */
  framing: MllpFraming | null;
  /**
   * HL7v2 store message name. Over HTTPS: from the ingest response when accepted. Over MLLP: a
   * DERIVED name for an accepted (AA) send (`messageNameSource`). Null when rejected or not
   * acknowledged.
   */
  messageName: string | null;
  /** Null exactly when `messageName` is null. */
  messageNameSource: MessageNameSource | null;
  /** accepted (AA/CA), rejected (AE/AR/CE/CR) or no-acknowledgment. */
  result: SendResultKind;
  ack: CommitAck;
  /** Whether the AuditEvent was written. A failure never hides the send result (T13). */
  audit: { recorded: true } | { recorded: false; detail: string };
}

/** True when the result's message name was computed by the simulator, not returned by the store. */
export function isDerivedMessageName(result: Pick<CuffSendResult, 'messageNameSource'>): boolean {
  return result.messageNameSource === 'derived-sha256-unverified';
}

export interface CuffAttemptTrace {
  attempt: SendAttemptEntry;
  /** ER7 as sent, or null when the proxy cannot find it (see README open questions). */
  messageSent: string | null;
  auditEvent: AuditEvent | null;
  observation: Observation | null;
  provenance: Provenance | null;
}

// ---------------------------------------------------------------------------------------------
// Messages log (D12)

/**
 * One cuff send attempt: the fields of T31's `SendAttemptRecord` (parse_send_attempt), read from
 * the send-attempt AuditEvent in the audit FHIR store (D15), plus the application-level outcome
 * joined from the stored message's labels where the send was accepted.
 */
export interface SendAttemptEntry {
  kind: 'cuff-send';
  /** The AuditEvent id (the stored message name for a store-only row). */
  id: string;
  /** When the message was sent (`period.start`). */
  sentAt: string;
  /** When the attempt was recorded (`recorded`); the log sorts on this. */
  recordedAt: string;
  /** Null only for a store-only row, whose transport is unknown. */
  transport: Transport | null;
  /** MSH-10 of the message sent; null for a sample with no MSH. */
  controlId: string | null;
  /** T31's result: AA/CA accepted; AE/AR/CE/CR rejected; anything else no-acknowledgment. */
  result: SendResultKind;
  /** The AuditEvent's R4 outcome code: 0 accepted, 4 rejected, 8 no acknowledgment. */
  outcome: '0' | '4' | '8';
  /** The malformed sample sent (`malformed-sample`), or null. */
  sample: string | null;
  commitAck: CommitAck;
  /** Null when the message was never stored (rejected or no acknowledgment). */
  application: ApplicationOutcome | null;
  /** HL7v2 store message name (HTTPS accept only, or found by the proxy). */
  messageName: string | null;
  /** The resulting Observation as a reference, `Observation/<id>`, once mapped. */
  observation: string | null;
  /** `store-only`: a stored message with no AuditEvent, for example sent straight to the adapter. */
  source: 'audit-event' | 'store-only';
}

/**
 * An AuditEvent that is ours (a cuff send attempt) but could not be read: T31's
 * `UnreadableSendAttempt`, for example a missing value or a newer format version. The proxy
 * surfaces it as a visible error row and never hides it; T19 shows it as such.
 */
export interface UnreadableSendAttemptEntry {
  kind: 'unreadable-send-attempt';
  id: string;
  /** `recorded` when it could be read, else null (sorted last). */
  recordedAt: string | null;
  /** What was wrong, from the parser's message. */
  problem: string;
}

/** One FHIR transaction Bundle written by an ingest service. */
export interface FhirTransactionEntry {
  kind: 'fhir-transaction';
  id: string;
  recordedAt: string;
  writer: 'health-sync' | 'v2-to-fhir';
  /** HTTP status of the executeBundle call, when known. */
  httpStatus: number | null;
  /** References written, `Observation/<id>` and so on. */
  resources: string[];
}

export type MessagesLogEntry = SendAttemptEntry | UnreadableSendAttemptEntry | FhirTransactionEntry;

export interface MessagesLogQuery {
  kind?: MessagesLogEntry['kind'];
  outcome?: ApplicationOutcome['outcome'];
  /** Page size; the proxy caps it. */
  count?: number;
  /** Opaque cursor from a previous page's `nextCursor`. */
  cursor?: string;
}

export interface MessagesLogPage {
  /** Newest first (unreadable rows without a time last). */
  entries: MessagesLogEntry[];
  /** How many AuditEvents on this page were ours but unreadable (also present as rows). */
  unreadable: number;
  nextCursor: string | null;
}

// ---------------------------------------------------------------------------------------------
// Share with clinic (T15)

export interface ShareRequest {
  /** References to Observations in the FHIR store, `Observation/<id>`. Only `final` ones share. */
  observations: string[];
}

export interface ShareStep {
  step:
    | 'token'
    | 'patient-search'
    | 'patient-create'
    | 'encounter-create'
    | 'observation-create'
    | 'read-back';
  label: string;
  /** The request sent to OpenEMR, path only. Never contains the key, assertion or token (T15). */
  request: { method: string; url: string; body: unknown };
  response: { status: number; body: unknown };
  result: 'ok' | 'already-shared' | 'failed';
  durationMs: number;
}

/**
 * The body of a 200 answer from `POST /share`. How failures are reported (proposal):
 * - Refused before anything is sent to OpenEMR (nothing selected, not `final`, or the Patient
 *   is incomplete, for example "Patient incomplete: missing birthDate"): 422 with an
 *   OperationOutcome; the client throws `ApiError` and no steps exist.
 * - Failed during the OpenEMR sequence: 200 with `outcome: 'failed'` or `'partly-shared'` and
 *   the steps so far, because the trace of what happened is the point of the panel.
 * - OpenEMR unreachable before the first step: 502 with an OperationOutcome.
 */
export interface ShareResult {
  outcome: 'shared' | 'already-shared' | 'partly-shared' | 'failed';
  steps: ShareStep[];
  /** What OpenEMR stored, read back by id (D13), for the "As stored in OpenEMR" panel. */
  readBack: { patient: Patient | null; encounter: Encounter | null; observations: Observation[] };
}

// ---------------------------------------------------------------------------------------------
// The client interface

/** Per-call options. Pass the feature's `signal` so a torn-down dashboard cancels its requests. */
export interface CallOptions {
  signal?: AbortSignal;
}

export interface PulseBridgeApi {
  readonly mode: 'mock' | 'http';

  /** Checks the password; on success stores the token in the session. Throws `LoginError`. */
  login(password: string, options?: CallOptions): Promise<LoginResult>;
  /** Forgets the token. Stateless tokens need no server call. */
  logout(): void;
  isLoggedIn(): boolean;

  getStatus(options?: CallOptions): Promise<StatusSummary>;

  /** Observations for one LOINC code in a time range, oldest first, all pages followed. */
  searchObservations(query: ObservationQuery, options?: CallOptions): Promise<Observation[]>;
  getHeartRateTrace(observationId: string, options?: CallOptions): Promise<HeartRateTrace>;

  /** The samples exactly as cuff-sim's `GET /cuff/samples` lists them (every non-valid T07 sample). */
  listMalformedSamples(options?: CallOptions): Promise<MalformedSample[]>;
  /**
   * One send. Resolves for every send that happened (AA, AE or no acknowledgment). Rejects with
   * `CuffSendError` (a subclass of `ApiError`, errors.ts) when the proxy or the simulator
   * refused before sending: 400 `invalid-body`, 413 `too-large`, 422 `invalid-input` (bounds,
   * types, unknown or wrong-transport sample; `fields` names them), 429 `rate-limited` (with
   * `retryAfterSeconds` from `Retry-After`), 500 `server-error`; and 504 `no-answer`, where the
   * attempt may have been sent but not recorded. Nothing is sent for 400, 413, 422 and 429.
   */
  submitCuffReading(request: CuffReadingRequest, options?: CallOptions): Promise<CuffSendResult>;
  getCuffAttemptTrace(attemptId: string, options?: CallOptions): Promise<CuffAttemptTrace>;

  listMessages(query?: MessagesLogQuery, options?: CallOptions): Promise<MessagesLogPage>;

  /** See `ShareResult` for how a refused or failed share is reported. */
  share(request: ShareRequest, options?: CallOptions): Promise<ShareResult>;
}
