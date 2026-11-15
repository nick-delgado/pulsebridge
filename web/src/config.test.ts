import { describe, expect, it } from 'vitest';
import { contentSecurityPolicy, validateProxyUrl } from '../vite.config';
import { proxyBaseUrl } from './config';

describe('proxyBaseUrl', () => {
  it('defaults to the same-origin /api path when unset or blank', () => {
    expect(proxyBaseUrl(undefined)).toBe('/api');
    expect(proxyBaseUrl('  ')).toBe('/api');
  });

  it('uses the configured URL without a trailing slash', () => {
    expect(proxyBaseUrl('https://proxy.example.test/api/')).toBe('https://proxy.example.test/api');
  });
});

describe('Content-Security-Policy', () => {
  it('allows no connections at all in a mock build', () => {
    expect(contentSecurityPolicy('mock', 'https://proxy.example.test')).toContain(
      "connect-src 'none'",
    );
  });

  it('allows connections only to the page origin and the configured proxy', () => {
    expect(contentSecurityPolicy('production', undefined)).toContain("connect-src 'self';");
    expect(contentSecurityPolicy('production', 'https://proxy.example.test/api')).toContain(
      "connect-src 'self' https://proxy.example.test;",
    );
  });

  it('allows scripts only from the page origin and forbids form submission', () => {
    const csp = contentSecurityPolicy('production', undefined);
    expect(csp).toContain("script-src 'self';");
    expect(csp).toContain("form-action 'none'");
    expect(csp).not.toContain('unsafe-inline');
  });
});

describe('VITE_FHIR_PROXY_URL validation at build time', () => {
  it.each([
    '',
    '  ',
    '/api',
    '/v1/proxy',
    'https://proxy.example.test',
    'https://proxy.example.test/api',
    'http://localhost:8080',
    'http://127.0.0.1:8080/api',
  ])('accepts %j', (value) => {
    expect(() => validateProxyUrl(value)).not.toThrow();
  });

  it.each([
    'http://proxy.example.test',
    '//proxy.example.test/api',
    'ftp://proxy.example.test',
    // eslint-disable-next-line no-restricted-syntax -- a hostile value is what this test feeds in
    'javascript:alert(1)',
    'proxy.example.test',
    'https://user:pw@proxy.example.test',
    'https://proxy.example.test/api?x=1',
    'http://localhost.example.test',
  ])('rejects %j with a message saying what is allowed', (value) => {
    expect(() => validateProxyUrl(value)).toThrow(/VITE_FHIR_PROXY_URL must be/);
  });

  it('accepts undefined (variable not set)', () => {
    expect(validateProxyUrl(undefined)).toBe('');
  });
});
