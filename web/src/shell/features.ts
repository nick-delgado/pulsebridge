/**
 * The contract between the shell and the four feature areas. Each area lives in its own
 * directory and exports ONE mount function; the shell calls it once after login with the
 * elements ("slots") it may fill. A feature never reaches outside its slots, so T17, T18 and T19
 * can be built in parallel without editing a shared file.
 *
 *   src/heart-rate/index.ts  mountHeartRate  slots: panel, trace           (T17)
 *   src/cuff/index.ts        mountCuff       slots: panel, trace           (T18)
 *   src/share/index.ts       mountShare      slots: action, trace, readback (T19)
 *   src/messages/index.ts    mountMessages   slots: log, summary           (T19)
 */
import type { PulseBridgeApi } from '../api/contract';
import type { DataChangeKind } from '../state/data-changed';
import type { SelectionStore } from '../state/selection';

export interface FeatureContext<Slot extends string> {
  /** The one API client (mock or HTTP). A 401 is handled for you: the page returns to login. */
  client: PulseBridgeApi;
  /** The page-wide selection (src/state/selection.ts). */
  selection: SelectionStore;
  /** The elements this feature owns. Each starts empty. */
  slots: Record<Slot, HTMLElement>;
  /**
   * Aborted when the dashboard is torn down (logout, or the session ending). Pass it to
   * addEventListener, subscribe() and fetch-like calls so nothing outlives the dashboard.
   */
  signal: AbortSignal;
  /** Asks the header to reload the status strip. Rarely needed: it already listens to changes. */
  refreshStatus(): void;
  /**
   * Tell the other features that server data changed, after a call of yours that changed it
   * (T18 after a cuff send: 'cuff-send'; T19 after a share: 'share'). See state/data-changed.ts.
   */
  notifyDataChanged(kind: DataChangeKind): void;
  /**
   * Run `listener` after any feature reports a change; re-fetch what you show there. Not called
   * at once. Unsubscribed when `signal` aborts (pass the context's signal).
   */
  onDataChanged(listener: (kind: DataChangeKind) => void, signal: AbortSignal): void;
}

export type MountFeature<Slot extends string> = (context: FeatureContext<Slot>) => void;

export type HeartRateSlot = 'panel' | 'trace';
export type CuffSlot = 'panel' | 'trace';
export type ShareSlot = 'action' | 'trace' | 'readback';
/** `summary` is a text-only span inside the strip's toggle button (for example "2 errors"). */
export type MessagesSlot = 'log' | 'summary';

export interface Features {
  heartRate: MountFeature<HeartRateSlot>;
  cuff: MountFeature<CuffSlot>;
  share: MountFeature<ShareSlot>;
  messages: MountFeature<MessagesSlot>;
}
