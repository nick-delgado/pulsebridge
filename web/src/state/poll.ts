/**
 * The polling convention for "pending" outcomes (D7): a stored cuff message has no
 * application-level outcome until v2-to-fhir has run, which takes seconds. The feature that SHOWS
 * a pending item polls; nobody else does.
 *
 * - Who: T18 for the reading it just sent (getCuffAttemptTrace), T19 for pending log rows
 *   (listMessages). Each polls only for what it displays.
 * - How often: every 3 s (`POLL_INTERVAL_MS`), only while the tab is visible.
 * - When to stop: as soon as nothing shown is pending, after 2 minutes (`POLL_MAX_MS`; leave the
 *   item shown as "pending", which is itself a real state: the mapper may have failed), or when
 *   the dashboard's signal aborts.
 * - After a change you caused, notify instead (data-changed.ts); polling is only for waiting.
 */

export const POLL_INTERVAL_MS = 3000;
export const POLL_MAX_MS = 2 * 60 * 1000;

export interface PollOptions {
  signal: AbortSignal;
  intervalMs?: number;
  maxMs?: number;
  /** Clock and visibility, injectable for tests. */
  now?: () => number;
  isVisible?: () => boolean;
}

/**
 * Calls `check` every interval until it returns true (done), the time limit passes, or the
 * signal aborts. Resolves to true if `check` reported done, false otherwise. A `check` that
 * throws stops the polling and rejects.
 */
export function pollUntil(check: () => Promise<boolean>, options: PollOptions): Promise<boolean> {
  const interval = options.intervalMs ?? POLL_INTERVAL_MS;
  const max = options.maxMs ?? POLL_MAX_MS;
  const now = options.now ?? (() => Date.now());
  const isVisible =
    options.isVisible ??
    (() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
  const started = now();

  return new Promise((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stop = (result: boolean) => {
      clearTimeout(timer);
      options.signal.removeEventListener('abort', onAbort);
      resolve(result);
    };
    const onAbort = () => {
      stop(false);
    };
    const tick = () => {
      if (options.signal.aborted) return;
      if (now() - started >= max) {
        stop(false);
        return;
      }
      if (!isVisible()) {
        timer = setTimeout(tick, interval);
        return;
      }
      check().then(
        (done) => {
          if (options.signal.aborted) return;
          if (done) stop(true);
          else timer = setTimeout(tick, interval);
        },
        (error: unknown) => {
          clearTimeout(timer);
          options.signal.removeEventListener('abort', onAbort);
          reject(error instanceof Error ? error : new Error(String(error)));
        },
      );
    };
    if (options.signal.aborted) {
      resolve(false);
      return;
    }
    options.signal.addEventListener('abort', onAbort, { once: true });
    timer = setTimeout(tick, interval);
  });
}
