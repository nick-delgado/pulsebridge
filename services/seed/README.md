# seed

Writes plausible, **synthetic** five-minute heart-rate windows for a **synthetic** patient into
the FHIR store, through the same T06 mapping `health-sync` uses. It backs the
`use_synthetic_data` flag: a stranger who clones the repository can see a filled dashboard
without a Pixel Watch, a Google Health OAuth app or any real health data.

**For deployments with `use_synthetic_data = true` only.** In such a deployment `health-sync` is
not deployed, and the seeder is the wearable source and the setup step (decision D11). Do not
run it against a store that holds a real person's data; it refuses to touch a Patient it did not
write (see "Safety").

```
pulsebridge-seed --days 30
  1. range       [floor(until) - 30 days, floor(until)), five-minute boundaries in UTC
  2. generate    one item per window: a pure function of (seed, TIME_ZONE, window start)
  3. safety      GET Patient?identifier=urn:pulsebridge:patient|SYN-…   (one search)
  4. setup       POST executeBundle: PUT Patient?identifier=…, PUT Device?identifier=…   (once)
  5. data        POST executeBundle × N: T06 heart_rate_transaction, 100 windows each
```

## Layout

```text
services/seed/
  pyproject.toml           dependencies: pulsebridge-fhir, pulsebridge-gcp
  pulsebridge_seed/
    generator.py           the synthetic person: windows in the Google Health roll-up shape
    synthetic.py           what marks the data as synthetic (identifier, names, Provenance, tag)
    seeder.py              one run: range, cutoff, safety check, setup Bundle, batches, report
    config.py              environment variables, validated before any work
    cli.py                 pulsebridge-seed: flags, exit codes, report on stdout
    logs.py                JSON log lines on stderr
  tests/                   pytest; no network, no credentials, no GCP
```

Read `generator.py` first, then `seeder.py`: their docstrings are the design.

## The generator

Every window is the JSON item spike S3 recorded for `dataPoints:rollUp` (shape only, no data
from the spike was used or seen):

```json
{"startTime": "2026-03-08T07:00:00Z", "endTime": "2026-03-08T07:05:00Z",
 "heartRate": {"beatsPerMinuteMin": 55, "beatsPerMinuteAvg": 58.37, "beatsPerMinuteMax": 63}}
```

Minimum and maximum are whole JSON numbers and the average a decimal, as S3 recorded. Because
the seeder hands T06 exactly what `health-sync` hands it, the same mapping, keys, validation and
Bundle code is exercised.

**The model** (all numbers hashed, see below):

| Part | How | Per |
| --- | --- | --- |
| resting rate | 54 to 66 /min for the person, ±2 day to day | seed; seed and local date |
| asleep / awake | wake 06:00 to 08:00, sleep 22:00 to 23:30 local; asleep about resting − 4, awake about resting + 14, 30-minute ramps | seed and local date |
| exercise | 0 bouts (25% of days), 1 (55%) or 2 (20%); 20 to 60 minutes; peak 115 to 165 /min; 5-minute warm-up, 10-minute cool-down | seed and local date |
| drift | smooth noise on a 30-minute UTC grid, interpolated, so neighbouring windows stay close | seed and UTC time |
| jitter, spread | ±1.5 on the average; min 2 to 15 below, max 2 to 15 above, wider when awake or exercising | seed and window start |
| off the wrist | on 80% of days one 20 to 75 minute gap in the morning (charging); about 1% of other windows missing | seed and local date; seed and window start |

