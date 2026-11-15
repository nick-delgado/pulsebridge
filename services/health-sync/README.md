# health-sync

Pulls five-minute heart-rate roll-ups from the Google Health API every 15 minutes, maps them to
FHIR R4 with T06's `heart_rate_transaction`, and writes the windows that are new or changed to
the FHIR store as transaction Bundles. It keeps no database: every run works out what to fetch
from the clock, and what to write from one read of the store.

```
Cloud Scheduler ──every 15 min, OIDC──> POST /sync ──┐
                                                     │ 1. read 3 secret files (mounted from Secret Manager)
oauth2.googleapis.com/token <── refresh_token grant ─┤ 2. access token (reused within the run)
health.googleapis.com  <── dataPoints:rollUp ────────┤ 3. 5-minute windows, last 3 hours, all pages
FHIR store  <── Patient? / Observation? searches ────┤ 4. what is already stored (read once)
health.googleapis.com  <── dataPoints?pageSize=1 ────┤ 5. device name, platform, recording method
                                                     │ 6. T06 mapping of the new or changed windows, batches of 100
FHIR store  <──────────────── executeBundle ─────────┘ 7. Patient/Device create-if-absent, Observation/Provenance upsert

fhir-proxy (T14) ──OIDC──> GET /status ──> token refresh attempt ──> connected | reconnect-needed | not-configured | unknown
```

## Layout

```text
services/health-sync/
  pyproject.toml          dependencies: pulsebridge-fhir, pulsebridge-gcp, Flask, gunicorn, requests
  health_sync/
    google_health.py      the Google Health API client and the OAuth refresh (injectable transport)
    sync.py               one run: ranges, cutoff, window cleanup, what to write, batching, outcomes
    stored.py             the FHIR searches of its own: the Patient's id, stranded windows
    status.py             the stateless "is the connection working" check
    credentials.py        reads the three mounted secret files
    retry.py              bounded backoff and the run deadline
    config.py             environment variables, validated at start-up
    app.py                Flask: POST /sync (one at a time), GET /status; JSON log lines
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
 "windows_fetched": 36, "windows_empty": 0, "windows_invalid": 0,
 "windows_unchanged": 33, "windows_new": 3, "windows_changed": 0, "windows_finalised": 3,
 "windows_amended": 0, "windows_withdrawn_kept": 0, "windows_held_final": 0, "windows_written": 6, "windows_final": 3, "windows_preliminary": 3,
 "bundles_written": 1, "stored_read": "ok", "stranded_preliminary": 0,
 "google_statuses": [200], "fhir_status": null, "fhir_issues": [], "token_refreshes": 1,
 "refresh_token_rotated": false, "scope_not_listed": false,
 "entry_statuses": {"Device 200": 1, "Observation 200": 3, "Observation 201": 3, "Patient 200": 1,
                    "Provenance 200": 3, "Provenance 201": 3},
 "duration_ms": 1840}
```

Only one run at a time per instance: a `POST /sync` that arrives while a run is in progress
answers at once with `200 {"outcome": "already-running"}` and does nothing.

#### What Scheduler sees

| `outcome` | HTTP | Meaning | What happens next |
| --- | --- | --- | --- |
| `synced` | 200 | every range done (zero windows written is still `synced`) | next run in 15 minutes |
| `reconnect-needed` | **200** | the refresh token expired or was revoked (`invalid_grant`), the OAuth client was refused, or the API answered `MISSING_OAUTH_SCOPE` | nothing until the deployer reconnects (T11); `/status` reports it |
| `not-configured` | **200** | a secret file is missing or empty (`secrets-missing:…`), or present but unusable (`secret-unreadable:…`) | as above |
| `already-running` | 200 | another run is in progress on this instance | nothing; that run carries on |
| `google-unavailable` | 503 | Google answered 429/5xx/408 or nothing, still after the bounded retries | next scheduled run (its range overlaps) |
| `fhir-unavailable` | 503 | FHIR store 429/5xx/network after retries (`fhir-write-failed`), or a persistent 409 (`fhir-conflict`) | next scheduled run |
| `deadline-reached` | 503 | the run stopped before starting a range or Bundle (`before-range`, `before-bundle`) because too little time was left | next scheduled run picks up the rest |
| `google-error` | 500 | Google refused the request (another 4xx, a second 401) or answered malformed JSON | needs a person; logs give the status and Google's reason code |
| `fhir-misconfigured` | 500 | the FHIR store answered 401/403 (`fhir-permission-denied`: the role is missing, or still propagating after an apply) or 404 (`fhir-store-missing`) | needs a person |
| `fhir-rejected` | 500 | the FHIR store refused a Bundle (4xx, `fhir-refused-bundle`; its OperationOutcome is logged without free text), or answered 2xx with a body that is not a Bundle (`fhir-malformed-response`) | needs a person |
| `mapping-failed` | 500 | T06 refused input that passed the window checks (a bug) | needs a person |

