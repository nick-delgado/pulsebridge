import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, SessionError, UnauthorizedError } from './errors';
import { LOINC } from './fhir';
import type { PulseBridgeApi } from './contract';
import { HttpClient } from './http-client';

const ORIGIN = 'https://dashboard.example.test';
/** Shaped like a key id; synthetic. */
const SITE_KEY = 'test-site-key-0000000000000000000000000000';

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
  recaptcha_site_key: SITE_KEY,
  recaptcha_action: 'view_demo',
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
  partial: false,
  // A readable trace attempt (parseSendAttemptEntry) and share result (parseShareResult).
  attempt: {
    kind: 'cuff-send',
    id: 'a-1',
    sentAt: '2026-01-01T00:00:00.000Z',
    result: 'rejected',
    commitAck: { received: true, code: 'AE', controlId: null, reason: null, raw: null },
  },
  outcome: 'shared',
  steps: [],
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
const CALLS: Record<Method, (c: HttpClient, signal: AbortSignal) => Promise<unknown>> = {
  getAuthConfig: (c, signal) => c.getAuthConfig({ signal }),
  startSession: (c, signal) => c.startSession('recaptcha-token', { signal }),
  checkSession: (c, signal) => c.checkSession({ signal }),
  // Not cancellable on purpose: the dashboard's teardown must not abort the logout.
  logout: (c) => c.logout(),
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
  share: (c, signal) => c.share({ observationId: 'hr-1' }, { signal }),
  getObservation: (c, signal) => c.getObservation('hr-1', { signal }),
};

/** An OperationOutcome with the proxy's reason code, as every proxy refusal is sent. */
function problem(reason: string, text = 'x') {
  return {
    resourceType: 'OperationOutcome',
    issue: [
      {
        severity: 'error',
        code: 'forbidden',
        diagnostics: text,
        details: { coding: [{ system: 'urn:pulsebridge:fhir-proxy:reason', code: reason }] },
      },
    ],
  };
}

