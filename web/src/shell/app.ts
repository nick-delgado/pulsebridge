/**
 * The app's two states, landing and dashboard, and the moves between them (D16):
 *
 *   start ── checkSession: does the proxy accept this browser's __session cookie?
 *     │        active ──▶ dashboard
 *     │        none, or the check failed ──▶ landing ── bot check passes ──▶ dashboard
 *   dashboard ── "Log out" ──▶ POST /auth/logout (clears the cookie) ──▶ landing
 *   dashboard ── any 401 (client calls onUnauthorized) ──▶ landing, "session has ended"
 *   dashboard ── any 429 (client calls onRateLimited) ──▶ stays; a notice says how long to wait
 *
 * The page never holds a token: the cookie is HttpOnly, so script cannot read it, and the only
 * way to know whether it is still good is to ask the proxy.
 *
 * Leaving the dashboard aborts its AbortSignal, so every feature's listeners and subscriptions
 * go with it, and the next session builds a fresh dashboard.
 */
import type { PulseBridgeApi } from '../api/contract';
import type { ClientDeps } from '../api/index';
import {
  ApiError,
  isServiceNotConfigured,
  rateLimitMessage,
  UnauthorizedError,
} from '../api/errors';
import type { BotCheck } from '../bot-check/index';
import { mountCuff } from '../cuff/index';
import { mountHeartRate } from '../heart-rate/index';
import { mountMessages } from '../messages/index';
import { mountShare } from '../share/index';
import { createDataChangeBus } from '../state/data-changed';
import { createSelectionStore } from '../state/selection';
import { el, placeholder, replaceChildren } from '../ui/dom';
import type { FeatureContext, Features, MountFeature } from './features';
import { dashboardLayout } from './layout';
import { landingView, START_LABEL } from './landing';
import { createSyncWatch } from './sync-watch';

export const SESSION_ENDED = `Your demo session has ended. Select "${START_LABEL}" to start a new one.`;

/** How long the rate-limit notice stays when the proxy sent no `Retry-After`. */
export const RATE_LIMIT_NOTICE_DEFAULT_MS = 60 * 1000;

/**
 * How often the status strip reloads while the tab is visible. health-sync runs every 15
 * minutes, so a new sync shows (and is announced) within 5 minutes. Each load is one cheap
 * proxy call; health-sync caches its own answer for 60 s.
 */
export const STATUS_REFRESH_MS = 5 * 60 * 1000;

/** A visibility change reloads the status only if no load started this recently. */
export const VISIBILITY_RELOAD_MIN_GAP_MS = 60 * 1000;

export const DEFAULT_FEATURES: Features = {
  heartRate: mountHeartRate,
  cuff: mountCuff,
  share: mountShare,
  messages: mountMessages,
};

export interface AppOptions {
  root: HTMLElement;
  makeClient: (deps: ClientDeps) => PulseBridgeApi | Promise<PulseBridgeApi>;
  /** reCAPTCHA in an HTTP build, the mock's stand-in in a mock build (bot-check/index.ts). */
  botCheck: BotCheck;
  features?: Features;
  now?: () => number;
}

export interface App {
  view(): 'landing' | 'dashboard';
  client: PulseBridgeApi;
}

