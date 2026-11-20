/**
 * The privacy note (web/privacy.html, T48): linked from the landing view and the dashboard, and
 * true of the code it describes. The note is plain text for visitors, so these drift tests tie
 * each claim to its source: if the proxy starts logging the address in another event, sends
 * Google more than the token, renames its cookie or the page renames a button, a test here
 * fails and the note must be changed with it. (Module phr's defaults behind "24 hours" and
 * "the last 7 days" are checked by infra/modules/phr/tests/check_proxy_settings.py.)
 */
import { readdirSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { MockClient } from '../api/mock-client';
import { mockBotCheck } from './test-support';
import { landingView, PRIVACY_HREF, RECAPTCHA_NOTICE, START_LABEL } from './landing';
import { dashboardLayout } from './layout';

/** A repository file by its path from the repository root (as in src/api/reasons.test.ts). */
function repoPath(path: string): string {
  const base = import.meta.url;
  return decodeURIComponent(new URL(`../../../${path}`, base).pathname).replace(/^\/@fs\//, '/');
}

function repoFile(path: string): string {
  const base = import.meta.url;
  const absolute = decodeURIComponent(new URL(`../../../${path}`, base).pathname);
  return readFileSync(absolute.replace(/^\/@fs\//, '/'), 'utf8');
}

const html = repoFile('web/privacy.html');
/** The note as a visitor reads it: tags and the comment removed, whitespace collapsed. */
const text = html
  .replace(/<!--[\s\S]*?-->/g, ' ')
  .replace(/<[^>]+>/g, ' ')
  .replace(/\s+/g, ' ');
const proxyApp = repoFile('services/fhir-proxy/fhir_proxy/app.py');
const botCheck = repoFile('services/fhir-proxy/fhir_proxy/bot_check.py');

describe('the privacy note is linked', () => {
  it('from the landing view, next to the reCAPTCHA notice', () => {
    const view = landingView({
      client: new MockClient({ onUnauthorized: () => undefined, latencyMs: 0 }),
      botCheck: mockBotCheck(),
      onSuccess: () => undefined,
    });
    const link = view.querySelector<HTMLAnchorElement>(`a[href="${PRIVACY_HREF}"]`);
    expect(link?.textContent).toMatch(/privacy/i);
    expect(view.textContent).toContain(RECAPTCHA_NOTICE);
  });

  it('from the dashboard header', () => {
    const layout = dashboardLayout({ mode: 'http', signal: new AbortController().signal });
    const header = layout.root.querySelector('header.app-header');
    const link = header?.querySelector<HTMLAnchorElement>(`a[href="${PRIVACY_HREF}"]`);
    expect(link?.textContent).toBe('Privacy');
  });

  it('to the page the build makes (vite.config.ts) and back to the app', () => {
    expect(PRIVACY_HREF).toBe('/privacy.html');
    expect(repoFile('web/vite.config.ts')).toMatch(/privacy: 'privacy\.html'/);
    expect(html).toContain('<a href="/">Back to PulseBridge</a>');
  });
});

describe('the privacy note stays inside the page rules', () => {
  it('has no script and loads nothing from another origin', () => {
    expect(html).not.toMatch(/<script\b/i);
    expect(html).not.toMatch(/<(iframe|img|object|embed|form)\b/i);
    // Every src and href is same-origin: a path, never a scheme or //host.
    const urls = [...html.matchAll(/\s(?:src|href)="([^"]*)"/g)].map((m) => m[1]);
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) expect(url).toMatch(/^\/(?!\/)/);
    expect(html).not.toMatch(/style="/);
  });

  it('names nobody: no e-mail address, domain or contact line', () => {
    expect(text).not.toMatch(/@/);
    // The only host named is Google's own, which reCAPTCHA talks to.
    const hosts = [...text.matchAll(/\b(?:[a-z0-9-]+\.)+(?:com|org|net|app|dev|io)\b/gi)].map(
      (m) => m[0],
    );
    expect(hosts.length).toBeGreaterThan(0);
    for (const host of hosts) expect(host).toMatch(/^(www\.)?google\.com$/);
  });
});

describe('every claim matches the code', () => {
  it("the button and the log-out wording are the page's own", () => {
    expect(text).toContain(`"${START_LABEL}"`);
    const layout = dashboardLayout({ mode: 'http', signal: new AbortController().signal });
    const buttons = [...layout.root.querySelectorAll('button')].map((b) => b.textContent);
    expect(buttons).toContain('Log out');
    expect(text).toContain('"Log out"');
  });

  it("the cookie is the proxy's __session, HttpOnly and only for the API path", () => {
    expect(proxyApp).toMatch(/^COOKIE_NAME = "__session"$/m);
    expect(proxyApp).toMatch(
      /Path=\{path\}; Max-Age=\{max_age\}; HttpOnly; Secure; SameSite=Strict/,
    );
    expect(text).toContain('__session');
    expect(text).toContain('scripts on the page cannot read it');
    expect(text).toContain('_GRECAPTCHA');
    // Not "no other cookies": requests to www.google.com can carry Google's own (FAQ).
    expect(text).toContain('This site sets no other cookie');
    expect(text).toContain('cookies Google had already set on google.com');
    expect(text).not.toMatch(/no other cookies|who viewed what/);
  });

  it('the server sends Google only the token, the site key and the action', () => {
    // bot_check.py's request body: exactly these three event fields (no userIpAddress or
    // userAgent, which are optional).
    expect(botCheck).toMatch(
      /"event": \{"token": token, "siteKey": self\._site_key, "expectedAction": self\._action\}/,
    );
    expect(botCheck).not.toMatch(/"userIpAddress"|"userAgent"/);
    expect(text).toContain("with the site's key and the action name");
    expect(text).toContain('does not send Google your IP address');
  });

  it('no other log call in the proxy carries an address', () => {
    // Every log(...) call in every module of the proxy (share/ included), found by scanning to
    // its closing parenthesis, so a call split over lines or naming the address another way
    // (ip=, remote_addr, the forwarded header, the address variable) is seen too.
    const dir = repoPath('services/fhir-proxy/fhir_proxy');
    const files = readdirSync(dir, { recursive: true })
      .map(String)
      .filter((f) => f.endsWith('.py'));
    expect(files).toContain('app.py');
    const calls: { file: string; text: string }[] = [];
    for (const file of files) {
      const source = readFileSync(`${dir}/${file}`, 'utf8');
      for (const m of source.matchAll(/\blog\(/g)) {
        let depth = 0;
        let end = m.index;
        for (; end < source.length; end++) {
          if (source[end] === '(') depth++;
          if (source[end] === ')' && --depth === 0) break;
        }
        calls.push({ file, text: source.slice(m.index, end + 1) });
      }
    }
    expect(calls.length).toBeGreaterThan(20);
    const addressy = /address|\bip\b|_ip\b|\bip_|remote_addr|forwarded_for|x-forwarded/i;
    const withAddress = calls.filter((c) => addressy.test(c.text));
    // The one allowed: _sampled's `client_address=address` (the three sampled refusals below).
    expect(withAddress.map((c) => `${c.file}: ${c.text.replace(/\s+/g, ' ')}`)).toEqual([
      'app.py: log(logging.WARNING, event, client_address=address, **fields)',
    ]);
  });

  it('the client address is logged only in the three sampled refusals', () => {
    // Every log call with the address goes through _sampled, and only these three events use it.
    const direct = [...proxyApp.matchAll(/client_address=/g)].length;
    expect(direct).toBe(1); // the one in _sampled
    const sampled = [...proxyApp.matchAll(/_sampled\(\w+, "([a-z-]+)"/g)].map((m) => m[1]);
    expect(sampled.sort()).toEqual(['origin-refused', 'rate-limited', 'session-rate-limited']);
    expect(proxyApp).toMatch(/^SESSION_LOG_WINDOW_SECONDS = 60$/m);
    expect(proxyApp).toMatch(/^RATE_LOG_WINDOW_SECONDS = 60$/m);
    expect(text).toContain('coming too fast or from another website');
    expect(text).toContain('first such refusal of each kind from an address in a minute');
  });

  it('states the data controller and processor, the cookie life, the window and the logs', () => {
    expect(text).toContain(
      "the site's owner is the data controller and Google is the data processor",
    );
    expect(text).toContain('since 2 April 2026');
    expect(text).toContain('It lasts 24 hours');
    expect(text).toContain('Only the last 7 days are shown');
    expect(text).toContain('Logs are kept for 30 days');
  });
});
