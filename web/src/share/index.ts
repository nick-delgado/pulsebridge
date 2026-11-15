/**
 * Share with clinic: OWNED BY T19. T16 ships the button and its enabled/disabled rule, because
 * the shell's acceptance depends on them; T19 keeps `mountShare`'s name and signature and adds the
 * share call, its step trace in `slots.trace`, and the "As stored in OpenEMR" panel in
 * `slots.readback`.
 *
 * Rule: the button is enabled only when the selection has an Observation to send
 * (`shareableObservation`); a cuff reading that was never stored or mapped has none.
 */
import type { FeatureContext, ShareSlot } from '../shell/features';
import { shareableObservation } from '../state/selection';
import { append, el, placeholder, replaceChildren } from '../ui/dom';

export function mountShare({ slots, selection, signal }: FeatureContext<ShareSlot>): void {
  const hint = el('p', {
    class: 'share__hint',
    id: 'share-hint',
    attrs: { 'aria-live': 'polite' },
  });
  const button = el('button', {
    class: 'button button--primary share__button',
    text: 'Share with clinic',
    attrs: { type: 'button', 'aria-describedby': 'share-hint', disabled: true },
    on: {
      click: () => {
        replaceChildren(
          slots.trace,
          placeholder('Sharing is not built yet; it arrives with the share action.'),
        );
      },
    },
    signal,
  });
  append(slots.action, button, hint);
  append(
    slots.trace,
    placeholder(
      'The token request, each request sent to OpenEMR and its response will appear here.',
    ),
  );
  append(
    slots.readback,
    placeholder('After a share, the Patient, Encounter and Observations as OpenEMR stored them.'),
  );

  selection.subscribe((current) => {
    const target = shareableObservation(current);
    button.disabled = target === null;
    hint.textContent =
      current === null
        ? 'Select a heart-rate point or a cuff reading first.'
        : target === null
          ? `${current.summary}: no Observation was stored for this reading, so there is nothing to share.`
          : `Selected: ${current.summary}`;
  }, signal);
}
