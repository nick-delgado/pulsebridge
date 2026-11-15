import { describe, expect, it } from 'vitest';
// The literal source: T07's manifest of synthetic HL7v2 samples.
import manifest from '../../../libs/pulsebridge_hl7v2/samples/samples.json';
import { MOCK_SAMPLES } from './mock-data';

interface SourceSample {
  name: string;
  class: string;
  description: string;
  transports: string[];
  segments: string[];
  segment_terminator: string;
  terminate_last_segment: boolean;
  expected: {
    commit_ack: string | null;
    commit_ack_reason: string | null;
    application_outcome: string | null;
    mapping_reason: string | null;
  };
}
const source = (manifest as { samples: SourceSample[]; classes: string[] }).samples;

describe('mock malformed samples against T07 samples.json', () => {
  it('offers every malformed sample T07 defines, and nothing else', () => {
    expect(MOCK_SAMPLES.map((s) => s.name).sort()).toEqual(
      source
        .filter((s) => s.class !== 'valid')
        .map((s) => s.name)
        .sort(),
    );
  });

  it('uses T07 class names, transports, payload bytes and expected outcomes', () => {
    const classes = (manifest as { classes: string[] }).classes;
    for (const mock of MOCK_SAMPLES) {
      const s = source.find((x) => x.name === mock.name);
      if (!s) throw new Error(mock.name);
      expect(classes, mock.name).toContain(mock.class);
      expect(mock.class, mock.name).toBe(s.class);
      expect(mock.transports, mock.name).toEqual(s.transports);
      expect(mock.label, mock.name).toBe(s.description);
      expect(mock.er7, mock.name).toBe(
        s.segments.join(s.segment_terminator) +
          (s.terminate_last_segment ? s.segment_terminator : ''),
      );
      expect(mock.commitAck, mock.name).toBe(s.expected.commit_ack);
      expect(mock.commitReason, mock.name).toBe(s.expected.commit_ack_reason);
      expect(mock.application, mock.name).toBe(s.expected.application_outcome);
      expect(mock.mappingReason, mock.name).toBe(s.expected.mapping_reason);
    }
  });
});
