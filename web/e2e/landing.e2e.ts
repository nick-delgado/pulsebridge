import { expect, test, type Page } from './clock';

/**
 * The landing view and the demo-session flow (D16, T45) in the built mock page: the landing, a
 * pass, each refusal, a 429 mid-session and a 401 mid-session. The mock simulates the proxy and
 * its bot check inside the page (`?mock-demo=`, `?mock-throttle=`, `?mock-session-seconds=`), so
 * no Google script and no network are involved: the mock build's CSP allows no connection and no
 * Google source at all, and the first test checks that nothing left the page.
 *
 * Time: every test runs on the fake page clock from e2e/clock.ts. The one that needs the session
 * to expire moves that clock forward by a fixed amount, so none depends on the time of day (T37).
 */

/** Collects console errors, uncaught exceptions and CSP violations. */
async function watchErrors(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.exposeFunction('__reportCsp', (text: string) => errors.push(`CSP: ${text}`));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      (window as unknown as { __reportCsp(t: string): void }).__reportCsp(
        `${event.violatedDirective} ${event.blockedURI}`,
      );
    });
  });
  return errors;
}

const start = (page: Page) => page.getByRole('button', { name: 'View the live demo' });
const dashboard = (page: Page) => page.getByRole('main', { name: 'PulseBridge dashboard' });
const landingError = (page: Page) => page.locator('#landing-error');

test('the landing view: what the demo is, one button, the reCAPTCHA notice, no password', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  const requests: string[] = [];
  page.on('request', (request) => requests.push(request.url()));
  await page.goto('/');

  await expect(page.getByRole('heading', { level: 1, name: 'PulseBridge' })).toBeVisible();
  await expect(page.getByText(/is a personal health record/)).toBeVisible();
  await expect(page.getByText('This site is protected by reCAPTCHA.')).toBeVisible();
  await expect(start(page)).toBeVisible();
  await expect(start(page)).toBeFocused();
  await expect(page.locator('input')).toHaveCount(0);
  await expect(page.getByText(/password/i)).toHaveCount(0);
  // In a mock build no Google script is ever added.
  await expect(page.locator('script[src*="recaptcha"]')).toHaveCount(0);

  await start(page).click();
  await expect(dashboard(page)).toBeVisible();
  expect(requests.filter((url) => !url.startsWith('http://127.0.0.1:4173/'))).toEqual([]);
  expect(errors).toEqual([]);
});

test('keyboard only: Enter on the focused button starts the demo', async ({ page }) => {
  await page.goto('/');
  await expect(start(page)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(dashboard(page)).toBeVisible();
});

const REFUSALS: [outcome: string, text: string | RegExp][] = [
  ['demo-busy', 'The demo is busy, try again tomorrow.'],
  ['bot-check-failed', /The automatic bot check did not let this visit through/],
  ['bot-check-unavailable', /The bot check is not available right now/],
  ['origin-refused', /does not accept requests from this address/],
  ['rate-limited', 'Too many requests. Wait 42 seconds, then try again.'],
  ['config-unreachable', /The server could not be reached/],
];

for (const [outcome, text] of REFUSALS) {
  test(`?mock-demo=${outcome}: a clear message, and the visitor stays on the landing view`, async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await page.goto(`/?mock-demo=${outcome}`);
    await start(page).click();
    await expect(landingError(page)).toHaveText(text);
    await expect(landingError(page)).toHaveAttribute('role', 'alert');
    await expect(dashboard(page)).toHaveCount(0);
    // The button is back for another try.
    await expect(start(page)).toBeEnabled();
    expect(errors).toEqual([]);
  });
}

test('a 429 on the dashboard shows the wait under the header and the visitor stays', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await page.goto('/?mock-throttle=status');
  await start(page).click();
  await expect(dashboard(page)).toBeVisible();
  const notice = page.locator('#shell-notice');
  await expect(notice).toHaveText('Too many requests. Wait 30 seconds, then try again.');
  await expect(notice).toHaveAttribute('role', 'status');
  // The status strip says so too, and there is still a Log out button: no logout.
  await expect(page.getByRole('region', { name: 'System status' })).toContainText(
    'Too many requests',
  );
  await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible();
  // The notice goes once the wait is over; the visitor is still on the dashboard.
  await page.clock.fastForward(31_000);
  await expect(notice).toHaveText('');
  await expect(dashboard(page)).toBeVisible();
  expect(errors).toEqual([]);
});

test('a 401 mid-session (the session cookie expired) returns to the landing view', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await page.goto('/?mock-session-seconds=60');
  await start(page).click();
  await expect(dashboard(page)).toBeVisible();
  // Past the session's 60 s and the status strip's 5-minute reload: that reload gets a 401.
  await page.clock.fastForward('05:01');
  await expect(start(page)).toBeVisible();
  await expect(
    page.getByRole('status').filter({ hasText: 'demo session has ended' }),
  ).toBeVisible();
  // And a new session can be started from there.
  await start(page).click();
  await expect(dashboard(page)).toBeVisible();
  expect(errors).toEqual([]);
});

test('phone width: the landing view fits without horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(start(page)).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("Google's badge, when its script adds it, is hidden with visibility (not display: none)", async ({
  page,
}) => {
  await page.goto('/');
  await expect(start(page)).toBeVisible();
  // enterprise.js adds <div class="grecaptcha-badge"> to the body; the mock build loads no Google
  // script, so the test adds one the same way to check the page's own stylesheet.
  const style = await page.evaluate(() => {
    const badge = document.createElement('div');
    badge.className = 'grecaptcha-badge';
    badge.textContent = 'badge';
    document.body.append(badge);
    const computed = getComputedStyle(badge);
    return { visibility: computed.visibility, display: computed.display };
  });
  // The rule Google's FAQ gives: visibility: hidden, the element kept in layout.
  expect(style).toEqual({ visibility: 'hidden', display: 'block' });
});
