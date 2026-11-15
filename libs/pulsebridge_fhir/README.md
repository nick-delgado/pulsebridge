# pulsebridge_fhir

Builds the FHIR R4 resources PulseBridge writes: `Patient`, `Device`, heart-rate and
blood-pressure `Observation`s, `Provenance`, the transaction `Bundle`s that write them
idempotently, and the `AuditEvent` that records each cuff send attempt (T31). Pure Python, no
runtime dependencies, no network or GCP calls.

Callers pass plain values (frozen dataclasses that validate themselves) and get plain dicts back,
ready for `json.dumps`. Any bad input raises `FhirInputError` (a `ValueError`) whose message names
the input and the field.

## Who owns the Patient record

Two kinds of caller touch the Patient, and they must not fight over it. **The setup step must
run before the ingest services (`health-sync`, `v2-to-fhir`) are enabled**: it writes the one
Patient record, and ingest only links new readings to it.

| Caller | Knows | Writes the Patient with |
| --- | --- | --- |
| Setup: connect script (T11) or seeder (T09) | everything: identifier, name, gender, birth date, Google `healthUserId` | **conditional update**, `PUT`: the full record, once |
| `health-sync` (T10) | the identifier only (configuration) | **conditional create**: only if absent |
| `v2-to-fhir` (T12) | PID-3 (identifier) and PID-5 (name); no birth date or sex | **conditional create**: only if absent |
| Share to OpenEMR (T15) | reads the stored Patient | nothing in our store; sends `patient_for_sharing(stored)` |

### Conditional update and conditional create

Both find the resource by a search (here, one identifier); they differ in what happens when it is
found.

<table>
<tr><th>Conditional update (setup)</th><th>Conditional create (ingest)</th></tr>
<tr><td>

```json
"request": {
  "method": "PUT",
  "url": "Patient?identifier=urn:pulsebridge:patient|PB-SYN-0001"
}
```

</td><td>

```json
"request": {
  "method": "POST",
  "url": "Patient",
  "ifNoneExist": "identifier=urn:pulsebridge:patient|PB-SYN-0001"
}
```

</td></tr>
<tr><td>Found: <b>replace</b> the stored Patient with this one. Not found: create it.</td>
<td>Found: <b>leave it exactly as it is</b>. Not found: create it.</td></tr>
</table>

- `PUT` replaces the **whole** resource; there is no merging. If `v2-to-fhir` had sent
  `PUT Patient` with only an identifier and a name, the stored gender, birth date and Google
  identifier would have been deleted; the next `health-sync` run would then have put its own
  version back. Two writers with different knowledge would overwrite each other forever.
- So ingest Bundles use conditional create. In normal operation the setup step has already
  written the Patient, the create matches it, and ingest never changes it. The create is a
  safety net: if setup has not run, ingest writes a minimal Patient (identifier, perhaps a name)
  rather than failing, and the setup upsert later replaces it with the full record.
- `ifNoneExist` is, per R4 `Bundle.entry.request.ifNoneExist`, "just the query portion of the
  URL - what follows the '?' (not including the '?')". It is built by the same function as the
  conditional-update URL, so the escaping is identical.
- References: the Observations in the same Bundle refer to the Patient entry by its `urn:uuid`
  `fullUrl`. When the conditional create matches an existing Patient, those references are
  **expected** to resolve to it (common servers implement this; the R4 text could not be
  consulted from the build environment, because hl7.org was blocked). See the next section.

### How the API enforces it

- **`build_ingest_bundle(patient=, devices=, records=)`** writes the Patient and Devices as
  conditional creates. `records` may hold only Observations and Provenance; a Patient or Device
  there is rejected. There is no argument that makes it upsert a Patient.
- **`build_setup_bundle(patient=, devices=())`** is the only function that writes a Patient or
  Device with `PUT`, and it first calls `check_complete_patient`.
- **Requests are never taken from the caller.** Both functions derive every `request` from the
  entry's own resource (`request_for`): an ingest Patient or Device is always `POST <its type>`
  with `ifNoneExist` on its own identifier; a record's URL always names its own type and its own
  identifier (or id, for Provenance); a setup `PUT` URL always carries the Patient's own
  identifier. `BuiltResource` stores no request, so there is nothing to swap or edit, and
  anything that is not a `BuiltResource` is a `FhirInputError`.
- The builders themselves (`build_patient`, `build_device`) never choose a write mode.

### Unverified server behaviour, and the fallback

That the Cloud Healthcare API resolves `urn:uuid` references to an entry whose conditional create
**matched** an existing resource is expected (common servers implement it; the R4 text could not
be consulted) but has not been tested against the real store. The first real `executeBundle` in
T10 or T12 will show it.

Symptoms to look for:

- **References not resolved:** the transaction is rejected (referential integrity refuses a
  reference it cannot resolve), or Observations point at a reference that does not exist.
