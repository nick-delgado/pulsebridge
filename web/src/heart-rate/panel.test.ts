import { afterEach, describe, expect, it } from 'vitest';
import type {
  CallOptions,
  HeartRateTrace,
  ObservationQuery,
  PulseBridgeApi,
  StatusSummary,
} from '../api/contract';
import { ApiError, UnauthorizedError } from '../api/errors';
import type { Observation } from '../api/fhir';
import { createDataChangeBus } from '../state/data-changed';
import { createSelectionStore, shareableObservation } from '../state/selection';
import type { ChartCallbacks, ChartFactory, ChartModel } from './chart';
import { createHeartRate } from './panel';
import { hrObservation, plusMinutes } from './test-support';

const NOW = Date.parse('2026-01-02T00:00:00Z');
const START = '2026-01-01T09:00:00-05:00';

/** Three synthetic windows: two final, the newest preliminary. */
function threeWindows(): Observation[] {
  return [
    hrObservation({ start: START, avg: 60, min: 55, max: 66, id: 'w1' }),
    hrObservation({ start: plusMinutes(START, 5), avg: 70, min: 65, max: 78, id: 'w2' }),
    hrObservation({
      start: plusMinutes(START, 10),
      avg: 80.5,
      min: 72,
      max: 90,
      id: 'w3',
      status: 'preliminary',
    }),
  ];
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function status(state: StatusSummary['watchSync']['state'] = 'connected'): StatusSummary {
  const checkedAt = new Date(NOW).toISOString();
  return {
    watchSync: {
      state,
      reason: null,
      notes: [],
      strandedPreliminary: null,
      checkedAt,
      lastSyncAt: checkedAt,
    },
    observationCounts: { watch: 0, cuff: 0 },
    fhirStore: { reachable: true, checkedAt, detail: null },
    openemr: { reachable: true, checkedAt, detail: null },
  };
}

/** A client whose answers each test controls: every search waits on a Deferred the test resolves. */
function fakeClient() {
  const searches: {
    query: ObservationQuery;
    signal: AbortSignal;
    answer: Deferred<Observation[]>;
  }[] = [];
  const traces: { id: string; signal: AbortSignal; answer: Deferred<HeartRateTrace> }[] = [];
  let statusValue: StatusSummary = status();
  /** When set, getStatus waits until the test resolves the Deferred it pushes here. */
  let holdStatus = false;
  const statuses: Deferred<StatusSummary>[] = [];
  const client = {
    mode: 'mock',
    searchObservations(query: ObservationQuery, options: CallOptions = {}) {
      const answer = deferred<Observation[]>();
      if (!options.signal) throw new Error('search without a signal');
      searches.push({ query, signal: options.signal, answer });
      return answer.promise;
    },
    getStatus() {
      if (!holdStatus) return Promise.resolve(statusValue);
      const answer = deferred<StatusSummary>();
      statuses.push(answer);
      return answer.promise;
    },
    getHeartRateTrace(id: string, options: CallOptions = {}) {
      const answer = deferred<HeartRateTrace>();
      if (!options.signal) throw new Error('trace without a signal');
      traces.push({ id, signal: options.signal, answer });
      return answer.promise;
    },
  } as unknown as PulseBridgeApi;
  return {
    client,
    searches,
    traces,
    statuses,
    setStatus(s: StatusSummary) {
      statusValue = s;
    },
    holdStatus() {
      holdStatus = true;
    },
  };
}

/** A trace answer built around one Observation, the way the client returns it. */
function traceOf(observation: Observation): HeartRateTrace {
  return { observation, provenance: null, device: null, source: null };
}

/** Stands in for uPlot (jsdom has no canvas); it keeps what it was given so a test can click. */
function fakeChart() {
  const state: { model: ChartModel | null; callbacks: ChartCallbacks | null; created: number } = {
    model: null,
    callbacks: null,
    created: 0,
  };
  const factory: ChartFactory = (_container, callbacks) => {
    state.created += 1;
    state.callbacks = callbacks;
    return {
      update(model) {
        state.model = model;
      },
      mark() {
        /* drawing only */
      },
      destroy() {
        /* nothing */
      },
    };
  };
  return { factory, state };
}

let teardown: AbortController | null = null;
afterEach(() => {
  teardown?.abort();
  document.body.replaceChildren();
});

function mount(before?: (api: ReturnType<typeof fakeClient>) => void) {
  const api = fakeClient();
  before?.(api);
  const chart = fakeChart();
  const selection = createSelectionStore();
  const bus = createDataChangeBus();
  const controller = new AbortController();
  teardown = controller;
  const slots = { panel: document.createElement('div'), trace: document.createElement('div') };
  document.body.append(slots.panel, slots.trace);
  createHeartRate(
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
    { chart: chart.factory, now: () => NOW },
  );
  const slider = () => {
    const s = slots.panel.querySelector<HTMLElement>('[role="slider"]');
    if (!s) throw new Error('no slider');
    return s;
  };
  const key = (k: string) => {
    slider().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
  };
  const button = (name: string) => {
    const b = [...slots.panel.querySelectorAll('button')].find(
      (x) => x.textContent === name || x.getAttribute('aria-label') === name,
    );
    if (!b) throw new Error(`no button ${name}`);
    return b;
  };
  return { ...api, chart, selection, bus, controller, slots, slider, key, button };
}

/** Lets resolved promises run their .then callbacks. */
const settle = () => new Promise((r) => setTimeout(r, 0));

async function loaded(observations = threeWindows()) {
  const m = mount();
  m.searches[0]?.answer.resolve(observations);
  await settle();
  return m;
}

describe('loading a range', () => {
  it('asks for heart rate (LOINC 8867-4) over the last 24 hours by default', () => {
    const m = mount();
    expect(m.searches).toHaveLength(1);
    expect(m.searches[0]?.query.code).toBe('8867-4');
    expect(m.searches[0]?.query.range.end.getTime()).toBe(NOW);
    expect(m.searches[0]?.query.range.start.getTime()).toBe(NOW - 24 * 3600_000);
    expect(m.button('Show the last 24 hours').getAttribute('aria-pressed')).toBe('true');
    expect(m.slots.panel.textContent).toContain('Loading the last 24 hours');
    expect(m.slots.panel.getAttribute('aria-busy')).toBe('true');
  });

  it('shows min, mean and max of what is displayed, and the counts', async () => {
    const m = await loaded();
    const stats = m.slots.panel.querySelector('.hr-stats')?.textContent ?? '';
    expect(stats).toContain('Minimum55 /min');
    expect(stats).toContain('Mean70.2 /min');
    expect(stats).toContain('Maximum90 /min');
    expect(stats).toContain('Windows31 provisional');
    expect(stats).toContain('mean of the five-minute averages');
    expect(m.chart.state.model?.points.map((p) => p.id)).toEqual(['w1', 'w2', 'w3']);
    expect(m.slots.panel.hasAttribute('aria-busy')).toBe(false);
  });

  it('gives a text alternative: the range, the count and the latest value', async () => {
    const m = await loaded();
    const summary = m.slots.panel.querySelector('#hr-summary')?.textContent ?? '';
    expect(summary).toContain('last 24 hours: 3 five-minute windows');
    expect(summary).toContain('Latest: 80.5 beats/min, provisional (status preliminary)');
    expect(m.slider().getAttribute('aria-describedby')).toBe('hr-summary');
  });

  it('states the zone times are shown in', async () => {
    const m = await loaded();
    expect(m.slots.panel.querySelector('.hr-zone')?.textContent).toBe(
      'Times as recorded by the watch (UTC-05:00).',
    );
  });
});

describe('the ranges (T47)', () => {
  it('offers 24 h and 7 d only: there is no 30 d, the proxy shows no more than a week', () => {
    const m = mount();
    const buttons = [
      ...m.slots.panel.querySelectorAll<HTMLButtonElement>('.hr-ranges__button'),
    ].map((b) => [b.textContent, b.getAttribute('aria-label')]);
    expect(buttons).toEqual([
      ['24 h', 'Show the last 24 hours'],
      ['7 d', 'Show the last 7 days'],
    ]);
  });
});

describe('switching range', () => {
  it('aborts the request in flight and ignores its late answer', async () => {
    const m = mount();
    m.button('Show the last 7 days').click();
    expect(m.searches).toHaveLength(2);
    expect(m.searches[0]?.signal.aborted).toBe(true);
    expect(m.searches[1]?.signal.aborted).toBe(false);
    expect(m.searches[1]?.query.range.start.getTime()).toBe(NOW - 7 * 24 * 3600_000);
    expect(m.button('Show the last 7 days').getAttribute('aria-pressed')).toBe('true');
    expect(m.button('Show the last 24 hours').getAttribute('aria-pressed')).toBe('false');

    // The old answer arrives after all: it must not be shown.
    m.searches[0]?.answer.resolve(threeWindows());
    await settle();
    expect(m.chart.state.model).toBeNull();
    m.searches[1]?.answer.resolve(threeWindows().slice(0, 1));
    await settle();
    expect(m.chart.state.model?.points.map((p) => p.id)).toEqual(['w1']);
  });

  it('aborts in-flight requests when the dashboard is torn down (logout)', () => {
    const m = mount();
    m.controller.abort();
    expect(m.searches[0]?.signal.aborted).toBe(true);
  });
});

describe('empty and error states', () => {
  it('says there is no data in the range', async () => {
    const m = await loaded([]);
    expect(m.slots.panel.textContent).toContain('No heart-rate data in this range.');
    expect(m.slots.panel.querySelector<HTMLElement>('.hr-chart-area')?.hidden).toBe(true);
    expect(m.slots.panel.textContent).not.toContain('Reconnect needed');
  });

  it('says the watch has not synced yet when the status has no last sync time', async () => {
    const m = mount();
    m.setStatus({ ...status(), watchSync: { ...status().watchSync, lastSyncAt: null } });
    m.button('Show the last 7 days').click();
    m.searches[1]?.answer.resolve([]);
    await settle();
    const text = m.slots.panel.querySelector('.hr-message')?.textContent ?? '';
    expect(text).toContain('The watch has not synced yet.');
    const synced = await loaded([]);
    expect(synced.slots.panel.textContent).not.toContain('not synced yet');
  });

  it('says a reconnect is needed when the status strip reports it', async () => {
    const m = mount();
    m.setStatus(status('reconnect-needed'));
    m.button('Show the last 7 days').click();
    m.searches[1]?.answer.resolve([]);
    await settle();
    const text = m.slots.panel.querySelector('.hr-message')?.textContent ?? '';
    expect(text).toContain('No heart-rate data in this range.');
    expect(text).toContain('Reconnect needed');
  });

  it('shows the client error as an alert, with a way to try again', async () => {
    const m = mount();
    m.searches[0]?.answer.reject(new ApiError(503, 'The FHIR store is unavailable.'));
    await settle();
    const alert = m.slots.panel.querySelector('[role="alert"]');
    expect(alert?.textContent).toBe(
      'Could not load heart-rate data: The FHIR store is unavailable.',
    );
    m.button('Try again').click();
    expect(m.searches).toHaveLength(2);
    m.searches[1]?.answer.resolve(threeWindows());
    await settle();
    expect(m.slots.panel.querySelector('[role="alert"]')).toBeNull();
  });

  it('shows nothing of its own for a 401: the shell is already showing the login form', async () => {
    const m = mount();
    m.searches[0]?.answer.reject(new UnauthorizedError());
    await settle();
    expect(m.slots.panel.querySelector('[role="alert"]')).toBeNull();
  });
});

describe('keyboard and selection', () => {
  it('moves between windows with the arrow keys and announces each in words', async () => {
    const m = await loaded();
    expect(m.slider().getAttribute('aria-valuetext')).toBe(
      '3 windows; press an arrow key to move through them',
    );
    m.slider().dispatchEvent(new FocusEvent('focus'));
    expect(m.slider().getAttribute('aria-valuenow')).toBe('3');
    expect(m.slider().getAttribute('aria-valuemax')).toBe('3');
    m.key('ArrowLeft');
    expect(m.slider().getAttribute('aria-valuenow')).toBe('2');
    expect(m.slider().getAttribute('aria-valuetext')).toBe(
      'Thu 1 Jan 2026, 09:05–09:10 (UTC-05:00): average 70 beats/min, range 65 to 78; final',
    );
    m.key('Home');
    expect(m.slider().getAttribute('aria-valuenow')).toBe('1');
    m.key('ArrowLeft'); // stays at the first window
    expect(m.slider().getAttribute('aria-valuenow')).toBe('1');
    m.key('End');
    expect(m.slider().getAttribute('aria-valuenow')).toBe('3');
    // Moving does not select; only Enter or Space does.
    expect(m.selection.get()).toBeNull();
  });

  it('Enter on a final window selects it as shareable, shows its details and loads its trace', async () => {
    const m = await loaded();
    m.slider().dispatchEvent(new FocusEvent('focus'));
    m.key('ArrowLeft');
    m.key('Enter');
    const sel = m.selection.get();
    expect(sel).toMatchObject({ source: 'watch', observationId: 'w2', unshareableReason: null });
    expect(shareableObservation(sel)).toBe('Observation/w2');
    const details = m.slots.panel.querySelector('.hr-details')?.textContent ?? '';
    expect(details).toContain('Thu 1 Jan 2026, 09:05–09:10 (UTC-05:00)');
    expect(details).toContain('70 beats/min average (minimum 65, maximum 78)');
    expect(details).toContain('SourceWatch, via Google Health');
    expect(m.traces.map((t) => t.id)).toEqual(['w2']);
    expect(m.slots.trace.textContent).toContain('Loading the trace');
  });

  it('a preliminary window is selected as not shareable, with the reason', async () => {
    const m = await loaded();
    m.slider().dispatchEvent(new FocusEvent('focus'));
    m.key(' ');
    const sel = m.selection.get();
    expect(sel?.source === 'watch' && sel.observationId).toBe('w3');
    expect(sel?.unshareableReason).toMatch(/still provisional/);
    expect(shareableObservation(sel)).toBeNull();
    expect(m.slots.panel.querySelector('.hr-details')?.textContent).toContain(
      'provisional (status preliminary)',
    );
  });

  it('a click on the chart selects the window the chart reports', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(0);
    expect(m.selection.get()).toMatchObject({ observationId: 'w1' });
  });

  it('a newer pick aborts the older trace request', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(0);
    m.chart.state.callbacks?.onPick(1);
    expect(m.traces[0]?.signal.aborted).toBe(true);
    expect(m.traces[1]?.signal.aborted).toBe(false);
  });

  it('drops its trace when the selection moves to the cuff side', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(0);
    m.selection.set({
      source: 'cuff',
      attemptId: 'a1',
      observationId: 'bp1',
      summary: 'BP 120/80',
      unshareableReason: null,
    });
    expect(m.traces[0]?.signal.aborted).toBe(true);
    expect(m.slots.trace.textContent).toContain('Select a point on the chart');
    expect(m.slots.panel.querySelector('.hr-details')?.textContent).toBe('');
  });
});

