# v2-to-fhir

Turns each HL7v2 ORU^R01 blood-pressure message stored in the HL7v2 store into a FHIR R4
blood-pressure Observation, and records an application-level outcome on every message.

```
HL7v2 store ──notification (message name, msgType)──> Pub/Sub ──push + OIDC──> v2-to-fhir
   ^                                                                              │
   │  GET <name>?view=RAW_ONLY ───────────────────────────────────────────────────┤
   │  PATCH <name>?updateMask=labels  {application_outcome, reason} ──────────────┤
                                                    FHIR store <── executeBundle ─┘
```

It is the first real service, so its layout is the pattern the others follow.

## Layout

```text
services/v2-to-fhir/
  pyproject.toml        dependencies: the three libs, Flask, gunicorn (exact pins)
  v2_to_fhir/
    mapping.py          THE MAPPING: HL7v2 -> FHIR. Pure: no GCP, no network, no clock.
    reasons.py          every outcome reason code, with the sentence the dashboard shows
    handler.py          one push in, one HTTP status out: fetch, map, write, label
    config.py           environment variables
    app.py              Flask app: one route, JSON log lines
  tests/                pytest; no network, no credentials (pulsebridge_gcp.testing.FakeTransport)
```

Read `mapping.py` top to bottom: its docstring has the field-by-field table, and the code runs
in the order a message is checked (MSH, PID, OBX, build).

## Input: the raw ER7, not `parsedData`

The handler fetches the stored message with `view=RAW_ONLY` and maps the exact bytes, parsed
with `hl7apy` through `pulsebridge_hl7v2.parse_er7` (decision D3). Spike S5 compared this with
the store's own `parsedData` and found that `parsedData` numbers MSH fields one lower than HL7
(MSH-10 is key `"9"`), changes its key format when a field repeats (`"3.1"` becomes
`"3[0].1"`), depends on the store's immutable `parserConfig`, and can only be produced by a real
store, so tests could not use T07's samples. The raw bytes read as HL7 (`obx.obx_14`), and the
same library builds and validates the messages in T07 and T13.

## The mapping

| HL7v2 | FHIR | Rule |
| --- | --- | --- |
| MSH-9 | none | must be `ORU^R01` |
| MSH-12 | none | must be `2.5.1` (what the cuff sends; other versions have other group and field rules) |
| MSH-3.1, MSH-4.1 | `Device.deviceName` `"CUFFSIM (PULSEBRIDGE)"`; `Observation.device` | conditional create (D11) |
| MSH-10 | `Observation.identifier` = UUIDv5(patient, `85354-9`, `MSH-3|MSH-4|MSH-10`) | conditional update on that identifier, so a redelivery rewrites the same Observation |
| PID-3.1 | `Patient.identifier` (`urn:pulsebridge:patient`) | must equal `PATIENT_ID`; any PID-3 repetition may carry it |
| PID-5.1.1, PID-5.2 | `Patient.name` family, given | either may be empty; only used if the Patient does not exist yet (conditional create) |
| OBX-3 `8480-6^…^LN` / `8462-4^…^LN` | `component[].code` (LOINC); `Observation.code` is the panel `85354-9` | any other code or coding system is refused |
| OBX-2, OBX-5 | `component[].valueQuantity.value` | OBX-2 `NM`; one value matching HL7's NM rule |
| OBX-6 | `valueQuantity.code` `mm[Hg]`, `system` `http://unitsofmeasure.org` | OBX-6.1 exactly `mm[Hg]`; OBX-6.3 empty or `UCUM` |
| OBX-11 | `Observation.status` | `F` final, `C` corrected, `P` preliminary; both OBX the same |
| OBX-14 | `Observation.effectiveDateTime` | required, with a UTC offset, kept as sent; both OBX the same instant |
| store message name | `Provenance.entity[role=source].what.identifier` | the name from the notification |

The Bundle itself is built by `pulsebridge_fhir.blood_pressure_transaction` (T06): Patient and
Device `POST` with `ifNoneExist`, the Observation `PUT Observation?identifier=…`, the Provenance
`PUT Provenance/<uuid>`. See `libs/pulsebridge_fhir/README.md` for the JSON.

### Decisions

- **No OBR-7 fallback for OBX-14.** D7 lists a missing OBX-14 as a malformed class and T07's
  `unmappable_missing_obx14` sample has OBR-7 set; the cuff always writes OBX-14, so an empty one
  is a sender bug worth seeing.
