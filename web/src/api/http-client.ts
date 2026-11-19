/**
 * The real client: calls fhir-proxy over HTTPS. The routes are the ones T14 built
 * (services/fhir-proxy/README.md, "Routes as built") plus the share route specified for T15;
 * they are listed in web/README.md ("HTTP contract").
 *
 * The session (D16) is the `__session` cookie the proxy sets on `POST /auth/session`. It is
 * HttpOnly, so this page never reads, stores or sends a token itself: every request is
 * same-origin with `credentials: 'same-origin'`, and the browser attaches the cookie. A 401 on a
 * protected route calls `onUnauthorized` (the app returns to the landing view); a 429 on any
 * route calls `onRateLimited` with the wait (the app says how long, and stays where it is).
 */
import type {
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  AuthConfig,
  DemoSession,
  HeartRateTrace,
  MalformedSample,
  MessagesLogPage,
  MessagesLogQuery,
  CallOptions,
  ObservationQuery,
  PulseBridgeApi,
  ShareRequest,
  ShareResult,
  SessionState,
  StatusSummary,
} from './contract';
import {
  ApiError,
  CuffSendError,
  proxyReason,
  rateLimitMessage,
  retryAfterSeconds,
  SESSION_REFUSALS,
  SessionError,
  ShareError,
  UnauthorizedError,
} from './errors';
import {
  bundleResources,
  isOperationOutcome,
  LOINC_SYSTEM,
  type AuditEvent,
  type Bundle,
  type Device,
  type Observation,
  type Provenance,
  type Resource,
} from './fhir';
import {
  parseAuthConfig,
  parseCuffSendResult,
  parseHeartRateSource,
  parseMessagesPage,
  parseSendAttemptEntry,
  parseShareResult,
  parseShareSteps,
  parseStatus,
  UNREADABLE_SHARE,
} from './parse';
export interface HttpClientOptions {
  /**
   * fhir-proxy's base path on the page's own origin, for example "/api" (the Firebase Hosting
   * rewrite). Same-origin only: the cookie is `SameSite=Strict` and the proxy allows no
   * cross-origin credentials.
   */
  baseUrl: string;
  /** Called after a 401 on a protected route that belonged to the current session. */
  onUnauthorized: () => void;
  /** Called after a 429 on a protected route, with the seconds from `Retry-After` (or null). */
  onRateLimited?: (retryAfterSeconds: number | null) => void;
  fetch?: typeof fetch;
  /** Used to resolve a relative base URL; the page's own location in the browser. */
  origin?: string;
}

/** Upper bound on search pages followed, so a broken `next` link cannot loop forever. */
export const MAX_SEARCH_PAGES = 50;
/** Page size requested for Observation searches. 30 days of 5-minute windows is about 8,640. */
export const SEARCH_PAGE_SIZE = 1000;

const FHIR_JSON = 'application/fhir+json';
const JSON_TYPE = 'application/json';

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  accept?: string;
  /**
   * False for the public session routes (`auth/config`, `auth/session`, `auth/logout`): their
   * answers, a 401 or 429 included, are the caller's to read, not the app's.
   */
  auth?: boolean;
  signal?: AbortSignal | undefined;
  /** Return a non-2xx response (after the 401 handling) instead of throwing; the share uses it. */
  allowError?: boolean;
}

export class HttpClient implements PulseBridgeApi {
  readonly mode = 'http' as const;
  readonly #base: URL;
  readonly #fetch: typeof fetch;

  constructor(private readonly options: HttpClientOptions) {
    const origin = options.origin ?? globalThis.location.origin;
    // A trailing slash makes relative paths resolve inside the base path, not beside it.
    this.#base = new URL(options.baseUrl.replace(/\/*$/, '/'), origin);
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  /**
   * Counts session changes (start, logout). A request remembers the value it was sent under, so a
   * slow request from an earlier session that answers 401 after a new session started cannot end
   * the new one. (The page cannot compare tokens: it never sees one.)
   */
  #generation = 0;
  /** The logout still being sent, if any; a new session waits for it (see `startSession`). */
  #loggingOut: Promise<void> | null = null;

  // -- session (D16) ----------------------------------------------------------------------------

  async getAuthConfig(options: CallOptions = {}): Promise<AuthConfig> {
    const response = await this.#sessionRoute('auth/config', { signal: options.signal });
    if (!response.ok) throw await sessionError(response);
    try {
      return parseAuthConfig(await response.json());
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new SessionError('unavailable', 'The server sent an unexpected bot-check setting.');
    }
  }

  async startSession(recaptchaToken: string, options: CallOptions = {}): Promise<DemoSession> {
    // A logout's answer clears the cookie (Max-Age=0). Were it to arrive after this request's
    // answer, it would clear the NEW cookie, so a session never starts while a logout is out.
    if (this.#loggingOut) await this.#loggingOut;
    const response = await this.#sessionRoute('auth/session', {
      method: 'POST',
      body: { recaptcha_token: recaptchaToken },
      signal: options.signal,
    });
    if (!response.ok) throw await sessionError(response);
    let body: { expires_at?: unknown } | null = null;
    try {
      body = (await response.json()) as { expires_at?: unknown } | null;
    } catch {
      // Not JSON: refused below.
    }
    if (typeof body?.expires_at !== 'string' || Number.isNaN(Date.parse(body.expires_at))) {
      throw new SessionError('unavailable', 'The server sent an unexpected session answer.');
    }
    this.#generation++;
    return { expiresAt: body.expires_at };
  }

