import { afterEach, describe, expect, it } from 'vitest';
import type { HeartRateTrace } from '../api/contract';
import { hrObservation } from './test-support';
import { tracePanel } from './trace';

const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

function trace(over: Partial<HeartRateTrace> = {}): HeartRateTrace {
  const observation = hrObservation({
    start: '2026-01-01T09:05:00-05:00',
    avg: 68.2,
    min: 64,
    max: 75,
    id: 'w1',
  });
  return {
    observation,
    provenance: {
      resourceType: 'Provenance',
      id: 'p1',
      target: [{ reference: 'Observation/w1' }],
      recorded: '2026-01-01T14:20:03+00:00',
      agent: [{ who: { reference: 'Device/d1' } }],
    },
    device: {
      resourceType: 'Device',
      id: 'd1',
      deviceName: [{ name: 'Synthetic watch', type: 'user-friendly-name' }],
    },
    source: {
      startTime: '2026-01-01T14:05:00Z',
      endTime: '2026-01-01T14:10:00Z',
      heartRate: { beatsPerMinuteMin: 64, beatsPerMinuteAvg: 68.2, beatsPerMinuteMax: 75 },
    },
    ...over,
  };
}

let controller: AbortController;
afterEach(() => {
  controller.abort();
});

function setup() {
  controller = new AbortController();
  const slot = document.createElement('div');
  document.body.append(slot);
  return { slot, panel: tracePanel(slot, controller.signal) };
}

const tabs = (slot: HTMLElement) =>
  [...slot.querySelectorAll('[role="tab"]')].map((t) => t.textContent);
const panelFor = (slot: HTMLElement, label: string) => {
  const tab = [...slot.querySelectorAll('[role="tab"]')].find((t) => t.textContent === label);
  const id = tab?.getAttribute('aria-controls') ?? '';
  return slot.querySelector<HTMLElement>(`#${id}`);
};

describe('right trace panel', () => {
  it('starts with a hint', () => {
    const { slot, panel } = setup();
    panel.render({ kind: 'empty' });
    expect(slot.textContent).toContain('Select a point on the chart');
    expect(slot.querySelector('[role="tablist"]')).toBeNull();
  });

  it('has the two tabs, and shows the Observation, Provenance and Device as JSON text', () => {
    const { slot, panel } = setup();
    const t = trace();
    panel.render({ kind: 'ready', trace: t });
    expect(tabs(slot)).toEqual(['FHIR as stored', 'Google Health JSON it came from']);
    const fhir = panelFor(slot, 'FHIR as stored');
    const blocks = [...(fhir?.querySelectorAll('pre code') ?? [])].map((c) => c.textContent);
    expect(blocks).toEqual([
      JSON.stringify(t.observation, null, 2),
      JSON.stringify(t.provenance, null, 2),
      JSON.stringify(t.device, null, 2),
    ]);
    expect(fhir?.textContent).toContain('Observation/w1');
    expect(fhir?.textContent).toContain('Provenance/p1');
    expect(fhir?.textContent).toContain('Device/d1');
  });

  it('labels the Google Health JSON honestly as reconstructed, and shows it as text', () => {
    const { slot, panel } = setup();
    const t = trace();
    panel.render({ kind: 'ready', trace: t });
    const source = panelFor(slot, 'Google Health JSON it came from');
    expect(source?.textContent).toContain('Reconstructed from the stored reading');
    expect(source?.querySelector('pre code')?.textContent).toBe(JSON.stringify(t.source, null, 2));
  });

  it('says so when the server returns no source window, Provenance or Device', () => {
    const { slot, panel } = setup();
    panel.render({ kind: 'ready', trace: trace({ source: null, provenance: null, device: null }) });
    expect(panelFor(slot, 'Google Health JSON it came from')?.textContent).toContain(
      'did not return the source window',
    );
    const fhir = panelFor(slot, 'FHIR as stored')?.textContent ?? '';
    expect(fhir).toContain('No Provenance was returned');
    expect(fhir).toContain('No Device was returned');
  });

  it('shows a hostile string in a resource as characters, never as markup', () => {
    const { slot, panel } = setup();
    const t = trace();
    t.observation.code = { text: HOSTILE };
    t.device = { resourceType: 'Device', id: HOSTILE, deviceName: [{ name: HOSTILE, type: 'x' }] };
    panel.render({ kind: 'ready', trace: t });
    expect(slot.querySelector('img, script')).toBeNull();
    expect(slot.textContent).toContain(HOSTILE);
    // JSON.stringify escapes the quotes inside; the angle brackets stay visible as text.
    expect(panelFor(slot, 'FHIR as stored')?.textContent).toContain(
      '<img src=x onerror=\\"alert(1)\\">',
    );
  });

  it('shows a failed trace call as an alert', () => {
    const { slot, panel } = setup();
    panel.render({ kind: 'error', summary: 'Heart rate 68 /min', message: 'Not found' });
    expect(slot.querySelector('[role="alert"]')?.textContent).toBe(
      'Could not load the trace for Heart rate 68 /min: Not found',
    );
  });

  it('keeps the chosen tab when the next trace arrives', () => {
    const { slot, panel } = setup();
    panel.render({ kind: 'ready', trace: trace() });
    const sourceTab = [...slot.querySelectorAll<HTMLButtonElement>('[role="tab"]')][1];
    sourceTab?.click();
    panel.render({ kind: 'ready', trace: trace() });
    expect(sourceTab?.getAttribute('aria-selected')).toBe('true');
  });
});
