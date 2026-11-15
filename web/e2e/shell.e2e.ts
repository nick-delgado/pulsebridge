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

test('keyboard order: skip link, Log out, cuff side, heart-rate side, share, log', async ({
  page,
}) => {
  const errors = watchErrors(page);
  await logIn(page);
  // Tab through the page and record which area each focus stop is in. Any number of controls
  // inside a feature area is fine (T17 and T18 will add some); only the ORDER of areas is fixed.
  const AREAS = ['skip', 'header', 'cuff', 'watch', 'share', 'log'];
  const visited: string[] = [];
  for (let i = 0; i < 80; i++) {
    await page.keyboard.press('Tab');
    const area = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return 'none';
      if (el.closest('.skip-link')) return 'skip';
      if (el.closest('.app-header')) return 'header';
      if (el.closest('.side--cuff')) return 'cuff';
      if (el.closest('.side--watch')) return 'watch';
      if (el.closest('.share')) return 'share';
      if (el.closest('.messages-strip')) return 'log';
      return `other:${el.tagName}`;
    });
    visited.push(area);
    if (area === 'log') break;
  }
  expect(visited[0]).toBe('skip');
  expect(visited).toContain('header');
  expect(visited.at(-1)).toBe('log');
  expect(visited.filter((a) => !AREAS.includes(a))).toEqual([]);
  const ranks = visited.map((a) => AREAS.indexOf(a));
  expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
  expect(errors).toEqual([]);
});

test('the figure is line art: no shape in the silhouette is filled', async ({ page }) => {
  await logIn(page);
  const fills = await page.evaluate(() =>
    [
      ...document.querySelectorAll('.figure__svg :is(path, rect, circle, ellipse, polygon, line)'),
    ].map((shape) => getComputedStyle(shape).fill),
  );
  expect(fills.length).toBeGreaterThan(5);
  expect(new Set(fills)).toEqual(new Set(['none']));
});

for (const scheme of ['light', 'dark'] as const) {
  test(`login field boundary has at least 3:1 contrast with its card (${scheme}, WCAG 1.4.11)`, async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto('/');
    await expect(page.getByLabel('Password')).toBeVisible();
    const ratio = await page.evaluate(() => {
      const rgb = (c: string) => (c.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number);
      const lum = (c: string) => {
        const [r = 0, g = 0, b = 0] = rgb(c).map((v) => {
          const x = v / 255;
          return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const input = document.querySelector('#login-password');
      const card = document.querySelector('.login__card');
      if (!input || !card) return 0;
      const a = lum(getComputedStyle(input).borderTopColor);
      const b = lum(getComputedStyle(card).backgroundColor);
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(3);
  });
}

test('landmark names are distinct and stable', async ({ page }) => {
  await logIn(page);
  await expect(page.getByRole('region', { name: 'Blood-pressure trace' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Heart-rate trace' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Messages log', exact: true })).toBeAttached();
  await expect(page.getByRole('region', { name: 'Trace', exact: true })).toHaveCount(0);
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
