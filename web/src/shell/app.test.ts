import { beforeEach, describe, expect, it, vi } from 'vitest';
import { HttpClient } from '../api/http-client';
import { MockClient } from '../api/mock-client';
import { SESSION_KEY, Session } from '../api/session';
import { DEFAULT_FEATURES, SESSION_ENDED, startApp } from './app';
import type { Features } from './features';

/** Lets pending promise callbacks (mock latency 0, fake fetch) run. */
const settle = () => new Promise((r) => setTimeout(r, 0));

function root(): HTMLElement {
  document.body.replaceChildren();
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

async function logIn(app: HTMLElement, password: string): Promise<void> {
  const input = app.querySelector<HTMLInputElement>('#login-password');
  if (!input) throw new Error('no password field');
  input.value = password;
  app.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  await settle();
}

const slot = (app: HTMLElement, name: string) => app.querySelector(`[data-slot="${name}"]`);

describe('app against the mock client', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  const start = (el: HTMLElement) =>
    startApp({
      root: el,
      makeClient: (deps) => new MockClient({ ...deps, latencyMs: 0 }),
    });

  it('starts at a password-only login form that says it is in mock mode', async () => {
    const el = root();
    const app = await start(el);
    expect(app.view()).toBe('login');
    const inputs = el.querySelectorAll('input');
    expect(inputs).toHaveLength(1);
    expect(inputs[0]?.type).toBe('password');
    expect(el.textContent).toContain('Any non-empty password logs in');
    expect(slot(el, 'heart-rate-panel')).toBeNull();
  });

  it('an empty password stays on the login form with an error', async () => {
    const el = root();
    const app = await start(el);
    await logIn(el, '');
    expect(app.view()).toBe('login');
    expect(el.querySelector('#login-error')?.textContent).not.toBe('');
  });

  it('logging in shows every region, keeps the token in sessionStorage, and loads the status', async () => {
    const el = root();
    const app = await start(el);
    await logIn(el, 'anything');
    expect(app.view()).toBe('dashboard');
    expect(sessionStorage.getItem(SESSION_KEY)).toMatch(/mock-session-/);
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
    expect(el.querySelector('main')).not.toBeNull();
    expect(el.querySelector('svg[role="img"]')).not.toBeNull();
    expect(el.querySelector('.status-strip')?.textContent).toContain('Newest watch data');
  });

  it('a token already in the session skips the login form; logout clears it', async () => {
    const el = root();
    const first = await start(el);
    await logIn(el, 'x');
    expect(first.view()).toBe('dashboard');

    const el2 = root();
    const second = await start(el2);
    expect(second.view()).toBe('dashboard');

    const logout = [...el2.querySelectorAll('button')].find((b) => b.textContent === 'Log out');
    logout?.click();
    expect(second.view()).toBe('login');
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(el2.querySelector('#login-password')).not.toBeNull();
  });
});

describe('app against the HTTP client', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('returns to the login form with a notice when a call gets 401', async () => {
    const session = new Session(sessionStorage);
    session.save({ token: 'stale', expiresAt: new Date(Date.now() + 60_000).toISOString() });
    const fetch = vi.fn(() => Promise.resolve(new Response('{}', { status: 401 })));
    const el = root();
    const app = await startApp({
      root: el,
      session,
      makeClient: (deps) =>
        new HttpClient({
          ...deps,
          baseUrl: '/api',
          origin: 'https://dashboard.example.test',
          fetch: fetch,
        }),
    });
    // The dashboard was shown (a token existed) and asked for the status strip, which got 401.
    expect(fetch).toHaveBeenCalled();
    await settle();
    expect(app.view()).toBe('login');
    expect(el.querySelector('#login-password')).not.toBeNull();
    expect(el.textContent).toContain(SESSION_ENDED);
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    // Not in mock mode, so no mock notice.
    expect(el.textContent).not.toContain('Any non-empty password');
  });
});

describe('feature context', () => {
  beforeEach(() => {
    sessionStorage.clear();
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
    const client = new MockClient({
      session: new Session(sessionStorage),
      onUnauthorized: () => undefined,
      latencyMs: 0,
    });
    await client.login('x');
    const getStatus = vi.spyOn(client, 'getStatus');
    await startApp({ root: el, makeClient: () => client, features });
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
    await startApp({
      root: el,
      makeClient: (deps) => new MockClient({ ...deps, latencyMs: 0 }),
      features,
    });
    await logIn(el, 'x');
    expect((featureSignal as AbortSignal | null)?.aborted).toBe(false);
    [...el.querySelectorAll('button')].find((b) => b.textContent === 'Log out')?.click();
    expect((featureSignal as AbortSignal | null)?.aborted).toBe(true);
    notify('sync');
    expect(heard).toEqual([]);
  });

  it('logout cancels the status request still in flight (its fetch gets an aborted signal)', async () => {
    const session = new Session(sessionStorage);
    session.save({ token: 't', expiresAt: new Date(Date.now() + 60_000).toISOString() });
    const signals: AbortSignal[] = [];
    const fetch = vi.fn((_url: unknown, init?: RequestInit) => {
      if (init?.signal) signals.push(init.signal);
      return new Promise<Response>(() => undefined); // never answers
    });
    const el = root();
    await startApp({
      root: el,
      session,
      makeClient: (deps) =>
        new HttpClient({
          ...deps,
          baseUrl: '/api',
          origin: 'https://dashboard.example.test',
          fetch: fetch,
        }),
    });
    // The status call plus any feature's first load: every one of them is cancelled on logout.
    // (Not "exactly one request": a feature that loads data when it mounts adds its own.)
    expect(signals.length).toBeGreaterThan(0);
    expect(signals.every((s) => !s.aborted)).toBe(true);
    [...el.querySelectorAll('button')].find((b) => b.textContent === 'Log out')?.click();
    expect(signals.every((s) => s.aborted)).toBe(true);
  });

  it('names the two trace regions differently', async () => {
    const el = root();
    await startApp({ root: el, makeClient: (deps) => new MockClient({ ...deps, latencyMs: 0 }) });
    await logIn(el, 'x');
    const names = [...el.querySelectorAll('section.trace')].map((section) => {
      const id = section.getAttribute('aria-labelledby') ?? '';
      return document.getElementById(id)?.textContent;
    });
    expect(names).toEqual(expect.arrayContaining(['Blood-pressure Trace', 'Heart-rate Trace']));
    expect(new Set(names).size).toBe(names.length);
  });
});
