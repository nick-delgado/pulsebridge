/**
 * Accessible tabs for the trace panels (T17: two tabs, T18: three), following the WAI-ARIA
 * Authoring Practices "Tabs" pattern: a `tablist` of `tab` buttons, each controlling one
 * `tabpanel`; Left/Right arrows, Home and End move between tabs; only the selected tab is in the
 * Tab order.
 */
import { el, uniqueId } from './dom';

export interface TabSpec {
  id: string;
  label: string;
}

export interface Tabs {
  /** The element to insert into the page. */
  root: HTMLElement;
  /** The panel element for a tab id, to fill with content. */
  panel(id: string): HTMLElement;
  select(id: string): void;
  selected(): string;
}

export function createTabs(label: string, specs: TabSpec[], signal?: AbortSignal): Tabs {
  if (specs.length === 0) throw new Error('createTabs needs at least one tab');
  const prefix = uniqueId('tabs');
  const list = el('div', { class: 'tabs__list', attrs: { role: 'tablist', 'aria-label': label } });
  const root = el('div', { class: 'tabs' }, list);
  const buttons = new Map<string, HTMLButtonElement>();
  const panels = new Map<string, HTMLElement>();
  let current = specs[0]?.id ?? '';

  const select = (id: string, focus = false) => {
    if (!buttons.has(id)) throw new Error(`No tab ${id}`);
    current = id;
    for (const [tabId, button] of buttons) {
      const on = tabId === id;
      button.setAttribute('aria-selected', String(on));
      button.tabIndex = on ? 0 : -1;
      const panel = panels.get(tabId);
      if (panel) panel.hidden = !on;
    }
    if (focus) buttons.get(id)?.focus();
  };

  const ids = specs.map((s) => s.id);
  const onKey = (event: KeyboardEvent) => {
    const i = ids.indexOf(current);
    const next: Record<string, number> = {
      ArrowRight: (i + 1) % ids.length,
      ArrowLeft: (i - 1 + ids.length) % ids.length,
      Home: 0,
      End: ids.length - 1,
    };
    const target = next[event.key];
    if (target === undefined) return;
    event.preventDefault();
    select(ids[target] ?? current, true);
  };

  for (const spec of specs) {
    const tabId = `${prefix}-tab-${spec.id}`;
    const panelId = `${prefix}-panel-${spec.id}`;
    const button = el('button', {
      class: 'tabs__tab',
      id: tabId,
      text: spec.label,
      attrs: { type: 'button', role: 'tab', 'aria-controls': panelId },
      on: {
        click: () => {
          select(spec.id);
        },
        keydown: onKey,
      },
      ...(signal ? { signal } : {}),
    });
    const panel = el('div', {
      class: 'tabs__panel',
      id: panelId,
      attrs: { role: 'tabpanel', 'aria-labelledby': tabId, tabindex: '0' },
    });
    buttons.set(spec.id, button);
    panels.set(spec.id, panel);
    list.appendChild(button);
    root.appendChild(panel);
  }
  select(current);

  return {
    root,
    panel(id) {
      const panel = panels.get(id);
      if (!panel) throw new Error(`No tab ${id}`);
      return panel;
    },
    select: (id) => {
      select(id);
    },
    selected: () => current,
  };
}
