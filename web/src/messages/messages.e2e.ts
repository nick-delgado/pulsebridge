import { CHARGING_GAP_CLOCK, expect, test, type Page } from '../../e2e/clock';

/**
 * Browser tests for the messages log, against the BUILT mock page (synthetic data only), under
 * the page's Content-Security-Policy. Each test fails on any console error, uncaught exception
 * or CSP violation.
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

const toggle = (page: Page) => page.getByRole('button', { name: /^Messages log/ });
const logRows = (page: Page) => page.locator('#shell-messages-log .log-row');

async function openLog(page: Page): Promise<void> {
  await toggle(page).click();
  await expect(toggle(page)).toHaveAttribute('aria-expanded', 'true');
  await expect(logRows(page).first()).toBeVisible();
}

test('every row kind, both acknowledgment levels in words, and the summary in the toggle', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await expect(toggle(page)).toContainText(/\d+ rows? loaded/);
  await openLog(page);
  // A note that no longer applies keeps its text but is hidden: `hidden` must win over the
  // note's CSS display (a partial page that a reload filled in leaves its text behind).
  await page.evaluate(() => {
    const note = document.getElementById('log-partial');
    if (note) note.textContent = 'left over from an earlier partial page';
  });
  await expect(page.locator('#log-partial')).toBeHidden();
  const log = page.locator('#shell-messages-log');
  for (const words of [
    'Cuff send',
    'Stored message, no send record',
    'FHIR transaction',
    'AE, rejected',
    'No acknowledgment',
    'Pending',
    'Not found',
    'Hint, not a confirmed link',
    "HL7's original acknowledgment mode",
  ]) {
    await expect(log, words).toContainText(words);
  }
  // Load everything; the unreadable rows (one with no id) come last.
  const more = page.locator('#log-more');
  for (let i = 0; i < 10 && (await more.isVisible()); i++) {
    const count = await logRows(page).count();
    await more.click();
    await expect.poll(() => logRows(page).count()).toBeGreaterThan(count);
  }
  await expect(more).toBeHidden();
  await expect(logRows(page).last()).toContainText('Unreadable send record');
  await expect(logRows(page).last()).toContainText('Time unknown');
  expect(errors).toEqual([]);
});

test('a log row selects a cuff attempt sent in this session, and the cuff trace loads', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  const cuff = page.getByRole('region', { name: 'Blood pressure' });
  await cuff.getByRole('button', { name: 'Take reading' }).click();
  await expect(cuff.locator('.cuff-reading')).toHaveCount(1);
  await openLog(page);
  // The log row of this send: the same instant as the cuff row (its <time datetime>). Not the
  // clock text: in the hour after a clock goes back a seeded send an hour earlier shows the
  // same wall time (T37).
  const sentAt = await cuff.locator('.cuff-reading time').first().getAttribute('datetime');
  const newest = logRows(page)
    .filter({ has: page.locator(`time[datetime="${sentAt ?? 'none'}"]`) })
    .filter({ hasText: 'Cuff send' });
  await expect(newest).toHaveCount(1);
  await newest.getByRole('button', { name: /Select/ }).click();
  await expect(newest.getByRole('button', { name: /Select/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  const trace = page.getByRole('region', { name: 'Blood-pressure trace' });
  await expect(trace.getByRole('tab', { name: 'HL7v2 as sent' })).toBeVisible();
  await expect(cuff.locator('.cuff-reading__show')).toHaveAttribute('aria-pressed', 'true');
  expect(errors).toEqual([]);
});

test('a log row from another session fills the cuff trace from the proxy', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await openLog(page);
  // A seeded send: not sent in this session, so the cuff panel has no row for it.
  const row = logRows(page)
    .filter({ hasText: 'Accepted' })
    .filter({ hasText: 'Observation/' })
    .first();
  await row.getByRole('button', { name: /Select/ }).click();
  const cuff = page.getByRole('region', { name: 'Blood pressure' });
  await expect(cuff.locator('.cuff-reading')).toHaveCount(0);
  const trace = page.getByRole('region', { name: 'Blood-pressure trace' });
  await trace.getByRole('tab', { name: 'HL7v2 as sent' }).click();
  await expect(trace.getByRole('tabpanel')).toContainText('ORU^R01');
  await trace.getByRole('tab', { name: 'ACK as received' }).click();
  await expect(trace.getByRole('tabpanel')).toContainText('Not kept in the send record');
  await trace.getByRole('tab', { name: 'FHIR as stored' }).click();
  await expect(trace.getByRole('tabpanel')).toContainText('"resourceType": "Observation"');
  await expect(page.locator('#share-hint')).toContainText('Selected: Cuff send at');
  expect(errors).toEqual([]);
});

test('focus and scroll survive a refresh that adds rows above', async ({ page }) => {
  const errors = await watchErrors(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await logIn(page);
  const cuff = page.getByRole('region', { name: 'Blood pressure' });
  await openLog(page);
  await cuff.getByRole('button', { name: 'Take reading' }).click();
  await expect(cuff.locator('.cuff-reading')).toHaveCount(1);
  const scroller = page.locator('#shell-messages-log');
  // Wait until the cuff-send refresh has put this send at the top (newest by send time).
  const sentAt = (await cuff.locator('.cuff-reading time').first().textContent()) ?? '';
  await expect(logRows(page).first()).toContainText(`, ${sentAt}`);
  await expect(logRows(page).first()).toContainText('Pending');
  const before = await logRows(page).count();
  // Scroll the log so some row is at the top, and focus that row's button.
  await scroller.evaluate((el) => {
    el.scrollTop = 600;
  });
  const anchorId = await page.evaluate(() => {
    const scroller = document.getElementById('shell-messages-log');
    if (!scroller) return '';
    const top = scroller.getBoundingClientRect().top;
    const rows = [...scroller.querySelectorAll<HTMLElement>('.log-row')];
    const row = rows.find((r) => r.getBoundingClientRect().top >= top && r.querySelector('button'));
    row?.querySelector('button')?.focus();
    return row?.id ?? '';
  });
  expect(anchorId).not.toBe('');
  const offset = () =>
    page.evaluate((id) => {
      const scroller = document.getElementById('shell-messages-log');
      const row = document.getElementById(id);
      return (row?.getBoundingClientRect().top ?? 0) - (scroller?.getBoundingClientRect().top ?? 0);
    }, anchorId);
  const offsetBefore = await offset();
  // The mock mapper labels the send after ~3 s; the log's polling (every 3 s) picks it up and
  // adds its v2-to-fhir transaction row at the top.
  await expect(logRows(page)).toHaveCount(before + 1, { timeout: 15_000 });
  await expect(logRows(page).nth(1)).toContainText('Accepted');
  expect(await page.evaluate(() => document.activeElement?.closest('.log-row')?.id)).toBe(anchorId);
  expect(Math.abs((await offset()) - offsetBefore)).toBeLessThanOrEqual(2);
  expect(errors).toEqual([]);
});

test('filters: an outcome filter follows short pages and lists only rows with that outcome', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await openLog(page);
  await page.getByLabel('Application-level outcome').selectOption('error');
  await expect(page.locator('#log-filter-note')).toContainText(
    'Rows with no outcome match no outcome filter',
  );
  await expect(logRows(page).first()).toBeVisible();
  const titles = await logRows(page).locator('.log-fact__label').allTextContents();
  expect(titles).toContain('Error');
  for (const row of await logRows(page).all()) {
    await expect(row.locator('.log-fact').nth(1)).toContainText('Error');
  }
  await page.getByLabel('Kind').selectOption('fhir-transaction');
  await expect(page.locator('#log-filter-note')).toContainText('no row can match both filters');
  await expect(page.locator('#shell-messages-log')).toContainText('No rows match these filters.');
  expect(errors).toEqual([]);
});

test('?mock-log=partial: the list says it may be incomplete and offers a reload', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-log=partial');
  await openLog(page);
  await expect(page.locator('#log-partial')).toContainText('This list may be incomplete');
  await expect(page.getByRole('button', { name: 'Reload the log' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('?mock-lookup=not-found: the cuff row stops at "not found"; the log hints at it from the stored message', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-lookup=not-found');
  const cuff = page.getByRole('region', { name: 'Blood pressure' });
  await cuff.getByRole('button', { name: 'Take reading' }).click();
  const row = cuff.locator('.cuff-reading').first();
  await expect(row).toContainText('Not found', { timeout: 10_000 });
  await expect(row).not.toContainText('Pending');
  await openLog(page);
  const hint = page.locator('.log-note--hint').first();
  await expect(hint).toContainText('Hint, not a confirmed link');
  const href = await hint.locator('a').getAttribute('href');
  expect(href).toMatch(/^#log-row-\d+$/);
  await hint.locator('a').click();
  await expect(page.locator(href ?? '#none')).toBeFocused();
  await expect(page.locator(href ?? '#none')).toContainText('Not found');
  expect(errors).toEqual([]);
});

test('hostile text in the log is text: no element, no CSP violation, no page scroll at phone width', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await logIn(page, '?mock-data=hostile');
  const cuff = page.getByRole('region', { name: 'Blood pressure' });
  await page.getByLabel('Send malformed').check();
  await cuff.getByText('unparseable_no_msh', { exact: true }).click();
  await cuff.getByRole('button', { name: 'Send malformed sample' }).click();
  await expect(cuff.locator('.cuff-reading')).toHaveCount(1);
  await openLog(page);
  const log = page.locator('#shell-messages-log');
  await expect(log).toContainText('PULSEBRIDGEHOSTILE');
  expect(await log.locator('img, script').count()).toBe(0);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  const logOverflow = await log.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(logOverflow).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

for (const scheme of ['light', 'dark'] as const) {
  test(`phone width, ${scheme}: the log has no horizontal scroll and text contrast is at least 4.5:1`, async ({
    page,
  }) => {
    const errors = await watchErrors(page);
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 390, height: 844 });
    await logIn(page);
    await openLog(page);
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
      const background = (el: Element): number[] => {
        for (let n: Element | null = el; n; n = n.parentElement) {
          const c = getComputedStyle(n).backgroundColor;
          if (c && !c.startsWith('rgba(0, 0, 0, 0)') && c !== 'transparent') return rgb(c);
        }
        return [255, 255, 255];
      };
      const out: Record<string, number> = {};
      for (const sel of [
        '.log-hint',
        '.log-row__time',
        '.log-row__title',
        '.log-fact__term',
        '.log-fact__label',
        '.log-fact__detail',
        '.log-filter__label',
        '.log-filter__select',
        '.log-summary',
        '.log-row__tag',
        '.log-note',
        '.log-link',
      ]) {
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

test('keyboard: filters, Select and "Show what it wrote" work without a mouse', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await openLog(page);
  await page.locator('#log-filter-kind').focus();
  await page.keyboard.press('Tab');
  await expect(page.locator('#log-filter-outcome')).toBeFocused();
  const tx = logRows(page).filter({ hasText: 'FHIR transaction' }).first();
  await tx.getByRole('button', { name: 'Show what it wrote' }).focus();
  await page.keyboard.press('Enter');
  await expect(tx.getByRole('button', { name: 'Hide what it wrote' })).toHaveAttribute(
    'aria-expanded',
    'true',
  );
  await expect(tx).toContainText('Provenance/');
  const attempt = logRows(page).filter({ hasText: 'Accepted' }).first();
  await attempt.getByRole('button', { name: /Select/ }).focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#share-hint')).toContainText('(from the messages log)');
  expect(errors).toEqual([]);
});

test('the status strip: not configured keeps the session; null counts and not-checked are worded', async ({
  page,
}) => {
  const errors = await watchErrors(page);
  await logIn(page, '?mock-status=counts-unknown');
  const strip = page.getByRole('region', { name: 'System status' });
  await expect(strip).toContainText('Observations: unknown watch · unknown cuff');
  await page.getByRole('button', { name: 'Log out' }).click();
  await logIn(page, '?mock-status=openemr-not-checked');
  await expect(strip).toContainText('OpenEMR: Not checked');
  await page.getByRole('button', { name: 'Log out' }).click();
  await logIn(page, '?mock-status=not-configured');
  await expect(strip).toContainText('Service: Not configured');
  // Still the dashboard: a 503 is not a 401.
  await expect(page.getByRole('main', { name: 'PulseBridge dashboard' })).toBeVisible();
  await openLog(page).catch(() => undefined);
  await expect(page.locator('#log-alert')).toContainText('The service is not configured');
  // 503s are console errors from no request (mock), so none are expected.
  expect(errors).toEqual([]);
});

test('ids on the whole page are unique, and every ARIA reference resolves', async ({ page }) => {
  const errors = await watchErrors(page);
  await logIn(page);
  await openLog(page);
  await logRows(page)
    .filter({ hasText: 'Accepted' })
    .first()
    .getByRole('button', { name: /Select/ })
    .click();
  await logRows(page)
    .filter({ hasText: 'FHIR transaction' })
    .first()
    .getByRole('button', { name: 'Show what it wrote' })
    .click();
  const problems = await page.evaluate(() => {
    const out: string[] = [];
    const ids = [...document.querySelectorAll('[id]')].map((e) => e.id);
    for (const id of ids) if (ids.indexOf(id) !== ids.lastIndexOf(id)) out.push(`duplicate ${id}`);
    for (const el of document.querySelectorAll(
      '[aria-labelledby],[aria-describedby],[aria-controls]',
    )) {
      for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-controls']) {
        for (const ref of (el.getAttribute(attr) ?? '').split(/\s+/).filter(Boolean)) {
          if (!document.getElementById(ref)) out.push(`${attr}=${ref}`);
        }
      }
    }
    for (const el of document.querySelectorAll('#shell-messages-log [id]')) {
      if (!el.id.startsWith('log-') && !el.id.startsWith('ui-')) out.push(`unprefixed ${el.id}`);
    }
    return out;
  });
  expect(problems).toEqual([]);
  expect(errors).toEqual([]);
});

test.describe('watch on the charger: a sync run that wrote nothing', () => {
  // 12:50 UTC (e2e/clock.ts): the 12:47 run found nothing new and nothing to finalise, so it
  // wrote no Bundle and no Provenance. The proxy builds transaction rows from Provenances, so
  // there is no row for it; the newest health-sync row is the 12:32 run, which finalised windows.
  // The log shows times in the browser's zone; this test names the runs in UTC, so it pins UTC.
  test.use({ clockAt: CHARGING_GAP_CLOCK, timezoneId: 'UTC' });

  test('no row for the empty run; every health-sync row lists what it wrote', async ({ page }) => {
    const errors = await watchErrors(page);
    await logIn(page);
    await openLog(page);
    await page.getByLabel('Kind').selectOption('fhir-transaction');
    const syncRows = logRows(page).filter({ hasText: 'health-sync' });
    await expect(syncRows.first()).toContainText('16 Nov, 12:32:00');
    await expect(logRows(page).filter({ hasText: '12:47:00' })).toHaveCount(0);
    await expect(page.locator('#shell-messages-log')).not.toContainText('None listed');
    const newest = syncRows.first();
    await expect(newest).toContainText('3 Observations, 3 Provenances');
    await newest.getByRole('button', { name: 'Show what it wrote' }).click();
    await expect(newest).toContainText(
      `Observation/mock-hr-${String(Date.parse('2026-11-16T11:55:00Z'))}`,
    );
    await expect(newest).toContainText('Provenance/');
    expect(errors).toEqual([]);
  });
});
