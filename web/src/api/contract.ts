/**
 * The contract between the dashboard and fhir-proxy: every call the page makes, as one
 * TypeScript interface (`PulseBridgeApi`) with two implementations, `HttpClient` (the real proxy)
 * and `MockClient` (synthetic data, no network).
 *
 * The page calls exactly one backend, fhir-proxy (D10). The routes behind each method are the
 * ones T14 built (services/fhir-proxy/README.md, "Routes as built" and its 21 amendments), plus
 * the share route, whose contract is specified here for T15 (`ShareResult`, `ShareError`); they
 * are listed in web/README.md ("HTTP contract").
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
// Demo session (D16, docs/workplan.md "The contract T43 to T45 share")

/**
 * `GET /auth/config`: what the page needs to run the bot check. The site key is public, but it is
 * fetched, never written into the page or its build, so a stranger's deployment uses its own key.
 */
export interface AuthConfig {
  /** The reCAPTCHA (Google Cloud Fraud Defense) score-based web key's id. */
  recaptchaSiteKey: string;
  /** The action name the page passes to `execute` and the proxy expects back (`view_demo`). */
  recaptchaAction: string;
}

/**
 * `POST /auth/session` on a pass. The session itself is the `__session` cookie, which is HttpOnly:
 * the page cannot read it and never sees a token. Only the expiry is returned.
 */
export interface DemoSession {
  /** RFC 3339 instant; 24 hours after the session started by default. */
  expiresAt: string;
}

/** What `checkSession` found: a session the proxy accepts, or none (it answered 401). */
export type SessionState = 'active' | 'none';

// ---------------------------------------------------------------------------------------------
// Status strip

export interface Reachability {
  reachable: boolean;
  /** When the proxy last checked, ISO 8601. */
  checkedAt: string;
  /**
   * A short code when not reachable, never the server's text (the proxy's `reachability_reason`:
   * `permission-denied`, `store-missing`, `timeout`, `unavailable`, `bad-answer`; for OpenEMR
   * `error`, or `not-checked`). `not-checked` (`REACHABILITY_NOT_CHECKED`) means nobody looked:
   * the share extension (T15) is not installed, so there is no OpenEMR probe. It is not a claim
   * that OpenEMR is down, and the status strip says "Not checked" for it.
   */
  detail: string | null;
}

/** `Reachability.detail` when the proxy did not check at all (amendment 10). */
export const REACHABILITY_NOT_CHECKED = 'not-checked';

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
   * When health-sync itself could not be asked, `state` is `unknown` and the reason is one of the
   * proxy's own codes (amendment 9, `HEALTH_SYNC_FAILURE_REASONS`).
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

/**
 * The proxy's reasons for `state: "unknown"` when health-sync could not be asked (amendment 9),
 * in words for the status strip. Any other reason code is health-sync's own and shown as itself.
 */
export const HEALTH_SYNC_FAILURE_REASONS: Readonly<Record<string, string>> = {
  'health-sync-timeout': 'health-sync did not answer in time',
  'health-sync-unreachable': 'health-sync could not be reached',
  'health-sync-error': 'health-sync answered with an error',
  'health-sync-bad-answer': 'health-sync sent an answer the proxy could not read',
  'health-sync-identity-token-unavailable':
    'the proxy could not get an identity token to call health-sync',
  'health-sync-not-deployed': 'health-sync is not deployed',
};

