/**
 * The real client: calls fhir-proxy over HTTPS. The routes are T16's proposal for T14/T15 and are
 * listed in web/README.md ("HTTP contract").
 *
 * Every protected request carries `Authorization: Bearer <token>`. A 401 on any of them clears the
 * session and calls `onUnauthorized`, which the app uses to show the login form again.
 */
import type {
  CuffAttemptTrace,
  CuffReadingRequest,
  CuffSendResult,
  HeartRateTrace,
  LoginResult,
  MalformedSample,
  MessagesLogPage,
  MessagesLogQuery,
  ObservationQuery,
  PulseBridgeApi,
  SendAttemptEntry,
  ShareRequest,
  ShareResult,
  StatusSummary,
  GoogleHealthRollUpWindow,
} from './contract';
import { ApiError, LoginError, UnauthorizedError } from './errors';
import {
  bundleResources,
  isOperationOutcome,
  LOINC_SYSTEM,
  type AuditEvent,
  type Bundle,
  type Device,
  type Encounter,
  type Observation,
  type Patient,
  type Provenance,
  type Resource,
} from './fhir';
import type { Session } from './session';

export interface HttpClientOptions {
  /** fhir-proxy base URL, for example "/api" or "https://proxy.example.test/api". */
  baseUrl: string;
  session: Session;
  /** Called after a 401 on a protected route, once the session has been cleared. */
  onUnauthorized: () => void;
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
  /** False only for the login route, which is the one route without a token. */
  auth?: boolean;
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

  // -- login ------------------------------------------------------------------------------------

  async login(password: string): Promise<LoginResult> {
    let response: Response;
    try {
      response = await this.#send('auth/login', {
        method: 'POST',
        body: { password },
        auth: false,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 0) {
        throw new LoginError('unavailable', 'The server could not be reached. Try again.');
      }
      throw error;
    }
    if (response.status === 401) throw new LoginError('wrong-password', 'Wrong password.');
    if (response.status === 429) {
      const retry = Number.parseInt(response.headers.get('Retry-After') ?? '', 10);
      throw new LoginError(
        'rate-limited',
        'Too many attempts. Wait a minute and try again.',
        Number.isFinite(retry) ? retry : null,
      );
    }
    if (response.status === 503) {
      throw new LoginError('not-configured', 'Reviewer login is not configured on the server.');
    }
    if (!response.ok) {
      throw new LoginError('unavailable', `Login failed (HTTP ${response.status}).`);
    }
    // Body as docs/spikes/s6.md specifies it: {"token": "...", "expires_at": "..."}.
    const body = (await response.json()) as { token?: unknown; expires_at?: unknown };
    if (typeof body.token !== 'string' || typeof body.expires_at !== 'string') {
      throw new LoginError('unavailable', 'The server sent an unexpected login response.');
    }
    const result: LoginResult = { token: body.token, expiresAt: body.expires_at };
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

  getStatus(): Promise<StatusSummary> {
    return this.#json<StatusSummary>('status');
  }

  async searchObservations(query: ObservationQuery): Promise<Observation[]> {
    // FHIR search: token search on code as system|code, and two date prefixes (ge, lt) that
    // together bound the range. _sort=date gives oldest first.
    const params = new URLSearchParams();
    params.set('code', `${LOINC_SYSTEM}|${query.code}`);
    params.append('date', `ge${query.range.start.toISOString()}`);
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
      const bundle: Bundle = await this.#json<Bundle>(url, FHIR_JSON);
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

  async getHeartRateTrace(observationId: string): Promise<HeartRateTrace> {
    const body = await this.#json<{ resources: Bundle; source: GoogleHealthRollUpWindow | null }>(
      `trace/heart-rate/${encodeURIComponent(observationId)}`,
    );
    const resources = bundleResources(body.resources);
    const observation = first<Observation>(resources, 'Observation');
    if (!observation) throw new ApiError(502, 'The trace has no Observation.');
    return {
      observation,
      provenance: first<Provenance>(resources, 'Provenance'),
      device: first<Device>(resources, 'Device'),
      source: body.source,
    };
  }

  listMalformedSamples(): Promise<MalformedSample[]> {
    return this.#json<MalformedSample[]>('cuff/samples');
  }

