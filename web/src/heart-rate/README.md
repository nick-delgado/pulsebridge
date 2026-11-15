# Heart-rate area (T17)

The right side of the dashboard: a chart of the watch's heart rate, the statistics under it, the
selected window's details, and the right trace panel. Mounted by the shell through
`mountHeartRate` (slots `panel` and `trace`, see `web/README.md`).

| File                             | What it does                                                                                                     |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `index.ts`                       | `mountHeartRate`: wires the real chart and clock into `createHeartRate`.                                         |
| `panel.ts`                       | The view and its flow: range switch, loading / empty / error states, keyboard, selection, re-fetch after a sync. |
| `data.ts`                        | Observation → point, chart series with gaps, statistics, selection. Pure functions.                              |
| `time.ts`                        | Times in the reading's recorded UTC offset; axis ticks across daylight-saving changes. Pure.                     |
| `chart.ts`                       | The only file that uses uPlot.                                                                                   |
| `trace.ts`                       | The two trace tabs, rendered as text.                                                                            |
| `heart-rate.css`                 | Styles, from the shell's tokens only.                                                                            |
| `*.test.ts`, `heart-rate.e2e.ts` | Unit tests (jsdom, with a stand-in chart) and browser tests (built mock page).                                   |

## From a stored Observation to a point

Each heart-rate Observation is one five-minute roll-up window (D8), as T06 writes it:
`effectivePeriod` is the window, `valueQuantity` (UCUM `/min`) its average, and the components
LOINC `96935-2` / `55426-1` its minimum and maximum. One Observation becomes one point:

- **the line** is the average, drawn at the middle of the window;
- **the band** is the minimum-to-maximum range inside each window, so a spike that the average
  smooths away is still visible. Its edges are thin lines in the muted text colour (at least 3:1
  against the surface, WCAG 1.4.11); the fill between them is a light tint. The legend swatch is
  drawn the same way;
- **a gap** is drawn where the next window does not start where the previous one ended (the watch
  was off or charging): `data.ts` inserts a row with null values, and uPlot breaks the line and the
  band there instead of joining the two sides;
- **provisional** windows (`preliminary`, the newest ones that may still receive late samples
  (T10), and `registered`) are a dashed line in the warning colour, with a marker on each, and the
  legend, the readout and the details say "provisional". Other non-final statuses (`amended`,
  `corrected`, `unknown`) are drawn on the solid line and described as "status amended: not
  shared (only final is)". Only `final` windows are shareable.

What is left out, and counted in the summary:

