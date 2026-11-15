# seed

Writes plausible, **synthetic** five-minute heart-rate windows for a **synthetic** patient into
the FHIR store, through the same T06 mapping `health-sync` uses. It backs the
`use_synthetic_data` flag: a stranger who clones the repository can see a filled dashboard
without a Pixel Watch, a Google Health OAuth app or any real health data.

**For deployments with `use_synthetic_data = true` only.** In such a deployment `health-sync` is
not deployed, and the seeder is the wearable source and the setup step (decision D11). Do not
run it against a store that holds a real person's data; it refuses to touch a Patient that looks
like a real person's record (see "Safety").

```
pulsebridge-seed --days 30
  1. range       [floor(until) - 30 days, floor(until)), five-minute boundaries in UTC
  2. generate    one item per window: a pure function of (seed, TIME_ZONE, window start)
  3. safety      GET Patient?identifier=urn:pulsebridge:patient|SYN-…   (one search, fails closed)
  4. setup       POST executeBundle: PUT Patient?identifier=…, PUT Device?identifier=…
                 (only when there is no Patient yet, or an incomplete one: D11, written once)
  5. read once   GET Observation?subject=Patient/<id>&code=…|8867-4&date=ge…&date=lt…  (paged)
  6. data        POST executeBundle × N: T06 heart_rate_transaction, 100 windows each,
                 only the windows that are new, newly final, or changed
```

## Layout

```text
services/seed/
  pyproject.toml           dependencies: pulsebridge-fhir, pulsebridge-gcp
  pulsebridge_seed/
    generator.py           the synthetic person: windows in the Google Health roll-up shape
    synthetic.py           what marks the data as synthetic; which stored Patients are "ours"
    seeder.py              one run: range, cutoff, safety check, setup Bundle, batches, report
    testing.py             test helpers: an in-memory FHIR store behind the real client
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
parts…])`, its top 53 bits read as a fraction in `[0, 1)` (exact in a float, never 1.0). There is no `random` module, no `hash()` (which Python
salts per process) and no generator state, so the value of a window does not depend on when the
seeder ran, which range it was asked for, or how many numbers were drawn before. The overlap of
two runs is identical, window for window (tested with different `--until` values and an odd
start such as 13:35). A golden test pins the values of one fixed day: a change to the model that
changes values must also bump `MODEL_VERSION` (`generator.py`), which is hashed into every
number, so old and new values never mix under one seed.

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
| Security label | HL7 v3 ActReason `HTEST` "test health data", system `http://terminology.hl7.org/CodeSystem/v3-ActReason` | `meta.security` on every resource; searchable with `_security` |

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
- **`HTEST` is a security label, so it goes in `meta.security`.** `meta.security` holds
  security labels; R4 binds it (extensibly) to "All Security Labels", and HL7's security-labels
  page lists purpose-of-use codes such as `HTEST` "test health data" among them. `meta.tag` is
  for workflow tags (an example binding, "Common Tags"); an earlier draft put `HTEST` there, and
  a label in either place is reported (`stored_patient_labelled`). T06's builders take no `meta`, so
  the seeder adds the label to each resource of T06's Bundle (`synthetic.mark_synthetic`); it
  changes no request URL or key. Whether Cloud Healthcare keeps `meta.security` on conditional
  writes is not verified (no GCP call was made); the safety check does not use it at all.
- **For T15 (share to OpenEMR).** `patient_for_sharing` sends the clinic only the `PT`
  identifier, the official name, gender and birth date, and drops `meta`. After a share, the
  synthetic name (`Synthetic`, `Demo`) and the `SYN-` identifier are the only markers OpenEMR
  holds; the security label stays in our store.

## Safety

The setup Bundle **replaces** the Patient with the configured identifier (`PUT`, D11), and the
ingest Bundles add heart rate to that Patient's record. Run against a real person's record, that
would overwrite their demographics and mix invented readings into their chart. Three guards:

1. **`PATIENT_ID` must start with `SYN-`**, or the seeder does not start (checked when the
   configuration is read and again in `SeedConfig` itself). A real deployment's patient
   identifier comes from its own configuration and has no reason to look like that.
