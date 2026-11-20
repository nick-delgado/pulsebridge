import {
  CHARGING_GAP_CLOCK,
  DEFAULT_CLOCK,
  expect,
  pageNow,
  test,
  type Page,
} from '../../e2e/clock';

/**
 * Browser tests for the heart-rate area, against the BUILT mock page (synthetic data only), so
 * they run the real uPlot canvas under the page's Content-Security-Policy.
 *
 * The page's clock is set for every test (e2e/clock.ts). Tests that need recent watch data, with
 * provisional windows at the end, pin DEFAULT_CLOCK; the "watch on the charger" tests pin
 * CHARGING_GAP_CLOCK; the rest hold at any time of day (PULSEBRIDGE_E2E_NOW moves them).
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
  await page.getByRole('button', { name: 'View the live demo' }).click();
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

test.describe('recent watch data: the newest windows are provisional', () => {
  // 09:20 UTC: the last sync run (09:17) wrote windows up to 09:15, and every window ending after
  // 08:47 is still preliminary. Pinned: at other times (the charging gap) there are none.
  test.use({ clockAt: DEFAULT_CLOCK });

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

  test('keyboard selection: Share stays disabled for a provisional window, enabled for a final one', async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await logIn(page);
    await expect(summary(page)).toContainText('five-minute windows');
    const share = page.getByRole('button', { name: 'Share with clinic' });

    // Reach the chart with Tab from the range buttons, and see the focus ring.
    await page.getByRole('button', { name: 'Show the last 7 days' }).focus();
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
});

test('switching ranges loads 7 days, and there is no 30-day range (T47)', async ({ page }) => {
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

  // Only 24 h and 7 d: the proxy shows nothing older than HISTORY_DAYS (7 by default).
  const ranges = watchSide(page).getByRole('group', { name: 'Time range' }).getByRole('button');
  await expect(ranges).toHaveText(['24 h', '7 d']);
  await expect(page.getByRole('button', { name: 'Show the last 30 days' })).toHaveCount(0);

  // Still responsive at ~2,000 windows: moving the keyboard cursor answers quickly.
  await slider(page).focus();
  const t0 = Date.now();
  for (let i = 0; i < 20; i++) await page.keyboard.press('ArrowLeft');
  await expect(slider(page)).toHaveAttribute('aria-valuenow', String(week - 20));
  expect(Date.now() - t0).toBeLessThan(5000);
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
        band: ratio(swatch('band'), surface),
        button: pressedStyle
          ? ratio(rgb(pressedStyle.color), rgb(pressedStyle.backgroundColor))
          : 0,
        animations: document.getAnimations().length,
      };
    });
    expect(ratios.line).toBeGreaterThanOrEqual(3);
    expect(ratios.provisional).toBeGreaterThanOrEqual(3);
    expect(ratios.band).toBeGreaterThanOrEqual(3);
    expect(ratios.button).toBeGreaterThanOrEqual(4.5);
    expect(ratios.animations).toBe(0);
    expect(errors).toEqual([]);
  });
}

/** The effectivePeriod start of the Observation in the trace panel's "FHIR as stored" tab. */
async function tracedStart(page: Page): Promise<string> {
  const trace = page.getByRole('region', { name: 'Heart-rate trace' });
  await expect(trace.getByRole('tabpanel')).toContainText('"effectivePeriod"');
  const text = (await trace.getByRole('tabpanel').textContent()) ?? '';
  return /"effectivePeriod": \{\s*"start": "([^"]+)"/.exec(text)?.[1] ?? '';
}

test('a click selects the window under the pointer', async ({ page }) => {
  const errors = await watchErrors(page);
  // Wide, so one pixel is little more than a minute and a click lands well inside one window.
  await page.setViewportSize({ width: 1920, height: 1100 });
  await logIn(page);
  await expect(summary(page)).toContainText('last 24 hours');
  // The page's range ends at its own Date.now() when it loaded: read the page's clock, not ours.
  const loadedAt = await pageNow(page);
  const over = watchSide(page).locator('.hr-chart .u-over');
  const box = await over.boundingBox();
  if (!box) throw new Error('no plot');

  // Aim at the middle of a known window about six hours back (away from the mock's 12:00-12:40
  // UTC charging gap).
  const FIVE_MIN = 5 * 60_000;
  let windowStart = Math.floor((loadedAt - 6 * 3600_000) / FIVE_MIN) * FIVE_MIN;
  const hour = new Date(windowStart).getUTCHours();
  if (hour >= 11 && hour <= 13) windowStart -= 3 * 3600_000;
  const rangeStart = loadedAt - 24 * 3600_000;
  const target = windowStart + FIVE_MIN / 2;
  const pageX = Math.round(box.x + ((target - rangeStart) / (24 * 3600_000)) * box.width);
  await page.mouse.click(pageX, box.y + box.height / 2);

  expect(Date.parse(await tracedStart(page))).toBe(windowStart);
  expect(errors).toEqual([]);
});