  async checkSession(options: CallOptions = {}): Promise<SessionState> {
    // Any protected GET would do; /status is the one the dashboard loads first anyway.
    const response = await this.#sessionRoute('status', { signal: options.signal });
    if (response.status === 401) return 'none';
    if (response.ok) return 'active';
    throw await toApiError(response);
  }

  logout(): Promise<void> {
    // From now on a 401 from a request sent before this belongs to an ended session.
    this.#generation++;
    const done = this.#sessionRoute('auth/logout', { method: 'POST' }).then(
      () => undefined,
      // Best effort: if the answer is lost the cookie may stay until it expires; a reload would
      // then find the session still active. Nothing here can do better.
      () => undefined,
    );
    this.#loggingOut = done;
    void done.then(() => {
      if (this.#loggingOut === done) this.#loggingOut = null;
    });
    return done;
  }

  /** A public session route: the response is returned whatever its status. */
  #sessionRoute(
    path: string,
    options: Pick<RequestOptions, 'method' | 'body' | 'signal'>,
  ): Promise<Response> {
    return this.#send(path, { ...options, auth: false }).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 0) {
        throw new SessionError('unavailable', 'The server could not be reached. Try again.');
      }
      throw error;
    });
  }

  // -- reads ------------------------------------------------------------------------------------

  async getStatus(options: CallOptions = {}): Promise<StatusSummary> {
    // health-sync's part (watchSync) is checked: an unknown state reads as `unknown`.
    return parseStatus(await this.#json<unknown>('status', JSON_TYPE, options.signal));
  }

  async searchObservations(
    query: ObservationQuery,
    options: CallOptions = {},
  ): Promise<Observation[]> {
    // FHIR search: token search on code as system|code, and two date prefixes (gt, lt) that
    // together bound the range; see TimeRange for the Period rule. _sort=date gives oldest first.
    const params = new URLSearchParams();
    params.set('code', `${LOINC_SYSTEM}|${query.code}`);
    params.append('date', `gt${query.range.start.toISOString()}`);
    params.append('date', `lt${query.range.end.toISOString()}`);
    params.set('_sort', 'date');
    params.set('_count', String(SEARCH_PAGE_SIZE));

    const results: Observation[] = [];
    let url: URL | null = this.#url(`fhir/Observation?${params.toString()}`);
    for (let page = 0; url !== null; page++) {
      if (page >= MAX_SEARCH_PAGES) {
        throw new ApiError(0, `Search returned more than ${MAX_SEARCH_PAGES} pages.`);
      }
      // Typed as a Bundle of any resource: a searchset may also carry an OperationOutcome entry
      // (search mode "outcome"), which is skipped here.
      const bundle: Bundle = await this.#json<Bundle>(url, FHIR_JSON, options.signal);
      results.push(
        ...bundleResources(bundle).filter(
          (r): r is Observation => r.resourceType === 'Observation',
        ),
      );
      const next = bundle.link?.find((l) => l.relation === 'next')?.url;
      url = next === undefined ? null : this.#sameBase(next);
    }
    return results;
  }

  async getObservation(observationId: string, options: CallOptions = {}): Promise<Observation> {
    // The read route each search entry's fullUrl names (amendment 20). It takes no parameters.
    const body = await this.#json<unknown>(
      `fhir/Observation/${encodeURIComponent(observationId)}`,
      FHIR_JSON,
      options.signal,
    );
    const resource = body as Partial<Observation> | null;
    if (resource?.resourceType !== 'Observation' || resource.id !== observationId) {
      throw new ApiError(502, 'The proxy answered with something that is not this Observation.');
    }
    return resource as Observation;
  }

  async getHeartRateTrace(
    observationId: string,
    options: CallOptions = {},
  ): Promise<HeartRateTrace> {
    // Wire shape: {"resources": Bundle, "source": {"kind", "window"} | null}. Parsed shape:
    // the Bundle's resources by type, and `source` checked (parse.ts).
    const body = await this.#json<{ resources: Bundle; source: unknown }>(
      `trace/heart-rate/${encodeURIComponent(observationId)}`,
      JSON_TYPE,
      options.signal,
    );
    const resources = bundleResources(body.resources);
    const observation = first<Observation>(resources, 'Observation');
    if (!observation) throw new ApiError(502, 'The trace has no Observation.');
    return {
      observation,
      provenance: first<Provenance>(resources, 'Provenance'),
      device: first<Device>(resources, 'Device'),
      source: parseHeartRateSource(body.source),
    };
  }

  listMalformedSamples(options: CallOptions = {}): Promise<MalformedSample[]> {
    return this.#json<MalformedSample[]>('cuff/samples', JSON_TYPE, options.signal);
  }

  async submitCuffReading(
    request: CuffReadingRequest,
    options: CallOptions = {},
  ): Promise<CuffSendResult> {
    let response: Response;
    try {
      response = await this.#send('cuff/readings', {
        method: 'POST',
        body: request,
        signal: options.signal,
      });
    } catch (error) {
      // cuff-sim's documented refusals (400, 413, 422, 429, 500) and a 504 become typed errors.
      throw error instanceof ApiError ? CuffSendError.from(error) : error;
    }
    return parseCuffSendResult(await response.json());
  }

  async getCuffAttemptTrace(
    attemptId: string,
    options: CallOptions = {},
  ): Promise<CuffAttemptTrace> {
    const body = await this.#json<{
      attempt: unknown;
      messageSent: unknown;
      resources: Bundle;
    }>(`trace/cuff-attempts/${encodeURIComponent(attemptId)}`, JSON_TYPE, options.signal);
    // The attempt is a log row, checked as one (messageLookup, no outcome unless confirmed).
    const attempt = parseSendAttemptEntry(body.attempt);
    if (attempt === null) throw new ApiError(502, 'The trace has no readable send attempt.');
    const resources = bundleResources(body.resources);
    return {
      attempt,
      messageSent: typeof body.messageSent === 'string' ? body.messageSent : null,
      auditEvent: first<AuditEvent>(resources, 'AuditEvent'),
      observation: first<Observation>(resources, 'Observation'),
      provenance: first<Provenance>(resources, 'Provenance'),
    };
  }

  async listMessages(
    query: MessagesLogQuery = {},
    options: CallOptions = {},
  ): Promise<MessagesLogPage> {
    const params = new URLSearchParams();
    if (query.kind) params.set('kind', query.kind);
    if (query.outcome) params.set('outcome', query.outcome);
    if (query.count !== undefined) params.set('count', String(query.count));
    if (query.cursor) params.set('cursor', query.cursor);
    const qs = params.toString();
    // Checked row by row (parse.ts): messageLookup, store-only rows, writers, `partial`.
    return parseMessagesPage(
      await this.#json<unknown>(
        qs === '' ? 'messages' : `messages?${qs}`,
        JSON_TYPE,
        options.signal,
      ),
    );
  }

  async share(request: ShareRequest, options: CallOptions = {}): Promise<ShareResult> {
    let response: Response;
    try {
      // Exactly the documented body: the Observation id, nothing a caller could widen.
      response = await this.#send('share', {
        method: 'POST',
        body: { observationId: request.observationId },
        signal: options.signal,
        allowError: true,
      });
    } catch (error) {
      throw error instanceof ApiError && !(error instanceof ShareError)
        ? ShareError.from(error)
        : error;
    }
    if (response.ok) {
      // A 200 the page cannot read is not "nothing happened": the proxy may have done any part
      // of the share. ShareError 502 without an OperationOutcome says exactly that.
      try {
        return parseShareResult(await response.json());
      } catch (error) {
        if (options.signal?.aborted) throw error;
        throw ShareError.from(new ApiError(502, UNREADABLE_SHARE));
      }
    }
    // A failure: an OperationOutcome, or (502/504 part-way) `{operationOutcome, steps}`
    // with the steps completed so far (web/README.md, "For T15: the share route").
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Not JSON.
    }
    const envelope =
      typeof body === 'object' && body !== null && 'operationOutcome' in body
        ? (body as { operationOutcome: unknown; steps?: unknown })
        : null;
    const outcomeBody = envelope ? envelope.operationOutcome : body;
    const outcome = isOperationOutcome(outcomeBody) ? outcomeBody : null;
    const retryAfter = retryAfterSeconds(response.headers.get('Retry-After'));
    const text =
      response.status === 429
        ? rateLimitMessage(retryAfter)
        : (outcome?.issue[0]?.diagnostics ?? `The server answered HTTP ${response.status}.`);
    throw ShareError.from(
      new ApiError(response.status, text, outcome, retryAfter),
      parseShareSteps(envelope?.steps),
    );
  }

  // -- plumbing ---------------------------------------------------------------------------------

  #url(path: string): URL {
    return new URL(path, this.#base);
  }

  /**
   * A `next` link from the server is followed only when it points inside the proxy's own base
   * URL: the page has one backend (D10), and only that path carries the session cookie.
   */
  #sameBase(link: string): URL {
    const url = new URL(link, this.#base);
    if (url.origin !== this.#base.origin || !url.pathname.startsWith(this.#base.pathname)) {
      throw new ApiError(0, 'The server returned a paging link outside the proxy.');
    }
    return url;
  }

  async #json<T>(path: string | URL, accept = JSON_TYPE, signal?: AbortSignal): Promise<T> {
    const response = await this.#send(path, { accept, signal });
    return (await response.json()) as T;
  }

  async #send(path: string | URL, options: RequestOptions = {}): Promise<Response> {
    const {
      method = 'GET',
      body,
      accept = JSON_TYPE,
      auth = true,
      signal,
      allowError = false,
    } = options;
    const headers = new Headers({ Accept: accept });
    if (body !== undefined) headers.set('Content-Type', JSON_TYPE);
    const generation = this.#generation;

    const init: RequestInit = {
      method,
      headers,
      // The session is the HttpOnly `__session` cookie: the browser sends it on same-origin
      // requests only, and the page never touches it. PHI is not kept in the HTTP cache; a
      // redirect is an error rather than a hop that could carry the request elsewhere.
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
    };
    if (body !== undefined) init.body = JSON.stringify(body);
    if (signal) init.signal = signal;

    let response: Response;
    try {
      response = await this.#fetch(typeof path === 'string' ? this.#url(path) : path, init);
    } catch (error) {
      // An aborted request (logout, teardown) rejects with the AbortError itself, which callers
      // ignore; anything else is a network failure.
      if (signal?.aborted) throw error;
      throw new ApiError(0, 'The server could not be reached.');
    }

    if (!auth) return response;
    if (response.status === 401) {
      // 401 `session-required`. Only end the session this request was sent under: a slow request
      // from before a logout or a new session must not send the visitor back to the landing view.
      if (generation === this.#generation) this.#unauthorized();
      throw new UnauthorizedError();
    }
    if (response.status === 429) {
      // T46: any route can answer 429 `rate-limited`. Say how long to wait; the session stays.
      this.options.onRateLimited?.(retryAfterSeconds(response.headers.get('Retry-After')));
    }
    if (response.ok || allowError) return response;
    throw await toApiError(response);
  }

  #unauthorized(): void {
    // The session is over on the server's side; count it as a change so the next 401s from the
    // same burst of requests do not call back again.
    this.#generation++;
    this.options.onUnauthorized();
  }
}