2. **One search before any write**: `Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>`.
   It **fails closed**: unless the answer is a `Bundle` of type `searchset` whose `entry` is
   absent or a list of Patients (an `OperationOutcome` with `search.mode` `outcome` is allowed),
   and whose `total`, when given, is no larger than the Patients returned, the run stops as
   `fhir-rejected` and nothing is written. An empty `200`, a `200` OperationOutcome or an
   `entry` object is never read as "no Patient". A failed search (5xx after retries, 401/403,
   404) also stops the run before any write.
   - no Patient: a fresh store; the setup Bundle writes it (`safety_check: no-patient`);
   - one Patient the seeder may write over (`synthetic.refusal_reason`). The same content rule
     applies to **every** Patient, labelled `HTEST` or not:
     - every identifier is a `SYN-` value in `urn:pulsebridge:patient`, and one of them is the
       configured `PATIENT_ID` (a search answer about another `SYN-` Patient is not ours);
     - every name (if any) is the synthetic name;
     - gender and birth date are absent or the seeder's own (`other`, 1980-01-01);
     - nothing else beyond the server-added `id` and `meta` (no telecom, address, Google
       Health identifier…).

     The label is reported (`stored_patient_labelled`) but never makes a Patient acceptable by
     itself: a label can outlive the content it described, for example if the connect script
     were run with a `SYN-` id and the store kept the old `meta.security` over its `PUT`. The
     rule accepts the identifier-only Patient that `v2-to-fhir` (or `health-sync`) creates by
     conditional create when an ingest reaches the store before the seeder, so that case does
     not lock the seeder out. Then go on: `synthetic-patient` if it is already complete (T06
     `check_complete_patient`), `synthetic-incomplete` if not, and the setup Bundle completes it;
   - anything else: **refuse**, exit code 4, nothing written (`refused`), with reason
     - `patient-name-not-synthetic` when the name is the only problem. In a synthetic
       deployment this almost always means the cuff simulator sent another PID-5: configure it
       with `PATIENT_FAMILY_NAME=Synthetic`, `PATIENT_GIVEN_NAME=Demo` and the same `SYN-`
       `PATIENT_ID`, then remove the wrong Patient (or use `--force`);
     - `patient-not-synthetic` for any other content (a real-looking record, another `SYN-`
       identifier, a Google Health identifier, other demographics);
     - `patients-duplicated` when several Patients have the identifier.
   - `--force` writes anyway and replaces the record (it logs a `seed-forced` warning). Use it
     only to take over a store that you know holds nothing real.
3. **This README and `--help` say** the seeder is for `use_synthetic_data = true` deployments.
4. **Never run the Google Health connect script (T11) with a `SYN-` `PATIENT_ID`.** Its setup
   step `PUT`s the real person's full Patient onto that identifier and replaces the synthetic
   Patient; the seeder would then refuse (`patient-not-synthetic`), but the real record would
   already sit on an identifier the synthetic deployment's readings point at.

Why one search and not more: the Patient is the resource a wrong run would damage, and every
heart-rate Observation of a patient refers to that Patient, so "a Patient that is not ours" is
exactly the case where non-synthetic data could exist. A search of Observations without the
label (`_security:not`) or by chained patient identifier would be more direct but relies on
search features not verified on Cloud Healthcare; `Patient?identifier=` is what `health-sync`
already relies on.

The search needs read access on the Patient: the `seeder` service account has FHIR Resource
Editor on the health-data store (identity module), which includes search.

## Re-running

- **No duplicates.** Every write is keyed (T06): Patient and Device by identifier, each
  Observation a conditional update `PUT Observation?identifier=urn:pulsebridge:observation|<UUIDv5
  of patient, 8867-4, window start>`, each Provenance `PUT Provenance/<UUIDv5 of that key>`.
  Two runs over the same range into empty stores send identical entries (tested), except
  Provenance `recorded` (the write time).
- **The setup Bundle is written once** (D11): only when the safety search found no Patient, or
  an incomplete one (or with `--force`). Later runs send only ingest Bundles, whose Patient and
  Device entries are conditional creates that match and change nothing.
- **Read once, write what changed.** After the safety check, one search reads the heart-rate
  windows already stored for the range (`pulsebridge_gcp.stored`, following `next` links). A window is
  written only if it is new, stored `preliminary` and now due `final`, or stored with another
  end or other values (only after a change of `--seed` or `TIME_ZONE`). As in `health-sync`:
  a window stored `final` that changed and is complete now is written **`amended`** (R4:
  "Subsequent to being Final, the observation has been modified"; T06's
  `heart_rate_transaction(amended=…)`, counted in `windows_amended`); one stored `final` that
  changed but would be `preliminary` now (for example after `FINAL_AFTER_MINUTES` was raised)
  is **not written** (`windows_held_final`), because R4 has no path from `final` back to
  `preliminary` and the window may already have been shared; a later run writes it `amended`.
  A window stored `entered-in-error` or `cancelled` is left alone. A key stored twice counts as not stored, so
  that window is written and its conditional update reports the duplicate. If the search fails
  or answers something unusable, every window is written (`stored_read: failed:…`): reading
  saves writes, it is never a condition for writing. The pages are bounded by the range, so a
  server that keeps sending `next` links cannot loop the run. This logic is **shared with
  `health-sync`** (T35): the read is `pulsebridge_gcp.stored.stored_windows` (here with one
  client for every page and `max_pages_for_range`), the decision `pulsebridge_fhir.stored.plan_writes`.
