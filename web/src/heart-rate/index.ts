/**
 * Heart-rate area (T17): the 24 h / 7 d chart of five-minute roll-up windows in
 * `slots.panel`, min/mean/max and the selected window under it, and the trace panel in
 * `slots.trace`. See README.md in this directory for the design, and panel.ts for the flow.
 */
import type { FeatureContext, HeartRateSlot } from '../shell/features';
import { createUplotChart } from './chart';
import './heart-rate.css';
import { createHeartRate } from './panel';

export function mountHeartRate(context: FeatureContext<HeartRateSlot>): void {
  createHeartRate(context, { chart: createUplotChart, now: () => Date.now() });
}