describe('after a sync', () => {
  it('re-fetches, and a selected window that became final becomes shareable', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(2);
    expect(shareableObservation(m.selection.get())).toBeNull();

    m.bus.notify('cuff-send'); // not ours to react to
    expect(m.searches).toHaveLength(1);
    m.bus.notify('sync');
    expect(m.searches).toHaveLength(2);
    const finalised = threeWindows().map((o) => ({ ...o, status: 'final' as const }));
    m.searches[1]?.answer.resolve(finalised);
    await settle();
    expect(shareableObservation(m.selection.get())).toBe('Observation/w3');
    expect(m.slots.panel.querySelector('.hr-stats')?.textContent).toContain('none provisional');
  });
});

describe('the selection after a reload (review M1)', () => {
  it('is dropped when the selected window comes back entered-in-error', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(1); // w2, final
    expect(shareableObservation(m.selection.get())).toBe('Observation/w2');
    m.bus.notify('sync');
    const again = threeWindows();
    again[1] = {
      ...hrObservation({ start: plusMinutes(START, 5), avg: 70, id: 'w2' }),
      status: 'entered-in-error',
    };
    m.searches[1]?.answer.resolve(again);
    await settle();
    expect(m.selection.get()).toBeNull();
    expect(m.slots.panel.querySelector('.hr-details')?.textContent).toBe('');
    expect(m.slots.trace.textContent).toContain('is no longer a stored result');
    expect(m.traces[0]?.signal.aborted).toBe(true);
  });

  it('is dropped when the selected window is missing from a range that overlaps it', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(1);
    m.bus.notify('sync');
    m.searches[1]?.answer.resolve(threeWindows().filter((o) => o.id !== 'w2'));
    await settle();
    expect(m.selection.get()).toBeNull();
    expect(m.traces).toHaveLength(1); // no re-check: the search would have returned it
  });

  /** Selects a window from 28 Dec (inside 7 d, outside 24 h), then switches back to 24 h. */
  async function selectOldThenShow24h() {
    const old = hrObservation({
      start: '2025-12-28T10:00:00-05:00',
      avg: 64,
      min: 60,
      max: 70,
      id: 'old',
    });
    const m = await loaded();
    m.button('Show the last 7 days').click();
    m.searches[1]?.answer.resolve([old, ...threeWindows()]);
    await settle();
    m.chart.state.callbacks?.onPick(0);
    m.traces[0]?.answer.resolve(traceOf(old));
    await settle();
    expect(shareableObservation(m.selection.get())).toBe('Observation/old');
    m.button('Show the last 24 hours').click();
    m.searches[2]?.answer.resolve(threeWindows());
    await settle();
    return { m, old };
  }

  it('is kept but re-checked through the trace when it lies outside the range shown', async () => {
    const { m, old } = await selectOldThenShow24h();
    expect(m.traces.map((t) => t.id)).toEqual(['old', 'old']);
    expect(shareableObservation(m.selection.get())).toBe('Observation/old');
    m.traces[1]?.answer.resolve(traceOf(old));
    await settle();
    expect(shareableObservation(m.selection.get())).toBe('Observation/old');
    expect(m.slots.trace.textContent).toContain('"id": "old"');
  });

  it('is dropped when that re-check finds it withdrawn', async () => {
    const { m, old } = await selectOldThenShow24h();
    m.traces[1]?.answer.resolve(traceOf({ ...old, status: 'entered-in-error' }));
    await settle();
    expect(m.selection.get()).toBeNull();
    expect(m.slots.trace.textContent).toContain('is no longer a stored result');
  });

  it('is dropped when that re-check finds it gone (404)', async () => {
    const { m } = await selectOldThenShow24h();
    m.traces[1]?.answer.reject(new ApiError(404, 'Observation/old not found'));
    await settle();
    expect(m.selection.get()).toBeNull();
  });
});

