/**
 * Minimal, hand-written FHIR R4 types: only the elements the dashboard reads or shows.
 *
 * Why not a typings package such as `@types/fhir`: it describes all ~150 resource types and every
 * element, which is a lot to load for a page that touches eight of them, and its very wide
 * optional types make the code no safer. These interfaces follow the R4 element names exactly
 * (https://hl7.org/fhir/R4/), so a later switch to a full package is a find-and-replace.
 * Each interface is open-ended on purpose: a resource from the server can carry more elements
 * than listed, and the trace panels show the whole resource as JSON.
 */

/** R4 datatype Coding: one code from one code system. */
export interface Coding {
  system?: string;
  code?: string;
  display?: string;
}

/** R4 datatype CodeableConcept: one concept, possibly coded in several systems, plus text. */
export interface CodeableConcept {
  coding?: Coding[];
  text?: string;
}

/** R4 datatype Reference, for example `{ "reference": "Patient/123" }`. */
export interface Reference {
  reference?: string;
  /** Logical reference by identifier, for a target with no resolvable URL (R4 Reference.identifier). */
  identifier?: Identifier;
  /** The target's resource type, for example "Device" (R4 Reference.type). */
  type?: string;
  display?: string;
}

/** R4 datatype Quantity. `system` + `code` carry the UCUM unit; `unit` is display text. */
export interface Quantity {
  value?: number;
  unit?: string;
  system?: string;
  code?: string;
}

export interface Period {
  start?: string;
  end?: string;
}

export interface Identifier {
  use?: string;
  type?: CodeableConcept;
  system?: string;
  value?: string;
}

export interface Meta {
  versionId?: string;
  lastUpdated?: string;
  profile?: string[];
}

export interface Resource {
  resourceType: string;
  id?: string;
  meta?: Meta;
  [element: string]: unknown;
}

export interface ObservationComponent {
  code: CodeableConcept;
  valueQuantity?: Quantity;
}

/** R4 Observation, as written by T06's builders (heart rate and the blood-pressure panel). */
export interface Observation extends Resource {
  resourceType: 'Observation';
  identifier?: Identifier[];
  /** R4 ObservationStatus (required binding): all eight codes. */
  status:
    | 'registered'
    | 'preliminary'
    | 'final'
    | 'amended'
    | 'corrected'
    | 'cancelled'
    | 'entered-in-error'
    | 'unknown';
  category?: CodeableConcept[];
  code: CodeableConcept;
  subject?: Reference;
  encounter?: Reference;
  effectiveDateTime?: string;
  effectivePeriod?: Period;
  valueQuantity?: Quantity;
  method?: CodeableConcept;
  device?: Reference;
  component?: ObservationComponent[];
}

export interface Patient extends Resource {
  resourceType: 'Patient';
  identifier?: Identifier[];
  name?: { use?: string; family?: string; given?: string[] }[];
  gender?: string;
  birthDate?: string;
}

export interface Encounter extends Resource {
  resourceType: 'Encounter';
  status: string;
  class?: Coding;
  subject?: Reference;
  period?: Period;
}

export interface Device extends Resource {
  resourceType: 'Device';
  identifier?: Identifier[];
  deviceName?: { name: string; type: string }[];
}

export interface Provenance extends Resource {
  resourceType: 'Provenance';
  target: Reference[];
  recorded: string;
  agent: { type?: CodeableConcept; who: Reference }[];
  entity?: { role: string; what: Reference }[];
}

/** R4 AuditEvent: one per cuff send attempt (D12), built by T31. */
export interface AuditEvent extends Resource {
  resourceType: 'AuditEvent';
  type: Coding;
  subtype?: Coding[];
  action?: string;
  recorded: string;
  /** R4 outcome codes: 0 success, 4 minor failure, 8 serious failure, 12 major failure. */
  outcome?: '0' | '4' | '8' | '12';
  outcomeDesc?: string;
  agent: { requestor: boolean; who?: Reference; type?: CodeableConcept }[];
  source: { observer: Reference; type?: Coding[] };
  entity?: {
    what?: Reference;
    type?: Coding;
    role?: Coding;
    /** R4 AuditEvent.entity.detail: value[x] is a string or base64Binary (raw ACK bytes, D12). */
    detail?: { type: string; valueString?: string; valueBase64Binary?: string }[];
  }[];
}

/** R4 OperationOutcome: the standard FHIR error body. */
export interface OperationOutcome extends Resource {
  resourceType: 'OperationOutcome';
  issue: {
    severity: 'fatal' | 'error' | 'warning' | 'information';
    code: string;
    diagnostics?: string;
    details?: CodeableConcept;
  }[];
}

export interface BundleEntry<T extends Resource = Resource> {
  fullUrl?: string;
  resource?: T;
}

/** R4 Bundle. A search returns `searchset`; trace and read-back bodies use `collection`. */
export interface Bundle<T extends Resource = Resource> extends Resource {
  resourceType: 'Bundle';
  type: 'searchset' | 'collection' | 'transaction' | 'transaction-response';
  total?: number;
  link?: { relation: string; url: string }[];
  entry?: BundleEntry<T>[];
}

/** LOINC codes the dashboard queries (system http://loinc.org). */
export const LOINC = {
  heartRate: '8867-4',
  bloodPressurePanel: '85354-9',
  systolic: '8480-6',
  diastolic: '8462-4',
} as const;

export type LoincCode = (typeof LOINC)[keyof typeof LOINC];

export const LOINC_SYSTEM = 'http://loinc.org';
export const UCUM_SYSTEM = 'http://unitsofmeasure.org';

/** True when a value has the shape of an R4 OperationOutcome. */
export function isOperationOutcome(value: unknown): value is OperationOutcome {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { resourceType?: unknown }).resourceType === 'OperationOutcome' &&
    Array.isArray((value as { issue?: unknown }).issue)
  );
}

/** The resources in a Bundle, in order, skipping entries without one. */
export function bundleResources<T extends Resource>(bundle: Bundle<T>): T[] {
  return (bundle.entry ?? []).flatMap((e) => (e.resource ? [e.resource] : []));
}