export interface StatusSummary {
  watchSync: WatchSyncStatus;
  /**
   * Observations in the FHIR store, per source. `null` is "not known" (the store could not be
   * asked in time; amendment 15), never 0; `0` means the store was asked and has none.
   */
  observationCounts: { watch: number | null; cuff: number | null };
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
  /** The attempt as a log row, with the same `messageLookup` the log shows. */
  attempt: SendAttemptEntry;
  /**
   * ER7 as sent, or null: a built reading that was rejected or unacknowledged (T31 keeps no
   * message bytes), or a sample the proxy cannot rebuild for the configured patient.
   */
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
  /**
   * When the message was sent (`period.start`); the log sorts attempt rows on this (amendment
   * 17). For a store-only row: the message's MSH-7 send time if readable, else its createTime.
   */
  sentAt: string;
  /**
   * When the attempt was recorded (`recorded`). For a store-only row: the stored message's
   * `createTime`, which is what the log sorts that row on (a sample's MSH-7 is years old).
   */
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
  /**
   * Null when there is no application-level outcome to show: the message was never stored
   * (rejected or no acknowledgment), or the store did not confirm this attempt's message name
   * (`messageLookup` `not-found` or `failed`). Only a `confirmed` lookup gives an outcome.
   */
  application: ApplicationOutcome | null;
  /**
   * HL7v2 store message name **returned by the store** (T31's `message_name`: an accepted HTTPS
   * send, from the ingest response). Never a derived name; null otherwise.
   */
  messageName: string | null;
  /**
   * The name cuff-sim **derived** for an accepted MLLP send (T31's `derived_message_name`, the
   * AuditEvent's `derived-message-name` detail; T33): `<store>/messages/<urlsafe-base64(sha256(
   * the bytes actually sent))>`. Unconfirmed: the proxy joins the attempt to its stored message
   * by it, confirms it with a `get`, and the page shows it labelled as derived, never as
   * `messageName`. Null when the store returned a name, or nothing was stored.
   */
  derivedMessageName: string | null;
  /**
   * What the HL7v2 store said about this attempt's message name (amendment 1):
   * - `confirmed`: the store has it; its labels are the application-level outcome;
   * - `not-found`: the store does not have it (the derived MLLP name was wrong, or the message
   *   was deleted). The row has no outcome and no Observation, and is never polled;
   * - `failed`: the store could not be asked in time (the page is then `partial`). No outcome
   *   either; a reload may confirm it;
   * - `null`: there is no name to look up (rejected, or no acknowledgment).
   * A store-only row is always `confirmed` (it was listed from the store).
   */
  messageLookup: MessageLookup;
  /** The resulting Observation as a reference, `Observation/<id>`, once mapped. */
  observation: string | null;
  /**
   * `store-only`: a stored message that no send attempt anywhere in the audit store claims, for
   * example one whose AuditEvent could not be written, or one sent straight to the adapter
   * (amendment 5). It has NO trace route (`getCuffAttemptTrace` takes AuditEvent ids), so it is
   * never put in the selection.
   */
  source: 'audit-event' | 'store-only';
  /**
   * Store-only rows only (amendment 14): the id of the ONE send attempt this message may belong
   * to (an accepted MLLP attempt with the same control ID whose own derived name was
   * `not-found`, sent at most 120 s before the message was created), or null. A hint, never a
   * confirmed link: it moves no outcome from one row to the other. Always null on an
   * `audit-event` row (the HTTP client sets it so).
   */
  possibleDuplicateOf: string | null;
}

export type MessageLookup = 'confirmed' | 'not-found' | 'failed' | null;

/**
 * An AuditEvent that is ours (a cuff send attempt) but could not be read: T31's
 * `UnreadableSendAttempt`, for example a missing value or a newer format version. The proxy
 * surfaces it as a visible error row and never hides it; T19 shows it as such.
 */
export interface UnreadableSendAttemptEntry {
  kind: 'unreadable-send-attempt';
  /** The AuditEvent id; `""` when the AuditEvent had none (amendment 6). Never an element id. */
  id: string;
  /** `recorded` when it could be read, else null (sorted last). */
  recordedAt: string | null;
  /** What was wrong, from the parser's message. */
  problem: string;
}

/**
 * One FHIR transaction Bundle written by an ingest service. The proxy derives these rows from
 * the `Provenance` each ingest Bundle writes (Provenances with the same `recorded` time and the
 * same assembler agent came from one `executeBundle`), pinned to the configured patient
 * (amendment 18). A transaction is all or nothing, so a Provenance proves it committed.
 */
export interface FhirTransactionEntry {
  kind: 'fhir-transaction';
  id: string;
  recordedAt: string;
  /**
   * The assembler agent of its Provenances (amendment 2): `seed` is the synthetic-data seeder
   * (T09); `unknown` is an agent the proxy does not recognise.
   */
  writer: 'health-sync' | 'v2-to-fhir' | 'seed' | 'unknown';
  /** HTTP status of the executeBundle call. Nothing records it, so the proxy always sends null. */
  httpStatus: number | null;
  /** The patient's Observations it wrote and their Provenances, `Observation/<id>`, `Provenance/<id>`. */
  resources: string[];
}

