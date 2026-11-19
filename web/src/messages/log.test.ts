import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  CallOptions,
  MessagesLogEntry,
  MessagesLogPage,
  MessagesLogQuery,
  PulseBridgeApi,
} from '../api/contract';
import { ApiError, UnauthorizedError } from '../api/errors';
import { deferred, type Deferred } from '../cuff/test-support';
import { createDataChangeBus } from '../state/data-changed';
import { createSelectionStore } from '../state/selection';
import { createMessagesLog } from './log';
import { attempt, noAckRow, rejected, storeOnly, transaction, unreadable } from './test-support';

const HOSTILE = `<img src="x" onerror="alert(1)"><script>alert(2)</script>${'W'.repeat(600)}`;

function fakeClient() {
  const calls: {
    query: MessagesLogQuery;
    signal: AbortSignal;
    answer: Deferred<MessagesLogPage>;
  }[] = [];
  const client = {
    mode: 'mock',
    listMessages(query: MessagesLogQuery = {}, options: CallOptions = {}) {
      if (!options.signal) throw new Error('listMessages without a signal');
      const answer = deferred<MessagesLogPage>();
      calls.push({ query, signal: options.signal, answer });
      return answer.promise;
    },
  } as unknown as PulseBridgeApi;
  return { client, calls };
}

async function flush() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
}

const page = (entries: MessagesLogEntry[], nextCursor: string | null = null, partial = false) => ({
  entries,
  nextCursor,
  unreadable: entries.filter((e) => e.kind === 'unreadable-send-attempt').length,
  partial,
});

let teardown: AbortController | null = null;
afterEach(() => {
  teardown?.abort();
  vi.useRealTimers();
  document.body.replaceChildren();
});

function mount(options: { maxAutoPages?: number; pageRows?: number } = {}) {
  const api = fakeClient();
  const selection = createSelectionStore();
  const bus = createDataChangeBus();
  const controller = new AbortController();
  teardown = controller;
  const slots = { log: document.createElement('div'), summary: document.createElement('span') };
  document.body.append(slots.log, slots.summary);
  createMessagesLog(
    {
      client: api.client,
      selection,
      slots,
      signal: controller.signal,
      refreshStatus: () => undefined,
      notifyDataChanged: (kind) => {
        bus.notify(kind);
      },
      onDataChanged: (listener, signal) => {
        bus.subscribe(listener, signal);
      },
    },
    { timeZone: 'UTC', poll: { isVisible: () => true }, ...options },
  );
  const answer = async (p: MessagesLogPage, i = api.calls.length - 1) => {
    api.calls[i]?.answer.resolve(p);
    await flush();
  };
  const rows = () => [...slots.log.querySelectorAll<HTMLLIElement>('.log-row')];
  const q = (sel: string): HTMLElement => {
    const found = slots.log.querySelector<HTMLElement>(sel);
    if (!found) throw new Error(`nothing matches ${sel}`);
    return found;
  };
  const select = (id: string, value: string) => {
    const s = q(`#${id}`) as HTMLSelectElement;
    s.value = value;
    s.dispatchEvent(new Event('change', { bubbles: true }));
  };
  return { ...api, selection, bus, controller, slots, answer, rows, q, select };
}

const ALL: MessagesLogEntry[] = [
  attempt(),
  attempt({
    id: 'att-pend',
    sentAt: '2026-03-10T14:16:00.000Z',
    application: { outcome: 'pending', reason: null },
    observation: null,
  }),
  rejected({ sentAt: '2026-03-10T14:15:30.000Z' }),
  attempt({
    id: 'att-nf',
    sentAt: '2026-03-10T14:14:58.000Z',
    messageLookup: 'not-found',
    application: null,
    observation: null,
  }),
  storeOnly({ possibleDuplicateOf: 'att-nf' }),
  storeOnly({
    id: `x/messages/direct`,
    messageName: 'x/messages/direct',
    controlId: 'ADAPTER-1',
    recordedAt: '2026-03-10T14:10:00.000Z',
  }),
  noAckRow({ sentAt: '2026-03-10T14:05:00.000Z' }),
  transaction(),
  unreadable({ id: '', recordedAt: null }),
];

