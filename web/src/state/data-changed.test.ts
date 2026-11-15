import { describe, expect, it, vi } from 'vitest';
import { createDataChangeBus, type DataChangeKind } from './data-changed';

describe('data-change bus', () => {
  it('tells every listener which kind of data changed, without calling them at subscribe', () => {
    const bus = createDataChangeBus();
    const a: DataChangeKind[] = [];
    const b: DataChangeKind[] = [];
    bus.subscribe((k) => a.push(k));
    bus.subscribe((k) => b.push(k));
    expect(a).toEqual([]);
    bus.notify('cuff-send');
    bus.notify('share');
    expect(a).toEqual(['cuff-send', 'share']);
    expect(b).toEqual(['cuff-send', 'share']);
  });

  it('unsubscribes with the returned function or when the signal aborts', () => {
    const bus = createDataChangeBus();
    const a = vi.fn();
    const b = vi.fn();
    const off = bus.subscribe(a);
    const controller = new AbortController();
    bus.subscribe(b, controller.signal);
    off();
    controller.abort();
    bus.notify('sync');
    expect(a).not.toHaveBeenCalled();
    expect(b).not.toHaveBeenCalled();
  });

  it('keeps notifying the others when one listener throws', () => {
    const bus = createDataChangeBus();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const after = vi.fn();
    bus.subscribe(() => {
      throw new Error('broken listener');
    });
    bus.subscribe(after);
    bus.notify('cuff-send');
    expect(after).toHaveBeenCalledWith('cuff-send');
  });
});