**Why 200 for reconnect-needed.** A retry cannot fix an expired grant, and every retry would hit
Google's token endpoint again with a token Google has already refused. Answering 200 ends the
attempt cleanly; the condition is visible in `/status` (and so on the dashboard) and in a
`WARNING` log line. It is not a crash and not a loop: the run makes exactly one token request and
stops.

#### Deadline: one run at a time

If a run outlived Cloud Scheduler's attempt deadline, Scheduler could start the next run while
the first is still writing. Three things prevent that:

1. **Every request's timeout is cut to the time left** before `RUN_DEADLINE_SECONDS` (150 s):
   Google and OAuth calls get `min(HTTP_TIMEOUT_SECONDS, left)`, each FHIR request
   `min(FHIR_TIMEOUT_SECONDS, left)`. No request is started with less than 2 s left, and no
   backoff wait is taken that would leave less. (An HTTP timeout limits each wait for data, not
   a whole transfer, so a server that trickles bytes could still overrun by one request.)
2. **No range or Bundle is started with less than 10 s left**; the run then ends as
   `deadline-reached` (503), and the next run, whose range overlaps, picks up the rest.
3. **Reads never starve writes.** Each FHIR search has its own 10 s timeout, and the searches
   are skipped altogether (`stored_read: "skipped-for-time"`, every window written) when the time
   left is less than three searches (30 s) plus, per Bundle, one device lookup and one
   `executeBundle` at their full timeouts (30 s + 60 s). With a FHIR store answering in 45 s, the
   first search times out after 10 s and the run writes everything (a test checks this).
4. **An in-process lock** around `/sync`: a second call answers `already-running`.

The `reason` of a `deadline-reached` outcome is one of these constant codes, naming the step
that was not started:

| `reason` | Not started |
| --- | --- |
| `before-range` | the next 14-day range (backfill pieces, or the sync's one range) |
| `before-bundle` | the next Bundle (its device lookup and write) |
| `before-token-request` | a request to Google's token endpoint |
| `before-google-request` | a Google Health API request |
| `before-fhir-search` | a FHIR search |
| `before-fhir-write` | an `executeBundle` (first attempt or a retry) |

**For T20:** Scheduler job `retry_config.retry_count = 0` (the next scheduled run is the retry:
its range overlaps) and `attempt_deadline = "180s"` (the default for HTTP targets); Cloud Run
request timeout at least 180 s; and **`max_instance_count = 1`** on the service, so the lock
covers the whole service (a run is light: one instance is plenty, and it still scales to zero).

#### Alerts (for T20)

A log-based alert on the `sync-run` lines makes a broken sync visible before the dashboard shows
stale data:

- `jsonPayload.event = "sync-run" AND jsonPayload.http_status >= 500`: something needs a person
  (permissions, a missing store, a refused Bundle, a bug);
- `jsonPayload.event = "sync-run" AND jsonPayload.outcome = "reconnect-needed"`: the deployer
  must reconnect (T11);
- optionally `jsonPayload.event = "stranded-preliminary"`: run a backfill.

### `GET /status`

Whether the Google Health connection works **now**, worked out statelessly: it reads the secret
files and attempts a token refresh, exactly as a run would (the access token is thrown away). It
makes **no FHIR request**, so it stays inside its 20 s budget (one token retry at most) whatever
the store is doing.

| `googleHealth` | When |
| --- | --- |
| `connected` | the refresh succeeded |
| `reconnect-needed` | `reason`: `refresh-token-invalid` (`invalid_grant`) or `oauth-client-rejected` (`invalid_client`, `unauthorized_client`, `deleted_client`); or `scope-not-granted` when a sync run met `MISSING_OAUTH_SCOPE` |
| `not-configured` | `reason`: `secrets-missing:<names>` (absent or empty, e.g. no connect yet) or `secret-unreadable:<names>` (present but unusable: a read error, over 4 KiB, not UTF-8, or spaces or control characters) |
| `unknown` | the check could not finish (token endpoint 429/5xx/unreachable, or an unexpected answer); `reason` names it |

**Every state answers HTTP 200**, so the proxy can tell "the check could not finish" (`unknown`)
from "health-sync is down" (any other status, or no answer).

```json
{"googleHealth": "connected", "reason": null, "notes": ["backfill-needed"],
 "strandedPreliminary": 12, "checkedAt": "2026-01-15T15:07:30Z"}
```

- `notes`, non-fatal facts for the dashboard: `refresh-token-rotated` (Google returned a new
  refresh token, which this service cannot store; reconnecting soon keeps the connection) and
  `backfill-needed` (`strandedPreliminary` windows are still `preliminary` but older than any
  future run's range, because runs were missed; `health-sync-backfill` finalises them).
  `strandedPreliminary` is the count made by **the most recent sync run on this instance**, held
  in memory: `null` when no run has counted since the instance started (for example right after
  a cold start), or when the last run could not count (the search failed, was skipped for time,
  or found duplicate Patients). With scale to zero, expect `null` between runs on a quiet
  instance; the next scheduled run fills it in.
- `not-configured` covers both secret cases on purpose: for the dashboard the action is the
  same (the deployer fixes the secret), and the `reason` says which it is.
- No secret material is ever in the response.
- A definite answer is reused for `STATUS_CACHE_SECONDS` (60 s) in memory and `unknown` for 15 s,
  so a busy dashboard does not call Google's token endpoint on every page load. A sync run that
  finds `reconnect-needed`, or that refreshes successfully, updates the cached answer at once.
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
tokens). It is logged as a `WARNING` (`refresh-token-rotated`, never the value), flagged in the
run report (`refresh_token_rotated: true`) and shown as a note in `/status`.

**Scope.** The token response's `scope` list is not trusted to fail a run: if the configured
scope is not in it, a `WARNING` (`scope-not-listed`) is logged and the run goes on. If the scope
is really missing, the API itself answers `403 MISSING_OAUTH_SCOPE`, which is `reconnect-needed`
(`scope-not-granted`). An exact-string comparison could otherwise fail on a harmless difference.

**Error codes.** Only known OAuth error codes are echoed into a `reason`: RFC 6749's
(`invalid_request`, `invalid_client`, `invalid_grant`, `unauthorized_client`,
`unsupported_grant_type`, `invalid_scope`, `access_denied`, `server_error`,
`temporarily_unavailable`) and Google's `deleted_client`, `disabled_client`,
`admin_policy_enforced`, `invalid_rapt`. Any other value is shown as `refused`, so nothing a
server sends reaches a log unchecked. `error_description` (free text) is never used.

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
  or unusable file is logged once at start-up (`secrets-missing`, naming the secret and the
  reason, never a value) and the service starts, so `/status` can say `not-configured` instead of
  the service failing to deploy. The environment variables that hold the paths are required.
- Reason codes tell the two cases apart: `secrets-missing:<names>` (no file, or an empty one:
  nothing stored yet) and `secret-unreadable:<names>` (a file that cannot be read, is over 4 KiB,
  is not UTF-8, or holds spaces or control characters: something is mounted, but wrong).

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
| `GOOGLE_HEALTH_SCOPE` | no | `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly` | the scope expected in the token (same value as T11's); a mismatch is a warning |
| `LOOKBACK_MINUTES` | no | `180` | how far back each run re-requests; multiple of 5, at most 14 days |
| `FINAL_AFTER_MINUTES` | no | `60` | a window is `final` once it ended this long ago |
| `SYNC_INTERVAL_MINUTES` | no | `15` | the Scheduler interval; `LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL` is enforced |
| `BATCH_WINDOWS` | no | `100` | windows per Bundle (1 to 1000) |
| `HTTP_TIMEOUT_SECONDS` | no | `30` | one Google or OAuth request |
| `FHIR_TIMEOUT_SECONDS` | no | `60` | one FHIR request (a search or an `executeBundle`) |
| `RUN_DEADLINE_SECONDS` | no | `150` | a scheduled run's time budget: every request's timeout and every wait fits inside it |
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
from it, the Patient and Device conditional creates. A window written twice lands on the same
Observation. A test compares the keys of two overlapping runs. In practice most windows in the
overlap are not written again at all (next section).

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

**The rule (intended behaviour).** A window is `final` once it ended at least `FINAL_AFTER` (60
minutes) ago, and the first run after that writes it `final`. The look-back must cover that
period plus one run interval (`LOOKBACK >= FINAL_AFTER + SYNC_INTERVAL`, checked at start-up), so
the run that finalises a window still requests it. The arithmetic, with the defaults: a window
that ended at time *t* is inside a run's range until *t* + 175 minutes (180 minutes of look-back
less the window's own 5), and is due for `final` from *t* + 60 minutes. Any run in those 115
minutes finalises it; at one run every 15 minutes that is seven or eight runs, so about seven
consecutive missed runs are tolerated. After a longer outage the run report and `/status` show
`stranded_preliminary` (below), and the backfill finalises those windows. A late sample that
arrives after a window was written `final` is still picked up while the window is inside the
range (see "Amendments").

**Range: a fixed look-back.** The range is worked out from the clock alone, so a run never
depends on what an earlier, perhaps partial, run did. What is already stored is read only to
avoid rewriting it (next section), never to decide the range.

### Read once, write what changed

Each run makes three FHIR searches (standard R4 search parameters), each with its
own 10 s timeout, a fresh client per page so each page's timeout is cut to the time left:

1. `Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>&_elements=id`: the Patient's id;
2. `Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&status=preliminary&date=lt<range start>&_summary=count`:
   stranded preliminary windows;
3. `Observation?subject=Patient/<id>&code=http://loinc.org|8867-4&date=ge<start>&date=lt<end>&_elements=identifier,status,effectivePeriod,valueQuantity,component&_count=1000`,
   following the `next` links (bounded at 20 pages): what is stored for the range.

The first two are `health_sync/stored.py`. The third, and the decision per window below, are
shared with the seeder since T35: `pulsebridge_gcp.stored.stored_windows` (called with a fresh
client per page) and `pulsebridge_fhir.stored.plan_writes`.

Then each window of the roll-up is written only if it is new, its values or end changed, or it is
stored `preliminary` and is now due for `final`. A window stored with the same values and the
status it would get now (or already final) is skipped. With prompt data, each window is written
twice: once `preliminary` (when it first appears, under 60 minutes old) and once `final`. A test
runs twelve consecutive runs and checks no window is written more than twice.

- **Read states** (`stored_read` in the report): `ok`; `patient-not-found` (no Patient yet, so
  nothing is stored and everything is new); `patients-duplicated` (more than one Patient has the
  identifier: a `WARNING`, every window written, and the conditional creates will report the
  412 until the duplicate is removed by hand, T06 README); `skipped-for-time` (see "Deadline");
  `failed:<HTTP status or error type>` (`stored-read-failed` logged, every window written:
  reading only saves writes); `not-attempted` (the run had no windows).
- **A search page that is not valid FHIR JSON** (since T35, the seeder's handling): an `entry`
  that is `null` or not an array is a read error (`failed:StoredReadError`, every window
  written), and a `component` item that is not an object is skipped (that window's min or max
  reads as unknown, so it is written again). Before T35 an `entry` of `null`, `0` or an object
  read as an empty page, and a number as `entry` or a non-object component failed the run as a
  `bug`. Writing everything is the safe direction: reading only saves writes.
- **A stranded-count failure alone** is logged (`stranded-count-failed`); the stored-window read
  and the writes go on as normal, and `stranded_preliminary` is `null`.
- **A key found twice** in the search result (two Observations with one identifier, which should
  not exist) counts as not stored: the window is written and `stored-duplicate-key` is logged
  with the number of such keys (never the keys).
- **Withdrawn copies are kept.** A window stored `entered-in-error` ("withdrawn following
  previous final release") or `cancelled` was set so by a person or another system; rewriting it
  `final` would undo that. Such windows are skipped whatever their values and counted
  (`windows_withdrawn_kept`).
- **Stranded preliminary windows**: the count from search 2 is in the report
  (`stranded_preliminary`) with a `WARNING` (`stranded-preliminary`) when above zero, and in
  `/status` as the note `backfill-needed`.
- The searches run once per run and only when the roll-up returned windows, so a run that ends
  at the token refresh, or finds no data, does not touch the FHIR store.

### Amendments

R4 `amended`: "Subsequent to being Final, the observation has been modified". A window already
stored `final` (or `amended`) whose values change (a late sample arrived more than 60 minutes
after the window ended) is written **`amended`** with the new values (T33). The plan
(`stored.plan_writes`) collects those windows' keys, and each Bundle passes their start times to
T06's `heart_rate_transaction(..., amended=...)`. The Observation keeps its key, so the
conditional update replaces the stored one; the store's version history keeps the earlier
values. The run counts them (`windows_amended`), and they are also counted in `windows_final`
(complete windows: `final` or `amended`).

- **Amended again.** A window stored `amended` whose values change again is written `amended`
  again; with the same values it is left alone (`windows_unchanged`).
- **Held: a final window that is still filling again.** If the cutoff moves back between runs
  (**raising `FINAL_AFTER_MINUTES` does this**), a window stored `final` can be inside the
  "still filling" part of the range. It is never written back as `preliminary`: R4 has no path
  from `final` back to `preliminary`, and the window may already have been shared with the
  clinic. T06 also refuses `amended` for a window that is not complete. So the window is **not
  written**: the stored `final` stays, the run counts it (`windows_held_final`), and once the
  cutoff passes it again a later run writes it `amended` with the new values.
- **When the read fails** (`stored_read` not `ok`), nothing is known about what is stored, so
  every window is written with the status the cutoff gives (`final` or `preliminary`), as
  before: a window that was `amended` goes back to `final` until its values change again, and
  after `FINAL_AFTER_MINUTES` was raised a stored `final` window inside the new "still filling"
  part could be written `preliminary`. Only a successful read can prevent that.
- **The dashboard** treats `amended` like other non-final statuses: drawn on the solid line,
  described as "status amended: not shared (only final is)" (`web/src/heart-rate`). So an
  amended window cannot be shared with the clinic (T15 shares only `final`).

**Longer-term option, not built:** write each window by a deterministic id
(`PUT Observation/<key>`, update-as-create) instead of a conditional update. It would let the
store answer "unchanged" by itself only if it skipped identical updates, which is unverified, so
it is noted here only.

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
- The same read-once rule applies: re-running a backfill over stored, unchanged windows writes
  nothing. The device lookup is made per Bundle (about 8 hours of windows), not per 14-day piece.
- Logs go to stderr, the report (JSON) to stdout. Exit codes: `0` synced; `3` reconnect needed
  or not configured; `1` another failure (the report says how many ranges completed; re-run);
  `2` bad arguments or configuration. `--days` is 1 to 366.
- Safe to re-run: every window lands on the same Observation.
- **It does not take the `/sync` lock** (it is a separate process) and may run while a
  scheduled sync is writing the same windows. That is safe: every write is keyed, so both land on
  the same resources, and a 409 from two writes to one Observation at once is retried.
- Where to run it (T20/T25 to decide): as a Cloud Run **job** from the same image with the same
  environment and secret mounts (`health-sync-backfill --days N` as the command), or locally with
  the same environment variables pointing at files the deployer creates and deletes themselves.

## Device and data source

Roll-ups carry no `dataSource` (spike S3), but T06's Provenance and Device want the device name,
the platform and the recording method. For each Bundle, health-sync makes **one cheap raw call**,
`GET …/heart-rate/dataPoints?filter=<the Bundle's windows>&pageSize=1`, and uses the newest
point's `dataSource.device.displayName`, `platform` and `recordingMethod`, exactly as reported:

- A part Google does not report (or reports as `*_UNSPECIFIED` / `UNKNOWN`) becomes `None`, and
  T06 then asserts nothing about it (no custodian agent, no recording-method role).
- **When that point has no `displayName`** (or there is no point in the span), a second call with
  only an upper bound, `physical_time < <span start>`, `pageSize=1`, takes the name from the
  newest earlier point. The Device then keeps the name earlier runs used instead of switching to
  an unnamed one for one batch. Platform and recording method still come from the span's point
  when it has them.
- Only when the user has no earlier named point at all is the Device named `Unnamed Google Health
  device`, which claims nothing about the hardware (T06's Device needs a name).
- **Remaining split:** a user whose first data has no device name gets the unnamed Device for
  those windows, and the named one once Google reports a name; a real change of watch also gives
  a second Device. The Device key is patient plus name (T06), so this cannot be merged here.
- The roll-up reconciles every source; the device of the newest raw point is the one credited
  for the Bundle. With one watch (the spike's case) that is exact; with several devices it is an
  approximation.
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
`backfill-run`, `status-check`, `google-retry`, `fhir-retry`, `secrets-missing`,
`stored-read-failed`, `stored-read-skipped`, `stored-duplicate-key`, `patients-duplicated`,
`stranded-preliminary`, `stranded-count-failed`, `scope-not-listed`,
`refresh-token-rotated`, `bug`), `outcome` and `reason` codes, the UTC range bounds and cutoff,
counts (pages, windows fetched, empty, invalid, unchanged, new, changed, finalised, amended,
withdrawn kept, held final, written, final, preliminary, stranded, Bundles), HTTP statuses, retry attempts and delays,
response status counts per resource type, durations. Never logged: heart-rate values, tokens,
the client ID or secret, the patient id, the device name, response bodies, exception messages
from the mapping (they can quote a value), or Google's free-text error descriptions. A test runs
a sync with a 401, retries and a reconnect and asserts that no log line or answer contains any
synthetic secret, the patient id, the device name or the distinctive heart-rate values.

## Running

```bash
gunicorn --bind ":${PORT:-8080}" --workers 1 --threads 4 --timeout 0 --preload "health_sync.app:create_app()"
```

- **The container image omits `--bind`** (T24): it sets `PORT=8080`, and gunicorn binds
  `0.0.0.0:$PORT` when `PORT` is set and no `--bind` is given, the same result as the command
  above without a shell to expand it. The image also sets
  `GUNICORN_CMD_ARGS="--no-control-socket --graceful-timeout 8"` (no management socket; finish
  requests within 8 s of SIGTERM, before Cloud Run's SIGKILL at 10 s). Otherwise its `CMD` is
  this command, argument for argument (`tests/tooling/test_container_images.py` checks it).
- **`--preload`** loads the app (`create_app()`, which reads and checks the configuration) in
  gunicorn's master before it opens the port and forks the worker. A configuration error then
  stops gunicorn (exit 1) before anything listens, so Cloud Run's startup probe fails and the
  deploy fails; without it the port opened first and the probe could pass while the worker
  crashed. What `create_app()` builds before the fork is safe to fork with one worker: the
  logging handler, the configuration, the secret files' check (files read and closed), the
  FHIR `AuthorizedSession` and the plain `requests.Session` for Google (no connection open, no
  token fetched until the first request), the status cache and the two `threading.Lock`s
  (unlocked: no thread runs in the master). No thread is started.
- Honours `PORT`. `--timeout 0` leaves request deadlines to Cloud Run.
- Routes: `POST /sync`, `GET /status`.
- For T24's container: install `libs/pulsebridge_fhir` and `libs/pulsebridge_gcp` before this
  package (they are not on PyPI). The image also provides the `health-sync-backfill` command.
- Scale to zero is fine: nothing is kept between requests except the status cache and the
  one-run-at-a-time lock. Set `max_instance_count = 1` (see "Deadline: one run at a time").

## Cost

Idle cost is zero (Cloud Run scales to zero; `max_instance_count = 1`).

Sizes, measured on the T06 output: one heart-rate Observation is about 1.5 KB and its Provenance
about 1.5 KB (a 100-window Bundle is 202 entries and about 330 KB of compact JSON). The store
keeps every version (versioning is on, healthcare module), so each write adds about 1.5 KB of
history. A day is 288 windows, about 0.9 MB of current data.

| | Writes a day | New versions a day |
| --- | --- | --- |
| Before (every run rewrote its 36 windows) | about 6,900 Observation + Provenance writes (each window about 12 times) | about 10 MB |
| Now (read once, write what changed) | about 1,150 (each window twice: preliminary, then final) plus any changed windows | about 1.7 MB |

Reads added: three FHIR searches per run that has windows (Patient, stranded count, stored
windows), plus one per extra result page: with a run every 15 minutes, at most 96 × 3 = 288 a
day. `/status` makes none. Each returns a few kilobytes (`_elements` keeps only the compared
fields). Google calls per run: one token refresh, one roll-up page, and one data-source call
per Bundle written (two when the name falls back to an earlier point; none when nothing
changed).

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
`scope` (if absent, the scope warning is skipped); whether the API accepts a range ending at the
current five-minute boundary with a still-filling window (the range ends before it, so this is
not relied on); decimal round-tripping in the FHIR store.

## For T25: confirm on the first real write

T06's ingest Bundle assumes the store resolves the in-Bundle `urn:uuid` references to the Patient
that the setup step already wrote (the conditional create matches and creates nothing). This
task made no GCP calls, so it is **not yet confirmed**; the check belongs to the first deployment
(T25). On the first real sync, check:

1. The run report's `entry_statuses` shows `Patient 200` (matched), not `Patient 201` (created a
   second Patient). `201` on the very first run before setup is expected; after setup it is not.
2. `GET Observation?identifier=urn:pulsebridge:observation|<a key>` returns one Observation whose
   `subject.reference` is the setup Patient's `Patient/<id>`.
3. `GET Patient?identifier=urn:pulsebridge:patient|<PATIENT_ID>` returns exactly one Patient.

If any fails, switch to the fixed-ID fallback in the T06 README ("Unverified server behaviour,
and the fallback"); that is a change to `libs/pulsebridge_fhir`, not to this service.

**The read step.** These search features are standard R4 but unverified on Cloud Healthcare:
`_elements` on Patient and Observation searches; `_summary=count` returning `Bundle.total`;
`date` on an `effectivePeriod` with R4's overlap semantics; `_count=1000`; and `next` links on
the store's own base URL (`next_page` refuses any other). If one is refused, the search fails
and the run writes everything (`stored_read: "failed:…"`), so a wrong assumption costs writes,
not data. It can also fail silently (for example `date` matching nothing), so check:

4. On the **second** sync run after deployment, the report has `stored_read: "ok"` and
   `windows_unchanged > 0`. If `windows_unchanged` stays 0 run after run while `stored_read` is
   `ok`, the read step is silently not working and every window is being rewritten.
5. `stranded_preliminary` is a number (not `null`) in the run report.
6. `GET /status` answers within a second or two (it makes one token request and no FHIR
   request); a slow answer means it is waiting on Google's token endpoint.

## Tests

`make test` runs `tests/`. No network, no credentials, no GCP: Google and the OAuth endpoint are
scripted with `pulsebridge_gcp.testing.FakeTransport`, the FHIR store is the real
`HealthcareClient` over `FhirFake` (a `FakeTransport` that also answers the searches from an
in-memory store), and the secret files are written to a temporary
directory. Every roll-up item is invented in the shape spike S3 recorded, and every credential is
assembled at run time from obviously synthetic pieces. Each behaviour was break-checked: the
implementation was deliberately broken and a test seen to fail.
