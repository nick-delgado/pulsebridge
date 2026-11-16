import { expect, test, type Page } from '../../e2e/clock';

/**
 * Browser tests for Share with clinic, against the BUILT mock page (synthetic data only), under
 * the page's Content-Security-Policy. The mock walks the share sequence specified for T15.
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

async function logIn(page: Page, query = ''): Promise<void> {
  await page.goto(`/${query}`);
  await page.getByLabel('Password').fill('any password works in mock mode');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('main', { name: 'PulseBridge dashboard' })).toBeVisible();
}

const shareButton = (page: Page) => page.getByRole('button', { name: 'Share with clinic' });
const steps = (page: Page) => page.locator('.share-steps > li');

/** Selects a mapped blood-pressure reading from the messages log (an attempt with an Observation). */
async function selectMappedReading(page: Page): Promise<void> {
  await page.getByRole('button', { name: /^Messages log/ }).click();
  const row = page
    .locator('#shell-messages-log .log-row--attempt')
    .filter({ hasText: 'Accepted' })
    .filter({ hasText: 'Observation/' })
    .first();
  await row.getByRole('button', { name: /Select/ }).click();
  await page.getByRole('button', { name: /^Messages log/ }).click();
  await expect(shareButton(page)).toBeEnabled();
}

test('keyboard only: confirm says what is sent and left out, then the trace and the read-back', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await selectMappedReading(page);
  // A closed confirm takes no space (its CSS display must not beat the hidden attribute).
  await expect(page.locator('#share-confirm')).toBeHidden();
  await shareButton(page).focus();
  await page.keyboard.press('Enter');
  const confirm = page.getByRole('group', { name: 'Send this reading to the clinic?' });
  await expect(confirm).toBeVisible();
  await expect(page.locator('#share-confirm-heading')).toBeFocused();
  await expect(confirm).toContainText('official name, gender and birth date');
  await expect(confirm).toContainText(
    'Left out: the device (the watch or the cuff), the performer',
  );
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(page.locator('#share-send')).toBeFocused();
  await page.keyboard.press('Enter');

  await expect(page.locator('#share-status')).toContainText('Shared: OpenEMR stored the reading.');
  await expect(steps(page)).toHaveCount(9);
  await expect(steps(page).nth(0)).toContainText('Token issued');
  await expect(steps(page).nth(1)).toContainText('No Patient yet: it is created next');
  await expect(steps(page).nth(5)).toContainText('POST /apis/default/fhir/Observation → 201');
  await expect(page.locator('#share-trace-summary')).toBeFocused();
  const readback = page.getByRole('region', { name: 'As stored in OpenEMR' });
  await expect(readback).toContainText('How OpenEMR’s copy differs from what was sent');
  await expect(readback).toContainText('was added by OpenEMR');
  // The credentials never reach the page.
  await expect(page.locator('body')).not.toContainText('mock-assertion');
  await expect(page.locator('body')).not.toContainText('mock-access-token');
  expect(errors).toEqual([]);
});

test('sharing the same reading again: no new Patient, Encounter reused, reported as already shared', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').click();
  await expect(page.locator('#share-status')).toContainText('Shared:');
  await expect(page.locator('#share-hint')).toContainText('in this session');
  await shareButton(page).click();
  await expect(page.locator('#share-confirm')).toContainText('You already shared this reading at');
  await page.locator('#share-send').click();
  await expect(page.locator('#share-status')).toContainText(
    'Already shared: OpenEMR already held this reading, so nothing new was written.',
  );
  await expect(page.locator('#share-status')).toContainText('The Patient already existed there');
  await expect(steps(page)).toHaveCount(7);
  await expect(steps(page).nth(1)).toContainText('Patient already existed: not created again');
  await expect(steps(page).nth(2)).toContainText('Encounter found: reused');
  await expect(steps(page).nth(3)).toContainText('refused as a repeat, nothing new written');
  // The first share's token is still valid: reused, and no "no answer" line for it (T15).
  await expect(steps(page).nth(0)).toContainText(
    'Token reused from the server’s memory: no request sent',
  );
  await expect(steps(page).nth(0)).toContainText('not sent (token reused)');
  await expect(steps(page).nth(0)).not.toContainText('no answer');
  expect(errors).toEqual([]);
});

