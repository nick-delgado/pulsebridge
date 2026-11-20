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
 * - A `429 rate-limited` (T46; added by T47): the check throws the ApiError, and polling WAITS for
 *   the answer's `Retry-After` (60 s when it has none), then goes on. Nothing is sent while it
 *   waits (polling on would only be refused again), and the wait does not use up the 2 minutes:
 *   they count time spent polling. The page already tells the visitor how long to wait (T45's
 *   `onRateLimited`). At most `POLL_MAX_RATE_LIMIT_WAIT_MS` of waits in all, then it gives up as
 *   it does after 2 minutes. Any other error a check throws stops the polling and rejects.
 * - After a change you caused, notify instead (data-changed.ts); polling is only for waiting.
 */
import { ApiError } from '../api/errors';

export const POLL_INTERVAL_MS = 3000;
export const POLL_MAX_MS = 2 * 60 * 1000;
/** The wait after a 429 whose `Retry-After` is missing or unreadable. */
export const POLL_DEFAULT_RETRY_AFTER_MS = 60 * 1000;
/** All the 429 waits of one polling together, at most (then it gives up, unfinished). */
export const POLL_MAX_RATE_LIMIT_WAIT_MS = 10 * 60 * 1000;

export interface PollOptions {
  signal: AbortSignal;
  intervalMs?: number;
  maxMs?: number;
  /** Clock and visibility, injectable for tests. */
  now?: () => number;
  isVisible?: () => boolean;
}

/**
 * The wait a `429 rate-limited` asks for, in milliseconds (its `Retry-After`, or the default),
 * or null when `error` is not a 429.
 */
export function rateLimitWaitMs(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 429) return null;
  const seconds = error.retryAfterSeconds;
  return seconds === null ? POLL_DEFAULT_RETRY_AFTER_MS : seconds * 1000;
}

/**
 * Calls `check` every interval until it returns true (done), the time limit passes, or the
 * signal aborts. Resolves to true if `check` reported done, false otherwise. A `check` that
 * throws a 429 is called again once its `Retry-After` has passed; one that throws anything else
 * stops the polling and rejects.
 */
export function pollUntil(check: () => Promise<boolean>, options: PollOptions): Promise<boolean> {
  const interval = options.intervalMs ?? POLL_INTERVAL_MS;
  const max = options.maxMs ?? POLL_MAX_MS;
  const now = options.now ?? (() => Date.now());
  const isVisible =
    options.isVisible ??
    (() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
  const started = now();
  // Time spent waiting out 429s: not polling time, so it does not count against `max`.
  let rateLimited = 0;

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
      if (now() - started - rateLimited >= max) {
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
          if (options.signal.aborted) return;
          const wait = rateLimitWaitMs(error);
          if (wait !== null) {
            if (rateLimited + wait > POLL_MAX_RATE_LIMIT_WAIT_MS) {
              stop(false); // refused for too long: give up, the item stays shown as pending
              return;
            }
            const delay = Math.max(wait, interval);
            rateLimited += delay;
            timer = setTimeout(tick, delay);
            return;
          }
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
