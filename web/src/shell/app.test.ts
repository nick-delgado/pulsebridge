import { afterEach, describe, expect, it, vi } from 'vitest';
import type { BotCheck } from '../bot-check/index';
import { NOT_CONFIGURED_MESSAGE } from '../api/errors';
import { HttpClient } from '../api/http-client';
import { MockClient, type MockClientOptions } from '../api/mock-client';
import { DEFAULT_FEATURES, RATE_LIMIT_NOTICE_DEFAULT_MS, SESSION_ENDED, startApp } from './app';
import type { Features } from './features';
import { RECAPTCHA_NOTICE, START_LABEL } from './landing';
import { logOut, mockBotCheck, settle, startButton, viewDemo } from './test-support';

function root(): HTMLElement {
  document.body.replaceChildren();
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

const slot = (app: HTMLElement, name: string) => app.querySelector(`[data-slot="${name}"]`);
const errorText = (app: HTMLElement) => app.querySelector('#landing-error')?.textContent ?? '';

/** A fetch for the HTTP client that answers by path, recording each call. */
function routes(answer: (path: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: { path: string; init: RequestInit }[] = [];
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(input instanceof Request ? input.url : input).pathname;
    calls.push({ path, init: init ?? {} });
    return Promise.resolve(answer(path, init ?? {}));
  });
  return { fetch: fetch as unknown as typeof globalThis.fetch, calls };
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });

const SITE_KEY = 'test-site-key-0000000000000000000000000000';

/**
 * A stand-in proxy for the HTTP client: a session exists once `POST /auth/session` answered 200
 * (or from the start with `active`), and protected routes answer 401 without one. `override`
 * answers a path first; return undefined to fall through.
 */
function demoProxy(
  options: {
    active?: boolean;
    override?: (path: string, init: RequestInit) => Response | Promise<Response> | undefined;
  } = {},
) {
  let session = options.active ?? false;
  const proxy = routes((path, init) => {
    const custom = options.override?.(path, init);
    if (custom) return custom;
    if (path === '/api/auth/config') {
      return json(200, { recaptcha_site_key: SITE_KEY, recaptcha_action: 'view_demo' });
    }
    if (path === '/api/auth/session') {
      session = true;
      return json(200, { expires_at: '2026-06-11T09:20:00Z' });
    }
    if (path === '/api/auth/logout') {
      session = false;
      return new Response(null, { status: 204 });
    }
    if (!session) return json(401, {});
    // Enough for the features to mount without throwing; their data is not under test here.
    return json(200, path === '/api/cuff/samples' ? [] : {});
  });
  return {
    ...proxy,
    expire: () => {
      session = false;
    },
  };
}