- **`412 Precondition Failed`:** a conditional create or conditional update whose search matches
  **more than one** resource is refused (R4: multiple matches fail the precondition).
- **Duplicate Patients:** if `health-sync` and `v2-to-fhir` both make first contact at the same
  moment before setup has run, each conditional create can find nothing and create its own
  Patient. From then on the identifier matches two Patients, so every ingest conditional create
  and the setup upsert fail with `412` until the duplicate is removed by hand. This is why setup
  must run before the ingest services are enabled.

Fallback, matching decision D11 (not implemented):

- **Setup** writes `PUT Patient/<uuidv5>` and `PUT Device/<uuidv5>`: a deterministic id derived
  from the identifier, written as "update as create" (needs the store's `enableUpdateCreate`,
  which Provenance already needs).
- **Ingest Bundles leave Patient and Device out** and refer to them literally,
  `"reference": "Patient/<uuidv5>"` and `"reference": "Device/<uuidv5>"`.
- This works only if setup has run before any ingest; otherwise referential integrity rejects the
  ingest Bundle. A conditional create cannot be used to place that id: R4 says that on create
  "if an id is provided, the server SHALL ignore it", so the server would pick its own id and the
  literal references would point at nothing.
- A second candidate: R4 **conditional references** inside a transaction,
  `"reference": "Patient?identifier=urn:pulsebridge:patient|<value>"`, which the server resolves by
  search when it processes the Bundle. Also unverified on Cloud Healthcare.

## Public API

| Name | What it is |
| --- | --- |
| `PatientInput(patient_id, family=None, given=(), gender=None, birth_date=None, google_health_user_id=None)` | The person. Only `patient_id` is required (configuration, or PID-3). |
| `DeviceInput(display_name, manufacturer=None)` | The measuring device. |
| `HeartRateWindow(start, end, bpm_min, bpm_avg, bpm_max)` | One roll-up window. Times timezone-aware, whole seconds. |
| `BloodPressureReading(effective, systolic, diastolic, source_key, unit="mm[Hg]")` | One cuff reading. `source_key` is its idempotency key, e.g. `"<MSH-3>/<MSH-10>"`. |
| `SourceInfo(*, record_type, record_id, platform, recording_method)` | The source record, for Provenance. `platform` and `recording_method` may be `None`. |
| `build_patient`, `build_device`, `build_heart_rate_observation`, `build_blood_pressure_observation`, `build_provenance` | Builders; each returns a `BuiltResource`. |
| `check_complete_patient(resource)` | Raises unless the Patient has a PT identifier, one official name with family and given parts, a gender and a real birth date. |
| `patient_for_sharing(stored)` | The Patient to send to OpenEMR: PT identifier, official name, gender, birth date only. |
| `build_ingest_bundle`, `build_setup_bundle` | The only two ways to make a Bundle (see above). |
| `Write` (`UPDATE`, `CREATE_IF_ABSENT`), `request_for(resource, write)` | The two write modes, and the request each implies for a resource. |
| `heart_rate_transaction(points, *, patient, device, data_source, tz, recorded, complete_before, assembler="PulseBridge health-sync")` | `health-sync`: roll-up JSON to an ingest Bundle. |
| `heart_rate_resources(windows, *, patient, device, data_source, tz, recorded, complete_before, assembler=…)` | The same, as a list of built Observations and Provenance, for a caller assembling its own ingest Bundle. |
| `blood_pressure_transaction(reading, *, patient, device, source, recorded, assembler="PulseBridge v2-to-fhir", status="final")` | `v2-to-fhir`: one cuff reading to an ingest Bundle. |
| `patient_setup_transaction(patient, *, devices=())` | Setup: the full Patient as an upsert. |
| `window_from_rollup(point)`, `parse_google_utc_offset("-18000s")`, `window_status(window, complete_before)` | Google helpers. |
| `GoogleDataSource(*, platform, recording_method)`, `rollup_source(window, data_source)` | Provenance source for a roll-up window. No defaults. |
| `heart_rate_key`, `blood_pressure_key` | The idempotency keys. |
| `SendAttempt(*, sent_at, transport, control_id, result, patient_id, agent, destination, message_name=None, sample_name=None)` | One cuff send attempt (T31). `result` is `Acknowledged(*, code, raw_ack, reason=None)` or `NotAcknowledged(*, why, http_status=None)`. |
| `build_send_attempt_audit_event(attempt, *, recorded)` | The AuditEvent for one attempt; a `BuiltResource`. |
| `audit_event_write(built)` | The single-resource write: `{method: "PUT", url: "AuditEvent/<id>", content_type, body}`. AuditEvent only. |
| `parse_send_attempt(resource)` | An AuditEvent read back from the store, as a `SendAttemptRecord`. |
| `send_attempt_search_params(*, count=50, since=None, before=None)` | Query parameters to list attempts newest first. |
| `send_attempt_key(attempt)`, `sanitise_reason(text)` | The attempt's id; the MSA-3 cleaning rule. |