- **History is not rewritten.** Values depend only on the seed, the zone and the window start,
  so a later run over an overlapping range finds the same values and skips them.
- **`preliminary` and `final`** follow `health-sync`'s rule: `complete_before = floor(now -
  FINAL_AFTER_MINUTES)` (60), using the real clock, not `--until` (tested with a backdated
  `--until`). A window that ended within the last hour is `preliminary`; the next run that
  covers it after that writes it `final`, once.
- **How long `--hours` must be.** A window that ends at time *t* is due `final` from *t* +
  `FINAL_AFTER`, and stays inside a run's range until *t* + `--hours` − 5 minutes. So some run
  must fall in between: with runs every *I*, `--hours` ≥ `FINAL_AFTER_MINUTES` + *I* + 5 minutes
  is the minimum, and **`--hours` ≥ `FINAL_AFTER_MINUTES` + 2 × *I* + 5 minutes** tolerates one
  missed run. Hourly with the default 60-minute `FINAL_AFTER`: at least 185 minutes, so
  `--hours 4`. A window left `preliminary` by a longer outage is finalised by any later run
  with a longer range (for example `--days 2`).

## Running it

```bash
pulsebridge-seed --days 30          # or: python -m pulsebridge_seed.cli --days 30
pulsebridge-seed --hours 4          # hourly top-up (see "How long --hours must be")
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
 "windows_unchanged": 0, "windows_new": 8395, "windows_changed": 0, "windows_finalised": 0,
 "windows_amended": 0, "windows_held_final": 0, "windows_withdrawn_kept": 0,
 "windows_written": 8395, "windows_final": 8383, "windows_preliminary": 12,
 "bundles_planned": 84, "bundles_written": 84, "setup_written": true,
 "safety_check": "no-patient", "stored_patient_labelled": null, "stored_read": "patient-not-found",
 "fhir_status": null, "fhir_issues": [],
 "entry_statuses": {"Device 200": 85, "Observation 201": 8395, "Patient 200": 84, "Patient 201": 1, "Provenance 201": 8395}}
```

(Illustrative counts.) The report and logs carry counts, UTC bounds and codes only: no heart-rate
values, no patient identifier, no seed.

| Exit | Meaning |
| --- | --- |
| 0 | `seeded`, or `dry-run` |
| 1 | a FHIR failure: `fhir-unavailable` (429/5xx/409/network after retries), `fhir-misconfigured` (401/403 `fhir-permission-denied`, 404 `fhir-store-missing`), `fhir-rejected` (another 4xx, its OperationOutcome summarised without free text; a malformed 2xx; or a safety-search answer that cannot be trusted, reason `safety-search-…`); or an unexpected error (outcome `bug`, reason the exception type only, reported with the counts so far). Re-run: every write is keyed |
| 2 | bad arguments or configuration (the message names the variable, never its value) |
| 4 | `refused` by the safety check |

A run stopped part-way leaves the setup Bundle (if it was due) and the Bundles before the
failure written; a transaction Bundle is all-or-nothing, so no Bundle is half written.

## Configuration

Environment variables (Terraform sets them on the Cloud Run job). Checked before any work.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PATIENT_ID` | yes | | the synthetic patient's identifier; must start with `SYN-`; letters, digits, `.`, `_`, `-`, at most 64. Set the same value for `fhir-proxy`, `v2-to-fhir` and the cuff simulator in a synthetic deployment, so the cuff readings land on the same Patient. For the cuff simulator also set `PATIENT_FAMILY_NAME=Synthetic` and `PATIENT_GIVEN_NAME=Demo` (PID-5), so a Patient that `v2-to-fhir` creates first carries only the synthetic name and the seeder accepts it |
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

When it is due, the setup Bundle is sent before the first data Bundle, so the full Patient
exists before any ingest Bundle's conditional create looks for it (D11).

## For T20, T22, T24 and T25: deploying it

- **Image (T24).** Same pattern as the other services: install `libs/pulsebridge_fhir` and
  `libs/pulsebridge_gcp`, then this package; the command is `pulsebridge-seed`. Run as a
  non-root user. No port: it is a job, not a service.
- **Where it runs (T20 or T25).** A **Cloud Run job** (`google_cloud_run_v2_job`) with the
  `seeder` service account, the environment variables above, `max_retries = 0` (the next
  scheduled execution is the retry; every write is keyed), and created only when
  `use_synthetic_data = true`.
