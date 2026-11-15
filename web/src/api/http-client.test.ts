import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, LoginError, UnauthorizedError } from './errors';
import { LOINC } from './fhir';
import type { PulseBridgeApi } from './contract';
import { HttpClient } from './http-client';
import { SESSION_KEY, Session } from './session';

const ORIGIN = 'https://dashboard.example.test';
const TOKEN = 'test-token-not-a-secret';

interface Call {
  url: string;
  init: RequestInit;
}

/** A fake fetch that records each call and answers from a queue of responses. */
function fakeFetch(...responses: Response[]) {
  const calls: Call[] = [];
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: input instanceof Request ? input.url : input.toString(), init: init ?? {} });
    const next = responses.shift();
    return Promise.resolve(next ?? json(200, {}));
  });
  return { fetch: fn as unknown as typeof fetch, calls };
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function header(call: Call | undefined, name: string): string | null {
  return new Headers(call?.init.headers).get(name);
}

/** One body every method can read its fields from, so the loop below needs no per-method setup. */
const ANSWERS_EVERYTHING = {
  token: TOKEN,
  expires_at: new Date(Date.now() + 3_600_000).toISOString(),
  resourceType: 'Bundle',
  type: 'searchset',
  entry: [],
  resources: {
    resourceType: 'Bundle',
    type: 'collection',
    entry: [{ resource: { resourceType: 'Observation', status: 'final', code: {} } }],
  },
  source: null,
  readBack: { resourceType: 'Bundle', type: 'collection', entry: [] },
  entries: [],
  nextCursor: null,
  // A readable cuff send result (parseCuffSendResult refuses one without these).
  transport: 'mllp',
  sentAt: '2026-01-01T00:00:00Z',
  messageSent: 'MSH|^~\\&|CUFFSIM\r',
  ack: { received: true, code: 'AA', controlId: null, reason: null, raw: null },
  audit: { recorded: true },
};

type Method = {
  [K in keyof PulseBridgeApi]: PulseBridgeApi[K] extends (...args: never[]) => unknown ? K : never;
}[keyof PulseBridgeApi];

/**
 * Every method of the API, with how to call it. Typed as a Record over the interface's method
 * names, so adding a method to PulseBridgeApi without adding it here fails the type check.
 */
const CALLS: Record<
  Method,
  ((c: HttpClient, signal: AbortSignal) => Promise<unknown>) | 'no-request'
> = {
  login: (c, signal) => c.login('pw', { signal }),
  logout: 'no-request',
  isLoggedIn: 'no-request',
  getStatus: (c, signal) => c.getStatus({ signal }),
  searchObservations: (c, signal) =>
    c.searchObservations(
      { code: LOINC.heartRate, range: { start: new Date(0), end: new Date(1) } },
      { signal },
    ),
  getHeartRateTrace: (c, signal) => c.getHeartRateTrace('hr-1', { signal }),
  listMalformedSamples: (c, signal) => c.listMalformedSamples({ signal }),
  submitCuffReading: (c, signal) =>
    c.submitCuffReading(
      { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null },
      { signal },
    ),
  getCuffAttemptTrace: (c, signal) => c.getCuffAttemptTrace('a-1', { signal }),
  listMessages: (c, signal) => c.listMessages({}, { signal }),
  share: (c, signal) => c.share({ observations: ['Observation/hr-1'] }, { signal }),
};

const futureLogin = () => ({
  token: TOKEN,
  expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
});

