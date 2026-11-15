/**
 * The header's status strip (brief, "Single page"): last watch sync, Observation count per source,
 * and whether the FHIR store and OpenEMR are reachable. When the Google Health token has expired
 * or been revoked it says "Reconnect needed" instead of a sync time. Every state is spelled out in
 * words, not only by colour.
 */
import type { Reachability, StatusSummary } from '../api/contract';
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
  const sync = status.watchSync;
  const syncItem =
    sync.state === 'reconnect-needed'
      ? item(
          'warn',
          'Watch sync',
          'Reconnect needed',
          sync.detail ?? 'The Google Health token has expired.',
        )
      : sync.state === 'never-synced' || sync.lastSyncAt === null
        ? item('neutral', 'Watch sync', 'Never synced')
        : item(
            sync.state === 'error' ? 'warn' : 'ok',
            'Last sync',
            relativeTime(sync.lastSyncAt, now),
            sync.state === 'error' ? (sync.detail ?? 'The last sync failed.') : undefined,
            sync.lastSyncAt,
          );
  const fmt = new Intl.NumberFormat();
  return [
    syncItem,
    item(
      'neutral',
      'Observations',
      `${fmt.format(status.observationCounts.watch)} watch · ${fmt.format(status.observationCounts.cuff)} cuff`,
    ),
    reach('FHIR store', status.fhirStore),
    reach('OpenEMR', status.openemr),
  ];
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
