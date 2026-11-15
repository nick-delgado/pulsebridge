/**
 * The heart-rate area: range switch, chart, statistics, the selected window's details, and the
 * right trace panel. `mountHeartRate` (index.ts) calls `createHeartRate` with the real uPlot chart;
 * the unit tests pass a stand-in chart and a fixed clock.
 *
 * Flow:
 *   load(range) ─ abort the previous load ─ searchObservations ─▶ prepareReadings
 *     ─▶ chart.update, statistics, summary; or the empty / error state
 *     (getStatus runs alongside; when it answers, a "reconnect needed" note is added. The chart
 *     never waits for it.)
 *   click, tap, or Enter on the keyboard cursor ─▶ selection.set(selectionFor(point))
 *     ─▶ details under the chart, and getHeartRateTrace ─▶ trace panel; the Observation the
 *     trace returns re-checks the selection (it may be newer than the chart's copy).
 *   onDataChanged('sync') ─▶ load(range) again, then reconcileSelection: a selected window is
 *     re-set from the new data (a window that became `final` becomes shareable), dropped if it
 *     was withdrawn or is gone from a range that should contain it, and re-checked through the
 *     trace call if it lies outside the range now shown.
 *
 * Accessibility: the chart is never the only way in. The plot area is a keyboard "slider" over the
 * windows (Left/Right one window, Page Up/Down one hour, Home/End), whose value text is the whole
 * window in words; Enter or Space selects it. The summary under the chart states the range, the
 * count and the latest value, and the statistics are plain text.
 */
import type { PulseBridgeApi, StatusSummary } from '../api/contract';
import { ApiError, UnauthorizedError } from '../api/errors';
import type { FeatureContext, HeartRateSlot } from '../shell/features';
import type { Selection } from '../state/selection';
import { append, el, replaceChildren } from '../ui/dom';
import type { ChartFactory, ChartHandle } from './chart';
import {
  bpm,
  chartSeries,
  describePoint,
  HEART_RATE_CODE,
  nearestPoint,
  noReadings,
  prepareReadings,
  selectionFor,
  statusText,
  toPoint,
  windowStats,
  type HeartRatePoint,
  type PreparedReadings,
} from './data';
import { DAY_MS, formatOffset, formatWindow, HOUR_MS } from './time';
import { tracePanel } from './trace';

export const RANGES = [
  { id: '24h', label: '24 h', name: 'last 24 hours', ms: DAY_MS },
  { id: '7d', label: '7 d', name: 'last 7 days', ms: 7 * DAY_MS },
  { id: '30d', label: '30 d', name: 'last 30 days', ms: 30 * DAY_MS },
] as const;
export type RangeId = (typeof RANGES)[number]['id'];

/** Page Up / Page Down move by this much time, to the window nearest that moment. */
const PAGE_STEP_MS = HOUR_MS;

export interface HeartRateDeps {
  chart: ChartFactory;
  now: () => number;
}

/** True for the rejection of a call whose signal was aborted (fetch and the mock both use this). */
function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

/** An AbortController that also aborts when `parent` does (logout tears the dashboard down). */
function childController(parent: AbortSignal): AbortController {
  const c = new AbortController();
  if (parent.aborted) c.abort();
  else
    parent.addEventListener(
      'abort',
      () => {
        c.abort();
      },
      { once: true, signal: c.signal },
    );
  return c;
}

/** 8370 → "8,370" (fixed locale, like the rest of the page's English text). */
const count = (n: number): string => n.toLocaleString('en-US');

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'unknown error';
}

