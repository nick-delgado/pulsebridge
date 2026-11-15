import { describe, expect, it } from 'vitest';
import type { MessagesLogEntry, MessagesLogPage } from '../api/contract';
import { gather, mergeRefresh, uniqueRows } from './paging';
import { attempt, transaction, unreadable } from './test-support';

const page = (
  entries: MessagesLogEntry[],
  nextCursor: string | null,
  partial = false,
): MessagesLogPage => ({ entries, nextCursor, unreadable: 0, partial });

/** A fake source of pages keyed by cursor ("" for the first), recording each call. */
function source(pages: Record<string, MessagesLogPage>) {
  const calls: (string | undefined)[] = [];
  return {
    calls,
    fetch: (cursor: string | undefined) => {
      calls.push(cursor);
      const p = pages[cursor ?? ''];
      return p ? Promise.resolve(p) : Promise.reject(new Error(`no page ${String(cursor)}`));
    },
  };
}

const row = (n: number) =>
  attempt({
    id: `a${String(n)}`,
    sentAt: new Date(Date.UTC(2026, 2, 10, 14, 0, 60 - n)).toISOString(),
  });

describe('gather: following short pages, bounded (amendment 4)', () => {
  it('follows empty and short pages until it has enough rows', async () => {
    const s = source({
      '': page([], 'c1'),
      c1: page([row(1)], 'c2'),
      c2: page([row(2), row(3)], 'c3'),
      c3: page([row(4)], null),
    });
    const g = await gather(s.fetch, undefined, 3, 10);
    expect(s.calls).toEqual([undefined, 'c1', 'c2']);
    expect(g.entries.map((e) => e.id)).toEqual(['a1', 'a2', 'a3']);
    expect(g.nextCursor).toBe('c3');
    expect(g.pagesRead).toBe(3);
  });

  it('stops after maxPages even when the pages keep coming back empty', async () => {
    const pages: Record<string, MessagesLogPage> = { '': page([], 'p1') };
    for (let i = 1; i < 100; i++) pages[`p${String(i)}`] = page([], `p${String(i + 1)}`);
    const s = source(pages);
    const g = await gather(s.fetch, undefined, 25, 4);
    expect(s.calls).toHaveLength(4);
    expect(g).toMatchObject({ entries: [], nextCursor: 'p4', pagesRead: 4 });
  });

  it('stops when the log ends, and remembers any partial page', async () => {
    const s = source({ '': page([row(1)], 'c1', true), c1: page([], null) });
    const g = await gather(s.fetch, undefined, 25, 4);
    expect(g).toMatchObject({ nextCursor: null, partial: true, pagesRead: 2 });
  });

  it('starts from a cursor for "load more"', async () => {
    const s = source({ x: page([row(9)], null) });
    await gather(s.fetch, 'x');
    expect(s.calls).toEqual(['x']);
  });
});

describe('mergeRefresh: keyed, keeps older rows already loaded', () => {
  const shown = {
    entries: [row(1), row(2), row(3), row(4), unreadable({ recordedAt: null, id: '' })],
    nextCursor: 'old-cursor',
    partial: false,
  };

  it('fresh rows first (updated in place by key), then older rows it did not reach, old cursor kept', () => {
    const updated = attempt({ ...row(2), application: { outcome: 'error', reason: 'x' } });
    const fresh = {
      entries: [row(0), row(1), updated],
      nextCursor: 'fresh-cursor',
      partial: false,
      pagesRead: 1,
    };
    const merged = mergeRefresh(shown, fresh);
    expect(merged.entries.map((e) => e.id)).toEqual(['a0', 'a1', 'a2', 'a3', 'a4', '']);
    expect(merged.entries[2]).toBe(updated);
    expect(merged.nextCursor).toBe('old-cursor');
  });

  it('a refresh that reached the end drops rows it did not return (they are gone)', () => {
    const merged = mergeRefresh(shown, {
      entries: [row(1)],
      nextCursor: null,
      partial: false,
      pagesRead: 1,
    });
    expect(merged.entries.map((e) => e.id)).toEqual(['a1']);
    expect(merged.nextCursor).toBeNull();
  });

  it('partial stays true while kept rows came from a partial page', () => {
    const merged = mergeRefresh(
      { ...shown, partial: true },
      { entries: [row(1)], nextCursor: 'c', partial: false, pagesRead: 1 },
    );
    expect(merged.partial).toBe(true);
    const fresh = mergeRefresh(shown, {
      entries: [row(1)],
      nextCursor: 'c',
      partial: true,
      pagesRead: 1,
    });
    expect(fresh.partial).toBe(true);
    expect(
      mergeRefresh(shown, { entries: [row(1)], nextCursor: 'c', partial: false, pagesRead: 1 })
        .partial,
    ).toBe(false);
  });

  it('never shows one row twice', () => {
    expect(uniqueRows([row(1), row(1), transaction(), transaction()]).map((e) => e.id)).toEqual([
      'a1',
      'tx-1',
    ]);
  });
});
