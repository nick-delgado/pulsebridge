import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { pollUntil } from './poll';

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
});