export function createHeartRate(context: FeatureContext<HeartRateSlot>, deps: HeartRateDeps): void {
  const { client, selection, slots, signal } = context;

  // -- static structure ------------------------------------------------------------------------
  const rangeButtons = new Map<RangeId, HTMLButtonElement>();
  const rangeGroup = el('div', {
    class: 'hr-ranges',
    attrs: { role: 'group', 'aria-label': 'Time range' },
  });
  for (const r of RANGES) {
    const b = el('button', {
      class: 'button hr-ranges__button',
      text: r.label,
      attrs: { type: 'button', 'aria-pressed': 'false', 'aria-label': `Show the ${r.name}` },
      on: {
        click: () => {
          if (range !== r.id) load(r.id);
        },
      },
      signal,
    });
    rangeButtons.set(r.id, b);
    rangeGroup.appendChild(b);
  }
  const zoneNote = el('p', { class: 'hr-zone' });
  const message = el('div', { class: 'hr-message', attrs: { role: 'status' } });
  const summaryId = 'hr-summary';
  const summary = el('p', { class: 'hr-summary', id: summaryId });
  const canvasHost = el('div', { class: 'hr-chart__canvas' });
  const cursor = el(
    'div',
    {
      class: 'hr-chart',
      attrs: {
        role: 'slider',
        tabindex: '0',
        'aria-label':
          'Heart-rate windows. Left and Right arrows move one window, Page Up and Page Down one hour, Home and End to the ends; Enter selects.',
        'aria-describedby': summaryId,
        'aria-valuemin': '1',
        'aria-valuemax': '1',
        'aria-valuenow': '1',
      },
    },
    canvasHost,
  );
  const legend = el(
    'ul',
    { class: 'hr-legend', attrs: { 'aria-label': 'Chart key' } },
    el(
      'li',
      {},
      el('span', { class: 'hr-swatch hr-swatch--line', attrs: { 'aria-hidden': 'true' } }),
      'Five-minute average',
    ),
    el(
      'li',
      {},
      el('span', { class: 'hr-swatch hr-swatch--band', attrs: { 'aria-hidden': 'true' } }),
      'Minimum to maximum within each window',
    ),
    el(
      'li',
      {},
      el('span', { class: 'hr-swatch hr-swatch--provisional', attrs: { 'aria-hidden': 'true' } }),
      'Provisional: not final yet, cannot be shared',
    ),
  );
  const readout = el('p', { class: 'hr-readout' });
  const chartArea = el('div', { class: 'hr-chart-area' }, cursor, legend, readout);
  const stats = el('div', { class: 'hr-stats' });
  const details = el('section', {
    class: 'hr-details',
    attrs: { 'aria-label': 'Selected reading', 'aria-live': 'polite' },
  });

  append(
    slots.panel,
    el('div', { class: 'hr-toolbar' }, rangeGroup, zoneNote),
    message,
    chartArea,
    summary,
    stats,
    details,
  );
  const trace = tracePanel(slots.trace, signal);
  trace.render({ kind: 'empty' });

  // -- state -----------------------------------------------------------------------------------
  let range: RangeId = '24h';
  let readings: PreparedReadings = noReadings();
  /** Keyboard / hover cursor, as an index into readings.points. */
  let active: number | null = null;
  /** The selected point, while the page selection is one of ours and in the shown data. */
  let selectedIndex: number | null = null;
  /**
   * The point behind the page selection while it is ours, even when it is not in the range now
   * shown (needed to tell "outside this range" from "gone"). Null otherwise.
   */
  let selectedPoint: HeartRatePoint | null = null;
  /** The latest status answer for the current load, or null while none has arrived. */
  let status: StatusSummary | null = null;
  /** Whether the current load's observations have arrived. */
  let loaded = false;
  let loadController: AbortController | null = null;
  let traceController: AbortController | null = null;
  let chart: ChartHandle | null = null;

  const points = () => readings.points;

  const ensureChart = (): ChartHandle => {
    chart ??= deps.chart(
      canvasHost,
      {
        onHover: (p) => {
          showReadout(p ?? active);
        },
        onPick: (p) => {
          setActive(p);
          pick(p);
        },
      },
      signal,
    );
    return chart;
  };

  const redrawMarks = () => {
    chart?.mark(active, selectedIndex);
  };

  const showReadout = (index: number | null) => {
    const p = index === null ? undefined : points()[index];
    readout.textContent = p
      ? describePoint(p)
      : 'Point at the chart, tap it, or focus it and use the arrow keys.';
  };

  const setActive = (index: number | null) => {
    const n = points().length;
    active = index === null || n === 0 ? null : Math.max(0, Math.min(n - 1, index));
    const p = active === null ? undefined : points()[active];
    cursor.setAttribute('aria-valuemax', String(Math.max(1, n)));
    cursor.setAttribute('aria-valuenow', String((active ?? 0) + 1));
    let valueText = 'No readings in this range';
    if (p) valueText = describePoint(p);
    else if (n > 0) valueText = `${count(n)} windows; press an arrow key to move through them`;
    cursor.setAttribute('aria-valuetext', valueText);
    showReadout(active);
    redrawMarks();
  };

  // -- selection -------------------------------------------------------------------------------
  const renderDetails = (p: HeartRatePoint | null, device?: string) => {
    if (!p) {
      replaceChildren(details);
      return;
    }
    const source = device ? `${device}, via Google Health` : 'Watch, via Google Health';
    replaceChildren(
      details,
      el('h3', { class: 'hr-details__title', text: 'Selected reading' }),
      el(
        'dl',
        { class: 'hr-details__list' },
        el('dt', { text: 'Time' }),
        el('dd', { text: formatWindow(p.startMs, p.endMs, p.offsetMinutes) }),
        el('dt', { text: 'Value' }),
        el('dd', {
          text:
            `${bpm(p.average)} beats/min average` +
            (p.minimum !== null && p.maximum !== null
              ? ` (minimum ${bpm(p.minimum)}, maximum ${bpm(p.maximum)})`
              : ''),
        }),
        el('dt', { text: 'Status' }),
        el('dd', { class: p.provisional ? 'hr-provisional' : '', text: statusText(p) }),
        el('dt', { text: 'Source' }),
        el('dd', { text: source }),
      ),
    );
  };

  /** Clears the page selection because the selected reading is no longer a result, and says so. */
  const dropSelection = (summaryText: string) => {
    selectedPoint = null;
    selection.set(null); // the subscriber clears the highlight, the details and the trace
    trace.render({ kind: 'gone', summary: summaryText });
  };

  /** Re-sets the page selection from a (possibly newer) copy of the selected window. */
  const refreshSelection = (p: HeartRatePoint) => {
    selectedPoint = p;
    selection.set(selectionFor(p));
  };

  const loadTrace = (p: HeartRatePoint, sel: Selection) => {
    traceController?.abort();
    const controller = childController(signal);
    traceController = controller;
    trace.render({ kind: 'loading', summary: sel.summary });
    client.getHeartRateTrace(p.id, { signal: controller.signal }).then(
      (result) => {
        // A newer pick, a deselection or a logout replaced this request: its answer is stale.
        if (controller.signal.aborted) return;
        // The trace's Observation is the freshest copy: re-check the selection against it.
        const fresh = toPoint(result.observation);
        if (typeof fresh === 'string') {
          dropSelection(sel.summary);
          return;
        }
        refreshSelection(fresh);
        trace.render({ kind: 'ready', trace: result });
        renderDetails(fresh, result.device?.deviceName?.[0]?.name);
      },
      (error: unknown) => {
        if (isAbort(error, controller.signal) || error instanceof UnauthorizedError) return;
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
          dropSelection(sel.summary);
          return;
        }
        trace.render({ kind: 'error', summary: sel.summary, message: errorMessage(error) });
      },
    );
  };

  const pick = (index: number) => {
    const p = points()[index];
    if (!p) return;
    const sel = selectionFor(p);
    selectedIndex = index;
    selectedPoint = p;
    selection.set(sel);
    renderDetails(p);
    redrawMarks();
    loadTrace(p, sel);
  };

  selection.subscribe((current) => {
    if (current?.source === 'watch') {
      const i = points().findIndex((p) => p.id === current.observationId);
      selectedIndex = i < 0 ? null : i;
    } else {
      // The selection moved to the cuff side (or was cleared): drop our highlight and trace.
      selectedIndex = null;
      selectedPoint = null;
      traceController?.abort();
      renderDetails(null);
      trace.render({ kind: 'empty' });
    }
    redrawMarks();
  }, signal);

  // -- keyboard --------------------------------------------------------------------------------
  cursor.addEventListener(
    'keydown',
    (event) => {
      const n = points().length;
      if (n === 0) return;
      const from = active ?? selectedIndex ?? n - 1;
      // Page Up / Down move by time, not by a count of windows, so a gap does not make "one
      // hour" jump further.
      const fromMid = points()[from]?.midMs ?? 0;
      const moves: Record<string, number> = {
        ArrowRight: from + 1,
        ArrowUp: from + 1,
        ArrowLeft: from - 1,
        ArrowDown: from - 1,
        PageUp: nearestPoint(points(), fromMid + PAGE_STEP_MS),
        PageDown: nearestPoint(points(), fromMid - PAGE_STEP_MS),
        Home: 0,
        End: n - 1,
      };
      const target = moves[event.key];
      if (target !== undefined) {
        event.preventDefault();
        setActive(active === null ? from : target);
        return;
      }
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (active === null) setActive(from);
        if (active !== null) pick(active);
      }
    },
    { signal },
  );
  cursor.addEventListener(
    'focus',
    () => {
      if (active === null && points().length > 0) setActive(selectedIndex ?? points().length - 1);
    },
    { signal },
  );

  // -- rendering a loaded range ----------------------------------------------------------------
  const rangeSpec = (id: RangeId) => RANGES.find((r) => r.id === id) ?? RANGES[0];

  const renderStats = () => {
    const s = windowStats(points());
    if (!s) {
      replaceChildren(stats);
      return;
    }
    const stat = (label: string, value: string, help: string) =>
      el(
        'div',
        { class: 'hr-stat' },
        el('dt', { class: 'hr-stat__label', text: label }),
        el('dd', { class: 'hr-stat__value', text: value }),
        el('dd', { class: 'hr-stat__help', text: help }),
      );
    replaceChildren(
      stats,
      el(
        'dl',
        {
          class: 'hr-stats__list',
          attrs: { 'aria-label': `Statistics for the ${rangeSpec(range).name}` },
        },
        stat('Minimum', `${bpm(s.minimum)} /min`, 'lowest five-minute minimum'),
        stat('Mean', `${s.mean.toFixed(1)} /min`, 'mean of the five-minute averages'),
        stat('Maximum', `${bpm(s.maximum)} /min`, 'highest five-minute maximum'),
        stat(
          'Windows',
          count(s.count),
          s.provisional === 0 ? 'none provisional' : `${count(s.provisional)} provisional`,
        ),
      ),
      el('p', {
        class: 'hr-stats__note',
        text: 'The mean counts every five-minute window once. It is not weighted by time or by the number of samples, and hours with no data are left out.',
      }),
    );
  };

  const renderZone = () => {
    const offsets = [...new Set(readings.segments.map((s) => s.offsetMinutes))].map(formatOffset);
    zoneNote.textContent =
      offsets.length === 0
        ? 'Times are shown as recorded by the watch.'
        : offsets.length === 1
          ? `Times as recorded by the watch (${offsets[0] ?? ''}).`
          : `Times as recorded by the watch (${offsets.join(', then ')}: a clock change falls in this range).`;
  };

  const renderSummary = () => {
    const ps = points();
    const latest = ps.at(-1);
    const spec = rangeSpec(range);
    if (!latest) {
      summary.textContent = '';
      return;
    }
    const first = ps[0] ?? latest;
    let skipped = '';
    if (readings.unreadable > 0)
      skipped += ` ${count(readings.unreadable)} stored readings could not be shown (missing or inconsistent time or values).`;
    if (readings.withdrawn > 0)
      skipped += ` ${count(readings.withdrawn)} withdrawn readings (entered in error or cancelled) are left out.`;
    if (readings.duplicates > 0)
      skipped += ` ${count(readings.duplicates)} duplicate readings for the same window were dropped (the most recently updated is shown).`;
    summary.textContent =
      `Heart rate, ${spec.name}: ${count(ps.length)} five-minute windows from ` +
      `${formatWindow(first.startMs, first.endMs, first.offsetMinutes)} to ${formatWindow(latest.startMs, latest.endMs, latest.offsetMinutes)}. ` +
      `Latest: ${bpm(latest.average)} beats/min, ${statusText(latest)}.${skipped}`;
  };

  const renderMessage = (text: string, tone: 'info' | 'warn' | 'error', retry = false) => {
    replaceChildren(
      message,
      el('p', {
        class: `hr-message__text hr-message__text--${tone}`,
        text,
        attrs: tone === 'error' ? { role: 'alert' } : {},
      }),
      retry
        ? el('button', {
            class: 'button',
            text: 'Try again',
            attrs: { type: 'button' },
            on: {
              click: () => {
                load(range);
              },
            },
            signal,
          })
        : null,
    );
  };

  /** The message above the chart once data is in: empty range, and the watch-sync state if known. */
  const renderNotice = () => {
    const reconnect = status?.watchSync.state === 'reconnect-needed';
    if (points().length === 0) {
      let text = 'No heart-rate data in this range.';
      if (reconnect)
        text +=
          ' Reconnect needed: the Google Health connection has expired, so no new readings arrive until the watch is reconnected.';
      else if (status?.watchSync.state === 'never-synced') text += ' The watch has not synced yet.';
      renderMessage(text, reconnect ? 'warn' : 'info');
    } else if (reconnect) {
      renderMessage(
        'Reconnect needed: the Google Health connection has expired, so newer readings are missing until the watch is reconnected.',
        'warn',
      );
    } else {
      replaceChildren(message);
    }
  };

  const showData = (startMs: number, endMs: number) => {
    renderZone();
    renderNotice();
    if (points().length === 0) {
      chartArea.hidden = true;
      replaceChildren(stats);
      summary.textContent = '';
      return;
    }
    chartArea.hidden = false;
    ensureChart().update({
      points: points(),
      series: chartSeries(points()),
      segments: readings.segments,
      rangeStartMs: startMs,
      rangeEndMs: endMs,
    });
    renderStats();
    renderSummary();
  };

  /**
   * After new data, check the selected window against it:
   *   - found: re-set it from the new copy (a window that became `final` becomes shareable); if
   *     its status or version changed, reload the trace so it shows the same version;
   *   - missing although its window overlaps the range just loaded (it was withdrawn, so left out
   *     of the points, or removed): drop the selection;
   *   - missing because its window lies outside the range now shown: keep it, but re-check it
   *     through the trace call, which drops or updates it from the stored Observation.
   */
  const reconcileSelection = (startMs: number, endMs: number) => {
    const current = selection.get();
    if (current?.source !== 'watch') {
      selectedIndex = null;
      return;
    }
    const i = points().findIndex((p) => p.id === current.observationId);
    selectedIndex = i < 0 ? null : i;
    const p = i < 0 ? undefined : points()[i];
    const before = selectedPoint;
    if (p) {
      refreshSelection(p);
      renderDetails(p);
      const changed =
        before !== null &&
        (before.status !== p.status ||
          before.observation.meta?.versionId !== p.observation.meta?.versionId);
      if (changed) loadTrace(p, selectionFor(p));
      return;
    }
    // Same overlap rule as the search (date=gt<start>&date=lt<end> on a Period): the search
    // would have returned the window, so it is gone. This also covers a window that came back
    // `entered-in-error` or `cancelled`: the search returns those only inside the range, and
    // prepareReadings leaves them out of the points.
    const inRange = before !== null && before.endMs > startMs && before.startMs < endMs;
    if (inRange || before === null) {
      dropSelection(current.summary);
      return;
    }
    loadTrace(before, current);
  };

  // -- loading ---------------------------------------------------------------------------------
  function load(next: RangeId): void {
    range = next;
    for (const [id, b] of rangeButtons) b.setAttribute('aria-pressed', String(id === next));
    loadController?.abort();
    const controller = childController(signal);
    loadController = controller;
    const endMs = deps.now();
    const startMs = endMs - rangeSpec(next).ms;
    slots.panel.setAttribute('aria-busy', 'true');
    renderMessage(`Loading the ${rangeSpec(next).name}…`, 'info');
    status = null;
    loaded = false;

    // The status tells an empty range apart from "reconnect needed". The chart does not wait for
    // it: the note is added when it answers. Its failure is the status strip's to report.
    getStatusQuietly(client, controller.signal).then((st) => {
      if (controller.signal.aborted || st === null) return;
      status = st;
      if (loaded) renderNotice();
    }, ignore);

    client
      .searchObservations(
        { code: HEART_RATE_CODE, range: { start: new Date(startMs), end: new Date(endMs) } },
        { signal: controller.signal },
      )
      .then(
        (obs) => {
          if (controller.signal.aborted) return;
          slots.panel.removeAttribute('aria-busy');
          readings = prepareReadings(obs);
          loaded = true;
          active = null;
          showData(startMs, endMs);
          reconcileSelection(startMs, endMs);
          setActive(null);
        },
        (error: unknown) => {
          if (isAbort(error, controller.signal) || error instanceof UnauthorizedError) return;
          slots.panel.removeAttribute('aria-busy');
          readings = noReadings();
          chartArea.hidden = true;
          replaceChildren(stats);
          summary.textContent = '';
          renderMessage(`Could not load heart-rate data: ${errorMessage(error)}`, 'error', true);
        },
      );
  }

  context.onDataChanged((kind) => {
    if (kind === 'sync') load(range);
  }, signal);

  chartArea.hidden = true;
  load(range);
}

function ignore(): void {
  /* nothing to do */
}

function getStatusQuietly(
  client: PulseBridgeApi,
  signal: AbortSignal,
): Promise<StatusSummary | null> {
  return client.getStatus({ signal }).catch(() => null);
}
