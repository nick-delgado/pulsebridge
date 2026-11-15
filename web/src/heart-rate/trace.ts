/**
 * The right trace panel: what one selected heart-rate window looks like underneath.
 *
 * Two tabs (brief, "Trace panels"):
 *   - "FHIR as stored": the Observation, its Provenance (who and what produced it) and the Device
 *     (the watch), exactly as the client's trace call returns them, pretty-printed as text.
 *   - "Google Health JSON it came from": the roll-up window, labelled from the trace's
 *     `source.kind`: `reconstructed` (today: PulseBridge keeps no raw API responses, so the proxy
 *     rebuilds the window from the stored Observation), `original`, or `unknown` (no claim).
 *
 * Every value is shown with `codeBlock` / `el`, which only ever create text nodes, so a hostile
 * string inside a resource (a device name, a display text) is shown as characters, never markup.
 */
import type { HeartRateTrace } from '../api/contract';
import { codeBlock, el, placeholder, replaceChildren } from '../ui/dom';
import { createTabs, type Tabs } from '../ui/tabs';

export const TRACE_TABS = {
  fhir: { id: 'fhir', label: 'FHIR as stored' },
  source: { id: 'source', label: 'Google Health JSON it came from' },
} as const;

export type TraceState =
  | { kind: 'empty' }
  | { kind: 'loading'; summary: string }
  | { kind: 'error'; summary: string; message: string }
  /** The selected reading was withdrawn or is no longer stored, so it was deselected. */
  | { kind: 'gone'; summary: string }
  | { kind: 'ready'; trace: HeartRateTrace };

export interface TracePanel {
  render(state: TraceState): void;
}

export function tracePanel(slot: HTMLElement, signal: AbortSignal): TracePanel {
  let tabs: Tabs | null = null;
  const ensureTabs = (): Tabs => {
    if (!tabs) {
      tabs = createTabs('Heart-rate trace', [TRACE_TABS.fhir, TRACE_TABS.source], signal);
    }
    return tabs;
  };

  return {
    render(state) {
      if (state.kind === 'empty') {
        replaceChildren(
          slot,
          placeholder(
            'Select a point on the chart to see its trace: the FHIR Observation as stored and the Google Health JSON it came from.',
          ),
        );
        return;
      }
      if (state.kind === 'loading') {
        replaceChildren(
          slot,
          el('p', {
            class: 'placeholder',
            text: `Loading the trace for ${state.summary}…`,
            attrs: { role: 'status' },
          }),
        );
        return;
      }
      if (state.kind === 'gone') {
        replaceChildren(
          slot,
          el('p', {
            class: 'hr-message__text hr-message__text--warn',
            text: `${state.summary} is no longer a stored result (it was withdrawn or removed), so it is no longer selected.`,
            attrs: { role: 'status' },
          }),
        );
        return;
      }
      if (state.kind === 'error') {
        replaceChildren(
          slot,
          el('p', {
            class: 'hr-error',
            text: `Could not load the trace for ${state.summary}: ${state.message}`,
            attrs: { role: 'alert' },
          }),
        );
        return;
      }
      const t = ensureTabs();
      fillFhir(t.panel(TRACE_TABS.fhir.id), state.trace);
      fillSource(t.panel(TRACE_TABS.source.id), state.trace);
      if (t.root.parentNode !== slot) replaceChildren(slot, t.root);
    },
  };
}

function section(title: string, body: Node): HTMLElement {
  return el(
    'section',
    { class: 'hr-trace__section' },
    el('h4', { class: 'hr-trace__heading', text: title }),
    body,
  );
}

function fillFhir(panel: HTMLElement, trace: HeartRateTrace): void {
  const ref = (type: string, id: string | undefined) => (id ? `${type}/${id}` : type);
  replaceChildren(
    panel,
    el('p', {
      class: 'hr-trace__note',
      text: 'The resources as the FHIR store holds them: the Observation (one five-minute window), the Provenance saying where it came from, and the Device that measured it.',
    }),
    section(
      ref('Observation', trace.observation.id),
      codeBlock(trace.observation, 'Observation JSON'),
    ),
    section(
      trace.provenance ? ref('Provenance', trace.provenance.id) : 'Provenance',
      trace.provenance
        ? codeBlock(trace.provenance, 'Provenance JSON')
        : placeholder('No Provenance was returned for this Observation.'),
    ),
    section(
      trace.device ? ref('Device', trace.device.id) : 'Device',
      trace.device
        ? codeBlock(trace.device, 'Device JSON')
        : placeholder('No Device was returned for this Observation.'),
    ),
  );
}

/** What the source tab says about where the window came from: the trace's own `source.kind`. */
const SOURCE_NOTE = {
  reconstructed:
    'Reconstructed from the stored reading, not the original response: PulseBridge does not keep raw Google Health API responses. This is the roll-up window (dataPoints:rollUp, five-minute windows) rebuilt from the Observation’s period, average, minimum and maximum, in the shape the API returns.',
  original:
    'The original response: the roll-up window (dataPoints:rollUp, five-minute windows) exactly as the Google Health API returned it.',
  unknown:
    'The server did not say whether this roll-up window is the original Google Health response or a reconstruction from the stored reading.',
} as const;

const SOURCE_LABEL = {
  reconstructed: 'Google Health roll-up window JSON (reconstructed)',
  original: 'Google Health roll-up window JSON (original)',
  unknown: 'Google Health roll-up window JSON',
} as const;

function fillSource(panel: HTMLElement, trace: HeartRateTrace): void {
  const source = trace.source;
  replaceChildren(
    panel,
    source ? el('p', { class: 'hr-trace__note', text: SOURCE_NOTE[source.kind] }) : null,
    source
      ? codeBlock(source.window, SOURCE_LABEL[source.kind])
      : placeholder('The server did not return the source window for this reading.'),
  );
}
