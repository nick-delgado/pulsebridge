// Tests for the chart-chunk rule over hand-made Vite manifests (the shape of
// dist/.vite/manifest.json), so each way the rule can break is checked without a build.
import { describe, expect, it } from 'vitest';
import {
  CHART_MARKER,
  chartChunkProblems,
  MOCK_MARKERS,
  mockMarkerProblems,
} from './bundle-rules.mjs';

const UPLOT_CODE = `var p=\`${CHART_MARKER}\`;export{p as default}`;
const OUR_CODE = 'console.log("dashboard")';

/** A good build: uPlot is a dynamic entry of its own, reached by import() from the entry. */
function goodBuild() {
  return {
    manifest: {
      'index.html': {
        file: 'assets/index-A.js',
        src: 'index.html',
        isEntry: true,
        imports: ['_shared-B.js'],
        dynamicImports: ['node_modules/uplot/dist/uPlot.esm.js'],
      },
      '_shared-B.js': { file: 'assets/shared-B.js' },
      'node_modules/uplot/dist/uPlot.esm.js': {
        file: 'assets/uPlot.esm-C.js',
        src: 'node_modules/uplot/dist/uPlot.esm.js',
        isDynamicEntry: true,
      },
    },
    files: {
      'assets/index-A.js': OUR_CODE,
      'assets/shared-B.js': OUR_CODE,
      'assets/uPlot.esm-C.js': UPLOT_CODE,
    },
  };
}

const check = ({ manifest, files }) => chartChunkProblems(manifest, (f) => files[f] ?? '');

describe('chart chunk rule', () => {
  it('passes when uPlot is only reachable through import()', () => {
    expect(check(goodBuild())).toEqual([]);
  });

  it('fails when uPlot is bundled into the main entry chunk', () => {
    const build = goodBuild();
    delete build.manifest['node_modules/uplot/dist/uPlot.esm.js'];
    build.manifest['index.html'].dynamicImports = [];
    build.files['assets/index-A.js'] = OUR_CODE + UPLOT_CODE;
    expect(check(build)).toEqual([
      expect.stringContaining("uPlot's code is inside the main entry chunk"),
    ]);
  });

  it('fails when the entry imports the uPlot chunk statically', () => {
    const build = goodBuild();
    build.manifest['index.html'].imports.push('node_modules/uplot/dist/uPlot.esm.js');
    build.manifest['index.html'].dynamicImports = [];
    expect(check(build)).toEqual([
      expect.stringContaining('is a chunk the entry imports statically'),
    ]);
  });

  it('fails when uPlot sits in a shared chunk the entry imports statically, however deep', () => {
    const build = goodBuild();
    build.manifest['_shared-B.js'].imports = ['_deeper-D.js'];
    build.manifest['_deeper-D.js'] = { file: 'assets/deeper-D.js' };
    build.files['assets/deeper-D.js'] = UPLOT_CODE;
    expect(check(build)).toEqual([
      expect.stringContaining('inside a chunk the entry imports statically (assets/deeper-D.js)'),
    ]);
  });

  it('a chunk loaded only dynamically may contain uPlot', () => {
    const build = goodBuild();
    build.manifest['index.html'].dynamicImports.push('src/heart-rate/chart.ts');
    build.manifest['src/heart-rate/chart.ts'] = {
      file: 'assets/chart-E.js',
      src: 'src/heart-rate/chart.ts',
      isDynamicEntry: true,
    };
    build.files['assets/chart-E.js'] = UPLOT_CODE;
    expect(check(build)).toEqual([]);
  });

  it('fails when no script contains uPlot, so a stale marker cannot pass silently', () => {
    const build = goodBuild();
    build.files['assets/uPlot.esm-C.js'] = OUR_CODE;
    expect(check(build)).toEqual([expect.stringContaining('is the chart check stale')]);
  });

  it('fails with no entry chunk', () => {
    const build = goodBuild();
    build.manifest['index.html'].isEntry = false;
    expect(check(build)).toEqual(['the build manifest has no entry chunk']);
  });
});

describe('mock markers: no mock code in an HTTP build', () => {
  it('lists every mock switch name', () => {
    for (const name of ['mock-audit', 'mock-lookup', 'mock-log', 'mock-status', 'mock-share']) {
      expect(MOCK_MARKERS).toContain(name);
    }
  });

  it('a clean HTTP build passes', () => {
    expect(mockMarkerProblems(OUR_CODE, 'http')).toEqual([]);
  });

  it.each(MOCK_MARKERS)('a planted "%s" fails the HTTP build', (marker) => {
    const bundle = `${OUR_CODE};const q=new URLSearchParams(location.search).get("${marker}");`;
    expect(mockMarkerProblems(bundle, 'http')).toEqual([
      `HTTP build contains mock code ("${marker}")`,
    ]);
  });

  it('a mock build missing a marker says the list is stale', () => {
    const all = MOCK_MARKERS.join(' ');
    expect(mockMarkerProblems(all, 'mock')).toEqual([]);
    expect(mockMarkerProblems(all.replace('mock-share', ''), 'mock')).toEqual([
      'mock build is missing "mock-share"; is the check stale?',
    ]);
  });
});