describe('rendering every row kind', () => {
  it('lists each kind with its words, and the summary counts', async () => {
    const m = mount();
    expect(m.calls[0]?.query).toEqual({ count: 25 });
    expect(m.slots.summary.textContent).toBe('loading');
    await m.answer(page(ALL));
    const rows = m.rows();
    expect(rows).toHaveLength(ALL.length);
    expect(rows.map((r) => r.querySelector('.log-row__title')?.textContent)).toEqual([
      'Cuff send',
      'Cuff send',
      'Cuff send',
      'Cuff send',
      'Stored message, no send record',
      'Stored message, no send record',
      'Cuff send',
      'FHIR transaction',
      'Unreadable send record',
    ]);
    expect(rows[1]?.textContent).toContain('Pending');
    expect(rows[2]?.textContent).toContain('AE, rejected');
    expect(rows[3]?.textContent).toContain('Not found');
    expect(rows[6]?.textContent).toContain('No acknowledgment');
    expect(rows[8]?.textContent).toContain('Time unknown');
    expect(m.slots.summary.textContent).toBe('9 rows loaded · 1 pending · 4 with a problem');
    // The acknowledgment-mode explanation, as on the cuff side.
    expect(m.slots.log.textContent).toContain(
      "HL7's original acknowledgment mode (AA or AE in MSA-1)",
    );
  });

  it('store-only rows cannot be selected; attempt rows can; transactions expand', async () => {
    const m = mount();
    await m.answer(page(ALL));
    const [first, , , , so] = m.rows();
    expect(first?.querySelector('.log-row__select')).not.toBeNull();
    expect(so?.querySelector('button')).toBeNull();
    expect(so?.textContent).toContain('cannot be selected');
    const tx = m.rows()[7];
    const expand = tx?.querySelector<HTMLButtonElement>('.log-row__expand');
    expect(expand?.getAttribute('aria-expanded')).toBe('false');
    expand?.click();
    expect(expand?.getAttribute('aria-expanded')).toBe('true');
    expect(tx?.querySelector('.log-row__wrote')?.textContent).toContain('Provenance/p-2');
  });

  it('the possibleDuplicateOf hint links to the attempt row, worded as a hint', async () => {
    const m = mount();
    await m.answer(page(ALL));
    const hinted = m.rows()[4];
    const link = hinted?.querySelector<HTMLAnchorElement>('a.log-link');
    expect(hinted?.textContent).toContain('Hint, not a confirmed link');
    expect(link?.getAttribute('href')).toBe(`#${m.rows()[3]?.id ?? 'none'}`);
    // The hinted-at attempt keeps no outcome (the hint moves nothing).
    expect(m.rows()[3]?.textContent).toContain('Not found');
  });

  it('a hint whose attempt is not loaded says so, without a link', async () => {
    const m = mount();
    await m.answer(page([storeOnly({ possibleDuplicateOf: 'att-elsewhere' })]));
    expect(m.rows()[0]?.querySelector('a')).toBeNull();
    expect(m.rows()[0]?.textContent).toContain('att-elsewhere (not among the rows loaded)');
  });

  it('every string from the server is text: no element is created from it', async () => {
    const m = mount();
    await m.answer(
      page([
        rejected({
          controlId: HOSTILE,
          commitAck: { received: true, code: 'AE', controlId: null, reason: HOSTILE, raw: HOSTILE },
        }),
        unreadable({ problem: HOSTILE }),
        transaction({ resources: [HOSTILE] }),
      ]),
    );
    expect(m.slots.log.querySelector('img, script')).toBeNull();
    expect(m.slots.log.textContent).toContain('<img src="x" onerror="alert(1)">');
  });

  it('a transaction row with no resources says the list is missing, not that nothing was written', async () => {
    const m = mount();
    await m.answer(page([transaction({ resources: [] }), transaction({ id: 'tx-2' })]));
    const [empty, full] = m.rows();
    expect(empty?.textContent).toContain('None listed');
    expect(empty?.textContent).toContain('the references are missing here');
    empty?.querySelector<HTMLButtonElement>('.log-row__expand')?.click();
    await flush();
    expect(empty?.textContent).not.toContain('as references in the FHIR store');
    expect(empty?.querySelector('.log-resources')).toBeNull();
    // A row with resources lists them, with no such note.
    full?.querySelector<HTMLButtonElement>('.log-row__expand')?.click();
    await flush();
    expect(full?.textContent).not.toContain('the references are missing here');
    expect(full?.querySelectorAll('.log-resources li')).toHaveLength(4);
  });

  it('ids are generated with the log- prefix (never a server id) and unique; ARIA references resolve', async () => {
    const m = mount();
    await m.answer(page(ALL));
    m.rows()[7]?.querySelector<HTMLButtonElement>('.log-row__expand')?.click();
    const ids = [...m.slots.log.querySelectorAll('[id]')].map((e) => e.id);
    expect(ids.every((id) => id.startsWith('log-'))).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
    for (const el of m.slots.log.querySelectorAll(
      '[aria-labelledby], [aria-describedby], [aria-controls]',
    )) {
      for (const attr of ['aria-labelledby', 'aria-describedby', 'aria-controls']) {
        for (const ref of (el.getAttribute(attr) ?? '').split(/\s+/).filter(Boolean)) {
          expect(document.getElementById(ref), `${attr}=${ref}`).not.toBeNull();
        }
      }
    }
  });
});

