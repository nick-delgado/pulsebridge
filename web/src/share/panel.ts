/**
 * Share with clinic (T19): the button, its hint and the confirm step in `slots.action`, the step
 * trace in `slots.trace`, and the "As stored in OpenEMR" read-back in `slots.readback` (D13).
 *
 * Flow:
 *   selection ─▶ button enabled exactly when `shareableObservation(selection)` is not null; the
 *     hint (the button's description) says why not, or what is selected
 *   "Share with clinic" ─▶ getObservation(id): the reading is checked in the FHIR store first
 *     ─▶ not `final`: refused here, nothing sent
 *     ─▶ the confirm step: what will be sent, what is left out, the expected Observation
 *   "Send to the clinic" ─▶ share({observationId}) (one request; buttons disabled meanwhile)
 *     ─▶ the trace (each step's status) and the read-back (sent beside stored, differences in
 *        words); notifyDataChanged('share'); the hint is worked out again from the selection
 *     ✗ ShareError: what did and did not happen, with the steps completed so far
 *   the selection changes while checking or confirming ─▶ the confirm closes, nothing is sent
 *   the selection changes while sending ─▶ the share is NOT cancelled (it is a write that is
 *     already under way); its result is shown, labelled with the reading it was for
 *   logout ─▶ every request is aborted; a late answer changes nothing
 *
 * Every string from the server or OpenEMR is rendered as text (`el`, `codeBlock`).
 */
import type { Observation } from '../api/fhir';
import type { ShareQuery, ShareResult, ShareStep } from '../api/contract';
import { ApiError, isServiceNotConfigured, ShareError, UnauthorizedError } from '../api/errors';
import type { FeatureContext, ShareSlot } from '../shell/features';
import { shareableObservation, type Selection } from '../state/selection';
import { append, codeBlock, el, placeholder, replaceChildren, uniqueId } from '../ui/dom';
import { differenceSentences } from './diff';
import {
  CONFIRM_LEFT_OUT,
  CONFIRM_SENT,
  expectedShared,
  failureSentences,
  NOT_SHOWN_NOTE,
  REPEAT_NOTE,
  resultHeadline,
  stepResult,
  stepTitle,
} from './words';

export interface ShareDeps {
  now: () => number;
  /** Zone for clock times; the browser's own when undefined. */
  timeZone?: string;
}

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

function childController(parent: AbortSignal): AbortController {
  const c = new AbortController();
  if (parent.aborted) c.abort();
  else
    parent.addEventListener(
      'abort',
      () => {
        c.abort();
      },
      { once: true, signal: c.signal },
    );
  return c;
}

/** A search step's parameters as text: `identifier=SYN-1 & date=2026-03-10`. */
export function queryText(query: ShareQuery): string {
  return Object.entries(query)
    .flatMap(([name, value]) => (Array.isArray(value) ? value : [value]).map((v) => `${name}=${v}`))
    .join(' & ');
}

function clock(ms: number, timeZone?: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
    ...(timeZone ? { timeZone } : {}),
  }).format(ms);
}

type State =
  | { kind: 'idle' }
  | { kind: 'checking'; id: string; summary: string; controller: AbortController }
  | { kind: 'confirm'; id: string; summary: string; observation: Observation }
  | { kind: 'sending'; id: string; summary: string };

