import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { POLL_MAX_RATE_LIMIT_WAIT_MS, pollUntil } from './poll';

describe('pollUntil', () => {
  let clock = 0;
  beforeEach(() => {
    vi.useFakeTimers();
    clock = 0;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const opts = (signal: AbortSignal, visible = () => true) => ({
    signal,
    intervalMs: 1000,
    maxMs: 5000,
    now: () => clock,
    isVisible: visible,
  });
  const advance = async (ms: number) => {
    clock += ms;
    await vi.advanceTimersByTimeAsync(ms);
  };

  it('checks every interval and stops once the check reports done', async () => {
    const results = [false, false, true];
    const check = vi.fn(() => Promise.resolve(results.shift() ?? true));
    const done = pollUntil(check, opts(new AbortController().signal));
    await advance(1000);
    await advance(1000);
    await advance(1000);
    await expect(done).resolves.toBe(true);
    await advance(5000);
    expect(check).toHaveBeenCalledTimes(3);
  });

  it('gives up after the time limit and leaves the item pending', async () => {
    const check = vi.fn(() => Promise.resolve(false));
    const done = pollUntil(check, opts(new AbortController().signal));
    for (let i = 0; i < 8; i++) await advance(1000);
    await expect(done).resolves.toBe(false);
    expect(check.mock.calls.length).toBeLessThanOrEqual(5);
  });

  it('stops at once when the dashboard signal aborts', async () => {
    const controller = new AbortController();
    const check = vi.fn(() => Promise.resolve(false));
    const done = pollUntil(check, opts(controller.signal));
    await advance(1000);
    controller.abort();
    await expect(done).resolves.toBe(false);
    await advance(3000);
    expect(check).toHaveBeenCalledTimes(1);
  });

  it('does not call the server while the tab is hidden', async () => {
    let visible = false;
    const check = vi.fn(() => Promise.resolve(true));
    const done = pollUntil(
      check,
      opts(new AbortController().signal, () => visible),
    );
    await advance(1000);
    await advance(1000);
    expect(check).not.toHaveBeenCalled();
    visible = true;
    await advance(1000);
    await expect(done).resolves.toBe(true);
  });

  // T47: a 429 rate-limited answer pauses the polling for its Retry-After, then it goes on.
  // The clock starts at 0 (never the time of day, T37).
  const limited = (seconds: number | null) =>
    new ApiError(429, 'Too many requests.', null, seconds);

  it('after a 429 waits for Retry-After, sends nothing meanwhile, then polls again', async () => {
    const answers: (() => Promise<boolean>)[] = [
      () => Promise.reject(limited(4)),
      () => Promise.resolve(false),
      () => Promise.resolve(true),
    ];
    const check = vi.fn(() => (answers.shift() ?? (() => Promise.resolve(true)))());
    const done = pollUntil(check, opts(new AbortController().signal));
    await advance(1000); // first check: refused
    expect(check).toHaveBeenCalledTimes(1);
    await advance(3999); // still inside the 4 s wait: nothing sent
    expect(check).toHaveBeenCalledTimes(1);
    await advance(1); // the wait has passed
    expect(check).toHaveBeenCalledTimes(2);
    await advance(1000);
    await expect(done).resolves.toBe(true);
    expect(check).toHaveBeenCalledTimes(3);
  });

  it('does not count the wait against the time limit', async () => {
    // maxMs 5 s; the 429 asks for 30 s. Polling still gets its 5 s of polling afterwards.
    const answers: (() => Promise<boolean>)[] = [() => Promise.reject(limited(30))];
    const check = vi.fn(() => (answers.shift() ?? (() => Promise.resolve(false)))());
    let settled: boolean | null = null;
    void pollUntil(check, opts(new AbortController().signal)).then((v) => (settled = v));
    await advance(1000);
    await advance(30_000);
    expect(check).toHaveBeenCalledTimes(2); // polled again after the wait
    await advance(3000);
    expect(settled).toBeNull(); // 4 s of polling so far: not given up yet
    for (let i = 0; i < 4; i++) await advance(1000);
    expect(settled).toBe(false);
  });

  it('waits a minute when the 429 carries no Retry-After', async () => {
    const answers: (() => Promise<boolean>)[] = [() => Promise.reject(limited(null))];
    const check = vi.fn(() => (answers.shift() ?? (() => Promise.resolve(true)))());
    const done = pollUntil(check, opts(new AbortController().signal));
    await advance(1000);
    await advance(60_000 - 1); // a minute, written out: the constant itself is under test
    expect(check).toHaveBeenCalledTimes(1);
    await advance(1);
    await expect(done).resolves.toBe(true);
  });

  it('gives up, without rejecting, once the 429 waits pass their own limit', async () => {
    const check = vi.fn(() => Promise.reject(limited(POLL_MAX_RATE_LIMIT_WAIT_MS / 1000 + 1)));
    const done = pollUntil(check, opts(new AbortController().signal));
    await advance(1000);
    await expect(done).resolves.toBe(false);
    expect(check).toHaveBeenCalledTimes(1);
  });

  it('stops for good on any other error, as before', async () => {
    const check = vi.fn(() => Promise.reject(new ApiError(503, 'Unavailable.')));
    const done = pollUntil(check, opts(new AbortController().signal));
    const failed = expect(done).rejects.toThrow('Unavailable.');
    await advance(1000);
    await failed;
    await advance(10_000);
    expect(check).toHaveBeenCalledTimes(1);
  });

  it('a logout during the wait stops it', async () => {
    const controller = new AbortController();
    const check = vi.fn(() => Promise.reject(limited(10)));
    const done = pollUntil(check, opts(controller.signal));
    await advance(1000);
    controller.abort();
    await expect(done).resolves.toBe(false);
    await advance(20_000);
    expect(check).toHaveBeenCalledTimes(1);
  });
});
