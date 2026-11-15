/**
 * Paging the messages log, as pure functions (paging.test.ts).
 *
 * The proxy filters a page AFTER it has joined it (amendment 4), so with a kind or outcome
 * filter a page can be short, even empty, while `nextCursor` is not null. The log therefore
 * follows `nextCursor` by itself until it has a screenful of rows, but only for a bounded
 * number of pages (`MAX_AUTO_PAGES`), then offers "Load more" so a rare filter never turns into
 * an unbounded run of requests.
 *
 * A refresh (after a data change, or while a row is pending) reloads the newest rows and merges
 * them into what is shown (`mergeRefresh`): rows are keyed, so a row that changed is updated in
 * place, new rows appear at the top, and older rows already loaded below stay.
 */
import type { MessagesLogEntry, MessagesLogPage } from '../api/contract';
import { rowKey, sortTime } from './rows';

/** Rows the log wants per load; the proxy's default page size (amendment 3). */
export const PAGE_ROWS = 25;
/** Pages one load may read on its own before it asks the user ("Load more"). */
export const MAX_AUTO_PAGES = 4;

export interface Gathered {
  entries: MessagesLogEntry[];
  /** Where to continue: after the last page read, or the page that failed (so a retry re-asks it). */
  nextCursor: string | null;
  /** True if any page read said `partial`. */
  partial: boolean;
  pagesRead: number;
  /**
   * Set when a page after the first failed: the rows gathered before it are kept and returned,
   * and `nextCursor` names the page that failed. A failure of the FIRST page is thrown instead
   * (there is nothing to show).
   */
  error: unknown;
}

/**
 * Reads pages from `cursor` (undefined: from the start) until at least `want` rows are gathered,
 * the log ends (`nextCursor` null), or `maxPages` pages were read. `fetchPage` is one
 * `listMessages` call with the filters already applied.
 */
export async function gather(
  fetchPage: (cursor: string | undefined) => Promise<MessagesLogPage>,
  cursor: string | undefined,
  want: number = PAGE_ROWS,
  maxPages: number = MAX_AUTO_PAGES,
): Promise<Gathered> {
  const entries: MessagesLogEntry[] = [];
  let next: string | null | undefined = cursor;
  let partial = false;
  let pagesRead = 0;
  do {
    let page: MessagesLogPage;
    try {
      page = await fetchPage(next ?? undefined);
    } catch (error) {
      // The first page failing leaves nothing to show; a later one keeps what was gathered.
      if (pagesRead === 0) throw error;
      return { entries, nextCursor: next ?? null, partial, pagesRead, error };
    }
    pagesRead++;
    entries.push(...page.entries);
    partial ||= page.partial;
    next = page.nextCursor;
  } while (next !== null && entries.length < want && pagesRead < maxPages);
  return { entries, nextCursor: next, partial, pagesRead, error: null };
}

/** Rows with duplicate keys removed (the first wins), so one row is never shown twice. */
export function uniqueRows(entries: MessagesLogEntry[]): MessagesLogEntry[] {
  const seen = new Set<string>();
  return entries.filter((e) => {
    const key = rowKey(e);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export interface Shown {
  entries: MessagesLogEntry[];
  nextCursor: string | null;
  partial: boolean;
}

/**
 * The rows to show after a refresh that read `fresh` from the start:
 * - every fresh row, in the server's order (newer data replaces older data, key by key);
 * - then the rows already shown that the refresh did not reach: older than (or as old as) the
 *   oldest fresh row, and not among the fresh rows. Their cursor is still the right one to
 *   continue from, so it is kept;
 * - if the refresh reached the end of the log (`nextCursor` null), nothing older exists, so rows
 *   it did not return are gone and are dropped.
 * `partial` stays true while any row shown came from a partial page.
 */
export function mergeRefresh(shown: Shown, fresh: Gathered): Shown {
  if (fresh.nextCursor === null) {
    return { entries: uniqueRows(fresh.entries), nextCursor: null, partial: fresh.partial };
  }
  const keys = new Set(fresh.entries.map(rowKey));
  const times = fresh.entries.map(sortTime).filter((t): t is string => t !== null);
  const oldest = times.length > 0 ? Math.min(...times.map((t) => Date.parse(t))) : null;
  const tail = shown.entries.filter((e) => {
    if (keys.has(rowKey(e))) return false;
    // A refresh that found no dated row (a filtered page can be empty while more pages exist)
    // reached nothing below the top: everything already shown stays, with its cursor.
    if (oldest === null) return true;
    const t = sortTime(e);
    if (t === null) return true; // undated rows are always last
    return Date.parse(t) <= oldest;
  });
  return {
    entries: uniqueRows([...fresh.entries, ...tail]),
    nextCursor: tail.length > 0 ? shown.nextCursor : fresh.nextCursor,
    partial: fresh.partial || (tail.length > 0 && shown.partial),
  };
}
