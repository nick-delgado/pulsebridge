/**
 * Cuff area (T18): the blood-pressure cuff simulator form and this session's readings list in
 * `slots.panel`, and the left trace panel in `slots.trace` (tabs "HL7v2 as sent", "ACK as
 * received" with the application-level outcome, "FHIR as stored"). See panel.ts for the flow
 * and README.md in this directory for the design.
 */
import type { CuffSlot, FeatureContext } from '../shell/features';
import './cuff.css';
import { createCuff } from './panel';

export function mountCuff(context: FeatureContext<CuffSlot>): void {
  createCuff(context, { now: () => Date.now() });
}
