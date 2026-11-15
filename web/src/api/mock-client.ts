/**
 * Mock implementation of PulseBridgeApi: synthetic data, in memory, no network at all (the mock
 * build's Content-Security-Policy is `connect-src 'none'`, so a stray request would fail loudly).
 *
 * It behaves like the real client where the page can tell the difference:
 * - any non-empty password logs in, and the token goes into the same sessionStorage session;
 * - every other call checks that token first, and with none it clears the session and calls
 *   `onUnauthorized`, exactly as the HTTP client does on a 401;
 * - inputs outside bounds give the same ApiError 422 with an OperationOutcome.
 */
import type {
  CommitAck,
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  FhirTransactionEntry,
  HeartRateTrace,
  LoginResult,
  MalformedSample,
  MessagesLogEntry,
  MessagesLogPage,
  MessagesLogQuery,
  CallOptions,
  ObservationQuery,
  PulseBridgeApi,
  SendAttemptEntry,
  ShareRequest,
  ShareResult,
  ShareStep,
  UnreadableSendAttemptEntry,
  StatusSummary,
  Transport,
} from './contract';
import { CUFF_BOUNDS } from './contract';
import { ApiError, LoginError, UnauthorizedError } from './errors';
import { LOINC, type Observation, type OperationOutcome, type Resource } from './fhir';
import {
  bloodPressureObservation,
  hash32,
  heartRateObservation,
  heartRateObservations,
  MOCK_SAMPLES,
  MOCK_WATCH,
  mockAck,
  MOCK_HL7V2_STORE,
  mockControlId,
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
}

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
}

export class MockClient implements PulseBridgeApi {
  readonly mode = 'mock' as const;
  readonly #now: () => number;
  readonly #latency: number;
  readonly #mappingDelay: number;
  readonly #attempts: MockAttempt[] = [];
  readonly #transactions: FhirTransactionEntry[] = [];
  readonly #unreadable: UnreadableSendAttemptEntry[] = [];
  readonly #shared = new Set<string>();
  #sequence = 0;

  constructor(private readonly options: MockClientOptions) {
    this.#now = options.now ?? (() => Date.now());
    this.#latency = options.latencyMs ?? 150;
    this.#mappingDelay = options.mappingDelayMs ?? 3000;
    this.#seed();
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
    const checkedAt = new Date(now).toISOString();
    return {
      watchSync: {
        state: 'ok',
        lastSyncAt: new Date(now - 7 * 60 * 1000).toISOString(),
        detail: null,
      },
      observationCounts: {
        watch: heartRateObservations(now - 30 * 864e5, now, now).length,
        cuff: this.#bloodPressure().length,
      },
      fhirStore: { reachable: true, checkedAt, detail: null },
      openemr: { reachable: true, checkedAt, detail: null },
    };
  }

