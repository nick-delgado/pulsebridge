import { describe, expect, it } from 'vitest';
import {
  contentSecurityPolicy,
  RECAPTCHA_CSP,
  validateDevProxyTarget,
  validateProxyUrl,
} from '../vite.config';
import { proxyBaseUrl } from './config';

/** A policy as a map of directive to its sources, so a test can compare one directive exactly. */
function directives(csp: string): Map<string, string[]> {
  return new Map(
    csp
      .split(';')
      .map((d) => d.trim().split(/\s+/))
      .filter((parts) => parts[0])
      .map(([name = '', ...values]) => [name, values]),
  );
}

describe('proxyBaseUrl', () => {
  it('defaults to the same-origin /api path when unset or blank', () => {
    expect(proxyBaseUrl(undefined)).toBe('/api');
    expect(proxyBaseUrl('  ')).toBe('/api');
  });

  it('uses the configured path without a trailing slash', () => {
    expect(proxyBaseUrl('/v1/proxy/')).toBe('/v1/proxy');
  });
});

describe('Content-Security-Policy', () => {
  it('a mock build allows no connections, no frames and no Google at all', () => {
    const csp = directives(contentSecurityPolicy('mock'));
    expect(csp.get('connect-src')).toEqual(["'none'"]);
    expect(csp.get('script-src')).toEqual(["'self'"]);
    expect(csp.has('frame-src')).toBe(false);
    expect(contentSecurityPolicy('mock')).not.toContain('google');
  });

  it("an HTTP build adds exactly reCAPTCHA's script, frame and connect paths (Google's FAQ)", () => {
    const csp = directives(contentSecurityPolicy('production'));
    expect(csp.get('script-src')).toEqual([
      "'self'",
      'https://www.google.com/recaptcha/',
      'https://www.gstatic.com/recaptcha/',
    ]);
    expect(csp.get('frame-src')).toEqual([
      'https://www.google.com/recaptcha/',
      'https://recaptcha.google.com/recaptcha/',
    ]);
    expect(csp.get('connect-src')).toEqual(["'self'", 'https://www.google.com/recaptcha/']);
    // Paths, never a whole host.
    for (const source of [...RECAPTCHA_CSP.script, ...RECAPTCHA_CSP.frame]) {
      expect(source.endsWith('/recaptcha/'), source).toBe(true);
    }
  });

  it('keeps styles and images on the page origin and forbids form submission and inline code', () => {
    const csp = contentSecurityPolicy('production');
    const map = directives(csp);
    expect(map.get('style-src')).toEqual(["'self'"]);
    expect(map.get('img-src')).toEqual(["'self'"]);
    expect(map.get('form-action')).toEqual(["'none'"]);
    expect(map.get('default-src')).toEqual(["'none'"]);
    expect(csp).not.toContain('unsafe-inline');
    expect(csp).not.toContain('unsafe-eval');
  });
});

describe('VITE_FHIR_PROXY_URL validation at build time', () => {
  it.each(['', '  ', '/api', '/v1/proxy'])('accepts %j', (value) => {
    expect(() => validateProxyUrl(value)).not.toThrow();
  });

  it.each([
    // D16: the session cookie is same-origin only, so no other origin is accepted any more.
    'https://proxy.example.test',
    'https://proxy.example.test/api',
    'http://localhost:8080',
    '//proxy.example.test/api',
    // eslint-disable-next-line no-restricted-syntax -- a hostile value is what this test feeds in
    'javascript:alert(1)',
    'proxy.example.test',
    '/api?x=1',
    '/api#x',
    '/a\\b',
  ])('rejects %j with a message saying what is allowed', (value) => {
    expect(() => validateProxyUrl(value)).toThrow(/VITE_FHIR_PROXY_URL must be/);
  });

  it('accepts undefined (variable not set)', () => {
    expect(validateProxyUrl(undefined)).toBe('');
  });
});

describe('PULSEBRIDGE_DEV_PROXY_TARGET (dev server only)', () => {
  it.each([
    [undefined, null],
    ['', null],
    ['https://proxy.example.test', 'https://proxy.example.test'],
    ['http://127.0.0.1:8080/', 'http://127.0.0.1:8080'],
  ])('%j gives %j', (value, expected) => {
    expect(validateDevProxyTarget(value)).toBe(expected);
  });

  it.each([
    'ftp://proxy.example.test',
    'https://proxy.example.test/api',
    'https://user:pw@proxy.example.test',
    'https://proxy.example.test?x=1',
    'proxy.example.test',
  ])('rejects %j', (value) => {
    expect(() => validateDevProxyTarget(value)).toThrow(/PULSEBRIDGE_DEV_PROXY_TARGET must be/);
  });
});