describe('the trace keeps the selection current', () => {
  it('a trace newer than the chart (now final) makes the selection shareable', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(2); // w3, preliminary on the chart
    expect(shareableObservation(m.selection.get())).toBeNull();
    const w3 = threeWindows()[2];
    if (!w3) throw new Error('fixture');
    m.traces[0]?.answer.resolve(traceOf({ ...w3, status: 'final' }));
    await settle();
    expect(shareableObservation(m.selection.get())).toBe('Observation/w3');
    expect(m.slots.panel.querySelector('.hr-details')?.textContent).toContain('Statusfinal');
  });

  it('ignores a trace answer that arrives after a newer pick', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(0);
    m.chart.state.callbacks?.onPick(1);
    const [w1, w2] = threeWindows();
    if (!w1 || !w2) throw new Error('fixture');
    m.traces[1]?.answer.resolve(traceOf(w2));
    await settle();
    m.traces[0]?.answer.resolve(traceOf(w1)); // late, for the older pick
    await settle();
    expect(m.selection.get()).toMatchObject({ observationId: 'w2' });
    expect(m.slots.trace.textContent).toContain('"id": "w2"');
    expect(m.slots.trace.textContent).not.toContain('"id": "w1"');
  });

  it('reloads the trace after a sync only when the selected window changed', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(1);
    expect(m.traces).toHaveLength(1);
    m.bus.notify('sync');
    m.searches[1]?.answer.resolve(threeWindows()); // unchanged
    await settle();
    expect(m.traces).toHaveLength(1);

    m.bus.notify('sync');
    const newVersion = threeWindows().map((o) =>
      o.id === 'w2' ? { ...o, meta: { versionId: '2' } } : o,
    );
    m.searches[2]?.answer.resolve(newVersion);
    await settle();
    expect(m.traces.map((t) => t.id)).toEqual(['w2', 'w2']);

    m.chart.state.callbacks?.onPick(2); // w3, preliminary
    m.bus.notify('sync');
    m.searches[3]?.answer.resolve(newVersion.map((o) => ({ ...o, status: 'final' as const })));
    await settle();
    expect(m.traces.map((t) => t.id)).toEqual(['w2', 'w2', 'w3', 'w3']);
  });
});

