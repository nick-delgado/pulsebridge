/**
 * The cuff area (T18): the simulator form and this session's readings list in `slots.panel`, and
 * the left trace panel in `slots.trace`. `mountCuff` (index.ts) calls `createCuff` with the real
 * clock; the unit tests pass a fake client, fake timers and a fixed time zone.
 *
 * Flow:
 *   submit ─ checkForm (the simulator's own limits) ─▶ submitCuffReading
 *     ─▶ a new row at the top of the list, notifyDataChanged('cuff-send'), and, if nothing else
 *        was selected meanwhile, the new row is selected
 *     ─▶ a stored (AA) and recorded send is polled (pollUntil, web/README.md) until the
 *        application-level outcome leaves "pending", the bound is reached, the row is dropped,
 *        or the dashboard is torn down (logout)
 *     ✗ CuffSendError: field errors next to their fields, a countdown for 429, and a message
 *       that says whether anything may have been sent
 *   "Show trace" on a row ─▶ selection.set(...) ─▶ getCuffAttemptTrace ─▶ trace panel
 *     the trace's Observation decides whether the selection may be shared (only `final`)
 *   onDataChanged('sync' | 'share') ─▶ the selected reading is marked "being checked" (so Share
 *     cannot act on stale data) and re-checked through its trace
 *
 * Every request has its own AbortController, chained to the dashboard's signal, and every answer
 * checks that controller before it renders, so a late answer (after another selection, a newer
 * request or a logout) is dropped.
 */
import {
  CUFF_SERVICE_LIMITS,
  MAPPER_PLAUSIBLE_RANGE,
  type CuffAttemptTrace,
  type CuffReadingRequest,
  type MalformedSample,
} from '../api/contract';
import { ApiError, CuffSendError, UnauthorizedError } from '../api/errors';
import type { CuffSlot, FeatureContext } from '../shell/features';
import { pollUntil, type PollOptions } from '../state/poll';
import type { Selection } from '../state/selection';
import { append, el, placeholder, replaceChildren, uniqueId } from '../ui/dom';
import {
  checkForm,
  CLASS_INFO,
  classTitle,
  plausibilityNote,
  sampleAvailable,
  samplesByClass,
  transportName,
  type CuffField,
  type CuffFormState,
  type FieldErrors,
} from './form';
import {
  applicationPhrase,
  clockTime,
  commitAckPhrase,
  fieldMessages,
  isPending,
  newReading,
  readingSummary,
  sendFailureText,
  unshareableReason,
  whatWasSent,
  withTrace,
  type Reading,
} from './readings';
import { APPLICATION_LEVEL_LABEL, COMMIT_LEVEL_LABEL, tracePanel } from './trace';

/** The list keeps this session's newest readings; older rows are dropped (and stop polling). */
export const MAX_READINGS = 20;

export const RECHECK_REASON = 'being checked again after a data change.';

export interface CuffDeps {
  /** Clock for the rate-limit countdown. */
  now: () => number;
  /** Zone for the clock times in the list; the browser's own when undefined. */
  timeZone?: string;
  /** Overrides for the polling convention (tests only). */
  poll?: Partial<Pick<PollOptions, 'intervalMs' | 'maxMs' | 'now' | 'isVisible'>>;
  maxReadings?: number;
}

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof DOMException && error.name === 'AbortError');
}

/** An AbortController that also aborts when `parent` does (logout tears the dashboard down). */
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'unknown error';
}

interface RowView {
  root: HTMLLIElement;
  selected: HTMLSpanElement;
  ack: HTMLElement;
  application: HTMLElement;
  show: HTMLButtonElement | null;
  checkAgain: HTMLButtonElement;
}

