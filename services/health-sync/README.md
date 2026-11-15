# health-sync

Pulls five-minute heart-rate roll-ups from the Google Health API every 15 minutes, maps them to
FHIR R4 with T06's `heart_rate_transaction`, and writes them to the FHIR store as transaction
Bundles. It keeps no database: every run works out what to fetch from the clock.

```
Cloud Scheduler ──every 15 min, OIDC──> POST /sync ──┐
                                                     │ 1. read 3 secret files (mounted from Secret Manager)
oauth2.googleapis.com/token <── refresh_token grant ─┤ 2. access token (reused within the run)
health.googleapis.com  <── dataPoints:rollUp ────────┤ 3. 5-minute windows, last 3 hours, all pages
health.googleapis.com  <── dataPoints?pageSize=1 ────┤ 4. device name, platform, recording method
                                                     │ 5. T06 mapping, batches of 100 windows
FHIR store  <──────────────── executeBundle ─────────┘ 6. Patient/Device create-if-absent, Observation/Provenance upsert

fhir-proxy (T14) ──OIDC──> GET /status ──> token refresh attempt ──> connected | reconnect-needed | not-configured
```

## Layout

```text
services/health-sync/
  pyproject.toml          dependencies: pulsebridge-fhir, pulsebridge-gcp, Flask, gunicorn, requests
  health_sync/
    google_health.py      the Google Health API client and the OAuth refresh (injectable transport)
    sync.py               one run: ranges, cutoff, window cleanup, batching, writes, outcomes
    status.py             the stateless "is the connection working" check
    credentials.py        reads the three mounted secret files
    retry.py              bounded backoff inside a deadline
    config.py             environment variables, validated at start-up
    app.py                Flask: POST /sync, GET /status; JSON log lines
    backfill.py           the `backfill --days N` command
    logs.py               the JSON log formatter
    testing.py            test helpers (synthetic data only), like pulsebridge_gcp.testing
  tests/                  pytest; no network, no credentials, no GCP
```

Read `sync.py` first: its docstring is the flow, and the code runs in that order.

## Routes (for T14, T20 and T24)

Both routes are behind Cloud Run IAM: the service does not allow unauthenticated calls.

### `POST /sync`

One sync run. Called by Cloud Scheduler with an OIDC token for `scheduler-invoker`. The body is
ignored. The answer is the run report as JSON (counts, UTC range bounds and codes only):

```json
{"outcome": "synced", "reason": null,
 "ranges": [{"start": "2026-01-15T12:05:00Z", "end": "2026-01-15T15:05:00Z"}],
 "ranges_completed": 1, "complete_before": "2026-01-15T14:05:00Z", "pages": 1,
 "windows_fetched": 36, "windows_empty": 0, "windows_invalid": 0, "windows_written": 36,
 "windows_final": 24, "windows_preliminary": 12, "bundles_written": 1,
 "google_statuses": [200], "fhir_status": null, "fhir_issues": [], "token_refreshes": 1,
 "refresh_token_rotated": false,
 "entry_statuses": {"Device 200": 1, "Observation 200": 36, "Patient 200": 1, "Provenance 200": 36},
 "duration_ms": 1840}
```

#### What Scheduler sees

| `outcome` | HTTP | Meaning | What happens next |
| --- | --- | --- | --- |
| `synced` | 200 | every range written (zero windows is still `synced`) | next run in 15 minutes |
| `reconnect-needed` | **200** | the refresh token expired or was revoked (`invalid_grant`), the OAuth client was refused, or the scope is missing | nothing until the deployer reconnects (T11); `/status` reports it |
| `not-configured` | **200** | a secret file is missing or empty (the connect script has not run) | as above |
| `google-unavailable` | 503 | Google answered 429/5xx/408 or nothing, still after the bounded retries | next scheduled run (its range overlaps) |
| `fhir-unavailable` | 503 | FHIR store 429/5xx/network after retries, a persistent 409, or 401/403/404 (IAM or store) | next scheduled run |
| `google-error` | 500 | Google refused the request (another 4xx, a second 401) or answered malformed JSON | needs a person; logs give the status and Google's reason code |
| `fhir-rejected` | 500 | the FHIR store refused a Bundle (4xx; its OperationOutcome is logged without free text), or answered 2xx with a body that is not a Bundle (`fhir-malformed-response`) | needs a person |
| `mapping-failed` | 500 | T06 refused input that passed the window checks (a bug) | needs a person |