describe('HttpClient', () => {
  let session: Session;
  let onUnauthorized: ReturnType<typeof vi.fn<() => void>>;

  beforeEach(() => {
    sessionStorage.clear();
    session = new Session(sessionStorage);
    onUnauthorized = vi.fn<() => void>();
  });

  function client(f: typeof fetch, baseUrl = '/api') {
    return new HttpClient({ baseUrl, session, onUnauthorized, fetch: f, origin: ORIGIN });
  }

  describe('bearer token', () => {
    it('every method: one backend (D10), bearer token, no cookies, no cache, no redirects, signal', async () => {
      session.save(futureLogin());
      const f = fakeFetch(...Object.keys(CALLS).map(() => json(200, ANSWERS_EVERYTHING)));
      const c = client(f.fetch);
      const signal = new AbortController().signal;
      for (const [name, call] of Object.entries(CALLS)) {
        if (call === 'no-request') continue;
        const before = f.calls.length;
        await call(c, signal);
        expect(f.calls.length, `${name} made no request`).toBeGreaterThan(before);
        for (const req of f.calls.slice(before)) {
          // One backend (D10): every request goes to fhir-proxy's base URL and nowhere else.
          expect(req.url.startsWith(`${ORIGIN}/api/`), `${name} -> ${req.url}`).toBe(true);
          expect(header(req, 'Authorization'), name).toBe(
            name === 'login' ? null : `Bearer ${TOKEN}`,
          );
          // No cookies either way, no HTTP caching of health data, a redirect is an error.
          expect(req.init.credentials, name).toBe('omit');
          expect(req.init.cache, name).toBe('no-store');
          expect(req.init.redirect, name).toBe('error');
          // The caller's AbortSignal reaches fetch, so a torn-down dashboard cancels it.
          expect(req.init.signal, name).toBe(signal);
        }
      }
    });

    it('the method list above covers every method of HttpClient', () => {
      const methods = Object.getOwnPropertyNames(HttpClient.prototype).filter(
        (n) => n !== 'constructor',
      );
      expect(methods.sort()).toEqual(Object.keys(CALLS).sort());
    });

    it('sends no Authorization header on login, even with a token already saved', async () => {
      session.save({ token: 'older-token', expiresAt: futureLogin().expiresAt });
      const login = futureLogin();
      const f = fakeFetch(json(200, { token: login.token, expires_at: login.expiresAt }));
      await client(f.fetch).login('correct horse');

      const [call] = f.calls;
      expect(call?.url).toBe(`${ORIGIN}/api/auth/login`);
      expect(call?.init.method).toBe('POST');
      expect(header(call, 'Authorization')).toBeNull();
      expect(JSON.parse(call?.init.body as string)).toEqual({ password: 'correct horse' });
      expect(session.token()).toBe(TOKEN);
      expect(sessionStorage.getItem(SESSION_KEY)).toContain(TOKEN);
    });

    it('logout() forgets the token without calling the server', () => {
      session.save(futureLogin());
      const f = fakeFetch();
      const c = client(f.fetch);
      expect(c.isLoggedIn()).toBe(true);
      c.logout();
      expect(c.isLoggedIn()).toBe(false);
      expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
      expect(f.calls).toHaveLength(0);
    });

    it('does not call the server at all when there is no token', async () => {
      const f = fakeFetch();
      await expect(client(f.fetch).getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(f.calls).toHaveLength(0);
      expect(onUnauthorized).toHaveBeenCalledOnce();
    });
  });

  describe('401 handling', () => {
    it('clears the session and calls onUnauthorized when a protected call gets 401', async () => {
      session.save(futureLogin());
      const f = fakeFetch(json(401, { error: 'expired' }));
      await expect(client(f.fetch).getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).toHaveBeenCalledOnce();
      expect(session.token()).toBeNull();
      expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    });

    it('a late 401 from a request sent with an old token does not end a newer session', async () => {
      session.save({ token: 'old-token', expiresAt: futureLogin().expiresAt });
      let answer: (r: Response) => void = () => undefined;
      const slow = vi.fn(() => new Promise<Response>((resolve) => (answer = resolve)));
      const c = client(slow);
      const pending = c.getStatus();
      // Meanwhile the user logs out and in again.
      session.save(futureLogin());
      answer(json(401, {}));
      await expect(pending).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).not.toHaveBeenCalled();
      expect(session.token()).toBe(TOKEN);
    });

    it('an aborted request rejects with the AbortError, not as a network failure', async () => {
      session.save(futureLogin());
      const controller = new AbortController();
      const hanging = vi.fn(
        (_url: unknown, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new DOMException('aborted', 'AbortError'));
            });
          }),
      );
      const pending = client(hanging).getStatus({
        signal: controller.signal,
      });
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it('treats 401 on login as a wrong password, not as a session ending', async () => {
      const f = fakeFetch(json(401, { error: 'no' }));
      const error = await client(f.fetch)
        .login('wrong')
        .catch((e: unknown) => e);
      expect(error).toBeInstanceOf(LoginError);
      expect((error as LoginError).reason).toBe('wrong-password');
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it('maps 429 with Retry-After and 503 to their own login failures', async () => {
      const f = fakeFetch(json(429, {}, { 'Retry-After': '42' }), json(503, {}));
      const c = client(f.fetch);
      const limited = (await c.login('x').catch((e: unknown) => e)) as LoginError;
      expect([limited.reason, limited.retryAfterSeconds]).toEqual(['rate-limited', 42]);
      const missing = (await c.login('x').catch((e: unknown) => e)) as LoginError;
      expect(missing.reason).toBe('not-configured');
    });
  });

  describe('errors', () => {
    it('a 429 from the rate-limited cuff route carries Retry-After', async () => {
      session.save(futureLogin());
      const f = fakeFetch(json(429, {}, { 'Retry-After': '30' }));
      const error = (await client(f.fetch)
        .submitCuffReading({ systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null })
        .catch((e: unknown) => e)) as ApiError;
      expect(error).toBeInstanceOf(ApiError);
      expect([error.status, error.retryAfterSeconds]).toEqual([429, 30]);
    });

    it('keeps a FHIR OperationOutcome from an error body', async () => {
      session.save(futureLogin());
      const outcome = {
        resourceType: 'OperationOutcome',
        issue: [{ severity: 'error', code: 'invalid', diagnostics: 'Systolic out of range' }],
      };
      const f = fakeFetch(json(422, outcome));
      const error = (await client(f.fetch)
        .submitCuffReading({ systolic: 999, diastolic: 80, transport: 'mllp', sendMalformed: null })
        .catch((e: unknown) => e)) as ApiError;
      expect(error).toBeInstanceOf(ApiError);
      expect(error.status).toBe(422);
      expect(error.message).toBe('Systolic out of range');
      expect(error.outcome?.issue[0]?.code).toBe('invalid');
      expect(onUnauthorized).not.toHaveBeenCalled();
    });
  });

  describe('Observation search', () => {
    it('builds a FHIR search and follows next links inside the proxy', async () => {
      session.save(futureLogin());
      const obs = (id: string) => ({ resource: { resourceType: 'Observation', id } });
      const f = fakeFetch(
        json(200, {
          resourceType: 'Bundle',
          type: 'searchset',
          entry: [obs('a'), obs('b')],
          link: [{ relation: 'next', url: `${ORIGIN}/api/fhir/Observation?page=2` }],
        }),
        json(200, { resourceType: 'Bundle', type: 'searchset', entry: [obs('c')] }),
      );
      const result = await client(f.fetch).searchObservations({
        code: LOINC.heartRate,
        range: { start: new Date('2026-01-01T00:00:00Z'), end: new Date('2026-01-02T00:00:00Z') },
      });

      expect(result.map((o) => o.id)).toEqual(['a', 'b', 'c']);
      const first = new URL(f.calls[0]?.url ?? '');
      expect(first.pathname).toBe('/api/fhir/Observation');
      expect(first.searchParams.get('code')).toBe('http://loinc.org|8867-4');
      expect(first.searchParams.getAll('date')).toEqual([
        'gt2026-01-01T00:00:00.000Z',
        'lt2026-01-02T00:00:00.000Z',
      ]);
      expect(header(f.calls[0], 'Accept')).toBe('application/fhir+json');
      expect(f.calls[1]?.url).toBe(`${ORIGIN}/api/fhir/Observation?page=2`);
    });

    it('refuses a next link to another host, so the token never leaves the proxy', async () => {
      session.save(futureLogin());
      const f = fakeFetch(
        json(200, {
          resourceType: 'Bundle',
          type: 'searchset',
          link: [{ relation: 'next', url: 'https://elsewhere.example.test/api/fhir/Observation' }],
        }),
      );
      await expect(
        client(f.fetch).searchObservations({
          code: LOINC.heartRate,
          range: { start: new Date(0), end: new Date(1) },
        }),
      ).rejects.toBeInstanceOf(ApiError);
      expect(f.calls).toHaveLength(1);
    });

    it('uses an absolute base URL from configuration as given', async () => {
      session.save(futureLogin());
      const f = fakeFetch(json(200, {}));
      await client(f.fetch, 'https://proxy.example.test/v1').getStatus();
      expect(f.calls[0]?.url).toBe('https://proxy.example.test/v1/status');
    });
  });
});
