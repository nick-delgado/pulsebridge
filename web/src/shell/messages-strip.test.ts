import { describe, expect, it } from 'vitest';
import { messagesStrip } from './messages-strip';

describe('messages strip', () => {
  it('starts collapsed and toggles open and closed with its button', () => {
    const strip = messagesStrip(new AbortController().signal);
    document.body.replaceChildren(strip.root);
    const toggle = strip.root.querySelector('button');
    if (!toggle) throw new Error('no toggle');

    expect(toggle.getAttribute('aria-controls')).toBe(strip.log.id);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(strip.log.hidden).toBe(true);

    toggle.click();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(strip.log.hidden).toBe(false);

    toggle.click();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(strip.log.hidden).toBe(true);
  });

  it('keeps the summary slot inside the toggle so T19 can show counts', () => {
    const strip = messagesStrip(new AbortController().signal);
    expect(strip.root.querySelector('button')?.contains(strip.summary)).toBe(true);
  });
});
