/**
 * The messages log (T19): `slots.log` holds the rows, the filters and the paging; `slots.summary`
 * (text inside the strip's toggle button) holds a one-line count. `mountMessages` (index.ts)
 * calls `createMessagesLog` with the real clock; the unit tests pass a fake client and fixed
 * options.
 *
 * Flow:
 *   mount ─▶ load(): gather() pages until a screenful (bounded) ─▶ render
 *   "Load more" ─▶ gather() from nextCursor ─▶ append
 *   onDataChanged('cuff-send' | 'sync' | 'share'), or a poll tick while a shown row is pending
 *     ─▶ refresh(): gather() from the start ─▶ mergeRefresh() ─▶ render (keyed: unchanged rows
 *        keep their DOM, so focus and scroll survive)
 *   a filter change ─▶ everything in flight is aborted ─▶ load() from the start
 *   "Select" on an attempt row ─▶ selection.set({source: 'cuff', attemptId, ...}): the cuff
 *     panel loads that attempt's trace when it sent it in this session, and the share area acts
 *     on it; "Show what it wrote" on a transaction row ─▶ its resources, inline
 *
 * One list request runs at a time. Each has its own AbortController chained to the dashboard's
 * signal and a generation number; an answer for an older generation (a filter changed, the
 * dashboard was torn down) is dropped. A refresh asked for while a request runs is run once it
 * ends.
 */
import type { MessagesLogEntry, MessagesLogQuery, SendAttemptEntry } from '../api/contract';
import { isServiceNotConfigured, UnauthorizedError } from '../api/errors';
import { ACK_MODE_NOTE } from '../cuff/readings';
import type { FeatureContext, MessagesSlot } from '../shell/features';
import { pollUntil, type PollOptions } from '../state/poll';
import { el, placeholder, replaceChildren, uniqueId } from '../ui/dom';
import { gather, mergeRefresh, MAX_AUTO_PAGES, PAGE_ROWS, uniqueRows, type Shown } from './paging';
import {
  attemptSummary,
  attemptUnshareableReason,
  isPendingRow,
  isProblemRow,
  LOG_SCOPE,
  logTime,
  NO_RESOURCES_NOTE,
  plural,
  resourceCount,
  rowFacts,
  rowKey,
  rowKind,
  rowTitle,
  sortTime,
  type Fact,
} from './rows';

export interface MessagesDeps {
  /** Zone for the times in the rows; the browser's own when undefined. */
  timeZone?: string;
  /** Overrides for the polling convention (tests only). */
  poll?: Partial<Pick<PollOptions, 'intervalMs' | 'maxMs' | 'now' | 'isVisible'>>;
  pageRows?: number;
  maxAutoPages?: number;
}

type KindFilter = NonNullable<MessagesLogQuery['kind']> | '';
type OutcomeFilter = NonNullable<MessagesLogQuery['outcome']> | '';

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

/** An AbortController that also aborts when `parent` does (logout tears the dashboard down). */
function childController(parent: AbortSignal): AbortController {
  const c = new AbortController();
  if (parent.aborted) c.abort();
  else
    parent.addEventListener(
      'abort',
      () => {
        c.abort();
      },
      { once: true, signal: c.signal },
    );
  return c;
}

function errorText(error: unknown): string {
  if (isServiceNotConfigured(error)) {
    return 'The service is not configured: the server is missing its session-signing key, so it answers no request. Your demo session is kept.';
  }
  return `Could not load the messages log: ${error instanceof Error ? error.message : 'unknown error'}`;
}

interface RowView {
  root: HTMLLIElement;
  body: HTMLDivElement;
  actions: HTMLDivElement;
  select: HTMLButtonElement | null;
  expand: HTMLButtonElement | null;
  wrote: HTMLDivElement | null;
  fingerprint: string;
}

