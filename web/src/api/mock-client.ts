/**
 * Mock implementation of PulseBridgeApi: synthetic data, in memory, no network at all (the mock
 * build's Content-Security-Policy is `connect-src 'none'`, so a stray request would fail loudly).
 *
 * It behaves like the real client where the page can tell the difference:
 * - any non-empty password logs in, and the token goes into the same sessionStorage session;
 * - every other call checks that token first, and with none it clears the session and calls
 *   `onUnauthorized`, exactly as the HTTP client does on a 401;
 * - a cuff send is checked as cuff-sim checks it (services/cuff-sim/README.md): the same limits,
 *   the same refusals as `CuffSendError` 422 with an OperationOutcome naming the field, and the
 *   same per-instance rate limit (429 with `retryAfterSeconds`);
 * - a send result has cuff-sim's fields, including the DERIVED message name over MLLP;
 * - heart-rate data arrives only at a mock sync run (every 15 minutes), so `lastSyncAt` (the newest write),
 *   the windows and their status change together, as they do with health-sync;
 * - the messages log is assembled as fhir-proxy assembles it (services/fhir-proxy/README.md and
 *   messages.py): `messageLookup` on every attempt, store-only rows with `possibleDuplicateOf`,
 *   transaction rows from Provenances with `httpStatus: null`, newest first by send time, pages
 *   of 25 (at most 50) cut BEFORE the kind/outcome filter (so a filtered page can be short or
 *   empty while `nextCursor` is set), an opaque cursor refused with 400 `invalid-cursor`;
 * - its refusals are the proxy's: 400 for a refused search, 422 for an unreadable attempt's
 *   trace, 503 `login-not-configured` on every route when its secret is missing (a switch);
 * - the share walks the sequence T15 will (contract.ts `ShareStepName`), with trace steps
 *   redacted exactly as T14's `extensions.trace_step` does it (`traceStep` below).
 */
import type {
  ApplicationOutcome,
  CommitAck,
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  FhirTransactionEntry,
  HeartRateTrace,
  LoginResult,
  MalformedSample,
  MessageLookup,
  MessagesLogEntry,
  MessagesLogPage,
  MessagesLogQuery,
  CallOptions,
  ObservationQuery,
  PulseBridgeApi,
  SendAttemptEntry,
  ShareRequest,
  ShareResult,
  ShareQuery,
  ShareStep,
  ShareStepResult,
  UnreadableSendAttemptEntry,
  StatusSummary,
  Transport,
} from './contract';
import {
  CUFF_SERVICE_LIMITS,
  MAPPER_PLAUSIBLE_RANGE,
  MESSAGES_DEFAULT_COUNT,
  MESSAGES_MAX_COUNT,
  REACHABILITY_NOT_CHECKED,
  SHARE_FHIR_STEP_SECRETS,
  SHARE_TOKEN_STEP_SECRETS,
} from './contract';
import {
  ApiError,
  CuffSendError,
  LoginError,
  PROXY_REASON_SYSTEM,
  ShareError,
  UnauthorizedError,
} from './errors';
import {
  LOINC,
  type Encounter,
  type Observation,
  type OperationOutcome,
  type Patient,
  type Resource,
} from './fhir';
import {
  bloodPressureObservation,
  derivedMessageName,
  hash32,
  HOSTILE_MARKUP,
  HOSTILE_REFUSED_SYSTOLIC,
  heartRateObservation,
  heartRateObservations,
  heartRateWritesAt,
  lastHeartRateWrite,
  lastSyncRun,
  MOCK_DATA_SETS,
  MOCK_SAMPLES,
  type MockDataSet,
  type MockSampleBehaviour,
  MOCK_WATCH,
  mockAck,
  MOCK_HL7V2_STORE,
  MOCK_PATIENT_IDENTIFIER,
  mockControlId,
  type MockLookupFailure,
  type MockShareFailure,
  type MockStatusMode,
  mockSendAuditEvent,
  outcomeCode,
  sendResult,
  mockOruR01,
  openemrPatient,
  provenanceFor,
  pseudoUuid,
  syntheticRollUp,
  windowStartFromId,
} from './mock-data';
import type { Session } from './session';

export interface MockClientOptions {
  session: Session;
  onUnauthorized: () => void;
  /** Clock, injectable for tests. */
  now?: () => number;
  /** Simulated network delay per call; 0 in tests. */
  latencyMs?: number;
  /** How long a stored message stays `pending` before the mock "mapper" labels it. */
  mappingDelayMs?: number;
  /** The zone heart-rate timestamps are written in (mock-data.ts, `MOCK_DATA_SETS`). */
  dataSet?: MockDataSet;
  /**
   * cuff-sim's per-instance token bucket (RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE; defaults 3
   * and 6). Null switches it off, for tests that send many readings.
   */
  rateLimit?: { burst: number; perMinute: number } | null;
  /**
   * Makes every cuff send fail to record its AuditEvent, as cuff-sim reports it: `build` (T31
   * refused a value, so there is no AuditEvent id: `attemptId` is null) or `write` (the audit
   * store did not take it: the id exists but nothing is stored under it). Either way the send
   * itself happened, `audit.recorded` is false, and the attempt is not in the messages log.
   */
  auditFailure?: 'build' | 'write' | null;
  /** `?mock-lookup=`: the store does not have (or cannot be asked for) any accepted send's name. */
  lookupFailure?: MockLookupFailure | null;
  /** `?mock-log=partial`: every messages-log page is `partial`. */
  partialLog?: boolean;
  /** `?mock-status=`: the status (or the 503) the proxy sends in that state. */
  statusMode?: MockStatusMode | null;
  /** `?mock-share=`: where a share stops or is refused. */
  shareFailure?: MockShareFailure | null;
}

/** cuff-sim's defaults (RATE_LIMIT_BURST=3, RATE_LIMIT_PER_MINUTE=6). */
export const MOCK_RATE_LIMIT = { burst: 3, perMinute: 6 } as const;

/** Mock tokens look nothing like a JWT on purpose, so nobody mistakes one for a real token. */
const MOCK_TOKEN_PREFIX = 'mock-session-';
const SESSION_TTL_MS = 8 * 60 * 60 * 1000;

interface MockAttempt {
  entry: SendAttemptEntry;
  messageSent: string;
  systolic: number;
  diastolic: number;
  /** Final application outcome once the mock mapper has "run"; null when never stored. */
  finalOutcome: { outcome: 'accepted' | 'error'; reason: string | null } | null;
  resolveAt: number;
  /** Sequence number, used to name the Observation once mapped. */
  seq: number;
  /** False when the AuditEvent was not recorded: the attempt is then not in the log. */
  audited: boolean;
  /**
   * An accepted MLLP send: cuff-sim's derived message name, still being hashed (SHA-256 is async
   * in the browser). `#settleDerivedNames` writes it into `entry.derivedMessageName`.
   */
  derivedName: Promise<string> | null;
  /**
   * Set for a seeded attempt whose derived name is WRONG: the store has its message under
   * another name, so the attempt's lookup is `not-found` and the real message is a store-only
   * row (the case amendment 14's hint is for).
   */
  storedAs: string | null;
  /** When the mock mapper labelled the stored message (its v2-to-fhir transaction time). */
  mappedAt: number | null;
}

/** A message in the HL7v2 store that no send attempt names: a store-only row (amendment 5). */
interface MockStoredMessage {
  name: string;
  /** The store's createTime: what the row sorts on. */
  createdAt: number;
  /** MSH-7 (the row's `sentAt`). */
  sentAt: number;
  /** `https` only when cuff-sim labelled it; the adapter cannot label an MLLP message. */
  transport: Transport | null;
  controlId: string | null;
  application: ApplicationOutcome;
  observation: string | null;
  mappedAt: number | null;
}

/** What the mock OpenEMR holds after shares, keyed as OpenEMR keys them (spike S1). */
interface MockOpenEmr {
  patientId: string | null;
  /** Encounter id per reading day (T15 reuses one Encounter per patient and day). */
  encounters: Map<string, string>;
  /** Observation id per (code, Encounter, effective time): OpenEMR's duplicate rule. */
  observations: Map<string, string>;
  stored: Map<string, Resource>;
  /** When the mock's access token was issued (ms); reused for 240 s, as the proxy does. */
  tokenAt: number | null;
}

/** The proxy reuses a 300 s token until 60 s before it expires (T15, `share/oauth.py`). */
const TOKEN_REUSE_MS = 240_000;