test('a click with a little pointer movement still selects (uPlot does not swallow it)', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(summary(page)).toContainText('five-minute windows');
  const box = await watchSide(page).locator('.hr-chart .u-over').boundingBox();
  if (!box) throw new Error('no plot');
  const x = box.x + box.width / 3;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 3, y + 2, { steps: 3 });
  await page.mouse.up();
  await expect(watchSide(page).getByRole('region', { name: 'Selected reading' })).toBeVisible();
  await expect(page.locator('#share-hint')).toContainText('Heart rate');
  expect(errors).toEqual([]);
});

test('a click on the left edge of the plot is not swallowed: it selects the window there', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(summary(page)).toContainText('last 24 hours');
  const box = await watchSide(page).locator('.hr-chart .u-over').boundingBox();
  if (!box) throw new Error('no plot');
  await page.mouse.click(Math.ceil(box.x), box.y + box.height / 2);
  // The window nearest the left edge is the oldest one in range, whatever the time of day: the
  // window containing the edge time, or, when that falls in the charging gap, the first one
  // after it. Home on the chart's slider selects exactly that window.
  const clicked = await tracedStart(page);
  expect(clicked).not.toBe('');
  await slider(page).focus();
  await page.keyboard.press('End');
  await page.keyboard.press('Enter');
  await expect.poll(() => tracedStart(page)).not.toBe(clicked);
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  await expect
    .poll(() => tracedStart(page), { message: 'Home selects the window the click selected' })
    .toBe(clicked);
  expect(errors).toEqual([]);
});

test('uPlot draws no legend of its own (the page writes its own key and readout)', async ({
  page,
}) => {
  await logIn(page);
  await expect(watchSide(page).locator('.hr-chart canvas')).toBeVisible();
  await expect(page.locator('.u-legend')).toHaveCount(0);
  await expect(watchSide(page).getByRole('list', { name: 'Chart key' })).toBeVisible();
});

test.describe('watch on the charger: no provisional window, the newest data 50 minutes old', () => {
  // 12:50 UTC: the mock's daily charging gap left out the windows from 12:00, and the 12:47 run
  // had nothing to write. A real state (the watch on its charger), so the page must say it
  // plainly rather than imply recent or provisional data.
  test.use({ clockAt: CHARGING_GAP_CLOCK });

  test('statistics say none provisional; the summary names the final 11:55 window as latest', async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await logIn(page);
    await expect(summary(page)).toContainText('last 24 hours');
    await expect(summary(page)).toContainText(
      /to Mon 16 Nov 2026, 11:55–12:00 \(UTC\+00:00\)\. Latest: \d+(\.\d)? beats\/min, final\./,
    );
    const windows = watchSide(page).getByRole('definition').filter({ hasText: 'provisional' });
    await expect(windows).toHaveText('none provisional');
    await expect(watchSide(page)).not.toContainText(/\d+ provisional/);
    // The status strip: the newest write was the 12:32 run (it finalised 11:45-12:00), not the
    // 12:47 run that wrote nothing.
    await expect(page.getByRole('region', { name: 'System status' })).toContainText(
      'Newest watch data: 18 min ago',
    );
    expect(errors).toEqual([]);
  });

  test('the newest window is final, so it can be shared', async ({ page }) => {
    const errors = await watchErrors(page);
    await logIn(page);
    await expect(summary(page)).toContainText('five-minute windows');
    await slider(page).focus();
    await page.keyboard.press('End');
    await expect(slider(page)).toHaveAttribute('aria-valuetext', /11:55–12:00.*; final$/);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('button', { name: 'Share with clinic' })).toBeEnabled();
    await expect(watchSide(page).getByRole('region', { name: 'Selected reading' })).toContainText(
      'Mon 16 Nov 2026, 11:55–12:00 (UTC+00:00)',
    );
    expect(errors).toEqual([]);
  });
});