  async submitCuffReading(request: CuffReadingRequest): Promise<CuffSendResult> {
    const response = await this.#send('cuff/readings', { method: 'POST', body: request });
    return (await response.json()) as CuffSendResult;
  }

  async getCuffAttemptTrace(attemptId: string): Promise<CuffAttemptTrace> {
    const body = await this.#json<{
      attempt: SendAttemptEntry;
      messageSent: string | null;
      resources: Bundle;
    }>(`trace/cuff-attempts/${encodeURIComponent(attemptId)}`);
    const resources = bundleResources(body.resources);
    return {
      attempt: body.attempt,
      messageSent: body.messageSent,
      auditEvent: first<AuditEvent>(resources, 'AuditEvent'),
      observation: first<Observation>(resources, 'Observation'),
      provenance: first<Provenance>(resources, 'Provenance'),
    };
  }

  listMessages(query: MessagesLogQuery = {}): Promise<MessagesLogPage> {
    const params = new URLSearchParams();
    if (query.kind) params.set('kind', query.kind);
    if (query.outcome) params.set('outcome', query.outcome);
    if (query.count !== undefined) params.set('count', String(query.count));
    if (query.cursor) params.set('cursor', query.cursor);
    const qs = params.toString();
    return this.#json<MessagesLogPage>(qs === '' ? 'messages' : `messages?${qs}`);
  }

  async share(request: ShareRequest): Promise<ShareResult> {
    const response = await this.#send('share', { method: 'POST', body: request });
    const body = (await response.json()) as Omit<ShareResult, 'readBack'> & { readBack: Bundle };
    const resources = bundleResources(body.readBack);
    return {
      outcome: body.outcome,
      steps: body.steps,
      readBack: {
        patient: first<Patient>(resources, 'Patient'),
        encounter: first<Encounter>(resources, 'Encounter'),
        observations: resources.filter((r): r is Observation => r.resourceType === 'Observation'),
      },
    };
  }

  // -- plumbing ---------------------------------------------------------------------------------

  #url(path: string): URL {
    return new URL(path, this.#base);
  }

  /**
   * A `next` link from the server is followed only when it points inside the proxy's own base
   * URL, so the bearer token is never sent to another host.
   */
  #sameBase(link: string): URL {
    const url = new URL(link, this.#base);
    if (url.origin !== this.#base.origin || !url.pathname.startsWith(this.#base.pathname)) {
      throw new ApiError(0, 'The server returned a paging link outside the proxy.');
    }
    return url;
  }

  async #json<T>(path: string | URL, accept = JSON_TYPE): Promise<T> {
    const response = await this.#send(path, { accept });
    return (await response.json()) as T;
  }

  async #send(path: string | URL, options: RequestOptions = {}): Promise<Response> {
    const { method = 'GET', body, accept = JSON_TYPE, auth = true } = options;
    const headers = new Headers({ Accept: accept });
    if (body !== undefined) headers.set('Content-Type', JSON_TYPE);
    if (auth) {
      const token = this.options.session.token();
      if (token === null) {
        // No point asking the server: without a token the answer is 401.
        this.#unauthorized();
        throw new UnauthorizedError();
      }
      headers.set('Authorization', `Bearer ${token}`);
    }

    const init: RequestInit = {
      method,
      headers,
      // No cookies in either direction (the proxy sets none, D9); PHI is not kept in the HTTP
      // cache; a redirect is an error rather than a hop that could carry the request elsewhere.
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
    };
    if (body !== undefined) init.body = JSON.stringify(body);

    let response: Response;
    try {
      response = await this.#fetch(typeof path === 'string' ? this.#url(path) : path, init);
    } catch {
      throw new ApiError(0, 'The server could not be reached.');
    }

    if (auth && response.status === 401) {
      this.#unauthorized();
      throw new UnauthorizedError();
    }
    if (!auth || response.ok) return response;
    throw await toApiError(response);
  }

  #unauthorized(): void {
    this.options.session.clear();
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
  const text = outcome?.issue[0]?.diagnostics ?? `The server answered HTTP ${response.status}.`;
  return new ApiError(response.status, text, outcome);
}
