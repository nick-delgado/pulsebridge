import { describe, expect, it, vi } from 'vitest';
import { createSelectionStore, shareableObservation, type Selection } from './selection';

const watch: Selection = { source: 'watch', observationId: 'hr-1', summary: 'HR 60' };
const cuff: Selection = { source: 'cuff', attemptId: 'a-1', observationId: 'bp-1', summary: 'BP' };

describe('selection store', () => {
  it('calls a new subscriber at once, then on every change', () => {
    const store = createSelectionStore();
    const seen: (Selection | null)[] = [];
    store.subscribe((s) => seen.push(s));
    store.set(watch);
    store.set(cuff);
    store.set(null);
    expect(seen).toEqual([null, watch, cuff, null]);
    expect(store.get()).toBeNull();
  });

  it('does not notify when the same item is selected again', () => {
    const store = createSelectionStore();
    const listener = vi.fn();
    store.set(watch);
    store.subscribe(listener);
    store.set({ ...watch });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('one selection for the page: a cuff reading replaces a heart-rate point', () => {
    const store = createSelectionStore();
    store.set(watch);
    store.set(cuff);
    expect(store.get()).toEqual(cuff);
  });

  it('unsubscribes with the returned function or when the signal aborts', () => {
    const store = createSelectionStore();
    const a = vi.fn();
    const b = vi.fn();
    const off = store.subscribe(a);
    const controller = new AbortController();
    store.subscribe(b, controller.signal);
    off();
    controller.abort();
    store.set(watch);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('shareableObservation gives the Observation reference, or null when there is none', () => {
    expect(shareableObservation(null)).toBeNull();
    expect(shareableObservation(watch)).toBe('Observation/hr-1');
    expect(shareableObservation(cuff)).toBe('Observation/bp-1');
    expect(shareableObservation({ ...cuff, observationId: null })).toBeNull();
  });
});
