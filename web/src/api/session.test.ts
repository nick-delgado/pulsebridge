import { beforeEach, describe, expect, it } from 'vitest';
import { SESSION_KEY, Session } from './session';

describe('Session', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('keeps the token in sessionStorage until it expires', () => {
    let now = Date.parse('2026-01-01T00:00:00Z');
    const session = new Session(sessionStorage, () => now);
    session.save({ token: 't', expiresAt: '2026-01-01T08:00:00Z' });
    expect(sessionStorage.getItem(SESSION_KEY)).not.toBeNull();
    expect(session.token()).toBe('t');

    now = Date.parse('2026-01-01T08:00:00Z');
    expect(session.token()).toBeNull();
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('clear() removes it', () => {
    const session = new Session(sessionStorage);
    session.save({ token: 't', expiresAt: new Date(Date.now() + 60_000).toISOString() });
    session.clear();
    expect(session.token()).toBeNull();
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('treats an unreadable stored value as logged out', () => {
    sessionStorage.setItem(SESSION_KEY, '{not json');
    expect(new Session(sessionStorage).token()).toBeNull();
  });

  it('never writes a cookie', () => {
    new Session(sessionStorage).save({
      token: 't',
      expiresAt: new Date(Date.now() + 1).toISOString(),
    });
    expect(document.cookie).toBe('');
  });
});