export class MockClient implements PulseBridgeApi {
  readonly mode = 'mock' as const;
  readonly #now: () => number;
  readonly #latency: number;
  readonly #mappingDelay: number;
  readonly #attempts: MockAttempt[] = [];
  readonly #transactions: FhirTransactionEntry[] = [];
  readonly #unreadable: UnreadableSendAttemptEntry[] = [];
  readonly #direct: MockStoredMessage[] = [];
  readonly #openemr: MockOpenEmr = {
    patientId: null,
    encounters: new Map(),
    observations: new Map(),
    stored: new Map(),
    tokenAt: null,
  };
  readonly #zone: string | null;
  /** The `hostile` data set: markup in text from other systems (mock-data.ts, HOSTILE_MARKUP). */
  readonly #hostile: boolean;
  readonly #rateLimit: { burst: number; perMinute: number } | null;
  #tokens: number;
  #tokensAt: number;
  #sequence = 0;

  constructor(private readonly options: MockClientOptions) {
    this.#now = options.now ?? (() => Date.now());
    this.#latency = options.latencyMs ?? 150;
    this.#mappingDelay = options.mappingDelayMs ?? 3000;
    this.#zone = (options.dataSet ?? MOCK_DATA_SETS.utc).timeZone;
    this.#hostile = (options.dataSet ?? MOCK_DATA_SETS.utc).hostile;
    this.#rateLimit = options.rateLimit === undefined ? MOCK_RATE_LIMIT : options.rateLimit;
    this.#tokens = this.#rateLimit?.burst ?? 0;
    this.#tokensAt = this.#now();
    this.#seed();
  }

  /** The heart-rate data as the last mock sync run left it in the store. */
  #lastSync(): number {
    return lastSyncRun(this.#now());
  }

  // -- login ------------------------------------------------------------------------------------

  async login(password: string, options: CallOptions = {}): Promise<LoginResult> {
    await this.#delay();
    options.signal?.throwIfAborted();
    if (password === '') throw new LoginError('wrong-password', 'Enter a password.');
    const result = {
      token: `${MOCK_TOKEN_PREFIX}${(this.#now() % 1e9).toString(36)}`,
      expiresAt: new Date(this.#now() + SESSION_TTL_MS).toISOString(),
    };
    this.options.session.save(result);
    return result;
  }

  logout(): void {
    this.options.session.clear();
  }

  isLoggedIn(): boolean {
    return this.options.session.token() !== null;
  }

  // -- reads ------------------------------------------------------------------------------------

  async getStatus(options: CallOptions = {}): Promise<StatusSummary> {
    await this.#authorised(options.signal);
    const now = this.#now();
    const sync = this.#lastSync();
    const written = lastHeartRateWrite(now);
    const checkedAt = new Date(now).toISOString();
    const mode = this.options.statusMode ?? null;
    if (mode === 'counts-unknown') {
      // The store searches did not finish in the budget (status.py `_store_unknown`): counts and
      // lastSyncAt are not known, and the store is reported unreachable with a short code.
      return {
        watchSync: {
          state: 'connected',
          reason: null,
          notes: [],
          strandedPreliminary: null,
          checkedAt,
          lastSyncAt: null,
        },
        observationCounts: { watch: null, cuff: null },
        fhirStore: { reachable: false, checkedAt, detail: 'timeout' },
        openemr: { reachable: true, checkedAt, detail: null },
      };
    }
    return {
      // health-sync's /status shape (services/health-sync/README.md), plus the proxy's lastSyncAt.
      watchSync: {
        // health-sync not answering is `unknown` with the proxy's own reason (amendment 9).
        state: mode === 'health-sync-timeout' ? 'unknown' : 'connected',
        reason: mode === 'health-sync-timeout' ? 'health-sync-timeout' : null,
        notes: [],
        // health-sync's count is per instance and in memory, so with scale to zero it is
        // usually null ("not known") between runs; the strip must cope with that.
        strandedPreliminary: null,
        checkedAt,
        // The proxy writes it in UTC with milliseconds (amendment 13), as toISOString does. It is
        // the newest heart-rate WRITE, not the last run: a run that wrote nothing (the watch on
        // the charger) does not move it (contract.ts).
        lastSyncAt: written === null ? null : new Date(written).toISOString(),
      },
      observationCounts: {
        watch: heartRateObservations(now - 30 * 864e5, now, sync).length,
        cuff: this.#bloodPressure().length,
      },
      fhirStore: { reachable: true, checkedAt, detail: null },
      // The mock implements the share, so it reports OpenEMR as T15's probe would. Before T15
      // the proxy says `not-checked` (amendment 10).
      openemr:
        mode === 'openemr-not-checked'
          ? { reachable: false, checkedAt, detail: REACHABILITY_NOT_CHECKED }
          : { reachable: true, checkedAt, detail: null },
    };
  }

  async searchObservations(
    query: ObservationQuery,
    options: CallOptions = {},
  ): Promise<Observation[]> {
    await this.#authorised(options.signal);
    const start = query.range.start.getTime();
    const end = query.range.end.getTime();
    if (query.code === LOINC.heartRate) {
      return heartRateObservations(start, end, this.#lastSync(), this.#zone);
    }
    if (query.code === LOINC.bloodPressurePanel) {
      return this.#bloodPressure().filter((o) => {
        const t = Date.parse(o.effectiveDateTime ?? '');
        return t > start && t < end; // date=gt<start>&date=lt<end>
      });
    }
    // A refused search parameter is a 400 (R4; amendment 7), as search.py `_refuse` words it.
    throw proxyProblem(
      400,
      'not-supported',
      'unsupported-value',
      'code must be http://loinc.org|8867-4 or http://loinc.org|85354-9.',
      ['code'],
    );
  }

  async getObservation(observationId: string, options: CallOptions = {}): Promise<Observation> {
    await this.#authorised(options.signal);
    if (!SAFE_ID.test(observationId)) {
      throw proxyProblem(400, 'invalid', 'invalid-id', 'That is not a valid id.');
    }
    const found = await this.#findObservation(observationId);
    if (!found) throw proxyProblem(404, 'not-found', 'not-found', 'The Observation was not found.');
    return found;
  }

  async getHeartRateTrace(
    observationId: string,
    options: CallOptions = {},
  ): Promise<HeartRateTrace> {
    await this.#authorised(options.signal);
    const start = windowStartFromId(observationId);
    const window = start === null ? null : syntheticRollUp(start);
    const sync = this.#lastSync();
    if (start === null || window === null || start + 5 * 60 * 1000 > sync) {
      throw notFound(`Observation/${observationId}`);
    }
    const observation = heartRateObservation(window, sync, this.#zone);
    return {
      observation,
      provenance: provenanceFor(`Observation/${observationId}`, window.endTime, 'watch'),
      device: MOCK_WATCH,
      // What the proxy will do (README open question 2): rebuild the window from the stored
      // reading, and say so.
      source: { kind: 'reconstructed', window },
    };
  }

  async listMalformedSamples(options: CallOptions = {}): Promise<MalformedSample[]> {
    await this.#authorised(options.signal);
    return MOCK_SAMPLES.map(({ name, label, class: cls, transports }) => ({
      name,
      label: this.#hostile ? `${label} ${HOSTILE_MARKUP}` : label,
      class: cls,
      transports: [...transports],
    }));
  }

  async submitCuffReading(
    request: CuffReadingRequest,
    options: CallOptions = {},
  ): Promise<CuffSendResult> {
    await this.#authorised(options.signal);
    // The same checks as cuff-sim's parse_request, in the same order: the body, then the rate
    // limit (a typo does not use up a send), then the send.
    const checked = this.#checkReading(request);
    if (this.#hostile && checked.sample === null && checked.systolic === HOSTILE_REFUSED_SYSTOLIC) {
      throw cuffRefusal(
        422,
        [['systolic', `is refused by the hostile data set ${HOSTILE_MARKUP}`]],
        'invalid',
      );
    }
    this.#takeToken();
    const { transport, sendMalformed, sample } = checked;

    const at = this.#now();
    const systolic = checked.systolic ?? 118;
    const diastolic = checked.diastolic ?? 76;
    // The mapper's own plausibility check (T12): inside cuff-sim's limits but outside these, a
    // reading is stored with AA and then gets the application-level outcome `out-of-range`.
    const { systolic: ms, diastolic: md } = MAPPER_PLAUSIBLE_RANGE;
    const plausible =
      systolic >= ms.min && systolic <= ms.max && diastolic >= md.min && diastolic <= md.max;
    const attempt = this.#record({
      at,
      transport,
      systolic,
      diastolic,
      sample: sendMalformed,
      commit: sample ? sample.commitAck : 'AA',
      commitReason:
        this.#hostile && sample?.commitReason
          ? `${sample.commitReason} ${HOSTILE_MARKUP}`
          : (sample?.commitReason ?? null),
      final: sample
        ? sample.application && { outcome: sample.application, reason: sample.mappingReason }
        : plausible
          ? { outcome: 'accepted', reason: null }
          : { outcome: 'error', reason: 'out-of-range' },
      resolveAt: at + this.#mappingDelay,
      audited: !this.options.auditFailure,
    });
    this.#resolve();

    await this.#settleDerivedNames();
    const entry = attempt.entry;
    // Over HTTPS the ingest response names the stored message; over MLLP the ACK does not, and
    // cuff-sim DERIVES the name for an accepted send (spike S5; unverified).
    let messageName: string | null = null;
    let messageNameSource: CuffSendResult['messageNameSource'] = null;
    if (entry.messageName !== null) {
      messageName = entry.messageName;
      messageNameSource = 'ingest-response';
    } else if (entry.derivedMessageName !== null) {
      messageName = entry.derivedMessageName;
      messageNameSource = 'derived-sha256-unverified';
    }
    const failure = this.options.auditFailure;
    return {
      // No AuditEvent could be built, so there is no id to look up (cuff-sim: `attemptId` null).
      attemptId: failure === 'build' ? null : entry.id,
      sentAt: entry.sentAt,
      transport,
      controlId: entry.controlId,
      sample: entry.sample,
      messageSent: attempt.messageSent,
      framing: transport === 'mllp' ? (sample?.mllpFraming ?? 'standard') : null,
      messageName,
      messageNameSource,
      result: entry.result,
      ack: structuredClone(entry.commitAck),
      audit: failure
        ? {
            recorded: false,
            detail:
              // cuff-sim's own sentences (services/cuff-sim/cuff_sim/audit.py, record_attempt).
              failure === 'build'
                ? 'the attempt could not be expressed as an AuditEvent'
                : 'the audit store write failed (HTTP 503)',
          }
        : { recorded: true },
    };
  }

  async getCuffAttemptTrace(
    attemptId: string,
    options: CallOptions = {},
  ): Promise<CuffAttemptTrace> {
    await this.#authorised(options.signal);
    this.#resolve();
    await this.#settleDerivedNames();
    // A store-only row's message name has slashes, so on the real proxy the URL matches no
    // route at all: 404 `unknown-route` (amendment table, web/README.md).
    if (attemptId.includes('/')) {
      throw proxyProblem(404, 'not-found', 'unknown-route', 'There is no such route.');
    }
    // Then the proxy checks the id is a FHIR id.
    if (!SAFE_ID.test(attemptId)) {
      throw proxyProblem(400, 'invalid', 'invalid-id', 'That is not a valid id.');
    }
    if (this.#unreadable.some((u) => u.id === attemptId)) {
      // Amendment 12: final, like a 404; the row stays in the log as unreadable.
      throw proxyProblem(
        422,
        'processing',
        'unreadable-send-attempt',
        "This send attempt's AuditEvent could not be read; it is listed in the messages log as unreadable.",
      );
    }
    const attempt = this.#attempts.find((a) => a.audited && a.entry.id === attemptId);
    if (!attempt) throw notFound(`AuditEvent/${attemptId}`);
    const row = this.#present(attempt);
    const observation = row.observation === null ? null : this.#observationFor(attempt);
    return {
      attempt: row,
      // The proxy reads the stored bytes by the confirmed name, or a sample's own bytes; a built
      // reading whose message it could not find has none (T31 keeps no bytes).
      messageSent:
        row.messageLookup === 'confirmed' || attempt.entry.sample !== null
          ? attempt.messageSent
          : null,
      // The resource as stored: the raw ACK stays base64 here (T31's model).
      auditEvent: mockSendAuditEvent(attempt.entry),
      observation,
      provenance: observation
        ? provenanceFor(
            `Observation/${observation.id ?? ''}`,
            attempt.entry.recordedAt,
            'cuff',
            attempt.entry.messageName ?? undefined,
          )
        : null,
    };
  }

  async listMessages(
    query: MessagesLogQuery = {},
    options: CallOptions = {},
  ): Promise<MessagesLogPage> {
    await this.#authorised(options.signal);
    this.#resolve();
    await this.#settleDerivedNames();
    // The proxy's parameter checks (app.py `messages`), in its order.
    let count = MESSAGES_DEFAULT_COUNT;
    if (query.count !== undefined) {
      if (!Number.isInteger(query.count) || query.count < 1 || query.count > 999) {
        throw proxyProblem(
          400,
          'invalid',
          'unsupported-value',
          'count must be a whole number from 1.',
          ['count'],
        );
      }
      count = Math.min(query.count, MESSAGES_MAX_COUNT);
    }
    const offset = query.cursor === undefined ? 0 : decodeMockCursor(query.cursor);

    // The sources the proxy reads for this query (messages.py `page`): attempts (with the
    // unreadable ones) unless only transactions are asked for; store-only rows with them unless
    // only unreadable rows are; transactions only with no outcome filter.
    const { kind, outcome } = query;
    const wantAttempts = kind === undefined || kind !== 'fhir-transaction';
    const wantStoreOnly = kind === undefined || kind === 'cuff-send';
    const wantTransactions =
      (kind === undefined || kind === 'fhir-transaction') && outcome === undefined;
    const rows: { at: string | null; entry: MessagesLogEntry }[] = [];
    if (wantAttempts) {
      for (const a of this.#attempts) {
        // An attempt whose AuditEvent was not recorded is not in the audit store.
        if (a.audited) rows.push({ at: a.entry.sentAt, entry: this.#present(a) });
      }
      for (const u of this.#unreadable) rows.push({ at: u.recordedAt, entry: structuredClone(u) });
    }
    if (wantStoreOnly) {
      for (const entry of this.#storeOnlyRows()) rows.push({ at: entry.recordedAt, entry });
    }
    if (wantTransactions) {
      for (const t of this.#allTransactions()) rows.push({ at: t.recordedAt, entry: t });
    }
    // Newest first by send time, ties by id descending; undated rows last (amendment 17).
    rows.sort((x, y) => {
      if (x.at === null || y.at === null) return x.at === y.at ? 0 : x.at === null ? 1 : -1;
      const byTime = Date.parse(y.at) - Date.parse(x.at);
      return byTime !== 0 ? byTime : y.entry.id.localeCompare(x.entry.id);
    });

    // The page is cut BEFORE the filter, as the proxy filters after the join (amendment 4).
    const window = rows.slice(offset, offset + count).map((r) => r.entry);
    const entries = window
      .filter((e) => kind === undefined || e.kind === kind)
      // Amendment 19: only rows WITH an outcome equal to it; transactions never match.
      .filter(
        (e) =>
          outcome === undefined ||
          (e.kind === 'cuff-send' && e.application !== null && e.application.outcome === outcome),
      );
    const failedLookup = window.some((e) => e.kind === 'cuff-send' && e.messageLookup === 'failed');
    return {
      entries,
      nextCursor: offset + count < rows.length ? encodeMockCursor(offset + count) : null,
      unreadable: entries.filter((e) => e.kind === 'unreadable-send-attempt').length,
      partial: this.options.partialLog === true || failedLookup,
    };
  }

  /** The lookup the proxy reports for an attempt (amendment 1). */
  #lookupOf(a: MockAttempt): MessageLookup {
    if (a.entry.result !== 'accepted') return null;
    if (a.storedAs !== null) return 'not-found';
    return this.options.lookupFailure ?? 'confirmed';
  }

  /** An attempt as the proxy presents it: no outcome or Observation unless confirmed. */
  #present(a: MockAttempt): SendAttemptEntry {
    const entry = structuredClone(a.entry);
    const lookup = this.#lookupOf(a);
    const confirmed = lookup === 'confirmed';
    return {
      ...entry,
      // The proxy's row keeps MSH-10 of the attempt as controlId, never MSA-2 (messages.py
      // `commit_ack`): MSA-2 is only in the send result.
      commitAck: entry.commitAck.received
        ? { ...entry.commitAck, controlId: null }
        : entry.commitAck,
      messageLookup: lookup,
      application: confirmed ? entry.application : null,
      observation: confirmed ? entry.observation : null,
      possibleDuplicateOf: null,
    };
  }

  /**
   * The stored messages no attempt claims, as store-only rows (amendments 5 and 14): a send
   * whose AuditEvent was not recorded, the real message behind a wrong derived name, and
   * messages sent straight to the adapter. The hint names an attempt only when exactly ONE
   * qualifies: accepted, MLLP, the same control ID, its own name `not-found`, sent at most
   * 120 s before the message was created.
   */
  #storeOnlyRows(): SendAttemptEntry[] {
    const messages: MockStoredMessage[] = [...this.#direct];
    for (const a of this.#attempts) {
      if (a.entry.result !== 'accepted') continue;
      const lookup = this.#lookupOf(a);
      let name: string | null = null;
      if (!a.audited) name = a.entry.messageName ?? a.entry.derivedMessageName;
      else if (a.storedAs !== null) name = a.storedAs;
      else if (lookup === 'not-found' && a.entry.transport === 'mllp') {
        // The derived rule was wrong for every send (the switch): the store holds the message
        // under a name of its own.
        name = `${MOCK_HL7V2_STORE}/messages/stored-${hash32(a.entry.id).toString(16)}`;
      }
      if (name === null) continue;
      const sentAt = Date.parse(a.entry.sentAt);
      messages.push({
        name,
        createdAt: sentAt + 500,
        sentAt,
        // cuff-sim labels an HTTPS ingest; the adapter cannot label (store_only_row).
        transport: a.entry.transport === 'https' ? 'https' : null,
        controlId: a.entry.controlId,
        application: structuredClone(a.entry.application ?? { outcome: 'pending', reason: null }),
        observation: a.entry.observation,
        mappedAt: a.mappedAt,
      });
    }
    const candidates = this.#attempts.filter(
      (a) => a.audited && a.entry.transport === 'mllp' && this.#lookupOf(a) === 'not-found',
    );
    return messages.map((m) => {
      const qualifying = candidates.filter((a) => {
        const gap = m.createdAt - Date.parse(a.entry.sentAt);
        return a.entry.controlId === m.controlId && gap >= 0 && gap <= 120_000;
      });
      return {
        kind: 'cuff-send',
        id: m.name,
        sentAt: new Date(m.sentAt).toISOString(),
        recordedAt: new Date(m.createdAt).toISOString(),
        transport: m.transport,
        controlId: m.controlId,
        result: 'accepted',
        outcome: '0',
        sample: null,
        commitAck: { received: true, code: 'AA', controlId: null, reason: null, raw: null },
        application: m.application,
        messageName: m.name,
        derivedMessageName: null,
        messageLookup: 'confirmed',
        observation: m.observation,
        source: 'store-only',
        possibleDuplicateOf: qualifying.length === 1 ? (qualifying[0]?.entry.id ?? null) : null,
      };
    });
  }

  /**
   * Transaction rows as the proxy derives them from Provenances (amendment 18): health-sync's
   * runs (seeded), and one v2-to-fhir Bundle per message the mock mapper accepted. Resources
   * are the patient's Observations and their Provenances; `httpStatus` is never recorded.
   */
  #allTransactions(): FhirTransactionEntry[] {
    const out = this.#transactions.map((t) => structuredClone(t));
    const mapped: { at: number; observation: string }[] = [];
    for (const a of this.#attempts) {
      if (a.mappedAt !== null && a.entry.observation !== null) {
        mapped.push({ at: a.mappedAt, observation: a.entry.observation });
      }
    }
    for (const m of this.#direct) {
      if (m.mappedAt !== null && m.observation !== null) {
        mapped.push({ at: m.mappedAt, observation: m.observation });
      }
    }
    for (const { at, observation } of mapped) {
      out.push({
        kind: 'fhir-transaction',
        id: pseudoUuid(`tx-v2-${observation}`),
        recordedAt: new Date(at).toISOString(),
        writer: 'v2-to-fhir',
        httpStatus: null,
        resources: [observation, `Provenance/mock-prov-${hash32(observation).toString(16)}`].sort(),
      });
    }
    return out;
  }

  // -- share ------------------------------------------------------------------------------------

  /**
   * The share as T15 will perform it (contract.ts `ShareStepName`; docs/spikes/s1.md): refusals
   * before OpenEMR is called, then token, Patient search (create only if absent), Encounter
   * search for the reading's day (create only if none: reused otherwise), the Observation POST
   * (OpenEMR refuses a repeat in the same Encounter with its "already exists" 400, reported as
   * already shared), and the Patient, Encounter and Observation read back by id. The mock
   * OpenEMR changes what it stores the way spike S1 saw OpenEMR do (see `openemrStores`).
   */
  async share(request: ShareRequest, options: CallOptions = {}): Promise<ShareResult> {
    await this.#authorised(options.signal);
    const failure = this.options.shareFailure ?? null;
    if (failure === 'not-configured') {
      throw shareProblem(
        503,
        'transient',
        'share-not-configured',
        'Sharing with the clinic is not configured on the server.',
      );
    }
    const id = (request as { observationId?: unknown }).observationId;
    if (typeof id !== 'string' || !SAFE_ID.test(id) || Object.keys(request).length !== 1) {
      throw shareProblem(
        400,
        'invalid',
        'invalid-body',
        'The body must be {"observationId": "<id>"}.',
      );
    }
    const obs = await this.#findObservation(id);
    if (!obs) throw shareProblem(404, 'not-found', 'not-found', 'The Observation was not found.');
    if (obs.status !== 'final') {
      throw shareProblem(
        422,
        'business-rule',
        'observation-not-final',
        `Only final Observations are shared; this one is ${obs.status}.`,
      );
    }
    const code = obs.code.coding?.[0]?.code;
    if (code !== LOINC.heartRate && code !== LOINC.bloodPressurePanel) {
      throw shareProblem(
        422,
        'business-rule',
        'observation-not-shareable',
        'Only heart-rate and blood-pressure readings are shared.',
      );
    }
    if (failure === 'patient-incomplete') {
      throw shareProblem(
        422,
        'required',
        'patient-incomplete',
        'The stored Patient is incomplete: missing birthDate.',
      );
    }

    const emr = this.#openemr;
    const steps: ShareStep[] = [];
    /** A failure part-way: 502 or 504 with the steps so far (web/README.md, "For T15"). */
    const stop = (
      status: 502 | 504,
      reason: 'openemr-unreachable' | 'openemr-refused' | 'openemr-timeout',
      text: string,
    ): ShareError =>
      new ShareError(
        reason,
        status,
        text,
        operationOutcome(status === 504 ? 'timeout' : 'exception', text, reason),
        steps,
      );
    const scope =
      'system/Patient.rs system/Patient.c system/Encounter.rs system/Encounter.c system/Observation.rs system/Observation.c';
    const tokenRequest = {
      grant_type: 'client_credentials',
      scope,
      client_assertion_type: 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
      client_assertion: 'eyJhbGciOiJSUzM4NCJ9.mock-assertion.mock-signature',
    };
    if (failure === 'openemr-unreachable') {
      steps.push(
        traceStep(
          'token',
          'Access token (client credentials, signed assertion)',
          'POST',
          '/oauth2/default/token',
          tokenRequest,
          0,
          null,
          'failed',
        ),
      );
      throw stop(502, 'openemr-unreachable', 'OpenEMR could not be reached; nothing was sent.');
    }
    const tokenAt = emr.tokenAt;
    if (tokenAt !== null && this.#now() - tokenAt < TOKEN_REUSE_MS) {
      // As the proxy: a token still valid in memory is reused, and no token request is sent.
      steps.push(
        traceStep(
          'token',
          `Access token reused from this proxy's memory (obtained ${String(Math.round((this.#now() - tokenAt) / 1000))} s ago, still valid): no token request was sent`,
          'POST',
          '/oauth2/default/token',
          null,
          0,
          null,
          'reused',
        ),
      );
    } else {
      steps.push(
        traceStep(
          'token',
          'Access token (client credentials, signed assertion)',
          'POST',
          '/oauth2/default/token',
          tokenRequest,
          200,
          {
            access_token: 'mock-access-token-never-shown',
            token_type: 'Bearer',
            expires_in: 300,
            scope,
          },
        ),
      );

      emr.tokenAt = this.#now();
    }

    // Patient: search by the PT identifier; create only if absent (OpenEMR ignores conditional
    // create, so a second create would be a second patient).
    const sentPatient = patientForSharing();
    const patientFound = emr.patientId !== null;
    steps.push(
      traceStep(
        'patient-search',
        'Search Patient by identifier',
        'GET',
        '/apis/default/fhir/Patient',
        null,
        200,
        searchset(patientFound ? [emr.stored.get(`Patient/${emr.patientId ?? ''}`)] : []),
        patientFound ? 'found' : 'not-found',
        { identifier: MOCK_PATIENT_IDENTIFIER },
      ),
    );
    if (emr.patientId === null) {
      const uuid = pseudoUuid(`openemr-patient-${String(this.#now())}`);
      steps.push(
        traceStep(
          'patient-create',
          'Create Patient',
          'POST',
          '/apis/default/fhir/Patient',
          sentPatient,
          201,
          {
            pid: 3,
            uuid,
          },
        ),
      );
      emr.patientId = uuid;
      emr.stored.set(`Patient/${uuid}`, openemrStoredPatient(uuid, sentPatient));
    }
    const patientRef = `Patient/${emr.patientId}`;

    // Encounter: one per patient and reading day, reused (so a repeat is caught as a duplicate).
    const effective = obs.effectiveDateTime ?? obs.effectivePeriod?.start ?? '';
    const day = new Date(Date.parse(effective)).toISOString().slice(0, 10);
    const existingEncounter = emr.encounters.get(day) ?? null;
    steps.push(
      traceStep(
        'encounter-search',
        'Search Encounter for the reading’s day',
        'GET',
        '/apis/default/fhir/Encounter',
        null,
        200,
        searchset(existingEncounter ? [emr.stored.get(`Encounter/${existingEncounter}`)] : []),
        existingEncounter ? 'found' : 'not-found',
        { patient: emr.patientId, date: day },
      ),
    );
    let sentEncounter: Encounter | null = null;
    let encounterId = existingEncounter;
    if (encounterId === null) {
      sentEncounter = {
        resourceType: 'Encounter',
        status: 'finished',
        class: { system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode', code: 'AMB' },
        subject: { reference: patientRef },
        period: { start: `${day}T00:00:00Z`, end: `${day}T23:59:59Z` },
      };
      if (failure === 'encounter-refused') {
        steps.push(
          traceStep(
            'encounter-create',
            'Create Encounter for the reading’s day',
            'POST',
            '/apis/default/fhir/Encounter',
            sentEncounter,
            400,
            {
              validationErrors: {
                class: 'Encounter.class must be one of the configured visit categories',
              },
            },
            'failed',
          ),
        );
        throw stop(
          502,
          'openemr-refused',
          'OpenEMR refused to create the Encounter; the reading was not sent.',
        );
      }
      const uuid = pseudoUuid(`openemr-encounter-${day}`);
      steps.push(
        traceStep(
          'encounter-create',
          'Create Encounter for the reading’s day',
          'POST',
          '/apis/default/fhir/Encounter',
          sentEncounter,
          201,
          {
            euuid: uuid,
            encounter: 7 + emr.encounters.size,
            pid: 3,
          },
        ),
      );
      encounterId = uuid;
      emr.encounters.set(day, uuid);
      emr.stored.set(`Encounter/${uuid}`, {
        ...sentEncounter,
        id: uuid,
        meta: { lastUpdated: new Date(this.#now()).toISOString() },
        subject: { reference: patientRef, type: 'Patient' },
      });
    }

    // The Observation: no reference to our store (T06: drop performer, device, meta, identifier).
    const sentObservation: Observation = {
      ...observationForSharing(obs),
      subject: { reference: patientRef },
      encounter: { reference: `Encounter/${encounterId}` },
    };
    const duplicateKey = `${code}|${encounterId}|${String(Date.parse(effective))}`;
    const existing = emr.observations.get(duplicateKey) ?? null;
    if (failure === 'observation-timeout') {
      steps.push(
        traceStep(
          'observation-create',
          `Create Observation (${obs.id ?? ''})`,
          'POST',
          '/apis/default/fhir/Observation',
          sentObservation,
          0,
          null,
          'failed',
        ),
      );
      throw stop(504, 'openemr-timeout', 'OpenEMR did not answer the Observation request in time.');
    }
    let observationId: string;
    if (existing !== null) {
      observationId = existing;
      steps.push(
        traceStep(
          'observation-create',
          `Create Observation (${obs.id ?? ''})`,
          'POST',
          '/apis/default/fhir/Observation',
          sentObservation,
          400,
          {
            validationErrors: {
              effectiveDateTime: `A "${code}" reading already exists for this encounter at this effectiveDateTime (Observation/${existing}); POST does not overwrite a stored reading -- update that Observation with PUT`,
            },
          },
          'already-shared',
        ),
      );
    } else {
      observationId = pseudoUuid(`openemr-${duplicateKey}`);
      steps.push(
        traceStep(
          'observation-create',
          `Create Observation (${obs.id ?? ''})`,
          'POST',
          '/apis/default/fhir/Observation',
          sentObservation,
          201,
          { uuid: observationId },
        ),
      );
      emr.observations.set(duplicateKey, observationId);
      emr.stored.set(
        `Observation/${observationId}`,
        openemrStoredObservation(observationId, sentObservation),
      );
    }

    // The read-back (D13), by id.
    const read = (step: ShareStep['step'], label: string, ref: string, fail = false) => {
      const stored = emr.stored.get(ref) ?? null;
      steps.push(
        fail
          ? traceStep(step, label, 'GET', `/apis/default/fhir/${ref}`, null, 502, null, 'failed')
          : traceStep(step, label, 'GET', `/apis/default/fhir/${ref}`, null, 200, stored),
      );
      return fail ? null : stored;
    };
    const storedPatient = read(
      'patient-read',
      'Read back the Patient',
      patientRef,
    ) as Patient | null;
    const storedEncounter = read(
      'encounter-read',
      'Read back the Encounter',
      `Encounter/${encounterId}`,
    ) as Encounter | null;
    const storedObservation = read(
      'observation-read',
      'Read back the Observation',
      `Observation/${observationId}`,
      failure === 'read-back',
    ) as Observation | null;
    if (failure === 'hosting-timeout') {
      // Everything above happened at the clinic; the page gets only the hosting layer's HTML
      // 504, which the HTTP client turns into exactly this (no OperationOutcome, no steps).
      throw ShareError.from(new ApiError(504, 'The server answered HTTP 504.'));
    }
    return {
      outcome: existing === null ? 'shared' : 'already-shared',
      steps,
      readBack: {
        patient: { sent: patientFound ? null : sentPatient, stored: storedPatient },
        encounter: { sent: sentEncounter, stored: storedEncounter },
        observation: { sent: sentObservation, stored: storedObservation },
      },
    };
  }

  // -- internals --------------------------------------------------------------------------------

  /** The same gate the proxy applies: no valid token, no data. */
  async #authorised(signal?: AbortSignal): Promise<void> {
    await this.#delay();
    // Like fetch: a call whose signal was aborted (the dashboard was torn down) rejects with
    // an AbortError, which callers ignore.
    signal?.throwIfAborted();
    const token = this.options.session.token();
    if (token !== null && this.options.statusMode === 'not-configured') {
      // The proxy cannot check any token without its password secret, so every route but
      // login answers 503 (amendment 11). The session is not ended: there was no 401.
      throw proxyProblem(
        503,
        'transient',
        'login-not-configured',
        'Reviewer login is not configured on the server.',
      );
    }
    if (token === null || !token.startsWith(MOCK_TOKEN_PREFIX)) {
      this.options.session.clear();
      this.options.onUnauthorized();
      throw new UnauthorizedError();
    }
  }

  async #delay(): Promise<void> {
    if (this.#latency > 0) await new Promise((r) => setTimeout(r, this.#latency));
  }

  /**
   * cuff-sim's `parse_request` (services/cuff-sim/cuff_sim/request.py), check for check, with
   * its exact issue texts: each issue is a field in `expression` and a fragment in `diagnostics`
   * ("is required"), all collected before refusing with 422 (`code` `invalid`). In its order:
   * - not a JSON object: 400, `body` "must be a JSON object";
   * - any other key: `body` "only these fields are accepted: ...";
   * - `transport` absent means `mllp`; anything but "mllp" or "https" (null included) is refused;
   * - `sendMalformed` null or absent, or a known sample name that lists the transport;
   * - `systolic`/`diastolic`: null counts as absent; else a JSON integer (not a boolean) from
   *   1 to 400; both required when `sendMalformed` is null or absent;
   * - when both are valid, systolic must be above diastolic, reported with the other issues.
   */
  #checkReading(body: unknown): {
    transport: Transport;
    sendMalformed: string | null;
    sample: MockSampleBehaviour | null;
    systolic: number | null;
    diastolic: number | null;
  } {
    if (typeof body !== 'object' || body === null || Array.isArray(body)) {
      throw cuffRefusal(400, [['body', 'must be a JSON object']], 'invalid');
    }
    const b = body as Record<string, unknown>;
    const issues: [field: string, text: string][] = [];
    const { min, max } = CUFF_SERVICE_LIMITS;
    if (Object.keys(b).some((k) => !(CUFF_FIELDS as readonly string[]).includes(k))) {
      issues.push(['body', `only these fields are accepted: ${CUFF_FIELDS.join(', ')}`]);
    }
    const rawTransport = 'transport' in b ? b.transport : 'mllp';
    const transport = rawTransport === 'mllp' || rawTransport === 'https' ? rawTransport : null;
    if (transport === null) issues.push(['transport', 'must be "mllp" or "https"']);

    const rawSample = b.sendMalformed ?? null;
    let sample: MockSampleBehaviour | null = null;
    if (rawSample !== null) {
      const found = MOCK_SAMPLES.find((s) => s.name === rawSample);
      if (typeof rawSample !== 'string' || !found) {
        issues.push(['sendMalformed', 'must be the name of a malformed sample, or null']);
      } else if (transport !== null && !found.transports.includes(transport)) {
        issues.push(['sendMalformed', `this sample cannot be sent over ${transport}`]);
      } else sample = found;
    }

    const pressure = (field: 'systolic' | 'diastolic'): number | null => {
      const value = b[field] ?? null;
      if (value === null) return null;
      if (typeof value !== 'number' || !Number.isInteger(value)) {
        issues.push([field, 'must be a whole number of mm[Hg]']);
        return null;
      }
      if (value < min || value > max) {
        issues.push([field, `must be from ${min} to ${max} mm[Hg]`]);
        return null;
      }
      return value;
    };
    const systolic = pressure('systolic');
    const diastolic = pressure('diastolic');
    if (rawSample === null) {
      for (const field of ['systolic', 'diastolic'] as const) {
        if ((b[field] ?? null) === null) issues.push([field, 'is required']);
      }
    }
    if (systolic !== null && diastolic !== null && systolic <= diastolic) {
      issues.push(['systolic', 'must be greater than diastolic']);
    }
    if (issues.length > 0 || transport === null) throw cuffRefusal(422, issues, 'invalid');
    return { transport, sendMalformed: sample?.name ?? null, sample, systolic, diastolic };
  }

  /** cuff-sim's token bucket: `burst` sends back to back, refilled at `perMinute`. */
  #takeToken(): void {
    const limit = this.#rateLimit;
    if (!limit) return;
    const now = this.#now();
    const perMs = limit.perMinute / 60_000;
    this.#tokens = Math.min(limit.burst, this.#tokens + (now - this.#tokensAt) * perMs);
    this.#tokensAt = now;
    if (this.#tokens >= 1) {
      this.#tokens -= 1;
      return;
    }
    // Retry-After: whole seconds until a token is back, rounded up (RFC 9110 section 10.2.3).
    const wait = Math.max(1, Math.ceil((1 - this.#tokens) / perMs / 1000));
    throw cuffRefusal(429, [['body', 'too many sends; try again later']], 'throttled', wait);
  }

  /** The configured patient's Observation with this id (heart rate or blood pressure), or null. */
  async #findObservation(id: string): Promise<Observation | null> {
    const bp = this.#bloodPressure().find((o) => o.id === id);
    if (bp) return bp;
    const start = windowStartFromId(id);
    const window = start === null ? null : syntheticRollUp(start);
    const sync = this.#lastSync();
    if (start === null || window === null || start + 5 * 60 * 1000 > sync) return null;
    return Promise.resolve(heartRateObservation(window, sync, this.#zone));
  }

  #observationFor(attempt: MockAttempt): Observation | null {
    if (!attempt.entry.observation) return null;
    return bloodPressureObservation(
      attempt.entry.observation.replace('Observation/', ''),
      attempt.entry.recordedAt,
      attempt.systolic,
      attempt.diastolic,
    );
  }

  #bloodPressure(): Observation[] {
    this.#resolve();
    // Every mapped message has its Observation in the FHIR store, whatever the log can confirm.
    const fromDirect = this.#direct.flatMap((m) =>
      m.observation === null
        ? []
        : [
            bloodPressureObservation(
              m.observation.replace('Observation/', ''),
              new Date(m.sentAt).toISOString(),
              131,
              84,
            ),
          ],
    );
    return [...this.#attempts.map((a) => this.#observationFor(a)), ...fromDirect]
      .filter((o): o is Observation => o !== null)
      .sort((a, b) => (a.effectiveDateTime ?? '').localeCompare(b.effectiveDateTime ?? ''));
  }

  /** The mock "mapper": labels each stored message once its delay has passed. */
  #resolve(): void {
    const now = this.#now();
    for (const a of this.#attempts) {
      if (a.entry.application?.outcome !== 'pending' || a.finalOutcome === null) continue;
      if (now < a.resolveAt) continue;
      a.entry.application = { ...a.finalOutcome };
      // When the mapper wrote its Bundle: a few seconds after the send (also for seeded rows).
      a.mappedAt = Math.min(a.resolveAt, Date.parse(a.entry.sentAt) + this.#mappingDelay);
      if (a.finalOutcome.outcome === 'accepted') {
        a.entry.observation = `Observation/mock-bp-${String(a.seq)}`;
      }
    }
  }

  #record(input: {
    at: number;
    transport: Transport;
    systolic: number;
    diastolic: number;
    sample: string | null;
    commit: 'AA' | 'AE' | null;
    commitReason: string | null;
    final: { outcome: 'accepted' | 'error'; reason: string | null } | null;
    resolveAt: number;
    noAck?: CommitAck & { received: false };
    audited?: boolean;
    storedAs?: string;
  }): MockAttempt {
    const n = this.#sequence++;
    const sample = MOCK_SAMPLES.find((x) => x.name === input.sample);
    // A sample carries its own MSH-10 (or none); a built reading gets a new one in T07's format.
    const controlId = sample ? sample.controlId : mockControlId(`${String(input.at)}-${String(n)}`);
    const stored = input.commit === 'AA';
    const commitAck: CommitAck =
      input.noAck ??
      (input.commit === null
        ? {
            received: false,
            why: 'timeout',
            httpStatus: null,
            detail:
              'No ACK within 10 s: the adapter is still waiting for the end of the MLLP frame.',
            replyPrefixBase64: null,
          }
        : {
            received: true,
            code: input.commit,
            controlId: input.commit === 'AA' ? controlId : null,
            reason: input.commitReason,
            raw: mockAck(input.commit, controlId, input.commitReason, input.at),
          });
    const result = sendResult(commitAck);
    // A timeout is recorded when the read gives up; anything else about a second after sending.
    const recordedMs = input.at + (commitAck.received ? 1000 : 10_000);
    const attempt: MockAttempt = {
      entry: {
        kind: 'cuff-send',
        id: `mock-attempt-${pseudoUuid(`${String(input.at)}-${String(n)}`).slice(0, 13)}`,
        sentAt: new Date(input.at).toISOString(),
        recordedAt: new Date(recordedMs).toISOString(),
        transport: input.transport,
        controlId,
        result,
        outcome: outcomeCode(result),
        sample: input.sample,
        commitAck,
        application: stored ? { outcome: 'pending', reason: null } : null,
        // The ingest response names the stored message over HTTPS only (T13); over MLLP the
        // ACK does not carry it.
        messageName:
          stored && input.transport === 'https'
            ? `${MOCK_HL7V2_STORE}/messages/${hash32(`${String(input.at)}-${String(n)}`).toString(16)}`
            : null,
        // Over MLLP cuff-sim derives the name (T33); filled in by #settleDerivedNames.
        derivedMessageName: null,
        // Presented by #present (the proxy's lookup); never read from here.
        messageLookup: null,
        observation: null,
        source: 'audit-event',
        possibleDuplicateOf: null,
      },
      // A malformed send shows the sample's own bytes (T07), not a message built from the inputs.
      messageSent: sample
        ? this.#hostileSegment(sample.er7)
        : mockOruR01(controlId ?? '', input.at, input.systolic, input.diastolic),
      systolic: input.systolic,
      diastolic: input.diastolic,
      finalOutcome: stored ? input.final : null,
      resolveAt: input.resolveAt,
      seq: n,
      audited: input.audited ?? true,
      derivedName: null,
      storedAs: input.storedAs ?? null,
      mappedAt: null,
    };
    if (stored && input.transport === 'mllp') {
      // cuff-sim hashes the bytes it actually sent. In the mock the configured patient is T07's
      // SYN-000123, so with_patient leaves every sample's bytes as the manifest has them.
      attempt.derivedName = derivedMessageName(MOCK_HL7V2_STORE, attempt.messageSent);
    }
    this.#attempts.push(attempt);
    return attempt;
  }

  /**
   * A sample's bytes, plus (in the `hostile` data set) an NTE (notes and comments) segment holding
   * HOSTILE_MARKUP, ended with the sample's own segment terminator.
   */
  #hostileSegment(er7: string): string {
    if (!this.#hostile) return er7;
    const end = er7.includes('\r\n') ? '\r\n' : er7.includes('\r') ? '\r' : '\n';
    return `${er7}NTE|1||${HOSTILE_MARKUP}${end}`;
  }

  /** Write every finished derived-name hash into its entry (see `MockAttempt.derivedName`). */
  async #settleDerivedNames(): Promise<void> {
    for (const attempt of this.#attempts) {
      if (attempt.derivedName && attempt.entry.derivedMessageName === null) {
        attempt.entry.derivedMessageName = await attempt.derivedName;
      }
    }
  }

  /** The starting log: one attempt per outcome the messages log must be able to show. */
  #seed(): void {
    const now = this.#now();
    const min = 60 * 1000;
    const done = now - 1;
    const sample = (name: string) => MOCK_SAMPLES.find((s) => s.name === name);
    const fromSample = (at: number, name: string, transport: Transport) => {
      const s = sample(name);
      if (!s) return;
      this.#record({
        at,
        transport,
        systolic: 118,
        diastolic: 76,
        sample: s.name,
        commit: s.commitAck,
        commitReason: s.commitReason,
        final: s.application && { outcome: s.application, reason: s.mappingReason },
        resolveAt: done,
      });
    };
    const accepted = { outcome: 'accepted' as const, reason: null };
    this.#record({
      at: now - 170 * min,
      transport: 'mllp',
      systolic: 121,
      diastolic: 79,
      sample: null,
      commit: 'AA',
      commitReason: null,
      final: accepted,
      resolveAt: done,
    });
    this.#record({
      at: now - 140 * min,
      transport: 'https',
      systolic: 117,
      diastolic: 75,
      sample: null,
      commit: 'AA',
      commitReason: null,
      final: accepted,
      resolveAt: done,
    });
    fromSample(now - 110 * min, 'unparseable_no_msh', 'mllp');
    fromSample(now - 95 * min, 'noack_missing_end_block', 'mllp');
    this.#record({
      at: now - 80 * min,
      transport: 'https',
      systolic: 124,
      diastolic: 81,
      sample: null,
      commit: null,
      commitReason: null,
      final: null,
      resolveAt: done,
      noAck: {
        received: false,
        why: 'api-error',
        httpStatus: 503,
        detail: 'The ingest endpoint answered 503 Service Unavailable.',
        replyPrefixBase64: null,
      },
    });
    // A reply that is not a readable ACK: a byte order mark before MSH (as in T31's example).
    this.#record({
      at: now - 70 * min,
      transport: 'mllp',
      systolic: 122,
      diastolic: 80,
      sample: null,
      commit: null,
      commitReason: null,
      final: null,
      resolveAt: done,
      noAck: {
        received: false,
        why: 'unreadable-reply',
        httpStatus: null,
        detail: 'The reply started with a byte order mark, so it could not be read as an ACK.',
        replyPrefixBase64: btoa(
          '\u00ef\u00bb\u00bfMSH|^~\\&|||||20260101143300||ACK|ACK-MOCK|P|2.5.1\rMSA|AA|',
        ),
      },
    });
    fromSample(now - 60 * min, 'unmappable_missing_obx14', 'mllp');
    // An accepted MLLP send whose DERIVED name is wrong: the store has the message under another
    // name, so the attempt's lookup is `not-found` (no outcome, no Observation) and the real
    // message is a store-only row created 2 s later with the same control ID: exactly one
    // candidate, so it carries `possibleDuplicateOf` (amendment 14).
    const wrongAt = now - 45 * min;
    this.#record({
      at: wrongAt,
      transport: 'mllp',
      systolic: 128,
      diastolic: 83,
      sample: null,
      commit: 'AA',
      commitReason: null,
      final: accepted,
      resolveAt: done,
      storedAs: `${MOCK_HL7V2_STORE}/messages/stored-${hash32('wrong-derived-name').toString(16)}`,
    });
    // A message sent straight to the adapter: no attempt anywhere, so a store-only row with
    // no hint, an unknown transport (the adapter cannot label) and its own outcome.
    this.#direct.push({
      name: `${MOCK_HL7V2_STORE}/messages/direct-${hash32('direct-adapter').toString(16)}`,
      createdAt: now - 100 * min,
      sentAt: now - 100 * min - 1000,
      transport: null,
      controlId: 'ADAPTER-0001',
      application: { outcome: 'accepted', reason: null },
      observation: 'Observation/mock-bp-direct',
      mappedAt: now - 100 * min + 4000,
    });
    this.#record({
      at: now - 30 * min,
      transport: 'mllp',
      systolic: 126,
      diastolic: 82,
      sample: null,
      commit: 'AA',
      commitReason: null,
      final: accepted,
      resolveAt: done,
    });
    // Stored but never labelled: the mapper failed or the message was dead-lettered (D7 "pending").
    this.#record({
      at: now - 12 * min,
      transport: 'mllp',
      systolic: 119,
      diastolic: 77,
      sample: null,
      commit: 'AA',
      commitReason: null,
      final: null,
      resolveAt: Number.POSITIVE_INFINITY,
    });
    this.#resolve();

    // An AuditEvent that is ours but unreadable (T31's UnreadableSendAttempt): shown as an error.
    this.#unreadable.push({
      kind: 'unreadable-send-attempt',
      id: 'mock-unreadable-0001',
      recordedAt: new Date(now - 50 * min).toISOString(),
      problem:
        "written in format version 2, newer than this library's 1: the MSA-1 detail is missing",
    });
    // One without an id or a readable time (amendment 6): id "", sorted last.
    this.#unreadable.push({
      kind: 'unreadable-send-attempt',
      id: '',
      recordedAt: null,
      problem: 'the AuditEvent has no id and no recorded time',
    });

    // health-sync runs every 15 minutes (brief, "Ingestion, wearable"): the last eight runs,
    // each listing what it wrote. A run that wrote nothing (the watch on the charger) wrote no
    // Provenance, so the proxy, which derives these rows from Provenances, has no row for it.
    for (let i = 0; i < 8; i++) {
      const at = lastSyncRun(now) - 15 * i * min;
      const ids = heartRateWritesAt(at, this.#zone);
      if (ids.length === 0) continue;
      this.#transactions.push(
        transactionRow(
          `mock-tx-${String(i)}`,
          at,
          'health-sync',
          ids.map((o) => `Observation/${o.id ?? ''}`),
        ),
      );
    }
    // The seeder's assembler (amendment 2) and one the proxy does not recognise.
    const older = lastSyncRun(now) - 26 * 60 * min;
    const seeded = heartRateObservations(older - 10 * min, older, older, this.#zone);
    this.#transactions.push(
      transactionRow(
        'mock-tx-seed',
        older,
        'seed',
        seeded.map((o) => `Observation/${o.id ?? ''}`),
      ),
      transactionRow('mock-tx-unknown', older - 60 * min, 'unknown', [
        seeded[0] ? `Observation/${seeded[0].id ?? ''}` : 'Observation/mock-unknown',
      ]),
    );
  }
}

