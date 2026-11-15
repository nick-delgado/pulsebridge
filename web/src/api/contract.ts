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

export interface TimeRange {
  /** Inclusive. */
  start: Date;
  /** Exclusive. */
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

/** The malformed classes from the work plan's "Malformed message handling" table (D7). */
export type MalformedClass =
  'structurally-invalid' | 'unparseable' | 'no-acknowledgment' | 'parses-but-cannot-map';

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

export type AckCode = 'AA' | 'AE' | 'AR';

export type NoAckReason = 'timeout' | 'connection-closed' | 'http-error' | 'network-error';

/**
 * The commit-level acknowledgment (D7): what the HL7v2 store (through the MLLP adapter, or the
 * HTTPS ingest response) said about storing the message. Either an ACK was received, or not.
 */
export type CommitAck =
  | {
      received: true;
      /** MSA-1. The store answers AA or AE; it never sends AR (spike S4). */
      code: AckCode;
      /** MSA-2, the control ID acknowledged. Empty (null) on a NACK the store could not read. */
      controlId: string | null;
      /** MSA-3 text, for example "Invalid MSH header". */
      reason: string | null;
      /** The ACK as received, segments separated by "\r". Null for a store-only log row. */
      raw: string | null;
    }
  | {
      received: false;
      why: NoAckReason;
      /** HTTP status when `why` is `http-error`. */
      httpStatus: number | null;
      detail: string;
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

/** One cuff send attempt, from its AuditEvent, joined to the stored message where there is one. */
export interface SendAttemptEntry {
  kind: 'cuff-send';
  id: string;
  recordedAt: string;
  transport: Transport | null;
  controlId: string | null;
  sample: string | null;
  commitAck: CommitAck;
  /** Null when the message was never stored (an AE or no acknowledgment). */
  application: ApplicationOutcome | null;
  messageName: string | null;
  /** The resulting Observation as a reference, `Observation/<id>`, once mapped. */
  observation: string | null;
  /** `store-only`: a stored message with no AuditEvent, for example sent straight to the adapter. */
  source: 'audit-event' | 'store-only';
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

export type MessagesLogEntry = SendAttemptEntry | FhirTransactionEntry;

export interface MessagesLogQuery {
  kind?: MessagesLogEntry['kind'];
  outcome?: ApplicationOutcome['outcome'];
  /** Page size; the proxy caps it. */
  count?: number;
  /** Opaque cursor from a previous page's `nextCursor`. */
  cursor?: string;
}

export interface MessagesLogPage {
  /** Newest first. */
  entries: MessagesLogEntry[];
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

export interface ShareResult {
  outcome: 'shared' | 'already-shared' | 'partly-shared' | 'failed';
  steps: ShareStep[];
  /** What OpenEMR stored, read back by id (D13), for the "As stored in OpenEMR" panel. */
  readBack: { patient: Patient | null; encounter: Encounter | null; observations: Observation[] };
}

// ---------------------------------------------------------------------------------------------
// The client interface

export interface PulseBridgeApi {
  readonly mode: 'mock' | 'http';

  /** Checks the password; on success stores the token in the session. Throws `LoginError`. */
  login(password: string): Promise<LoginResult>;
  /** Forgets the token. Stateless tokens need no server call. */
  logout(): void;
  isLoggedIn(): boolean;

  getStatus(): Promise<StatusSummary>;

  /** Observations for one LOINC code in a time range, oldest first, all pages followed. */
  searchObservations(query: ObservationQuery): Promise<Observation[]>;
  getHeartRateTrace(observationId: string): Promise<HeartRateTrace>;

  listMalformedSamples(): Promise<MalformedSample[]>;
  /** Throws `ApiError` 422 (with an OperationOutcome) for inputs outside bounds; nothing is sent. */
  submitCuffReading(request: CuffReadingRequest): Promise<CuffSendResult>;
  getCuffAttemptTrace(attemptId: string): Promise<CuffAttemptTrace>;

  listMessages(query?: MessagesLogQuery): Promise<MessagesLogPage>;

  share(request: ShareRequest): Promise<ShareResult>;
}
