import { describe, expect, it } from 'vitest';
import { contentSecurityPolicy } from '../vite.config';
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
