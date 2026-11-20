/**
 * The status strip announces a new watch sync (`notifyDataChanged('sync')`) so features re-fetch,
 * and reloads itself periodically so a sync during a session is noticed at all.
 */
import { afterEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import type { StatusSummary } from '../api/contract';
import { MockClient } from '../api/mock-client';
import { SYNC_INTERVAL_MS } from '../api/mock-data';
import type { DataChangeKind } from '../state/data-changed';
import { startApp, STATUS_REFRESH_MS, VISIBILITY_RELOAD_MIN_GAP_MS } from './app';
import type { FeatureContext, Features } from './features';
import { mockBotCheck, viewDemo } from './test-support';

const settle = async () => {
  for (let i = 0; i < 3; i++) await new Promise((r) => setTimeout(r, 0));
};

function status(lastSyncAt: string | null): StatusSummary {
  const checkedAt = '2026-03-10T12:00:00Z';
  return {
    watchSync: {
      state: 'connected',
      reason: null,
      notes: [],
      strandedPreliminary: null,
      checkedAt,
      lastSyncAt,
    },
    observationCounts: { watch: 1, cuff: 0 },
    fhirStore: { reachable: true, checkedAt, detail: null },
    openemr: { reachable: true, checkedAt, detail: null },
  };
}

interface Harness {
  heard: DataChangeKind[];
  getStatus: MockInstance<MockClient['getStatus']>;
  /** The listening feature's context, to trigger a reload or a change the way a feature would. */
  ctx: () => FeatureContext<string>;
  stripText: () => string;
  setNow: (ms: number) => void;
  now: () => number;
  /** Extra listeners for the heart-rate stand-in, called with the context and the kind. */
  onKind: ((ctx: FeatureContext<string>, kind: DataChangeKind) => void)[];
  logOut: () => void;
  logIn: () => Promise<void>;
}

const quiet = () => undefined;

async function start(answers?: (StatusSummary | Promise<StatusSummary>)[]): Promise<Harness> {
  document.body.replaceChildren();
  const root = document.createElement('div');
  document.body.append(root);
  let now = Date.parse('2026-03-10T12:00:00Z');
  const client = new MockClient({
    onUnauthorized: quiet,
    latencyMs: 0,
    now: () => now,
  });
  await client.startSession('mock-recaptcha-test');
  const getStatus = vi.spyOn(client, 'getStatus');
  if (answers) {
    for (const answer of answers) getStatus.mockImplementationOnce(() => Promise.resolve(answer));
  }
  const heard: DataChangeKind[] = [];
  const onKind: Harness['onKind'] = [];
  let context: FeatureContext<string> | null = null;
  const features: Features = {
    heartRate: (c) => {
      context = c;
      c.onDataChanged((kind) => {
        heard.push(kind);
        for (const f of onKind) f(c, kind);
      }, c.signal);
    },
    cuff: quiet,
    share: quiet,
    messages: quiet,
  };
  await startApp({
    root,
    makeClient: () => client,
    botCheck: mockBotCheck(),
    features,
    now: () => now,
  });
  await settle();
  return {
    heard,
    getStatus,
    ctx: () => {
      if (!context) throw new Error('not mounted');
      return context;
    },
    stripText: () => root.querySelector('.status-strip')?.textContent ?? '',
    setNow: (ms) => {
      now = ms;
    },
    now: () => now,
    onKind,
    logOut: () => {
      [...root.querySelectorAll('button')].find((b) => b.textContent === 'Log out')?.click();
    },
    logIn: async () => {
      await viewDemo(root);
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('status strip: announcing a new sync', () => {
  it('announces nothing on the first load, nor when the time is unchanged', async () => {
    const h = await start([status('2026-03-10T11:47:00Z'), status('2026-03-10T11:47:00Z')]);
    expect(h.getStatus).toHaveBeenCalledTimes(1);
    expect(h.heard).toEqual([]);
    h.ctx().refreshStatus();
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(2);
    expect(h.heard).toEqual([]);
  });

  it("announces 'sync' once when the time changes, without reloading itself again", async () => {
    const h = await start([
      status('2026-03-10T11:47:00Z'),
      status('2026-03-10T12:02:00Z'),
      status('2026-03-10T12:02:00Z'),
    ]);
    h.ctx().refreshStatus();
    await settle();
    expect(h.heard).toEqual(['sync']);
    // Two loads only: the announcement did not trigger the strip's own reload (no loop).
    expect(h.getStatus).toHaveBeenCalledTimes(2);
    // Another feature's change reloads it; the same time is not announced again.
    h.ctx().notifyDataChanged('cuff-send');
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(3);
    expect(h.heard).toEqual(['sync', 'cuff-send']);
  });

  it("still reloads for another change a 'sync' listener reports during the announcement", async () => {
    const h = await start([
      status('2026-03-10T11:47:00Z'),
      status('2026-03-10T12:02:00Z'),
      status('2026-03-10T12:02:00Z'),
    ]);
    h.onKind.push((ctx, kind) => {
      if (kind === 'sync') ctx.notifyDataChanged('share');
    });
    h.ctx().refreshStatus();
    await settle();
    expect(h.heard).toEqual(['sync', 'share']);
    // Load 1, load 2 (announces), and the reload for 'share'; none for the strip's own 'sync'.
    expect(h.getStatus).toHaveBeenCalledTimes(3);
  });

  it('announces nothing when the time becomes null', async () => {
    const h = await start([status('2026-03-10T11:47:00Z'), status(null)]);
    h.ctx().refreshStatus();
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(2);
    expect(h.heard).toEqual([]);
  });

  it('ignores an answer that arrives after a newer load started', async () => {
    let answerOld: (s: StatusSummary) => void = quiet;
    const old = new Promise<StatusSummary>((r) => {
      answerOld = r;
    });
    const h = await start([status('2026-03-10T11:47:00Z'), old, status('2026-03-10T11:47:00Z')]);
    h.ctx().refreshStatus(); // load 2: still waiting
    h.ctx().refreshStatus(); // load 3: answers at once, same time
    await settle();
    // Load 2 answers late with a different time: it is stale, so neither shown nor announced.
    answerOld(status('2026-03-10T09:00:00Z'));
    await settle();
    expect(h.heard).toEqual([]);
    expect(h.stripText()).toContain('13 min ago');
  });
});

describe('status strip: periodic reload', () => {
  it('reloads every STATUS_REFRESH_MS while the tab is visible, and not while it is hidden', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const h = await start();
    expect(h.getStatus).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(STATUS_REFRESH_MS);
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(2);

    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    vi.advanceTimersByTime(STATUS_REFRESH_MS * 3);
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(2);

    // Coming back to the tab reloads at once, a minute or more after the last load started.
    h.setNow(h.now() + VISIBILITY_RELOAD_MIN_GAP_MS);
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(3);
  });

  it('a visibility change within a minute of the last load does not reload', async () => {
    const h = await start();
    h.setNow(h.now() + VISIBILITY_RELOAD_MIN_GAP_MS - 1000);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(1);
    h.setNow(h.now() + 1000);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(2);
  });

  it('logout stops the old dashboard polling; after logging in again only the new one polls', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const h = await start();
    expect(h.getStatus).toHaveBeenCalledTimes(1);
    h.logOut();
    // Logged out: neither the timer nor a visibility change loads the status.
    h.setNow(h.now() + 10 * VISIBILITY_RELOAD_MIN_GAP_MS);
    vi.advanceTimersByTime(STATUS_REFRESH_MS * 2);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(1);

    await h.logIn();
    expect(h.getStatus).toHaveBeenCalledTimes(2); // the new dashboard's first load
    vi.advanceTimersByTime(STATUS_REFRESH_MS);
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(3); // one timer, not two
    h.setNow(h.now() + VISIBILITY_RELOAD_MIN_GAP_MS);
    document.dispatchEvent(new Event('visibilitychange'));
    await settle();
    expect(h.getStatus).toHaveBeenCalledTimes(4); // one visibility listener, not two
  });

  it('with the mock: a sync run during the session is noticed and announced', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const h = await start();
    const lastSync = async (i: number): Promise<string | null | undefined> =>
      ((await h.getStatus.mock.results.at(i)?.value) as StatusSummary | undefined)?.watchSync
        .lastSyncAt;
    const first = await lastSync(0);
    vi.advanceTimersByTime(STATUS_REFRESH_MS);
    await settle();
    expect(h.heard).toEqual([]); // no run between: same lastSyncAt

    // The mock's next health-sync run happens; the next periodic reload sees it.
    h.setNow(Date.parse('2026-03-10T12:00:00Z') + SYNC_INTERVAL_MS);
    vi.advanceTimersByTime(STATUS_REFRESH_MS);
    await settle();
    expect(h.heard).toEqual(['sync']);
    expect(await lastSync(-1)).not.toBe(first);
  });
});
