/**
 * Messages log: OWNED BY T19. T16 ships this placeholder so the shell has something to mount.
 *
 * T19 replaces the body of `mountMessages` (keep its name and signature): rows from
 * `client.listMessages()` in `slots.log` (cuff send attempts with the commit-level
 * acknowledgment and application-level outcome, and FHIR transactions), and optional short text
 * in `slots.summary`, which sits inside the strip's toggle button (text only, no controls).
 */
import type { FeatureContext, MessagesSlot } from '../shell/features';
import { append, placeholder } from '../ui/dom';

export function mountMessages({ slots }: FeatureContext<MessagesSlot>): void {
  append(
    slots.log,
    placeholder(
      'Every cuff send attempt (including rejected and unacknowledged ones) and every FHIR transaction will be listed here.',
    ),
  );
}