A `BuiltResource` has `.resource` (the dict), `.full_url` (`urn:uuid:…`), `.key` and
`.reference()`. `.update` (the `PUT` request) and `.create_if_absent` (the conditional create,
Patient and Device only) are computed from `.resource` each time they are read.

### Setup: connect script (T11) and seeder (T09)

```python
bundle = patient_setup_transaction(
    PatientInput(configured_patient_id, family=family, given=(given,), gender=gender,
                 birth_date=birth_date, google_health_user_id=health_user_id),
    devices=[DeviceInput(google_display_name)],   # or devices=() to let health-sync create it
)
```

The Device key is derived from the patient id **and the display name**. If setup writes the watch,
it must use exactly the `dataSource.device.displayName` Google reports, the same value
`health-sync` will pass; a different spelling gives a second watch Device. If setup cannot read
that name, pass no devices and let `health-sync` create the Device on first sync.

### `health-sync` (T10)

```python
bundle = heart_rate_transaction(
    rollup_items,                                   # Google roll-up items
    patient=PatientInput(configured_patient_id),    # identifier only
    device=DeviceInput(device_display_name),
    data_source=GoogleDataSource(platform=None, recording_method=None),  # or what a listed point says
    tz=ZoneInfo(configured_time_zone),              # IANA zone, e.g. "America/New_York"
    recorded=datetime.now(UTC),
    complete_before=start_of_next_overlap,          # windows ending later are "preliminary"
)
```

### `v2-to-fhir` (T12), from PID-3 and PID-5 only

```python
family, given = pid5_1_family_name, pid5_2_given_name      # either may be empty
bundle = blood_pressure_transaction(
    BloodPressureReading(obx14_time, systolic, diastolic, source_key=f"{msh3}/{msh10}", unit=obx6),
    patient=PatientInput(pid3_1_id, family=family or None, given=(given,) if given else ()),
    device=DeviceInput(sending_application),
    source=SourceInfo(record_type="hl7v2-message", record_id=store_message_name,
                      platform="HL7V2_STORE", recording_method=None),  # nothing asserted when unknown
    recorded=datetime.now(UTC),
)
```

- The identifier is **PID-3.1 only** (CX.1, the ID number; not the assigning authority in CX.4),
  and it must equal the configured `patient_id` the setup step used, or the reading lands on a
  different Patient.
- An empty PID-5.1 or PID-5.2 must not fail a valid message: pass `None` and `()` as above. The
  library accepts an identifier with a partial or absent name for ingest; it rejects an empty
  string, so the conversion is the caller's.
- `recording_method=None`: the ORU^R01 message does not say how the reading was taken, so the
  Provenance asserts nothing about it.

A `FhirInputError` from any of these is the application-level `error` reason (D7).

### Share to OpenEMR (T15)

Read the Patient from the store and send `patient_for_sharing(stored)`, never the stored record
itself. The stored Patient carries the Google Health user id (an identifier at another company,
which the clinic has no need for: minimum necessary) plus our store's `id` and `meta`.
`patient_for_sharing` runs `check_complete_patient` (a clear error if setup has not run) and
returns only the `PT` identifier, the official name, gender and birth date: the shape OpenEMR
needs (spike S1).

Share only Observations with `status: final`. A `preliminary` window can still change in our
store, and the copy in OpenEMR would not be updated. Before `POST`ing an Observation, replace
`subject` with OpenEMR's Patient, add `encounter`, and drop `device`, `meta` and `identifier`
(OpenEMR discards them). Units are already exactly `/min` and `mm[Hg]`.

## Design decisions

### Write strategy

