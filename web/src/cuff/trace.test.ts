import { afterEach, describe, expect, it } from 'vitest';
import type { CuffAttemptTrace, CuffSendResult } from '../api/contract';
import { newReading, withTrace, type Reading } from './readings';
import {
  APPLICATION_LEVEL_LABEL,
  COMMIT_LEVEL_LABEL,
  framingNote,
  messageNameFacts,
  terminatorNote,
  tracePanel,
  type TracePanel,
} from './trace';
import { AE, attemptTrace, bpObservation, noAck, sendResult, STORE } from './test-support';

const built = { systolic: 120, diastolic: 80, transport: 'mllp', sendMalformed: null } as const;

let teardown: AbortController | null = null;
afterEach(() => {
  teardown?.abort();
  document.body.replaceChildren();
});

function mountPanel(): { slot: HTMLElement; panel: TracePanel } {
  teardown = new AbortController();
  const slot = document.createElement('div');
  document.body.append(slot);
  return { slot, panel: tracePanel(slot, teardown.signal) };
}

function ready(reading: Reading, trace: CuffAttemptTrace) {
  const { slot, panel } = mountPanel();
  panel.render({ kind: 'ready', reading, trace, summary: 'BP 120/80' });
  const tab = (label: string) => {
    const t = [...slot.querySelectorAll<HTMLElement>('[role="tab"]')].find(
      (x) => x.textContent === label,
    );
    if (!t) throw new Error(`no tab ${label}`);
    const panelId = t.getAttribute('aria-controls') ?? '';
    const p = slot.querySelector<HTMLElement>(`#${panelId}`);
    if (!p) throw new Error(`no panel for ${label}`);
    return p;
  };
  return { slot, tab };
}

function settledReading(result: CuffSendResult, trace: CuffAttemptTrace): Reading {
  return withTrace(newReading('cuff-row-t', built, result), trace);
}

