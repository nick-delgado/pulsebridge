import { expect, test as base, type Page } from '@playwright/test';

/**
 * The page's clock in every browser test.
 *
 * The mock runs INSIDE the page (there is no mock server: `vite preview` only serves the built
 * files), and everything it returns is relative to the page's `Date.now()`: which heart-rate
 * windows exist (none in the daily 12:00-12:40 UTC "charging gap"), which are still
 * `preliminary` (they end within 30 minutes of the last sync run at minute 2, 17, 32 or 47),
 * what the newest health-sync run wrote, and the seeded messages-log times. A test that let the
 * page read the wall clock passed or failed with the time of day it ran (T37).
 *
 * So every test gets a fake page clock, installed before the page loads with Playwright's
 * `page.clock.install({ time })`. It STARTS at `clockAt` and then runs at the real rate, timers
 * included, so polling, the mock mapper's delay and the cuff rate limit behave as with a real
 * clock; only the starting instant is chosen.
 *
 * - `DEFAULT_CLOCK` is an ordinary instant: a weekday mid-morning, mid-month, no clock change
 *   in any zone for weeks, recent watch data with provisional windows.
 * - A test about one situation pins its own instant with `test.use({ clockAt: ... })`, and says
 *   why (for example the charging gap, CHARGING_GAP_CLOCK).
 * - `PULSEBRIDGE_E2E_NOW=<ISO instant>` replaces the default for one run, to check that the tests
 *   that do not pin a time hold at any time of day (README, "Browser tests and the clock"). It
 *   does not move tests that pin their own instant: they are about that instant.
 * - `clockAt: null` installs nothing (the page reads the real clock); a test that needs that
 *   must say so.
 */

// Node's `process`, declared here rather than adding @types/node for one read.
declare const process: { env: Record<string, string | undefined> };

/** Wednesday 10 June 2026, 09:20 UTC. */
export const DEFAULT_CLOCK = '2026-06-10T09:20:00Z';

/**
 * Monday 16 November 2026, 12:50 UTC: the watch has been on the charger since 12:00 (the mock's
 * daily gap, 12:00-12:40 UTC, then no run has collected the windows after it yet). The newest
 * window is 11:55-12:00 and is final; the last sync run (12:47) had nothing to write.
 */
export const CHARGING_GAP_CLOCK = '2026-11-16T12:50:00Z';

function clockFromEnv(): string {
  const raw = process.env.PULSEBRIDGE_E2E_NOW;
  if (raw === undefined || raw === '') return DEFAULT_CLOCK;
  if (Number.isNaN(Date.parse(raw))) {
    throw new Error(`PULSEBRIDGE_E2E_NOW must be an ISO 8601 instant; got "${raw}".`);
  }
  return raw;
}

export const test = base.extend<{ clockAt: string | null; pageClock: undefined }>({
  clockAt: [clockFromEnv(), { option: true }],
  pageClock: [
    async ({ page, clockAt }, use) => {
      if (clockAt !== null) await page.clock.install({ time: new Date(clockAt) });
      await use(undefined);
    },
    { auto: true },
  ],
});

export { expect };
export type { Page };

/** The page's own `Date.now()`: the clock its mock and its ranges use, not the test runner's. */
export function pageNow(page: Page): Promise<number> {
  return page.evaluate(() => Date.now());
}