- **OBX-14 without an offset is refused** (`missing-time-zone`). HL7 says such a time is in "the
  local time zone of the sender", which the receiver cannot know; reading it as UTC would move a
  reading by hours silently. With an offset, the offset is kept (`20261113155329-0500` becomes
  `2026-11-13T15:53:29-05:00`): the wall time the person saw, as the correct instant.
- **Unknown OBX segments refuse the message** rather than being dropped: silently losing part of
  a clinical message is worse than an `error` the log shows.
- **The idempotency key includes MSH-3 and MSH-4**, because HL7 makes MSH-10 unique per sending
  system, not globally. It excludes MSH-7, so a resend of the same reading (same control ID,
  new MSH-7, therefore a new stored message, spike S5) updates the same Observation.
- **Version 2.5.1 only.** hl7apy can read 2.3 to 2.8, but group names and field types differ;
  mapping only the version we test keeps the rules honest.
- **Ranges** are T06's (systolic 50-300, diastolic 20-200 mm[Hg]), checked here first so the
  reason is precise.

## Outcome reasons

Every stored message ends with two labels: `application_outcome` (`accepted` or `error`) and
`reason`. They are written together in one `messages.patch` with `updateMask=labels`, so a
reprocessed message never keeps a stale reason, and the store merges them with other labels
(the cuff simulator's `transport` stays). Patching labels publishes no notification (S5), so
there is no loop. Codes keep to the documented label limits. The sentence for each code is in
`reasons.py` (`SENTENCES`), for the dashboard.

| Reason | Outcome | Meaning |
| --- | --- | --- |
| `mapped` | accepted | written to the FHIR store |
| `unparseable-by-mapper` | error | hl7apy could not read the stored bytes (or the store returned no usable `data`) |
| `unsupported-version` | error | MSH-12 is not 2.5.1, including versions hl7apy cannot load (`9.9`, empty) |
| `unsupported-message-type` | error | MSH-9 is not ORU^R01 (empty, `ADT^A01`, bare `MSH|^~\&`) |
| `missing-control-id` | error | MSH-10 empty |
| `missing-sending-application` | error | MSH-3 empty |
| `no-observations` | error | no OBX segment (MSH-only message) |
| `multiple-patients` | error | more than one PID |
| `missing-patient-identifier` | error | no PID, or no PID-3.1 |
| `unknown-patient` | error | no PID-3 repetition is the configured patient |
| `unknown-obx-3-code` | error | an OBX-3 that is not LOINC 8480-6 or 8462-4 |
| `duplicate-obx-3-code` | error | the same OBX-3 twice |
| `missing-systolic` / `missing-diastolic` | error | one of the two values is absent |
| `wrong-value-type` | error | OBX-2 is not NM |
| `missing-obx-5` / `repeated-obx-5` / `non-numeric-obx-5` | error | OBX-5 empty, repeated, or not an HL7 NM number |
| `missing-unit` / `wrong-unit` | error | OBX-6 empty, or not UCUM `mm[Hg]` |
| `unsupported-result-status` / `mixed-result-status` | error | OBX-11 not F, C or P; or the two differ |
| `missing-obx-14` / `invalid-obx-14` / `missing-time-zone` / `mismatched-obx-14` | error | OBX-14 empty; not a DTM to the minute; no offset; the two differ |
| `out-of-range` | error | outside the plausible range |
| `systolic-not-above-diastolic` | error | systolic not greater than diastolic |
| `fhir-input-rejected` | error | T06's builders refused a value (should not happen after the checks above) |
| `mapping-failed` | error | unexpected exception while mapping (a bug); logged by type and line, never by content |
| `fhir-rejected` | error | the FHIR store refused the Bundle with a 4xx; its OperationOutcome is logged without free text |

Against T07's samples: every `parses_but_cannot_map` and `structurally_invalid` sample ends
`error` with the reason above; T07 suggested `unparseable-by-mapper` for MSH-12 `9.9` and empty,
and we use the more precise `unsupported-version`. The `unparseable` samples never reach the
mapper (the store answers `AE`), but would end `unparseable-by-mapper` if they did.

## Acknowledge or retry

A Pub/Sub push is acknowledged by a 102, 200, 201, 202 or 204 answer; anything else is retried
with backoff and, after `max_delivery_attempts` (10 in T05), moved to the dead-letter topic. So
the subscription never stalls: a message is either acknowledged or, at worst, parked in the
dead-letter subscription.

| Situation | HTTP | Labels |
| --- | --- | --- |
| mapped and written | 204 | `accepted` / `mapped` |
| any content problem (every `error` reason except `fhir-rejected`) | 204 | `error` / reason |
| FHIR store 4xx other than 401, 403, 404, 409 | 204 | `error` / `fhir-rejected` |
| notification with `msgType` `ACK` | 204 | none, nothing fetched |
| message no longer exists (404 on get or patch) | 204 | none |
| 5xx, 429, 408, timeout, network error, token refresh failure (either store) | 503 | none |
| 401/403 (IAM missing or still propagating), FHIR 404 (store missing), FHIR 409 (concurrent write) | 503 | none |
| body is not a Pub/Sub push, or names a message outside `HL7V2_STORE_ID` | 400 | none |
| a bug in the handler itself | 500 | none |

- Labels are written only after the outcome is final. A message whose FHIR write is being
  retried stays unlabelled, which the dashboard shows as "pending", never a false `accepted`.
- Retrying is safe (at-least-once delivery): the Observation is keyed on the control ID, the
  Provenance on the Observation, the Patient and Device are conditional creates, and the label
  patch overwrites both keys.
- 400 for an unusable envelope: Pub/Sub only sends well-formed pushes, so this means a changed
  contract or a caller that is not Pub/Sub; after the retries it is kept in the dead-letter
  subscription for inspection rather than dropped.

### Who may call it

The Cloud Run service does not allow unauthenticated calls. Pub/Sub attaches an OIDC token
signed for the `pubsub-push` service account; Cloud Run verifies it and checks that account's
`roles/run.invoker` on this service before the request reaches the container (T04, T05, T20).
The handler itself checks only that the body is a push envelope and that the message name is
inside the configured dataset's HL7v2 store.

## Configuration

Environment variables, set by Terraform (T20). Nothing deployment-specific is in the code.

| Variable | Required | Meaning |
| --- | --- | --- |
| `GCP_PROJECT_ID` | yes | project of the Healthcare dataset |
| `HEALTHCARE_LOCATION` | yes | dataset region, e.g. `us-central1` |
| `HEALTHCARE_DATASET_ID` | yes | dataset id |
| `FHIR_STORE_ID` | yes | FHIR R4 store id |
| `HL7V2_STORE_ID` | yes | HL7v2 store id; notifications for other stores are refused |
| `PATIENT_ID` | yes | the configured patient; PID-3.1 must equal it (the setup step's id) |
| `PATIENT_ASSIGNING_AUTHORITY` | no | if set, the matching PID-3 must also carry it in PID-3.4.1 |
| `PORT` | no | listening port (Cloud Run sets it; default 8080 in the command below) |

The FHIR identifier system for the Patient is `urn:pulsebridge:patient`, fixed in T06's
`codes.py` and the same in every clone; it is not configured here.

Credentials: Application Default Credentials, which on Cloud Run are the service's own
`v2-to-fhir` service account (roles: FHIR Resource Editor on the FHIR store, HL7v2 Consumer on
the HL7v2 store; see `infra/modules/healthcare/README.md`). No key file.

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 "v2_to_fhir.app:create_app()"
```

- Honours `PORT` (Cloud Run sets it). `--timeout 0` leaves request deadlines to Cloud Run, as
  Google's Cloud Run Python samples do.
- The push endpoint is `POST /pubsub/hl7v2`.
- For T24's container: install `libs/pulsebridge_fhir`, `libs/pulsebridge_hl7v2` and
  `libs/pulsebridge_gcp` before this package. They are not on PyPI and are declared by name
  only, so installing this package alone would look for them on PyPI.
- Logs are one JSON object per line on stdout (`severity`, `message`, `event`, `http_status`,
  `application_outcome`, `reason`, `hl7v2_message` id, `control_id`, Pub/Sub ids). They never
  contain message content or patient fields.

## Tests

`make test` runs `tests/`. All data is synthetic; T07's samples are loaded by name through
`pulsebridge_hl7v2.samples`, never copied. Handler and app tests use the real
`HealthcareClient` over `FakeTransport`, so the error classification is tested end to end with
no network and no credentials.

## Not verified against the real API

This task made no GCP calls. To confirm on the first deployment (T25): the push body shape
(spike S5 took it from documentation), that the store resolves the Bundle's `urn:uuid`
references to a Patient a conditional create matched (T06 README), that `hl7V2Consumer` allows
the label patch (T05 README), and the exact status codes `executeBundle` returns for a refused
transaction.