describe('HttpClient', () => {
  let onUnauthorized: ReturnType<typeof vi.fn<() => void>>;
  let onRateLimited: ReturnType<typeof vi.fn<(seconds: number | null) => void>>;

  beforeEach(() => {
    onUnauthorized = vi.fn<() => void>();
    onRateLimited = vi.fn<(seconds: number | null) => void>();
  });

  function client(f: typeof fetch, baseUrl = '/api') {
    return new HttpClient({ baseUrl, onUnauthorized, onRateLimited, fetch: f, origin: ORIGIN });
  }

  describe('the session cookie (D16)', () => {
    it('every method: one backend (D10), no token header, same-origin cookie, no cache, no redirects, signal', async () => {
      // The read route must answer with the Observation asked for; everything else shares one body.
      const f = fakeFetch(
        ...Object.keys(CALLS).map((name) =>
          json(
            200,
            name === 'getObservation'
              ? { resourceType: 'Observation', id: 'hr-1', status: 'final', code: {} }
              : ANSWERS_EVERYTHING,
          ),
        ),
      );
      const c = client(f.fetch);
      const signal = new AbortController().signal;
      for (const [name, call] of Object.entries(CALLS)) {
        const before = f.calls.length;
        await call(c, signal);
        expect(f.calls.length, `${name} made no request`).toBeGreaterThan(before);
        for (const req of f.calls.slice(before)) {
          // One backend (D10): every request goes to fhir-proxy's base URL and nowhere else.
          expect(req.url.startsWith(`${ORIGIN}/api/`), `${name} -> ${req.url}`).toBe(true);
          // The page holds no token: the HttpOnly cookie is the browser's to send.
          expect(header(req, 'Authorization'), name).toBeNull();
          expect(req.init.credentials, name).toBe('same-origin');
          expect(req.init.cache, name).toBe('no-store');
          expect(req.init.redirect, name).toBe('error');
          // The caller's AbortSignal reaches fetch, so a torn-down dashboard cancels it.
          expect(req.init.signal, name).toBe(name === 'logout' ? undefined : signal);
        }
      }
    });

    it('the method list above covers every method of HttpClient', () => {
      const methods = Object.getOwnPropertyNames(HttpClient.prototype).filter(
        (n) => n !== 'constructor',
      );
      expect(methods.sort()).toEqual(Object.keys(CALLS).sort());
    });

    it('getAuthConfig reads GET /auth/config', async () => {
      const f = fakeFetch(
        json(200, { recaptcha_site_key: SITE_KEY, recaptcha_action: 'view_demo' }),
      );
      expect(await client(f.fetch).getAuthConfig()).toEqual({
        recaptchaSiteKey: SITE_KEY,
        recaptchaAction: 'view_demo',
      });
      expect(f.calls[0]?.url).toBe(`${ORIGIN}/api/auth/config`);
      expect(f.calls[0]?.init.method).toBe('GET');
    });

    it.each([
      ['no key', { recaptcha_action: 'view_demo' }],
      [
        'a key that would change the script URL',
        { recaptcha_site_key: `${SITE_KEY}&x=1`, recaptcha_action: 'view_demo' },
      ],
      ['a short key', { recaptcha_site_key: 'abc', recaptcha_action: 'view_demo' }],
      ['an action with a space', { recaptcha_site_key: SITE_KEY, recaptcha_action: 'view demo' }],
      ['no action', { recaptcha_site_key: SITE_KEY }],
    ])('getAuthConfig refuses a config with %s', async (_what, body) => {
      const error = await client(fakeFetch(json(200, body)).fetch)
        .getAuthConfig()
        .catch((e: unknown) => e);
      expect(error).toMatchObject({ name: 'SessionError', reason: 'unavailable' });
    });

    it('startSession posts exactly {recaptcha_token} to /auth/session and returns the expiry', async () => {
      const expires = '2026-06-11T09:20:00Z';
      const f = fakeFetch(json(200, { expires_at: expires }));
      expect(await client(f.fetch).startSession('the-token')).toEqual({ expiresAt: expires });
      const [call] = f.calls;
      expect(call?.url).toBe(`${ORIGIN}/api/auth/session`);
      expect(call?.init.method).toBe('POST');
      expect(header(call, 'Content-Type')).toBe('application/json');
      expect(JSON.parse(call?.init.body as string)).toEqual({ recaptcha_token: 'the-token' });
    });

    it('startSession refuses a 200 without a readable expires_at', async () => {
      for (const body of [{}, { expires_at: 'soon' }, { expires_at: 5 }]) {
        const error = await client(fakeFetch(json(200, body)).fetch)
          .startSession('t')
          .catch((e: unknown) => e);
        expect(error).toMatchObject({ reason: 'unavailable' });
      }
    });

    it.each([
      [403, 'bot-check-failed', 'bot-check-failed', /bot check did not let this visit through/],
      [403, 'origin-refused', 'origin-refused', /does not accept requests from this address/],
      [503, 'demo-busy', 'demo-busy', /^The demo is busy, try again tomorrow\.$/],
      [503, 'bot-check-unavailable', 'bot-check-unavailable', /not available right now/],
      // Only an exact code with its own status is trusted.
      [503, 'bot-check-failed', 'unavailable', /HTTP 503/],
      [403, 'demo-busy', 'unavailable', /HTTP 403/],
      [400, 'invalid-body', 'unavailable', /HTTP 400/],
    ])('startSession: %i %s is SessionError %s', async (status, code, reason, text) => {
      const error = (await client(fakeFetch(json(status, problem(code))).fetch)
        .startSession('t')
        .catch((e: unknown) => e)) as SessionError;
      expect(error).toBeInstanceOf(SessionError);
      expect(error.reason).toBe(reason);
      expect(error.message).toMatch(text);
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it('startSession: a 503 page from the hosting layer (not JSON) is not "busy"', async () => {
      const page = new Response('<html>Service Unavailable</html>', { status: 503 });
      const error = await client(fakeFetch(page).fetch)
        .startSession('t')
        .catch((e: unknown) => e);
      expect(error).toMatchObject({ reason: 'unavailable' });
    });

    it('startSession: 429 is rate-limited with the wait from Retry-After', async () => {
      const f = fakeFetch(json(429, problem('rate-limited'), { 'Retry-After': '42' }));
      const error = (await client(f.fetch)
        .startSession('t')
        .catch((e: unknown) => e)) as SessionError;
      expect([error.reason, error.retryAfterSeconds]).toEqual(['rate-limited', 42]);
      expect(error.message).toBe('Too many requests. Wait 42 seconds, then try again.');
    });

    it('startSession: no connection is "unavailable"', async () => {
      const down = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
      const error = await client(down)
        .startSession('t')
        .catch((e: unknown) => e);
      expect(error).toMatchObject({ reason: 'unavailable', message: /could not be reached/ });
    });

    it('checkSession: 200 is active, 401 is none without ending anything, other statuses reject', async () => {
      const f = fakeFetch(
        json(200, ANSWERS_EVERYTHING),
        json(401, problem('session-required')),
        json(500, {}),
      );
      const c = client(f.fetch);
      expect(await c.checkSession()).toBe('active');
      expect(await c.checkSession()).toBe('none');
      expect(onUnauthorized).not.toHaveBeenCalled();
      await expect(c.checkSession()).rejects.toMatchObject({ status: 500 });
      expect(f.calls.map((call) => new URL(call.url).pathname)).toEqual([
        '/api/status',
        '/api/status',
        '/api/status',
      ]);
    });

    it('logout posts to /auth/logout and does not reject when the server is down', async () => {
      const f = fakeFetch(new Response(null, { status: 204 }));
      await client(f.fetch).logout();
      expect(f.calls[0]?.url).toBe(`${ORIGIN}/api/auth/logout`);
      expect(f.calls[0]?.init.method).toBe('POST');
      const down = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
      await expect(client(down).logout()).resolves.toBeUndefined();
    });

    it('a new session waits until a logout still being sent has answered', async () => {
      let answerLogout: (r: Response) => void = () => undefined;
      const order: string[] = [];
      const f = vi.fn((input: RequestInfo | URL) => {
        const path = new URL(input instanceof Request ? input.url : input).pathname;
        order.push(path);
        if (path === '/api/auth/logout') {
          return new Promise<Response>((resolve) => (answerLogout = resolve));
        }
        return Promise.resolve(json(200, { expires_at: '2026-06-11T09:20:00Z' }));
      });
      const c = client(f);
      void c.logout();
      const starting = c.startSession('t');
      await new Promise((r) => setTimeout(r, 0));
      // Had the session been posted now, the logout's Max-Age=0 could clear the new cookie.
      expect(order).toEqual(['/api/auth/logout']);
      answerLogout(new Response(null, { status: 204 }));
      await starting;
      expect(order).toEqual(['/api/auth/logout', '/api/auth/session']);
    });
  });

  describe('401 handling', () => {
    it('a protected call that gets 401 calls onUnauthorized and rejects with UnauthorizedError', async () => {
      const f = fakeFetch(json(401, problem('session-required')));
      await expect(client(f.fetch).getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).toHaveBeenCalledOnce();
    });

    it('several 401s from one session call onUnauthorized once', async () => {
      const f = fakeFetch(json(401, {}), json(401, {}));
      const c = client(f.fetch);
      await Promise.allSettled([c.getStatus(), c.listMessages()]);
      expect(onUnauthorized).toHaveBeenCalledOnce();
    });

    it('a late 401 from a request sent before a new session started does not end the new one', async () => {
      let answer: (r: Response) => void = () => undefined;
      const f = vi.fn((input: RequestInfo | URL) =>
        new URL(input instanceof Request ? input.url : input).pathname === '/api/status'
          ? new Promise<Response>((resolve) => (answer = resolve))
          : Promise.resolve(json(200, { expires_at: '2026-06-11T09:20:00Z' })),
      );
      const c = client(f);
      const pending = c.getStatus();
      // Meanwhile the visitor starts a new session.
      await c.startSession('t');
      answer(json(401, {}));
      await expect(pending).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it('a late 401 from a request sent before a logout does not call back either', async () => {
      let answer: (r: Response) => void = () => undefined;
      const f = vi.fn((input: RequestInfo | URL) =>
        new URL(input instanceof Request ? input.url : input).pathname === '/api/status'
          ? new Promise<Response>((resolve) => (answer = resolve))
          : Promise.resolve(new Response(null, { status: 204 })),
      );
      const c = client(f);
      const pending = c.getStatus();
      await c.logout();
      answer(json(401, {}));
      await expect(pending).rejects.toBeInstanceOf(UnauthorizedError);
      expect(onUnauthorized).not.toHaveBeenCalled();
    });

    it('an aborted request rejects with the AbortError, not as a network failure', async () => {
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

    it('a 401 on the session route is a refusal to read, not a session ending', async () => {
      const error = await client(fakeFetch(json(401, {})).fetch)
        .startSession('t')
        .catch((e: unknown) => e);
      expect(error).toMatchObject({ reason: 'unavailable' });
      expect(onUnauthorized).not.toHaveBeenCalled();
    });
  });

  describe('429 rate-limited from any route (T46)', () => {
    it.each([
      ['status', (c: HttpClient) => c.getStatus()],
      ['messages', (c: HttpClient) => c.listMessages()],
      ['share', (c: HttpClient) => c.share({ observationId: 'hr-1' })],
    ])(
      '%s: onRateLimited gets the wait, the error says it, the session stays',
      async (_name, call) => {
        const f = fakeFetch(
          json(429, problem('rate-limited', 'slow down'), { 'Retry-After': '30' }),
        );
        const error = (await call(client(f.fetch)).catch((e: unknown) => e)) as ApiError;
        expect(error).toBeInstanceOf(ApiError);
        expect([error.status, error.retryAfterSeconds]).toEqual([429, 30]);
        expect(onRateLimited).toHaveBeenCalledExactlyOnceWith(30);
        expect(onUnauthorized).not.toHaveBeenCalled();
      },
    );

    it('the message carries the wait on routes that use the generic error', async () => {
      const f = fakeFetch(json(429, problem('rate-limited', 'slow down'), { 'Retry-After': '30' }));
      const error = (await client(f.fetch)
        .getStatus()
        .catch((e: unknown) => e)) as ApiError;
      expect(error.message).toBe('Too many requests. Wait 30 seconds, then try again.');
    });

    it('without Retry-After the wait is null', async () => {
      await client(fakeFetch(json(429, {})).fetch)
        .getStatus()
        .catch(() => undefined);
      expect(onRateLimited).toHaveBeenCalledExactlyOnceWith(null);
    });
  });

  describe('errors', () => {
    it('a 429 from the rate-limited cuff route carries Retry-After', async () => {
      const f = fakeFetch(json(429, {}, { 'Retry-After': '30' }));
      const error = (await client(f.fetch)
        .submitCuffReading({ systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null })
        .catch((e: unknown) => e)) as ApiError;
      expect(error).toBeInstanceOf(ApiError);
      expect([error.status, error.retryAfterSeconds]).toEqual([429, 30]);
    });

    it('keeps a FHIR OperationOutcome from an error body', async () => {
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
      const f = fakeFetch(json(200, {}));
      await client(f.fetch, 'https://proxy.example.test/v1').getStatus();
      expect(f.calls[0]?.url).toBe('https://proxy.example.test/v1/status');
    });
  });
});