Invariants, tested over many seeded days: `min <= average <= max`, all within 38 to 195 /min
(inside T06's 1 to 300). A full day is 288 windows (D8); with the gaps a day typically has about
270 to 285, so about 540 to 570 Observation and Provenance entries plus two per Bundle.

**Determinism.** Each number is `BLAKE2b(canonical JSON of [model version, seed, purpose,
parts…])` read as a fraction in `[0, 1)`. There is no `random` module, no `hash()` (which Python
salts per process) and no generator state, so the value of a window does not depend on when the
seeder ran, which range it was asked for, or how many numbers were drawn before. The overlap of
two runs is identical, window for window (tested with different `--until` values).

**Why the zone is an input.** A person's rhythm follows their local clock. With the rhythm on
UTC, a deployment in Tokyo would show the night dip mid-morning. So the values depend on
`(seed, TIME_ZONE, window start)`: the seed and the zone together define the synthetic person.
Both are fixed configuration for a deployment; changing either is choosing another person
(windows already stored would then be rewritten with the new person's values on the next run
over them). **The window keys never depend on the zone**: T06 keys the Observation on the
patient, LOINC `8867-4` and the window's UTC start (tested). On a daylight-saving day the local
day is 23 or 25 hours long (276 or 300 windows) and the local times in `effectivePeriod` carry
the right offset on each side of the change (tested for New York).

## What marks the data as synthetic

| Marker | Value | Where it shows |
| --- | --- | --- |
| Patient identifier | must start with `SYN-` (checked at start-up) | `Patient.identifier` (`PT`), every reference |
| Patient name | family `Synthetic`, given `Demo`; gender `other`, born 1980-01-01 (fixed, invented) | dashboard, OpenEMR after a share |
| Device | `PulseBridge synthetic heart-rate generator`, manufacturer `PulseBridge (synthetic data)` | `Device.deviceName`, `Observation.device` |
| Provenance custodian | source platform `PULSEBRIDGE_SYNTHETIC` (a watch has `FITBIT`) | `Provenance.agent[type=custodian]` |
| Provenance recording method | `SYNTHETIC` (a watch has `PASSIVELY_MEASURED`) | `Provenance.agent[type=performer].role` |
| Provenance assembler | `PulseBridge seed (synthetic data)` | `Provenance.agent[type=assembler]` |
| Tag | HL7 v3 ActReason `HTEST` "test health data", system `http://terminology.hl7.org/CodeSystem/v3-ActReason` | `meta.tag` on every resource; searchable with `_tag` |

Notes:

- **Provenance** (FHIR's record of where a resource came from) is the place a reader looks to
  know whether an Observation was measured. T06's `build_provenance` lets a caller choose the
  platform, recording method and assembler; those three say "synthetic". The custodian ("who
  holds the original") is the generator, which can recreate every window exactly from the seed.
- **The source record type is a known gap.** `heart_rate_transaction` always names the source
  record `google-health-heart-rate-rollup` (the `Provenance.entity` identifier system), which
  here describes the *shape* of the window, not that Google sent it. The other markers make the
  origin plain. A `record_type` argument on `heart_rate_transaction` would fix it; that is a
  change to `libs/pulsebridge_fhir`, proposed as a follow-up and not made here.
- **`HTEST` in `meta.tag`.** `meta.tag`'s binding in R4 is an example binding ("Common Tags"), so
  any code may be used; `HTEST` is the HL7 code for test data. T06's builders take no `meta`, so
  the seeder adds the tag to each resource of T06's Bundle (`synthetic.mark_synthetic`); it does
  not change any request URL or key. Whether Cloud Healthcare keeps `meta.tag` on a conditional
  update is not verified (no GCP call was made); the safety check does not rely on it alone.

## Safety

The setup Bundle **replaces** the Patient with the configured identifier (`PUT`, D11), and the
ingest Bundles add heart rate to that Patient's record. Run against a real person's record, that
would overwrite their demographics and mix invented readings into their chart. Three guards:

1. **`PATIENT_ID` must start with `SYN-`**, or the seeder does not start. A real deployment's
   patient identifier comes from its own configuration and has no reason to look like that.
2. **One search before any write**: `Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>`.
   - no Patient: a fresh store, go on (`safety_check: no-patient`);
   - one Patient that the seeder wrote (tagged `HTEST`, or carrying the synthetic name, in case
     the store dropped the tag): go on (`synthetic-patient`);
   - otherwise (one Patient the seeder did not write, for example one the connect script T11
     wrote, or that `health-sync` or `v2-to-fhir` created on first contact; or several Patients
     with the identifier): **refuse**, exit code 4, nothing written (`refused`, reason
     `patient-not-synthetic` or `patients-duplicated`).
   - `--force` writes anyway (it logs `seed-forced`). Use it only to take over a store that you
     know holds nothing real, for example after an earlier version of the seeder wrote an
     untagged Patient.
3. **This README and `--help` say** the seeder is for `use_synthetic_data = true` deployments.

Why one search and not more: the Patient is the resource a wrong run would damage, and every
heart-rate Observation of a patient refers to that Patient, so "a Patient the seeder did not
write" is exactly the case where non-synthetic data could exist. A search of Observations
without the tag (`_tag:not`) or by chained patient identifier would be more direct but relies on
search features not verified on Cloud Healthcare; `Patient?identifier=` is what `health-sync`
already relies on.

The search needs read access on the Patient: the `seeder` service account has FHIR Resource
Editor on the health-data store (identity module), which includes search.

## Re-running

- **No duplicates.** Every write is keyed (T06): Patient and Device by identifier, each
  Observation a conditional update `PUT Observation?identifier=urn:pulsebridge:observation|<UUIDv5
  of patient, 8867-4, window start>`, each Provenance `PUT Provenance/<UUIDv5 of that key>`.
  Running the same range twice sends Bundles with identical entries (same requests, same
  resources, tested), except Provenance `recorded` (the write time) and, for a window that has
  become old enough since, `status` `preliminary` to `final`.
- **History is not rewritten.** Values depend only on the seed, the zone and the window start,
  so a later run over an overlapping range sends the same values for the overlap. The store's
  version history gains a version per rewrite, with the same values.
- **`preliminary` and `final`** follow `health-sync`'s rule: `complete_before = floor(now -
  FINAL_AFTER_MINUTES)` (60), using the real clock, not `--until`. A window that ended within the
  last hour is `preliminary`; the next run that covers it writes it `final`.

## Running it

```bash
pulsebridge-seed --days 30          # or: python -m pulsebridge_seed.cli --days 30
pulsebridge-seed --hours 3          # top-up: the last three hours
pulsebridge-seed --days 1 --dry-run # counts only; no requests; needs only PATIENT_ID and TIME_ZONE
```

| Flag | Meaning |
| --- | --- |
| `--days N` / `--hours H` | the range length, one of them required: 1 to 366 days, or 1 to 336 hours |
| `--until T` | end of the range (exclusive), ISO 8601 with `Z` or an offset, floored to five minutes; default now; a later time than now is refused (no future data) |
| `--seed S` | overrides `SYNTHETIC_SEED` |
| `--dry-run` | generate and count, write nothing; the FHIR variables are not needed |
| `--force` | write even if the safety check refuses (see "Safety") |

Logs (JSON lines) go to stderr, the report (JSON) to stdout:

```json
{"outcome": "seeded", "reason": null,
 "range": {"start": "2026-02-08T12:00:00Z", "end": "2026-03-10T12:00:00Z"},
 "complete_before": "2026-03-10T11:00:00Z",
 "windows_in_range": 8640, "windows_generated": 8395, "windows_absent": 245,
 "windows_written": 8395, "windows_final": 8383, "windows_preliminary": 12,
 "bundles_planned": 84, "bundles_written": 84, "setup_written": true,
 "safety_check": "no-patient", "fhir_status": null, "fhir_issues": [],
 "entry_statuses": {"Device 200": 85, "Observation 201": 8395, "Patient 200": 84, "Patient 201": 1, "Provenance 201": 8395}}
```

(Illustrative counts.) The report and logs carry counts, UTC bounds and codes only: no heart-rate
values, no patient identifier, no seed.

| Exit | Meaning |
| --- | --- |
| 0 | `seeded`, or `dry-run` |
| 1 | a FHIR failure: `fhir-unavailable` (429/5xx/409/network after retries), `fhir-misconfigured` (401/403 `fhir-permission-denied`, 404 `fhir-store-missing`), `fhir-rejected` (another 4xx, its OperationOutcome summarised without free text, or a malformed 2xx); or an unexpected error (`bug`, type only). Re-run: every write is keyed |
| 2 | bad arguments or configuration (the message names the variable, never its value) |
| 4 | `refused` by the safety check |

A run stopped part-way leaves the setup Bundle and the Bundles before the failure written; a
transaction Bundle is all-or-nothing, so no Bundle is half written.

## Configuration

Environment variables (Terraform sets them on the Cloud Run job). Checked before any work.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PATIENT_ID` | yes | | the synthetic patient's identifier; must start with `SYN-`; letters, digits, `.`, `_`, `-`, at most 64. Set the same value for `fhir-proxy`, `v2-to-fhir` and the cuff simulator in a synthetic deployment, so the cuff readings land on the same Patient |
| `TIME_ZONE` | yes | | IANA zone of the synthetic person, e.g. `America/New_York`; fixed offsets refused (as `health-sync`) |
| `GCP_PROJECT_ID` | for writes | | project of the Healthcare dataset |
| `HEALTHCARE_LOCATION` | for writes | | dataset region |
| `HEALTHCARE_DATASET_ID` | for writes | | dataset id |
| `FHIR_STORE_ID` | for writes | | the main (health-data) FHIR store, not the audit store |
| `SYNTHETIC_SEED` | no | `1` | 0 to 2^63 − 1; another seed is another synthetic person |
| `FINAL_AFTER_MINUTES` | no | `60` | a window is `final` once it ended this long ago; multiple of 5 |
| `BATCH_WINDOWS` | no | `100` | windows per Bundle (1 to 1000) |
| `FHIR_TIMEOUT_SECONDS` | no | `60` | one FHIR request |
| `MAX_ATTEMPTS` | no | `4` | tries per FHIR request for 429, 5xx, 409 and network failures (backoff 1, 2, 4 … 16 s) |

FHIR credentials are Application Default Credentials: on Cloud Run, the `seeder` service
account (FHIR Resource Editor on the health-data store only; identity module). No secrets.

## Batching

`BATCH_WINDOWS = 100`, as `health-sync`: each Bundle is the Patient and Device conditional
creates plus 100 Observations and 100 Provenances, 202 entries. A day is three Bundles (about
580 entries with no gaps); 30 days about 85. Google says transactions over 1,000 entries may
time out; 202 is far inside. Each Bundle repeats the Patient and Device conditional creates
because its Observations refer to them by in-Bundle `urn:uuid` (T06).

The setup Bundle is sent once per run, before the first data Bundle, so the full Patient exists
before any ingest Bundle's conditional create looks for it (D11).

## For T20, T22, T24 and T25: deploying it

- **Image (T24).** Same pattern as the other services: install `libs/pulsebridge_fhir` and
  `libs/pulsebridge_gcp`, then this package; the command is `pulsebridge-seed`. Run as a
  non-root user. No port: it is a job, not a service.
- **Where it runs (T20 or T25).** A **Cloud Run job** (`google_cloud_run_v2_job`) with the
  `seeder` service account, the environment variables above, `max_retries = 0` (the next
  scheduled execution is the retry; every write is keyed), a task timeout of 600 s for the first
  seeding, and created only when `use_synthetic_data = true`.
- **First seeding (T25, the setup step).** Execute the job once with args `["--days", "30"]`
  **before** the ingest paths are switched on (the push subscription for `v2-to-fhir`), as the
  T25 AC requires of the setup step: it writes the full synthetic Patient first (D11). 30 days
  is about 8,600 windows and about 85 Bundles.
- **Keeping it current.** A Cloud Scheduler job **every hour** executes the job with args
  `["--hours", "3"]` (36 windows, one Bundle). Three hours covers the 60-minute `final` delay
  plus a missed run: a window written `preliminary` is rewritten `final` by a later run. Every
  15 minutes (like `health-sync`) also works but quadruples the writes for no visible gain.
  Scheduler calls the Cloud Run Admin API's `jobs.run` with an OAuth token (a Google API, so
  OAuth rather than OIDC), and its service account needs permission to run this job
  (`run.jobs.run`; which predefined role to grant on the job is for T20 to confirm against the
  Cloud Run documentation, not verified here).
- **Cost.** Nothing always-on: a job execution is billed for its seconds (a top-up takes a few
  seconds), Scheduler's first three jobs are free. Store writes: about 24 × 36 × 2 ≈ 1,700
  resources a day, about 2.6 MB of version history a day at about 1.5 KB each.
- **`health-sync` is not deployed** in a synthetic deployment, so its Scheduler job, secrets and
  OAuth app are not needed. The cuff path (cuff simulator, `v2-to-fhir`) works as normal, with
  the same `SYN-` `PATIENT_ID`.

## Logging

Events: `seed-run` (the report fields), `fhir-retry` (status, attempt, delay), `seed-forced`
(reason code), `bug` (exception type only). Never logged: heart-rate values, the patient
identifier, the seed, the device name, a response body, OperationOutcome diagnostics, or an
exception message. The data is synthetic, but the rule is the same as for the services that
handle real data. A test runs a seeding with retries and a refusal and checks the captured logs
and report for values and the identifier.

## Not verified (no GCP call was made)

- That Cloud Healthcare keeps `meta.tag` on conditional updates and conditional creates (the
  safety check also recognises the synthetic name, so a dropped tag does not lock the seeder
  out).
- That the store resolves in-Bundle references to the Patient the setup Bundle wrote (the same
  open point as for `health-sync`; T06 README, "Unverified server behaviour"). On the first real
  run, `entry_statuses` should show `Patient 201` once (setup, fresh store) and `Patient 200` for
  every data Bundle.
- The Cloud Run job and Scheduler wiring above (T20/T25 build it).

## Tests

`make test` runs `tests/`. No network, no credentials, no GCP: the FHIR store is the real
`HealthcareClient` over `pulsebridge_gcp.testing.FakeTransport`. All data is generated by the
seeder itself. Each behaviour was break-checked: the implementation was deliberately broken and
a test seen to fail.
