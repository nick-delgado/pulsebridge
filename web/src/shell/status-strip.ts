/**
 * The header's status strip (brief, "Single page"): last watch sync, Observation count per source,
 * and whether the FHIR store and OpenEMR are reachable. The watch part follows health-sync's
 * `/status` (services/health-sync/README.md): "Reconnect needed" or "Not connected" replace the
 * sync time when the Google Health connection cannot work, an `unknown` check keeps the last sync
 * time but says the check did not finish, and the `backfill-needed` and `refresh-token-rotated`
 * notes are shown as items of their own. Every state is spelled out in words, not only by colour.
 */
import type { Reachability, StatusSummary, WatchSyncStatus } from '../api/contract';
import { el, replaceChildren } from '../ui/dom';

export function statusStrip(): { root: HTMLElement; render(state: StripState): void } {
  const list = el('ul', { class: 'status-strip__items' });
  const root = el(
    'section',
    { class: 'status-strip', attrs: { 'aria-label': 'System status' } },
    list,
  );
  return {
    root,
    render(state) {
      replaceChildren(list, ...items(state));
    },
  };
}

export type StripState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: StatusSummary; now: number };

function items(state: StripState): HTMLLIElement[] {
  if (state.kind === 'loading') return [item('neutral', 'Status', 'Loading…')];
  if (state.kind === 'error') return [item('warn', 'Status', `Unavailable: ${state.message}`)];
  const { status, now } = state;
  const fmt = new Intl.NumberFormat();
  return [
    syncItem(status.watchSync, now),
    ...noteItems(status.watchSync),
    item(
      'neutral',
      'Observations',
      `${fmt.format(status.observationCounts.watch)} watch · ${fmt.format(status.observationCounts.cuff)} cuff`,
    ),
    reach('FHIR store', status.fhirStore),
    reach('OpenEMR', status.openemr),
  ];
}

/** health-sync's reason code as detail text, for example "(refresh-token-invalid)". */
const because = (reason: string | null) => (reason ? ` (${reason})` : '');

function syncItem(sync: WatchSyncStatus, now: number): HTMLLIElement {
  if (sync.state === 'reconnect-needed') {
    return item(
      'warn',
      'Watch sync',
      'Reconnect needed',
      `The Google Health connection no longer works${because(sync.reason)}: re-run the connect script.`,
    );
  }
  if (sync.state === 'not-configured') {
    return item(
      'warn',
      'Watch sync',
      'Not connected',
      `Google Health is not set up on the server${because(sync.reason)}.`,
    );
  }
  // `unknown` is not a failure of the connection: the check could not finish (for example
  // Google's token endpoint was busy), so the last sync time still stands.
  const unknown =
    sync.state === 'unknown'
      ? `The connection check could not finish${because(sync.reason)}; it is retried on the next load.`
      : undefined;
  if (sync.lastSyncAt === null) {
    return item('neutral', 'Watch sync', 'Never synced', unknown);
  }
  return item(
    unknown ? 'neutral' : 'ok',
    'Last sync',
    relativeTime(sync.lastSyncAt, now),
    unknown,
    sync.lastSyncAt,
  );
}

function noteItems(sync: WatchSyncStatus): HTMLLIElement[] {
  const out: HTMLLIElement[] = [];
  if (sync.notes.includes('backfill-needed')) {
    const windows =
      sync.strandedPreliminary === null
        ? 'Some windows'
        : `${new Intl.NumberFormat().format(sync.strandedPreliminary)} windows`;
    out.push(
      item(
        'warn',
        'Watch data',
        'Backfill needed',
        `${windows} from missed sync runs are still provisional; running the backfill finalises them.`,
      ),
    );
  }
  if (sync.notes.includes('refresh-token-rotated') && sync.state !== 'reconnect-needed') {
    out.push(
      item(
        'warn',
        'Watch sync',
        'Reconnect needed soon',
        'Google issued a new refresh token that the server cannot store; reconnect soon to keep syncing.',
      ),
    );
  }
  return out;
}

function reach(label: string, r: Reachability): HTMLLIElement {
  return r.reachable
    ? item('ok', label, 'Reachable')
    : item('warn', label, 'Unreachable', r.detail ?? undefined);
}

function item(
  tone: 'ok' | 'warn' | 'neutral',
  label: string,
  value: string,
  detail?: string,
  datetime?: string,
): HTMLLIElement {
  const valueNode = datetime
    ? el('time', { class: 'status-item__value', text: value, attrs: { datetime } })
    : el('span', { class: 'status-item__value', text: value });
  return el(
    'li',
    { class: `status-item status-item--${tone}`, attrs: { title: detail } },
    el('span', { class: 'status-item__dot', attrs: { 'aria-hidden': 'true' } }),
    el('span', { class: 'status-item__label', text: `${label}: ` }),
    valueNode,
    detail ? el('span', { class: 'visually-hidden', text: ` (${detail})` }) : null,
  );
}

/** "just now", "7 min ago", "3 h ago", "2 d ago". */
export function relativeTime(iso: string, now: number): string {
  const minutes = Math.round((now - Date.parse(iso)) / 60000);
  if (!Number.isFinite(minutes)) return 'unknown';
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