export type MessagesLogEntry = SendAttemptEntry | UnreadableSendAttemptEntry | FhirTransactionEntry;

/** The proxy's page size for `GET /messages` when `count` is not given, and its cap (amendment 3). */
export const MESSAGES_DEFAULT_COUNT = 25;
export const MESSAGES_MAX_COUNT = 50;

export interface MessagesLogQuery {
  kind?: MessagesLogEntry['kind'];
  /**
   * Matches only rows WITH an application-level outcome equal to it (amendment 19). A row with
   * `application: null` (rejected, unacknowledged, or a name the store could not confirm) and a
   * transaction row match no outcome filter, `pending` included.
   */
  outcome?: ApplicationOutcome['outcome'];
  /** Page size: default 25, capped at 50 by the proxy. */
  count?: number;
  /**
   * Opaque cursor from a previous page's `nextCursor`. One the proxy cannot read is refused
   * with 400 `invalid-cursor` (amendment 21): load the log from the start.
   */
  cursor?: string;
}

export interface MessagesLogPage {
  /**
   * Newest first by SEND time (amendment 17): attempt rows by `sentAt`, store-only rows by the
   * message's createTime (`recordedAt`), transaction rows by `recordedAt`, ties by id
   * descending; unreadable rows without a time last.
   *
   * With a `kind` or `outcome` filter the proxy filters AFTER it has joined a page, so a page can
   * hold fewer rows than `count`, even none, while `nextCursor` is not null (amendment 4):
   * follow `nextCursor` until it is null.
   */
  entries: MessagesLogEntry[];
  /** How many AuditEvents on this page were ours but unreadable (also present as rows). */
  unreadable: number;
  nextCursor: string | null;
  /**
   * True when the page was cut short by the proxy's time budget or a store answer that could not
   * be read completely (amendment 16): rows of its window may be missing (lookups not finished
   * show `messageLookup: "failed"`; unchecked store-only rows are left out, never guessed).
   * Reloading may fill it in. The HTTP client reads anything but an explicit `false` as true.
   */
  partial: boolean;
}

// ---------------------------------------------------------------------------------------------
// Share with clinic (T15). T19 specifies this contract for T15 to implement; web/README.md
// ("For T15: the share route") has the exact wire shapes and errors.

/**
 * The body of `POST /share`: `{"observationId": "<id>"}`, the FHIR id of ONE Observation in the
 * health-data store, and nothing else. Never a patient, a reference to another resource type or
 * a URL: the proxy finds the configured patient itself, checks the Observation is theirs, and
 * builds everything it sends to OpenEMR from its own store.
 */
export interface ShareRequest {
  observationId: string;
}

/**
 * The steps of one share, in the order T15 performs them (docs/spikes/s1.md, "Design
 * consequences"): an access token (client credentials, `private_key_jwt`); the Patient searched
 * by identifier and created only if absent (OpenEMR ignores conditional create); an Encounter
 * found for the reading's day and reused, or created (OpenEMR refuses an Observation without
 * one, and refuses a repeated reading only within one Encounter); the Observation `POST`ed; then
 * the Patient, the Encounter and the Observation read back by id (D13).
 */
export type ShareStepName =
  | 'token'
  | 'patient-search'
  | 'patient-create'
  | 'encounter-search'
  | 'encounter-create'
  | 'observation-create'
  | 'patient-read'
  | 'encounter-read'
  | 'observation-read';

/**
 * What one step achieved:
 * - `ok`: done as asked (a token, a 201, a read);
 * - `reused`: only for `token`: an access token still valid in the proxy's memory was used, so
 *   no token request was sent (the step has no bodies and status 0);
 * - `found` / `not-found`: a search's answer (the Patient already existed, or will be created;
 *   an Encounter will be reused, or created);
 * - `already-shared`: OpenEMR refused the Observation as a repeat of one it holds (its
 *   "already exists" 400), which is not a failure;
 * - `failed`: the step did not succeed (the share stopped here, or a read-back failed);
 * - `unknown`: the proxy sent a value this page does not know. The HTTP client produces it; the
 *   page then claims nothing about the step.
 */