describe('pointer and Page keys', () => {
  it('a click moves the keyboard cursor too, so the slider value matches', async () => {
    const m = await loaded();
    m.chart.state.callbacks?.onPick(0);
    expect(m.slider().getAttribute('aria-valuenow')).toBe('1');
    expect(m.slider().getAttribute('aria-valuetext')).toMatch(/^Thu 1 Jan 2026, 09:00–09:05/);
  });

  it('Page Up and Page Down move by one hour of time, across gaps', async () => {
    // 09:00 to 09:25 (6 windows), a gap, then 10:30 to 11:55 (18 windows).
    const obs = [
      ...[0, 5, 10, 15, 20, 25].map((min) =>
        hrObservation({ start: plusMinutes(START, min), avg: 60 + min / 5, id: `a${String(min)}` }),
      ),
      ...Array.from({ length: 18 }, (_, k) =>
        hrObservation({
          start: plusMinutes(START, 90 + 5 * k),
          avg: 70,
          id: `b${String(90 + 5 * k)}`,
        }),
      ),
    ];
    const m = await loaded(obs);
    m.slider().dispatchEvent(new FocusEvent('focus'));
    m.key('Home');
    m.key('PageUp'); // from 09:02:30 to 10:02:30: nearer 10:32:30 (b90) than 09:27:30 (a25)
    expect(m.slider().getAttribute('aria-valuetext')).toMatch(/10:30–10:35/);
    m.key('PageUp'); // 11:32:30: window b150 (11:30)
    expect(m.slider().getAttribute('aria-valuetext')).toMatch(/11:30–11:35/);
    m.key('PageDown'); // back to 10:32:30
    expect(m.slider().getAttribute('aria-valuetext')).toMatch(/10:30–10:35/);
    m.key('PageDown'); // 09:32:30: nearest is a25 (09:27:30)
    expect(m.slider().getAttribute('aria-valuetext')).toMatch(/09:25–09:30/);
  });
});

describe('the chart does not wait for the status (review minor 9)', () => {
  it('draws when the search answers, and adds the reconnect note when the status arrives', async () => {
    const m = mount((api) => {
      api.holdStatus();
    });
    m.searches[0]?.answer.resolve(threeWindows());
    await settle();
    expect(m.chart.state.model?.points).toHaveLength(3);
    expect(m.slots.panel.querySelector('.hr-message')?.textContent).toBe('');
    m.statuses[0]?.resolve(status('reconnect-needed'));
    await settle();
    expect(m.slots.panel.querySelector('.hr-message')?.textContent).toContain('Reconnect needed');
    expect(m.chart.state.model?.points).toHaveLength(3);
  });
});
