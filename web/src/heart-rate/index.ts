/**
 * Heart-rate area: OWNED BY T17. T16 ships this placeholder so the shell has something to mount.
 *
 * T17 replaces the body of `mountHeartRate` (keep its name and signature): the 24 h / 7 d / 30 d
 * chart of five-minute averages in `slots.panel`, min/mean/max under it, and the trace panel in
 * `slots.trace` (tabs "FHIR as stored" and "Google Health JSON", via src/ui/tabs.ts).
 * Selecting a point calls `selection.set({ source: 'watch', observationId, summary })`.
 */
import type { FeatureContext, HeartRateSlot } from '../shell/features';
import { append, placeholder } from '../ui/dom';

export function mountHeartRate({ slots }: FeatureContext<HeartRateSlot>): void {
  append(slots.panel, placeholder('The heart-rate chart (five-minute averages) will appear here.'));
  append(
    slots.trace,
    placeholder(
      'Select a point on the chart to see its trace: the FHIR Observation as stored and the Google Health JSON it came from.',
    ),
  );
}