describe('trace panel: three tabs', () => {
  it('has exactly the brief’s three tabs, the first selected', () => {
    const result = sendResult();
    const trace = attemptTrace(result, { outcome: 'pending', reason: null });
    const { slot } = ready(settledReading(result, trace), trace);
    const tabs = [...slot.querySelectorAll('[role="tab"]')];
    expect(tabs.map((t) => t.textContent)).toEqual([
      'HL7v2 as sent',
      'ACK as received',
      'FHIR as stored',
    ]);
    expect(tabs.map((t) => t.getAttribute('aria-selected'))).toEqual(['true', 'false', 'false']);
  });

  it('accepted: message one segment per line, AA explained, accepted outcome, Observation and Provenance', () => {
    const result = sendResult();
    const trace = attemptTrace(
      result,
      { outcome: 'accepted', reason: null },
      { observation: bpObservation('bp-7') },
    );
    const { tab } = ready(settledReading(result, trace), trace);

    const hl7 = tab('HL7v2 as sent');
    const code = hl7.querySelector('pre code')?.textContent ?? '';
    expect(
      code
        .split('\n')
        .slice(0, 5)
        .map((l) => l.slice(0, 3)),
    ).toEqual(['MSH', 'PID', 'OBR', 'OBX', 'OBX']);
    expect(hl7.textContent).toContain('standard frame: start byte 0x0B');
    expect(hl7.textContent).toContain('Control ID (MSH-10)C0FFEE0000000000000A');

    const ack = tab('ACK as received');
    const headings = [...ack.querySelectorAll('h4')].map((h) => h.textContent);
    expect(headings).toEqual([COMMIT_LEVEL_LABEL, APPLICATION_LEVEL_LABEL]);
    expect(COMMIT_LEVEL_LABEL).toBe('Commit-level acknowledgment (from the store)');
    expect(APPLICATION_LEVEL_LABEL).toBe('Application-level outcome (from the mapper)');
    expect(ack.textContent).toContain(
      'MSA-1 AA: Application Accept: the HL7v2 store parsed the message and stored it.',
    );
    expect(ack.querySelector('pre code')?.textContent).toBe(
      'MSH|^~\\&|||||20260301091531+0000||ACK|A1|P|2.5.1\nMSA|AA|C0FFEE0000000000000A',
    );
    expect(ack.textContent).toContain('Accepted: the mapper wrote a FHIR Observation');

    const fhir = tab('FHIR as stored');
    const blocks = [...fhir.querySelectorAll('pre')].map((p) => p.getAttribute('aria-label'));
    expect(blocks).toEqual(['Observation JSON', 'Provenance JSON', 'AuditEvent JSON']);
    expect(fhir.textContent).toContain('"id": "bp-7"');
  });

  it('rejected (AE): MSA-3 shown, no application outcome, no Observation', () => {
    const result = sendResult({ ack: AE, sample: 'unparseable_no_msh' });
    const trace = attemptTrace(result, null);
    const { tab } = ready(
      newReading('k', { transport: 'mllp', sendMalformed: 'unparseable_no_msh' }, result),
      trace,
    );
    const ack = tab('ACK as received');
    expect(ack.textContent).toContain('MSA-1 AE: Application Error');
    expect(ack.textContent).toContain('MSA-3 (reason)Invalid MSH header');
    expect(ack.textContent).toContain('Not stored, so the mapper never saw it');
    expect(tab('FHIR as stored').textContent).toContain(
      'No Observation: the message was not stored, so nothing was mapped.',
    );
    expect(tab('HL7v2 as sent').textContent).toContain(
      'None: the store did not report storing this message.',
    );
  });

  it('error: the reason code and its sentence, as two facts', () => {
    const result = sendResult();
    const trace = attemptTrace(result, { outcome: 'error', reason: 'out-of-range' });
    const { tab } = ready(settledReading(result, trace), trace);
    const ack = tab('ACK as received');
    expect(ack.textContent).toContain('Reason codeout-of-range');
    expect(ack.textContent).toContain(
      'In wordsA value is outside the plausible range (systolic 50 to 300, diastolic 20 to 200 mm[Hg]).',
    );
    expect(tab('FHIR as stored').textContent).toContain('No Observation: the mapper could not map');
  });

  it('pending: says the mapper has not run', () => {
    const result = sendResult();
    const trace = attemptTrace(result, { outcome: 'pending', reason: null });
    const { tab } = ready(settledReading(result, trace), trace);
    expect(tab('ACK as received').textContent).toContain('Pending: stored, waiting for the mapper');
    expect(tab('FHIR as stored').textContent).toContain('No Observation yet');
  });

  it.each(['timeout', 'connection-closed', 'api-error', 'unreadable-reply'] as const)(
    'no acknowledgment (%s): the reason and detail, never an ACK code',
    (why) => {
      const result = sendResult({ ack: noAck(why) });
      const trace = attemptTrace(result, null);
      const { tab } = ready(newReading('k', built, result), trace);
      const ack = tab('ACK as received');
      expect(ack.textContent).toContain('No acknowledgment received');
      expect(ack.textContent).toContain(`Detailsynthetic ${why} detail`);
      expect(ack.textContent).not.toContain('MSA-1');
      expect(ack.textContent?.includes('HTTP status503')).toBe(why === 'api-error');
      expect(ack.textContent?.includes('77u/TVNIfA==')).toBe(why === 'unreadable-reply');
    },
  );
});

