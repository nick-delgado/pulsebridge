/**
 * Messages log (T19): every cuff send attempt (D12) with its commit-level acknowledgment and
 * application-level outcome (D7), stored messages no send record names, unreadable send
 * records, and FHIR transactions, in `slots.log`; a one-line count in `slots.summary` (text
 * inside the strip's toggle button). See log.ts for the flow and README.md here for the design.
 */
import type { FeatureContext, MessagesSlot } from '../shell/features';
import { createMessagesLog } from './log';
import './messages.css';

export function mountMessages(context: FeatureContext<MessagesSlot>): void {
  createMessagesLog(context);
}
