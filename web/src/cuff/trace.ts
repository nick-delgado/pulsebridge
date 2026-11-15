/**
 * The left trace panel: what one selected cuff send looks like underneath, in three tabs (brief,
 * "Trace panels"):
 *
 *   1. "HL7v2 as sent": the ORU^R01 message text, one segment per line, with the MLLP framing and
 *      segment terminator stated, the control ID (MSH-10) and the stored message's name (marked
 *      as derived when the simulator computed it rather than the store returning it).
 *   2. "ACK as received": two separately labelled items (D7):
 *        - "Commit-level acknowledgment (from the store)": the raw ACK with MSA-1 explained, or
 *          "no acknowledgment" with its reason;
 *        - "Application-level outcome (from the mapper)": accepted, error with the reason in
 *          words, or pending.
 *   3. "FHIR as stored": the resulting Observation and its Provenance when accepted, and the
 *      send-attempt AuditEvent from the audit store (D12, D15).
 *
 * Every value is rendered with `el` / `codeBlock`, which create text nodes only, so a hostile
 * string in a segment, an MSA-3 reason or a diagnostics text is shown as characters, never markup.
 *
 * The panel keeps one fixed-height frame for every state (empty, loading, error, ready), so
 * selecting a reading or switching tabs does not move the page (no layout shift).
 *
 * Rendering a ready trace again is cheap and quiet: each tab is a list of parts, and a part whose
 * content is unchanged keeps its DOM node. So a poll tick that changes nothing touches nothing
 * (focus and scroll position inside a code block survive), and an outcome that settles replaces
 * only the parts it changes. The summary line, a live region, is written only when its text
 * changes.
 */
import type { CommitAck, CuffAttemptTrace } from '../api/contract';
import { isDerivedMessageName } from '../api/contract';
import { codeBlock, el, placeholder, replaceChildren } from '../ui/dom';
import { createTabs, type Tabs } from '../ui/tabs';
import {
  ACK_CODE_MEANING,
  ACK_MODE_NOTE,
  applicationPhrase,
  commitAckPhrase,
  NO_ACK_REASON,
  type Reading,
} from './readings';
import { reasonText } from '../api/reasons';

export const TRACE_TABS = {
  hl7v2: { id: 'hl7v2', label: 'HL7v2 as sent' },
  ack: { id: 'ack', label: 'ACK as received' },
  fhir: { id: 'fhir', label: 'FHIR as stored' },
} as const;

export const COMMIT_LEVEL_LABEL = 'Commit-level acknowledgment (from the store)';
export const APPLICATION_LEVEL_LABEL = 'Application-level outcome (from the mapper)';

export type TraceState =
  | { kind: 'empty' }
  | { kind: 'loading'; summary: string }
  | { kind: 'error'; summary: string; message: string; retry: () => void }
  | { kind: 'gone'; summary: string; unreadable?: boolean }
  | { kind: 'ready'; reading: Reading; trace: CuffAttemptTrace; summary: string };

export interface TracePanel {
  render(state: TraceState): void;
}

