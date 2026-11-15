import { expect, test, type Page } from '@playwright/test';

/**
 * Browser tests for the cuff area, against the BUILT mock page (synthetic data only), under the
 * page's Content-Security-Policy. Each test fails on any console error, uncaught exception or
 * CSP violation.
 */

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

const cuffSide = (page: Page) => page.getByRole('region', { name: 'Blood pressure' });
const trace = (page: Page) => page.getByRole('region', { name: 'Blood-pressure trace' });
const rows = (page: Page) => cuffSide(page).locator('.cuff-reading');
const share = (page: Page) => page.getByRole('button', { name: 'Share with clinic' });

test('keyboard only: enter a reading, send it, open its trace, and it becomes shareable', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);

  // Tab from the top of the page until the systolic field has focus.
  for (let i = 0; i < 20; i++) {
    if ((await page.evaluate(() => document.activeElement?.id)) === 'cuff-systolic') break;
    await page.keyboard.press('Tab');
  }
  await expect(page.locator('#cuff-systolic')).toBeFocused();
  await page.keyboard.press('Control+A');
  await page.keyboard.type('118');
  await page.keyboard.press('Tab');
  await expect(page.locator('#cuff-diastolic')).toBeFocused();
  await page.keyboard.press('Control+A');
  await page.keyboard.type('76');
  await page.keyboard.press('Tab');
  await expect(page.locator('#cuff-transport-mllp')).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(page.locator('#cuff-transport-https')).toBeChecked();
  await page.keyboard.press('Tab');
  await expect(page.locator('#cuff-malformed')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.locator('#cuff-send')).toBeFocused();
  const outline = await page
    .locator('#cuff-send')
    .evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).toBe('solid');
  await page.keyboard.press('Enter');

  await expect(rows(page)).toHaveCount(1);
  const row = rows(page).first();
  await expect(row).toContainText('BP 118/76 mm[Hg]');
  await expect(row).toContainText('HTTPS');
  await expect(row).toContainText('AA, stored');
  await expect(page.locator('#cuff-form-message')).toContainText(
    'Commit-level acknowledgment: AA, stored',
  );

  // Tab to the row's "Show trace" and press it.
  for (let i = 0; i < 10; i++) {
    const focused = await page.evaluate(() => document.activeElement?.className ?? '');
    if (focused.includes('cuff-reading__show')) break;
    await page.keyboard.press('Tab');
  }
  await page.keyboard.press('Enter');
  await expect(row.locator('.cuff-reading__show')).toHaveAttribute('aria-pressed', 'true');

  // Into the trace tabs; arrow keys move between them.
  const hl7Tab = trace(page).getByRole('tab', { name: 'HL7v2 as sent' });
  await hl7Tab.focus();
  await page.keyboard.press('ArrowRight');
  const ackTab = trace(page).getByRole('tab', { name: 'ACK as received' });
  await expect(ackTab).toHaveAttribute('aria-selected', 'true');
  await expect(trace(page).getByRole('tabpanel')).toContainText(
    'Commit-level acknowledgment (from the store)',
  );
  await expect(trace(page).getByRole('tabpanel')).toContainText(
    'Application-level outcome (from the mapper)',
  );

  // The mock maps after about 3 s; the panel polls every 3 s.
  await expect(row).toContainText('Accepted', { timeout: 15_000 });
  await expect(trace(page).getByRole('tabpanel')).toContainText('Accepted: the mapper wrote');
  await expect(share(page)).toBeEnabled();
  await expect(page.locator('#share-hint')).toContainText('Selected: BP 118/76 mm[Hg]');
  await page.keyboard.press('ArrowRight');
  await expect(trace(page).getByRole('tabpanel')).toContainText('"resourceType": "Observation"');
  await expect(trace(page).getByRole('tabpanel')).toContainText('"code": "85354-9"');
  expect(errors).toEqual([]);
});