describe('app against the mock client', () => {
  const start = (el: HTMLElement, options: Partial<MockClientOptions> = {}) =>
    startApp({
      root: el,
      makeClient: (deps) => new MockClient({ ...deps, latencyMs: 0, ...options }),
      botCheck: mockBotCheck(),
    });

  it('starts at the landing view: no password, the button, what the demo is, the reCAPTCHA notice', async () => {
    const el = root();
    const app = await start(el);
    expect(app.view()).toBe('landing');
    expect(el.querySelectorAll('input')).toHaveLength(0);
    expect(el.querySelector('input[type="password"]')).toBeNull();
    expect(startButton(el)).not.toBeNull();
    expect(el.textContent).toContain('personal health record');
    expect(el.textContent).toContain(RECAPTCHA_NOTICE);
    expect(el.textContent).toContain('Mock mode');
    expect(slot(el, 'heart-rate-panel')).toBeNull();
  });

  it('"View the live demo" passes the bot check and shows every region and the status', async () => {
    const el = root();
    const app = await start(el);
    await viewDemo(el);
    expect(app.view()).toBe('dashboard');
    for (const name of [
      'cuff-panel',
      'cuff-trace',
      'heart-rate-panel',
      'heart-rate-trace',
      'share-action',
      'share-trace',
      'share-readback',
      'messages-log',
      'messages-summary',
    ]) {
      expect(slot(el, name), name).not.toBeNull();
    }
    expect(el.querySelector('header')).not.toBeNull();
    expect(el.querySelector('svg[role="img"]')).not.toBeNull();
    expect(el.querySelector('.status-strip')?.textContent).toContain('Newest watch data');
  });

  it('keeps nothing in sessionStorage or localStorage', async () => {
    sessionStorage.clear();
    localStorage.clear();
    const el = root();
    await start(el);
    await viewDemo(el);
    expect(sessionStorage).toHaveLength(0);
    expect(localStorage).toHaveLength(0);
  });

  it.each([
    ['demo-busy', 'The demo is busy, try again tomorrow.'],
    ['bot-check-failed', /bot check did not let this visit through/],
    ['bot-check-unavailable', /bot check is not available right now/],
    ['rate-limited', 'Too many requests. Wait 42 seconds, then try again.'],
    ['config-unreachable', /could not be reached/],
  ] as const)('?mock-demo=%s stays on the landing view with its message', async (outcome, text) => {
    const el = root();
    const app = await start(el, { demoOutcome: outcome });
    await viewDemo(el);
    expect(app.view()).toBe('landing');
    if (typeof text === 'string') expect(errorText(el)).toBe(text);
    else expect(errorText(el)).toMatch(text);
    // The button is back, for another try.
    expect(startButton(el)?.disabled).toBe(false);
  });

  it('logout ends the session and returns to the landing view', async () => {
    const el = root();
    let client: MockClient | null = null;
    const app = await startApp({
      root: el,
      makeClient: (deps) => (client = new MockClient({ ...deps, latencyMs: 0 })),
      botCheck: mockBotCheck(),
    });
    await viewDemo(el);
    logOut(el);
    expect(app.view()).toBe('landing');
    expect(startButton(el)).not.toBeNull();
    expect(await (client as MockClient | null)?.checkSession()).toBe('none');
  });

  it('a 429 on the dashboard shows the wait under the header and keeps the visitor there', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const el = root();
      const app = await start(el, { throttle: 'status' });
      const ticks = async () => {
        for (let i = 0; i < 5; i++) await vi.advanceTimersByTimeAsync(0);
      };
      startButton(el)?.click();
      await ticks();
      expect(app.view()).toBe('dashboard');
      const notice = el.querySelector('#shell-notice');
      expect(notice?.getAttribute('role')).toBe('status');
      expect(notice?.textContent).toBe('Too many requests. Wait 30 seconds, then try again.');
      // Gone once the wait is over; still on the dashboard.
      await vi.advanceTimersByTimeAsync(29_999);
      expect(notice?.textContent).not.toBe('');
      await vi.advanceTimersByTimeAsync(1);
      expect(notice?.textContent).toBe('');
      expect(app.view()).toBe('dashboard');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('app against the HTTP client', () => {
  const http = (fetch: typeof globalThis.fetch) => (deps: Parameters<typeof makeHttp>[0]) =>
    makeHttp(deps, fetch);
  function makeHttp(
    deps: { onUnauthorized: () => void; onRateLimited?: (s: number | null) => void },
    fetch: typeof globalThis.fetch,
  ) {
    return new HttpClient({
      ...deps,
      baseUrl: '/api',
      origin: 'https://dashboard.example.test',
      fetch,
    });
  }

  /** A bot check that records what it was asked and gives a fixed token. */
  function recordingBotCheck() {
    const asked: string[] = [];
    const botCheck: BotCheck = {
      prepare: (config) => {
        asked.push(`prepare ${config.recaptchaSiteKey}`);
        return Promise.resolve();
      },
      token: (config) => {
        asked.push(`token ${config.recaptchaAction}`);
        return Promise.resolve('recaptcha-token-from-google');
      },
    };
    return { botCheck, asked };
  }

  it('a reload with a cookie the proxy still accepts goes straight to the dashboard', async () => {
    const f = demoProxy({ active: true });
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
    });
    expect(app.view()).toBe('dashboard');
    expect(f.calls[0]?.path).toBe('/api/status');
    expect(f.calls.some((c) => c.path === '/api/auth/config')).toBe(false);
  });

  it('without a session: config first, then the script, then on the click the token is posted', async () => {
    const f = demoProxy();
    const { botCheck, asked } = recordingBotCheck();
    const el = root();
    const app = await startApp({ root: el, makeClient: http(f.fetch), botCheck });
    await settle();
    expect(app.view()).toBe('landing');
    // The config is fetched and the script prepared BEFORE any click; no token yet.
    expect(f.calls.map((c) => c.path)).toEqual(['/api/status', '/api/auth/config']);
    expect(asked).toEqual([`prepare ${SITE_KEY}`]);
    expect(el.textContent).not.toContain('Mock mode');
    expect(el.textContent).toContain(RECAPTCHA_NOTICE);

    await viewDemo(el);
    expect(asked).toEqual([`prepare ${SITE_KEY}`, 'token view_demo']);
    const post = f.calls.find((c) => c.path === '/api/auth/session');
    expect(JSON.parse(post?.init.body as string)).toEqual({
      recaptcha_token: 'recaptcha-token-from-google',
    });
    expect(app.view()).toBe('dashboard');
  });

  it('the start button is disabled while the flow runs, so one click makes one session request', async () => {
    let answer: (r: Response) => void = () => undefined;
    const f = demoProxy({
      override: (path) =>
        path === '/api/auth/session' ? new Promise<Response>((r) => (answer = r)) : undefined,
    });
    const el = root();
    await startApp({ root: el, makeClient: http(f.fetch), botCheck: recordingBotCheck().botCheck });
    await settle();
    const button = startButton(el);
    button?.click();
    await settle();
    expect(button?.disabled).toBe(true);
    expect(button?.getAttribute('aria-busy')).toBe('true');
    button?.click();
    await settle();
    expect(f.calls.filter((c) => c.path === '/api/auth/session')).toHaveLength(1);
    answer(json(503, {}));
    await settle();
    expect(button?.disabled).toBe(false);
    expect(button?.textContent).toBe(START_LABEL);
  });

  it('a script that cannot load says so and asks for no session', async () => {
    const f = demoProxy();
    const { BotCheckLoadError } = await import('../bot-check/index');
    const blocked: BotCheck = {
      prepare: () => Promise.reject(new BotCheckLoadError('blocked')),
      token: () => Promise.reject(new BotCheckLoadError('blocked')),
    };
    const el = root();
    await startApp({ root: el, makeClient: http(f.fetch), botCheck: blocked });
    await viewDemo(el);
    expect(errorText(el)).toMatch(/bot check could not load in this browser/);
    expect(f.calls.some((c) => c.path === '/api/auth/session')).toBe(false);
  });

  it('a config that failed at first is fetched again on the click', async () => {
    let configCalls = 0;
    const f = demoProxy({
      override: (path) => {
        if (path !== '/api/auth/config') return undefined;
        configCalls++;
        return configCalls === 1 ? json(500, {}) : undefined;
      },
    });
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
    });
    await settle();
    await viewDemo(el);
    expect(configCalls).toBe(2);
    expect(app.view()).toBe('dashboard');
  });

  it('a 401 mid-session (the cookie expired) returns to the landing view with a notice', async () => {
    let call: () => Promise<unknown> = () => Promise.resolve();
    const features: Features = {
      ...DEFAULT_FEATURES,
      cuff: (ctx) => {
        call = () => ctx.client.listMalformedSamples({ signal: ctx.signal });
      },
    };
    const f = demoProxy({ active: true });
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
      features,
    });
    expect(app.view()).toBe('dashboard');
    f.expire();
    await call().catch(() => undefined);
    expect(app.view()).toBe('landing');
    expect(el.textContent).toContain(SESSION_ENDED);
  });

  it('a 429 without Retry-After keeps the notice a minute, then clears it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    try {
      let call: () => Promise<unknown> = () => Promise.resolve();
      const features: Features = {
        ...DEFAULT_FEATURES,
        cuff: (ctx) => {
          call = () => ctx.client.listMalformedSamples({ signal: ctx.signal });
        },
      };
      let throttled = false;
      const f = demoProxy({
        active: true,
        override: (path) => (throttled && path === '/api/cuff/samples' ? json(429, {}) : undefined),
      });
      const el = root();
      const app = await startApp({
        root: el,
        makeClient: http(f.fetch),
        botCheck: recordingBotCheck().botCheck,
        features,
      });
      throttled = true;
      await call().catch(() => undefined);
      const notice = el.querySelector('#shell-notice');
      expect(notice?.textContent).toBe('Too many requests. Wait a minute, then try again.');
      await vi.advanceTimersByTimeAsync(RATE_LIMIT_NOTICE_DEFAULT_MS - 1);
      expect(notice?.textContent).not.toBe('');
      await vi.advanceTimersByTimeAsync(1);
      expect(notice?.textContent).toBe('');
      expect(app.view()).toBe('dashboard');
    } finally {
      vi.useRealTimers();
    }
  });

  it('a 503 session-not-configured at start says so on the landing view, before any click', async () => {
    const outcome = {
      resourceType: 'OperationOutcome',
      issue: [
        {
          severity: 'error',
          code: 'transient',
          diagnostics: 'x',
          details: {
            coding: [
              { system: 'urn:pulsebridge:fhir-proxy:reason', code: 'session-not-configured' },
            ],
          },
        },
      ],
    };
    const f = demoProxy({
      override: (path) => (path === '/api/status' ? json(503, outcome) : undefined),
    });
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
    });
    expect(app.view()).toBe('landing');
    expect(el.querySelector('.landing__notice')?.textContent).toBe(NOT_CONFIGURED_MESSAGE);
  });

  it('a 429 when checking for a session at start shows the wait on the landing view', async () => {
    const f = routes((path) =>
      path === '/api/status' ? json(429, {}, { 'Retry-After': '20' }) : json(500, {}),
    );
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
    });
    expect(app.view()).toBe('landing');
    expect(el.textContent).toContain('Too many requests. Wait 20 seconds, then try again.');
  });

  it('logout posts /auth/logout and cancels the requests still in flight', async () => {
    const signals: AbortSignal[] = [];
    let first = true;
    const f = routes((path, init) => {
      if (path === '/api/status' && first) {
        first = false;
        return json(200, {});
      }
      if (path === '/api/auth/logout') return new Response(null, { status: 204 });
      if (init.signal) signals.push(init.signal);
      return new Promise<Response>(() => undefined); // never answers
    });
    const el = root();
    const app = await startApp({
      root: el,
      makeClient: http(f.fetch),
      botCheck: recordingBotCheck().botCheck,
    });
    expect(app.view()).toBe('dashboard');
    expect(signals.length).toBeGreaterThan(0);
    expect(signals.every((s) => !s.aborted)).toBe(true);
    logOut(el);
    expect(signals.every((s) => s.aborted)).toBe(true);
    expect(f.calls.some((c) => c.path === '/api/auth/logout')).toBe(true);
    expect(app.view()).toBe('landing');
  });
});

