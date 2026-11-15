import { describe, expect, it } from 'vitest';
import type { PulseBridgeApi } from '../api/contract';
import { createSelectionStore, type Selection } from '../state/selection';
import { mountShare } from './index';

function mount() {
  const slots = {
    action: document.createElement('div'),
    trace: document.createElement('div'),
    readback: document.createElement('div'),
  };
  const selection = createSelectionStore();
  const controller = new AbortController();
  mountShare({
    client: {} as PulseBridgeApi,
    selection,
    slots,
    signal: controller.signal,
    refreshStatus: () => undefined,
    notifyDataChanged: () => undefined,
    onDataChanged: () => undefined,
  });
  const button = slots.action.querySelector('button');
  if (!button) throw new Error('no share button');
  return { button, selection, controller, hint: slots.action.querySelector('.share__hint') };
}

const watch = (over: Partial<Extract<Selection, { source: 'watch' }>> = {}): Selection => ({
  source: 'watch',
  observationId: 'hr-1',
  summary: 'Heart rate 61 /min',
  unshareableReason: null,
  ...over,
});
const cuff = (over: Partial<Extract<Selection, { source: 'cuff' }>> = {}): Selection => ({
  source: 'cuff',
  attemptId: 'a',
  observationId: 'bp-1',
  summary: 'BP 120/80',
  unshareableReason: null,
  ...over,
});

describe('Share with clinic button', () => {
  it('is disabled until something is selected', () => {
    const { button, hint } = mount();
    expect(button.textContent).toBe('Share with clinic');
    expect(button.disabled).toBe(true);
    expect(hint?.textContent).toMatch(/Select a heart-rate point or a cuff reading/);
  });

  it('is enabled when a heart-rate point or a stored cuff reading is selected', () => {
    const { button, selection, hint } = mount();
    selection.set(watch());
    expect(button.disabled).toBe(false);
    expect(hint?.textContent).toContain('Heart rate 61 /min');
    selection.set(cuff());
    expect(button.disabled).toBe(false);
    selection.set(null);
    expect(button.disabled).toBe(true);
  });

  it('stays disabled for a cuff reading that produced no Observation', () => {
    const { button, selection, hint } = mount();
    selection.set(cuff({ observationId: null, summary: 'Rejected' }));
    expect(button.disabled).toBe(true);
    expect(hint?.textContent).toContain('nothing to share');
  });

  it('stays disabled, and says why, for a selection marked not shareable (a preliminary window)', () => {
    const { button, selection, hint } = mount();
    const reason = 'this window is still preliminary; only final readings are shared.';
    selection.set(watch({ unshareableReason: reason }));
    expect(button.disabled).toBe(true);
    expect(hint?.textContent).toContain(reason);
    expect(button.getAttribute('aria-describedby')).toBe(hint?.id);
  });

  it('stops following the selection once the dashboard is torn down', () => {
    const { button, selection, controller } = mount();
    controller.abort();
    selection.set(watch());
    expect(button.disabled).toBe(true);
  });
});
