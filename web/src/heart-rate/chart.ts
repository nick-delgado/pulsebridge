/**
 * The canvas chart, drawn with uPlot (https://github.com/leeoniya/uPlot, MIT, no dependencies).
 *
 * This file is the only one that knows uPlot exists. The panel (panel.ts) talks to it through
 * `ChartHandle`, so the unit tests can run the panel in jsdom (which has no canvas) with a stand-in,
 * and the browser test exercises this file for real.
 *
 * What is drawn, bottom to top:
 *   - a band between each window's minimum and maximum (the shaded range, with thin edges),
 *     series 1 and 2, drawn first so the averages stay on top at 30 days;
 *   - the line of five-minute averages of windows that are not provisional, solid, series 3;
 *   - the line of provisional windows, dashed, with a marker on each, series 4;
 *   - our own markers: the keyboard / hover cursor (a vertical line) and the selected point.
 * Null values break every line and the band, so a gap with no data stays a gap.
 *
 * Why it stays fast at 30 days (about 8,600 windows): uPlot's line path builder already reduces
 * the points to one min/max pair per pixel column when it builds the path, so drawing costs
 * the plot width, not the point count. We do not aggregate the data ourselves: every window stays
 * one row, so a click or the keyboard picks one real Observation.
 *
 * Security: uPlot draws labels on the canvas with fillText; it sets no HTML. Its legend (which
 * uses textContent) is turned off; the panel writes its own readout with the DOM helpers. It
 * positions its elements through CSSOM (`element.style.left = …`), which `style-src 'self'`
 * allows, and needs no eval. Its stylesheet is bundled with ours, from 'self'.
 *
 * Loading: uPlot is imported dynamically the first time there is data to draw, so it is a separate
 * script (from 'self') that the login form never waits for, and so importing this module does not
 * run uPlot's start-up code (which reads `matchMedia`) in environments without it, such as the
 * shell's jsdom tests. Where the browser lacks what the chart needs, the area says so in text and
 * everything else (summary, statistics, keyboard selection, trace) keeps working.
 */
// Types only here: the library itself is loaded on first use (see `loadUplot`).
import type uPlot from 'uplot';
import 'uplot/dist/uPlot.min.css';
import { nearestPoint, type ChartSeries, type HeartRatePoint } from './data';
import { timeTicks, type OffsetSegment } from './time';

export interface ChartModel {
  points: readonly HeartRatePoint[];
  series: ChartSeries;
  segments: readonly OffsetSegment[];
  /** The x range shown: the requested time range, so a range with gaps at its ends looks right. */
  rangeStartMs: number;
  rangeEndMs: number;
}

export interface ChartCallbacks {
  /** Pointer moved over the chart: the nearest point, or null when it left. */
  onHover(point: number | null): void;
  /** Pointer clicked or tapped: the nearest point. */
  onPick(point: number): void;
}

export interface ChartHandle {
  update(model: ChartModel): void;
  /** Draws the keyboard/hover cursor and the selected marker (point indexes, or null). */
  mark(active: number | null, selected: number | null): void;
  destroy(): void;
}

export type ChartFactory = (
  container: HTMLElement,
  callbacks: ChartCallbacks,
  signal: AbortSignal,
) => ChartHandle;

/** Reads a colour token (src/styles/tokens.css) as the canvas needs a real colour value. */
function token(el: HTMLElement, name: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim() || '#888888';
}

/** `#rrggbb` with an alpha, for the band fill. Other colour syntaxes are returned unchanged. */
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color);
  if (!m) return color;
  const [r, g, b] = [m[1], m[2], m[3]].map((h) => Number.parseInt(h ?? '0', 16));
  return `rgba(${String(r)}, ${String(g)}, ${String(b)}, ${String(alpha)})`;
}

/**
 * The point under a horizontal position in the plot (CSS pixels from its left edge): the window
 * whose middle is nearest to the time at that position, or null when there are none. It searches
 * the points, not the chart rows (which include gap rows), so the answer is always one real
 * Observation. `posToVal` is uPlot's pixel-to-time conversion.
 */
export function pointAtPosition(
  points: readonly HeartRatePoint[],
  posToVal: (left: number) => number,
  left: number,
): number | null {
  if (points.length === 0) return null;
  const p = nearestPoint(points, posToVal(left));
  return p < 0 ? null : p;
}

