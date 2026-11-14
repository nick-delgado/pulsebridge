# pulsebridge_fhir

Builds the FHIR R4 resources PulseBridge writes: `Patient`, `Device`, heart-rate and
blood-pressure `Observation`s, `Provenance`, and the transaction `Bundle`s that write them
idempotently. Pure Python, no runtime dependencies, no network or GCP calls.

Callers pass plain values (frozen dataclasses that validate themselves) and get plain dicts back,
ready for `json.dumps`. Any bad input raises `FhirInputError` (a `ValueError`) whose message names
the input and the field.

## Who owns the Patient record

Two kinds of caller touch the Patient, and they must not fight over it.

| Caller | Knows | Writes the Patient with |
| --- | --- | --- |
| Setup: connect script (T11) or seeder (T09) | everything: identifier, name, gender, birth date, Google `healthUserId` | **conditional update**, `PUT`: the full record, once |
| `health-sync` (T10) | the identifier only (configuration) | **conditional create**: only if absent |
| `v2-to-fhir` (T12) | PID-3 (identifier) and PID-5 (name); no birth date or sex | **conditional create**: only if absent |
| Share to OpenEMR (T15) | reads the stored Patient | nothing in our store; checks it is complete before sending |

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
- So ingest Bundles use conditional create: the Patient is created on first contact (a minimal
  record), and once the setup step has written the full record, ingest never changes it.
- `ifNoneExist` is, per R4 `Bundle.entry.request.ifNoneExist`, "just the query portion of the
  URL - what follows the '?' (not including the '?')". It is built by the same function as the
  conditional-update URL, so the escaping is identical.
- References: the Observations in the same Bundle refer to the Patient entry by its `urn:uuid`
  `fullUrl`. The R4 transaction rules (http.html, "Transaction Processing Rules") have references
  to an entry whose conditional create matched resolve to the existing resource. That page could
  not be read from the build environment (hl7.org was blocked); see the next section.

### How the API enforces it

- **`build_ingest_bundle(patient=, devices=, records=)`** writes the Patient and Devices as
  conditional creates. `records` may hold only Observations and Provenance; a Patient or Device
  there is rejected. There is no argument that makes it upsert a Patient.
- **`build_setup_bundle(patient=, devices=())`** is the only function that writes a Patient or
  Device with `PUT`, and it first calls `check_complete_patient`.
- The builders themselves (`build_patient`, `build_device`) never choose a write mode.

### Unverified server behaviour, and the fallback

That the Cloud Healthcare API resolves `urn:uuid` references to an entry whose conditional create
**matched** an existing resource is the R4 rule but has not been tested against the real store;
it will be by the first real `executeBundle` in T10 or T12.

- **Symptom if it does not:** the transaction fails (referential integrity rejects a reference it
  cannot resolve), or Observations point at a reference that does not exist.
- **Fallback (not implemented):** give Patient and Device deterministic ids (UUIDv5 of the
  identifier), write them as `PUT Patient/<id>` for setup and conditional create with that id for
  ingest, and make every other entry refer to the literal `Patient/<id>` instead of `urn:uuid`.

## Public API

| Name | What it is |
| --- | --- |
| `PatientInput(patient_id, family=None, given=(), gender=None, birth_date=None, google_health_user_id=None)` | The person. Only `patient_id` is required (configuration, or PID-3). |
| `DeviceInput(display_name, manufacturer=None)` | The measuring device. |
| `HeartRateWindow(start, end, bpm_min, bpm_avg, bpm_max)` | One roll-up window. Times timezone-aware, whole seconds. |
| `BloodPressureReading(effective, systolic, diastolic, source_key, unit="mm[Hg]")` | One cuff reading. `source_key` is its idempotency key, e.g. `"<MSH-3>/<MSH-10>"`. |
| `SourceInfo(*, record_type, record_id, platform, recording_method)` | The source record, for Provenance. `platform` and `recording_method` may be `None`. |
| `build_patient`, `build_device`, `build_heart_rate_observation`, `build_blood_pressure_observation`, `build_provenance` | Builders; each returns a `BuiltResource`. |
| `check_complete_patient(resource)` | Raises unless the Patient has a PT identifier, official name, gender and full birth date. |
| `build_ingest_bundle`, `build_setup_bundle` | The only two ways to make a Bundle (see above). |
| `heart_rate_transaction(points, *, patient, device, data_source, tz, recorded, complete_before)` | `health-sync`: roll-up JSON to an ingest Bundle. |
| `blood_pressure_transaction(reading, *, patient, device, source, recorded)` | `v2-to-fhir`: one cuff reading to an ingest Bundle. |
| `patient_setup_transaction(patient, *, devices=())` | Setup: the full Patient as an upsert. |
| `window_from_rollup(point)`, `parse_google_utc_offset("-18000s")`, `window_status(window, complete_before)` | Google helpers. |
| `GoogleDataSource(*, platform, recording_method)`, `rollup_source(window, data_source)` | Provenance source for a roll-up window. No defaults. |
| `heart_rate_key`, `blood_pressure_key` | The idempotency keys. |