test('double click on "Send to the clinic" sends once', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').dblclick();
  await expect(page.locator('#share-status')).toContainText('Shared:');
  // One share: the first and only one creates the Patient.
  await expect(steps(page).nth(1)).toContainText('No Patient yet');
  await shareButton(page).click();
  await page.locator('#share-send').click();
  await expect(page.locator('#share-status')).toContainText('Already shared');
  expect(errors).toEqual([]);
});

test('?mock-share=observation-timeout: the steps so far, and that it is not known whether it was stored', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-share=observation-timeout');
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').click();
  const alert = page.locator('#share-alert');
  await expect(alert).toContainText('The share stopped at step 6 (Send the reading)');
  await expect(alert).toContainText('It is not known whether OpenEMR stored the reading');
  await expect(steps(page)).toHaveCount(6);
  await expect(steps(page).last()).toContainText('Failed: no answer');
  await expect(page.getByRole('region', { name: 'As stored in OpenEMR' })).toContainText(
    'Nothing to read back',
  );
  expect(errors).toEqual([]);
});

test("?mock-share=hosting-timeout: the hosting layer's HTML 504 never claims nothing was sent", async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-share=hosting-timeout');
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').click();
  const alert = page.locator('#share-alert');
  await expect(alert).toContainText(
    'The server answered HTTP 504 without saying what happened, so it is not known what reached the clinic',
  );
  await expect(alert).toContainText('Sharing again is safe');
  await expect(alert).not.toContainText('Nothing was sent');
  await expect(page.getByRole('region', { name: 'Share trace' })).toContainText(
    'The server sent no trace, so it is not known which requests reached OpenEMR.',
  );
  expect(errors).toEqual([]);
});

test('?mock-share=patient-incomplete: refused before OpenEMR, nothing sent', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-share=patient-incomplete');
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').click();
  await expect(page.locator('#share-alert')).toContainText('missing birthDate');
  await expect(page.locator('#share-alert')).toContainText('Nothing was sent to the clinic.');
  await expect(page.locator('#share-trace-summary')).toBeFocused();
  expect(errors).toEqual([]);
});

for (const scheme of ['light', 'dark'] as const) {
  test(`phone width, ${scheme}: share trace and read-back fit, contrast at least 4.5:1`, async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 390, height: 844 });
    await logIn(page);
    await selectMappedReading(page);
    await shareButton(page).click();
    await expect(page.locator('#share-confirm')).toBeVisible();
    const ratios = async (selectors: string[]) =>
      page.evaluate((sels) => {
        const rgb = (c: string) => (c.match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number);
        const lum = (v: number[]) => {
          const [r = 0, g = 0, b = 0] = v.map((x) => {
            const s = x / 255;
            return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
          });
          return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        };
        const background = (el: Element): number[] => {
          for (let n: Element | null = el; n; n = n.parentElement) {
            const c = getComputedStyle(n).backgroundColor;
            if (c && !c.startsWith('rgba(0, 0, 0, 0)') && c !== 'transparent') return rgb(c);
          }
          return [255, 255, 255];
        };
        const out: Record<string, number> = {};
        for (const sel of sels) {
          const el = document.querySelector(sel);
          if (!el) {
            out[sel] = -1;
            continue;
          }
          const [x, y] = [lum(rgb(getComputedStyle(el).color)), lum(background(el))];
          out[sel] = (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
        }
        return out;
      }, selectors);
    const confirmRatios = await ratios([
      '.share-confirm__heading',
      '.share-confirm__list li',
      '.share-confirm__left',
    ]);
    await page.locator('#share-send').click();
    await expect(page.locator('#share-status')).toContainText('Shared:');
    const after = await ratios([
      '.share-status',
      '.share-step__title',
      '.share-step__result',
      '.share-mono',
      '.share-hint',
      '.share-diff li',
      '.share-readback__heading',
    ]);
    for (const [sel, ratio] of Object.entries({ ...confirmRatios, ...after })) {
      expect(ratio, sel).toBeGreaterThanOrEqual(4.5);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    expect(errors).toEqual([]);
  });
}

test('logout while sharing: the late answer does not reach the next session', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await selectMappedReading(page);
  await shareButton(page).click();
  await page.locator('#share-send').click();
  await page.getByRole('button', { name: 'Log out' }).click();
  await logIn(page);
  // Give the old answer time to arrive; it must not render into the new dashboard.
  await page.waitForTimeout(1500);
  await expect(steps(page)).toHaveCount(0);
  await expect(page.locator('#share-status')).toHaveText('');
  expect(errors).toEqual([]);
});