export type ShareStepResult =
  'ok' | 'reused' | 'found' | 'not-found' | 'already-shared' | 'failed' | 'unknown';

/**
 * One step of the share trace, as T14's `extensions.trace_step` builds it once T15 has applied
 * the FINAL redaction rule (web/README.md, "For T15: the share route"):
 * - `request.url` is a PATH ONLY: no scheme, no host, no query string;
 * - `request.query` holds a search's parameters (parameter name to one value or several), or
 *   null when the request has none; `request.body` is only a real request body (null for a GET);
 * - credentials are replaced by `"[not shown]"`: in the token step's bodies, every OAuth
 *   parameter (`SHARE_TOKEN_STEP_SECRETS`); in every other step, only names that cannot collide
 *   with a FHIR element (`SHARE_FHIR_STEP_SECRETS`), so FHIR codes stay visible.
 * Bodies came from OpenEMR, so they are untrusted text.
 */
export interface ShareStep {
  /** A known step name, or the name the proxy sent when this page does not know it. */
  step: ShareStepName | (string & {});
  /** The proxy's short description, for example "Search Patient by identifier". */
  label: string;
  /** `url` is a path; the HTTP client replaces anything that is not with `SHARE_URL_HIDDEN`. */
  request: { method: string; url: string; query: ShareQuery | null; body: unknown };
  response: { status: number; body: unknown };
  result: ShareStepResult;
  durationMs: number;
}

/** A search step's parameters: `{"identifier": "…"}`, or several values for a repeated name. */
export type ShareQuery = Record<string, string | string[]>;

/**
 * The FINAL redaction rule for T15 (the proxy is to be changed to match). Key names are
 * compared without regard to case, at any depth.
 * - The token step's request and response bodies: every OAuth 2.0 / JWT bearer parameter that
 *   carries a credential, including the generic `code` and `token`.
 * - Every other step (FHIR bodies): only names that cannot collide with a FHIR element.
 */
export const SHARE_TOKEN_STEP_SECRETS = [
  'authorization',
  'client_secret',
  'client_assertion',
  'assertion',
  'password',
  'token',
  'access_token',
  'refresh_token',
  'id_token',
  'code',
  'code_verifier',
  'private_key',
  'registration_access_token',
] as const;
export const SHARE_FHIR_STEP_SECRETS = [
  'access_token',
  'refresh_token',
  'id_token',
  'client_assertion',
  'client_secret',
  'assertion',
  'private_key',
  'password',
  'authorization',
  'registration_access_token',
] as const;

/** What the page shows for a step `url` that was not a path (so it never shows a host). */
export const SHARE_URL_HIDDEN = '(not shown: not a path)';

/**
 * One resource of the read-back (D13: "As stored in OpenEMR"), with what was sent beside it so
 * the panel can show the differences:
 * - `sent`: the resource exactly as the proxy sent it to OpenEMR, or null when nothing was sent
 *   (the Patient already existed; the Encounter was reused). It holds no credential;
 * - `stored`: the resource as OpenEMR returned it when read back by id, or null when the read
 *   failed (its step says why).
 */
export interface SentAndStored<T> {
  sent: T | null;
  stored: T | null;
}

/**
 * The 200 answer of `POST /share`: the reading is in OpenEMR. `shared`: it was written now.
 * `already-shared`: OpenEMR already held it (its "already exists" 400 names the existing
 * Observation, which is what `readBack.observation.stored` shows); nothing new was written and
 * no Patient was created twice. Failures are thrown as `ShareError` (errors.ts).
 */
export interface ShareResult {
  outcome: 'shared' | 'already-shared';
  steps: ShareStep[];
  readBack: {
    patient: SentAndStored<Patient>;
    encounter: SentAndStored<Encounter>;
    observation: SentAndStored<Observation>;
  };
}

