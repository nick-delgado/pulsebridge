/**
 * The collapsible messages-log strip at the bottom of the page. The shell owns the strip and its
 * toggle; T19 fills the `log` slot (rows) and the `summary` slot (text in the toggle).
 * Disclosure pattern (WAI-ARIA APG): a button with `aria-expanded` controlling the region.
 */
import { el } from '../ui/dom';

export interface MessagesStrip {
  root: HTMLElement;
  log: HTMLElement;
  summary: HTMLElement;
  setExpanded(expanded: boolean): void;
  isExpanded(): boolean;
}

export function messagesStrip(signal: AbortSignal): MessagesStrip {
  const summary = el('span', {
    class: 'messages-strip__summary',
    attrs: { 'data-slot': 'messages-summary' },
  });
  const log = el('div', {
    id: 'messages-log',
    class: 'messages-strip__body',
    attrs: { 'data-slot': 'messages-log', role: 'region', 'aria-label': 'Messages log entries' },
  });
  const chevron = el('span', {
    class: 'messages-strip__chevron',
    attrs: { 'aria-hidden': 'true' },
  });
  const toggle = el(
    'button',
    {
      class: 'messages-strip__toggle',
      attrs: { type: 'button', 'aria-controls': 'messages-log', 'aria-expanded': 'false' },
      on: {
        click: () => {
          setExpanded(!isExpanded());
        },
      },
      signal,
    },
    chevron,
    el('span', { class: 'messages-strip__title', text: 'Messages log' }),
    summary,
  );
  const root = el(
    'section',
    { class: 'messages-strip', attrs: { 'aria-labelledby': 'messages-heading' } },
    el('h2', { id: 'messages-heading', class: 'messages-strip__heading' }, toggle),
    log,
  );

  function isExpanded(): boolean {
    return toggle.getAttribute('aria-expanded') === 'true';
  }
  function setExpanded(expanded: boolean): void {
    toggle.setAttribute('aria-expanded', String(expanded));
    log.hidden = !expanded;
    root.classList.toggle('messages-strip--open', expanded);
  }
  setExpanded(false);
  return { root, log, summary, setExpanded, isExpanded };
}
