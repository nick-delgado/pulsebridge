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
/**
 * Checks VITE_FHIR_PROXY_URL before anything is built, so a typo cannot ship a page that sends
 * the reviewer's token somewhere unexpected. Accepted: empty (same-origin /api), a same-origin
 * path ("/api"), an https: URL, or for local development http://localhost or http://127.0.0.1.
 * Returns the value; throws with a message saying what is allowed.
 */
export function validateProxyUrl(raw: string | undefined): string {
  const value = (raw ?? '').trim();
  if (value === '') return value;
  const help =
    'VITE_FHIR_PROXY_URL must be empty, a same-origin path such as /api, an https:// URL, ' +
    'or http://localhost / http://127.0.0.1 for development';
  if (value.startsWith('/')) {
    // "//host" is a protocol-relative URL to another host, not a path.
    if (value.startsWith('//') || value.includes('\\')) throw new Error(`${help}; got "${value}".`);
    return value;
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${help}; got "${value}".`);
  }
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  const ok = url.protocol === 'https:' || (url.protocol === 'http:' && local);
  if (!ok || url.username !== '' || url.password !== '' || url.search !== '' || url.hash !== '') {
    throw new Error(`${help}; got "${value}".`);
  }
  return value;
}

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
  const proxyUrl = validateProxyUrl(env.VITE_FHIR_PROXY_URL);
  return {
    plugins: [cspMeta(mode, proxyUrl)],
    build: {
      // Inline nothing as data: URIs, so img-src/font-src need no `data:`.
      assetsInlineLimit: 0,
      sourcemap: false,
      // dist/.vite/manifest.json: which chunk imports which, statically or with import(). The
      // bundle check reads it to prove the chart library is not loaded with the entry. It lists
      // only the public file names of the build; the hosting layer (T22) may leave it out.
      manifest: true,
    },
    server: { port: 5173, strictPort: true },
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.ts', 'scripts/**/*.test.mjs'],
      restoreMocks: true,
    },
  };
});
