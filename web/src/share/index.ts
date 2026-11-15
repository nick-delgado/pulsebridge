/**
 * Share with clinic (T19): the button, its hint and the confirm step in `slots.action`, the step
 * trace in `slots.trace`, and the "As stored in OpenEMR" read-back in `slots.readback` (D13).
 * See panel.ts for the flow and README.md here for the design and the contract given to T15.
 *
 * Rule (kept from T16): the button is enabled only when the selection has an Observation to send
 * (`shareableObservation`): a cuff reading that was never stored or mapped has none, and a
 * selection with an `unshareableReason` (for example a `preliminary` window) is not sent.
 */
import type { FeatureContext, ShareSlot } from '../shell/features';
import { createShare } from './panel';
import './share.css';

export function mountShare(context: FeatureContext<ShareSlot>): void {
  createShare(context, { now: () => Date.now() });
}
