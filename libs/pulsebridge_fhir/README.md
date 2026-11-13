# pulsebridge_fhir

Builds the FHIR R4 resources PulseBridge writes: `Patient`, `Device`, heart-rate and
blood-pressure `Observation`s, `Provenance`, and the transaction `Bundle` that writes them
idempotently. Pure Python, no runtime dependencies, no network or GCP calls.

Callers pass plain values (frozen dataclasses that validate themselves) and get plain dicts back,
ready for `json.dumps`. Any bad input raises `FhirInputError` (a `ValueError`) whose message names
the input and the field.

## Public API

| Name | What it is |
| --- | --- |
| `PatientInput(patient_id, family, given, gender, birth_date, google_health_user_id=None)` | The person. `patient_id` is PulseBridge's own id (configuration, or PID-3 on the HL7v2 path). |
| `DeviceInput(display_name, manufacturer=None)` | The measuring device. |
| `HeartRateWindow(start, end, bpm_min, bpm_avg, bpm_max)` | One roll-up window. Times must be timezone-aware. |
| `BloodPressureReading(effective, systolic, diastolic, source_key, unit="mm[Hg]")` | One cuff reading. `source_key` is its idempotency key, e.g. `"<MSH-3>/<MSH-10>"`. |
| `SourceInfo(platform, record_type, record_id, recording_method=None)` | The source record, for Provenance. |
| `build_patient(patient)` | `Patient` |
| `build_device(device, *, patient)` | `Device`, identifier derived from patient and name |
| `build_heart_rate_observation(window, *, patient, device, tz, status="final")` | heart-rate `Observation` |
| `build_blood_pressure_observation(reading, *, patient, device, status="final")` | blood-pressure panel `Observation` |
| `build_provenance(*, target, device, source, recorded, assembler)` | `Provenance` for one Observation |
| `build_transaction_bundle(resources)` | transaction `Bundle` of `PUT`s, with checks |
| `window_from_rollup(point)` | Google roll-up JSON item to `HeartRateWindow` |
| `parse_google_utc_offset("-18000s")` | Google offset to a fixed `timezone` |
| `GoogleDataSource(platform="FITBIT", recording_method="PASSIVELY_MEASURED")`, `rollup_source(window, data_source)` | Provenance source for a roll-up window |
| `heart_rate_resources(...)`, `heart_rate_transaction(points, *, patient, device, data_source, tz, recorded)` | Whole `health-sync` mapping: roll-up JSON to one Bundle |
| `heart_rate_key`, `blood_pressure_key` | The idempotency keys, exposed for callers that look resources up |

Every builder returns a `BuiltResource`: `.resource` (the dict), `.full_url` (`urn:uuid:…`),
`.request_url` (the `PUT` target), `.key`, `.reference()` and `.bundle_entry()`.

### `health-sync` (T10) and the seeder (T09)

```python
from zoneinfo import ZoneInfo
from pulsebridge_fhir import GoogleDataSource, heart_rate_transaction

bundle = heart_rate_transaction(
    rollup_json["rollupDataPoints"],          # list of Google roll-up items (field name per T10)
    patient=patient_input,                    # from configuration
    device=DeviceInput("Pixel Watch 4"),      # dataSource.device.displayName
    data_source=GoogleDataSource(),           # dataSource.platform / recordingMethod
    tz=ZoneInfo(user_time_zone),              # or parse_google_utc_offset(sample["utcOffset"])
    recorded=datetime.now(UTC),
)
# POST bundle to the FHIR store base URL (executeBundle).
```

### `v2-to-fhir` (T12)

```python
patient = build_patient(PatientInput(pid3_id, family, (given,), gender, birth_date))
cuff = build_device(DeviceInput(sending_application), patient=patient)
obs = build_blood_pressure_observation(
    BloodPressureReading(obx14_time, systolic, diastolic, source_key=f"{msh3}/{msh10}", unit=obx6),
    patient=patient, device=cuff)
prov = build_provenance(target=obs, device=cuff,
    source=SourceInfo("HL7V2_STORE", "hl7v2-message", store_message_name, "ACTIVELY_MEASURED"),
    recorded=datetime.now(UTC), assembler="PulseBridge v2-to-fhir")
bundle = build_transaction_bundle([patient, cuff, obs, prov])
```

A `FhirInputError` from any of these is the application-level `error` reason (D7).

### Share to OpenEMR (T15)

`build_patient(...).resource` is already the shape OpenEMR needs (spike S1): a `PT`-typed
identifier, a name with `use: official`, gender and birth date. Before `POST`ing an Observation
read from the store, T15 replaces `subject` with OpenEMR's Patient, adds `encounter`, and may drop
`device`, `meta` and `identifier` (OpenEMR discards them). Units are already exactly `/min` and
`mm[Hg]`.