export async function startApp(options: AppOptions): Promise<App> {
  const { root } = options;
  const features = options.features ?? DEFAULT_FEATURES;
  const now = options.now ?? (() => Date.now());
  let view: 'landing' | 'dashboard' = 'landing';
  let teardown: AbortController | null = null;
  /** Shows the rate-limit notice on the dashboard; null while there is no dashboard. */
  let rateLimited: ((retryAfterSeconds: number | null) => void) | null = null;

  const client = await options.makeClient({
    onUnauthorized: () => {
      showLanding(SESSION_ENDED);
    },
    onRateLimited: (seconds) => {
      rateLimited?.(seconds);
    },
  });

  function leaveDashboard(): void {
    teardown?.abort();
    teardown = null;
    rateLimited = null;
  }

  function showLanding(notice?: string): void {
    if (view === 'landing' && root.childElementCount > 0) {
      // Several calls can fail with 401 at once; show the landing view once.
      return;
    }
    leaveDashboard();
    view = 'landing';
    replaceChildren(
      root,
      landingView({
        client,
        botCheck: options.botCheck,
        ...(notice ? { notice } : {}),
        onSuccess: showDashboard,
      }),
    );
    document.getElementById('landing-start')?.focus();
  }

  function showDashboard(): void {
    leaveDashboard();
    const controller = new AbortController();
    teardown = controller;
    view = 'dashboard';
    const { signal } = controller;
    const layout = dashboardLayout({ mode: client.mode, signal });
    const selection = createSelectionStore();

    // T46: any route can answer 429 `rate-limited`. The visitor stays on the dashboard; the
    // notice says how long to wait and goes once the wait is over. Each feature also shows its
    // own error for the call that was refused.
    let noticeTimer: ReturnType<typeof setTimeout> | undefined;
    rateLimited = (seconds) => {
      layout.notice.textContent = rateLimitMessage(seconds);
      clearTimeout(noticeTimer);
      noticeTimer = setTimeout(
        () => {
          layout.notice.textContent = '';
        },
        seconds === null ? RATE_LIMIT_NOTICE_DEFAULT_MS : seconds * 1000,
      );
    };
    signal.addEventListener('abort', () => {
      clearTimeout(noticeTimer);
    });
    const dataChanges = createDataChangeBus();

    // The status strip. Each load replaces the last one's answer; a load that answers after a
    // newer one started is ignored, so an old answer can neither overwrite a newer one nor make
    // the sync check below compare against a stale time.
    const syncWatch = createSyncWatch();
    let statusLoads = 0;
    let shown = false;
    let announcing = false;
    let lastLoadStartedAt = Number.NEGATIVE_INFINITY;
    const refreshStatus = () => {
      const load = ++statusLoads;
      lastLoadStartedAt = now();
      // "Loading…" only before the first answer; a reload keeps the last answer on screen.
      if (!shown) layout.status.render({ kind: 'loading' });
      client.getStatus({ signal }).then(
        (status) => {
          if (signal.aborted || load !== statusLoads) return;
          shown = true;
          layout.status.render({ kind: 'ready', status, now: now() });
          // A new sync since the last load: tell the features, so they re-fetch (T17's chart
          // reloads and a window that was provisional can become final). Our own listener below
          // skips this announcement, so it cannot loop: and a reload with the same time is not
          // a change anyway.
          if (syncWatch.changed(status.watchSync.lastSyncAt)) {
            announcing = true;
            try {
              dataChanges.notify('sync');
            } finally {
              announcing = false;
            }
          }
        },
        (error: unknown) => {
          if (signal.aborted || load !== statusLoads || error instanceof UnauthorizedError) return;
          shown = false;
          // A proxy that cannot check sessions answers 503 everywhere (amendment 11); the
          // session stays.
          layout.status.render(
            isServiceNotConfigured(error)
              ? { kind: 'not-configured' }
              : {
                  kind: 'error',
                  message: error instanceof Error ? error.message : 'unknown error',
                },
          );
        },
      );
    };

    // Reload the status every few minutes while the tab is visible, and when it becomes visible
    // again, so a sync run (every 15 minutes) is noticed during a session without any action.
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refreshStatus();
    }, STATUS_REFRESH_MS);
    signal.addEventListener('abort', () => {
      clearInterval(timer);
    });
    document.addEventListener(
      'visibilitychange',
      () => {
        // Coming back to the tab reloads, unless a load started within the last minute
        // (switching tabs back and forth must not hammer the proxy).
        if (
          document.visibilityState === 'visible' &&
          now() - lastLoadStartedAt >= VISIBILITY_RELOAD_MIN_GAP_MS
        ) {
          refreshStatus();
        }
      },
      { signal },
    );

    layout.logoutButton.addEventListener(
      'click',
      () => {
        // Abort first: every request the dashboard still has in flight is cancelled before the
        // session ends, so none of them can answer into the next one. The logout request itself
        // is not tied to the dashboard, so it is not cancelled with it.
        leaveDashboard();
        void client.logout();
        showLanding();
      },
      { signal },
    );

    const mount = <S extends string>(
      name: string,
      fn: MountFeature<S>,
      slots: Record<S, HTMLElement>,
    ) => {
      const context: FeatureContext<S> = {
        client,
        selection,
        slots,
        signal,
        refreshStatus,
        notifyDataChanged: (kind) => {
          dataChanges.notify(kind);
        },
        onDataChanged: (listener, listenerSignal) => {
          dataChanges.subscribe(listener, listenerSignal);
        },
      };
      try {
        fn(context);
      } catch (error) {
        // One broken area must not take the page down; say so in its first slot.
        console.error(`Mounting ${name} failed`, error);
        const firstSlot = Object.values<HTMLElement>(slots)[0];
        if (firstSlot)
          replaceChildren(firstSlot, placeholder(`This area failed to load (${name}).`));
      }
    };

    // The status strip shows counts and the last sync, so any change refreshes it, except the
    // strip's own 'sync' announcement (it has just loaded the status).
    dataChanges.subscribe((kind) => {
      // Only the strip's own 'sync' is skipped: any other change, even one a 'sync' listener
      // reports while the announcement is running, reloads the strip.
      if (!(announcing && kind === 'sync')) refreshStatus();
    }, signal);

    replaceChildren(root, layout.root);
    mount('heart rate', features.heartRate, layout.slots.heartRate);
    mount('cuff', features.cuff, layout.slots.cuff);
    mount('share', features.share, layout.slots.share);
    mount('messages', features.messages, layout.slots.messages);
    refreshStatus();
  }

  // A reload keeps the cookie, so a visitor who already passed the bot check goes straight back
  // to the dashboard. If the proxy cannot be asked, the landing view is the safe place: starting
  // a new session there works either way.
  let notice: string | undefined;
  let state: 'active' | 'none' = 'none';
  try {
    state = await client.checkSession();
  } catch (error) {
    if (error instanceof ApiError && error.status === 429) {
      notice = rateLimitMessage(error.retryAfterSeconds);
    }
  }
  if (state === 'active') showDashboard();
  else showLanding(notice);

  return { view: () => view, client };
}

/** Shown if the app cannot start at all (for example, the root element is missing). */
export function fatal(root: HTMLElement, message: string): void {
  replaceChildren(root, el('p', { class: 'placeholder', text: message, attrs: { role: 'alert' } }));
}