export function createMessagesLog(
  context: FeatureContext<MessagesSlot>,
  deps: MessagesDeps = {},
): void {
  const { client, selection, slots, signal } = context;
  const pageRows = deps.pageRows ?? PAGE_ROWS;
  const maxAutoPages = deps.maxAutoPages ?? MAX_AUTO_PAGES;

  // -- static parts ----------------------------------------------------------------------------
  const kindSelect = el(
    'select',
    {
      class: 'log-filter__select',
      id: 'log-filter-kind',
      attrs: { 'aria-describedby': 'log-filter-note' },
    },
    el('option', { text: 'All rows', attrs: { value: '' } }),
    el('option', { text: 'Cuff sends and stored messages', attrs: { value: 'cuff-send' } }),
    el('option', { text: 'Unreadable send records', attrs: { value: 'unreadable-send-attempt' } }),
    el('option', { text: 'FHIR transactions', attrs: { value: 'fhir-transaction' } }),
  );
  const outcomeSelect = el(
    'select',
    {
      class: 'log-filter__select',
      id: 'log-filter-outcome',
      attrs: { 'aria-describedby': 'log-filter-note' },
    },
    el('option', { text: 'Any (no filter)', attrs: { value: '' } }),
    el('option', { text: 'Accepted', attrs: { value: 'accepted' } }),
    el('option', { text: 'Error', attrs: { value: 'error' } }),
    el('option', { text: 'Pending', attrs: { value: 'pending' } }),
  );
  const filterNote = el('p', { class: 'log-hint', id: 'log-filter-note' });
  const filters = el(
    'div',
    { class: 'log-filters', attrs: { role: 'group', 'aria-label': 'Filter the messages log' } },
    el(
      'div',
      { class: 'log-filter' },
      el('label', { class: 'log-filter__label', text: 'Kind', attrs: { for: 'log-filter-kind' } }),
      kindSelect,
    ),
    el(
      'div',
      { class: 'log-filter' },
      el('label', {
        class: 'log-filter__label',
        text: 'Application-level outcome',
        attrs: { for: 'log-filter-outcome' },
      }),
      outcomeSelect,
    ),
    filterNote,
  );
  // Two fixed live regions: polite for progress, assertive only for a failure. Each is written
  // only when its text changes, so a refresh that changes nothing announces nothing.
  const status = el('p', { class: 'log-status', id: 'log-status', attrs: { role: 'status' } });
  const alert = el('div', { class: 'log-alert', id: 'log-alert', attrs: { role: 'alert' } });
  const partialNote = el('div', { class: 'log-note log-note--warn', id: 'log-partial' });
  partialNote.hidden = true;
  const list = el('ol', {
    class: 'log-rows',
    id: 'log-rows',
    attrs: { 'aria-label': 'Messages log rows' },
  });
  const empty = placeholder('');
  empty.hidden = true;
  const moreButton = el('button', {
    class: 'button log-more',
    id: 'log-more',
    text: 'Load more',
    attrs: { type: 'button', 'aria-describedby': 'log-more-note' },
    on: {
      click: () => {
        loadMore();
      },
    },
    signal,
  });
  const moreNote = el('p', { class: 'log-hint', id: 'log-more-note' });
  const pollNote = el('div', { class: 'log-note' });
  pollNote.hidden = true;

  replaceChildren(
    slots.log,
    el(
      'div',
      { class: 'log' },
      el('p', { class: 'log-hint log-scope', id: 'log-scope', text: LOG_SCOPE }),
      el('p', { class: 'log-hint', id: 'log-ack-mode', text: ACK_MODE_NOTE }),
      filters,
      status,
      alert,
      partialNote,
      list,
      empty,
      pollNote,
      el('div', { class: 'log-footer' }, moreButton, moreNote),
    ),
  );
  const summary = el('span', { class: 'log-summary', id: 'log-summary' });
  replaceChildren(slots.summary, summary);

  // -- state -----------------------------------------------------------------------------------
  let shown: Shown = { entries: [], nextCursor: null, partial: false };
  let loaded = false;
  /** How the last load ended: did it stop at the bound with the log not finished? */
  let lastGather: { pagesRead: number; found: number; stoppedAtBound: boolean } | null = null;
  let generation = 0;
  let current: AbortController | null = null;
  let busy = false;
  let refreshQueued = false;
  const views = new Map<string, RowView>();
  const expanded = new Set<string>();
  let poller: AbortController | null = null;
  /** Pending rows seen when polling last gave up: polling restarts only for a new one. */
  let gaveUpOn: Set<string> | null = null;

  const setText = (node: HTMLElement, text: string) => {
    if (node.textContent !== text) node.textContent = text;
  };

  const query = (): MessagesLogQuery => ({
    ...(kindSelect.value ? { kind: kindSelect.value as Exclude<KindFilter, ''> } : {}),
    ...(outcomeSelect.value ? { outcome: outcomeSelect.value as Exclude<OutcomeFilter, ''> } : {}),
  });

  const fetchPage = (c: AbortController) => (cursor: string | undefined) =>
    client.listMessages(
      { ...query(), count: pageRows, ...(cursor ? { cursor } : {}) },
      { signal: c.signal },
    );

  // -- rows ------------------------------------------------------------------------------------
  const selectedAttempt = (): string | null => {
    const s = selection.get();
    return s?.source === 'cuff' ? s.attemptId : null;
  };

  const factList = (facts: Fact[]) => {
    const dl = el('dl', { class: 'log-facts' });
    for (const f of facts) {
      dl.append(
        el(
          'div',
          { class: `log-fact log-tone log-tone--${f.tone}` },
          el('dt', { class: 'log-fact__term', text: f.term }),
          el(
            'dd',
            { class: 'log-fact__value' },
            el('span', { class: 'log-fact__label', text: f.label }),
            f.detail ? el('span', { class: 'log-fact__detail', text: f.detail }) : null,
          ),
        ),
      );
    }
    return dl;
  };

  /** The `possibleDuplicateOf` hint: a pointer to the attempt row, never a confirmed link. */
  const hintFor = (e: SendAttemptEntry): HTMLElement | null => {
    if (e.possibleDuplicateOf === null) return null;
    const attempt = shown.entries.find(
      (x): x is SendAttemptEntry =>
        x.kind === 'cuff-send' && x.source === 'audit-event' && x.id === e.possibleDuplicateOf,
    );
    const target = attempt ? views.get(rowKey(attempt)) : undefined;
    const lead = 'Hint, not a confirmed link: this may be the stored message of the cuff send ';
    const why =
      ', whose recorded name the store did not have (same control ID, sent shortly before). Neither row takes the other’s outcome.';
    return el(
      'p',
      { class: 'log-note log-note--hint' },
      lead,
      target && attempt
        ? el('a', {
            class: 'log-link',
            text: `at ${logTime(attempt.sentAt, deps.timeZone)}`,
            attrs: { href: `#${target.root.id}` },
          })
        : el('span', { text: `${e.possibleDuplicateOf} (not among the rows loaded)` }),
      why,
    );
  };

  /** What a row shows, as one string: the row is rebuilt only when this changes. */
  const fingerprintOf = (e: MessagesLogEntry): string => {
    const key = rowKey(e);
    const hintTarget =
      e.kind === 'cuff-send' && e.possibleDuplicateOf !== null
        ? shown.entries.some(
            (x) =>
              x.kind === 'cuff-send' &&
              x.source === 'audit-event' &&
              x.id === e.possibleDuplicateOf,
          )
        : null;
    return JSON.stringify([
      e,
      selectedAttempt() === (e.kind === 'cuff-send' ? e.id : null),
      expanded.has(key),
      hintTarget,
    ]);
  };

  const buildBody = (e: MessagesLogEntry, view: RowView) => {
    const key = rowKey(e);
    const time = sortTime(e);
    const isSelected =
      e.kind === 'cuff-send' && e.source === 'audit-event' && selectedAttempt() === e.id;
    const tags: string[] = [];
    if (e.kind === 'cuff-send' && e.sample !== null) tags.push('malformed sample');
    const headingId = `${view.root.id}-heading`;
    view.root.setAttribute('aria-labelledby', headingId);
    view.root.classList.toggle('log-row--selected', isSelected);
    replaceChildren(
      view.body,
      el(
        'p',
        { class: 'log-row__heading', id: headingId },
        time === null
          ? el('span', { class: 'log-row__time', text: 'Time unknown' })
          : el('time', {
              class: 'log-row__time',
              text: logTime(time, deps.timeZone),
              attrs: { datetime: time },
            }),
        el('span', { class: 'log-row__title', text: rowTitle(e) }),
        ...tags.map((t) => el('span', { class: 'log-row__tag', text: t })),
        isSelected ? el('span', { class: 'log-row__selected', text: 'Selected' }) : null,
      ),
      factList(rowFacts(e)),
      e.kind === 'cuff-send' ? hintFor(e) : null,
      e.kind === 'cuff-send' && e.source === 'store-only'
        ? el('p', {
            class: 'log-hint',
            text: 'A stored message with no send record has no trace to open, so it cannot be selected.',
          })
        : null,
    );
    if (view.select && e.kind === 'cuff-send') {
      view.select.setAttribute('aria-pressed', String(isSelected));
    }
    if (view.expand && view.wrote && e.kind === 'fhir-transaction') {
      const open = expanded.has(key);
      view.expand.setAttribute('aria-expanded', String(open));
      view.expand.textContent = open ? 'Hide what it wrote' : 'Show what it wrote';
      view.wrote.hidden = !open;
      // An empty list is missing information (rows.ts, NO_RESOURCES_NOTE): say so, no empty list.
      replaceChildren(
        view.wrote,
        ...(e.resources.length === 0
          ? [el('p', { class: 'log-hint', text: NO_RESOURCES_NOTE })]
          : [
              el('p', {
                class: 'log-hint',
                text: `${resourceCount(e.resources)}, as references in the FHIR store:`,
              }),
              el(
                'ul',
                { class: 'log-resources' },
                ...e.resources.map((r) => el('li', { class: 'log-mono', text: r })),
              ),
            ]),
      );
    }
  };

  const createRow = (e: MessagesLogEntry): RowView => {
    const key = rowKey(e);
    // Generated ids only: a server id (which may be "") never becomes an element id.
    const root = el('li', {
      class: `log-row log-row--${rowKind(e)}`,
      id: uniqueId('log-row'),
      attrs: { tabindex: '-1' },
    });
    const body = el('div', { class: 'log-row__body' });
    const actions = el('div', { class: 'log-row__actions' });
    const view: RowView = {
      root,
      body,
      actions,
      select: null,
      expand: null,
      wrote: null,
      fingerprint: '',
    };
    if (e.kind === 'cuff-send' && e.source === 'audit-event') {
      view.select = el(
        'button',
        {
          class: 'button log-row__select',
          attrs: {
            type: 'button',
            'aria-pressed': 'false',
            'aria-describedby': `${root.id}-heading`,
          },
          on: {
            click: () => {
              pickAttempt(key);
            },
          },
          signal,
        },
        'Select',
        el('span', { class: 'visually-hidden', text: ' this cuff send' }),
      );
      actions.append(view.select);
    }
    if (e.kind === 'fhir-transaction') {
      const wroteId = `${root.id}-wrote`;
      view.wrote = el('div', { class: 'log-row__wrote', id: wroteId });
      view.wrote.hidden = true;
      view.expand = el(
        'button',
        {
          class: 'button log-row__expand',
          attrs: {
            type: 'button',
            'aria-expanded': 'false',
            'aria-controls': wroteId,
            'aria-describedby': `${root.id}-heading`,
          },
          on: {
            click: () => {
              if (expanded.has(key)) expanded.delete(key);
              else expanded.add(key);
              renderRows();
            },
          },
          signal,
        },
        'Show what it wrote',
      );
      actions.append(view.expand);
    }
    root.append(body, actions);
    if (view.wrote) root.append(view.wrote);
    return view;
  };

  /** The row nearest the top of the scrolled log, and its offset, to keep it in place. */
  const scrollAnchor = (): { node: HTMLElement; offset: number } | null => {
    const scroller = slots.log;
    if (scroller.scrollTop <= 0) return null;
    const top = scroller.getBoundingClientRect().top;
    for (const child of list.children) {
      const box = child.getBoundingClientRect();
      if (box.bottom > top) return { node: child as HTMLElement, offset: box.top - top };
    }
    return null;
  };

  /**
   * Makes the list hold exactly the shown rows, in order. A row whose content is unchanged keeps
   * its DOM untouched; a changed row rebuilds its body but keeps its buttons (so a focused
   * button stays focused); rows are moved only when their order changed; and the row at the top
   * of the scrolled view stays where it was when rows are added above it.
   */
  const renderRows = () => {
    const anchor = scrollAnchor();
    const wanted = shown.entries.map((e) => ({ e, key: rowKey(e) }));
    const keep = new Set(wanted.map((w) => w.key));
    for (const [key, view] of views) {
      if (!keep.has(key)) {
        view.root.remove();
        views.delete(key);
        expanded.delete(key);
      }
    }
    // Create missing rows first, so a hint can link to a row created in the same pass.
    for (const { e, key } of wanted) if (!views.has(key)) views.set(key, createRow(e));
    let cursor: ChildNode | null = list.firstChild;
    for (const { e, key } of wanted) {
      const view = views.get(key);
      if (!view) continue;
      const fingerprint = fingerprintOf(e);
      if (fingerprint !== view.fingerprint) {
        buildBody(e, view);
        view.fingerprint = fingerprint;
      }
      if (view.root === cursor) cursor = cursor.nextSibling;
      else list.insertBefore(view.root, cursor);
    }
    if (anchor) {
      const moved = anchor.node.getBoundingClientRect().top - slots.log.getBoundingClientRect().top;
      slots.log.scrollTop += moved - anchor.offset;
    }
  };

  const filterText = (): string => {
    const q = query();
    if (q.outcome && q.kind === 'fhir-transaction') {
      return 'FHIR transactions have no application-level outcome, so no row can match both filters.';
    }
    if (q.outcome) {
      return `Showing only rows whose application-level outcome is ${q.outcome}. Rows with no outcome match no outcome filter: rejected and unacknowledged sends, sends whose stored message could not be found or checked, unreadable records and FHIR transactions.`;
    }
    return 'The outcome filter matches only rows that have an application-level outcome.';
  };

  const renderChrome = () => {
    setText(filterNote, filterText());
    const n = shown.entries.length;
    empty.hidden = !(loaded && n === 0);
    if (loaded && n === 0) {
      setText(
        empty,
        shown.nextCursor === null
          ? query().kind || query().outcome
            ? 'No rows match these filters.'
            : 'The log is empty: no send attempts or transactions yet.'
          : 'No matching rows in the pages searched so far.',
      );
    }
    moreButton.hidden = !loaded || shown.nextCursor === null;
    moreButton.disabled = busy;
    const bound =
      lastGather?.stoppedAtBound && shown.nextCursor !== null
        ? `Searched ${plural(lastGather.pagesRead, 'page')} and found ${plural(lastGather.found, 'matching row')}; there may be more.`
        : '';
    setText(moreNote, moreButton.hidden ? '' : bound);
    if (shown.partial) {
      if (partialNote.hidden) {
        replaceChildren(
          partialNote,
          el('span', {
            text: 'This list may be incomplete: the server ran out of time, or could not read part of its stores. Rows may be missing, and some outcomes show as "could not be checked". ',
          }),
          el('button', {
            class: 'button',
            text: 'Reload the log',
            attrs: { type: 'button' },
            on: {
              click: () => {
                reload();
              },
            },
            signal,
          }),
        );
        partialNote.hidden = false;
      }
    } else partialNote.hidden = true;
    const pending = shown.entries.filter(isPendingRow).length;
    const problems = shown.entries.filter(isProblemRow).length;
    const parts = [`${plural(n, 'row')} loaded`];
    if (pending > 0) parts.push(`${pending} pending`);
    if (problems > 0) parts.push(`${problems} with a problem`);
    setText(summary, loaded ? parts.join(' · ') : 'loading');
  };

  const render = () => {
    renderRows();
    renderChrome();
  };

  // -- selection -------------------------------------------------------------------------------
  const pickAttempt = (key: string) => {
    const e = shown.entries.find((x) => rowKey(x) === key);
    if (e?.kind !== 'cuff-send' || e.source !== 'audit-event') return;
    selection.set({
      source: 'cuff',
      attemptId: e.id,
      observationId: e.observation?.replace(/^Observation\//, '') ?? null,
      summary: attemptSummary(e, deps.timeZone),
      unshareableReason: attemptUnshareableReason(e),
    });
  };
  selection.subscribe(() => {
    if (loaded) renderRows();
  }, signal);

  // -- loading ---------------------------------------------------------------------------------
  const failed = (error: unknown, retry: () => void) => {
    replaceChildren(
      alert,
      el('p', { class: 'log-note log-note--error', text: errorText(error) }),
      el('button', {
        class: 'button',
        text: 'Try again',
        attrs: { type: 'button' },
        on: { click: retry },
        signal,
      }),
    );
  };

  /**
   * After a load that kept some rows but could not read a later page: the rows stay, and the
   * notice offers to continue from the page that failed.
   */
  const afterGather = (g: { error: unknown }) => {
    if (g.error === null) {
      replaceChildren(alert);
      return;
    }
    replaceChildren(
      alert,
      el('p', {
        class: 'log-note log-note--error',
        text: `Loading stopped early, so the rows below may not be all of them. ${errorText(g.error)}`,
      }),
      el('button', {
        class: 'button',
        text: 'Try again',
        attrs: { type: 'button' },
        on: {
          click: () => {
            loadMore();
          },
        },
        signal,
      }),
    );
  };

  /** Runs one list request at a time; `work` gets a controller and the generation it belongs to. */
  const run = (work: (c: AbortController, gen: number) => Promise<void>) => {
    busy = true;
    renderChrome();
    const c = childController(signal);
    current = c;
    const gen = generation;
    work(c, gen)
      .catch((error: unknown) => {
        if (isAbort(error, c.signal) || error instanceof UnauthorizedError || gen !== generation)
          return;
        setText(status, '');
        failed(error, () => {
          if (loaded) void refresh();
          else reload();
        });
      })
      .finally(() => {
        if (current === c) current = null;
        if (gen !== generation || signal.aborted) return;
        busy = false;
        renderChrome();
        if (refreshQueued) {
          refreshQueued = false;
          void refresh();
        }
      });
  };

  const noteGather = (found: number, pagesRead: number, next: string | null) => {
    lastGather = {
      pagesRead,
      found,
      stoppedAtBound: next !== null && found < pageRows && pagesRead >= maxAutoPages,
    };
  };

  /** Everything from the start: on mount, after a filter change, or from "Reload the log". */
  function reload(): void {
    generation++;
    current?.abort();
    refreshQueued = false;
    shown = { entries: [], nextCursor: null, partial: false };
    loaded = false;
    replaceChildren(alert);
    setText(status, 'Loading the messages log…');
    render();
    run(async (c, gen) => {
      const g = await gather(fetchPage(c), undefined, pageRows, maxAutoPages);
      if (c.signal.aborted || gen !== generation) return;
      shown = { entries: uniqueRows(g.entries), nextCursor: g.nextCursor, partial: g.partial };
      loaded = true;
      noteGather(g.entries.length, g.pagesRead, g.nextCursor);
      afterGather(g);
      setText(status, `Messages log loaded: ${plural(shown.entries.length, 'row')}.`);
      render();
      maybePoll();
    });
  }

  /** The newest rows again, merged into what is shown (data changes and polling). */
  function refresh(): Promise<void> {
    if (!loaded) {
      // A change during the first load is not lost: it runs once that load has ended.
      if (busy) refreshQueued = true;
      else reload();
      return Promise.resolve();
    }
    if (busy) {
      refreshQueued = true;
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      run(async (c, gen) => {
        try {
          const g = await gather(fetchPage(c), undefined, pageRows, maxAutoPages);
          if (c.signal.aborted || gen !== generation) return;
          shown = mergeRefresh(shown, g);
          afterGather(g);
          render();
          maybePoll();
        } finally {
          resolve();
        }
      });
    });
  }

  function loadMore(): void {
    if (busy || shown.nextCursor === null) return;
    const from = shown.nextCursor;
    setText(status, 'Loading more rows…');
    run(async (c, gen) => {
      const g = await gather(fetchPage(c), from, pageRows, maxAutoPages);
      if (c.signal.aborted || gen !== generation) return;
      shown = {
        entries: uniqueRows([...shown.entries, ...g.entries]),
        nextCursor: g.nextCursor,
        partial: shown.partial || g.partial,
      };
      noteGather(g.entries.length, g.pagesRead, g.nextCursor);
      afterGather(g);
      setText(status, `${plural(g.entries.length, 'more row')} loaded.`);
      render();
      maybePoll();
    });
  }

  // -- polling for pending rows (web/README.md, "Waiting for pending") -------------------------
  const pendingKeys = () => new Set(shown.entries.filter(isPendingRow).map(rowKey));

  function maybePoll(): void {
    const pending = pendingKeys();
    if (pending.size === 0 || poller !== null) return;
    // After giving up, only a pending row we had not seen restarts the polling.
    if (gaveUpOn !== null && [...pending].every((k) => gaveUpOn?.has(k))) return;
    gaveUpOn = null;
    pollNote.hidden = true;
    const c = childController(signal);
    poller = c;
    pollUntil(
      async () => {
        await refresh();
        return pendingKeys().size === 0;
      },
      {
        signal: c.signal,
        // Only while the strip is open (the rows are visible) and the tab is visible.
        isVisible: () => document.visibilityState !== 'hidden' && !slots.log.hidden,
        ...deps.poll,
      },
    ).then(
      (done) => {
        if (poller === c) poller = null;
        if (c.signal.aborted || done) return;
        gaveUpOn = pendingKeys();
        replaceChildren(
          pollNote,
          el('span', {
            text: 'Some rows are still pending after 2 minutes, so the log stopped checking. Pending is a real state: the mapper may have failed, or the message waits to be replayed. ',
          }),
          el('button', {
            class: 'button',
            text: 'Check again',
            attrs: { type: 'button' },
            on: {
              click: () => {
                gaveUpOn = null;
                void refresh();
              },
            },
            signal,
          }),
        );
        pollNote.hidden = false;
      },
      () => {
        if (poller === c) poller = null;
      },
    );
  }

  // -- wiring ----------------------------------------------------------------------------------
  const onFilter = () => {
    poller?.abort();
    poller = null;
    gaveUpOn = null;
    reload();
  };
  kindSelect.addEventListener('change', onFilter, { signal });
  outcomeSelect.addEventListener('change', onFilter, { signal });

  // Every data change ('cuff-send', 'sync', 'share') can change what the log shows; a new send
  // may be pending, so polling may start again for it.
  context.onDataChanged(() => {
    void refresh();
  }, signal);

  signal.addEventListener('abort', () => {
    current?.abort();
    poller?.abort();
  });

  reload();
}