describe('feature context', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const mockApp = (el: HTMLElement, features: Features) =>
    startApp({
      root: el,
      makeClient: (deps) => new MockClient({ ...deps, latencyMs: 0 }),
      botCheck: mockBotCheck(),
      features,
    });

  it('one feature notifies a data change, another hears it, and the status strip reloads', async () => {
    const heard: string[] = [];
    let notify: (kind: 'cuff-send' | 'share' | 'sync') => void = () => undefined;
    const features: Features = {
      ...DEFAULT_FEATURES,
      cuff: (ctx) => {
        notify = (kind) => {
          ctx.notifyDataChanged(kind);
        };
      },
      messages: (ctx) => {
        ctx.onDataChanged((kind) => heard.push(kind), ctx.signal);
      },
    };
    const el = root();
    const client = new MockClient({ onUnauthorized: () => undefined, latencyMs: 0 });
    await client.startSession('mock-recaptcha-test');
    const getStatus = vi.spyOn(client, 'getStatus');
    await startApp({ root: el, makeClient: () => client, botCheck: mockBotCheck(), features });
    await settle();
    const before = getStatus.mock.calls.length;
    notify('cuff-send');
    expect(heard).toEqual(['cuff-send']);
    expect(getStatus.mock.calls.length).toBe(before + 1);
  });

  it('logout aborts the signal every feature was given, so their listeners and requests stop', async () => {
    let featureSignal: AbortSignal | null = null;
    let notify: (kind: 'cuff-send' | 'share' | 'sync') => void = () => undefined;
    const heard: string[] = [];
    const features: Features = {
      ...DEFAULT_FEATURES,
      heartRate: (ctx) => {
        featureSignal = ctx.signal;
        notify = (kind) => {
          ctx.notifyDataChanged(kind);
        };
        ctx.onDataChanged((kind) => heard.push(kind), ctx.signal);
      },
    };
    const el = root();
    await mockApp(el, features);
    await viewDemo(el);
    expect((featureSignal as AbortSignal | null)?.aborted).toBe(false);
    logOut(el);
    expect((featureSignal as AbortSignal | null)?.aborted).toBe(true);
    notify('sync');
    expect(heard).toEqual([]);
  });

  it('a 401 from any feature call returns to the landing view with the session-ended notice', async () => {
    let call: () => Promise<unknown> = () => Promise.resolve();
    const features: Features = {
      ...DEFAULT_FEATURES,
      cuff: (ctx) => {
        call = () => ctx.client.listMessages({}, { signal: ctx.signal });
      },
    };
    const el = root();
    let client: MockClient | null = null;
    const app = await startApp({
      root: el,
      makeClient: (deps) => (client = new MockClient({ ...deps, latencyMs: 0 })),
      botCheck: mockBotCheck(),
      features,
    });
    await viewDemo(el);
    expect(app.view()).toBe('dashboard');
    // The cookie goes (as when it expires); the next call by any feature is a 401.
    await (client as MockClient | null)?.logout();
    await call().catch(() => undefined);
    expect(app.view()).toBe('landing');
    expect(el.textContent).toContain(SESSION_ENDED);
    expect(document.activeElement).toBe(startButton(el));
  });

  it('names the two trace regions differently', async () => {
    const el = root();
    await mockApp(el, DEFAULT_FEATURES);
    await viewDemo(el);
    const names = [...el.querySelectorAll('section.trace')].map((section) => {
      const id = section.getAttribute('aria-labelledby') ?? '';
      return document.getElementById(id)?.textContent;
    });
    expect(names).toEqual(expect.arrayContaining(['Blood-pressure Trace', 'Heart-rate Trace']));
    expect(new Set(names).size).toBe(names.length);
  });
});
