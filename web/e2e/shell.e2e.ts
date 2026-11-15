import { expect, test, type Page } from '@playwright/test';

/** Collects console errors and uncaught exceptions, including CSP violations. */
function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  return errors;
}

async function logIn(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('Password').fill('any password works in mock mode');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('main', { name: 'PulseBridge dashboard' })).toBeVisible();
}

test('login, regions, share button, log strip, no console errors', async ({ page }) => {
  const errors = watchErrors(page);
  const requests: string[] = [];
  page.on('request', (request) => requests.push(request.url()));

  await page.goto('/');
  await expect(page.getByText('Any non-empty password logs in')).toBeVisible();
  await logIn(page);

  // Token in sessionStorage, no cookie.
  const stored = await page.evaluate(() => sessionStorage.getItem('pulsebridge.session'));
  expect(stored).toContain('mock-session-');
  expect(await page.context().cookies()).toEqual([]);

  // Landmarks and regions.
  await expect(page.getByRole('banner')).toBeVisible();
  await expect(page.getByRole('region', { name: 'System status' })).toContainText('Last sync');
  await expect(page.getByRole('region', { name: 'Blood pressure' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Heart rate' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Share with clinic' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'As stored in OpenEMR' })).toBeVisible();
  await expect(page.getByRole('img', { name: /blood-pressure cuff and a watch/ })).toBeVisible();
  for (const slot of [
    'cuff-panel',
    'cuff-trace',
    'heart-rate-panel',
    'heart-rate-trace',
    'share-trace',
    'share-readback',
  ]) {
    await expect(page.locator(`[data-slot="${slot}"]`)).toBeVisible();
  }

  // Share is disabled with nothing selected.
  await expect(page.getByRole('button', { name: 'Share with clinic' })).toBeDisabled();

  // The messages-log strip starts collapsed, then expands and collapses.
  const toggle = page.getByRole('button', { name: /Messages log/ });
  const log = page.locator('#messages-log');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(log).toBeHidden();
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(log).toBeVisible();
  await toggle.press('Enter');
  await expect(log).toBeHidden();

  // Logout returns to the login form and forgets the token.
  await page.getByRole('button', { name: 'Log out' }).click();
  await expect(page.getByLabel('Password')).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('pulsebridge.session'))).toBeNull();

  // Mock mode makes no request beyond the page's own files on the preview server.
  expect(requests.filter((url) => !url.startsWith('http://127.0.0.1:4173/'))).toEqual([]);
  expect(errors).toEqual([]);
});

test('keyboard: the skip link and controls are reachable in order', async ({ page }) => {
  const errors = watchErrors(page);
  await logIn(page);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to dashboard' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Log out' })).toBeFocused();
  // Share is disabled, so the next stop is the messages-log toggle.
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: /Messages log/ })).toBeFocused();
  expect(errors).toEqual([]);
});

test('phone width: everything stacks in one column without horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await logIn(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  const cuff = await page.getByRole('region', { name: 'Blood pressure' }).boundingBox();
  const watch = await page.getByRole('region', { name: 'Heart rate' }).boundingBox();
  expect(cuff && watch && watch.y > cuff.y + cuff.height - 1).toBe(true);
});