describe('empty, loading and error states', () => {
  it('empty log', async () => {
    const m = mount();
    await m.answer(page([]));
    expect(m.slots.log.textContent).toContain('The log is empty');
    expect(m.q('#log-more').hidden).toBe(true);
  });

  it('an error offers "Try again", which loads again', async () => {
    const m = mount();
    m.calls[0]?.answer.reject(new ApiError(502, 'The stores refused the query.'));
    await flush();
    expect(m.q('#log-alert').textContent).toContain(
      'Could not load the messages log: The stores refused the query.',
    );
    m.q('#log-alert').querySelector('button')?.click();
    expect(m.calls).toHaveLength(2);
    await m.answer(page([attempt()]));
    expect(m.rows()).toHaveLength(1);
    expect(m.q('#log-alert').textContent).toBe('');
  });

  it('503 session-not-configured: "the service is not configured", the session is kept', async () => {
    const m = mount();
    m.calls[0]?.answer.reject(
      new ApiError(503, 'x', {
        resourceType: 'OperationOutcome',
        issue: [
          {
            severity: 'error',
            code: 'transient',
            details: {
              coding: [
                { system: 'urn:pulsebridge:fhir-proxy:reason', code: 'session-not-configured' },
              ],
            },
          },
        ],
      }),
    );
    await flush();
    expect(m.q('#log-alert').textContent).toContain('The service is not configured');
    expect(m.q('#log-alert').textContent).toContain('Your demo session is kept');
  });

  it('a 401 is left to the shell', async () => {
    const m = mount();
    m.calls[0]?.answer.reject(new UnauthorizedError());
    await flush();
    expect(m.q('#log-alert').textContent).toBe('');
  });
});

describe('paging (amendments 3, 4, 16)', () => {
  it('follows short and empty pages by itself, bounded, then offers "Load more"', async () => {
    const m = mount({ maxAutoPages: 3 });
    await m.answer(page([], 'c1'));
    expect(m.calls[1]?.query).toEqual({ count: 25, cursor: 'c1' });
    await m.answer(page([attempt()], 'c2'));
    await m.answer(page([], 'c3'));
    // Bound reached: no fourth request on its own.
    expect(m.calls).toHaveLength(3);
    expect(m.rows()).toHaveLength(1);
    const more = m.q('#log-more') as HTMLButtonElement;
    expect(more.hidden).toBe(false);
    expect(m.q('#log-more-note').textContent).toBe(
      'Searched 3 pages and found 1 matching row; there may be more.',
    );
    more.click();
    expect(m.calls[3]?.query).toEqual({ count: 25, cursor: 'c3' });
    await m.answer(page([rejected()], null));
    expect(m.rows()).toHaveLength(2);
    expect(more.hidden).toBe(true);
  });

  it('nothing found in the pages searched: says so, not "empty"', async () => {
    const m = mount({ maxAutoPages: 2 });
    await m.answer(page([], 'c1'));
    await m.answer(page([], 'c2'));
    expect(m.slots.log.textContent).toContain('No matching rows in the pages searched so far.');
  });

  it('a partial page says the list may be incomplete and offers a reload', async () => {
    const m = mount();
    await m.answer(page([attempt()], null, true));
    const note = m.q('#log-partial');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain('This list may be incomplete');
    note.querySelector('button')?.click();
    expect(m.calls).toHaveLength(2);
    expect(m.calls[1]?.query).toEqual({ count: 25 });
    await m.answer(page([attempt()], null, false));
    expect(note.hidden).toBe(true);
  });
});

