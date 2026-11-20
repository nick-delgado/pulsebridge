/// <reference types="vitest/config" />
import { defineConfig, loadEnv, type Plugin } from 'vite';

/**
 * Content-Security-Policy for the built page, as a <meta> tag (the hosting layer sends the real
 * one as a header, infra/modules/phr; a browser applies both, so each must allow what the page
 * needs). What it allows:
 *   - scripts, styles, images only from the page's own origin (no inline script, no CDN), plus
 *     in an HTTP build Google's reCAPTCHA (RECAPTCHA_CSP);
 *   - connections only to the page's own origin, where fhir-proxy is (`/api`, D16: same-origin
 *     only), plus reCAPTCHA in an HTTP build. In a mock build, no connections and no Google at
 *     all, so a stray request in mock mode is blocked and shows as a console error in the
 *     browser test;
 *   - no plugins, no <base> tag, and forms may not submit anywhere (the page posts in script).
 * A <meta> policy cannot set frame-ancestors; that needs the header.
 */
/**
 * What reCAPTCHA needs, exactly as Google's reCAPTCHA FAQ lists it ("Can I use reCAPTCHA with a
 * Content Security Policy?", https://docs.cloud.google.com/recaptcha/docs/faq, read directly on
 * 19 November 2026; page last updated 2026-11-13): script-src https://www.google.com/recaptcha/
 * and https://www.gstatic.com/recaptcha/; frame-src https://www.google.com/recaptcha/ and
 * https://recaptcha.google.com/recaptcha/; connect-src https://www.google.com/recaptcha/. Paths,
 * not whole hosts, so nothing else on those hosts is admitted.
 */
export const RECAPTCHA_CSP = {
  script: ['https://www.google.com/recaptcha/', 'https://www.gstatic.com/recaptcha/'],
  frame: ['https://www.google.com/recaptcha/', 'https://recaptcha.google.com/recaptcha/'],
  connect: ['https://www.google.com/recaptcha/'],
} as const;

/**
 * Checks VITE_FHIR_PROXY_URL before anything is built. With D16 the session is a
 * `SameSite=Strict` cookie and the proxy allows no cross-origin credentials, so only a
 * same-origin path works: empty (the default `/api`, Firebase Hosting's rewrite) or a path such
 * as `/api`. For local development against a real proxy, use PULSEBRIDGE_DEV_PROXY_TARGET
 * (Vite's dev-server proxy), which keeps the browser on one origin.
 * Returns the value; throws with a message saying what is allowed.
 */
export function validateProxyUrl(raw: string | undefined): string {
  const value = (raw ?? '').trim();
  if (value === '') return value;
  const help =
    'VITE_FHIR_PROXY_URL must be empty or a same-origin path such as /api (the session cookie ' +
    'is same-origin only; for development use PULSEBRIDGE_DEV_PROXY_TARGET)';
  // "//host" is a protocol-relative URL to another host, not a path.
  const path = /^\/[A-Za-z0-9._~/-]*$/;
  if (!path.test(value) || value.startsWith('//')) throw new Error(`${help}; got "${value}".`);
  return value;
}

/**
 * The real proxy `npm run dev:http` forwards `/api` to (Vite's `server.proxy`), so the browser
 * sees one origin and the cookie works. Not a VITE_ variable: it is never in the bundle. Only
 * an http(s) origin with no path, user or query.
 */
export function validateDevProxyTarget(raw: string | undefined): string | null {
  const value = (raw ?? '').trim();
  if (value === '') return null;
  const help = 'PULSEBRIDGE_DEV_PROXY_TARGET must be an http:// or https:// origin';
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${help}; got "${value}".`);
  }
  const ok =
    (url.protocol === 'https:' || url.protocol === 'http:') &&
    url.username === '' &&
    url.password === '' &&
    url.pathname === '/' &&
    url.search === '' &&
    url.hash === '';
  if (!ok) throw new Error(`${help}; got "${value}".`);
  return url.origin;
}

export function contentSecurityPolicy(mode: string): string {
  const google = mode !== 'mock';
  const list = (base: string[], extra: readonly string[]) =>
    [...base, ...(google ? extra : [])].join(' ');
  return [
    "default-src 'none'",
    `script-src ${list(["'self'"], RECAPTCHA_CSP.script)}`,
    "style-src 'self'",
    "img-src 'self'",
    `connect-src ${mode === 'mock' ? "'none'" : list(["'self'"], RECAPTCHA_CSP.connect)}`,
    ...(google ? [`frame-src ${list([], RECAPTCHA_CSP.frame)}`] : []),
    "base-uri 'none'",
    "form-action 'none'",
    "object-src 'none'",
  ].join('; ');
}

function cspMeta(mode: string): Plugin {
  return {
    name: 'pulsebridge-csp-meta',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: {
            'http-equiv': 'Content-Security-Policy',
            content: contentSecurityPolicy(mode),
          },
          injectTo: 'head-prepend',
        },
      ];
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', ['VITE_', 'PULSEBRIDGE_']);
  const proxyPath = validateProxyUrl(env.VITE_FHIR_PROXY_URL) || '/api';
  const devTarget = validateDevProxyTarget(env.PULSEBRIDGE_DEV_PROXY_TARGET);
  return {
    plugins: [cspMeta(mode)],
    build: {
      // Inline nothing as data: URIs, so img-src/font-src need no `data:`.
      assetsInlineLimit: 0,
      sourcemap: false,
      // dist/.vite/manifest.json: which chunk imports which, statically or with import(). The
      // bundle check reads it to prove the chart library is not loaded with the entry. It lists
      // only the public file names of the build; the hosting layer (T22) may leave it out.
      manifest: true,
    },
    server: {
      port: 5173,
      strictPort: true,
      // `npm run dev:http`: same origin for the browser, forwarded to a real proxy. The proxy
      // must list http://localhost:5173 in ALLOWED_ORIGINS and the reCAPTCHA key must allow
      // localhost, or the session is refused (web/README.md, "Configuration").
      ...(devTarget
        ? { proxy: { [proxyPath]: { target: devTarget, changeOrigin: true, secure: true } } }
        : {}),
    },
    test: {
      environment: 'jsdom',
      include: ['src/**/*.test.ts', 'scripts/**/*.test.mjs'],
      restoreMocks: true,
    },
  };
});
