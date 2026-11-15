import { describe, expect, it } from 'vitest';
import { createTabs } from './tabs';

function setup() {
  const tabs = createTabs('Trace', [
    { id: 'fhir', label: 'FHIR as stored' },
    { id: 'source', label: 'Google Health JSON' },
    { id: 'third', label: 'Third' },
  ]);
  document.body.replaceChildren(tabs.root);
  const buttons = [...tabs.root.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  return { tabs, buttons };
}

const key = (el: HTMLElement, k: string) =>
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));

describe('tabs', () => {
  it('gives every tab and panel a unique `ui-` id (README, "Element ids")', () => {
    const a = setup().tabs.root;
    const b = createTabs('Other', [{ id: 'fhir', label: 'FHIR' }]).root;
    const ids = [...a.querySelectorAll('[id]'), ...b.querySelectorAll('[id]')].map((e) => e.id);
    expect(ids).toHaveLength(8);
    expect(ids.every((id) => id.startsWith('ui-'))).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('shows the first panel and links each tab to its panel', () => {
    const { tabs, buttons } = setup();
    expect(tabs.selected()).toBe('fhir');
    expect(tabs.panel('fhir').hidden).toBe(false);
    expect(tabs.panel('source').hidden).toBe(true);
    expect(buttons[0]?.getAttribute('aria-controls')).toBe(tabs.panel('fhir').id);
    expect(buttons.map((b) => b.tabIndex)).toEqual([0, -1, -1]);
  });

  it('moves with arrow keys, Home and End, wrapping around', () => {
    const { tabs, buttons } = setup();
    const [first] = buttons;
    if (!first) throw new Error('no tab');
    key(first, 'ArrowRight');
    expect(tabs.selected()).toBe('source');
    expect(document.activeElement).toBe(buttons[1]);
    key(first, 'End');
    expect(tabs.selected()).toBe('third');
    key(first, 'ArrowRight');
    expect(tabs.selected()).toBe('fhir');
    key(first, 'ArrowLeft');
    expect(tabs.selected()).toBe('third');
    expect(tabs.panel('third').hidden).toBe(false);
    expect(buttons[2]?.getAttribute('aria-selected')).toBe('true');
  });

  it('selects on click', () => {
    const { tabs, buttons } = setup();
    buttons[1]?.click();
    expect(tabs.selected()).toBe('source');
  });
});