/** Opacity of the band's tint; the legend swatch uses the same value (heart-rate.css). */
const BAND_ALPHA = 0.3;
const HEIGHT_WIDE = 240;
const HEIGHT_NARROW = 200;
/** Leave about this much room per x label. */
const PX_PER_TICK = 64;

/** The uPlot constructor, imported once on first use. */
let uplotModule: Promise<typeof uPlot> | null = null;
function loadUplot(): Promise<typeof uPlot> {
  uplotModule ??= import('uplot').then((m) => m.default);
  return uplotModule;
}

/** What the chart needs from the browser beyond canvas: resize and colour-scheme events. */
function chartSupported(): boolean {
  return typeof ResizeObserver === 'function' && typeof matchMedia === 'function';
}

const UNSUPPORTED =
  'The chart cannot be drawn in this browser. The summary, the statistics and the keyboard cursor below still give every reading.';

export const createUplotChart: ChartFactory = (container, callbacks, signal) => {
  if (!chartSupported()) {
    container.textContent = UNSUPPORTED;
    return { update: () => undefined, mark: () => undefined, destroy: () => undefined };
  }
  let plot: uPlot | null = null;
  let model: ChartModel | null = null;
  let active: number | null = null;
  let selected: number | null = null;

  const color = {
    line: () => token(container, '--color-continuous'),
    provisional: () => token(container, '--color-warn'),
    text: () => token(container, '--color-text'),
    muted: () => token(container, '--color-text-muted'),
    grid: () => token(container, '--color-border'),
    focus: () => token(container, '--color-focus'),
    surface: () => token(container, '--color-surface'),
  };
  const font = () => `12px ${token(container, '--font-sans')}`;

  const size = () => {
    const width = Math.max(200, Math.floor(container.clientWidth));
    return { width, height: width < 480 ? HEIGHT_NARROW : HEIGHT_WIDE };
  };

  const data = (m: ChartModel): uPlot.AlignedData => [
    m.series.x,
    m.series.maximum,
    m.series.minimum,
    m.series.averageSettled,
    m.series.averageProvisional,
  ];

  const pointAt = (u: uPlot, left: number): number | null =>
    model ? pointAtPosition(model.points, (px) => u.posToVal(px, 'x'), left) : null;

  const drawMarkers = (u: uPlot) => {
    if (!model) return;
    const ctx = u.ctx;
    const px = devicePixelRatio || 1;
    const { top, height } = u.bbox;
    const mark = (index: number | null, kind: 'active' | 'selected') => {
      const p = index === null ? undefined : model?.points[index];
      if (!p) return;
      const x = u.valToPos(p.midMs, 'x', true);
      const y = u.valToPos(p.average, 'y', true);
      ctx.save();
      if (kind === 'active') {
        ctx.strokeStyle = color.focus();
        ctx.lineWidth = 1.5 * px;
        ctx.setLineDash([4 * px, 3 * px]);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + height);
        ctx.stroke();
      } else {
        ctx.fillStyle = p.provisional ? color.provisional() : color.line();
        ctx.strokeStyle = color.text();
        ctx.lineWidth = 2 * px;
        ctx.beginPath();
        ctx.arc(x, y, 5 * px, 0, 2 * Math.PI);
        ctx.fill();
        ctx.stroke();
      }
      ctx.restore();
    };
    mark(active, 'active');
    mark(selected, 'selected');
  };

  const create = (UPlot: typeof uPlot, m: ChartModel) => {
    const { width, height } = size();
    const opts: uPlot.Options = {
      width,
      height,
      legend: { show: false },
      // Our own cursor and markers; no drag-to-zoom, no hover points per series.
      cursor: {
        x: false,
        y: false,
        points: { show: false },
        // No drag-to-zoom. `click` replaces uPlot's default post-drag handler, which stops the
        // click event whenever the pointer moved at all between press and release (even 1 px),
        // so an ordinary slightly-moving click or tap would select nothing.
        drag: { x: false, y: false, setScale: false, click: () => undefined },
      },
      select: { show: false, left: 0, top: 0, width: 0, height: 0 },
      scales: {
        x: { time: false, range: () => [model?.rangeStartMs ?? 0, model?.rangeEndMs ?? 1] },
        y: {
          range: (_u, min, max) => {
            const lo = Number.isFinite(min) ? min : 40;
            const hi = Number.isFinite(max) ? max : 120;
            return [Math.floor((lo - 5) / 10) * 10, Math.ceil((hi + 5) / 10) * 10];
          },
        },
      },
      series: [
        {},
        {
          // The band's edges: thin lines in the muted text colour, which has at least 3:1
          // contrast with the surface (WCAG 1.4.11); the fill between them is only a tint.
          label: 'Maximum',
          stroke: color.muted,
          width: 1,
          spanGaps: false,
          points: { show: false },
        },
        {
          label: 'Minimum',
          stroke: color.muted,
          width: 1,
          spanGaps: false,
          points: { show: false },
        },
        {
          label: 'Five-minute average (final)',
          stroke: color.line,
          width: 2,
          spanGaps: false,
          points: { show: false },
        },
        {
          label: 'Five-minute average (provisional)',
          stroke: color.provisional,
          width: 2,
          dash: [6, 4],
          spanGaps: false,
          points: {
            show: true,
            size: 6,
            width: 1.5,
            stroke: color.provisional,
            fill: color.surface,
          },
        },
      ],
      bands: [{ series: [1, 2], fill: () => withAlpha(color.line(), BAND_ALPHA) }],
      axes: [
        {
          stroke: color.muted,
          font: font(),
          grid: { stroke: color.grid, width: 1 },
          ticks: { stroke: color.grid, width: 1 },
          size: 54,
          splits: (u) =>
            model
              ? timeTicks(
                  model.rangeStartMs,
                  model.rangeEndMs,
                  model.segments,
                  Math.floor(u.bbox.width / (devicePixelRatio || 1) / PX_PER_TICK),
                ).map((t) => t.ms)
              : [],
          values: (u, splits) => {
            if (!model) return splits.map(() => '');
            const ticks = timeTicks(
              model.rangeStartMs,
              model.rangeEndMs,
              model.segments,
              Math.floor(u.bbox.width / (devicePixelRatio || 1) / PX_PER_TICK),
            );
            const byMs = new Map(ticks.map((t) => [t.ms, t.label]));
            return splits.map((ms) => byMs.get(ms) ?? '');
          },
        },
        {
          stroke: color.muted,
          font: font(),
          labelFont: font(),
          grid: { stroke: color.grid, width: 1 },
          ticks: { show: false },
          label: 'beats/min',
          labelSize: 18,
          size: 40,
        },
      ],
      hooks: {
        draw: [drawMarkers],
        setCursor: [
          (u) => {
            const left = u.cursor.left;
            callbacks.onHover(left === undefined || left < 0 ? null : pointAt(u, left));
          },
        ],
      },
    };
    const u = new UPlot(opts, data(m), container);
    // A click or tap picks the nearest window. (uPlot's own click handling is for zooming,
    // which is turned off.)
    u.over.addEventListener(
      'click',
      (event) => {
        const rect = u.over.getBoundingClientRect();
        const p = pointAt(u, event.clientX - rect.left);
        if (p !== null) callbacks.onPick(p);
      },
      { signal },
    );
    return u;
  };

  const resize = new ResizeObserver(() => {
    if (plot) plot.setSize(size());
  });
  resize.observe(container);

  // Colours come from the tokens at draw time, so a light/dark switch only needs a redraw.
  const scheme = matchMedia('(prefers-color-scheme: dark)');
  scheme.addEventListener(
    'change',
    () => {
      plot?.redraw(false, true);
    },
    { signal },
  );

  const handle: ChartHandle = {
    update(next) {
      model = next;
      if (plot) {
        plot.setData(data(next));
        return;
      }
      loadUplot().then(
        (UPlot) => {
          // Created once, from the newest model, unless the dashboard went away meanwhile.
          if (plot || signal.aborted || !model) return;
          plot = create(UPlot, model);
          plot.redraw(false, false);
        },
        (error: unknown) => {
          console.error('Loading the chart library failed', error);
          container.textContent = UNSUPPORTED;
        },
      );
    },
    mark(nextActive, nextSelected) {
      active = nextActive;
      selected = nextSelected;
      plot?.redraw(false, false);
    },
    destroy() {
      resize.disconnect();
      plot?.destroy();
      plot = null;
    },
  };
  signal.addEventListener('abort', () => {
    handle.destroy();
  });
  return handle;
};