test('HL7v2 tab: segments one per line, standard framing, the derived name labelled as such', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await cuffSide(page).getByRole('button', { name: 'Take reading' }).click();
  await expect(rows(page)).toHaveCount(1);
  const panel = trace(page).getByRole('tabpanel');
  await expect(panel).toContainText('standard frame');
  const code = (await panel.locator('pre code').first().textContent()) ?? '';
  expect(
    code
      .split('\n')
      .map((l) => l.slice(0, 3))
      .slice(0, 5),
  ).toEqual(['MSH', 'PID', 'OBR', 'OBX', 'OBX']);
  await expect(panel).toContainText('Stored message name (derived, unconfirmed)');
  await expect(trace(page).locator('script, img')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('out of the mapper range: stored with AA, then an application-level error in words', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await page.getByRole('textbox', { name: 'Systolic' }).fill('320');
  await expect(cuffSide(page)).toContainText("Outside the mapper's plausible systolic range");
  await cuffSide(page).getByRole('button', { name: 'Take reading' }).click();
  const row = rows(page).first();
  await expect(row).toContainText('AA, stored');
  await expect(row).toContainText('Pending');
  await expect(row).toContainText('Error: A value is outside the plausible range', {
    timeout: 15_000,
  });
  await expect(row).toContainText('(reason code out-of-range)');
  await expect(share(page)).toBeDisabled();
  await expect(page.locator('#share-hint')).toContainText(
    'the mapper could not turn it into an Observation: A value is outside the plausible range',
  );
  expect(errors).toEqual([]);
});

test('send malformed: samples by class, MLLP-only disabled over HTTPS, AE and no acknowledgment', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await page.getByLabel('Send malformed').check();
  const samples = page.getByRole('group', { name: 'Malformed sample' });
  await expect(samples).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Systolic' })).toBeDisabled();

  await page.getByRole('radio', { name: 'HTTPS to the store' }).check();
  await expect(page.getByRole('radio', { name: 'noack_missing_end_block' })).toBeDisabled();
  await page.getByRole('radio', { name: 'unparseable_no_msh' }).check();
  await page.getByRole('button', { name: 'Send malformed sample' }).click();
  await expect(rows(page).first()).toContainText('Malformed sample unparseable_no_msh');
  await expect(rows(page).first()).toContainText('AE, rejected');
  await expect(rows(page).first()).toContainText('Invalid MSH header');

  await page.getByRole('radio', { name: 'MLLP through the adapter' }).check();
  await page.getByRole('radio', { name: 'noack_missing_end_block' }).check();
  await page.getByRole('button', { name: 'Send malformed sample' }).click();
  await expect(rows(page)).toHaveCount(2);
  await expect(rows(page).first()).toContainText('No acknowledgment');
  const panel = trace(page).getByRole('tabpanel');
  await expect(panel).toContainText('Framing NOT standard');
  await trace(page).getByRole('tab', { name: 'ACK as received' }).click();
  await expect(panel).toContainText('No acknowledgment received');
  await expect(share(page)).toBeDisabled();
  expect(errors).toEqual([]);
});

test('rate limit: the fourth quick send gets a countdown and a disabled button', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  const button = cuffSide(page).locator('#cuff-send');
  for (let i = 1; i <= 3; i++) {
    await button.click();
    await expect(rows(page)).toHaveCount(i);
  }
  await button.click();
  await expect(page.locator('#cuff-form-message')).toContainText('Too many readings');
  await expect(page.locator('#cuff-countdown')).toHaveText(/You can send again in \d+ s\./);
  await expect(button).toBeDisabled();
  await expect(rows(page)).toHaveCount(3);
  expect(errors).toEqual([]);
});

test('the trace frame keeps its height across states and tabs (no layout shift)', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  const frame = trace(page).locator('.cuff-trace__frame');
  const height = async () => (await frame.boundingBox())?.height ?? 0;
  const empty = await height();
  expect(empty).toBeGreaterThan(200);
  await cuffSide(page).getByRole('button', { name: 'Take reading' }).click();
  await expect(trace(page).getByRole('tab', { name: 'FHIR as stored' })).toBeVisible();
  expect(await height()).toBe(empty);
  for (const name of ['ACK as received', 'FHIR as stored', 'HL7v2 as sent']) {
    await trace(page).getByRole('tab', { name }).click();
    expect(await height()).toBe(empty);
  }
  expect(errors).toEqual([]);
});

for (const scheme of ['light', 'dark'] as const) {
  test(`phone width, ${scheme}: no horizontal scroll, text contrast at least 4.5:1`, async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 390, height: 844 });
    await logIn(page);
    await page.getByRole('textbox', { name: 'Systolic' }).fill('0');
    await cuffSide(page).getByRole('button', { name: 'Take reading' }).click();
    await expect(page.locator('#cuff-systolic-error')).toBeVisible();
    await page.getByRole('textbox', { name: 'Systolic' }).fill('120');
    await cuffSide(page).getByRole('button', { name: 'Take reading' }).click();
    await expect(rows(page)).toHaveCount(1);
    await page.getByLabel('Send malformed').check();
    await trace(page).getByRole('tab', { name: 'ACK as received' }).click();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);

    const ratios = await page.evaluate(() => {
      const rgb = (c: string) => (c.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number);
      const lum = (v: number[]) => {
        const [r = 0, g = 0, b = 0] = v.map((x) => {
          const s = x / 255;
          return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      /** The first opaque background at or above the element. */
      const background = (el: Element): number[] => {
        for (let n: Element | null = el; n; n = n.parentElement) {
          const c = getComputedStyle(n).backgroundColor;
          if (c && !c.startsWith('rgba(0, 0, 0, 0)') && c !== 'transparent') return rgb(c);
        }
        return [255, 255, 255];
      };
      const selectors = [
        '.cuff-reading__label',
        '.cuff-reading__detail',
        '.cuff-reading__term',
        '.cuff-hint',
        '.cuff-field__error',
        '.cuff-field__note',
        '.cuff-choice__help',
        '.cuff-legend',
        '.cuff-form-message',
        '.cuff-reading__selected',
        '.cuff-trace__note',
        '.cuff-facts dt',
        '.cuff-trace__body .code-block code',
        '.cuff-field__unit',
      ];
      const out: Record<string, number> = {};
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (!el) {
          out[sel] = -1;
          continue;
        }
        const [x, y] = [lum(rgb(getComputedStyle(el).color)), lum(background(el))];
        out[sel] = (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
      }
      return out;
    });
    for (const [sel, ratio] of Object.entries(ratios)) {
      expect(ratio, sel).toBeGreaterThanOrEqual(4.5);
    }
    expect(errors).toEqual([]);
  });
}