describe('filters (amendment 19)', () => {
  it('send kind and outcome, reload from the start, and explain the semantics', async () => {
    const m = mount();
    await m.answer(page(ALL));
    m.select('log-filter-outcome', 'pending');
    expect(m.calls.at(-1)?.query).toEqual({ outcome: 'pending', count: 25 });
    expect(m.q('#log-filter-note').textContent).toContain(
      'Rows with no outcome match no outcome filter',
    );
    m.select('log-filter-kind', 'fhir-transaction');
    expect(m.calls.at(-1)?.query).toEqual({
      kind: 'fhir-transaction',
      outcome: 'pending',
      count: 25,
    });
    expect(m.q('#log-filter-note').textContent).toContain('no row can match both filters');
  });

  it('an answer for the previous filter, arriving late, is ignored', async () => {
    const m = mount();
    m.select('log-filter-outcome', 'error');
    expect(m.calls[0]?.signal.aborted).toBe(true);
    await m.answer(page([rejected()]), 1);
    // The first request answers after the filter changed: dropped.
    m.calls[0]?.answer.resolve(page(ALL));
    await flush();
    expect(m.rows()).toHaveLength(1);
  });
});

describe('refreshing (data changes, polling) without losing focus', () => {
  it('a data change refreshes; unchanged rows keep their DOM and the focused button keeps focus', async () => {
    const m = mount();
    await m.answer(page([attempt(), rejected({ sentAt: '2026-03-10T14:00:00.000Z' })]));
    const before = m.rows();
    const button = before[0]?.querySelector<HTMLButtonElement>('.log-row__select');
    button?.focus();
    expect(document.activeElement).toBe(button);
    const firstBody = before[0]?.querySelector('.log-row__body')?.firstChild;
    m.bus.notify('cuff-send');
    expect(m.calls).toHaveLength(2);
    await m.answer(
      page([
        attempt({ id: 'att-new', sentAt: '2026-03-10T14:20:00.000Z' }),
        attempt(),
        rejected({ sentAt: '2026-03-10T14:00:00.000Z' }),
      ]),
    );
    const after = m.rows();
    expect(after).toHaveLength(3);
    expect(after[1]).toBe(before[0]);
    expect(after[1]?.querySelector('.log-row__body')?.firstChild).toBe(firstBody);
    expect(document.activeElement).toBe(button);
  });

  it('a row that changed is rebuilt in place, its button kept', async () => {
    const m = mount();
    const pend = attempt({ application: { outcome: 'pending', reason: null }, observation: null });
    await m.answer(page([pend]));
    const row = m.rows()[0];
    const button = row?.querySelector('.log-row__select');
    m.bus.notify('sync');
    await m.answer(page([attempt()]));
    expect(m.rows()[0]).toBe(row);
    expect(row?.querySelector('.log-row__select')).toBe(button);
    expect(row?.textContent).toContain('Accepted');
  });

  it('a data change during the FIRST load is queued and runs once that load ends', async () => {
    const m = mount();
    m.bus.notify('share');
    m.bus.notify('cuff-send');
    expect(m.calls).toHaveLength(1);
    await m.answer(page([attempt()]));
    // One refresh for both changes, after the first load.
    expect(m.calls).toHaveLength(2);
    await m.answer(
      page([attempt({ id: 'att-new', sentAt: '2026-03-10T14:20:00.000Z' }), attempt()]),
    );
    expect(m.rows()).toHaveLength(2);
    expect(m.calls).toHaveLength(2);
  });

  it('a refresh asked for while a later request runs waits for it, then runs once', async () => {
    const m = mount();
    await m.answer(page([attempt()]));
    m.bus.notify('share');
    m.bus.notify('cuff-send');
    expect(m.calls).toHaveLength(2);
    await m.answer(page([attempt()]));
    expect(m.calls).toHaveLength(3);
  });

  it('a filtered refresh that finds nothing but has a cursor keeps the rows shown and their cursor', async () => {
    const m = mount({ maxAutoPages: 1 });
    m.select('log-filter-outcome', 'error');
    await m.answer(
      page(
        [rejected(), attempt({ id: 'e2', application: { outcome: 'error', reason: 'x' } })],
        'old-cursor',
      ),
      1,
    );
    expect(m.rows()).toHaveLength(2);
    m.bus.notify('sync');
    // An empty page with a cursor: the bound (one page here) is reached with nothing found.
    await m.answer(page([], 'c0'));
    expect(m.rows()).toHaveLength(2);
    (m.q('#log-more') as HTMLButtonElement).click();
    expect(m.calls.at(-1)?.query).toMatchObject({ cursor: 'old-cursor' });
  });

  it('a later page failing keeps the rows gathered, says so, and "Try again" continues from it', async () => {
    const m = mount();
    await m.answer(page([], 'c1'));
    await m.answer(page([attempt()], 'c2'));
    m.calls[2]?.answer.reject(new ApiError(504, 'The stores did not answer in time.'));
    await flush();
    expect(m.rows()).toHaveLength(1);
    expect(m.q('#log-alert').textContent).toContain('Loading stopped early');
    expect(m.q('#log-alert').textContent).toContain('The stores did not answer in time.');
    m.q('#log-alert').querySelector('button')?.click();
    expect(m.calls[3]?.query).toEqual({ count: 25, cursor: 'c2' });
    await m.answer(page([rejected()], null));
    expect(m.rows()).toHaveLength(2);
    expect(m.q('#log-alert').textContent).toBe('');
  });

  it('polls while a row is pending, stops when it settles', async () => {
    vi.useFakeTimers();
    const m = mount();
    await m.answer(
      page([attempt({ application: { outcome: 'pending', reason: null }, observation: null })]),
    );
    await vi.advanceTimersByTimeAsync(3000);
    expect(m.calls).toHaveLength(2);
    await m.answer(page([attempt()]));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(m.calls).toHaveLength(2);
  });

  it('polling is bounded and then offers "Check again"', async () => {
    vi.useFakeTimers();
    const m = mount();
    const pending = page([
      attempt({ application: { outcome: 'pending', reason: null }, observation: null }),
    ]);
    await m.answer(pending);
    for (let i = 0; i < 45; i++) {
      await vi.advanceTimersByTimeAsync(3000);
      for (const c of m.calls) c.answer.resolve(pending);
      await flush();
    }
    const n = m.calls.length;
    expect(n).toBeGreaterThan(30);
    expect(n).toBeLessThanOrEqual(42);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(m.calls).toHaveLength(n);
    expect(m.slots.log.textContent).toContain('still pending after 2 minutes');
    // A data change with the same pending row does not start polling again.
    m.bus.notify('sync');
    await m.answer(pending);
    await vi.advanceTimersByTimeAsync(9000);
    expect(m.calls).toHaveLength(n + 1);
  });

  it('logout: requests are aborted and late answers change nothing', async () => {
    const m = mount();
    m.controller.abort();
    expect(m.calls[0]?.signal.aborted).toBe(true);
    await m.answer(page(ALL));
    expect(m.rows()).toHaveLength(0);
  });
});

