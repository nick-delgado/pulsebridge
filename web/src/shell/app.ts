/**
 * The app's two states, login and dashboard, and the moves between them:
 *
 *   start ── token in sessionStorage? ── yes ──▶ dashboard
 *     │                                  no ───▶ login ── password OK ──▶ dashboard
 *   dashboard ── "Log out" ──▶ clear token ──▶ login
 *   dashboard ── any 401 (client calls onUnauthorized) ──▶ token already cleared ──▶ login
 *
 * Leaving the dashboard aborts its AbortSignal, so every feature's listeners and subscriptions
 * go with it, and the next login builds a fresh dashboard.
 */
import type { PulseBridgeApi } from '../api/contract';
import type { ClientDeps } from '../api/index';
import { Session } from '../api/session';
import { UnauthorizedError } from '../api/errors';
import { mountCuff } from '../cuff/index';
import { mountHeartRate } from '../heart-rate/index';
import { mountMessages } from '../messages/index';
import { mountShare } from '../share/index';
import { createDataChangeBus } from '../state/data-changed';
import { createSelectionStore } from '../state/selection';
import { el, placeholder, replaceChildren } from '../ui/dom';
import type { FeatureContext, Features, MountFeature } from './features';
import { dashboardLayout } from './layout';
import { loginView } from './login';
import { createSyncWatch } from './sync-watch';

export const SESSION_ENDED = 'Your session has ended. Log in again.';

/**
 * How often the status strip reloads while the tab is visible. health-sync runs every 15
 * minutes, so a new sync shows (and is announced) within 5 minutes. Each load is one cheap
 * proxy call; health-sync caches its own answer for 60 s.
 */
export const STATUS_REFRESH_MS = 5 * 60 * 1000;

export const DEFAULT_FEATURES: Features = {
  heartRate: mountHeartRate,
  cuff: mountCuff,
  share: mountShare,
  messages: mountMessages,
};

export interface AppOptions {
  root: HTMLElement;
  makeClient: (deps: ClientDeps) => PulseBridgeApi | Promise<PulseBridgeApi>;
  session?: Session;
  features?: Features;
  now?: () => number;
}

export interface App {
  view(): 'login' | 'dashboard';
  client: PulseBridgeApi;
}

export async function startApp(options: AppOptions): Promise<App> {
  const { root } = options;
  const session = options.session ?? new Session();
  const features = options.features ?? DEFAULT_FEATURES;
  const now = options.now ?? (() => Date.now());
  let view: 'login' | 'dashboard' = 'login';
  let teardown: AbortController | null = null;

  const client = await options.makeClient({
    session,
    onUnauthorized: () => {
      showLogin(SESSION_ENDED);
    },
  });

  function leaveDashboard(): void {
    teardown?.abort();
    teardown = null;
  }

  function showLogin(notice?: string): void {
    if (view === 'login' && root.childElementCount > 0) {
      // Several calls can fail with 401 at once; show the login form once.
      return;
    }
    leaveDashboard();
    view = 'login';
    replaceChildren(
      root,
      loginView({
        client,
        ...(notice ? { notice } : {}),
        onSuccess: showDashboard,
      }),
    );
    document.getElementById('login-password')?.focus();
  }

  function showDashboard(): void {
    leaveDashboard();
    const controller = new AbortController();
    teardown = controller;
    view = 'dashboard';
    const { signal } = controller;
    const layout = dashboardLayout({ mode: client.mode, signal });
    const selection = createSelectionStore();
    const dataChanges = createDataChangeBus();

    // The status strip. Each load replaces the last one's answer; a load that answers after a
    // newer one started is ignored, so an old answer can neither overwrite a newer one nor make
    // the sync check below compare against a stale time.
    const syncWatch = createSyncWatch();
    let statusLoads = 0;
    let shown = false;
    let announcing = false;
    const refreshStatus = () => {
      const load = ++statusLoads;
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
          layout.status.render({
            kind: 'error',
            message: error instanceof Error ? error.message : 'unknown error',
          });
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
        if (document.visibilityState === 'visible') refreshStatus();
      },
      { signal },
    );

    layout.logoutButton.addEventListener(
      'click',
      () => {
        // Abort first: every request the dashboard still has in flight is cancelled before the
        // token goes, so none of them can answer after the next login.
        leaveDashboard();
        client.logout();
        showLogin();
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
    dataChanges.subscribe(() => {
      if (!announcing) refreshStatus();
    }, signal);

    replaceChildren(root, layout.root);
    mount('heart rate', features.heartRate, layout.slots.heartRate);
    mount('cuff', features.cuff, layout.slots.cuff);
    mount('share', features.share, layout.slots.share);
    mount('messages', features.messages, layout.slots.messages);
    refreshStatus();
  }

  if (client.isLoggedIn()) showDashboard();
  else showLogin();

  return { view: () => view, client };
}

/** Shown if the app cannot start at all (for example, the root element is missing). */
export function fatal(root: HTMLElement, message: string): void {
  replaceChildren(root, el('p', { class: 'placeholder', text: message, attrs: { role: 'alert' } }));
}
