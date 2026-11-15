import { expect, test, type Page } from '@playwright/test';

/**
 * The mock's `dst-new-york` data set in the built page: heart-rate windows written in the FIXED
 * zone America/New_York, across the autumn 2026 clock change, viewed from a browser in another
 * zone. The chart's tick labels and the selected reading's details must show New York local
 * times as recorded, and the new offset after the change, never the browser's zone.
 *
 * The facts used below (US rules): daylight saving ends on Sunday 1 November 2026 at 02:00 EDT
 * (UTC-04:00), which is 06:00 UTC; clocks go back to 01:00 EST (UTC-05:00). So 01:00 local
 * happens twice: at 05:00 UTC (EDT) and at 06:00 UTC (EST).
 *
 * The page's clock is pinned to 2026-11-01 17:00 UTC (12:00 EST), so its 24-hour range runs from
 * Saturday 13:00 EDT to Sunday 12:00 EST and contains the change. The browser runs in
 * Asia/Kolkata (UTC+05:30): had the page used the browser's zone, whole-hour ticks would read
 * ":30" and the days would differ.
 */
test.use({ timezoneId: 'Asia/Kolkata', viewport: { width: 1280, height: 900 } });

const NOW = new Date('2026-11-01T17:00:00Z');

async function open(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text());
  });
  page.on('pageerror', (e) => errors.push(e.message));
  // Record the strings drawn on the canvas in its latest frame: uPlot draws the axis labels with
  // fillText, and starts every redraw by clearing the whole canvas with clearRect.
  await page.addInitScript(() => {
    const w = window as unknown as { __drawn: string[] };
    w.__drawn = [];
    const proto = CanvasRenderingContext2D.prototype;
    const fillText = Object.getOwnPropertyDescriptor(proto, 'fillText')?.value as (
      this: CanvasRenderingContext2D,
      ...args: Parameters<CanvasRenderingContext2D['fillText']>
    ) => void;
    const clearRect = Object.getOwnPropertyDescriptor(proto, 'clearRect')?.value as (
      this: CanvasRenderingContext2D,
      ...args: Parameters<CanvasRenderingContext2D['clearRect']>
    ) => void;
    proto.fillText = function (this: CanvasRenderingContext2D, text, ...rest) {
      w.__drawn.push(text);
      fillText.call(this, text, ...rest);
    };
    proto.clearRect = function (this: CanvasRenderingContext2D, ...args) {
      w.__drawn = [];
      clearRect.call(this, ...args);
    };
  });
  await page.clock.setFixedTime(NOW);
  await page.goto('/?mock-data=dst-new-york');
  await page.getByLabel('Password').fill('any password works in mock mode');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.locator('#hr-summary')).toContainText('last 24 hours');
  return errors;
}

const watchSide = (page: Page) => page.getByRole('region', { name: 'Heart rate' });
const details = (page: Page) => watchSide(page).getByRole('region', { name: 'Selected reading' });

/** The x-axis labels drawn last: lines that are a clock time, a day, or an offset. */
async function axisLabels(page: Page): Promise<string[]> {
  const drawn = await page.evaluate(() => (window as unknown as { __drawn: string[] }).__drawn);
  return drawn.filter((t) => /^(\d{2}:\d{2}|\d{1,2} [A-Z][a-z]{2}|UTC[+-]\d{2}:\d{2})$/.test(t));
}

test('the zone note names both New York offsets, not the browser zone', async ({ page }) => {
  const errors = await open(page);
  await expect(watchSide(page)).toContainText(
    'Times as recorded by the watch (UTC-04:00, then UTC-05:00: a clock change falls in this range).',
  );
  await expect(watchSide(page)).not.toContainText('UTC+05:30');
  expect(errors).toEqual([]);
});

test('the tick labels are New York local times, and name the new offset after the change', async ({
  page,
}) => {
  const errors = await open(page);
  await expect(watchSide(page).locator('.hr-chart canvas')).toBeVisible();
  await expect.poll(async () => (await axisLabels(page)).length).toBeGreaterThan(4);
  const labels = await axisLabels(page);

  // At this width the axis has a tick every 6 local hours. Worked out by hand in New York time:
  //   before the change (EDT, UTC-04:00): 18:00 Sat (22:00 UTC), 00:00 Sun (04:00 UTC), naming
  //     the day on the first tick and at local midnight;
  //   the change at 06:00 UTC: 06:00 EDT would be 10:00 UTC, but by then the clock is EST, so the
  //     EST segment ticks at 06:00 EST (11:00 UTC), naming the new offset, then 12:00 EST.
  // In the browser's zone (Kolkata) the first tick would read 03:30; in UTC, 18:00 then 00:00
  // with no offset named.
  expect(labels).toEqual(['18:00', '31 Oct', '00:00', '1 Nov', '06:00', 'UTC-05:00', '12:00']);
  expect(errors).toEqual([]);
});

test('the details show the local time and the offset of each side of the change', async ({
  page,
}) => {
  const errors = await open(page);
  const slider = watchSide(page).getByRole('slider');
  await slider.focus();

  // The oldest window in range: Saturday 13:00 EDT (17:00 UTC).
  await page.keyboard.press('Home');
  await page.keyboard.press('Enter');
  await expect(details(page)).toContainText('Sat 31 Oct 2026, 13:00–13:05 (UTC-04:00)');

  // Twelve hours later: 05:00 UTC, the first 01:00, still daylight time.
  for (let i = 0; i < 12; i++) await page.keyboard.press('PageUp');
  await page.keyboard.press('Enter');
  await expect(details(page)).toContainText('Sun 1 Nov 2026, 01:00–01:05 (UTC-04:00)');

  // One hour later: 06:00 UTC, the second 01:00, now standard time.
  await page.keyboard.press('PageUp');
  await page.keyboard.press('Enter');
  await expect(details(page)).toContainText('Sun 1 Nov 2026, 01:00–01:05 (UTC-05:00)');

  // The newest window: the mock's last sync run was 16:47 UTC, so 16:40-16:45 UTC, 11:40 EST.
  await page.keyboard.press('End');
  await page.keyboard.press('Enter');
  await expect(details(page)).toContainText('Sun 1 Nov 2026, 11:40–11:45 (UTC-05:00)');

  // The trace shows the Observation exactly as stored: New York offsets in effectivePeriod.
  const trace = page.getByRole('region', { name: 'Heart-rate trace' });
  await expect(trace.getByRole('tabpanel')).toContainText('"start": "2026-11-01T11:40:00-05:00"');
  expect(errors).toEqual([]);
});
