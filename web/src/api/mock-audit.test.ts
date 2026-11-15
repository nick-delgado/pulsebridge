import { beforeAll, describe, expect, it } from 'vitest';
import type { SendAttemptEntry } from './contract';
import type { AuditEvent } from './fhir';
import { MockClient } from './mock-client';
import { Session } from './session';
// T31's generated examples, copied from libs/pulsebridge_fhir/examples/ (see t31-examples/README.md).
import accepted from './t31-examples/auditevent-send-accepted.json';
import noAck from './t31-examples/auditevent-send-no-ack.json';
import rejected from './t31-examples/auditevent-send-rejected.json';
import unreadable from './t31-examples/auditevent-send-unreadable-reply.json';

const NOW = Date.parse('2026-03-10T15:02:00Z');

/** Every leaf of a resource as [path, value], with array indices, so order and count matter. */
function leaves(value: unknown, path = ''): [string, unknown][] {
  if (Array.isArray(value)) return value.flatMap((v, i) => leaves(v, `${path}.${String(i)}`));
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value).flatMap(([k, v]) => leaves(v, path ? `${path}.${k}` : k));
  }
  return [[path, value]];
}

/** Leaves whose value is part of the model (codes, systems, roles), not of one attempt. */
const MODEL_LEAF =
  /(\.|^)(system|code|display|action|outcome|requestor|resourceType)$|\.who\.type$|\.what\.type$|detail\.\d+\.type$/;
/** detail entries whose value is fixed by the scenario, not by the particular attempt. */
const SCENARIO_DETAILS = new Set([
  'format-version',
  'transport',
  'MSA-1',
  'MSA-3',
  'no-ack-reason',
  'malformed-sample',
]);

function detailOf(event: AuditEvent | Record<string, unknown>, type: string) {
  const e = event as AuditEvent;
  return e.entity?.[1]?.detail?.find((d) => d.type === type);
}

describe("mock send-attempt AuditEvents against T31's examples", () => {
  const events = new Map<string, { event: AuditEvent; entry: SendAttemptEntry }>();

  beforeAll(async () => {
    sessionStorage.clear();
    const session = new Session(sessionStorage, () => NOW);
    const client = new MockClient({
      session,
      onUnauthorized: () => undefined,
      now: () => NOW,
      latencyMs: 0,
    });
    await client.login('x');
    const { entries } = await client.listMessages({ kind: 'cuff-send' });
    const pick = (name: string, test: (e: SendAttemptEntry) => boolean) => {
      const entry = (entries as SendAttemptEntry[]).find(test);
      if (!entry) throw new Error(`mock has no ${name} attempt`);
      return entry;
    };
    const scenarios: [string, SendAttemptEntry][] = [
      ['accepted', pick('accepted', (e) => e.result === 'accepted' && e.messageName !== null)],
      ['rejected', pick('rejected', (e) => e.sample === 'unparseable_no_msh')],
      ['no-ack', pick('no-ack', (e) => e.sample === 'noack_missing_end_block')],
      [
        'unreadable',
        pick('unreadable', (e) => !e.commitAck.received && e.commitAck.why === 'unreadable-reply'),
      ],
    ];
    for (const [name, entry] of scenarios) {
      const trace = await client.getCuffAttemptTrace(entry.id);
      if (!trace.auditEvent) throw new Error(`no AuditEvent for ${name}`);
      events.set(name, { event: trace.auditEvent, entry });
    }
  });

  const cases: [string, Record<string, unknown>][] = [
    ['accepted', accepted],
    ['rejected', rejected],
    ['no-ack', noAck],
    ['unreadable', unreadable],
  ];

  it.each(cases)('%s: same elements, in the same places, as the example', (name, example) => {
    const mine = events.get(name)?.event;
    expect(leaves(mine).map(([p]) => p)).toEqual(leaves(example).map(([p]) => p));
  });

  it.each(cases)('%s: same codes, systems, roles and outcome as the example', (name, example) => {
    const mine = new Map(leaves(events.get(name)?.event));
    for (const [path, value] of leaves(example)) {
      if (MODEL_LEAF.test(path)) expect(mine.get(path), path).toEqual(value);
    }
    expect((events.get(name)?.event as AuditEvent).outcomeDesc).toBe(
      (example as { outcomeDesc?: string }).outcomeDesc,
    );
  });

  it.each(cases)(
    '%s: same scenario detail values (transport, MSA-1, reason, sample)',
    (name, example) => {
      const mine = events.get(name)?.event;
      if (!mine) throw new Error(name);
      for (const type of SCENARIO_DETAILS) {
        expect(detailOf(mine, type), type).toEqual(detailOf(example, type));
      }
    },
  );

  it('keeps the raw ACK base64 in the resource, and the log row has the same bytes decoded', () => {
    const found = events.get('rejected');
    if (!found) throw new Error('rejected');
    const stored = detailOf(found.event, 'ack-message')?.valueBase64Binary;
    expect(stored).toBeDefined();
    expect(found.entry.commitAck.received && found.entry.commitAck.raw).toBe(atob(stored ?? ''));
  });

  it('names the stored message under the source-record system only when it was stored', () => {
    expect(events.get('accepted')?.event.entity?.[1]?.what?.identifier?.system).toBe(
      'urn:pulsebridge:source-record:hl7v2-message',
    );
    expect(events.get('rejected')?.event.entity?.[1]?.what).toBeUndefined();
  });
});