describe('message name, framing and terminators', () => {
  it('a derived MLLP name is labelled derived and unconfirmed', () => {
    const r = newReading('k', built, sendResult());
    expect(messageNameFacts(r)[0]).toEqual([
      'Stored message name (derived, unconfirmed)',
      `${STORE}/messages/derivedAbc=`,
    ]);
  });

  it('an HTTPS name from the ingest response is labelled as the store’s', () => {
    const r = newReading(
      'k',
      built,
      sendResult({
        transport: 'https',
        framing: null,
        messageName: `${STORE}/messages/xyz`,
        messageNameSource: 'ingest-response',
      }),
    );
    expect(messageNameFacts(r)).toEqual([
      ['Stored message name (returned by the store)', `${STORE}/messages/xyz`],
    ]);
  });

  it('the derived label shows in the HL7v2 tab', () => {
    const result = sendResult();
    const trace = attemptTrace(result, { outcome: 'pending', reason: null });
    const { tab } = ready(newReading('k', built, result), trace);
    expect(tab('HL7v2 as sent').textContent).toContain(
      'Stored message name (derived, unconfirmed)',
    );
  });

  it('framing is called out when it is not standard', () => {
    expect(framingNote('missing_end_block', 'mllp')).toMatch(/^Framing NOT standard: .*0x1C 0x0D/);
    expect(framingNote('missing_final_cr', 'mllp')).toMatch(/^Framing NOT standard: .*0x0D/);
    expect(framingNote(null, 'https')).toContain('no MLLP frame');
    expect(framingNote('standard', 'mllp')).not.toContain('NOT');
  });

  it('a non-standard segment terminator is named (one per line would hide it)', () => {
    expect(terminatorNote('MSH|a\rPID|b\r')).toContain('end with CR (carriage return)');
    expect(terminatorNote('MSH|a\nPID|b\n')).toContain('end with LF');
    expect(terminatorNote('MSH|a\r\nPID|b\r\n')).toContain('end with CR LF');
  });

  it('when the server has no message text, the send’s own copy is shown and said so', () => {
    const result = sendResult();
    const trace = attemptTrace(result, { outcome: 'pending', reason: null }, { messageSent: null });
    const { tab } = ready(newReading('k', built, result), trace);
    expect(tab('HL7v2 as sent').textContent).toContain('this is the copy the send returned');
    expect(tab('HL7v2 as sent').querySelector('pre code')?.textContent).toContain(
      'MSH|^~\\&|CUFFSIM',
    );
  });
});

describe('hostile strings are text', () => {
  it('markup in the message, the ACK reason and the raw ACK creates no element', () => {
    const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
    const result = sendResult({
      messageSent: `MSH|^~\\&|${evil}\rPID|${evil}\r`,
      ack: { received: true, code: 'AE', controlId: null, reason: evil, raw: `MSA|AE||${evil}` },
      controlId: evil,
    });
    const trace = attemptTrace(result, null);
    const { slot, tab } = ready(newReading('k', built, result), trace);
    expect(slot.querySelectorAll('img, script')).toHaveLength(0);
    expect(tab('HL7v2 as sent').querySelector('pre code')?.textContent).toContain(evil);
    expect(tab('ACK as received').textContent).toContain(`MSA-3 (reason)${evil}`);
  });
});

describe('states keep one frame', () => {
  it('empty, loading, error with retry, gone and ready all render inside the same frame', () => {
    const { slot, panel } = mountPanel();
    const frame = slot.querySelector('.cuff-trace__frame');
    panel.render({ kind: 'empty' });
    expect(slot.textContent).toContain('Select a reading to see its trace');
    panel.render({ kind: 'loading', summary: 'BP 1' });
    expect(slot.querySelector('[role="status"]')?.textContent).toBe('Loading the trace for BP 1…');
    let retried = 0;
    panel.render({
      kind: 'error',
      summary: 'BP 1',
      message: 'boom',
      retry: () => {
        retried += 1;
      },
    });
    expect(slot.querySelector('[role="alert"]')?.textContent).toBe(
      'Could not load the trace: boom',
    );
    slot.querySelector<HTMLButtonElement>('button')?.click();
    expect(retried).toBe(1);
    panel.render({ kind: 'gone', summary: 'BP 1' });
    expect(slot.textContent).toContain('no longer in the audit store');
    expect(slot.querySelector('.cuff-trace__frame')).toBe(frame);
  });
});