export function tracePanel(slot: HTMLElement, signal: AbortSignal): TracePanel {
  // One frame for every state, so its height never changes (see the CSS: fixed block size).
  const heading = el('p', { class: 'cuff-trace__summary', attrs: { 'aria-live': 'polite' } });
  /** Writes the live summary only when its text changes, so it is announced once per change. */
  const setSummary = (text: string) => {
    if (heading.textContent !== text) heading.textContent = text;
  };
  const body = el('div', { class: 'cuff-trace__body' });
  const frame = el('div', { class: 'cuff-trace__frame' }, heading, body);
  replaceChildren(slot, frame);
  let tabs: Tabs | null = null;

  const ensureTabs = (): Tabs => {
    tabs ??= createTabs(
      'Blood-pressure trace',
      [TRACE_TABS.hl7v2, TRACE_TABS.ack, TRACE_TABS.fhir],
      signal,
    );
    return tabs;
  };

  return {
    render(state) {
      if (state.kind === 'empty') {
        setSummary('');
        replaceChildren(
          body,
          placeholder(
            'Select a reading to see its trace: the HL7v2 message as sent, the acknowledgment as received with the outcome from the mapper, and the FHIR resources as stored.',
          ),
        );
        return;
      }
      setSummary(state.summary);
      if (state.kind === 'loading') {
        replaceChildren(
          body,
          el('p', {
            class: 'placeholder',
            text: `Loading the trace for ${state.summary}…`,
            attrs: { role: 'status' },
          }),
        );
        return;
      }
      if (state.kind === 'gone') {
        replaceChildren(
          body,
          el('p', {
            class: 'cuff-note cuff-note--warn',
            text: state.unreadable
              ? 'This send attempt’s record in the audit store could not be read, so it has no trace and is no longer selected. The messages log lists it as unreadable.'
              : 'This send attempt is no longer in the audit store, so it has no trace and is no longer selected.',
            attrs: { role: 'status' },
          }),
        );
        return;
      }
      if (state.kind === 'error') {
        replaceChildren(
          body,
          el('p', {
            class: 'cuff-note cuff-note--error',
            text: `Could not load the trace: ${state.message}`,
            attrs: { role: 'alert' },
          }),
          el('button', {
            class: 'button',
            text: 'Try again',
            attrs: { type: 'button' },
            on: { click: state.retry },
            signal,
          }),
        );
        return;
      }
      const t = ensureTabs();
      patch(t.panel(TRACE_TABS.hl7v2.id), hl7v2Parts(state.reading, state.trace));
      patch(t.panel(TRACE_TABS.ack.id), ackParts(state.reading));
      patch(t.panel(TRACE_TABS.fhir.id), fhirParts(state.reading, state.trace));
      if (t.root.parentNode !== body) replaceChildren(body, t.root);
    },
  };
}

/**
 * One block of a tab: `key` names it, `content` is everything it is built from (compared as
 * JSON), and `build` makes its element. A part is rebuilt only when its content changed.
 */
interface Part {
  key: string;
  content: unknown;
  build: () => HTMLElement;
}

const contentOf = new WeakMap<Element, string>();

/** Makes `panel` hold exactly `parts`, in order, keeping the node of every unchanged part. */
function patch(panel: HTMLElement, parts: (Part | null)[]): void {
  const old = new Map<string, HTMLElement>();
  for (const child of [...panel.children]) {
    if (child instanceof HTMLElement) old.set(child.dataset.part ?? '', child);
  }
  const nodes = parts
    .filter((p): p is Part => p !== null)
    .map((p) => {
      // Every part's content is defined (null where there is nothing), so this is a string.
      const fingerprint = JSON.stringify(p.content);
      const existing = old.get(p.key);
      if (existing && contentOf.get(existing) === fingerprint) return existing;
      const node = p.build();
      node.dataset.part = p.key;
      contentOf.set(node, fingerprint);
      return node;
    });
  const keep = new Set<Node>(nodes);
  for (const child of [...panel.children]) if (!keep.has(child)) child.remove();
  nodes.forEach((node, i) => {
    if (panel.children[i] !== node) panel.insertBefore(node, panel.children[i] ?? null);
  });
}

function section(title: string, ...children: (Node | null)[]): HTMLElement {
  return el(
    'section',
    { class: 'cuff-trace__section' },
    el('h4', { class: 'cuff-trace__heading', text: title }),
    ...children,
  );
}

function facts(rows: [term: string, value: string][]): HTMLDListElement {
  const list = el('dl', { class: 'cuff-facts' });
  for (const [term, value] of rows)
    list.append(el('dt', { text: term }), el('dd', { text: value }));
  return list;
}

/** The segment terminator the message uses, in words; the code block shows one segment a line. */
export function terminatorNote(message: string): string {
  if (message.includes('\r\n')) {
    return 'Segments end with CR LF (a Windows line ending), not the standard CR. Shown one per line.';
  }
  if (message.includes('\n') && !message.includes('\r')) {
    return 'Segments end with LF (a line feed), not the standard CR. Shown one per line.';
  }
  return 'Segments end with CR (carriage return), as HL7v2 requires. Shown one per line.';
}

