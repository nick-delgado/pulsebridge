/**
 * "Something changed on the server": the second piece of shared state, next to the selection.
 *
 * A feature that changes data calls `notify(kind)`; features that show that data listen and
 * RE-FETCH through the client. The event carries no data on purpose: the server (or the mock) is
 * the source of truth, and a listener asks it for exactly the view it shows.
 *
 *   kind         notified by                         typical listeners
 *   'cuff-send'  T18, after submitCuffReading       T19 messages log, the status strip
 *   'share'      T19, after share()                 T19 log (if it shows shares), status strip
 *   'sync'       anyone who learns of a new sync    T17 chart, status strip
 *
 * Same shape as the selection store: framework-free, `subscribe` returns an unsubscribe function
 * and also accepts an AbortSignal. Unlike the selection, a new listener is NOT called at once.
 */

export type DataChangeKind = 'cuff-send' | 'share' | 'sync';

export type DataChangeListener = (kind: DataChangeKind) => void;

export interface DataChangeBus {
  notify(kind: DataChangeKind): void;
  subscribe(listener: DataChangeListener, signal?: AbortSignal): () => void;
}

export function createDataChangeBus(): DataChangeBus {
  const listeners = new Set<DataChangeListener>();
  return {
    notify(kind) {
      for (const listener of [...listeners]) {
        try {
          listener(kind);
        } catch (error) {
          // One failing listener must not stop the others from refreshing.
          console.error('A data-change listener failed', error);
        }
      }
    },
    subscribe(listener, signal) {
      const unsubscribe = () => listeners.delete(listener);
      if (signal?.aborted) return unsubscribe;
      listeners.add(listener);
      signal?.addEventListener('abort', unsubscribe, { once: true });
      return unsubscribe;
    },
  };
}
