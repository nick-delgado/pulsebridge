import { beforeEach, describe, expect, it, vi } from 'vitest';
import { HttpClient } from '../api/http-client';
import { MockClient } from '../api/mock-client';
import { SESSION_KEY, Session } from '../api/session';
import { SESSION_ENDED, startApp } from './app';

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
    expect(el.querySelector('.status-strip')?.textContent).toContain('Last sync');
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