function first<T extends Resource>(resources: Resource[], type: T['resourceType']): T | null {
  return (resources.find((r) => r.resourceType === type) as T | undefined) ?? null;
}

/** Turns an error response into an ApiError, keeping a FHIR OperationOutcome body if present. */
async function toApiError(response: Response): Promise<ApiError> {
  let outcome = null;
  try {
    const body: unknown = await response.json();
    if (isOperationOutcome(body)) outcome = body;
  } catch {
    // Not JSON.
  }
  const retryAfter = retryAfterSeconds(response.headers.get('Retry-After'));
  // A 429 reads the same on every route (T46), whatever the body says.
  const text =
    response.status === 429
      ? rateLimitMessage(retryAfter)
      : (outcome?.issue[0]?.diagnostics ?? `The server answered HTTP ${response.status}.`);
  return new ApiError(response.status, text, outcome, retryAfter);
}

/**
 * The `SessionError` for a refused `auth/config` or `auth/session`, from the proxy's reason code
 * (errors.ts, `SessionFailure`). Only an exact code is trusted: a 503 page from the hosting layer
 * is "unavailable", not "the demo is busy".
 */
async function sessionError(response: Response): Promise<SessionError> {
  const { status } = response;
  const retryAfter = retryAfterSeconds(response.headers.get('Retry-After'));
  if (status === 429)
    return new SessionError('rate-limited', rateLimitMessage(retryAfter), retryAfter);
  let reason: string | null = null;
  try {
    const body: unknown = await response.json();
    if (isOperationOutcome(body)) reason = proxyReason(body);
  } catch {
    // Not JSON.
  }
  // The reason codes this route documents, each only with its own status.
  const known: Partial<Record<number, readonly (keyof typeof SESSION_REFUSALS)[]>> = {
    403: ['bot-check-failed', 'origin-refused'],
    503: ['demo-busy', 'bot-check-unavailable'],
  };
  const match = known[status]?.find((r) => r === reason);
  if (match) return new SessionError(match, SESSION_REFUSALS[match]);
  return new SessionError('unavailable', `The demo could not start (HTTP ${status}). Try again.`);
}