/** The request fields cuff-sim accepts (`FIELDS` in request.py), in its order. */
const CUFF_FIELDS = ['systolic', 'diastolic', 'transport', 'sendMalformed'] as const;

/**
 * A refusal exactly as cuff-sim writes it (`_operation_outcome` in app.py): one issue per
 * (field, fragment), all with the same `code`, turned into the typed error the HTTP client
 * would make of it.
 */
function cuffRefusal(
  status: number,
  issues: [field: string, text: string][],
  code: string,
  retryAfter: number | null = null,
): ApiError {
  const outcome: OperationOutcome = {
    resourceType: 'OperationOutcome',
    issue: issues.map(([field, text]) => ({
      severity: 'error',
      code,
      diagnostics: text,
      expression: [field],
    })),
  };
  return CuffSendError.from(
    new ApiError(status, `The server answered HTTP ${status}.`, outcome, retryAfter),
  );
}

/**
 * An Observation as the share sends it to OpenEMR, following T06: no server `id` or `meta`, no
 * `identifier` (our conditional-update key means nothing to the clinic), no `device` reference
 * (the Device lives in our store, not in OpenEMR) and no `performer` (it names our Patient;
 * OpenEMR does not keep it, spike S1). `subject` and `encounter` are set by the caller.
 */
function observationForSharing(obs: Observation): Observation {
  const copy = withoutServerFields(obs);
  delete copy.identifier;
  delete copy.device;
  delete copy.performer;
  // OpenEMR 8.5.0 reads only effectiveDateTime: a heart-rate window is sent at its start.
  if (copy.effectivePeriod?.start !== undefined && copy.effectiveDateTime === undefined) {
    copy.effectiveDateTime = copy.effectivePeriod.start;
  }
  delete copy.effectivePeriod;
  return copy;
}