- `entered-in-error` and `cancelled` Observations: R4 says they are not results.
- Unreadable ones: no id or valid period; a value, or a minimum/maximum component, that is not
  UCUM `/min` (`system` http://unitsofmeasure.org and `code` `/min`; a bare number is refused); or
  contradictory numbers (minimum above maximum, or the average outside minimum..maximum). The page
  cannot know which of contradictory numbers is wrong, so it shows the problem as a count rather
  than drawing a "repaired" band.
- Duplicates: two Observations for the same window start (writes are idempotent, so this means an
  upstream fault). The most recently updated (`meta.lastUpdated`) is kept.

### 30 days stay responsive

Thirty days is about 8,600 windows. uPlot's line path builder already reduces the points to one
minimum/maximum pair per pixel column while it builds the path, so drawing costs the plot width,
not the number of points. We therefore do **not** aggregate the data for display: every window is
still its own row, and a click (the window whose middle is nearest the pointer,
`pointAtPosition`) or the keyboard cursor (one window per arrow key) always lands on exactly one
stored Observation,
`points[i].observation`, whose id goes into the selection and the trace call. Gap rows are never
selectable because selection works on `points`, not on chart rows.

### Statistics

Computed from exactly what is displayed:

- **Minimum**: the lowest per-window minimum (the window's average where it has no minimum).
- **Mean**: the arithmetic mean of the five-minute averages, every window counted once. It is
  **not** time-weighted or weighted by the number of samples, and hours with no data do not count.
  The page says this under the statistics.
- **Maximum**: the highest per-window maximum.
- **Windows**: how many windows have data, and how many of them are provisional.

## Times and time zones

The axis, the readout and the details show each reading's **own local time as recorded**, taken
from the offset in its `effectivePeriod` (T06 writes the user's zone, for example
`09:05:00-05:00`), never the viewer's browser zone. For a personal health record, "07:30" should
mean 07:30 where the wearer was. The toolbar says which offset is shown.

The x axis is linear in real time (UTC instants), so a daylight-saving change can neither stack
two hours on top of each other nor leave a hole. Ticks fall on round local times in each offset
segment: in spring, hourly ticks read 01:00 then 03:00 (02:00 did not exist); in autumn, 01:00
then "01:00 UTC-05:00" an hour later. The first tick after a change names the new offset, so no
two labels read the same. A window label uses the offset of its start for both ends and names it
("01:55–02:00 (UTC-04:00)").

The mock writes UTC (`Z`), so the mock page shows "UTC+00:00"; the daylight-saving behaviour is
covered by unit tests with hand-made New York data.

## Selection, Share and the trace panel

Click, tap, or Enter/Space on the keyboard cursor selects a window: the shared selection gets
the Observation id, a short summary, and, for a window that is not `final`, an
`unshareableReason`, so the Share button stays disabled and explains why. uPlot's post-drag click
filter is replaced, so a click whose pointer moves a pixel or two still selects.

The selection is kept honest as data changes:

- After a reload (range switch or sync) a selected window that is still there is re-set from the
  new copy (a window that became `final` becomes shareable); if its status or `meta.versionId`
  changed, the trace is reloaded too.
- A selected window that is missing although its window overlaps the range just loaded (it came
  back `entered-in-error` or was removed) is deselected, and the trace panel says why.
- A selected window outside the range now shown stays selected but is re-checked through the
  trace call.
- The Observation the trace call returns is the freshest copy: it re-sets the selection, and a
  withdrawn copy or a 404 deselects it.

The trace panel (`trace.ts`, the shell's `createTabs`) shows:

- **FHIR as stored**: the Observation, Provenance and Device from `getHeartRateTrace`;
- **Google Health JSON it came from**: the roll-up window from the trace's `source`, labelled by
  `source.kind`: `reconstructed` (rebuilt from the stored reading; what the proxy sends today,
  because PulseBridge keeps no raw API responses), `original` (the response as the API returned
  it), or `unknown` (the server made no claim, and the panel says so).

## Accessibility

- The plot is a keyboard **slider** over the windows (`role="slider"`): Left/Right one window,
  Page Up/Down by one hour of time (to the window nearest that moment, so a gap does not make it
  jump further), Home/End; its `aria-valuetext` is the whole window in words. Moving only
  previews; Enter or Space selects.
- A text alternative is always on the page: the summary (range, number of windows, first and
  last window, latest value and its status) is the slider's description, and the statistics and
  details are plain text.
- Visible focus ring (`--color-focus`); colours only from the tokens, checked in the browser test
  (line and provisional line at least 3:1 against the surface, the pressed range button's text
  4.5:1) in light and dark. The canvas reads the tokens at draw time and redraws when the colour
  scheme changes.
- Nothing animates, so `prefers-reduced-motion` needs no change; the CSS keeps it that way.
- At phone width the chart is shorter and fills the width; the statistics wrap to two columns.

## Security

All data is rendered as text through `el()` / `codeBlock()`; nothing here uses an HTML-parsing
API (the lint rules enforce it). uPlot draws axis labels on the canvas with `fillText`; its legend
(which uses `textContent`) is switched off, and the readout is ours.

## The chart library: uPlot 1.6.32

Pinned exactly in `package.json` as the page's first runtime dependency (D5).

|              |                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Licence      | MIT                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Dependencies | none                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Size         | about 51 kB minified, 22 kB gzip. HTTP build before: one script of 19,956 B (7,583 B gzip). After: the main script 41,370 B (15,440 B gzip, our code) plus uPlot as its own chunk, 50,990 B (22,240 B gzip), loaded on first draw; CSS from 8,534 B to 13,570 B.                                                                                                                                                          |
| CSP          | No `eval` or `new Function`, no HTML sinks (`innerHTML` and friends), no `<style>` elements, no `style` attributes set with `setAttribute`. It positions elements through CSSOM (`element.style.left = …`), which `style-src 'self'` allows: that directive blocks inline `<style>` blocks and `style="…"` markup, not property writes from an allowed script. Its stylesheet is bundled with ours, served from `'self'`. |
| Rendering    | Canvas 2D; pixel-column decimation in its path builder, so ~8,600 points draw quickly.                                                                                                                                                                                                                                                                                                                                    |

Alternatives considered: **Chart.js** (MIT, about four times the size, animations to turn off,
tooltips we would replace anyway); **TradingView Lightweight Charts** (Apache-2.0, built for
financial series, one extra dependency); **a hand-written SVG chart** (no dependency, but 8,600
SVG nodes per redraw and axis, band and gap handling to write and test ourselves). uPlot is the
smallest that does time series with bands and gaps well.
