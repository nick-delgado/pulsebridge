// Pure checks over a finished build, used by check-bundle.mjs and tested by
// bundle-rules.test.mjs with hand-made manifests (no build needed).

/**
 * A string that exists only in uPlot's code (a CSS class it builds its cursor points with), so
 * finding it in a script means uPlot is in that script. Our own code never names it.
 */
export const CHART_MARKER = 'u-cursor-pt';

/** A manifest record whose source module is the chart library itself. */
const isChartModule = (key, record) => /(^|\/)node_modules\/uplot\//.test(record.src ?? key);

/**
 * The chart library (uPlot, D5) must load only when there is something to draw: the login form
 * and the rest of the page must never wait for it. With Vite that means uPlot is a dynamic
 * import, so it lands in a chunk of its own that the entry chunk does not import statically.
 *
 * `manifest` is Vite's build manifest (`.vite/manifest.json`, `build.manifest: true`): one record
 * per chunk, `{file, src?, isEntry?, isDynamicEntry?, imports?: [key], dynamicImports?: [key]}`,
 * where `imports` are static imports (loaded before the chunk runs) and `dynamicImports` are
 * `import()` calls (loaded when reached). `contentOf(file)` returns a script's text.
 *
 * Returns problems as sentences; an empty list means the rule holds:
 * 1. there is an entry chunk;
 * 2. no script loaded with the entry (the entry and everything it imports statically, however
 *    deep) contains uPlot's code, and none of them is uPlot's own chunk;
 * 3. some script does contain uPlot (otherwise the marker is stale and rule 2 proves nothing).
 */
export function chartChunkProblems(manifest, contentOf, marker = CHART_MARKER) {
  const problems = [];
  const entries = Object.keys(manifest).filter((key) => manifest[key].isEntry);
  if (entries.length === 0) return ['the build manifest has no entry chunk'];

  // Everything the browser loads before the entry runs: the entry and its static imports.
  const eager = new Set();
  const visit = (key) => {
    if (eager.has(key) || !manifest[key]) return;
    eager.add(key);
    for (const next of manifest[key].imports ?? []) visit(next);
  };
  entries.forEach(visit);

  for (const key of eager) {
    const record = manifest[key];
    const how = manifest[key].isEntry
      ? 'the main entry chunk'
      : 'a chunk the entry imports statically';
    if (isChartModule(key, record)) {
      problems.push(`uPlot (${record.file}) is ${how}: import it with import(), not import`);
    } else if (record.file.endsWith('.js') && contentOf(record.file).includes(marker)) {
      problems.push(`uPlot's code is inside ${how} (${record.file}): import it with import()`);
    }
  }

  const scripts = [...new Set(Object.values(manifest).map((r) => r.file))].filter((f) =>
    f.endsWith('.js'),
  );
  if (!scripts.some((file) => contentOf(file).includes(marker))) {
    problems.push(`no script contains uPlot ("${marker}"); is the chart check stale?`);
  }
  return problems;
}
