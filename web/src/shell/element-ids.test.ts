/**
 * The element-id rule (web/README.md, "Element ids"): one document holds the shell and four
 * features built in parallel, so every id is prefixed by its owner and none is used twice.
 * This mounts the real features against the mock, so a feature that breaks the rule after a
 * merge fails here.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { MockClient } from '../api/mock-client';
import { Session } from '../api/session';
import { startApp } from './app';

/** The id prefix each feature owns, by the slot it is mounted in. */
const SLOT_PREFIX: Record<string, string> = {
  'heart-rate-panel': 'hr-',
  'heart-rate-trace': 'hr-',
  'cuff-panel': 'cuff-',
  'cuff-trace': 'cuff-',
  'share-action': 'share-',
  'share-trace': 'share-',
  'share-readback': 'share-',
  'messages-log': 'log-',
  'messages-summary': 'log-',
};
/** Ids the shell's helpers generate (`uniqueId`, `createTabs`): unique by construction. */
const HELPER_PREFIX = 'ui-';
/** The shell's own ids; `main` is the skip link's target. */
const SHELL = /^(shell-|main$)/;

const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};

describe('element ids on the dashboard', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('are unique, and each is prefixed by the shell or the feature whose slot holds it', async () => {
    document.body.replaceChildren();
    const root = document.createElement('div');
    document.body.append(root);
    const session = new Session(sessionStorage);
    const client = new MockClient({ session, onUnauthorized: () => undefined, latencyMs: 0 });
    await client.login('x');
    await startApp({ root, makeClient: () => client });
    await settle();

    const elements = [...root.querySelectorAll<HTMLElement>('[id]')];
    expect(elements.length).toBeGreaterThan(5);
    const ids = elements.map((e) => e.id);
    expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);

    const wrong: string[] = [];
    for (const element of elements) {
      const slot = element.closest<HTMLElement>('[data-slot]');
      // The slot element itself belongs to the shell.
      const owner = slot && slot !== element ? slot.dataset.slot : undefined;
      const prefix = owner === undefined ? undefined : SLOT_PREFIX[owner];
      const ok =
        element.id.startsWith(HELPER_PREFIX) ||
        (prefix === undefined ? SHELL.test(element.id) : element.id.startsWith(prefix));
      if (!ok) wrong.push(`${element.id} (in ${owner ?? 'the shell'})`);
    }
    expect(wrong).toEqual([]);
  });
});