/** A resource as a client sends it for creation: no server-assigned `id` or `meta`. */
function withoutServerFields<T extends Resource>(resource: T): T {
  const copy = { ...resource };
  delete copy.id;
  delete copy.meta;
  return copy;
}

/** A FHIR id as the proxy checks a path id (store.py SAFE_ID): at least one letter or digit. */
const SAFE_ID = /^(?=[A-Za-z0-9.-]*[A-Za-z0-9])[A-Za-z0-9.-]{1,64}$/;

/** An OperationOutcome as fhir-proxy writes one (errors.py `Problem.outcome`). */
function operationOutcome(
  code: string,
  diagnostics: string,
  reason?: string,
  expression?: string[],
): OperationOutcome {
  return {
    resourceType: 'OperationOutcome',
    issue: [
      {
        severity: 'error',
        code,
        ...(reason ? { details: { coding: [{ system: PROXY_REASON_SYSTEM, code: reason }] } } : {}),
        diagnostics,
        ...(expression ? { expression } : {}),
      },
    ],
  };
}

/** The error the HTTP client makes of one of the proxy's refusals. */
function proxyProblem(
  status: number,
  code: string,
  reason: string,
  text: string,
  expression?: string[],
): ApiError {
  return new ApiError(status, text, operationOutcome(code, text, reason, expression));
}

