/**
 * The dashboard's page structure: header with the status strip, then (on a laptop) three
 * columns: the episodic cuff side on the left, the figure and the share area in the centre, the
 * continuous watch side on the right; and the messages-log strip pinned to the bottom. On a phone
 * the same elements stack: figure, cuff side, watch side, share, log. The DOM order is that phone
 * order, so keyboard order is left side, right side, then share, at every width.
 *
 * Landmarks: banner (header), main, one region per side and for the share area and the log.
 */
import { el } from '../ui/dom';
import type { CuffSlot, HeartRateSlot, MessagesSlot, ShareSlot } from './features';
import { messagesStrip, type MessagesStrip } from './messages-strip';
import { silhouette } from './silhouette';
import { statusStrip } from './status-strip';

export interface DashboardLayout {
  root: DocumentFragment;
  status: ReturnType<typeof statusStrip>;
  logoutButton: HTMLButtonElement;
  messages: MessagesStrip;
  slots: {
    heartRate: Record<HeartRateSlot, HTMLElement>;
    cuff: Record<CuffSlot, HTMLElement>;
    share: Record<ShareSlot, HTMLElement>;
    messages: Record<MessagesSlot, HTMLElement>;
  };
}

function slot(name: string, className: string): HTMLElement {
  return el('div', { class: className, attrs: { 'data-slot': name } });
}

/** One side of the page: heading, the feature's main panel, and its trace panel. */
function side(opts: {
  key: 'cuff' | 'watch';
  title: string;
  kind: string;
  panel: HTMLElement;
  trace: HTMLElement;
  /** Prefix for the trace region's accessible name, for example "Blood-pressure". */
  traceName: string;
}): HTMLElement {
  const headingId = `${opts.key}-heading`;
  const traceHeadingId = `${opts.key}-trace-heading`;
  return el(
    'section',
    { class: `side side--${opts.key}`, attrs: { 'aria-labelledby': headingId } },
    el(
      'header',
      { class: 'side__header' },
      el('h2', { id: headingId, class: 'side__title', text: opts.title }),
      el('p', { class: 'side__kind', text: opts.kind }),
    ),
    el('div', { class: 'card side__panel' }, opts.panel),
    el(
      'section',
      { class: 'card trace', attrs: { 'aria-labelledby': traceHeadingId } },
      // Visible text "Trace"; the hidden prefix gives each region a distinct accessible name
      // ("Blood-pressure trace", "Heart-rate trace") in a screen reader's landmark list.
      el(
        'h3',
        { id: traceHeadingId, class: 'trace__title' },
        el('span', { class: 'visually-hidden', text: `${opts.traceName} ` }),
        'Trace',
      ),
      opts.trace,
    ),
  );
}

export function dashboardLayout(options: {
  mode: 'mock' | 'http';
  signal: AbortSignal;
}): DashboardLayout {
  const status = statusStrip();
  const logoutButton = el('button', {
    class: 'button button--quiet',
    text: 'Log out',
    attrs: { type: 'button' },
  });
  const messages = messagesStrip(options.signal);

  const slots = {
    cuff: { panel: slot('cuff-panel', 'slot'), trace: slot('cuff-trace', 'slot') },
    heartRate: { panel: slot('heart-rate-panel', 'slot'), trace: slot('heart-rate-trace', 'slot') },
    share: {
      action: slot('share-action', 'slot share__action'),
      trace: slot('share-trace', 'slot'),
      readback: slot('share-readback', 'slot'),
    },
    messages: { log: messages.log, summary: messages.summary },
  };

  const header = el(
    'header',
    { class: 'app-header' },
    el(
      'div',
      { class: 'app-header__brand' },
      el('p', { class: 'app-header__name', text: 'PulseBridge' }),
      options.mode === 'mock'
        ? el('span', {
            class: 'mode-badge',
            text: 'Mock data',
            attrs: { title: 'Synthetic data, no server' },
          })
        : null,
    ),
    status.root,
    logoutButton,
  );

  const figure = el(
    'figure',
    { class: 'body-figure', attrs: { 'aria-label': 'Where the data comes from' } },
    silhouette(),
    el(
      'figcaption',
      { class: 'body-figure__caption' },
      el('span', { class: 'body-figure__left', text: '← cuff: episodic' }),
      el('span', { class: 'body-figure__right', text: 'watch: continuous →' }),
    ),
  );

  const share = el(
    'section',
    { class: 'share', attrs: { 'aria-labelledby': 'share-heading' } },
    // The button carries the visible label; the heading names the region for screen readers.
    el('h2', { id: 'share-heading', class: 'visually-hidden', text: 'Share with clinic' }),
    slots.share.action,
    el(
      'section',
      { class: 'card trace', attrs: { 'aria-labelledby': 'share-trace-heading' } },
      el('h3', { id: 'share-trace-heading', class: 'trace__title', text: 'Share trace' }),
      slots.share.trace,
    ),
    el(
      'section',
      { class: 'card trace', attrs: { 'aria-labelledby': 'readback-heading' } },
      el('h3', { id: 'readback-heading', class: 'trace__title', text: 'As stored in OpenEMR' }),
      slots.share.readback,
    ),
  );

  const main = el(
    'main',
    {
      id: 'main',
      class: 'dashboard',
      attrs: { tabindex: '-1', 'aria-label': 'PulseBridge dashboard' },
    },
    el('h1', { class: 'visually-hidden', text: 'PulseBridge personal health record' }),
    figure,
    side({
      key: 'cuff',
      title: 'Blood pressure',
      kind: 'Episodic · simulated cuff · HL7v2',
      traceName: 'Blood-pressure',
      panel: slots.cuff.panel,
      trace: slots.cuff.trace,
    }),
    side({
      key: 'watch',
      title: 'Heart rate',
      kind: 'Continuous · watch · Google Health',
      traceName: 'Heart-rate',
      panel: slots.heartRate.panel,
      trace: slots.heartRate.trace,
    }),
    share,
  );

  const root = document.createDocumentFragment();
  root.append(
    el('a', { class: 'skip-link', text: 'Skip to dashboard', attrs: { href: '#main' } }),
    header,
    main,
    messages.root,
  );
  return { root, status, logoutButton, messages, slots };
}
