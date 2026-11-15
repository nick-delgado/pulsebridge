/**
 * "What is selected": the one shared piece of state on the page.
 *
 * The chart (T17) and the readings list (T18) set it; the trace panels and the Share with clinic
 * button (T19) read it. There is one selection for the whole page, so selecting a cuff reading
 * replaces a selected heart-rate point and the other side can drop its highlight.
 *
 * Deliberately tiny and framework-free: `get`, `set`, `subscribe`. No library is worth its
 * weight for one value.
 */

export type Selection =
  | {
      source: 'watch';
      /** FHIR store id of the heart-rate Observation (one five-minute window). */
      observationId: string;
      /** Short text for the share area, for example "Heart rate 68 /min, 09:05-09:10". */
      summary: string;
      /**
       * Null when the Observation may be shared. Otherwise the reason it may not, shown next to
       * the disabled button. T06/T15 share only `final` Observations, so T17 sets this for a
       * window whose status is not `final` (the newest windows are `preliminary`).
       */
      unshareableReason: string | null;
    }
  | {
      source: 'cuff';
      /** AuditEvent id of the send attempt (the key for the trace lookup). */
      attemptId: string;
      /** The resulting blood-pressure Observation, or null when nothing was stored or mapped. */
      observationId: string | null;
      summary: string;
      /** As for a heart-rate point; null when the reading's Observation may be shared. */
      unshareableReason: string | null;
    };

export type SelectionListener = (selection: Selection | null) => void;

export interface SelectionStore {
  get(): Selection | null;
  /** Replaces the selection (null clears it) and notifies listeners if it changed. */
  set(selection: Selection | null): void;
  /**
   * Calls `listener` now with the current value, then after every change. Returns a function
   * that unsubscribes; pass `signal` instead to unsubscribe when the panel is torn down.
   */
  subscribe(listener: SelectionListener, signal?: AbortSignal): () => void;
}

export function createSelectionStore(): SelectionStore {
  let current: Selection | null = null;
  const listeners = new Set<SelectionListener>();

  return {
    get: () => current,
    set(next) {
      if (sameSelection(current, next)) return;
      current = next;
      // Copy first, so a listener that unsubscribes during the loop does not skip another one.
      for (const listener of [...listeners]) listener(current);
    },
    subscribe(listener, signal) {
      const unsubscribe = () => listeners.delete(listener);
      if (signal?.aborted) return unsubscribe;
      listeners.add(listener);
      signal?.addEventListener('abort', unsubscribe, { once: true });
      listener(current);
      return unsubscribe;
    },
  };
}

function sameSelection(a: Selection | null, b: Selection | null): boolean {
  if (a === null || b === null) return a === b;
  if (a.summary !== b.summary || a.unshareableReason !== b.unshareableReason) return false;
  if (a.source === 'watch' && b.source === 'watch') return a.observationId === b.observationId;
  if (a.source === 'cuff' && b.source === 'cuff') {
    return a.attemptId === b.attemptId && a.observationId === b.observationId;
  }
  return false;
}

/**
 * The Observation the Share button would send for this selection, or null if there is none:
 * nothing selected, a cuff reading that produced no Observation, or a selection marked
 * not shareable (`unshareableReason`).
 */
export function shareableObservation(selection: Selection | null): string | null {
  if (selection === null || selection.observationId === null) return null;
  if (selection.unshareableReason !== null) return null;
  return `Observation/${selection.observationId}`;
}