/** A refusal of the share route, before anything is sent (no steps). */
function shareProblem(status: number, code: string, reason: string, text: string): ShareError {
  return ShareError.from(proxyProblem(status, code, reason, text));
}

function notFound(what: string): ApiError {
  return proxyProblem(404, 'not-found', 'not-found', `${what} was not found.`);
}

/** The mock's cursor: opaque to the page, like the proxy's (base64url JSON with a version). */
function encodeMockCursor(offset: number): string {
  return btoa(JSON.stringify({ v: 1, o: offset }))
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '');
}

/** The offset in a mock cursor; anything else is refused as the proxy does (amendment 21). */
function decodeMockCursor(cursor: string): number {
  const bad = proxyProblem(
    400,
    'invalid',
    'invalid-cursor',
    'The cursor is not valid; load the log from the start.',
    ['cursor'],
  );
  if (!/^[A-Za-z0-9_-]{1,8192}$/.test(cursor)) throw bad;
  try {
    const body = JSON.parse(atob(cursor.replaceAll('-', '+').replaceAll('_', '/'))) as unknown;
    const o = (body as { v?: unknown; o?: unknown } | null)?.o;
    if (
      (body as { v?: unknown }).v === 1 &&
      typeof o === 'number' &&
      Number.isInteger(o) &&
      o >= 0
    ) {
      return o;
    }
  } catch {
    // fall through
  }
  throw bad;
}

