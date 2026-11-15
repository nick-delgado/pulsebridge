import { expect, test, type Page } from '@playwright/test';

/**
 * Browser tests for the heart-rate area, against the BUILT mock page (synthetic data only), so
 * they run the real uPlot canvas under the page's Content-Security-Policy.
 */

/** Console errors, uncaught exceptions and CSP violations (reported both ways, to be sure). */
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

async function logIn(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByLabel('Password').fill('any password works in mock mode');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('main', { name: 'PulseBridge dashboard' })).toBeVisible();
}

const watchSide = (page: Page) => page.getByRole('region', { name: 'Heart rate' });
const slider = (page: Page) => watchSide(page).getByRole('slider');
const summary = (page: Page) => page.locator('#hr-summary');

/** "… 8,370 five-minute windows …" → 8370. */
async function windowCount(page: Page): Promise<number> {
  const text = (await summary(page).textContent()) ?? '';
  const match = /: ([\d,]+) five-minute windows/.exec(text);
  return Number((match?.[1] ?? '0').replaceAll(',', ''));
}

test('the chart renders the mock data with statistics and a text summary', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  const canvas = watchSide(page).locator('.hr-chart canvas');
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  expect(box && box.width > 200 && box.height > 150).toBe(true);

  await expect(summary(page)).toContainText('last 24 hours');
  const n = await windowCount(page);
  // 24 h of five-minute windows is 288; the mock leaves out about 40 minutes of "charging".
  expect(n).toBeGreaterThan(250);
  expect(n).toBeLessThanOrEqual(288);
  const stats = watchSide(page)
    .getByRole('definition')
    .filter({ hasText: /\/min$/ });
  await expect(stats).toHaveCount(3);
  await expect(watchSide(page)).toContainText('mean of the five-minute averages');
  await expect(watchSide(page)).toContainText(/\d+ provisional/);
  await expect(watchSide(page)).toContainText('Times as recorded by the watch (UTC+00:00).');
  await expect(watchSide(page).getByText('Provisional: not final yet')).toBeVisible();
  expect(errors).toEqual([]);
});

test('switching ranges loads 7 and 30 days', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(summary(page)).toContainText('last 24 hours');
  const day = await windowCount(page);

  await page.getByRole('button', { name: 'Show the last 7 days' }).click();
  await expect(page.getByRole('button', { name: 'Show the last 7 days' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(summary(page)).toContainText('last 7 days');
  const week = await windowCount(page);
  expect(week).toBeGreaterThan(6 * day);

  await page.getByRole('button', { name: 'Show the last 30 days' }).click();
  await expect(summary(page)).toContainText('last 30 days');
  const month = await windowCount(page);
  expect(month).toBeGreaterThan(8000);

  // Still responsive at ~8,600 windows: moving the keyboard cursor answers quickly.
  await slider(page).focus();
  const t0 = Date.now();
  for (let i = 0; i < 20; i++) await page.keyboard.press('ArrowLeft');
  await expect(slider(page)).toHaveAttribute('aria-valuenow', String(month - 20));
  expect(Date.now() - t0).toBeLessThan(5000);
  expect(errors).toEqual([]);
});

test('keyboard selection: Share stays disabled for a provisional window, enabled for a final one', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(summary(page)).toContainText('five-minute windows');
  const share = page.getByRole('button', { name: 'Share with clinic' });

  // Reach the chart with Tab from the range buttons, and see the focus ring.
  await page.getByRole('button', { name: 'Show the last 30 days' }).focus();
  await page.keyboard.press('Tab');
  await expect(slider(page)).toBeFocused();
  const outline = await slider(page).evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).toBe('solid');

  // The newest window is preliminary (the mock's last 30 minutes).
  await page.keyboard.press('End');
  await expect(slider(page)).toHaveAttribute(
    'aria-valuetext',
    /provisional \(status preliminary\)/,
  );
  await page.keyboard.press('Enter');
  await expect(share).toBeDisabled();
  await expect(page.locator('#share-hint')).toContainText('still provisional');
  await expect(watchSide(page).getByRole('region', { name: 'Selected reading' })).toContainText(
    'provisional (status preliminary)',
  );

  // The oldest window is final.
  await page.keyboard.press('Home');
  await expect(slider(page)).toHaveAttribute('aria-valuetext', /; final$/);
  await page.keyboard.press('Enter');
  await expect(share).toBeEnabled();
  await expect(page.locator('#share-hint')).toContainText('Selected: Heart rate');
  expect(errors).toEqual([]);
});

