/**
 * The application-level outcome reasons (D7) as readable text: the ONE copy the page has, shared
 * by the cuff trace (T18) and the messages log (T19).
 *
 * v2-to-fhir (T12) labels every stored HL7v2 message with `application_outcome` and a `reason`
 * code. The codes are label values (lower case, digits and `-`), not sentences, so the page looks
 * them up here. The sentences are a copy of `SENTENCES` in services/v2-to-fhir/v2_to_fhir/reasons.py;
 * `reasons.test.ts` checks that every code in that file has a sentence here, so the two cannot
 * drift without a test failing.
 *
 * The HL7v2 field names in the sentences (MSH-9, OBX-14, ...) are "segment-field": MSH-9 is the
 * ninth field of the MSH (message header) segment, OBX-14 the fourteenth of an OBX (observation)
 * segment.
 */
import { MAPPER_PLAUSIBLE_RANGE } from './contract';

const { systolic: S, diastolic: D } = MAPPER_PLAUSIBLE_RANGE;

export const REASON_SENTENCES: Readonly<Record<string, string>> = {
  mapped: 'The reading was mapped to a FHIR Observation and written to the FHIR store.',
  'unparseable-by-mapper':
    'The mapper could not read the stored bytes as an HL7v2 message, although the HL7v2 store accepted them.',
  'unsupported-version':
    'MSH-12 (version ID) is not 2.5.1, the only HL7v2 version this mapper reads.',
  'unsupported-message-type':
    'MSH-9 (message type) is not ORU^R01, the observation result message this mapper reads.',
  'missing-control-id':
    'MSH-10 (message control ID) is empty, so the reading has no key to be stored under.',
  'control-id-too-long': 'MSH-10 (message control ID) is longer than 199 characters.',
  'missing-sending-application':
    'MSH-3 (sending application) is empty, so the measuring device cannot be named.',
  'invalid-sending-application':
    'MSH-3 or MSH-4 (sending application or facility) contains control characters.',
  'no-observations': 'The message has no OBX (observation) segments: there is no reading.',
  'multiple-patients':
    'The message has more than one PID segment; one reading is about one patient.',
  'missing-patient-identifier': 'There is no PID segment, or PID-3.1 (patient ID) is empty.',
  'unknown-patient': 'No PID-3 identifier matches the patient this deployment is configured for.',
  'invalid-patient-name': 'PID-5 (patient name) contains control characters.',
  'missing-obr':
    'The OBX segments are not under an OBR (observation request) segment that opens the order.',
  'multiple-orders':
    'The message has more than one order (OBR); one reading is one blood-pressure panel.',
  'unknown-obr-4-code':
    'OBR-4 (universal service identifier) is not the LOINC blood-pressure panel 85354-9.',
  'unknown-obx-3-code':
    'An OBX-3 (observation identifier) is not LOINC 8480-6 (systolic) or 8462-4 (diastolic) with coding system LN.',
  'duplicate-obx-3-code': 'Two OBX segments carry the same OBX-3 code.',
  'missing-systolic': 'There is no OBX with LOINC 8480-6 (systolic blood pressure).',
  'missing-diastolic': 'There is no OBX with LOINC 8462-4 (diastolic blood pressure).',
  'wrong-value-type': 'An OBX-2 (value type) is not NM (numeric).',
  'missing-obx-5': 'An OBX-5 (observation value) is empty.',
  'repeated-obx-5': 'An OBX-5 (observation value) has more than one repetition.',
  'non-numeric-obx-5': 'An OBX-5 (observation value) is not a number.',
  'missing-unit': 'An OBX-6 (units) is empty.',
  'wrong-unit': 'An OBX-6 (units) is not the UCUM code mm[Hg].',
  'unsupported-result-status':
    'An OBX-11 (result status) is not F (final) or P (preliminary); corrections and deletions are not supported.',
  'mixed-result-status': 'The two OBX segments have different OBX-11 result statuses.',
  'missing-obx-14': 'An OBX-14 (date/time of the observation) is empty.',
  'repeated-obx-14': 'An OBX-14 (date/time of the observation) has more than one value.',
  'invalid-obx-14':
    'An OBX-14 (date/time of the observation) is not an HL7 date/time to at least the minute.',
  'missing-time-zone':
    'An OBX-14 (date/time of the observation) has no UTC offset, so the moment is ambiguous.',
  'mismatched-obx-14': 'The systolic and diastolic OBX-14 times are different moments.',
  'out-of-range': `A value is outside the plausible range (systolic ${S.min} to ${S.max}, diastolic ${D.min} to ${D.max} mm[Hg]).`,
  'systolic-not-above-diastolic': 'The systolic value is not greater than the diastolic.',
  'fhir-input-rejected':
    'The FHIR builders refused a value read from the message; see the service log.',
  'mapping-failed': 'The mapper hit an unexpected problem with this message; see the service log.',
  'fhir-rejected': 'The FHIR store refused the mapped Bundle (HTTP 4xx); see the service log.',
};

/**
 * The sentence for a reason code. An unknown code is shown as itself with a note, never dropped
 * and never replaced by a guess (the server may know a newer code than this page).
 */
export function reasonText(code: string | null): string {
  if (code === null || code === '') return 'No reason was given.';
  return (
    REASON_SENTENCES[code] ??
    `The mapper gave the reason code "${code}", which this page does not know.`
  );
}