| Resource | Ingest Bundle | Setup Bundle | Key |
| --- | --- | --- | --- |
| Patient | `POST Patient`, `ifNoneExist: identifier=urn:pulsebridge:patient\|<id>` | `PUT Patient?identifier=…` | the configured patient id |
| Device | `POST Device`, `ifNoneExist: identifier=urn:pulsebridge:device\|<uuid>` | `PUT Device?identifier=…` | UUIDv5(patient, display name) |
| Observation (HR) | `PUT Observation?identifier=urn:pulsebridge:observation\|<uuid>` | not written | UUIDv5(patient, `8867-4`, window start UTC) |
| Observation (BP) | same | not written | UUIDv5(patient, `85354-9`, source key) |
| Provenance | `PUT Provenance/<uuid>` | not written | UUIDv5(the Observation's key) |
| AuditEvent | not written (refused as a record) | not written | written alone, `PUT AuditEvent/<uuid>`: UUIDv5(sender, transport, MSH-10, sent time) |

- Observations are conditional updates on purpose: a window that was only partly synced has new
  values and must replace the old ones. The key leaves out the window end and the values.
- Keys are UUIDv5 over a canonical JSON list of the parts (NFC-normalised), so they are URL-safe,
  cannot collide by concatenation, and do not depend on how a name was encoded. Google's data
  points have no `name` to key on (spike S3). Keys are pinned by a test: changing how they are
  derived would make every stored Observation unreachable and the next sync would duplicate them.
- **Provenance has no `identifier` element in R4**, and the Cloud Healthcare API's conditional
  update matches on identifier only. So Provenance is written by a deterministic id
  (`PUT Provenance/<uuid>`, "update as create"). **This needs the FHIR store's
  `enableUpdateCreate = true` (task T05).**

### Identifiers

- Systems are URNs owned by this software (`urn:pulsebridge:…`, all in `codes.py`): the same in
  every clone, so nothing identifies the author.
- `Patient.identifier[0]`: `use: official`, `type` v2-0203 `PT` (Patient external identifier).
  The matching key, and what T15 searches OpenEMR by.
- `Patient.identifier[1]`: Google's `healthUserId`, `use: secondary`, written by setup only.
- `Observation.identifier`: `use: official`, the deterministic key.
- `Device.identifier`: derived (UUIDv5 of patient and display name), because Google sends only a
  display name (spike S3).

### Times and time zones

- Heart rate uses **`effectivePeriod`** (the value summarises an interval); blood pressure uses
  **`effectiveDateTime`** (one moment).
- Google sends UTC (`…Z`) in whole seconds; sub-second bounds are rejected so two windows cannot
  share a key. The builder writes times in the user's zone: `14:05:00Z` becomes
  `2026-01-01T09:05:00-05:00` in New York in winter. The key uses the UTC instant.
- **Use an IANA zone** (`zoneinfo.ZoneInfo("America/New_York")`) from configuration. Roll-up items
  carry no `utcOffset`, and one `tz` applies to every window in a call, so a fixed offset is
  correct only for a range that does not cross a daylight-saving change; across one, local times
  on the far side are an hour off (a test shows both).
- FHIR `Period` bounds are inclusive; Google's windows are `[start, end)`. Google's `end` is kept,
  so adjacent windows share a boundary instant. Treat `end` as exclusive.

### Status: windows still filling

R4 `observation-status`:

- `preliminary`: "This is an initial or interim observation: data may be incomplete or unverified."
- `final`: "The observation is complete and there are no further actions needed."

A five-minute window can still gain samples after a sync (the watch uploads late), which is why
`health-sync` re-requests an overlap. `heart_rate_transaction` takes `complete_before` (required;
pass `None` only to declare every window complete): a window whose end is after that instant is
written `preliminary`. T10 should pass the start of the overlap it will re-request on its next
run; that run rewrites the same Observation (same key) as `final` once the window is complete.

### Heart rate: value, components and the LOINC codes

- `code`: LOINC `8867-4` Heart rate (fixed by the R4 heartrate profile).
- `valueQuantity`: the window **average**, `/min` (UCUM). `method.text` says it is a mean.
- `component`: minimum and maximum, each `/min`.

| Statistic | LOINC | Long common name |
| --- | --- | --- |
| minimum | `96935-2` | Heart rate unspecified time minimum by Pedometer |
| maximum | `55426-1` | Heart rate unspecified time maximum by Pedometer |

Why these: LOINC has no minimum/maximum variant of `8867-4`. The timed codes (`8873-2` Heart rate
24 hour maximum, `8883-1` 24 hour minimum, and the 1, 8, 10 and 12 hour codes) fix a duration in
their TIME axis and would misstate a five-minute window. These use TIME `XXX^max` / `XXX^min`:
"the interval is given elsewhere", here `effectivePeriod`. The task review corroborated both codes
through several independent secondary sources, including an HL7 implementation guide that uses
`55426-1` for peak heart rate, and found nothing better in LOINC. A third candidate, `101692-2`
("Maximum HR"), could not be confirmed and has no minimum counterpart.

Two caveats:

1. **Their METHOD axis is "Pedometer"**: they belong to LOINC's activity-tracker family (panel
   `55413-9`). That fits a wrist wearable, but it is a method claim the base `8867-4` does not make.
2. **loinc.org itself could not be consulted** from the build environment; the codes were confirmed
   only through secondary listings. Re-check them on loinc.org.

### Blood pressure

Panel `85354-9` with components `8480-6` (systolic) and `8462-4` (diastolic), each `mm[Hg]`, and
no top-level value: the shape the R4 bp profile requires. `unit` must be exactly `mm[Hg]`.

### Provenance