test('the trace tabs show the stored FHIR and the reconstructed Google Health window as text', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(summary(page)).toContainText('five-minute windows');
  // A click on the chart picks the nearest window.
  const canvas = watchSide(page).locator('.hr-chart .u-over');
  await canvas.click({ position: { x: 100, y: 50 } });
  const trace = page.getByRole('region', { name: 'Heart-rate trace' });
  const fhirTab = trace.getByRole('tab', { name: 'FHIR as stored' });
  await expect(fhirTab).toHaveAttribute('aria-selected', 'true');
  await expect(trace.getByRole('tabpanel')).toContainText('"resourceType": "Observation"');
  await expect(trace.getByRole('tabpanel')).toContainText('"resourceType": "Provenance"');
  await expect(trace.getByRole('tabpanel')).toContainText('"resourceType": "Device"');
  await expect(trace.getByRole('tabpanel')).toContainText('"code": "8867-4"');

  // Arrow keys move between the tabs (the shell's accessible tabs).
  await fhirTab.focus();
  await page.keyboard.press('ArrowRight');
  const sourceTab = trace.getByRole('tab', { name: 'Google Health JSON it came from' });
  await expect(sourceTab).toHaveAttribute('aria-selected', 'true');
  await expect(trace.getByRole('tabpanel')).toContainText('Reconstructed from the stored reading');
  await expect(trace.getByRole('tabpanel')).toContainText('"beatsPerMinuteAvg"');
  await expect(trace.locator('script, img')).toHaveCount(0);
  expect(errors).toEqual([]);
});

for (const scheme of ['light', 'dark'] as const) {
  test(`phone width, ${scheme}: no horizontal scroll, chart fits, colours contrast`, async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 390, height: 844 });
    await logIn(page);
    await expect(summary(page)).toContainText('five-minute windows');
    const canvas = watchSide(page).locator('.hr-chart canvas');
    const box = await canvas.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= 390).toBe(true);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);

    // Tap a point on a phone.
    await watchSide(page)
      .locator('.hr-chart .u-over')
      .click({ position: { x: 60, y: 40 } });
    await expect(watchSide(page).getByRole('region', { name: 'Selected reading' })).toBeVisible();

    // The line, the provisional line and the selected range button against their background:
    // at least 3:1 for graphics (WCAG 1.4.11), 4.5:1 for the button text.
    const ratios = await page.evaluate(() => {
      const rgb = (c: string) => (c.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number);
      const lum = (v: number[]) => {
        const [r = 0, g = 0, b = 0] = v.map((x) => {
          const s = x / 255;
          return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const ratio = (a: number[], b: number[]) => {
        const [x, y] = [lum(a), lum(b)];
        return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
      };
      // The legend swatches use the same tokens the canvas reads (chart.ts), so their computed
      // colours are what the chart draws with; the card is the background behind the chart.
      const swatch = (cls: string) => {
        const el = document.querySelector(`.hr-swatch--${cls}`);
        return el ? rgb(getComputedStyle(el).borderTopColor) : [0, 0, 0];
      };
      const card = document.querySelector('.side--watch .side__panel');
      const surface = card ? rgb(getComputedStyle(card).backgroundColor) : [0, 0, 0];
      const pressed = document.querySelector('.hr-ranges__button[aria-pressed="true"]');
      const pressedStyle = pressed ? getComputedStyle(pressed) : null;
      return {
        line: ratio(swatch('line'), surface),
        provisional: ratio(swatch('provisional'), surface),
        button: pressedStyle
          ? ratio(rgb(pressedStyle.color), rgb(pressedStyle.backgroundColor))
          : 0,
        animations: document.getAnimations().length,
      };
    });
    expect(ratios.line).toBeGreaterThanOrEqual(3);
    expect(ratios.provisional).toBeGreaterThanOrEqual(3);
    expect(ratios.button).toBeGreaterThanOrEqual(4.5);
    expect(ratios.animations).toBe(0);
    expect(errors).toEqual([]);
  });
}
