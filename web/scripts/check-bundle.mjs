// Checks a finished build, so a regression fails `npm run build` rather than reaching a reviewer.
//   node scripts/check-bundle.mjs <dist-dir> <http|mock>
//
// 1. index.html carries the Content-Security-Policy <meta>, directive by directive:
//    default-src, base-uri, form-action and object-src 'none'; script-src 'self'; connect-src
//    exactly 'none' (mock) or 'self' plus the configured proxy origin (HTTP), nothing wider.
// 2. index.html has no inline <script> (the CSP would block it anyway).
// 3. An HTTP build contains none of the mock client or its synthetic data. Vite drops the mock
//    branch because the mode is a build-time constant; this proves it did.
// 4. No build points at a developer machine (localhost, 127.0.0.1).
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { loadEnv } from 'vite';

const [dir, mode] = process.argv.slice(2);
if (!dir || (mode !== 'http' && mode !== 'mock')) {
  console.error('usage: check-bundle.mjs <dist-dir> <http|mock>');
  process.exit(2);
}

// Strings that only exist in src/api/mock-client.ts and src/api/mock-data.ts.
const MOCK_MARKERS = ['mock-session-', 'SYN-000123', 'Synthetic watch (mock mode)'];

const files = readdirSync(dir, { recursive: true }).map(String);
const read = (f) => readFileSync(join(dir, f), 'utf8');
const scripts = files.filter((f) => f.endsWith('.js'));
const problems = [];

const html = read('index.html');
const match = html.match(/<meta http-equiv="Content-Security-Policy" content="([^"]+)"/);
const csp = match?.[1].replaceAll('&#39;', "'").replaceAll('&quot;', '"');
if (!csp) problems.push('index.html has no Content-Security-Policy meta tag');
else {
  const directives = new Map(
    csp
      .split(';')
      .map((d) => d.trim().split(/\s+/))
      .filter((parts) => parts[0])
      .map(([name, ...values]) => [name, values.join(' ')]),
  );
  // The proxy origin the build was configured with, read the way Vite reads it.
  const proxyUrl = (
    loadEnv(mode === 'mock' ? 'mock' : 'production', '.', 'VITE_').VITE_FHIR_PROXY_URL ?? ''
  ).trim();
  const proxyOrigin = /^https?:\/\//i.test(proxyUrl) ? new URL(proxyUrl).origin : null;
  const expected = {
    'default-src': "'none'",
    'script-src': "'self'",
    'base-uri': "'none'",
    'form-action': "'none'",
    'object-src': "'none'",
    'connect-src': mode === 'mock' ? "'none'" : proxyOrigin ? `'self' ${proxyOrigin}` : "'self'",
  };
  for (const [name, value] of Object.entries(expected)) {
    const actual = directives.get(name);
    if (actual !== value)
      problems.push(`CSP ${name} is "${actual ?? '(missing)'}", expected "${value}"`);
  }
}
if (/<script(?![^>]*\bsrc=)[^>]*>/i.test(html)) problems.push('index.html has an inline <script>');

const bundle = scripts.map(read).join('\n');
for (const marker of MOCK_MARKERS) {
  const found = bundle.includes(marker);
  if (mode === 'http' && found) problems.push(`HTTP build contains mock code ("${marker}")`);
  if (mode === 'mock' && !found)
    problems.push(`mock build is missing "${marker}"; is the check stale?`);
}
for (const host of ['localhost', '127.0.0.1']) {
  if (bundle.includes(host) || html.includes(host)) problems.push(`build references ${host}`);
}

if (problems.length > 0) {
  console.error(`check-bundle (${mode}) failed:\n  - ${problems.join('\n  - ')}`);
  process.exit(1);
}
console.log(`check-bundle (${mode}): ${scripts.length} script file(s) OK`);