  async searchObservations(
    query: ObservationQuery,
    options: CallOptions = {},
  ): Promise<Observation[]> {
    await this.#authorised(options.signal);
    const start = query.range.start.getTime();
    const end = query.range.end.getTime();
    if (query.code === LOINC.heartRate) return heartRateObservations(start, end, this.#now());
    if (query.code === LOINC.bloodPressurePanel) {
      return this.#bloodPressure().filter((o) => {
        const t = Date.parse(o.effectiveDateTime ?? '');
        return t > start && t < end; // date=gt<start>&date=lt<end>
      });
    }
    throw this.#unprocessable(`Searching for code ${query.code} is not supported.`);
  }

  async getHeartRateTrace(
    observationId: string,
    options: CallOptions = {},
  ): Promise<HeartRateTrace> {
    await this.#authorised(options.signal);
    const start = windowStartFromId(observationId);
    const window = start === null ? null : syntheticRollUp(start);
    if (start === null || window === null || start + 5 * 60 * 1000 > this.#now()) {
      throw notFound(`Observation/${observationId}`);
    }
    const observation = heartRateObservation(window, this.#now());
    return {
      observation,
      provenance: provenanceFor(`Observation/${observationId}`, window.endTime, 'watch'),
      device: MOCK_WATCH,
      source: window,
    };
  }

  async listMalformedSamples(options: CallOptions = {}): Promise<MalformedSample[]> {
    await this.#authorised(options.signal);
    return MOCK_SAMPLES.map(({ name, label, class: cls, transports }) => ({
      name,
      label,
      class: cls,
      transports: [...transports],
    }));
  }

  async submitCuffReading(
    request: CuffReadingRequest,
    options: CallOptions = {},
  ): Promise<CuffSendResult> {
    await this.#authorised(options.signal);
    const { systolic, diastolic, transport, sendMalformed } = request;
    const sample =
      sendMalformed === null ? null : MOCK_SAMPLES.find((s) => s.name === sendMalformed);
    if (sample === undefined) throw this.#unprocessable(`Unknown sample ${String(sendMalformed)}.`);
    if (sample && !sample.transports.includes(transport)) {
      throw this.#unprocessable(`Sample ${sample.name} cannot be sent over ${transport}.`);
    }
    if (sample === null) {
      const issues = boundsIssues(systolic, diastolic);
      if (issues.length > 0) throw this.#unprocessable(issues.join(' '));
    }

    const attempt = this.#record({
      at: this.#now(),
      transport,
      systolic,
      diastolic,
      sample: sample?.name ?? null,
      commit: sample ? sample.commitAck : 'AA',
      commitReason: sample?.commitReason ?? null,
      final: sample
        ? sample.application && { outcome: sample.application, reason: sample.mappingReason }
        : { outcome: 'accepted', reason: null },
      resolveAt: this.#now() + this.#mappingDelay,
    });
    this.#resolve();
    return {
      attemptId: attempt.entry.id,
      sentAt: attempt.entry.sentAt,
      transport,
      controlId: attempt.entry.controlId,
      sample: attempt.entry.sample,
      messageSent: attempt.messageSent,
      messageName: attempt.entry.messageName,
      ack: attempt.entry.commitAck,
      audit: { recorded: true },
    };
  }

  async getCuffAttemptTrace(
    attemptId: string,
    options: CallOptions = {},
  ): Promise<CuffAttemptTrace> {
    await this.#authorised(options.signal);
    this.#resolve();
    const attempt = this.#attempts.find((a) => a.entry.id === attemptId);
    if (!attempt) throw notFound(`AuditEvent/${attemptId}`);
    const observation = this.#observationFor(attempt);
    return {
      attempt: structuredClone(attempt.entry),
      messageSent: attempt.messageSent,
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
    const all: MessagesLogEntry[] = [
      ...this.#attempts.map((a) => structuredClone(a.entry)),
      ...this.#unreadable.map((u) => structuredClone(u)),
      ...this.#transactions.map((t) => structuredClone(t)),
    ]
      .filter((e) => !query.kind || e.kind === query.kind)
      .filter(
        (e) =>
          !query.outcome || (e.kind === 'cuff-send' && e.application?.outcome === query.outcome),
      )
      // Newest first; an unreadable row without a time goes last.
      .sort((a, b) => (b.recordedAt ?? '').localeCompare(a.recordedAt ?? ''));
    const offset = query.cursor ? Number.parseInt(query.cursor, 10) || 0 : 0;
    const count = Math.min(query.count ?? 50, 100);
    const entries = all.slice(offset, offset + count);
    return {
      entries,
      nextCursor: offset + count < all.length ? String(offset + count) : null,
      unreadable: entries.filter((e) => e.kind === 'unreadable-send-attempt').length,
    };
  }

  // -- share ------------------------------------------------------------------------------------

  async share(request: ShareRequest, options: CallOptions = {}): Promise<ShareResult> {
    await this.#authorised(options.signal);
    if (request.observations.length === 0) throw this.#unprocessable('Nothing selected to share.');
    const observations = await Promise.all(request.observations.map((ref) => this.#lookup(ref)));
    const notFinal = observations.filter((o) => o.status !== 'final');
    if (notFinal.length > 0) {
      throw this.#unprocessable(
        'Only final Observations are shared; this window is still preliminary.',
      );
    }

    const firstShare = this.#shared.size === 0;
    const patientId = pseudoUuid('openemr-patient');
    const encounterId = pseudoUuid(`openemr-encounter-${String(this.#sequence++)}`);
    const steps: ShareStep[] = [
      step(
        'token',
        'Token request (client credentials, private_key_jwt)',
        'POST',
        '/oauth2/default/token',
        {
          grant_type: 'client_credentials',
          client_assertion_type: 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
          client_assertion: '[not shown]',
          scope: 'system/Patient.rs system/Encounter.crs system/Observation.crs',
        },
        200,
        { token_type: 'Bearer', expires_in: 300, access_token: '[not shown]' },
      ),
      step(
        'patient-search',
        'Search Patient by identifier',
        'GET',
        '/apis/default/fhir/Patient?identifier=SYN-000123',
        null,
        200,
        { resourceType: 'Bundle', type: 'searchset', total: firstShare ? 0 : 1 },
      ),
    ];
    if (firstShare) {
      steps.push(
        step(
          'patient-create',
          'Create Patient',
          'POST',
          '/apis/default/fhir/Patient',
          withoutServerFields(openemrPatient('')),
          201,
          { resourceType: 'Patient', id: patientId },
        ),
      );
    }
    steps.push(
      step(
        'encounter-create',
        'Create Encounter for this share',
        'POST',
        '/apis/default/fhir/Encounter',
        {
          resourceType: 'Encounter',
          status: 'finished',
          subject: { reference: `Patient/${patientId}` },
        },
        201,
        { resourceType: 'Encounter', id: encounterId },
      ),
    );

    let already = 0;
    const stored: Observation[] = [];
    for (const obs of observations) {
      const ref = `Observation/${obs.id ?? ''}`;
      const body: Observation = {
        ...observationForSharing(obs),
        subject: { reference: `Patient/${patientId}` },
        encounter: { reference: `Encounter/${encounterId}` },
      };
      if (this.#shared.has(ref)) {
        already++;
        steps.push({
          ...step(
            'observation-create',
            `Create ${ref}`,
            'POST',
            '/apis/default/fhir/Observation',
            body,
            400,
            operationOutcome('duplicate', 'Observation already exists'),
          ),
          result: 'already-shared',
        });
      } else {
        this.#shared.add(ref);
        const id = pseudoUuid(`openemr-${ref}`);
        steps.push(
          step(
            'observation-create',
            `Create ${ref}`,
            'POST',
            '/apis/default/fhir/Observation',
            body,
            201,
            { resourceType: 'Observation', id },
          ),
        );
        stored.push({ ...body, id });
      }
    }
    for (const o of stored) {
      steps.push(
        step(
          'read-back',
          `Read back Observation/${o.id ?? ''}`,
          'GET',
          `/apis/default/fhir/Observation/${o.id ?? ''}`,
          null,
          200,
          o,
        ),
      );
    }

    return {
      outcome:
        already === observations.length
          ? 'already-shared'
          : already > 0
            ? 'partly-shared'
            : 'shared',
      steps,
      readBack: {
        patient: openemrPatient(patientId),
        encounter: {
          resourceType: 'Encounter',
          id: encounterId,
          status: 'finished',
          class: { system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode', code: 'AMB' },
          subject: { reference: `Patient/${patientId}` },
          period: { start: new Date(this.#now()).toISOString() },
        },
        observations: stored,
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
    if (token === null || !token.startsWith(MOCK_TOKEN_PREFIX)) {
      this.options.session.clear();
      this.options.onUnauthorized();
      throw new UnauthorizedError();
    }
  }

  async #delay(): Promise<void> {
    if (this.#latency > 0) await new Promise((r) => setTimeout(r, this.#latency));
  }

  #unprocessable(message: string): ApiError {
    return new ApiError(422, message, operationOutcome('invalid', message));
  }

  async #lookup(reference: string): Promise<Observation> {
    const [type, id = ''] = reference.split('/');
    if (type !== 'Observation') throw this.#unprocessable(`${reference} is not an Observation.`);
    const bp = this.#bloodPressure().find((o) => o.id === id);
    if (bp) return bp;
    return (await this.getHeartRateTrace(id)).observation;
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
    return this.#attempts
      .map((a) => this.#observationFor(a))
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
        observation: null,
        source: 'audit-event',
      },
      // A malformed send shows the sample's own bytes (T07), not a message built from the inputs.
      messageSent:
        sample?.er7 ?? mockOruR01(controlId ?? '', input.at, input.systolic, input.diastolic),
      systolic: input.systolic,
      diastolic: input.diastolic,
      finalOutcome: stored ? input.final : null,
      resolveAt: input.resolveAt,
      seq: n,
    };
    this.#attempts.push(attempt);
    return attempt;
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

    // health-sync runs every 15 minutes (brief, "Ingestion, wearable").
    for (let i = 0; i < 8; i++) {
      const at = now - (7 + 15 * i) * min;
      const ids = heartRateObservations(at - 30 * min, at, at).slice(-3);
      this.#transactions.push({
        kind: 'fhir-transaction',
        id: `mock-tx-${String(i)}`,
        recordedAt: new Date(at).toISOString(),
        writer: 'health-sync',
        httpStatus: 200,
        resources: [
          'Patient/mock-patient',
          'Device/mock-watch',
          ...ids.map((o) => `Observation/${o.id ?? ''}`),
        ],
      });
    }
  }
}

function boundsIssues(systolic: number, diastolic: number): string[] {
  const issues: string[] = [];
  const { systolic: s, diastolic: d } = CUFF_BOUNDS;
  if (!Number.isInteger(systolic) || systolic < s.min || systolic > s.max) {
    issues.push(`Systolic must be a whole number from ${s.min} to ${s.max} mm[Hg].`);
  }
  if (!Number.isInteger(diastolic) || diastolic < d.min || diastolic > d.max) {
    issues.push(`Diastolic must be a whole number from ${d.min} to ${d.max} mm[Hg].`);
  }
  if (issues.length === 0 && diastolic >= systolic)
    issues.push('Systolic must be above diastolic.');
  return issues;
}

/**
 * An Observation as the share sends it to OpenEMR, following T06: no server `id` or `meta`, no
 * `identifier` (our conditional-update key means nothing to the clinic) and no `device` reference
 * (the Device lives in our store, not in OpenEMR). `subject` and `encounter` are set by the caller.
 */
function observationForSharing(obs: Observation): Observation {
  const copy = withoutServerFields(obs);
  delete copy.identifier;
  delete copy.device;
  return copy;
}

/** A resource as a client sends it for creation: no server-assigned `id` or `meta`. */
function withoutServerFields<T extends Resource>(resource: T): T {
  const copy = { ...resource };
  delete copy.id;
  delete copy.meta;
  return copy;
}

function operationOutcome(code: string, diagnostics: string): OperationOutcome {
  return { resourceType: 'OperationOutcome', issue: [{ severity: 'error', code, diagnostics }] };
}

function notFound(what: string): ApiError {
  return new ApiError(
    404,
    `${what} was not found.`,
    operationOutcome('not-found', `${what} was not found.`),
  );
}

function step(
  name: ShareStep['step'],
  label: string,
  method: string,
  url: string,
  body: unknown,
  status: number,
  response: unknown,
): ShareStep {
  return {
    step: name,
    label,
    request: { method, url, body },
    response: { status, body: response },
    result: status < 300 ? 'ok' : 'failed',
    durationMs: 40 + (hash32(label) % 160),
  };
}