function transactionRow(
  id: string,
  at: number,
  writer: FhirTransactionEntry['writer'],
  observations: string[],
): FhirTransactionEntry {
  return {
    kind: 'fhir-transaction',
    id,
    recordedAt: new Date(at).toISOString(),
    writer,
    // Nothing records the executeBundle status, so the proxy always sends null.
    httpStatus: null,
    resources: [
      ...observations,
      ...observations.map((o) => `Provenance/mock-prov-${hash32(o).toString(16)}`),
    ].sort(),
  };
}

const NOT_SHOWN = '[not shown]';

/**
 * `extensions.redact` under the FINAL rule for T15 (contract.ts `SHARE_TOKEN_STEP_SECRETS`,
 * `SHARE_FHIR_STEP_SECRETS`): a copy with the value of every listed key replaced, at any depth,
 * comparing names without regard to case.
 */
export function redact(value: unknown, secrets: readonly string[]): unknown {
  const names = new Set(secrets);
  const walk = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(walk);
    if (typeof v === 'object' && v !== null) {
      return Object.fromEntries(
        Object.entries(v).map(([k, x]) => [k, names.has(k.toLowerCase()) ? NOT_SHOWN : walk(x)]),
      );
    }
    return v;
  };
  return walk(value);
}

/**
 * `extensions.trace_step` with the final rule: the url a path only, a search's parameters in
 * `query`, bodies redacted with the token step's list for the token step and the FHIR-safe list
 * for every other step (so FHIR codes stay visible).
 */
