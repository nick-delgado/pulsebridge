import { defineConfig, devices } from '@playwright/test';

// Node's `process`, declared here rather than adding @types/node for four reads.
declare const process: { env: Record<string, string | undefined> };
const CI = Boolean(process.env.CI);

/**
 * Browser tests run against the BUILT mock page (`npm run build:mock`, served by `vite preview`),
 * so they exercise the real bundle and its Content-Security-Policy, with no backend.
 *
 * Test files are `*.e2e.ts`, anywhere under e2e/ or src/, so each feature task keeps its browser
 * tests inside the directory it owns (for example src/heart-rate/chart.e2e.ts).
 */
export default defineConfig({
  testDir: '.',
  testMatch: ['e2e/**/*.e2e.ts', 'src/**/*.e2e.ts'],
  fullyParallel: true,
  forbidOnly: CI,
  retries: 0,
  reporter: CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'off',
    // The browser's zone, fixed so a run does not depend on the machine's: UTC unless
    // PULSEBRIDGE_E2E_TZ names an IANA zone. The page's clock is set per test (e2e/clock.ts).
    timezoneId: process.env.PULSEBRIDGE_E2E_TZ || 'UTC',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run preview:mock -- --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !CI,
    timeout: 30_000,
  },
});