| Element | Value | Why |
| --- | --- | --- |
| `target` | the Observation | what this Provenance is about |
| `recorded` | when PulseBridge wrote it | passed in by the caller, keeping builders deterministic |
| `activity` | v3 DataOperation `CREATE` | v3 defines CREATE as "the act of bringing an object into existence": here, the observation data. The device measured it and PulseBridge assembled it into the FHIR record. `ProvenanceActivityType` (the value set R4 binds here) includes DataOperation |
| `agent` type `performer` | the Device; its `role` carries the recording method (`PASSIVELY_MEASURED`) when the source states one | R4: the performer "actually and principally carries out the activity"; the device took the measurement. `agent.role` is "the function of the agent with respect to the activity": how the device measured. Its binding is only an example, so a local code is allowed |
| `agent` type `custodian` | source platform by identifier (`FITBIT`, `HL7V2_STORE`) | holds the original record. **Left out when the caller does not know it**: the roll-up response has no `dataSource`, so nothing is asserted by default |
| `agent` type `assembler` | the PulseBridge service, by display | the software that derived the FHIR resource |
| `entity` role `source` | the source record by identifier (HL7v2 store message name, roll-up window) | lets the trace panel find the original |

The recording method used to sit in `activity`. That was wrong: how a value was recorded
(passively by a watch, or by a person starting a cuff reading) is not the activity this Provenance
describes. Considered and rejected for it: an extra coding in `activity` (codings in one
CodeableConcept must mean the same thing), and `Observation.method` (a good fit, but the
acceptance criterion places the recording method in Provenance, and `method` already says the
value is a mean).

### Category

`category` carries `http://terminology.hl7.org/CodeSystem/observation-category#vital-signs`: the
vital-signs profile requires it (slice VSCat), and OpenEMR routes on it.

### Input checks

- Heart rate 1 to 300 per minute; min ≤ average ≤ max; cuff values in plausible ranges; unit
  exactly `mm[Hg]`.
