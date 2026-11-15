/**
 * Build-time configuration.
 *
 * - The mode comes from `vite --mode mock` (`npm run dev`, `npm run build:mock`); see
 *   `api/index.ts`, which is the only place that reads it. Everything else asks the client
 *   (`client.mode`), so no module needs to know how the choice was made.
 * - The proxy URL comes from `VITE_FHIR_PROXY_URL`. No URL is written in the code.
 */

/** Same-origin path that Firebase Hosting rewrites to fhir-proxy (D4) when no URL is set. */
export const DEFAULT_PROXY_BASE = '/api';

export function proxyBaseUrl(
  raw: string | undefined = import.meta.env.VITE_FHIR_PROXY_URL,
): string {
  const value = (raw ?? '').trim();
  return (value === '' ? DEFAULT_PROXY_BASE : value).replace(/\/+$/, '');
}
