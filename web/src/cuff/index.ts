/**
 * Cuff area: OWNED BY T18. T16 ships this placeholder so the shell has something to mount.
 *
 * T18 replaces the body of `mountCuff` (keep its name and signature): the simulator form
 * (bounded systolic/diastolic, transport toggle, "send malformed"), the readings list in
 * `slots.panel`, and the trace panel in `slots.trace` (tabs "HL7v2 as sent", "ACK as received"
 * with the application-level outcome, "FHIR as stored"). Selecting a reading calls
 * `selection.set({ source: 'cuff', attemptId, observationId, summary })`.
 */
import type { CuffSlot, FeatureContext } from '../shell/features';
import { append, placeholder } from '../ui/dom';

export function mountCuff({ slots }: FeatureContext<CuffSlot>): void {
  append(slots.panel, placeholder('The cuff simulator and its readings list will appear here.'));
  append(
    slots.trace,
    placeholder(
      'Select a reading to see its trace: the HL7v2 message as sent, the ACK as received, and the FHIR Observation as stored.',
    ),
  );
}