- A heart rate sent as a string must be plain ASCII digits with an optional decimal part: `"1_00"`,
  `"+72"`, `"7e1"` and Arabic-Indic digits are rejected (Python's `int()` would accept them). Times
  and offsets are matched with `[0-9]`, not `\d`, for the same reason.
- A huge JSON integer (10^400) is a range error, never an `OverflowError` or `ValueError`.
- Text is normalised to Unicode NFC.

### Conformance to the R4 Vital Signs profiles

Checked against the R4 (4.0.1) StructureDefinitions `vitalsigns`, `heartrate` and `bp`.

| Rule | Status |
| --- | --- |
| status, category VSCat, code, subject, effective[x] (dateTime or Period) | followed |
| heartrate: code `8867-4`; `valueQuantity` value, unit, UCUM system, code `/min` | followed |
| bp: code `85354-9`; no `valueQuantity`; one `8480-6` and one `8462-4` component in `mm[Hg]` | followed |
| vs-2 (value or component), vs-3 (component value or dataAbsentReason) | followed |
| `component.value[x]` bound **required** to ucum-vitals-common | followed (`/min`, `mm[Hg]`) |
| `component.code` bound **extensible** to observation-vitalsignresult | **deliberate divergence** for the heart-rate min/max components: that 13-code value set has no min/max codes, which is when an extensible binding allows another code. Expect a validator warning. |
| `urn:pulsebridge:*` code systems | local, undefined; the validator may report it cannot check them. |

## AuditEvent for a cuff send attempt (T31)

### What it is, and how it differs from Provenance

An **AuditEvent** is FHIR's record of something that happened in or between systems: who did
what to which thing, when, and whether it worked. It was designed to carry the same content as a
DICOM / IHE ATNA audit message, which is why most of its codes come from DICOM.

A **Provenance** (which T06 already writes) is about one resource: where an Observation came
from and who made it. It is written in the same transaction as the Observation, and only when a
message was mapped. A rejected message or one that never got an answer produces no Observation,
so there is nothing for a Provenance to describe. The AuditEvent describes the **send** itself,
whatever happened to it (decision D12). The HL7v2 store keeps neither rejected messages nor ACK
bytes (spikes S4 and S5), so this record is the only place they survive.

### The model

| Element | Value | Why |
| --- | --- | --- |
| `type` | DICOM `110106` Export | "Data has been exported out of a system": the simulator sends a reading out to the HL7v2 store. |
| `subtype` | v2-0003 `R01` ORU/ACK | The HL7v2 trigger event attempted. A malformed sample is still an attempt at this transaction. |
| `action` | `R` (read) | DICOM PS3.15 A.5.3.4 (Data Export) and IHE (for example the ITI-41 source audit) fix an Export's action to R: the sender reads what it sends. The receiver would record its own Import (`110107`) with action C. |
| `period` | `start`: sent; `end`: recorded | When the activity happened: the send, then the wait for the answer. |
| `recorded` | when the attempt was recorded | R4 requires it. Passed in by the caller, so the builder stays deterministic. |
| `outcome` | `0` / `4` / `8` | See the mapping table below. |
| `outcomeDesc` | fixed wording, plus the cleaned MSA-3 reason | Only for a failure, or an accept that carried MSA-3 text. No caller text enters it. |
| `agent[0]` | DICOM `110153` Source Role ID, `who` = `urn:pulsebridge:service\|cuff-sim`, `requestor: true` | The sender, which started the activity. |
| `agent[1]` | DICOM `110152` Destination Role ID, `who` = `urn:pulsebridge:hl7v2-store\|<store name>`, `requestor: false` | The receiver. Over MLLP it is reached through the adapter, but the store is the destination. |
| `source.observer` | `urn:pulsebridge:service\|cuff-sim`; `source.type` security-source-type `4` Application Server | R4 requires an observer: the system that reports the event, here the simulator itself (a Cloud Run service). |
| `entity[0]` | the patient: `what` = `{type: Patient, identifier: urn:pulsebridge:patient\|<id>}`; `type` audit-entity-type `1` Person; `role` object-role `1` Patient | What the reading was about. **The patient is not an agent**: they took no part in sending it. |
| `entity[1]` | the message: `what` = `urn:pulsebridge:source-record:hl7v2-message\|<stored name>` when stored; `type` `2` System Object; `role` `3` Report; `detail` (below) | An ORU^R01 is an unsolicited observation report. Its identifier system is the one T06's Provenance uses for the same message, so the trace panel can join the two. |

Not used: `entity.lifecycle` (R4's DICOM lifecycle code system is marked "not currently used",
and the element is optional) and `purposeOfEvent`.

`entity[1].detail` is a list of name/value pairs. In R4 `detail.type` is a plain string, so no
code system is involved. HL7v2 field names are used where the value is an HL7v2 field, as IHE's
audit messages for HL7v2 transactions do for MSH-10.

| `detail.type` | Value | Present when |
| --- | --- | --- |
| `transport` | `mllp` or `https` | always |
| `MSH-10` | the message control ID | the message had one (a "no MSH" sample has none) |
| `MSA-1` | `AA`, `AE`, `AR`, `CA`, `CE`, `CR` | an ACK came back |
| `MSA-3` | the cleaned reason | the ACK carried one |
| `ack-message` | the raw ACK, `valueBase64Binary` | an ACK came back |
| `no-ack-reason` | `timeout`, `connection-closed`, `api-error` | no ACK came back |
| `http-status` | for example `"503"` | `api-error` only |
| `malformed-sample` | the T07 sample name, for example `unparseable_no_msh` | "send malformed" was used |

### Code systems

| System | Codes used | Kind |
| --- | --- | --- |
| `http://dicom.nema.org/resources/ontology/DCM` | `110106` Export, `110153` Source Role ID, `110152` Destination Role ID | standard (DICOM PS3.16); in R4's AuditEventID and ParticipationRoleType value sets |
| `http://terminology.hl7.org/CodeSystem/v2-0003` | `R01` | standard (HL7v2 table 0003) |
| `http://hl7.org/fhir/audit-event-action` | `R` (the `action` code) | standard, required binding |
| `http://hl7.org/fhir/audit-event-outcome` | `0`, `4`, `8` (the `outcome` code) | standard, required binding |
| `http://terminology.hl7.org/CodeSystem/security-source-type` | `4` Application Server | standard |
| `http://terminology.hl7.org/CodeSystem/audit-entity-type` | `1` Person, `2` System Object | standard |
| `http://terminology.hl7.org/CodeSystem/object-role` | `1` Patient, `3` Report | standard |
| `urn:pulsebridge:service` | identifier system for a PulseBridge service name (`cuff-sim`) | local: our software has no standard identifier |
| `urn:pulsebridge:hl7v2-store` | identifier system for a Cloud Healthcare HL7v2 store name | local |
| `urn:pulsebridge:source-record:hl7v2-message` | identifier system for a stored message name (shared with Provenance) | local, from T06 |
| `urn:pulsebridge:patient` | the patient identifier (from T06) | local, from T06 |

The code meanings were checked through web search against DICOM PS3.15 and PS3.16, IHE audit
definitions and published FHIR value set listings; hl7.org itself was not reachable from the build
environment. The examples parse with an R4 (4.0.1) model library (`fhir.resources` 6.4.0, used in
a scratch environment only, not a dependency). The HL7 validator (T08) is the definitive check.

### Results and `outcome`

R4 `audit-event-outcome`: `0` Success; `4` Minor failure, "often equivalent to an HTTP 400
response"; `8` Serious failure, "some kind of unexpected error (often equivalent to an HTTP 500
response)"; `12` Major failure, "the system is no longer available for use".

| Result | Input | `outcome` | Why |
| --- | --- | --- | --- |
| accepted | `Acknowledged(code="AA")` or `CA` | `0` | The store took the message (commit-level accept, D7). |
| rejected | `Acknowledged(code="AE")`, `AR`, `CE`, `CR` | `4` | The receiver answered and refused the message itself, like an HTTP 400. S4 found the store only returns AE; AR is treated the same way so the mapping depends only on accept or refuse. |
| no acknowledgment | `NotAcknowledged(why="timeout" \| "connection-closed" \| "api-error")` | `8` | No answer at all: the delivery path failed, like an HTTP 500, and the message's fate is unknown. |

`12` is never used. A timeout or a closed connection does not show that the store is down, only
that this attempt got no answer.

The AuditEvent records only the **commit-level** acknowledgment. Whether `v2-to-fhir` could map
an accepted message (the application-level outcome) is in the stored message's labels (D7).

### Inputs and validation

- `SendAttempt` takes plain values only, with no HL7v2 or Google types. Exactly one result shape:
  `Acknowledged` or `NotAcknowledged`. There is no field for the message itself.
- `sent_at` and `recorded`: time-zone aware, **whole milliseconds**, and `recorded` must not be
  before `sent_at`. This is milliseconds, not whole seconds as for heart-rate windows, because a
  resend can follow within the same second and the key includes the send time.
- `transport`: `mllp` or `https`. `api-error` happens only over `https` and always has an
  `http_status` of 400 to 599. The status is allowed with nothing else.
- `control_id` (MSH-10): 1 to 64 of `A-Za-z0-9._-` (T07 builds 1 to 20 of them). It may be `None`
  only for a malformed sample.
- `raw_ack`: 1 to 4096 bytes, the ACK message without the MLLP frame (no `0x0B`, no `0x1C`),
  starting with `MSH`. This library does not parse HL7v2: `code` and `reason` come from T07's
  ACK parser.
- `message_name`: only with an accepting ACK (a rejected message is not stored), and it must be
  `<destination>/messages/<id>`.
- `destination`: `projects/<p>/locations/<l>/datasets/<d>/hl7V2Stores/<s>`. `agent`: a Cloud
  Run-style service name. `sample_name`: a T07 sample name. `patient_id`: the same rule as
  `PatientInput`, so the reference always matches the Patient T06 writes.

**The MSA-3 reason is cleaned, not rejected** (`sanitise_reason`). The text comes from the store,
not from a person. Rejecting it would lose the audit record of a real attempt, and the raw ACK
keeps the original bytes anyway. The steps:

1. normalise to NFC;
2. replace every control, format, private-use, surrogate or unassigned character (Unicode `C*`)
   and every line or paragraph separator with U+FFFD, so a CR cannot forge a log line and a
   bidirectional override cannot disguise the text, and the reader can see that something was
   replaced;
3. trim the ends;
4. cut text over 200 characters to 199 plus an ellipsis. MSA-3 is 80 characters in v2.5.1, so a
   conforming reason is never cut.

Applying it twice gives the same result, so a stored reason validates to itself on read. The
reason is stored as the caller passes it. T07's `parse_ack` decodes HL7v2 escapes (`\E\` becomes
`\`), and the raw ACK keeps them as sent.

### Privacy

- The raw HL7v2 **message** is never stored. An accepted message is in the HL7v2 store, and a
  malformed one is a fixed synthetic sample identified by name. Only the raw **ACK** is kept,
  which is MSH and MSA with no patient data.
- The patient appears by identifier only: no name, no display.

### References by identifier

The patient, the sender and the store are **logical references**: a `Reference` with an
`identifier` and no `reference` (no `Patient/<id>`). R4 allows a Reference to carry only an
identifier when the target is known by identifier rather than by server id.

This suits an AuditEvent written **outside a transaction Bundle**:

- There is no Bundle, so there is no `urn:uuid` to point at.
- D11 says ingest never assumes the Patient's server id.
- The simulator writes the AuditEvent before, and whether or not, `v2-to-fhir` has created
  anything.
- The sender and the store are not FHIR resources at all.

A logical reference names the patient the same way every other PulseBridge resource does
(`urn:pulsebridge:patient|<id>`) and never dangles. The cost: the server does not resolve it, so
the R4 `patient` and `agent` reference searches will not find these events (see Listing).

### Idempotency

R4 AuditEvent has no `identifier` element, and Cloud Healthcare's conditional update matches on
identifier only. So, as for Provenance, the AuditEvent is written by a deterministic id:
`PUT AuditEvent/<uuid>` ("update as create", which needs the store's `enableUpdateCreate`, T05).

The id is UUIDv5 of `["AuditEvent", "cuff-send-attempt", <agent>, <transport>, <MSH-10 or "">,
<sent time, UTC, milliseconds>]`:

- **Sent time**: the same control ID can legitimately be sent twice (a resend reuses MSH-10), and
  those are two attempts with two AuditEvents.
- **Agent and transport** keep two simulators, or an MLLP and an HTTPS send at the same
  millisecond, apart.
- **Not the result, the destination or `recorded`**: they are what is recorded about the
  attempt. Writing the same attempt again (a retried write) replaces the same AuditEvent.

The three example ids are pinned by a test.

`audit_event_write` derives method and URL from the resource with `request_for`, like every other
request in this library. It refuses anything but an AuditEvent, so it cannot write a Patient or
Device outside the two bundle functions. `build_ingest_bundle` still refuses an AuditEvent as a
record.

### Usage: cuff simulator (T13), recording an attempt

```python
from pulsebridge_fhir import (Acknowledged, NotAcknowledged, SendAttempt, FhirInputError,
                              audit_event_write, build_send_attempt_audit_event)

if ack is not None:   # T07 parse_ack(payload): the frame payload, or base64-decoded hl7Ack/hl7Nack
    result = Acknowledged(code=ack.code, raw_ack=ack.raw, reason=ack.reason)
else:                 # timeout, connection closed, or an HTTPS error with no hl7Nack
    result = NotAcknowledged(why=why, http_status=status if why == "api-error" else None)
attempt = SendAttempt(
    sent_at=sent_at, transport=transport, control_id=control_id, result=result,
    patient_id=configured_patient_id, agent="cuff-sim", destination=configured_hl7v2_store,
    message_name=ingest_response_name,          # HTTPS 200 only; None otherwise
    sample_name=malformed_sample_name,          # None unless "send malformed"
)
write = audit_event_write(build_send_attempt_audit_event(attempt, recorded=datetime.now(UTC)))
# PUT {fhir_base}/{write["url"]}, Content-Type: write["content_type"], body json.dumps(write["body"])
```

Truncate `datetime.now(UTC)` and `sent_at` to milliseconds
(`.replace(microsecond=t.microsecond // 1000 * 1000)`). A `FhirInputError` or a failed write is
logged and reported, and never hides the send's result (T13 AC).

### Usage: FHIR proxy (T14), the messages log

```python
from urllib.parse import urlencode
from pulsebridge_fhir import parse_send_attempt, send_attempt_search_params, FhirInputError

url = f"{fhir_base}/AuditEvent?{urlencode(send_attempt_search_params(count=50, since=since))}"
for entry in bundle.get("entry", []):
    try:
        row = parse_send_attempt(entry["resource"])
    except FhirInputError:
        continue                                # not one of ours; skip it, log at debug
    row.sent_at, row.transport, row.control_id, row.result          # accepted / rejected / no-acknowledgment
    row.ack_code, row.reason, row.raw_ack, row.no_ack_reason, row.http_status
    row.message_name                            # join to the stored message and its labels
```

`parse_send_attempt` tolerates what a server adds on read (`id`, `meta`, `text`) and a time
written in another valid form (`Z` for `+00:00`). Otherwise the values are read out, validated as
a `SendAttempt`, built again, and the result must equal the given resource. Anything else, or
an id that does not match, is a `FhirInputError`.

### Listing

`send_attempt_search_params()` returns `type=http://dicom.nema.org/resources/ontology/DCM|110106`,
`subtype=http://terminology.hl7.org/CodeSystem/v2-0003|R01`, optionally `date=ge…` and
`date=lt…` (UTC, `Z`), `_sort=-date` and `_count`. All are standard R4 AuditEvent search
parameters: `type` and `subtype` are tokens, and `date` searches `AuditEvent.recorded`. **Cloud
Healthcare's support for each of them on AuditEvent is unverified until the first real call in
T14.**

`date` sorts by `recorded`, not by the send time. One simulator sends one message at a time, so
the order is the same. `outcome` (token) is also a standard parameter if a filter is wanted. The
`patient` and `agent` reference searches do not apply, because those references are logical.

## Examples for the validator (T08)

`examples/` holds one file per resource type plus three transaction Bundles, all synthetic:
`bundle-patient-setup.json` (the Patient upsert), `bundle-heart-rate-ingest.json` and
`bundle-blood-pressure-ingest.json` (conditional creates; the latter built from PID-3 and PID-5
only, and the former with one `preliminary` window). Three AuditEvents (T31) cover each send
result: `auditevent-send-accepted.json` (HTTPS, `AA`, stored message name),
`auditevent-send-rejected.json` (MLLP, the `unparseable_no_msh` sample, `AE` with reason) and
`auditevent-send-no-ack.json` (MLLP, the `noack_missing_end_block` sample, timeout). That makes
twelve files. The store name in them uses the placeholder project `example-project`. Expected
validator warnings, beyond those listed above: the local `urn:pulsebridge:*` identifier systems.
They are generated, never edited by hand:

```bash
.venv/bin/python -m pulsebridge_fhir.examples   # rewrites libs/pulsebridge_fhir/examples/
```

A test fails if the committed files differ from what the code produces.

## Tests

`make test` runs `tests/`. All data is synthetic. Each behaviour was break-checked: the
implementation was deliberately broken and a test seen to fail.