**Why 200 for reconnect-needed.** A retry cannot fix an expired grant, and every retry would hit
Google's token endpoint again with a token Google has already refused. Answering 200 ends the
attempt cleanly; the condition is visible in `/status` (and so on the dashboard) and in a
`WARNING` log line. It is not a crash and not a loop: the run makes exactly one token request and
stops.

**For T20, the Scheduler job:** `retry_config.retry_count = 0` (the next scheduled run is the
retry: its range overlaps), `attempt_deadline = "180s"` (Scheduler's default for HTTP targets),
and the Cloud Run request timeout at least 180 s. A run gives up retrying after
`RUN_DEADLINE_SECONDS` (150 s), inside that deadline.

### `GET /status`

Whether the Google Health connection works **now**, worked out statelessly: it reads the secret
files and attempts a token refresh, exactly as a run would. The access token is thrown away.

| `googleHealth` | HTTP | When |
| --- | --- | --- |
| `connected` | 200 | the refresh succeeded and the token carries the configured scope |
| `reconnect-needed` | 200 | `reason`: `refresh-token-invalid` (`invalid_grant`), `oauth-client-rejected` (`invalid_client`, `unauthorized_client`, `deleted_client`) or `scope-not-granted` |
| `not-configured` | 200 | `reason`: `secrets-missing:<names>`, e.g. `secrets-missing:refresh-token` |
| `unknown` | 503 | the check could not finish (token endpoint 429/5xx/unreachable, or an unexpected answer); `reason` names it |

```json
{"googleHealth": "reconnect-needed", "reason": "refresh-token-invalid", "checkedAt": "2026-01-15T15:07:30Z"}
```

- No secret material is ever in the response.
- A definite answer is reused for `STATUS_CACHE_SECONDS` (60 s) in memory, so a busy dashboard does
  not call Google's token endpoint on every page load. A sync run that finds `reconnect-needed`,
  or that refreshes successfully, updates the cached answer at once.
- **For T20/T22:** `fhir-proxy`'s service account needs `roles/run.invoker` on this service,
  besides `scheduler-invoker`. The identity README's open question "where `health-sync` records
  reconnect needed" is answered by "nowhere": it is recomputed on request, so no extra grant is
  needed.

## OAuth: how a run gets an access token

Three values live in Secret Manager (decision D2), written by the connect script (T11):

| Secret | What it is |
| --- | --- |
| OAuth client ID | the public name of the deployer's own OAuth app at Google |
| OAuth client secret | the app's password; proves the request comes from that app |
| refresh token | the user's long-lived permission, issued once when they clicked "Allow" |

Each run sends all three to `https://oauth2.googleapis.com/token` as a form
(`client_id`, `client_secret`, `refresh_token`, `grant_type=refresh_token`) and gets back an
`access_token` valid for about an hour (`expires_in` 3599 s, spike S3). That access token goes
in `Authorization: Bearer …` on each Google Health call and is reused for the rest of the run; it
is replaced 60 s before it expires (long backfills), and once after a `401`.

When Google answers the refresh with `invalid_grant`, the refresh token no longer works: the user
revoked access, or it expired. **In Testing status Google issues refresh tokens that expire after
7 days** (Google Identity, "Refresh token expiration": an External app in "Testing" "is issued a
refresh token expiring in 7 days"). Only a reconnect fixes it, so the service reports
`reconnect-needed`.

If a token response ever carries a new `refresh_token`, it is not stored (this service may only
read the secret; the identity README asks for a write grant only if T10 finds Google rotates
tokens). The report flags it (`refresh_token_rotated: true`) without the value.

## Secrets: what T20 must mount

Cloud Run mounts each secret **as a file** (a secret volume), version `latest`:

| Secret ID (identity module, `<prefix>-…`) | Mount path (one directory per secret) | Environment variable |
| --- | --- | --- |
| `google-health-client-id` | `/secrets/google-health-client-id/value` | `GOOGLE_HEALTH_CLIENT_ID_FILE` |
| `google-health-client-secret` | `/secrets/google-health-client-secret/value` | `GOOGLE_HEALTH_CLIENT_SECRET_FILE` |
| `google-health-refresh-token` | `/secrets/google-health-refresh-token/value` | `GOOGLE_HEALTH_REFRESH_TOKEN_FILE` |

The paths are a suggestion (Cloud Run does not allow two secrets in one mount directory, hence
three directories); the environment variables are what the code reads.

Why files and not environment variables:

- "When reading a volume, Cloud Run always fetches the secret value from the Secret Manager to use
  the value with the latest version"; environment variables "are resolved at instance startup
  time" (Cloud Run docs, "Configure secrets for services"). The files are read at the start of **every run**,
  so after the deployer reconnects, the next run (at most 15 minutes later) uses the new refresh
  token with no redeploy. That is how "read from Secret Manager at run time" is met. A test
  rewrites the file between two runs and checks the second run sends the new value.
- No Secret Manager client library and no secret names in code; access stays in Terraform
  (`roles/secretmanager.secretAccessor` on the three secrets, already granted by the identity
  module).
- Cloud Run does not check volume secrets at start-up, and neither does this service: a missing
  or empty file is logged once at start-up (`secrets-missing`, naming the secret, never a value)
  and the service starts, so `/status` can say `not-configured` instead of the service failing to
  deploy. The environment variables that hold the paths are required.

## Configuration

Environment variables, set by Terraform (T20). Nothing deployment-specific is in the code.
Everything is checked at start-up; an error names the variable, never its value.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GCP_PROJECT_ID` | yes | | project of the Healthcare dataset |
| `HEALTHCARE_LOCATION` | yes | | dataset region |
| `HEALTHCARE_DATASET_ID` | yes | | dataset id |
| `FHIR_STORE_ID` | yes | | the main FHIR store (not the audit store) |
| `PATIENT_ID` | yes | | the configured patient id, the one the setup step wrote (D11) |
| `TIME_ZONE` | yes | | the user's IANA zone, e.g. `America/New_York`; fixed offsets (`UTC-5`, `Etc/GMT+5`) are refused |
| `GOOGLE_HEALTH_CLIENT_ID_FILE` | yes | | absolute path of the mounted client ID |
| `GOOGLE_HEALTH_CLIENT_SECRET_FILE` | yes | | absolute path of the mounted client secret |
| `GOOGLE_HEALTH_REFRESH_TOKEN_FILE` | yes | | absolute path of the mounted refresh token |
| `GOOGLE_HEALTH_SCOPE` | no | `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` | the scope the token must carry (same value as T11's) |
| `LOOKBACK_MINUTES` | no | `180` | how far back each run re-requests; multiple of 5, at most 14 days |
| `FINAL_AFTER_MINUTES` | no | `60` | a window is `final` once it ended this long ago |
| `SYNC_INTERVAL_MINUTES` | no | `15` | the Scheduler interval; `LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL` is enforced |
| `BATCH_WINDOWS` | no | `100` | windows per Bundle (1 to 1000) |
| `HTTP_TIMEOUT_SECONDS` | no | `30` | one Google or OAuth request |
| `FHIR_TIMEOUT_SECONDS` | no | `60` | one `executeBundle` |
| `RUN_DEADLINE_SECONDS` | no | `150` | a scheduled run stops retrying after this |
| `MAX_ATTEMPTS` | no | `4` | tries per request for 429/5xx/408/network |
| `STATUS_CACHE_SECONDS` | no | `60` | `/status` answer reuse; `0` disables |
| `PORT` | no | 8080 in the command below | listening port (Cloud Run sets it) |

FHIR store credentials are Application Default Credentials: on Cloud Run, the `health-sync`
service account (FHIR Resource Editor on the main store; healthcare module README). It is used
only for the FHIR store; Google Health calls use a separate plain HTTPS session carrying the
user's access token, so the service account's token is never sent to a non-GCP API.

## Overlap and status

**Range.** Each run asks for `[floor(now) − LOOKBACK, floor(now))`, where `floor` rounds down to a
five-minute boundary in UTC. The window still in progress is left to the next run. With the
defaults that is 36 windows, one request, one page, one Bundle.

**Why aligned.** The API starts its first window at the start of the requested range ("Every
bucket starts sequentially from the beginning of your range", Google Health "Aggregate data"). An
unaligned start would shift every window, give every window a new key, and the overlap would
duplicate data instead of rewriting it.

**Why the overlap is safe.** Every write is keyed (T06): the Observation is a conditional update
on UUIDv5(patient, LOINC `8867-4`, window start in UTC), the Provenance a `PUT` by an id derived
from it, the Patient and Device conditional creates. A window seen by three runs is written three
times to the same Observation; the store's history keeps each version. A test compares the keys
of two overlapping runs.

**`preliminary` and `final`.** R4 `preliminary` is "an initial or interim observation: data may
be incomplete"; `final` is "complete". A window can still gain samples after a run, because the
watch syncs to the phone and the phone to Google late. The cutoff passed to T06 is
`complete_before = floor(now − FINAL_AFTER)`: windows that ended after it are `preliminary`.

```
run at 15:07:30 UTC (defaults)        cutoff 14:05            range end 15:05
range   |12:05 ---------------------------|------------------------| (15:05 still in progress)
status    final  final  ...  final  final | prelim ... prelim prelim
run at 15:22:30                            cutoff 14:20
range        |12:20 --------------------------------|-----------------------|15:20
             the 14:05-14:20 windows are rewritten here as final, with any late samples
```

A preliminary window becomes final on the first run at least 60 minutes after it ended; the
look-back guarantees that run still requests it. Any run from 60 to 175 minutes after a window
ended finalises it, so about seven consecutive missed runs are tolerated. A longer outage is
recovered with the backfill. Windows inside the overlap but before the cutoff are rewritten as
`final` with the latest values, so a late sample is never lost; it only arrives after the label.

**Deviation from the literal AC wording.** The AC says preliminary windows "become final on the
next run". With `FINAL_AFTER = LOOKBACK − SYNC_INTERVAL` (165) that is exactly what happens, but
one missed run would then leave windows `preliminary` for good. The default (60) trades the
literal "next run" for tolerance of missed runs; both are one variable apart.

**Why a fixed look-back and not "read the newest stored Observation".** It needs no read before
each run, no FHIR search permissions beyond the editor role, and cannot be confused by a partial
earlier write. The cost is rewriting about 36 windows per run (about 7,000 Observation and
Provenance writes a day, each a new version in the store's history; see "Cost"). A long outage is
handled by the backfill, which is the same code with a longer range.

## Batching

`BATCH_WINDOWS = 100` windows per transaction Bundle: 100 Observations + 100 Provenances + the
Patient and Device conditional creates = 202 entries, about 330 KB of compact JSON. A day (288
windows) is three Bundles (202 + 202 + 178 = 582 entries; the AC's "about 580"). Why 100:

- Google: transaction bundles "exceeding 1,000 entries might time out", over 4,500 are rejected,
  and the request limit is 50 MB (Cloud Healthcare "Quotas and limits"). 202 entries is far inside.
- A transaction is all-or-nothing; a failed Bundle loses at most 100 windows of work, all
  rewritten by the next run.
- A normal run (36 windows) is a single Bundle.

Every Bundle carries the Patient and Device conditional creates because a Bundle's Observations
refer to them by in-Bundle `urn:uuid` (T06). Batches are independent: if batch 2 fails, batch 1
stays written and the run reports the failure; re-running rewrites both.

## Backfill

```bash
health-sync-backfill --days 30            # or: python -m health_sync.backfill --days 30
```

The same configuration, mapping, keys, batching and retries as a run. The range is the last
`N × 24` hours ending at the last five-minute boundary, split into requests of at most 14 days
(the API's limit for heart-rate roll-ups), oldest first. The split is in physical (UTC) time, so
a piece crossing a daylight-saving change is still exactly 336 hours; a local-calendar "14 days"
across the autumn change would be 337 hours and refused. Example, 30 days from 2026-02-20T12:03Z:
`[01-21T12:00, 02-04T12:00)`, `[02-04T12:00, 02-18T12:00)`, `[02-18T12:00, 02-20T12:00)`.

- No overall deadline (per-request retries are still bounded). One access token for the whole
  backfill, refreshed when it nears expiry.
- Logs go to stderr, the report (JSON) to stdout. Exit codes: `0` synced; `3` reconnect needed
  or not configured; `1` another failure (the report says how many ranges completed; re-run);
  `2` bad arguments or configuration. `--days` is 1 to 366.
- Safe to re-run: every window lands on the same Observation.
- Where to run it (T20/T25 to decide): as a Cloud Run **job** from the same image with the same
  environment and secret mounts (`health-sync-backfill --days N` as the command), or locally with
  the same environment variables pointing at files the deployer creates and deletes themselves.

## Device and data source

Roll-ups carry no `dataSource` (spike S3), but T06's Provenance and Device want the device name,
the platform and the recording method. Per range with data, health-sync makes **one cheap raw
call**, `GET …/heart-rate/dataPoints?filter=<range>&pageSize=1`, and uses the newest point's
`dataSource.device.displayName`, `platform` and `recordingMethod`, exactly as reported:

- A part Google does not report (or reports as `*_UNSPECIFIED` / `UNKNOWN`) becomes `None`, and
  T06 then asserts nothing about it (no custodian agent, no recording-method role).
- With no `displayName`, the Device is named `Unnamed Google Health device`, which claims nothing
  about the hardware (T06's Device needs a name). A watch that later reports its name gets its
  own Device.
- The roll-up reconciles every source; the device of the newest raw point is the one credited
  for the range. With one watch (the spike's case) that is exact; with several devices it is an
  approximation, noted here rather than hidden.
- The raw point's heart-rate value is read over the wire but never used, stored or logged.

## For T14: the trace panel's "Google Health JSON it came from"

No raw API response is stored anywhere. The roll-up window is rebuilt exactly from the stored
Observation (a test does this and compares with the input item):

| Roll-up field | From |
| --- | --- |
| `startTime`, `endTime` | `effectivePeriod.start` / `.end`, converted to UTC (`Z`, whole seconds); also in the Provenance's `entity[role=source].what.identifier.value` as `<start>/<end>` in UTC |
| `heartRate.beatsPerMinuteAvg` | `valueQuantity.value` |
| `heartRate.beatsPerMinuteMin` | `component[code=96935-2].valueQuantity.value` |
| `heartRate.beatsPerMinuteMax` | `component[code=55426-1].valueQuantity.value` |

T06 keeps the average's precision: Google sends a JSON double (`format: double` in the discovery
document), Python reads it as a float and writes it back with the shortest exact representation,
so `73.41666666666667` is sent to the store unchanged. Not verified: whether the Cloud Healthcare
FHIR store returns decimals exactly as sent (FHIR `decimal` is meant to preserve precision).

## Logging

One JSON object per line on stdout (stderr for the backfill). Logged: `event` (`sync-run`,
`backfill-run`, `status-check`, `google-retry`, `fhir-retry`, `secrets-missing`, `bug`),
`outcome` and `reason` codes, the UTC range bounds and cutoff, counts (pages, windows fetched,
empty, invalid, written, final, preliminary, Bundles), HTTP statuses, retry attempts and delays,
response status counts per resource type, durations. Never logged: heart-rate values, tokens,
the client ID or secret, the patient id, the device name, response bodies, exception messages
from the mapping (they can quote a value), or Google's free-text error descriptions. A test runs
a sync with a 401, retries and a reconnect and asserts that no log line or answer contains any
synthetic secret, the patient id, the device name or the distinctive heart-rate values.

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 "health_sync.app:create_app()"
```

- Honours `PORT`. `--timeout 0` leaves request deadlines to Cloud Run.
- Routes: `POST /sync`, `GET /status`.
- For T24's container: install `libs/pulsebridge_fhir` and `libs/pulsebridge_gcp` before this
  package (they are not on PyPI). The image also provides the `health-sync-backfill` command.
- Scale to zero is fine: nothing is kept between requests except the 60-second status cache.

## Cost

Idle cost is zero (Cloud Run scales to zero). Each run makes three Google calls and one FHIR
transaction of about 74 entries; 96 runs a day is about 7,100 FHIR writes and the same number of
new resource versions (versioning is on, healthcare module), roughly 7 MB of history a day at
about 1 KB a version. Lower `LOOKBACK_MINUTES` to reduce it; the trade-off is less tolerance of
missed runs and late phone syncs.

## What is confirmed, and where from

| Fact | Source |
| --- | --- |
| `rollUp` body fields `range.startTime/endTime`, `windowSize` (duration string), `pageSize` (max 10,000, default 1,440), `pageToken` in the body with other fields unchanged; response `rollupDataPoints`, `nextPageToken` | discovery document (v4) |
| `range` is closed-open; 14-day maximum for `heart-rate` | discovery document, Google Health guide "Aggregate data", and spike S3 |
| windows start at the range start; a range not a multiple of the window truncates the last window | Google Health guide "Aggregate data" |
| newest window first | guide "Aggregate data" and spike S3 |
| windows with no data are left out | spike S3 only |
| `HeartRateRollupValue` min, avg, max are doubles | discovery document |
| `dataPoints.list` `filter` on `heart_rate.sample_time.physical_time` with `>=`, `<`, `AND`; newest first; `dataSource.device.displayName`, `platform`, `recordingMethod` enums | discovery document; spike S3 for the live shape |
| token endpoint request fields; response `access_token`, `expires_in`, `scope`, `token_type`; `invalid_grant` when "the token may have expired or has been invalidated" | Google Identity, "Using OAuth 2.0 for Web Server Applications" |
| 7-day refresh tokens in Testing status | Google Identity, "Using OAuth 2.0 to Access Google APIs", "Refresh token expiration"; also spike S3 (`refresh_token_expires_in` 604799) |
| 429 for rate limits ("back off and retry"); per-user limit 300 requests a minute | Google Health guide "Rate limits"; error catalogue (`RESOURCE_EXHAUSTED` 429, `MISSING_OAUTH_SCOPE` 403, `UNAUTHENTICATED` 401) |
| Cloud Run volume secrets read the latest version on every read; env vars at start-up; no start-up check for volumes | Cloud Run docs, "Configure secrets for services" |
| `executeBundle` limits (1,000 entries may time out, 4,500 rejected, 50 MB) | Cloud Healthcare "Quotas and limits" |
| Scheduler `attemptDeadline` default 3 min for HTTP; `retryCount = 0` waits for the next run | Cloud Scheduler REST reference |

**Not verified (no call to Google or GCP was made in this task):** the exact JSON of Google's
`401`/`403`/`429` error bodies (handled whether or not they carry an `ErrorInfo` reason); whether
Google ever returns a new refresh token on refresh; whether the token response always carries
`scope` (if absent, the scope check is skipped); whether the API accepts a range ending at the
current five-minute boundary with a still-filling window (the range ends before it, so this is
not relied on); decimal round-tripping in the FHIR store.

## Still to confirm on the first real write (AC, T25)

T06's ingest Bundle assumes the store resolves the in-Bundle `urn:uuid` references to the Patient
that the setup step already wrote (the conditional create matches and creates nothing). This
task made no GCP calls, so it is **not yet confirmed**. On the first real sync, check:

1. The run report's `entry_statuses` shows `Patient 200` (matched), not `Patient 201` (created a
   second Patient). `201` on the very first run before setup is expected; after setup it is not.
2. `GET Observation?identifier=urn:pulsebridge:observation|<a key>` returns one Observation whose
   `subject.reference` is the setup Patient's `Patient/<id>`.
3. `GET Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>` returns exactly one Patient.

If any fails, switch to the fixed-ID fallback in the T06 README ("Unverified server behaviour,
and the fallback"); that is a change to `libs/pulsebridge_fhir`, not to this service.

## Tests

`make test` runs `tests/`. No network, no credentials, no GCP: Google and the OAuth endpoint are
scripted with `pulsebridge_gcp.testing.FakeTransport`, the FHIR store is the real
`HealthcareClient` over another `FakeTransport`, and the secret files are written to a temporary
directory. Every roll-up item is invented in the shape spike S3 recorded, and every credential is
assembled at run time from obviously synthetic pieces. Each behaviour was break-checked: the
implementation was deliberately broken and a test seen to fail.