/** The MLLP framing in words; null over HTTPS. */
export function framingNote(framing: Reading['result']['framing'], transport: string): string {
  if (transport === 'https') {
    return 'Sent over HTTPS to the HL7v2 store’s messages:ingest endpoint: no MLLP frame.';
  }
  switch (framing) {
    case 'standard':
      return 'Sent over MLLP in a standard frame: start byte 0x0B, the message, then the end bytes 0x1C 0x0D.';
    case 'missing_end_block':
      return 'Framing NOT standard: sent over MLLP without the end bytes 0x1C 0x0D, on purpose. The adapter keeps waiting for the end of the frame, so no acknowledgment comes back.';
    case 'missing_final_cr':
      return 'Framing NOT standard: sent over MLLP with the end byte 0x1C but without the final 0x0D, on purpose, so the adapter never sees a complete frame.';
    default:
      return 'Sent over MLLP; the server did not say which framing was used.';
  }
}

/** The stored message's name and where it came from. */
export function messageNameFacts(reading: Reading): [string, string][] {
  const r = reading.result;
  if (r.messageName === null) {
    return [
      [
        'Stored message name',
        r.result === 'accepted'
          ? 'Not given.'
          : 'None: the store did not report storing this message.',
      ],
    ];
  }
  if (isDerivedMessageName(r)) {
    const check: [string, string][] =
      reading.lookup === 'message-not-found'
        ? [['Store check', 'Not found: the HL7v2 store has no message under this derived name.']]
        : reading.lookup === 'message-unchecked'
          ? [['Store check', 'Could not be checked: the proxy could not ask the store in time.']]
          : [];
    return [
      ['Stored message name (derived, unconfirmed)', r.messageName],
      [
        'Why derived',
        'An MLLP acknowledgment does not carry the stored name, so the simulator computed it from the SHA-256 of the bytes it sent. The store has not confirmed it.',
      ],
      ...check,
    ];
  }
  return [['Stored message name (returned by the store)', r.messageName]];
}

/** What the HL7v2 tab says the message is: an ORU^R01, or a deliberately malformed sample. */
export function messageNote(reading: Reading): string {
  const sample = reading.result.sample;
  const oru =
    'an ORU^R01 (observation result) message: MSH is the header, PID the patient, OBR the order (LOINC 85354-9, the blood-pressure panel) and one OBX per value (8480-6 systolic, 8462-4 diastolic).';
  return sample === null
    ? `A built reading, ${oru}`
    : `A deliberately malformed sample (${sample}), sent as its own fixed bytes; it may not be an ORU^R01 at all. A well-formed reading is ${oru}`;
}

function hl7v2Parts(reading: Reading, trace: CuffAttemptTrace): (Part | null)[] {
  const message = trace.messageSent ?? reading.result.messageSent;
  const fromResult = trace.messageSent === null;
  const r = reading.result;
  const nonStandard = r.framing !== null && r.framing !== 'standard';
  return [
    {
      key: 'what',
      content: r.sample,
      build: () => el('p', { class: 'cuff-trace__note', text: messageNote(reading) }),
    },
    {
      key: 'framing',
      content: [r.framing, r.transport],
      build: () =>
        el('p', {
          class: nonStandard ? 'cuff-note cuff-note--warn' : 'cuff-trace__note',
          text: framingNote(r.framing, r.transport),
        }),
    },
    {
      key: 'terminator',
      content: terminatorNote(message),
      build: () => el('p', { class: 'cuff-trace__note', text: terminatorNote(message) }),
    },
    fromResult
      ? {
          key: 'copy',
          content: true,
          build: () =>
            el('p', {
              class: 'cuff-trace__note',
              text: 'The server did not return the message for this attempt; this is the copy the send returned.',
            }),
        }
      : null,
    {
      key: 'message',
      content: message,
      build: () => codeBlock(message, 'HL7v2 message as sent (ER7)'),
    },
    {
      key: 'facts',
      content: [r.controlId, r.sentAt, messageNameFacts(reading)],
      build: () =>
        facts([
          ['Control ID (MSH-10)', r.controlId ?? 'None: the message has no readable MSH-10.'],
          ['Sent at', r.sentAt],
          ...messageNameFacts(reading),
        ]),
    },
  ];
}

