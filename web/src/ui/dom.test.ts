import { describe, expect, it } from 'vitest';
import {
  append,
  assertSafeAttribute,
  codeBlock,
  el,
  isSafeUrl,
  placeholder,
  replaceChildren,
  svg,
} from './dom';
import { createTabs } from './tabs';

const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script><b>bold</b>';

describe('rendering never parses markup', () => {
  it('el(): props.text and string children are text', () => {
    const fromText = el('p', { text: HOSTILE });
    const fromChild = el('p', {}, HOSTILE);
    for (const node of [fromText, fromChild]) {
      expect(node.textContent).toBe(HOSTILE);
      expect(node.children).toHaveLength(0);
    }
  });

  it('every public helper: a hostile string creates no element beyond the expected ones', () => {
    const host = document.createElement('div');
    const tabs = createTabs(HOSTILE, [{ id: 'a', label: HOSTILE }]);
    tabs.panel('a').append(HOSTILE);
    const title = svg('title');
    title.append(HOSTILE);
    const replaced = el('div');
    replaceChildren(replaced, HOSTILE);
    const appended = el('div');
    append(appended, HOSTILE, 42);
    host.append(
      el('p', { text: HOSTILE, attrs: { title: HOSTILE, 'aria-label': HOSTILE } }, HOSTILE),
      codeBlock(HOSTILE, HOSTILE),
      codeBlock({ note: HOSTILE }, HOSTILE),
      placeholder(HOSTILE),
      replaced,
      appended,
      tabs.root,
      svg('svg', { 'aria-label': HOSTILE }, title),
    );
    const tags = new Set([...host.querySelectorAll('*')].map((n) => n.tagName.toLowerCase()));
    expect([...tags].sort()).toEqual(['button', 'code', 'div', 'p', 'pre', 'svg', 'title']);
    expect(host.textContent).toContain(HOSTILE);
  });
});

describe('attributes that could run script are refused', () => {
  it.each(['onclick', 'onError', 'ONLOAD', 'onfocus'])('event handler attribute %s', (name) => {
    expect(() => el('div', { attrs: { [name]: 'alert(1)' } })).toThrow(/props\.on/);
    expect(() => svg('g', { [name]: 'alert(1)' })).toThrow(/props\.on/);
  });

  it('srcdoc', () => {
    // eslint-disable-next-line no-restricted-syntax -- a hostile value is what this test feeds in
    expect(() => el('iframe', { attrs: { srcdoc: '<p>x</p>' } })).toThrow();
  });

  it.each([
    // eslint-disable-next-line no-restricted-syntax -- a hostile value is what this test feeds in
    'javascript:alert(1)',
    // eslint-disable-next-line no-restricted-syntax -- a hostile value is what this test feeds in
    ' JavaScript:alert(1)',
    'java\tscript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'http://example.test/',
    'vbscript:x',
  ])('unsafe URL %j in href, src and xlink:href', (url) => {
    expect(isSafeUrl(url)).toBe(false);
    expect(() => el('a', { attrs: { href: url } })).toThrow(/URLs are allowed/);
    expect(() => el('img', { attrs: { src: url } })).toThrow(/URLs are allowed/);
    expect(() => svg('use', { 'xlink:href': url })).toThrow(/URLs are allowed/);
    expect(() => svg('a', { href: url })).toThrow(/URLs are allowed/);
  });

  it.each(['#main', '/favicon.svg', 'assets/x.png', '../up', '?q=1', 'https://example.test/x'])(
    'safe URL %j is allowed',
    (url) => {
      expect(isSafeUrl(url)).toBe(true);
      expect(el('a', { attrs: { href: url } }).getAttribute('href')).toBe(url);
      expect(() => {
        assertSafeAttribute('src', url);
      }).not.toThrow();
    },
  );

  it('ordinary attributes pass', () => {
    const node = el('button', { attrs: { type: 'button', 'aria-label': 'x', 'data-slot': 'y' } });
    expect(node.getAttribute('data-slot')).toBe('y');
  });
});

describe('codeBlock', () => {
  it('shows ER7 segments on separate lines', () => {
    const block = codeBlock('MSH|^~\\&|A\rPID|1', 'HL7v2');
    expect(block.textContent).toBe('MSH|^~\\&|A\nPID|1');
  });
});