export function createCuff(context: FeatureContext<CuffSlot>, deps: CuffDeps): void {
  const { client, selection, slots, signal } = context;
  const maxReadings = deps.maxReadings ?? MAX_READINGS;
  const { min, max } = CUFF_SERVICE_LIMITS;
  const { systolic: ps, diastolic: pd } = MAPPER_PLAUSIBLE_RANGE;

  // -- form ------------------------------------------------------------------------------------
  const fieldError = (id: string) =>
    el('p', { class: 'cuff-field__error', id, attrs: { 'aria-live': 'polite' } });
  const pressureInput = (field: 'systolic' | 'diastolic', label: string, value: string) => {
    const id = `cuff-${field}`;
    const input = el('input', {
      class: 'cuff-field__input',
      id,
      attrs: {
        type: 'text',
        inputmode: 'numeric',
        autocomplete: 'off',
        name: field,
        'aria-describedby': `${id}-unit ${id}-note ${id}-error`,
      },
    });
    input.value = value;
    const note = el('p', { class: 'cuff-field__note', id: `${id}-note` });
    const error = fieldError(`${id}-error`);
    const wrap = el(
      'div',
      { class: 'cuff-field' },
      el('label', { class: 'cuff-field__label', attrs: { for: id }, text: label }),
      el(
        'div',
        { class: 'cuff-field__row' },
        input,
        el('span', { class: 'cuff-field__unit', id: `${id}-unit`, text: 'mm[Hg]' }),
      ),
      note,
      error,
    );
    const updateNote = () => {
      note.textContent = plausibilityNote(field, input.value) ?? '';
    };
    input.addEventListener('input', updateNote, { signal });
    updateNote();
    return { input, error, wrap };
  };
  const systolic = pressureInput('systolic', 'Systolic', '120');
  const diastolic = pressureInput('diastolic', 'Diastolic', '80');
  const limits = el('p', {
    class: 'cuff-hint',
    id: 'cuff-limits',
    text:
      `The simulator accepts whole numbers from ${min} to ${max} mm[Hg], systolic above diastolic. ` +
      `The mapper is stricter: it only maps systolic ${ps.min}–${ps.max} and diastolic ${pd.min}–${pd.max}. ` +
      `A value between the two is sent and stored (AA), then rejected by the mapper as "out-of-range": an application-level error you can try, for example 320/80.`,
  });
  const malformedNotice = el('p', {
    class: 'cuff-hint',
    text: 'Not used while "Send malformed" is on: a sample is sent as its own fixed bytes.',
  });
  malformedNotice.hidden = true;
  const pressures = el(
    'fieldset',
    { class: 'cuff-fieldset cuff-pressures', attrs: { 'aria-describedby': 'cuff-limits' } },
    el('legend', { class: 'cuff-legend', text: 'Reading' }),
    el('div', { class: 'cuff-pressures__inputs' }, systolic.wrap, diastolic.wrap),
    limits,
    malformedNotice,
  );

  const transportRadio = (value: 'mllp' | 'https', label: string, help: string) => {
    const id = `cuff-transport-${value}`;
    const input = el('input', {
      id,
      attrs: {
        type: 'radio',
        name: 'cuff-transport',
        value,
        'aria-describedby': `${id}-help`,
      },
    });
    return {
      input,
      wrap: el(
        'div',
        { class: 'cuff-choice' },
        input,
        el(
          'div',
          { class: 'cuff-choice__text' },
          el('label', { class: 'cuff-choice__label', attrs: { for: id }, text: label }),
          el('span', { class: 'cuff-choice__help', id: `${id}-help`, text: help }),
        ),
      ),
    };
  };
  const mllp = transportRadio(
    'mllp',
    'MLLP through the adapter',
    'The default: a TCP frame to the MLLP adapter on the VM, which ingests it and relays the store’s ACK.',
  );
  const https = transportRadio(
    'https',
    'HTTPS to the store',
    'Straight to the HL7v2 store’s messages:ingest endpoint; the response carries the ACK.',
  );
  mllp.input.checked = true;
  const transportError = fieldError('cuff-transport-error');
  const transport = el(
    'fieldset',
    { class: 'cuff-fieldset', attrs: { 'aria-describedby': 'cuff-transport-error' } },
    el('legend', { class: 'cuff-legend', text: 'Transport' }),
    mllp.wrap,
    https.wrap,
    transportError,
  );

  const malformed = el('input', {
    id: 'cuff-malformed',
    attrs: {
      type: 'checkbox',
      'aria-controls': 'cuff-samples',
      'aria-describedby': 'cuff-malformed-help',
    },
  });
  const sampleError = fieldError('cuff-sample-error');
  const sampleArea = el('div', { class: 'cuff-samples__body' });
  const samplesBox = el(
    'fieldset',
    {
      class: 'cuff-fieldset cuff-samples',
      id: 'cuff-samples',
      attrs: { 'aria-describedby': 'cuff-sample-error' },
    },
    el('legend', { class: 'cuff-legend', text: 'Malformed sample' }),
    sampleArea,
    sampleError,
  );
  samplesBox.hidden = true;
  const malformedBox = el(
    'div',
    { class: 'cuff-malformed' },
    el(
      'div',
      { class: 'cuff-choice' },
      malformed,
      el(
        'div',
        { class: 'cuff-choice__text' },
        el('label', {
          class: 'cuff-choice__label',
          attrs: { for: 'cuff-malformed' },
          text: 'Send malformed',
        }),
        el('span', {
          class: 'cuff-choice__help',
          id: 'cuff-malformed-help',
          text: 'Skip validation and send one of the synthetic broken messages instead, to see where each kind is caught.',
        }),
      ),
    ),
    samplesBox,
  );

  const sendButton = el('button', {
    class: 'button button--primary cuff-send',
    id: 'cuff-send',
    text: 'Take reading',
    attrs: { type: 'submit', 'aria-describedby': 'cuff-form-message cuff-countdown' },
  });
  const formMessage = el('p', {
    class: 'cuff-form-message',
    id: 'cuff-form-message',
    attrs: { role: 'status' },
  });
  const countdown = el('p', { class: 'cuff-countdown', id: 'cuff-countdown' });
  const form = el(
    'form',
    {
      class: 'cuff-form',
      id: 'cuff-form',
      attrs: { novalidate: true, 'aria-label': 'Cuff simulator' },
    },
    pressures,
    transport,
    malformedBox,
    el('div', { class: 'cuff-actions' }, sendButton, formMessage, countdown),
  );

  // -- readings list ---------------------------------------------------------------------------
  const listEmpty = placeholder(
    'No readings sent in this session yet. Take a reading to see the acknowledgment and the outcome here.',
  );
  const list = el('ol', {
    class: 'cuff-readings__list',
    attrs: { 'aria-labelledby': 'cuff-readings-heading' },
  });
  const readingsSection = el(
    'section',
    { class: 'cuff-readings', attrs: { 'aria-labelledby': 'cuff-readings-heading' } },
    el('h3', {
      class: 'cuff-readings__title',
      id: 'cuff-readings-heading',
      text: 'Readings this session',
    }),
    listEmpty,
    list,
  );
  append(slots.panel, form, readingsSection);
  const trace = tracePanel(slots.trace, signal);
  trace.render({ kind: 'empty' });

  // -- state -----------------------------------------------------------------------------------
  let samples: MalformedSample[] = [];
  let samplesController: AbortController | null = null;
  let sending = false;
  let sendController: AbortController | null = null;
  /** The rate limit's end, from Retry-After; 0 when not waiting. */
  let waitUntil = 0;
  let countdownTimer: ReturnType<typeof setTimeout> | undefined;
  /** Newest first. */
  const readings: Reading[] = [];
  const views = new Map<string, RowView>();
  const pollers = new Map<string, AbortController>();
  /** The latest trace applied per row, for re-rendering when an older answer is dropped. */
  const traces = new Map<string, CuffAttemptTrace>();
  /** The row behind the page selection while the selection is ours; null otherwise. */
  let selectedKey: string | null = null;
  let traceController: AbortController | null = null;

  const find = (key: string) => readings.find((r) => r.key === key);
  const replace = (next: Reading) => {
    const i = readings.findIndex((r) => r.key === next.key);
    if (i >= 0) readings[i] = next;
    renderRow(next);
  };

  // -- form behaviour --------------------------------------------------------------------------
  const currentTransport = () => (https.input.checked ? 'https' : 'mllp');
  const chosenSample = () =>
    sampleArea.querySelector<HTMLInputElement>('input[name="cuff-sample"]:checked')?.value ?? '';
  const formState = (): CuffFormState => ({
    systolic: systolic.input.value,
    diastolic: diastolic.input.value,
    transport: currentTransport(),
    sendMalformed: malformed.checked,
    sample: chosenSample(),
  });

  const errorTargets: Record<CuffField, { message: HTMLElement; control: HTMLElement | null }> = {
    systolic: { message: systolic.error, control: systolic.input },
    diastolic: { message: diastolic.error, control: diastolic.input },
    transport: { message: transportError, control: mllp.input },
    sendMalformed: { message: sampleError, control: null },
    body: { message: formMessage, control: null },
  };

  const clearFieldErrors = () => {
    for (const field of ['systolic', 'diastolic', 'transport', 'sendMalformed'] as const) {
      errorTargets[field].message.textContent = '';
    }
    systolic.input.removeAttribute('aria-invalid');
    diastolic.input.removeAttribute('aria-invalid');
  };

  /** Shows each field's error next to it; returns the first control to focus, if any. */
  const showFieldErrors = (errors: FieldErrors): HTMLElement | null => {
    let first: HTMLElement | null = null;
    for (const field of ['systolic', 'diastolic', 'transport', 'sendMalformed'] as const) {
      const text = errors[field];
      if (text === undefined) continue;
      errorTargets[field].message.textContent = text;
      if (field === 'systolic' || field === 'diastolic') {
        const input = field === 'systolic' ? systolic.input : diastolic.input;
        input.setAttribute('aria-invalid', 'true');
      }
      first ??=
        errorTargets[field].control ??
        sampleArea.querySelector<HTMLInputElement>('input[name="cuff-sample"]:not(:disabled)');
    }
    return first;
  };

  const updateButton = () => {
    const waiting = waitUntil > 0;
    sendButton.disabled = sending || waiting;
    sendButton.textContent = sending
      ? 'Sending…'
      : malformed.checked
        ? 'Send malformed sample'
        : 'Take reading';
    if (sending) form.setAttribute('aria-busy', 'true');
    else form.removeAttribute('aria-busy');
  };

  const setMessage = (text: string, tone: 'info' | 'ok' | 'warn' | 'error') => {
    formMessage.textContent = text;
    formMessage.className = `cuff-form-message cuff-form-message--${tone}`;
    // An error interrupts; anything else is announced politely.
    formMessage.setAttribute('role', tone === 'error' || tone === 'warn' ? 'alert' : 'status');
  };

  const stopCountdown = () => {
    clearTimeout(countdownTimer);
    waitUntil = 0;
    countdown.textContent = '';
    updateButton();
  };

  /** The 429 countdown from `Retry-After`: the button is disabled until it reaches zero. */
  const startCountdown = (seconds: number | null) => {
    clearTimeout(countdownTimer);
    if (seconds === null || seconds <= 0) {
      waitUntil = 0;
      countdown.textContent = 'Wait a little before sending again.';
      updateButton();
      return;
    }
    waitUntil = deps.now() + seconds * 1000;
    const tick = () => {
      if (signal.aborted) return;
      const left = Math.ceil((waitUntil - deps.now()) / 1000);
      if (left <= 0) {
        stopCountdown();
        countdown.textContent = 'You can send again now.';
        return;
      }
      countdown.textContent = `You can send again in ${left} s.`;
      countdownTimer = setTimeout(tick, 1000);
    };
    tick();
    updateButton();
  };
  signal.addEventListener('abort', () => {
    clearTimeout(countdownTimer);
  });

  const renderSamples = () => {
    const t = currentTransport();
    const chosen = chosenSample();
    replaceChildren(sampleArea);
    for (const group of samplesByClass(samples)) {
      const info = group.cls === 'other' ? null : CLASS_INFO[group.cls];
      const box = el(
        'fieldset',
        { class: 'cuff-sample-group' },
        el('legend', { class: 'cuff-sample-group__title', text: classTitle(group.cls) }),
        info ? el('p', { class: 'cuff-hint', text: info.layer }) : null,
      );
      for (const s of group.samples) {
        const id = `cuff-sample-${s.name}`;
        const available = sampleAvailable(s, t);
        const input = el('input', {
          id,
          attrs: {
            type: 'radio',
            name: 'cuff-sample',
            value: s.name,
            'aria-describedby': `${id}-help`,
            disabled: !available,
          },
        });
        input.checked = available && s.name === chosen;
        const only =
          s.transports.length === 1 ? ` ${transportName(s.transports[0] ?? 'mllp')} only.` : '';
        box.append(
          el(
            'div',
            { class: 'cuff-choice cuff-choice--sample' },
            input,
            el(
              'div',
              { class: 'cuff-choice__text' },
              el('label', {
                class: 'cuff-choice__label cuff-mono',
                attrs: { for: id },
                text: s.name,
              }),
              el('span', {
                class: 'cuff-choice__help',
                id: `${id}-help`,
                text: `${s.label}${only}${available ? '' : ` Not offered over ${transportName(t)}.`}`,
              }),
            ),
          ),
        );
      }
      sampleArea.append(box);
    }
    if (samples.length === 0) sampleArea.append(placeholder('The simulator offers no samples.'));
  };

  const loadSamples = () => {
    samplesController?.abort();
    const c = childController(signal);
    samplesController = c;
    replaceChildren(
      sampleArea,
      el('p', { class: 'placeholder', text: 'Loading the samples…', attrs: { role: 'status' } }),
    );
    client.listMalformedSamples({ signal: c.signal }).then(
      (list) => {
        if (c.signal.aborted) return;
        samples = list;
        renderSamples();
      },
      (error: unknown) => {
        if (isAbort(error, c.signal) || error instanceof UnauthorizedError) return;
        replaceChildren(
          sampleArea,
          el('p', {
            class: 'cuff-note cuff-note--error',
            text: `Could not load the samples: ${errorMessage(error)}`,
            attrs: { role: 'alert' },
          }),
          el('button', {
            class: 'button',
            text: 'Try again',
            attrs: { type: 'button' },
            on: { click: loadSamples },
            signal,
          }),
        );
      },
    );
  };

  const onMalformedToggle = () => {
    const on = malformed.checked;
    samplesBox.hidden = !on;
    pressures.disabled = on;
    malformedNotice.hidden = !on;
    clearFieldErrors();
    updateButton();
  };
  malformed.addEventListener('change', onMalformedToggle, { signal });

  const onTransportChange = () => {
    const before = chosenSample();
    renderSamples();
    // A sample not offered over the new transport is unchosen, and the user is told why.
    if (before !== '' && chosenSample() === '') {
      const only = (samples.find((s) => s.name === before)?.transports ?? []).map(transportName);
      sampleError.textContent = `${before} can only be sent over ${only.join(' or ')}; choose another sample or switch back.`;
    } else sampleError.textContent = '';
  };
  mllp.input.addEventListener('change', onTransportChange, { signal });
  https.input.addEventListener('change', onTransportChange, { signal });
  sampleArea.addEventListener(
    'change',
    () => {
      sampleError.textContent = '';
    },
    { signal },
  );

  // -- readings list ---------------------------------------------------------------------------
  const selectionFor = (r: Reading): Selection | null => {
    if (!r.recorded || r.result.attemptId === null) return null;
    return {
      source: 'cuff',
      attemptId: r.result.attemptId,
      observationId: r.observationId,
      summary: readingSummary(r, deps.timeZone),
      unshareableReason: unshareableReason(r),
    };
  };

  const phraseItem = (label: string, phrase: { label: string; detail: string; tone: string }) =>
    [
      el('dt', { class: 'cuff-reading__term', text: label }),
      el(
        'dd',
        { class: `cuff-reading__value cuff-tone cuff-tone--${phrase.tone}` },
        el('span', { class: 'cuff-reading__label', text: phrase.label }),
        el('span', { class: 'cuff-reading__detail', text: phrase.detail }),
      ),
    ] as const;

  const createRow = (r: Reading): RowView => {
    const headingId = `${r.key}-heading`;
    const selected = el('span', { class: 'cuff-reading__selected', text: 'Selected' });
    const ack = el('div');
    const application = el('div');
    const show = r.recorded
      ? el(
          'button',
          {
            class: 'button cuff-reading__show',
            attrs: { type: 'button', 'aria-pressed': 'false' },
            on: {
              click: () => {
                const now = find(r.key);
                if (now) pick(now);
              },
            },
            signal,
          },
          'Show trace',
          el('span', {
            class: 'visually-hidden',
            text: ` for ${whatWasSent(r)} at ${clockTime(r.result.sentAt, deps.timeZone)}`,
          }),
        )
      : null;
    const checkAgain = el('button', {
      class: 'button cuff-reading__again',
      text: 'Check again',
      attrs: { type: 'button' },
      on: {
        click: () => {
          const now = find(r.key);
          if (now) startPolling(now);
        },
      },
      signal,
    });
    const root = el(
      'li',
      { class: 'cuff-reading', id: r.key, attrs: { 'aria-labelledby': headingId } },
      el(
        'p',
        { class: 'cuff-reading__heading', id: headingId },
        el('time', {
          attrs: { datetime: r.result.sentAt },
          text: clockTime(r.result.sentAt, deps.timeZone),
        }),
        el('span', { class: 'cuff-reading__what', text: whatWasSent(r) }),
        el('span', { class: 'cuff-reading__transport', text: transportName(r.result.transport) }),
        selected,
      ),
      el('dl', { class: 'cuff-reading__facts' }, ack, application),
      r.recorded
        ? null
        : el('p', {
            class: 'cuff-note cuff-note--warn',
            text: `Not recorded: ${r.result.audit.recorded ? 'the attempt has no id' : r.result.audit.detail}. This attempt has no trace to open and is not in the messages log.`,
          }),
      el('div', { class: 'cuff-reading__actions' }, show, checkAgain),
    );
    return { root, selected, ack, application, show, checkAgain };
  };

  const renderRow = (r: Reading) => {
    const view = views.get(r.key);
    if (!view) return;
    replaceChildren(view.ack, ...phraseItem(COMMIT_LEVEL_LABEL, commitAckPhrase(r.result.ack)));
    replaceChildren(view.application, ...phraseItem(APPLICATION_LEVEL_LABEL, applicationPhrase(r)));
    const isSelected = selectedKey === r.key;
    view.root.classList.toggle('cuff-reading--selected', isSelected);
    view.selected.hidden = !isSelected;
    view.show?.setAttribute('aria-pressed', String(isSelected));
    view.checkAgain.hidden = !(r.lookup === 'gave-up' && r.application?.outcome === 'pending');
  };

  const renderAllRows = () => {
    for (const r of readings) renderRow(r);
  };

  const addReading = (r: Reading) => {
    readings.unshift(r);
    const view = createRow(r);
    views.set(r.key, view);
    list.prepend(view.root);
    listEmpty.hidden = true;
    renderRow(r);
    while (readings.length > maxReadings) {
      const old = readings.pop();
      if (!old) break;
      pollers.get(old.key)?.abort();
      pollers.delete(old.key);
      traces.delete(old.key);
      views.get(old.key)?.root.remove();
      views.delete(old.key);
      if (selectedKey === old.key) {
        selectedKey = null;
        traceController?.abort();
        selection.set(null);
        trace.render({ kind: 'empty' });
      }
    }
  };

  // -- selection and trace ---------------------------------------------------------------------
  /** Clears the selection because its attempt is gone from the audit store, and says so. */
  const dropSelection = (r: Reading) => {
    selectedKey = null;
    selection.set(null);
    trace.render({ kind: 'gone', summary: readingSummary(r, deps.timeZone) });
    renderAllRows();
  };

  /**
   * Applies a trace answer to its row. Returns false when the answer was older than what the row
   * already knows (it said "pending" after the row settled), and so was not used.
   */
  const applyTrace = (key: string, t: CuffAttemptTrace): boolean => {
    const before = find(key);
    if (!before) return false;
    const after = withTrace(before, t);
    const used = after !== before;
    if (used) {
      traces.set(key, t);
      replace(after);
    }
    if (selectedKey === key) {
      const latest = traces.get(key);
      const shown = find(key);
      if (latest && shown) {
        traceController?.abort();
        selection.set(selectionFor(shown));
        trace.render({
          kind: 'ready',
          reading: shown,
          trace: latest,
          summary: readingSummary(shown, deps.timeZone),
        });
      }
    }
    return used;
  };

  const loadTrace = (r: Reading) => {
    const id = r.result.attemptId;
    if (id === null) return;
    traceController?.abort();
    const c = childController(signal);
    traceController = c;
    const summary = readingSummary(r, deps.timeZone);
    trace.render({ kind: 'loading', summary });
    client.getCuffAttemptTrace(id, { signal: c.signal }).then(
      (t) => {
        // A newer pick, a deselection or a logout replaced this request: its answer is stale.
        if (c.signal.aborted || selectedKey !== r.key) return;
        applyTrace(r.key, t);
      },
      (error: unknown) => {
        if (isAbort(error, c.signal) || error instanceof UnauthorizedError) return;
        if (selectedKey !== r.key) return;
        const current = find(r.key) ?? r;
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
          pollers.get(r.key)?.abort();
          replace({ ...current, lookup: isPending(current) ? 'missing' : current.lookup });
          dropSelection(current);
          return;
        }
        // Not knowing is not shareable: say why on the selection, and offer a retry.
        const sel = selectionFor(current);
        if (sel)
          selection.set({
            ...sel,
            unshareableReason: `could not be checked: ${errorMessage(error)}`,
          });
        trace.render({
          kind: 'error',
          summary,
          message: errorMessage(error),
          retry: () => {
            const now = find(r.key);
            if (now && selectedKey === now.key) loadTrace(now);
          },
        });
      },
    );
  };

  const pick = (r: Reading) => {
    const sel = selectionFor(r);
    if (sel === null) return;
    selectedKey = r.key;
    selection.set(sel);
    renderAllRows();
    loadTrace(r);
  };

  selection.subscribe((current) => {
    const ours =
      current?.source === 'cuff'
        ? readings.find((r) => r.recorded && r.result.attemptId === current.attemptId)
        : undefined;
    if (ours) {
      selectedKey = ours.key;
    } else if (selectedKey !== null) {
      // The selection moved to the watch side or was cleared: drop our highlight and trace.
      selectedKey = null;
      traceController?.abort();
      trace.render({ kind: 'empty' });
    }
    renderAllRows();
  }, signal);

  // -- polling for the application-level outcome -----------------------------------------------
  function startPolling(r: Reading): void {
    const id = r.result.attemptId;
    if (id === null || !isPending(r)) return;
    pollers.get(r.key)?.abort();
    const c = childController(signal);
    pollers.set(r.key, c);
    replace({ ...r, lookup: 'polling' });
    pollUntil(
      async () => {
        try {
          const t = await client.getCuffAttemptTrace(id, { signal: c.signal });
          if (c.signal.aborted) return true;
          applyTrace(r.key, t);
          const now = find(r.key);
          return now === undefined || !isPending(now);
        } catch (error) {
          if (isAbort(error, c.signal) || error instanceof UnauthorizedError) throw error;
          if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
            const now = find(r.key);
            if (now) replace({ ...now, lookup: 'missing' });
            return true;
          }
          return false; // a passing failure: try again at the next tick
        }
      },
      { signal: c.signal, ...deps.poll },
    ).then(
      (done) => {
        if (c.signal.aborted) return;
        pollers.delete(r.key);
        const now = find(r.key);
        if (!done && now && isPending(now)) replace({ ...now, lookup: 'gave-up' });
      },
      () => {
        pollers.delete(r.key);
      },
    );
  }

  // -- sending ---------------------------------------------------------------------------------
  const sentMessage = (r: Reading): string => {
    const ack = commitAckPhrase(r.result.ack);
    let text = `Sent over ${transportName(r.result.transport)}. Commit-level acknowledgment: ${ack.label}.`;
    if (r.application?.outcome === 'pending' && r.recorded) text += ' Waiting for the mapper…';
    if (!r.recorded) {
      text += ` The attempt was not recorded (${r.result.audit.recorded ? 'no id' : r.result.audit.detail}), so it has no trace.`;
    }
    return text;
  };

  const send = (request: CuffReadingRequest) => {
    sending = true;
    updateButton();
    setMessage('Sending…', 'info');
    const c = childController(signal);
    sendController = c;
    const selectionAtStart = selection.get();
    client.submitCuffReading(request, { signal: c.signal }).then(
      (result) => {
        // Logged out (or torn down) meanwhile: the answer belongs to a dashboard that is gone.
        if (c.signal.aborted) return;
        sending = false;
        updateButton();
        const r = newReading(uniqueId('cuff-row'), request, result);
        addReading(r);
        setMessage(sentMessage(r), r.result.result === 'accepted' ? 'ok' : 'warn');
        context.notifyDataChanged('cuff-send');
        // Select the new reading, unless the user picked something else while it was sending.
        if (r.recorded && selection.get() === selectionAtStart) pick(r);
        if (isPending(r)) startPolling(r);
      },
      (error: unknown) => {
        if (isAbort(error, c.signal) || error instanceof UnauthorizedError) return;
        sending = false;
        updateButton();
        if (error instanceof CuffSendError) {
          const byField = fieldMessages(error);
          const errors: FieldErrors = {};
          for (const field of ['systolic', 'diastolic', 'transport', 'sendMalformed'] as const) {
            const text = byField[field];
            if (text !== undefined) errors[field] = `The simulator says: ${text}`;
          }
          showFieldErrors(errors);
          if (error.reason === 'rate-limited') startCountdown(error.retryAfterSeconds);
          // It may have been sent: whatever happened is now on the server, so others re-fetch.
          if (error.reason === 'no-answer') context.notifyDataChanged('cuff-send');
        }
        setMessage(sendFailureText(error).message, 'error');
      },
    );
  };

  form.addEventListener(
    'submit',
    (event) => {
      event.preventDefault();
      // One send at a time, and none while the rate limit's countdown runs.
      if (sending || waitUntil > 0) return;
      clearFieldErrors();
      const check = checkForm(formState(), samples);
      if (!check.ok) {
        const first = showFieldErrors(check.errors);
        setMessage('Not sent: correct the marked fields.', 'error');
        first?.focus();
        return;
      }
      send(check.request);
    },
    { signal },
  );

  // -- data changes from elsewhere -------------------------------------------------------------
  context.onDataChanged((kind) => {
    // 'cuff-send' is our own announcement; a sync or a share may change what is behind the
    // selected reading (for example its Observation), so re-check it before Share can use it.
    if (kind === 'cuff-send' || selectedKey === null) return;
    const r = find(selectedKey);
    const sel = r ? selectionFor(r) : null;
    if (!r || !sel) return;
    selection.set({ ...sel, unshareableReason: RECHECK_REASON });
    loadTrace(r);
  }, signal);

  signal.addEventListener('abort', () => {
    sendController?.abort();
  });

  updateButton();
  loadSamples();
}