export function createShare(context: FeatureContext<ShareSlot>, deps: ShareDeps): void {
  const { client, selection, slots, signal } = context;

  // -- the action area -------------------------------------------------------------------------
  const hint = el('p', {
    class: 'share__hint',
    id: 'share-hint',
    attrs: { 'aria-live': 'polite' },
  });
  const button = el('button', {
    class: 'button button--primary share__button',
    id: 'share-button',
    text: 'Share with clinic',
    attrs: { type: 'button', 'aria-describedby': 'share-hint', disabled: true },
    on: {
      click: () => {
        startCheck();
      },
    },
    signal,
  });
  // Labelled by its heading only while open: a closed confirm has no heading to point at.
  const confirmBox = el('div', {
    class: 'share-confirm',
    id: 'share-confirm',
    attrs: { role: 'group' },
  });
  confirmBox.hidden = true;
  // Two fixed live regions: progress and results are polite, failures assertive. Each is written
  // only when its text changes.
  const status = el('p', { class: 'share-status', id: 'share-status', attrs: { role: 'status' } });
  const alert = el('div', { class: 'share-alert', id: 'share-alert', attrs: { role: 'alert' } });
  append(slots.action, button, hint, confirmBox, status, alert);
  replaceChildren(
    slots.trace,
    placeholder(
      'After a share: the token request, each request sent to OpenEMR and its response, in order.',
    ),
  );
  replaceChildren(
    slots.readback,
    placeholder(
      'After a share: the Patient, the Encounter and the reading as OpenEMR stored them, read back by id, beside what was sent.',
    ),
  );

  let state: State = { kind: 'idle' };
  /** This session's shares, by Observation id, for the repeat note. */
  const shares = new Map<string, { at: number; outcome: ShareResult['outcome'] }>();

  const setText = (node: HTMLElement, text: string) => {
    if (node.textContent !== text) node.textContent = text;
  };
  const setAlert = (lines: string[]) => {
    if (lines.length === 0) {
      replaceChildren(alert);
      return;
    }
    replaceChildren(
      alert,
      ...lines.map((line) => el('p', { class: 'share-note share-note--error', text: line })),
    );
  };

  const target = (s: Selection | null): string | null =>
    shareableObservation(s)?.replace(/^Observation\//, '') ?? null;

  /** The button and its hint from the current selection and state: the "re-check". */
  const renderAction = () => {
    const current = selection.get();
    const id = target(current);
    const busy = state.kind === 'checking' || state.kind === 'sending';
    button.disabled = id === null || busy;
    button.textContent = state.kind === 'sending' ? 'Sharing…' : 'Share with clinic';
    if (busy) slots.action.setAttribute('aria-busy', 'true');
    else slots.action.removeAttribute('aria-busy');
    let text: string;
    if (current === null) text = 'Select a heart-rate point or a cuff reading first.';
    // The feature's own reason first: it is more precise (for example the mapper's error).
    else if (current.unshareableReason !== null)
      text = `${current.summary}: ${current.unshareableReason}`;
    else if (current.observationId === null)
      text = `${current.summary}: no Observation was stored for this reading, so there is nothing to share.`;
    else {
      text = `Selected: ${current.summary}`;
      const before = id === null ? undefined : shares.get(id);
      if (before) {
        text += `. ${before.outcome === 'shared' ? 'Shared' : 'Found already shared'} at ${clock(before.at, deps.timeZone)} in this session.`;
      }
    }
    setText(hint, text);
  };

  const closeConfirm = () => {
    confirmBox.hidden = true;
    confirmBox.removeAttribute('aria-labelledby');
    replaceChildren(confirmBox);
  };

  // -- checking and confirming -----------------------------------------------------------------
  function startCheck(): void {
    const current = selection.get();
    const id = target(current);
    if (id === null || current === null || state.kind === 'checking' || state.kind === 'sending')
      return;
    const controller = childController(signal);
    state = { kind: 'checking', id, summary: current.summary, controller };
    closeConfirm();
    setAlert([]);
    setText(status, `Checking ${current.summary} in the record before sharing…`);
    renderAction();
    client.getObservation(id, { signal: controller.signal }).then(
      (observation) => {
        if (controller.signal.aborted || state.kind !== 'checking' || state.id !== id) return;
        if (observation.status !== 'final') {
          state = { kind: 'idle' };
          setText(status, '');
          setAlert([
            `Not shared: this reading's status is ${observation.status}, and only final readings are shared (a reading that can still change would not be updated at the clinic).`,
            'Nothing was sent to the clinic.',
          ]);
          renderAction();
          return;
        }
        showConfirm(id, state.summary, observation);
      },
      (error: unknown) => {
        if (isAbort(error, controller.signal) || error instanceof UnauthorizedError) return;
        if (state.kind !== 'checking' || state.id !== id) return;
        state = { kind: 'idle' };
        setText(status, '');
        setAlert([
          isServiceNotConfigured(error)
            ? 'Not shared: the service is not configured (the server is missing its session-signing key). Your demo session is kept.'
            : error instanceof ApiError && error.status === 404
              ? 'Not shared: the reading was not found in the record.'
              : `Not shared: the reading could not be checked (${error instanceof Error ? error.message : 'unknown error'}).`,
          'Nothing was sent to the clinic.',
        ]);
        renderAction();
      },
    );
  }

  function showConfirm(id: string, summary: string, observation: Observation): void {
    state = { kind: 'confirm', id, summary, observation };
    setText(status, '');
    const before = shares.get(id);
    const previewId = uniqueId('share-preview');
    const send = el('button', {
      class: 'button button--primary',
      id: 'share-send',
      text: 'Send to the clinic',
      attrs: { type: 'button' },
      on: {
        click: () => {
          sendShare();
        },
      },
      signal,
    });
    const cancel = el('button', {
      class: 'button',
      id: 'share-cancel',
      text: 'Cancel',
      attrs: { type: 'button' },
      on: {
        click: () => {
          state = { kind: 'idle' };
          closeConfirm();
          setText(status, 'Cancelled: nothing was sent.');
          renderAction();
          button.focus();
        },
      },
      signal,
    });
    const heading = el('h3', {
      class: 'share-confirm__heading',
      id: 'share-confirm-heading',
      text: 'Send this reading to the clinic?',
      attrs: { tabindex: '-1' },
    });
    replaceChildren(
      confirmBox,
      heading,
      el('p', { class: 'share-confirm__what', text: summary }),
      el('p', { class: 'share-confirm__label', text: 'Sent to OpenEMR, the clinic’s system:' }),
      el('ul', { class: 'share-confirm__list' }, ...CONFIRM_SENT.map((t) => el('li', { text: t }))),
      el('p', { class: 'share-confirm__left', text: CONFIRM_LEFT_OUT }),
      before
        ? el('p', {
            class: 'share-note',
            text: `You already shared this reading at ${clock(before.at, deps.timeZone)}. ${REPEAT_NOTE}`,
          })
        : null,
      el(
        'details',
        { class: 'share-confirm__preview' },
        el('summary', {
          text: 'The reading as it is expected to be sent',
          attrs: { id: previewId },
        }),
        codeBlock(expectedShared(observation), 'The Observation as it is expected to be sent'),
      ),
      el('div', { class: 'share-confirm__actions' }, send, cancel),
    );
    confirmBox.setAttribute('aria-labelledby', 'share-confirm-heading');
    confirmBox.hidden = false;
    renderAction();
    heading.focus();
  }

  // -- sending ---------------------------------------------------------------------------------
  function sendShare(): void {
    if (state.kind !== 'confirm') return; // one share at a time: a second click does nothing
    const { id, summary } = state;
    state = { kind: 'sending', id, summary };
    for (const b of confirmBox.querySelectorAll('button')) b.disabled = true;
    confirmBox.setAttribute('aria-busy', 'true');
    setText(status, `Sharing ${summary} with the clinic… this can take several seconds.`);
    renderAction();
    // Not cancelled by a selection change (the write is under way); only by logout.
    const controller = childController(signal);
    client.share({ observationId: id }, { signal: controller.signal }).then(
      (result) => {
        if (controller.signal.aborted) return;
        shares.set(id, { at: deps.now(), outcome: result.outcome });
        finish();
        const moved = target(selection.get()) !== id;
        setText(
          status,
          `${resultHeadline(result)}${moved ? ` (This result is for ${summary}; the selection changed while it was sent.)` : ''}`,
        );
        renderTrace(summary, result.steps, null);
        renderReadBack(result);
        context.notifyDataChanged('share');
        renderAction();
        focusResult();
      },
      (error: unknown) => {
        if (isAbort(error, controller.signal) || error instanceof UnauthorizedError) return;
        const failure =
          error instanceof ShareError
            ? error
            : ShareError.from(error instanceof ApiError ? error : new ApiError(0, 'unknown error'));
        finish();
        setText(status, '');
        const lines = isServiceNotConfigured(failure)
          ? [
              'Not shared: the service is not configured (the server is missing its session-signing key). Your demo session is kept.',
              'Nothing was sent to the clinic.',
            ]
          : failureSentences(failure);
        setAlert(lines);
        renderTrace(summary, failure.steps, lines[0] ?? 'The share failed.', failure.nothingSent);
        replaceChildren(
          slots.readback,
          placeholder('Nothing to read back: the share did not finish.'),
        );
        // Unless the proxy refused before calling OpenEMR, something may have happened at the
        // clinic (a Patient, an Encounter or the reading created), so the others re-check.
        if (!failure.nothingSent) context.notifyDataChanged('share');
        renderAction();
        focusResult();
      },
    );
  }

  function finish(): void {
    state = { kind: 'idle' };
    confirmBox.removeAttribute('aria-busy');
    closeConfirm();
  }

  /** After a share, focus moves to its trace heading (the confirm it came from is gone). */
  function focusResult(): void {
    const heading = slots.trace.querySelector<HTMLElement>('#share-trace-summary');
    heading?.focus();
  }

  // -- the trace -------------------------------------------------------------------------------
  function renderTrace(
    summary: string,
    steps: ShareStep[],
    stoppedWith: string | null,
    nothingSent = false,
  ): void {
    const list = el('ol', {
      class: 'share-steps',
      attrs: { 'aria-label': 'Steps of the share, in order' },
    });
    steps.forEach((step, i) => {
      const result = stepResult(step);
      const id = uniqueId('share-step');
      list.append(
        el(
          'li',
          {
            class: `share-step share-tone share-tone--${result.tone}`,
            attrs: { 'aria-labelledby': `${id}-title` },
          },
          el(
            'p',
            { class: 'share-step__head', id: `${id}-title` },
            el('span', { class: 'share-step__number', text: `${String(i + 1)}.` }),
            el('span', { class: 'share-step__title', text: stepTitle(step) }),
            el('span', { class: 'share-step__result', text: result.text }),
          ),
          el(
            'p',
            { class: 'share-step__request share-mono' },
            // A reused token was never requested: no "→ no answer" line for it.
            step.result === 'reused'
              ? `${step.request.method} ${step.request.url}: not sent (token reused)`
              : `${step.request.method} ${step.request.url} → ${step.response.status === 0 ? 'no answer' : String(step.response.status)}`,
            el('span', { class: 'share-step__time', text: ` · ${String(step.durationMs)} ms` }),
          ),
          step.request.query
            ? el('p', {
                class: 'share-step__query share-mono',
                text: `Search parameters: ${queryText(step.request.query)}`,
              })
            : null,
          el('p', { class: 'share-hint', text: step.label }),
          el(
            'details',
            { class: 'share-step__bodies' },
            el('summary', { text: 'Request and response bodies' }),
            el('p', { class: 'share-hint', text: 'Request body' }),
            codeBlock(step.request.body ?? 'none', `Step ${String(i + 1)} request body`),
            el('p', { class: 'share-hint', text: 'Response body' }),
            codeBlock(step.response.body ?? 'none', `Step ${String(i + 1)} response body`),
          ),
        ),
      );
    });
    replaceChildren(
      slots.trace,
      el('p', {
        class: 'share-trace__summary',
        id: 'share-trace-summary',
        text: `Share of ${summary}`,
        attrs: { tabindex: '-1' },
      }),
      steps.length > 0
        ? list
        : placeholder(
            nothingSent
              ? 'No request reached OpenEMR.'
              : 'The server sent no trace, so it is not known which requests reached OpenEMR.',
          ),
      stoppedWith ? el('p', { class: 'share-note share-note--error', text: stoppedWith }) : null,
      steps.length > 0 ? el('p', { class: 'share-hint', text: NOT_SHOWN_NOTE }) : null,
    );
  }

  // -- the read-back ---------------------------------------------------------------------------
  function pair(
    title: string,
    sent: object | null,
    stored: object | null,
    notSent: string,
    note?: string,
  ): HTMLElement {
    const section = el('section', {
      class: 'share-readback__item',
      attrs: { 'aria-label': title },
    });
    section.append(el('h4', { class: 'share-readback__heading', text: title }));
    if (note) section.append(el('p', { class: 'share-note', text: note }));
    if (stored === null) {
      section.append(
        el('p', {
          class: 'share-note share-note--warn',
          text: 'Reading it back from OpenEMR failed, so what it stored cannot be shown.',
        }),
      );
    }
    if (sent === null) section.append(el('p', { class: 'share-hint', text: notSent }));
    if (sent !== null && stored !== null) {
      const sentences = differenceSentences(sent, stored);
      append(
        section,
        el('p', {
          class: 'share-hint',
          text:
            sentences.length === 0
              ? 'OpenEMR stored exactly what was sent.'
              : 'How OpenEMR’s copy differs from what was sent:',
        }),
        sentences.length > 0
          ? el('ul', { class: 'share-diff' }, ...sentences.map((t) => el('li', { text: t })))
          : null,
      );
    }
    append(
      section,
      el(
        'div',
        { class: 'share-readback__pair' },
        sent === null
          ? null
          : el(
              'div',
              { class: 'share-readback__side' },
              el('p', { class: 'share-hint', text: 'Sent' }),
              codeBlock(sent, `${title} as sent`),
            ),
        stored === null
          ? null
          : el(
              'div',
              { class: 'share-readback__side' },
              el('p', { class: 'share-hint', text: 'As stored in OpenEMR' }),
              codeBlock(stored, `${title} as stored in OpenEMR`),
            ),
      ),
    );
    return section;
  }

  function renderReadBack(result: ShareResult): void {
    const { patient, encounter, observation } = result.readBack;
    replaceChildren(
      slots.readback,
      el('p', {
        class: 'share-hint',
        text: 'Read back from OpenEMR’s FHIR API by id after the share. OpenEMR’s own screens are not reachable from the internet, so this is how the page shows what the clinic holds.',
      }),
      pair(
        'Observation (the reading)',
        observation.sent,
        observation.stored,
        'Not sent.',
        result.outcome === 'already-shared'
          ? 'OpenEMR refused this copy as a repeat; it is compared with the reading OpenEMR already held.'
          : undefined,
      ),
      pair(
        'Patient',
        patient.sent,
        patient.stored,
        'Not sent: the Patient already existed in OpenEMR (found by its identifier).',
      ),
      pair(
        'Encounter',
        encounter.sent,
        encounter.stored,
        'Not sent: an Encounter for the reading’s day already existed and was reused.',
      ),
    );
  }

  // -- selection -------------------------------------------------------------------------------
  selection.subscribe((current) => {
    // Checking or confirming another reading than the one selected now: stop, send nothing.
    if ((state.kind === 'checking' || state.kind === 'confirm') && target(current) !== state.id) {
      if (state.kind === 'checking') state.controller.abort();
      state = { kind: 'idle' };
      closeConfirm();
      setText(status, 'The selection changed, so nothing was sent.');
    }
    renderAction();
  }, signal);
}