- **Task timeout: size it with the range.** 30 days is about 8,600 windows and 85 Bundles;
  `--days 366` is about 105,000 windows and **about 1,050 Bundles**. The time per Bundle on
  Cloud Healthcare was not measured; allowing about 10 s per Bundle gives about 15 minutes
  for 30 days and about 3 hours for 366 days (Cloud Run jobs allow up to 24 hours per task).
  An hourly top-up is one Bundle.
- **First seeding (T25, the setup step).** Execute the job once with args `["--days", "30"]`
  and let it **finish before the cuff path's push subscription is enabled** (and before any
  other ingest), as the T25 AC requires of the setup step: it writes the full synthetic Patient
  first (D11). If an ingest does get there first, `v2-to-fhir` creates an identifier-only
  Patient; the seeder accepts it and completes it with the setup Bundle (see "Safety"), so the
  order is recoverable, but two first contacts at once can still create two Patients (T06
  README), which only a person can fix.
- **Keeping it current.** A Cloud Scheduler job **every hour** executes the job with args
  `["--hours", "4"]` (48 windows; with the read-once step each run writes about 24: the 12 of
  the last hour as `preliminary`, the 12 of the hour before as `final`; one Bundle). The rule
  for `--hours` is in "How long `--hours` must be". Every 15 minutes (like `health-sync`) also
  works but adds runs and searches for no visible gain. Scheduler calls the Cloud Run Admin
  API's `jobs.run` with an OAuth token (a Google API, so OAuth rather than OIDC), and **its
  service account needs permission to run this job** (`run.jobs.run`); which predefined role
  to grant on the job is for T20 to confirm against the Cloud Run documentation (not verified
  here).
- **Cost.** Nothing always-on: a job execution is billed for its seconds (a top-up takes a few
  seconds); Scheduler's first three jobs are free. Every write, hourly with `--hours 4`:

  | Write | Per day | New versions |
  | --- | --- | --- |
  | Observation | about 550: each of about 275 windows twice (`preliminary`, then `final`) | about 550 |
  | Provenance | about 550, one per Observation write | about 550 |
  | Patient and Device conditional creates | 48 (two per Bundle, 24 Bundles) | none: they match the stored ones |
  | setup Bundle | none after the first seeding | |
  | searches | 48 (the safety search and the stored-window search per run) | |

  About 1,100 resource versions a day, about 1.7 MB of version history at about 1.5 KB each.
  Without the read-once step the same schedule wrote each window four times (once
  `preliminary`, then `final` on every later run that still covered it): about 2,200 resource
  versions and 3.3 MB a day; with `--hours 3`, three times (about 1,650 and 2.5 MB).
- **`health-sync` is not deployed** in a synthetic deployment, so its Scheduler job, secrets and
  OAuth app are not needed. The cuff path (cuff simulator, `v2-to-fhir`) works as normal, with
  the same `SYN-` `PATIENT_ID`.

## Logging

Events: `seed-run` (the report fields; `ERROR` with outcome `bug` for an unexpected error,
still with its counts), `fhir-retry` (status, attempt, delay), `seed-forced` (reason code),
`stored-read-failed` (HTTP status), `stored-duplicate-key` (a count). Never logged: heart-rate values, the patient
identifier, the seed, the device name, a response body, OperationOutcome diagnostics, or an
exception message. The data is synthetic, but the rule is the same as for the services that
handle real data. A test runs a seeding with retries and a refusal and checks the captured logs
and report for values and the identifier.

## Not verified (no GCP call was made)

- That Cloud Healthcare keeps `meta.security` on conditional updates and conditional creates.
  Nothing depends on it: the safety check decides on content alone.
- The read-once search features on Cloud Healthcare (`_elements`, `date` on `effectivePeriod`,
  `_count=1000`, `next` links on the store's own base URL); the same open point as
  `health-sync` (its README, "For T25"). If one is refused, the run writes everything. On the
  second real run, check `stored_read: ok` and `windows_unchanged > 0`.
- That the store resolves in-Bundle references to the Patient the setup Bundle wrote (the same
  open point as for `health-sync`; T06 README, "Unverified server behaviour"). On the first real
  run, `entry_statuses` should show `Patient 201` once (setup, fresh store) and `Patient 200` for
  every data Bundle.
- The Cloud Run job and Scheduler wiring above (T20/T25 build it).

## Tests

`make test` runs `tests/`. No network, no credentials, no GCP: the FHIR store is the real
`HealthcareClient` over `pulsebridge_seed.testing.StoreFake`, a `FakeTransport` that keeps what
it is sent and answers the searches from it (so a second run sees the first run's writes). All data is generated by the
seeder itself. Each behaviour was break-checked: the implementation was deliberately broken and
a test seen to fail.
