/// <reference types="vitest/config" />
import { defineConfig, loadEnv, type Plugin } from 'vite';

/**
 * Content-Security-Policy for the built page, as a <meta> tag (the hosting layer, T22, sends the
 * real one as a header). What it allows:
 *   - scripts, styles, images only from the page's own origin (no inline script, no CDN);
 *   - connections only to fhir-proxy: the page's own origin, plus the proxy's origin when
 *     VITE_FHIR_PROXY_URL points elsewhere. In a mock build, no connections at all, so a stray
 *     request in mock mode is blocked and shows as a console error in the browser test;
 *   - no plugins, no <base> tag, and forms may not submit anywhere (login is handled in script).
 * A <meta> policy cannot set frame-ancestors; that needs the header.
 */
export function contentSecurityPolicy(mode: string, proxyUrl: string | undefined): string {
  let connect = "'self'";
  if (mode === 'mock') {
    connect = "'none'";
  } else if (proxyUrl && /^https?:\/\//i.test(proxyUrl)) {
    connect = `'self' ${new URL(proxyUrl).origin}`;
  }
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    `connect-src ${connect}`,
    "base-uri 'none'",
    "form-action 'none'",
    "object-src 'none'",
  ].join('; ');
}

function cspMeta(mode: string, proxyUrl: string | undefined): Plugin {
  return {
    name: 'pulsebridge-csp-meta',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: {
            'http-equiv': 'Content-Security-Policy',
            content: contentSecurityPolicy(mode, proxyUrl),
          },
          injectTo: 'head-prepend',
        },
      ];
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_');
  return {
    plugins: [cspMeta(mode, env.VITE_FHIR_PROXY_URL)],
    build: {
      // Inline nothing as data: URIs, so img-src/font-src need no `data:`.
      assetsInlineLimit: 0,
      sourcemap: false,
    },
    server: { port: 5173, strictPort: true },
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.ts'],
      restoreMocks: true,
    },
  };
});
