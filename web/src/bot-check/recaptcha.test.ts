/**
 * The reCAPTCHA loader, in jsdom with a fake `grecaptcha.enterprise` (jsdom never fetches the
 * script, so the test fires its load or error event itself). No request goes to Google.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthConfig } from '../api/contract';
import { BotCheckLoadError } from './index';
import { RECAPTCHA_SCRIPT, RecaptchaBotCheck } from './recaptcha';

const CONFIG: AuthConfig = {
  recaptchaSiteKey: 'test-site-key-0000000000000000000000000000',
  recaptchaAction: 'view_demo',
};

interface FakeEnterprise {
  ready: ReturnType<typeof vi.fn<(cb: () => void) => void>>;
  execute: ReturnType<typeof vi.fn<(key: string, o: { action: string }) => Promise<string>>>;
  /** Runs the callbacks `ready` was given, as Google's library does once it has started. */
  start: () => void;
}

function fakeEnterprise(token: Promise<string> = Promise.resolve('google-token')): FakeEnterprise {
  const waiting: (() => void)[] = [];
  let started = false;
  return {
    ready: vi.fn((cb: () => void) => {
      if (started) cb();
      else waiting.push(cb);
    }),
    execute: vi.fn(() => token),
    start: () => {
      started = true;
      waiting.splice(0).forEach((cb) => {
        cb();
      });
    },
  };
}

const scripts = () =>
  [...document.querySelectorAll<HTMLScriptElement>('script')].filter((s) =>
    s.src.startsWith(RECAPTCHA_SCRIPT),
  );

/** Google's script "arrives": the global is set and the element's load event fires. */
function loadScript(api: FakeEnterprise | null): void {
  (window as unknown as { grecaptcha?: unknown }).grecaptcha = api ? { enterprise: api } : {};
  scripts().at(-1)?.dispatchEvent(new Event('load'));
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe('RecaptchaBotCheck', () => {
  beforeEach(() => {
    document.head.replaceChildren();
    delete (window as unknown as { grecaptcha?: unknown }).grecaptcha;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('adds one async enterprise.js script with render=<key>, only when asked', async () => {
    const check = new RecaptchaBotCheck();
    expect(scripts()).toHaveLength(0);
    const preparing = check.prepare(CONFIG);
    const [script] = scripts();
    expect(script?.getAttribute('src')).toBe(
      `https://www.google.com/recaptcha/enterprise.js?render=${CONFIG.recaptchaSiteKey}`,
    );
    expect(script?.hasAttribute('async')).toBe(true);
    const api = fakeEnterprise();
    loadScript(api);
    api.start();
    await preparing;
    // A second prepare or a token reuses the same script.
    await check.prepare(CONFIG);
    await check.token(CONFIG);
    expect(scripts()).toHaveLength(1);
  });

  it('calls execute(key, {action}) only after ready, and returns its token', async () => {
    const check = new RecaptchaBotCheck();
    const api = fakeEnterprise(Promise.resolve('google-token'));
    const token = check.token(CONFIG);
    loadScript(api);
    await tick();
    expect(api.ready).toHaveBeenCalledOnce();
    // The library has loaded but not started: execute must wait.
    expect(api.execute).not.toHaveBeenCalled();
    api.start();
    expect(await token).toBe('google-token');
    expect(api.execute).toHaveBeenCalledExactlyOnceWith(CONFIG.recaptchaSiteKey, {
      action: 'view_demo',
    });
  });

  it('asks for a fresh token on each call (tokens expire after two minutes)', async () => {
    const check = new RecaptchaBotCheck();
    const api = fakeEnterprise();
    const first = check.token(CONFIG);
    loadScript(api);
    api.start();
    await first;
    await check.token(CONFIG);
    expect(api.execute).toHaveBeenCalledTimes(2);
  });

  it('a script that fails to load rejects with BotCheckLoadError, and a retry adds a new one', async () => {
    const check = new RecaptchaBotCheck();
    const failed = check.prepare(CONFIG);
    scripts()[0]?.dispatchEvent(new Event('error'));
    await expect(failed).rejects.toBeInstanceOf(BotCheckLoadError);
    expect(scripts()).toHaveLength(0);

    const retry = check.prepare(CONFIG);
    expect(scripts()).toHaveLength(1);
    const api = fakeEnterprise();
    loadScript(api);
    api.start();
    await expect(retry).resolves.toBeUndefined();
  });

  it('a script that loads without grecaptcha.enterprise is a load error', async () => {
    const check = new RecaptchaBotCheck();
    const preparing = check.prepare(CONFIG);
    loadScript(null);
    await expect(preparing).rejects.toThrow(/grecaptcha.enterprise is missing/);
  });

  it('a script that never becomes ready times out', async () => {
    vi.useFakeTimers();
    const check = new RecaptchaBotCheck(1000);
    const preparing = check.prepare(CONFIG);
    const assertion = expect(preparing).rejects.toThrow(/did not load in time/);
    await vi.advanceTimersByTimeAsync(1000);
    await assertion;
  });

  it('execute failing or giving no token is a BotCheckLoadError', async () => {
    const check = new RecaptchaBotCheck();
    const api = fakeEnterprise(Promise.resolve(''));
    const token = check.token(CONFIG);
    loadScript(api);
    api.start();
    await expect(token).rejects.toBeInstanceOf(BotCheckLoadError);
  });

  it('encodes the key into the URL (a key cannot add query parameters)', () => {
    const check = new RecaptchaBotCheck();
    void check.prepare({ ...CONFIG, recaptchaSiteKey: 'a&badge=inline' }).catch(() => undefined);
    expect(scripts()[0]?.getAttribute('src')).toBe(`${RECAPTCHA_SCRIPT}?render=a%26badge%3Dinline`);
  });
});