## Design decisions

### Writing idempotently: conditional update, and why Provenance differs

| Resource | `Bundle.entry.request` | Key |
| --- | --- | --- |
| Patient | `PUT Patient?identifier=urn:pulsebridge:patient\|<patient_id>` | the configured patient id |
| Device | `PUT Device?identifier=urn:pulsebridge:device\|<uuid>` | UUIDv5(patient, display name) |
| Observation (HR) | `PUT Observation?identifier=urn:pulsebridge:observation\|<uuid>` | UUIDv5(patient, `8867-4`, window start UTC) |
| Observation (BP) | same | UUIDv5(patient, `85354-9`, source key) |
| Provenance | `PUT Provenance/<uuid>` | UUIDv5(the Observation's key) |

- **Conditional update** (`PUT Type?search`) tells the server: update the one resource that
  matches, or create one if none does. Sending the same Bundle twice therefore changes nothing new,
  and a window whose values changed (it was only partly synced) is updated in place. The key leaves
  out the window end and the values for that reason.
- The resource body of a conditional update carries **no `id`**: the server picks the id on create,
  and finds the existing one on update.
- Keys are UUIDv5 (name-based, deterministic) over a canonical JSON list of the parts, so they are
  URL-safe and cannot collide by concatenation. Google's data points have no `name` to key on
  (spike S3).
- **Provenance has no `identifier` element in R4**, and the Cloud Healthcare API's conditional
  update matches on identifier only. So Provenance is written by a deterministic id
  (`PUT Provenance/<uuid>`, "update as create"). **This needs the FHIR store's
  `enableUpdateCreate = true` (task T05).** Re-syncing a window overwrites its one Provenance; the
  store's version history keeps each write. Rejected: `POST` (a new Provenance on every re-sync)
  and a `_tag` search key (not identifier-based, so not supported by the store's conditional
  update).

### References inside the Bundle: `urn:uuid`

Each entry has `fullUrl: urn:uuid:<uuid>`, and other entries refer to it by that value
(`"subject": {"reference": "urn:uuid:…"}`). On commit the server rewrites them to the real
`Patient/<id>` (R4 bundle.html, "Resolving references in Bundles"). This works whether the Patient
is being created or already exists. Conditional references (`"reference": "Patient?identifier=…"`)
are also legal in R4 transactions but are less widely supported; `urn:uuid` is the portable
choice. The bundle builder refuses a `urn:uuid` reference with no matching entry, and two entries
writing the same URL.

### Identifiers

- Systems are URNs owned by this software (`urn:pulsebridge:…`, all in `codes.py`): the same in
  every clone, so nothing identifies the author.
- `Patient.identifier[0]`: `use: official`, `type` v2-0203 `PT` (Patient external identifier).
  The conditional-update key, and what T15 searches OpenEMR by.
- `Patient.identifier[1]`: Google's `healthUserId`, `use: secondary` (it identifies the person
  only within Google Health). Not the key: reconnecting a different Google account does not create
  a second patient.
- `Observation.identifier`: `use: official`, the deterministic key. PulseBridge is the system
  that assigns it, and the conditional update matches on it.
- `Device.identifier`: derived (UUIDv5 of patient and display name), because Google sends only a
  display name and no serial number (spike S3).

### Times

- Heart rate uses **`effectivePeriod`**: the value summarises an interval, so a single
  `effectiveDateTime` would misstate it. Blood pressure uses **`effectiveDateTime`**: one moment.
- Google sends UTC (`…Z`). The builder writes times in the user's zone (`tz`): `14:05:00Z` with
  offset `-18000s` becomes `2026-01-01T09:05:00-05:00`. Same instant, the wall time the user saw.
  A `zoneinfo.ZoneInfo` follows daylight saving per window; `parse_google_utc_offset` gives a
  fixed offset. The key uses the UTC instant, so changing `tz` never changes the key.
- FHIR `Period` bounds are inclusive; Google's windows are `[start, end)`. Google's `end` is kept
  as is, so adjacent windows share a boundary instant. Readers should treat `end` as exclusive.

### Heart rate: value, components and the LOINC codes

- `code`: LOINC `8867-4` Heart rate (fixed by the R4 heartrate profile).
- `valueQuantity`: the window **average**, `/min` (UCUM). `method.text` says it is a mean.
- `component`: minimum and maximum, each `/min`.

#### Heart-rate component codes

