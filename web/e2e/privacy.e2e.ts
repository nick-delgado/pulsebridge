import { expect, test, type Page } from './clock';

/**
 * The privacy note (privacy.html, T48) in the built mock page: reachable from the landing view
 * and from the dashboard, readable with no script and no request off the site, free of CSP
 * violations (the built page carries the CSP <meta>), with a way back to the app.
 */

async function watch(page: Page): Promise<{ errors: string[]; offSite: string[] }> {
  const errors: string[] = [];
  const offSite: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (!request.url().startsWith('http://127.0.0.1:4173/')) offSite.push(request.url());
  });
  await page.exposeFunction('__reportCsp', (text: string) => errors.push(`CSP: ${text}`));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      (window as unknown as { __reportCsp(t: string): void }).__reportCsp(
        `${event.violatedDirective} ${event.blockedURI}`,
      );
    });
  });
  return { errors, offSite };
}

const privacyHeading = (page: Page) => page.getByRole('heading', { level: 1, name: 'Privacy' });

test('from the landing view: the privacy note, with no script, and back', async ({ page }) => {
  const seen = await watch(page);
  await page.goto('/');
  await page.getByRole('link', { name: /privacy/i }).click();
  await expect(privacyHeading(page)).toBeVisible();
  await expect(page).toHaveURL(/\/privacy\.html$/);
  await expect(page.getByText('__session', { exact: true })).toBeVisible();
  await expect(page.getByText('Only the last 7 days are shown', { exact: false })).toBeVisible();
  expect(await page.locator('script').count()).toBe(0);
  // Styled by its own same-origin stylesheet (the CSP allows style-src 'self' only).
  const padding = await page
    .locator('.privacy__card')
    .evaluate((node) => getComputedStyle(node).paddingTop);
  expect(padding).not.toBe('0px');
  await page.getByRole('link', { name: 'Back to PulseBridge' }).click();
  await expect(page.getByRole('button', { name: 'View the live demo' })).toBeVisible();
  expect(seen.offSite).toEqual([]);
  expect(seen.errors).toEqual([]);
});

test('from the dashboard header: the privacy note, then back to the dashboard', async ({
  page,
}) => {
  const seen = await watch(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'View the live demo' }).click();
  const dashboard = page.getByRole('main', { name: 'PulseBridge dashboard' });
  await expect(dashboard).toBeVisible();
  await page.getByRole('banner').getByRole('link', { name: 'Privacy' }).click();
  await expect(privacyHeading(page)).toBeVisible();
  // The mock keeps its "cookie" in page memory, so coming back starts on the landing view; a
  // deployed page's __session cookie survives the visit (checked at rollout, checklist P17).
  await page.goBack();
  await expect(page.getByRole('button', { name: 'View the live demo' })).toBeVisible();
  expect(seen.offSite).toEqual([]);
  expect(seen.errors).toEqual([]);
});