function traceStep(
  name: ShareStep['step'],
  label: string,
  method: string,
  path: string,
  requestBody: unknown,
  status: number,
  responseBody: unknown,
  result?: ShareStepResult,
  query: ShareQuery | null = null,
): ShareStep {
  if (!/^\/(?!\/)[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$/.test(path)) {
    throw new Error("a trace step's url is a path with no scheme, host or query");
  }
  const secrets = name === 'token' ? SHARE_TOKEN_STEP_SECRETS : SHARE_FHIR_STEP_SECRETS;
  return {
    step: name,
    label,
    request: { method, url: path, query, body: redact(requestBody, secrets) },
    response: { status, body: redact(responseBody, secrets) },
    result: result ?? (status >= 200 && status < 300 ? 'ok' : 'failed'),
    durationMs: 40 + (hash32(label) % 160),
  };
}

function searchset(resources: (Resource | undefined)[]): unknown {
  const found = resources.filter((r): r is Resource => r !== undefined);
  return {
    resourceType: 'Bundle',
    type: 'searchset',
    total: found.length,
    entry: found.map((resource) => ({ resource })),
  };
}

/** T06's `patient_for_sharing` of the mock patient: PT identifier, official name, gender, birth date. */
function patientForSharing(): Patient {
  const stored = openemrPatient('');
  return {
    resourceType: 'Patient',
    identifier: stored.identifier ?? [],
    name: stored.name ?? [],
    gender: stored.gender ?? 'unknown',
    birthDate: stored.birthDate ?? '',
  };
}

/**
 * What the mock OpenEMR keeps of a Patient: spike S1 saw it drop the identifier's `system`
 * (the value goes into its "external ID" field) and add its own id and meta.
 */
function openemrStoredPatient(id: string, sent: Patient): Patient {
  return {
    ...sent,
    id,
    meta: { lastUpdated: '2026-01-01T00:00:00Z' },
    identifier: (sent.identifier ?? []).map((i) => {
      const copy = { ...i };
      delete copy.system;
      return copy;
    }),
  };
}

/**
 * What the mock OpenEMR returns for an Observation read back by id. Modelled on spike S1
 * ("what is lost on the round trip"): it adds `id` and `meta.profile`, adds `type` to the
 * references, rewrites the `display` texts and unit texts, and writes the time in its own zone
 * (UTC, `+00:00`). For a heart rate it keeps one pulse value: the minimum and maximum
 * components and the period are not kept (its Vitals form has one pulse at one time). The
 * last two are this mock's assumption, not something S1 measured.
 */
function openemrStoredObservation(id: string, sent: Observation): Observation {
  const heartRate = sent.code.coding?.[0]?.code === LOINC.heartRate;
  const utc = (iso: string | undefined) =>
    iso === undefined
      ? undefined
      : new Date(Date.parse(iso)).toISOString().replace('.000Z', '+00:00');
  const stored: Observation = {
    ...structuredClone(sent),
    id,
    meta: {
      profile: [
        heartRate
          ? 'http://hl7.org/fhir/StructureDefinition/heartrate'
          : 'http://hl7.org/fhir/StructureDefinition/bp',
      ],
    },
    code: {
      coding: (sent.code.coding ?? []).map((c) => ({
        ...c,
        display: heartRate ? 'Heart rate' : 'Blood pressure systolic and diastolic',
      })),
    },
    subject: { ...sent.subject, type: 'Patient' },
    encounter: { ...sent.encounter, type: 'Encounter' },
  };
  if (heartRate) {
    delete stored.component;
    delete stored.effectivePeriod;
    delete stored.method;
    const at = utc(sent.effectivePeriod?.start ?? sent.effectiveDateTime);
    if (at !== undefined) stored.effectiveDateTime = at;
    if (stored.valueQuantity) stored.valueQuantity = { ...stored.valueQuantity, unit: '/min' };
  } else {
    const at = utc(sent.effectiveDateTime);
    if (at !== undefined) stored.effectiveDateTime = at;
    stored.component = (sent.component ?? []).map((c) => ({
      ...c,
      valueQuantity: { ...c.valueQuantity, unit: 'mmHg' },
    }));
  }
  return stored;
}