function commitSection(ack: CommitAck): HTMLElement {
  if (!ack.received) {
    return section(
      COMMIT_LEVEL_LABEL,
      el('p', { class: 'cuff-note cuff-note--warn', text: commitAckPhrase(ack).detail }),
      facts([
        ['Reason', NO_ACK_REASON[ack.why]],
        ...(ack.httpStatus !== null
          ? [['HTTP status', String(ack.httpStatus)] as [string, string]]
          : []),
        ['Detail', ack.detail],
      ]),
      ack.replyPrefixBase64
        ? el(
            'div',
            {},
            el('p', {
              class: 'cuff-trace__note',
              text: 'The first bytes that came back, base64-encoded because they may not be text:',
            }),
            codeBlock(ack.replyPrefixBase64, 'Reply bytes (base64)'),
          )
        : null,
    );
  }
  return section(
    COMMIT_LEVEL_LABEL,
    el('p', {
      class: `cuff-note cuff-note--${commitAckPhrase(ack).tone}`,
      text: `MSA-1 ${ack.code} (original acknowledgment mode): ${ACK_CODE_MEANING[ack.code]}`,
    }),
    facts([
      ['MSA-2 (control ID acknowledged)', ack.controlId ?? 'Empty'],
      ['MSA-3 (reason)', ack.reason ?? 'None'],
    ]),
    ack.raw !== null
      ? codeBlock(ack.raw, 'ACK as received (ER7)')
      : placeholder('The raw acknowledgment is not available.'),
  );
}

function ackParts(reading: Reading): Part[] {
  const app = applicationPhrase(reading);
  const reason = reading.application?.reason ?? null;
  // The ACK as the send received it: it carries MSA-2, which the attempt record does not keep.
  const ack = reading.result.ack;
  return [
    {
      key: 'intro',
      content: 1,
      build: () =>
        el(
          'div',
          { class: 'cuff-trace__intro' },
          el('p', {
            class: 'cuff-trace__note',
            text: 'Two separate answers. The store’s ACK only says whether it stored the message; whether the reading became FHIR is decided later by the mapper.',
          }),
          el('p', { class: 'cuff-trace__note cuff-ack-mode', text: ACK_MODE_NOTE }),
        ),
    },
    { key: 'commit', content: ack, build: () => commitSection(ack) },
    {
      key: 'application',
      content: [app, reading.application],
      build: () =>
        section(
          APPLICATION_LEVEL_LABEL,
          el('p', { class: `cuff-note cuff-note--${app.tone}`, text: app.detail }),
          reading.application?.outcome === 'error'
            ? facts([
                ['Reason code', reason ?? 'none'],
                ['In words', reasonText(reason)],
              ])
            : null,
        ),
    },
  ];
}

function fhirParts(reading: Reading, trace: CuffAttemptTrace): (Part | null)[] {
  const why =
    reading.application?.outcome === 'pending'
      ? 'No Observation yet: the mapper has not run for this message.'
      : reading.application?.outcome === 'error'
        ? 'No Observation: the mapper could not map this message (see "ACK as received").'
        : reading.application === null
          ? 'No Observation: the message was not stored, so nothing was mapped.'
          : 'No Observation was returned for this reading.';
  const { observation, provenance, auditEvent } = trace;
  return [
    {
      key: 'intro',
      content: 1,
      build: () =>
        el('p', {
          class: 'cuff-trace__note',
          text: 'The blood-pressure panel Observation (LOINC 85354-9, systolic and diastolic as components) and the Provenance pointing at the HL7v2 message it came from, as the FHIR store holds them; and the AuditEvent recording this send attempt in the separate audit store.',
        }),
    },
    {
      key: 'observation',
      content: observation ?? why,
      build: () =>
        section(
          'Observation',
          observation ? codeBlock(observation, 'Observation JSON') : placeholder(why),
        ),
    },
    provenance
      ? {
          key: 'provenance',
          content: provenance,
          build: () => section('Provenance', codeBlock(provenance, 'Provenance JSON')),
        }
      : null,
    {
      key: 'audit',
      content: auditEvent,
      build: () =>
        section(
          'Send-attempt record (AuditEvent, audit store)',
          auditEvent
            ? codeBlock(auditEvent, 'AuditEvent JSON')
            : placeholder('The AuditEvent was not returned.'),
        ),
    },
  ];
}