A `BuiltResource` has `.resource` (the dict), `.full_url` (`urn:uuid:…`), `.key`, `.update`
(the `PUT` request), `.create_if_absent` (the conditional-create request, Patient and Device
only) and `.reference()`.

### Setup: connect script (T11) and seeder (T09)

```python
bundle = patient_setup_transaction(
    PatientInput("PB-0001", family="Doe", given=("Sam",), gender="female",
                 birth_date=date(1980, 4, 12), google_health_user_id=health_user_id),
    devices=[DeviceInput("Pixel Watch 4")],
)
```

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
bundle = blood_pressure_transaction(
    BloodPressureReading(obx14_time, systolic, diastolic, source_key=f"{msh3}/{msh10}", unit=obx6),
    patient=PatientInput(pid3_id, family=pid5_family, given=(pid5_given,)),
    device=DeviceInput(sending_application),
    source=SourceInfo(record_type="hl7v2-message", record_id=store_message_name,
                      platform="HL7V2_STORE", recording_method=None),
    recorded=datetime.now(UTC),
)
```

A `FhirInputError` from any of these is the application-level `error` reason (D7).

### Share to OpenEMR (T15)

Read the Patient from the store, call `check_complete_patient(patient)` (a clear error if the
setup step has not run yet), then send it: it already has the shape OpenEMR needs (spike S1). Before
`POST`ing an Observation, replace `subject` with OpenEMR's Patient, add `encounter`, and drop
`device`, `meta` and `identifier` if wanted (OpenEMR discards them). Units are already exactly
`/min` and `mm[Hg]`.

## Design decisions

### Write strategy

| Resource | Ingest Bundle | Setup Bundle | Key |
| --- | --- | --- | --- |
| Patient | `POST Patient`, `ifNoneExist: identifier=urn:pulsebridge:patient\|<id>` | `PUT Patient?identifier=…` | the configured patient id |
| Device | `POST Device`, `ifNoneExist: identifier=urn:pulsebridge:device\|<uuid>` | `PUT Device?identifier=…` | UUIDv5(patient, display name) |
| Observation (HR) | `PUT Observation?identifier=urn:pulsebridge:observation\|<uuid>` | not written | UUIDv5(patient, `8867-4`, window start UTC) |
| Observation (BP) | same | not written | UUIDv5(patient, `85354-9`, source key) |
| Provenance | `PUT Provenance/<uuid>` | not written | UUIDv5(the Observation's key) |

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
| `activity` | v3 DataOperation `CREATE` | the activity is PulseBridge creating the FHIR record from the source record; `ProvenanceActivityType` (the value set R4 binds here) includes DataOperation |
| `agent` type `performer` | the Device; its `role` carries the recording method (`PASSIVELY_MEASURED`) when the source states one | `agent.role` is "the function of the agent with respect to the activity": how the device took part. Its binding is only an example, so a local code is allowed |
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

## Examples for the validator (T08)

`examples/` holds one file per resource type plus three transaction Bundles, all synthetic:
`bundle-patient-setup.json` (the Patient upsert), `bundle-heart-rate-ingest.json` and
`bundle-blood-pressure-ingest.json` (conditional creates; the latter built from PID-3 and PID-5
only, and the former with one `preliminary` window). They are generated, never edited by hand:

```bash
.venv/bin/python -m pulsebridge_fhir.examples   # rewrites libs/pulsebridge_fhir/examples/
```

A test fails if the committed files differ from what the code produces.

## Tests

`make test` runs `tests/`. All data is synthetic. Each behaviour was break-checked: the
implementation was deliberately broken and a test seen to fail.
