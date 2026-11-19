/**
 * The mock's demo-session flow (D16), which `npm run dev` and the browser tests rely on instead
 * of a Google key: the config, a pass, each refusal the proxy can send, the cookie's expiry, and
 * a 429 from a protected route that does not end the session.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MockBotCheck } from '../bot-check/mock';
import {
  ApiError,
  DEMO_BUSY_MESSAGE,
  proxyReason,
  SessionError,
  UnauthorizedError,
} from './errors';
import {
  MOCK_RETRY_AFTER_SECONDS,
  MOCK_SITE_KEY,
  MockClient,
  type MockClientOptions,
} from './mock-client';
import { mockSwitchesFromSearch, type MockDemoOutcome } from './mock-data';
import { parseAuthConfig } from './parse';

const START = Date.parse('2026-06-10T09:20:00Z');

describe('MockClient demo session', () => {
  let now: number;
  let onUnauthorized: ReturnType<typeof vi.fn<() => void>>;
  let onRateLimited: ReturnType<typeof vi.fn<(s: number | null) => void>>;

  beforeEach(() => {
    now = START;
    onUnauthorized = vi.fn<() => void>();
    onRateLimited = vi.fn<(s: number | null) => void>();
  });

  const make = (options: Partial<MockClientOptions> = {}) =>
    new MockClient({ onUnauthorized, onRateLimited, now: () => now, latencyMs: 0, ...options });

  /** What the landing view does: config, then a token from the mock bot check, then the post. */
  async function startWithBotCheck(client: MockClient) {
    const config = await client.getAuthConfig();
    return client.startSession(await new MockBotCheck().token(config));
  }

  it('gives a config that passes the same check as a real one, with the action view_demo', async () => {
    const config = await make().getAuthConfig();
    expect(config).toEqual({ recaptchaSiteKey: MOCK_SITE_KEY, recaptchaAction: 'view_demo' });
    expect(
      parseAuthConfig({ recaptcha_site_key: MOCK_SITE_KEY, recaptcha_action: 'view_demo' }),
    ).toEqual(config);
  });

  it('has no session at first: protected calls are 401 and call onUnauthorized', async () => {
    const client = make();
    expect(await client.checkSession()).toBe('none');
    await expect(client.getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it('a pass starts a 24-hour session; protected calls then work', async () => {
    const client = make();
    const session = await startWithBotCheck(client);
    expect(Date.parse(session.expiresAt) - START).toBe(86_400_000);
    expect(await client.checkSession()).toBe('active');
    expect((await client.getStatus()).watchSync).toBeDefined();
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('refuses a token that did not come from the bot check (the shape check)', async () => {
    const error = await make()
      .startSession('not-a-token')
      .catch((e: unknown) => e);
    expect(error).toMatchObject({ name: 'SessionError', reason: 'bot-check-failed' });
  });

  it.each<[MockDemoOutcome, string, string | RegExp, number | null]>([
    ['bot-check-failed', 'bot-check-failed', /bot check did not let this visit through/, null],
    ['origin-refused', 'origin-refused', /does not accept requests from this address/, null],
    ['demo-busy', 'demo-busy', DEMO_BUSY_MESSAGE, null],
    ['bot-check-unavailable', 'bot-check-unavailable', /not available right now/, null],
    [
      'rate-limited',
      'rate-limited',
      `Too many requests. Wait ${MOCK_RETRY_AFTER_SECONDS.session} seconds, then try again.`,
      MOCK_RETRY_AFTER_SECONDS.session,
    ],
  ])('?mock-demo=%s refuses with reason %s and no session', async (outcome, reason, text, wait) => {
    const client = make({ demoOutcome: outcome });
    const error = (await startWithBotCheck(client).catch((e: unknown) => e)) as SessionError;
    expect(error).toBeInstanceOf(SessionError);
    expect(error.reason).toBe(reason);
    if (typeof text === 'string') expect(error.message).toBe(text);
    else expect(error.message).toMatch(text);
    expect(error.retryAfterSeconds).toBe(wait);
    expect(await client.checkSession()).toBe('none');
  });

  it('demo-busy says exactly "The demo is busy, try again tomorrow."', async () => {
    const error = (await startWithBotCheck(make({ demoOutcome: 'demo-busy' })).catch(
      (e: unknown) => e,
    )) as SessionError;
    expect(error.message).toBe('The demo is busy, try again tomorrow.');
  });

  it('?mock-demo=config-unreachable: the config cannot be fetched', async () => {
    await expect(make({ demoOutcome: 'config-unreachable' }).getAuthConfig()).rejects.toMatchObject(
      { reason: 'unavailable' },
    );
  });

  it('the cookie expires: after sessionSeconds the next call is 401 (a 401 mid-session)', async () => {
    const client = make({ sessionSeconds: 60 });
    await startWithBotCheck(client);
    now = START + 59_999;
    await expect(client.getStatus()).resolves.toBeDefined();
    now = START + 60_000;
    await expect(client.getStatus()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(await client.checkSession()).toBe('none');
  });

  it('logout ends the session', async () => {
    const client = make();
    await startWithBotCheck(client);
    await client.logout();
    expect(await client.checkSession()).toBe('none');
  });

  it('?mock-throttle=status: 429 rate-limited with the wait, onRateLimited, the session stays', async () => {
    const client = make({ throttle: 'status' });
    await startWithBotCheck(client);
    const error = (await client.getStatus().catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect([error.status, error.retryAfterSeconds, proxyReason(error.outcome)]).toEqual([
      429,
      MOCK_RETRY_AFTER_SECONDS.status,
      'rate-limited',
    ]);
    expect(error.message).toBe('Too many requests. Wait 30 seconds, then try again.');
    expect(onRateLimited).toHaveBeenCalledWith(MOCK_RETRY_AFTER_SECONDS.status);
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(await client.checkSession()).toBe('active');
  });

  it("cuff-sim's 429 also calls onRateLimited with its wait", async () => {
    const client = make({ rateLimit: { burst: 1, perMinute: 1 } });
    await startWithBotCheck(client);
    const reading = {
      systolic: 120,
      diastolic: 80,
      transport: 'mllp',
      sendMalformed: null,
    } as const;
    await client.submitCuffReading(reading);
    await expect(client.submitCuffReading(reading)).rejects.toMatchObject({ status: 429 });
    expect(onRateLimited).toHaveBeenCalledWith(60);
  });
});

describe('the demo-session mock switches', () => {
  it('parse only exact values', () => {
    expect(mockSwitchesFromSearch('?mock-demo=demo-busy')).toMatchObject({
      demoOutcome: 'demo-busy',
    });
    expect(mockSwitchesFromSearch('?mock-demo=busy')).toMatchObject({ demoOutcome: null });
    expect(mockSwitchesFromSearch('?mock-throttle=status')).toMatchObject({ throttle: 'status' });
    expect(mockSwitchesFromSearch('?mock-throttle=all')).toMatchObject({ throttle: null });
  });

  it.each([
    ['5', 5],
    ['86400', 86_400],
    ['0', null],
    ['86401', null],
    ['-5', null],
    ['1.5', null],
    ['', null],
  ])('?mock-session-seconds=%s is %s', (value, expected) => {
    expect(mockSwitchesFromSearch(`?mock-session-seconds=${value}`).sessionSeconds).toBe(expected);
  });
});
