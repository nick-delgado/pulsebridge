import type { LoginResult } from './contract';

/** The sessionStorage key holding the bearer token and its expiry. */
export const SESSION_KEY = 'pulsebridge.session';

/**
 * Where the bearer token lives (D9, docs/spikes/s6.md "What the page does").
 *
 * `sessionStorage` is per tab and cleared when the tab closes, so a token does not outlive the
 * visit; it is not sent anywhere automatically. A cookie would be: the browser attaches it to
 * every request to the proxy's origin, which is what cross-site request forgery rides on, and the
 * proxy sets none. The trade-off is that script on the page can read sessionStorage, which is why
 * the page renders data as text only and its CSP allows scripts only from its own origin.
 */
export class Session {
  #memory: string | null = null;

  constructor(
    private readonly storage: Storage | null = safeSessionStorage(),
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** The token, or null when there is none or it has expired (an expired one is removed). */
  token(): string | null {
    const raw = this.read();
    if (raw === null) return null;
    try {
      const parsed = JSON.parse(raw) as Partial<LoginResult>;
      const expires = Date.parse(parsed.expiresAt ?? '');
      if (typeof parsed.token === 'string' && parsed.token !== '' && expires > this.now()) {
        return parsed.token;
      }
    } catch {
      // Unreadable value: treat as logged out.
    }
    this.clear();
    return null;
  }

  save(login: LoginResult): void {
    this.write(JSON.stringify({ token: login.token, expiresAt: login.expiresAt }));
  }

  clear(): void {
    this.#memory = null;
    try {
      this.storage?.removeItem(SESSION_KEY);
    } catch {
      // Storage blocked: nothing stored there either.
    }
  }

  private read(): string | null {
    try {
      const stored = this.storage?.getItem(SESSION_KEY) ?? null;
      if (stored !== null) return stored;
    } catch {
      // Fall through to memory.
    }
    return this.#memory;
  }

  private write(value: string): void {
    try {
      if (this.storage) {
        this.storage.setItem(SESSION_KEY, value);
        return;
      }
    } catch {
      // Blocked storage (some private windows): keep the token for this page load only.
    }
    this.#memory = value;
  }
}

function safeSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}