describe('selection wiring', () => {
  it('"Select" sets the shared cuff selection, and the row shows it is selected', async () => {
    const m = mount();
    await m.answer(page(ALL));
    const row = m.rows()[0];
    row?.querySelector<HTMLButtonElement>('.log-row__select')?.click();
    expect(m.selection.get()).toEqual({
      source: 'cuff',
      attemptId: 'att-1',
      observationId: 'bp-1',
      summary: 'Cuff send at 10 Mar, 14:17:00, MLLP (from the messages log)',
      unshareableReason: null,
    });
    expect(row?.querySelector('.log-row__select')?.getAttribute('aria-pressed')).toBe('true');
    expect(row?.textContent).toContain('Selected');
    // A not-found row is selected as not shareable, with the reason.
    m.rows()[3]?.querySelector<HTMLButtonElement>('.log-row__select')?.click();
    expect(m.selection.get()).toMatchObject({ attemptId: 'att-nf', observationId: null });
    expect(m.selection.get()?.unshareableReason).toContain('no message under');
    expect(row?.querySelector('.log-row__select')?.getAttribute('aria-pressed')).toBe('false');
  });

  it('the selection moving elsewhere drops the highlight', async () => {
    const m = mount();
    await m.answer(page(ALL));
    m.rows()[0]?.querySelector<HTMLButtonElement>('.log-row__select')?.click();
    m.selection.set({
      source: 'watch',
      observationId: 'hr-1',
      summary: 'HR',
      unshareableReason: null,
    });
    expect(m.rows()[0]?.classList.contains('log-row--selected')).toBe(false);
  });
});