/** Why a share was refused or failed: the proxy's reason code in the OperationOutcome. */
export type ShareFailureReason =
  /** 400: the body is not `{"observationId": <FHIR id>}`. */
  | 'invalid-body'
  /** 404: no such Observation, or another patient's (answered alike). */
  | 'not-found'
  /** 422, before OpenEMR is called: the Observation is not `final` (for example `amended`). */
  | 'observation-not-final'
  /** 422, before OpenEMR is called: not a heart rate or a blood-pressure panel. */
  | 'observation-not-shareable'
  /** 422, before OpenEMR is called: the stored Patient is incomplete (the text says what is missing). */
  | 'patient-incomplete'
  /** 502, with the steps so far: OpenEMR could not be reached, or refused a step unexpectedly. */
  | 'openemr-unreachable'
  | 'openemr-refused'
  /** 504, with the steps so far: OpenEMR did not answer within the request budget. */
  | 'openemr-timeout'
  /** 503: the share is not configured on the server (T15's variables or key missing). */
  | 'share-not-configured'
  /** 503, before OpenEMR is called: another share was in progress on this proxy instance. */
  | 'share-busy'
  /** 502, with the steps so far: the proxy itself failed after OpenEMR was called. */
  | 'share-internal-error'
  /** 429 (T46), before OpenEMR is called: too many shares; `retryAfterSeconds` says how long. */
  | 'rate-limited'
  /** Any other status or reason (including the gate's 503 `session-not-configured`, or a 429). */
  | 'other';

// ---------------------------------------------------------------------------------------------
// The client interface

/** Per-call options. Pass the feature's `signal` so a torn-down dashboard cancels its requests. */
export interface CallOptions {
  signal?: AbortSignal;
}

export interface PulseBridgeApi {
  readonly mode: 'mock' | 'http';

  /** `GET /auth/config` (public). Rejects with `SessionError` (errors.ts). */
  getAuthConfig(options?: CallOptions): Promise<AuthConfig>;
  /**
   * `POST /auth/session` (public) with a fresh reCAPTCHA token. On a pass the proxy sets the
   * `__session` cookie and every later call carries it. Rejects with `SessionError`, whose
   * `reason` is the proxy's code (`bot-check-failed`, `demo-busy`, `rate-limited`, ...).
   */
  startSession(recaptchaToken: string, options?: CallOptions): Promise<DemoSession>;
  /**
   * Whether the browser still has a session the proxy accepts (for example after a reload: the
   * page cannot read the HttpOnly cookie, so it asks). A 401 resolves `'none'` without calling
   * `onUnauthorized`; any other failure rejects with `ApiError`.
   */
  checkSession(options?: CallOptions): Promise<SessionState>;
  /**
   * Ends the session: `POST /auth/logout`, which clears the cookie. Never rejects; requests still
   * in flight that answer 401 afterwards no longer end anything.
   */
  logout(): Promise<void>;

  getStatus(options?: CallOptions): Promise<StatusSummary>;

  /** Observations for one LOINC code in a time range, oldest first, all pages followed. */
  searchObservations(query: ObservationQuery, options?: CallOptions): Promise<Observation[]>;
  /**
   * One Observation by id (`GET /fhir/Observation/{id}`, amendment 20): the configured
   * patient's, else `ApiError` 404 (another patient's answers like a missing one). The share
   * area reads the selected Observation with it before asking to confirm.
   */
  getObservation(observationId: string, options?: CallOptions): Promise<Observation>;
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
  /**
   * The trace of one recorded send attempt, by AuditEvent id. Final answers (retrying cannot
   * help): `ApiError` 404 (no such attempt, or another patient's), 410, and 422 when the
   * AuditEvent is ours but unreadable (amendment 12; the log lists it as an unreadable row).
   */
  getCuffAttemptTrace(attemptId: string, options?: CallOptions): Promise<CuffAttemptTrace>;

  /** One page of the messages log; see `MessagesLogPage` for order, short pages and `partial`. */
  listMessages(query?: MessagesLogQuery, options?: CallOptions): Promise<MessagesLogPage>;

  /**
   * Shares one final Observation with the clinic. Resolves when the reading is in OpenEMR
   * (`shared` or `already-shared`). Rejects with `ShareError` (errors.ts) otherwise: 400, 404,
   * 422 and 503 before anything was sent (no steps), and 502 or 504 with the steps completed so
   * far, so the page can say what did and did not happen.
   */
  share(request: ShareRequest, options?: CallOptions): Promise<ShareResult>;
}
