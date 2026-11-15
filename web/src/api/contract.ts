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

export interface StatusSummary {
  watchSync: {
    /**
     * `reconnect-needed`: the Google refresh token expired or was revoked (T10), and the deployer
     * must re-run the connect script (T11).
     */
    state: 'ok' | 'reconnect-needed' | 'never-synced' | 'error';
    lastSyncAt: string | null;
    detail: string | null;
  };
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

export interface HeartRateTrace {
  observation: Observation;
  provenance: Provenance | null;
  device: Device | null;
  /** Null when the proxy cannot supply the source JSON (open question in the README). */
  source: GoogleHealthRollUpWindow | null;
}

// ---------------------------------------------------------------------------------------------
// Cuff simulator (T13, forwarded by the proxy, D10)

export type Transport = 'mllp' | 'https';

/** Proposed input bounds; T13 owns the final numbers and rejects anything outside them. */
export const CUFF_BOUNDS = {
  systolic: { min: 60, max: 250 },
  diastolic: { min: 30, max: 150 },
} as const;

/**
 * The malformed classes from the work plan's "Malformed message handling" table (D7), spelled
 * exactly as `classes` in libs/pulsebridge_hl7v2/samples/samples.json (T07).
 */
export type MalformedClass =
  'structurally_invalid' | 'unparseable' | 'no_acknowledgment' | 'parses_but_cannot_map';

/** A synthetic sample from libs/pulsebridge_hl7v2/samples that "send malformed" can send. */
export interface MalformedSample {
  /** Sample name exactly as in samples.json, for example `unparseable_no_msh`. */
  name: string;
  label: string;
  class: MalformedClass;
  /** Bad-framing samples exist only on MLLP. */
  transports: Transport[];
}

export interface CuffReadingRequest {
  systolic: number;
  diastolic: number;
  transport: Transport;
  /** A sample name to send instead of the built message (bypasses validation), or null. */
  sendMalformed: string | null;
}

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

/** The result of one send, in T13's single result shape for both transports. */
export interface CuffSendResult {
  /** Id of the AuditEvent recording this attempt (D12); used for trace lookups. */
  attemptId: string;
  sentAt: string;
  transport: Transport;
  /** MSH-10 of the message sent. */
  controlId: string | null;
  /** The malformed sample sent, or null for a built reading. */
  sample: string | null;
  /** The message as sent, ER7 with "\r" between segments. */
  messageSent: string;
  /** HL7v2 store message name from the HTTPS ingest response; null over MLLP or when rejected. */
  messageName: string | null;
  ack: CommitAck;
  /** Whether the AuditEvent was written. A failure never hides the send result (T13). */
  audit: { recorded: true } | { recorded: false; detail: string };
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
  result: 'accepted' | 'rejected' | 'no-acknowledgment';
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

  listMalformedSamples(options?: CallOptions): Promise<MalformedSample[]>;
  /**
   * Throws `ApiError` 422 (with an OperationOutcome) for inputs outside bounds; nothing is sent.
   * The route is rate-limited: 429 with `Retry-After` becomes `ApiError` with
   * `retryAfterSeconds`.
   */
  submitCuffReading(request: CuffReadingRequest, options?: CallOptions): Promise<CuffSendResult>;
  getCuffAttemptTrace(attemptId: string, options?: CallOptions): Promise<CuffAttemptTrace>;

  listMessages(query?: MessagesLogQuery, options?: CallOptions): Promise<MessagesLogPage>;

  /** See `ShareResult` for how a refused or failed share is reported. */
  share(request: ShareRequest, options?: CallOptions): Promise<ShareResult>;
}