| Statistic | LOINC | Long common name |
| --- | --- | --- |
| minimum | `96935-2` | Heart rate unspecified time minimum by Pedometer |
| maximum | `55426-1` | Heart rate unspecified time maximum by Pedometer |

Why these: LOINC has no minimum/maximum variant of `8867-4` itself. The timed codes
(`8873-2` Heart rate 24 hour maximum, `8883-1` 24 hour minimum, and the 1, 8, 10 and 12 hour
codes) carry a fixed duration in their TIME axis and would misstate a five-minute window. These two
use TIME `XXX^max` / `XXX^min`, meaning "the interval is given elsewhere", which here is
`effectivePeriod`. Their METHOD, "Pedometer", is the method of LOINC's pedometer tracking family
(panel `55413-9`, alongside `55425-3` mean heart rate and `55423-8` step count), the codes used for
wrist-worn activity trackers. The method is the one imperfect fit. One other candidate,
`101692-2` (listed with the short name "Maximum HR"), appeared in search results, but its
definition could not be confirmed and it has no minimum counterpart, so it is not used.

**Verification status:** confirmed through web search against secondary listings of the LOINC
table (findacode.com entries for both codes; the NLM VSAC "Heart Rate" value set; the HL7 Physical
Activity IG value sets). loinc.org, findacode.com and hl7.org could not be fetched directly from the
build environment. Re-check both codes on loinc.org before relying on them.

### Blood pressure

Panel `85354-9` with components `8480-6` (systolic) and `8462-4` (diastolic), each `mm[Hg]`, and
no top-level value: the shape the R4 bp profile requires. `unit` must be exactly `mm[Hg]`; a
wrong unit is rejected, never relabelled.

### Category

`category` carries `http://terminology.hl7.org/CodeSystem/observation-category#vital-signs`: the
vital-signs profile requires it (slice VSCat), and OpenEMR routes on it.

### Provenance

| Element | Value | Why |
| --- | --- | --- |
| `target` | the Observation | what this Provenance is about |
| `recorded` | when PulseBridge wrote it | passed in by the caller, keeping builders deterministic |
| `agent` (type `performer`) | the Device | it took the measurement |
| `agent` (type `custodian`) | source platform by identifier (`FITBIT`, `HL7V2_STORE`) | holds the original record |
| `agent` (type `assembler`) | the PulseBridge service, by display | the software that derived the FHIR resource |
| `activity` | recording method (`PASSIVELY_MEASURED`), `urn:pulsebridge:recording-method` | how the source says it was measured |
| `entity` (role `source`) | the source record by identifier (HL7v2 store message name, roll-up window) | lets the trace panel find the original |

Agent type codes come from `provenance-participant-type`; `assembler` is defined as "a device that
operates … on custodian's algorithms for data extraction of existing information for purpose of
generating a new artifact", which is what the mapper does.

### Conformance to the R4 Vital Signs profiles

Checked against the R4 (4.0.1) StructureDefinitions `vitalsigns`, `heartrate` and `bp`.

| Rule | Status |
| --- | --- |
| status, category VSCat, code, subject, effective[x] (dateTime or Period) | followed |
| heartrate: code `8867-4`; `valueQuantity` value, unit, UCUM system, code `/min` | followed |
| bp: code `85354-9`; no `valueQuantity`; one `8480-6` and one `8462-4` component in `mm[Hg]` | followed |
| vs-2 (value or component), vs-3 (component value or dataAbsentReason) | followed |
| `component.value[x]` bound **required** to ucum-vitals-common | followed (`/min`, `mm[Hg]`) |
| `component.code` bound **extensible** to observation-vitalsignresult | **deliberate divergence** for the heart-rate min/max components: that 13-code value set has no min/max codes, which is exactly when an extensible binding allows another code. Expect a validator warning, not an error. |
| `Provenance.activity` bound extensible to provenance-activity-type | **deliberate divergence**: no code there describes a recording method. Expect a warning. |
| `urn:pulsebridge:*` code systems | local, undefined code systems; the validator may report it cannot check them. |

Each Observation claims its profile in `meta.profile`, so the validator in T08 checks it against
that profile, not only against the base resource.

## Examples for the validator (T08)

`examples/` holds one file per resource type plus two transaction Bundles, all synthetic. They
are generated, never edited by hand:

```bash
.venv/bin/python -m pulsebridge_fhir.examples   # rewrites libs/pulsebridge_fhir/examples/
```

A test fails if the committed files differ from what the code produces.

## Tests

`make test` runs `tests/`. All data is synthetic. Each test was break-checked: the builder was
deliberately broken and the test seen to fail.
