# PulseBridge work plan

Derived from `pulsebridge-projectbrief.md`. Each task is a unit of work that one agent can finish in
one git worktree. The orchestrator assigns tasks, and updates the Status column here.

## How to use this file

- **Status values:** `todo`, `in-progress`, `in-review`, `done`, `blocked`.
- **Only the orchestrator edits this file.** Builder agents report back; they do not change status.
- **A task is `done`** when it has passed the `review-work` skill and been merged into the
  integration branch.
- **Depends on** lists tasks that must be `done` first. Tasks in the same wave with no dependency
  on each other can run in parallel.
- **Owns** lists the paths a task may create or change. Two tasks running in parallel must not own
  the same path. If a task needs a change outside its paths, the agent reports it instead of
  making it.
- Branch and worktree naming: branch `task/<id>-<slug>`, worktree `.worktrees/<id>-<slug>`
  (see `.claude/skills/feature-dev/SKILL.md`). Task branches are pushed to GitHub.

## Decisions made

| # | Decision | Outcome |
| --- | --- | --- |
| D1 | Dev project | The existing GCP project is prod. A second project for dev is created when we reach T26; until then CI only runs `plan` for dev. |
| D2 | Connecting Google Health | A CLI script runs the OAuth flow. Three values live in Secret Manager: OAuth client ID, OAuth client secret, refresh token. |
| D3 | HL7v2 parsing | Use `hl7apy` to parse, build and validate messages. "Own parser" in the brief means the v2-to-FHIR mapping is ours (no Whistle or Dataflow). The mapping is the code a reviewer should read. |
| D8 | Heart-rate granularity | Five-minute roll-ups from the Google Health API's `rollUp` method (about 288 Observations a day), not raw samples (about 37,000 a day). See `docs/spikes/s3.md`. |
| D9 | Reviewer login | A shared password in Secret Manager, checked by `fhir-proxy`, which issues an 8-hour signed bearer token. No Identity Platform. See `docs/spikes/s6.md`. |
| D10 | One front door | `fhir-proxy` is the only public service the page calls. The cuff simulator is not publicly invocable; the proxy forwards cuff requests to it, so it sits behind the reviewer login. |
| D6 | Task branches | Builders push `task/*` branches to GitHub so work survives a lost container. |
| D7 | Two acknowledgment levels | Modelled on HL7 enhanced acknowledgment mode (MSH-15 accept, MSH-16 application). The HL7v2 store's ACK is the **commit-level acknowledgment**; the mapper's result is the **application-level outcome**. Both are labelled that way in the messages log and trace panel. See "Malformed message handling" below. |

## Open decisions (need the user)

| # | Decision | Recommendation |
| --- | --- | --- |
| D4 | Dashboard hosting: bucket + Cloud CDN, or Firebase Hosting. | Decide in T22; bucket + load balancer has an hourly forwarding-rule cost, Firebase Hosting does not. |
| D5 | Dashboard framework. | Vite + TypeScript with a small chart library, no heavy framework. |

## Malformed message handling (D7)

Each layer rejects a different class of bad message, and the tests cover each class.

| Class | Example | Caught by | What the user sees |
| --- | --- | --- | --- |
| Structurally invalid | missing required segment or field, wrong data type | Cuff simulator, `hl7apy` validation, before sending | Validation error; nothing is sent |
| Unparseable | no MSH segment, wrong segment terminator, separators that disagree with MSH-1, empty payload | HL7v2 store at ingest; the adapter relays the store's reply | Commit-level `AE` with the store's reason from MSA-3 |
| No acknowledgment | bad MLLP framing (adapter waits silently); Healthcare API error such as a missing store or denied permission (adapter closes the connection) | MLLP adapter | "No acknowledgment received", with timeout or connection closed as the reason |
| Parses but cannot be mapped | unknown OBX-3 code, missing OBX-14, non-numeric OBX-5, empty or unknown MSH-9 or MSH-12, MSH-only message | `v2-to-fhir` mapping | Commit-level `AA`, application-level outcome `error` with a reason |

- The mapper records `application_outcome: accepted | error` plus a `reason` for every message.
- Optional: `v2-to-fhir` also writes an `ACK^R01` with `MSA-1 = AE` back into the HL7v2 store.
- A mapping failure never crashes or stalls the Pub/Sub subscription.
- Found in spike S4: the store accepts almost anything with a readable header (even a lone
  `MSH|^~\&`) with `AA`, never returns `AR`, and ignores MSH-15/16. It replies in the original
  (simple) acknowledgment style; "commit-level" and "application-level" are our labels, modelled on
  enhanced mode. The store's ACK has message type `ACK`, not `ACK^R01`, and on a NACK MSA-2 is empty.
- The simulator has a "send malformed" checkbox that bypasses validation so the later layers'
  error paths can be demonstrated.

## Wave 0: spikes (not started; on hold until the user says go)

Each spike ends with a short findings note in `docs/spikes/<id>.md` and, where it changes the
design, a proposed edit to the brief. Spikes create no permanent resources.

| ID | Spike | Depends on | Needs | Status |
| --- | --- | --- | --- | --- |
| S1 | OpenEMR FHIR write support: does `POST /apis/default/fhir/Observation` (vital-signs) work, and where does it land? Fallback: standard REST vitals endpoint. | none | Docker (local) | done |
| S2 | OpenEMR + MariaDB + MLLP adapter in 1 GB plus swap: measure page load and FHIR API latency. | none | a temporary e2-micro | done |
| S3 | Google Health API: register OAuth app, get a token, pull one day of heart-rate points, record exact field names and webhook payload shape. | none | the user (console) | done |
| S4 | Google's mllp-adapter image on e2-micro next to MariaDB. Fallback: small Python MLLP listener. | none | a temporary e2-micro | done |
| S5 | HL7v2 store: confirm `ingest` publishes to Pub/Sub and record the push message body. Also record what the store and the MLLP adapter return for an unparseable message (D7). | none | temporary dataset | done |
| S6 | Reviewer auth: Identity Platform or a static password in the proxy. | none | none (desk research) | done |

## Wave 1: foundations (all parallel after T01)

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T01 | Repo scaffolding | none | `Makefile`, `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `services/README.md`, `libs/README.md`, `tests/tooling/` | done |
| T02 | Terraform bootstrap | T01 | `infra/bootstrap/` | in-progress |
| T03 | Module `project-base` | T01 | `infra/modules/project-base/` | done |
| T04 | Module `identity` | T01 | `infra/modules/identity/` | in-progress |
| T05 | Module `healthcare` | T01 | `infra/modules/healthcare/` | in-progress |
| T06 | FHIR mapping library | T01 | `libs/pulsebridge_fhir/` | in-progress |
| T07 | HL7v2 helpers (hl7apy) | T01 | `libs/pulsebridge_hl7v2/` | in-progress |

**T01 Repo scaffolding.** Python tooling (ruff, pytest), a Makefile with `lint`, `test`, `fmt`,
`tf-validate` targets, pre-commit hooks including a secret scanner, and a CI workflow that runs
lint and tests on pull requests.
- AC: `make lint` and `make test` pass on a clean clone.
- AC: CI runs both on a pull request and fails when either fails.
- AC: the pre-commit secret scan rejects a staged file containing a fake service account key.
- Added in review: CI also scans each pull request's commits with gitleaks (`make secret-scan`),
  because the commit hook only sees staged changes on machines where it is installed.
- Note for T26: the `terraform` job in `ci.yml` overlaps with T26, which should take it over.

**T02 Terraform bootstrap.** One-time root run by a human with their own Google credentials.
- AC: `create_project = true` creates a project and links the given billing account;
  `create_project = false` adopts an existing project ID. No organization is required.
- AC: creates a versioned GCS state bucket, a deployer service account, and a Workload Identity
  Federation pool and provider restricted to one GitHub repository (a variable).
- AC: outputs the values the env roots and GitHub Actions need; no key files are created.
- AC: no project ID, repository name or account is hardcoded; `terraform.tfvars.example` exists.
- AC: `terraform validate` passes; variables have descriptions and validation blocks.

**T03 Module `project-base`.** APIs, VPC, subnet, Cloud NAT (off by default), firewall,
Artifact Registry.
- AC: the list of enabled APIs is a variable with a default covering the stack. Identity
  Platform (`identitytoolkit`) is not in it (see D9).
- AC: Cloud NAT is created only when its flag is true.
- AC: `README.md` generated by terraform-docs; `terraform validate` passes.

**T04 Module `identity`.** Service accounts, IAM bindings, Secret Manager secrets (no values),
and the reviewer login resources from D9: a `reviewer-password` secret and its accessor binding
for the proxy's service account. No Identity Platform resources.
- AC: one service account per service, each with only the roles its service needs, listed in the README.
- AC: secrets are created without versions; no secret value appears in code or state. The Google
  Health secrets are three: OAuth client ID, OAuth client secret, refresh token.
- AC: for dev only, the reviewer password may be set from `tfvars` through a write-only argument
  (`secret_data_wo`), so it never enters state; a test searches a state file produced with a
  dummy value and does not find it. `required_version` is pinned to a Terraform release that
  supports write-only arguments.
- AC: only the proxy may invoke the cuff simulator (see D10).
- AC: `terraform validate` passes.

**T05 Module `healthcare`.** Healthcare dataset, FHIR R4 store, HL7v2 store, Pub/Sub topic and
push subscription, HL7v2 notification config.
- AC: FHIR store is R4 with referential integrity and update-create settings stated and explained.
- AC: the HL7v2 store publishes ingest notifications to the topic.
- AC: the HL7v2 store's `parserConfig.version` is set explicitly and explained (it cannot be
  changed after creation); `rejectDuplicateMessage` and `allowNullHeader` stay at their defaults.
- AC: the push subscription has a dead-letter topic and an OIDC-authenticated push endpoint variable.
- AC: `terraform validate` passes.

**T06 FHIR mapping library.** Pure Python, no GCP calls. Builders for `Patient`, `Device`,
heart-rate `Observation` (LOINC 8867-4, UCUM `/min`; one per five-minute roll-up window, see D8),
blood-pressure panel `Observation`
(LOINC 85354-9 with components 8480-6 and 8462-4, `mm[Hg]`), `Provenance`, and a transaction
`Bundle` using conditional updates.
- AC: every Observation has status, vital-signs category, code, subject, effective time, value with
  UCUM unit, and a `device` reference.
- AC: building the same input twice gives the same conditional-update key (idempotency). For
  heart rate the key is built from patient, code and window start time; Google's data points have
  no `name`.
- AC: a heart-rate Observation uses `effectivePeriod` for the window, the average as
  `valueQuantity`, and the minimum and maximum as components. The component codes are checked
  against LOINC and the choice is explained, not guessed.
- AC: the mapper takes the roll-up shape recorded in `docs/spikes/s3.md` (numbers for minimum and
  maximum, a decimal average, UTC window times) and applies the user's UTC offset to the FHIR times.
- AC: `Patient.identifier` can carry the Google Health user ID; the `Device` identifier is
  derived, because Google supplies only a display name. `Provenance` records the source platform
  and recording method.
- AC: invalid input (missing time, out-of-range value, wrong unit) raises a clear error.
- AC: example outputs are written to `libs/pulsebridge_fhir/examples/` for the validator (T08).

**T07 HL7v2 library.** Thin, shared helpers over `hl7apy` (see D3); no hand-written ER7 parser.
ORU^R01 builder for a blood-pressure reading (two OBX: LOINC 8480-6 and 8462-4), validation of a
message before it is sent, ACK parsing, MLLP framing for the client side, and a small set of sample
messages reused by T12 and T13: valid ones, and at least one per malformed class in D7
(structurally invalid, unparseable, no acknowledgment, and each parses-but-cannot-map case). Per
spike S4 the reliable NACK samples are "no MSH segment" and "line-feed segment terminators", and a
missing MLLP end byte demonstrates the no-acknowledgment path.
- AC: the builder produces an ORU^R01 that passes `hl7apy` validation; a message with a missing
  required field or a wrong OBX value type fails validation with a clear error.
- AC: a built message parses back to the same patient identifier, LOINC codes, values, units and
  observation time (OBX-14).
- AC: ACK parsing returns the acknowledgment code (`AA`, `AE`, `AR`) and the control ID it answers.
- AC: MLLP framing handles a message split across TCP reads and two messages in one read.
- AC: sample messages are synthetic and live in `libs/pulsebridge_hl7v2/samples/`.

## Wave 2: services and CI checks

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T08 | FHIR validator in CI | T06 | `.github/workflows/fhir-validate.yml`, `scripts/fhir-validate.sh` | todo |
| T09 | Synthetic data seeder | T06 | `services/seed/` | todo |
| T10 | `health-sync` service | T06, S3 | `services/health-sync/` | todo |
| T11 | Google Health connect flow | S3, T04 | `scripts/connect-google-health/` | todo |
| T12 | `v2-to-fhir` service | T06, T07, S5 | `services/v2-to-fhir/` | todo |
| T13 | Cuff simulator backend | T07 | `services/cuff-sim/` | todo |
| T14 | FHIR proxy: read API and reviewer login | T06, S6 | `services/fhir-proxy/` (except `share/`) | todo |
| T15 | FHIR proxy: share to OpenEMR | T14, S1 | `services/fhir-proxy/share/` | todo |
| T16 | Dashboard shell | none | `web/` | todo |

**T08 FHIR validator in CI.** Runs the HL7 FHIR validator (R4, base profiles) over the example
resources from T06.
- AC: CI fails when an example resource is made invalid (demonstrated in the task summary).

**T09 Synthetic data seeder.** Backs the `use_synthetic_data` flag: generates plausible heart-rate
roll-up windows (the same five-minute shape the Google Health API returns) for a synthetic patient
and writes them through the T06 mapper.
- AC: output is deterministic for a given seed; re-running does not duplicate Observations.
- AC: no real health data is used.

**T10 `health-sync` service.** Pulls five-minute heart-rate roll-ups from the Google Health API
(`dataPoints:rollUp`, see D8 and `docs/spikes/s3.md`), maps them with T06, writes a transaction
Bundle.
- AC: idempotent on re-delivery (conditional update keyed on patient, code and window start).
- AC: each run re-requests a range that overlaps the previous run, and a window that was only
  partly synced is updated on the next run, not skipped.
- AC: follows `nextPageToken`; handles results arriving newest first and windows with no data
  being absent.
- AC: `backfill --days N` command exists and splits the range into requests of at most 14 days
  (the API's limit for heart-rate roll-ups).
- AC: an expired or revoked refresh token gives a clear "reconnect needed" status that the proxy
  can show, not a crash or a retry loop.
- AC: refresh token is read from Secret Manager at run time; nothing is logged that reveals it.
- AC: tests use recorded synthetic responses, not the author's data.

**T11 Google Health connect flow.** CLI script that lets the deployer authorise their own account
(see D2).
- AC: reads the OAuth client ID and client secret from Secret Manager; if they are absent it
  prompts for them without echo and stores them. None is committed or printed.
- AC: runs the OAuth authorization-code flow with PKCE and a `state` check, requesting offline
  access and a consent prompt (without them Google returns no refresh token), and stores the
  refresh token as a new Secret Manager version; prints only a success message.
- AC: listens on a loopback port for the redirect when run on the deployer's machine, and
  accepts a pasted redirect address when it cannot.
- AC: reports when the refresh token will expire (7 days while the OAuth app is in Testing status).
- AC: the scope requested is `googlehealth.health_metrics_and_measurements.readonly`, as a variable.
- AC: `health-sync` needs exactly these three secrets and nothing else from the deployer.
- AC: documented in `docs/setup.md` terms a new user can follow.

**T12 `v2-to-fhir` service.** Pub/Sub push handler plus the v2-to-FHIR mapping, which is the
showcase code. The mapping lives in a single module (`services/v2-to-fhir/mapping.py`) with no
GCP calls, so it can be read and tested on its own. Input is either the raw ER7 parsed with
`hl7apy` or the HL7v2 store's `parsedData`; pick one and explain why in the service README.
- AC: OBX-3 LOINC codes map to `Observation.code` (panel 85354-9) and `component` (8480-6, 8462-4);
  OBX-5 and OBX-6 map to the component value and UCUM unit.
- AC: OBX-14 maps to `effectiveDateTime`, with the time zone handled explicitly.
- AC: MSH and PID identifiers map to the `Patient` and `Device` references.
- AC: a `Provenance` points back to the HL7v2 store message name.
- AC: the write is a conditional update keyed on the message control ID, so a redelivered push
  does not create a second Observation.
- AC: every message gets `application_outcome: accepted | error` plus a `reason`, stored where
  the proxy can read it (suggested: labels on the HL7v2 store message; state the choice in the README).
- AC: unknown OBX-3 code, missing OBX-14 and non-numeric OBX-5 each give outcome `error` with a
  distinct reason and write no Observation.
- AC: on any mapping error the push is still acknowledged; the handler never raises to Pub/Sub
  and the subscription never stalls (see D7).
- AC: unit tests run the mapping against the T07 sample messages, with at least one fixture per
  malformed class.
- Optional: write an `ACK^R01` with `MSA-1 = AE` back into the HL7v2 store on error.

**T13 Cuff simulator backend.** Not publicly invocable: only `fhir-proxy` calls it (D10). Builds an ORU^R01 with the T07 builder from bounded systolic and
diastolic values, validates it with `hl7apy` before sending, and sends it by MLLP (default) or
HTTPS ingest; returns the ACK.
- AC: a message that fails validation is never sent, unless "send malformed" is set.
- AC: a `send_malformed` option bypasses validation and sends a chosen malformed sample from T07
  (never free text); it is still rate-limited.
- AC: a NACK from the store is returned to the caller as the commit-level acknowledgment with
  the reason from MSA-3, not turned into a generic error. MSA-2 is not required to match on a NACK
  (it is empty when the header could not be read), and the reply type `ACK` is accepted.
- AC: the MLLP client has a read timeout and reports three distinct outcomes: an ACK frame,
  connection closed with no data, and timeout. The last two are "no acknowledgment received".
- AC: segments end with a carriage return all the way to the wire; nothing converts them to line feeds.
- AC: inputs outside bounds are rejected; no free text reaches the message.
- AC: rate-limited; both transports covered by tests against a fake listener.
- AC: it has no login of its own and relies on Cloud Run's invoker check; the README says so.

**T14 FHIR proxy: read API.** Narrow read API for the dashboard: Observation search by patient,
code and date; trace lookups (v2 message, commit-level ACK, application-level outcome and reason,
source JSON); status strip data; messages log; `CapabilityStatement`; and the reviewer login from
D9 (`docs/spikes/s6.md`): a login endpoint that checks the shared password and issues a signed
bearer token, the token check on every other route, CORS for the dashboard's origin, and a login
rate limit. It also forwards cuff requests to the cuff simulator (D10).
- AC: unsupported search parameters return a FHIR `OperationOutcome`, not a pass-through.
- AC: the `CapabilityStatement` lists exactly what is implemented (test compares the two).
- AC: unauthenticated requests are rejected.
- AC: a wrong password gives `401`; a token that is expired, altered, or signed under the
  previous password gives `401`; the sixth failed login in a minute from one address gives `429`;
  a missing secret version gives `503`.
- AC: the password comparison is constant-time, and no log line contains a submitted password.
- AC: the client address used for the rate limit is taken from the right `X-Forwarded-For`
  entry for Cloud Run; this is checked, not assumed.
- AC: the token is a bearer token in a header, not a cookie, and expires after 8 hours
  (configurable).
- AC: the `CapabilityStatement` states the security scheme truthfully (it is not SMART on FHIR).
- AC: cuff requests are forwarded to the simulator with the proxy's own identity, only for a
  logged-in caller.

**T15 FHIR proxy: share to OpenEMR.** Sequence found in spike S1 (`docs/spikes/s1.md`): get a
token; search `Patient` by identifier and create it if absent; create or reuse an `Encounter`;
`POST` each `Observation` with `subject` and `encounter` set; trace every step. OpenEMR has no
Bundle endpoint and ignores conditional create.
- AC: uses a registered client with the client credentials grant, authenticated by a signed
  assertion (`private_key_jwt`) with `system/*` scopes; no admin API key and no user login.
- AC: sharing the same reading twice does not create a duplicate patient (search, then create).
- AC: the Patient sent has a `PT`-typed identifier, a name with `use: official`, gender and birth date.
- AC: OpenEMR's "already exists" `400` for a repeated reading is reported as already shared, not
  as a failure.
- AC: the chart link is built from the numeric patient id in the Encounter-create response.
- AC: the assertion's audience comes from configuration (OpenEMR's `site_addr_oath` value).
- AC: the trace returned to the dashboard never contains the private key, the signed assertion
  or the full token.
- AC: the access token is cached for its lifetime (300 seconds), not fetched per share.
- AC: TLS verification of OpenEMR's certificate is never disabled; the trusted certificate or CA
  is configuration.

**T16 Dashboard shell.** Single page: login, header status strip, line-art silhouette, empty left
and right panels, share button placeholder, collapsible messages log, a typed API client with mock data.
- AC: runs locally against mock data with one command; layout matches the brief.
- AC: login is a password-only form; the token is kept in `sessionStorage`; the API client adds
  it to every request and returns to the login form on `401`. No Firebase SDK.
- AC: the page calls one backend only, `fhir-proxy` (D10).

## Wave 3: dashboard features and infrastructure for services

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T17 | Dashboard: heart-rate chart and right trace panel | T16 | `web/src/heart-rate/` | todo |
| T18 | Dashboard: cuff simulator and left trace panel | T16 | `web/src/cuff/` | todo |
| T19 | Dashboard: share action and messages log | T16 | `web/src/share/`, `web/src/messages/` | todo |
| T20 | Module `ingestion` | T04, T05 | `infra/modules/ingestion/` | todo |
| T21 | Module `edge-vm` | T03, S2, S4 | `infra/modules/edge-vm/` | todo |
| T22 | Module `phr` | T03, T04 | `infra/modules/phr/` | todo |
| T23 | Module `openemr` | T21, S1 | `infra/modules/openemr/` | todo |
| T24 | Container build pipeline | T01 | `cloudbuild.yaml`, `services/*/Dockerfile` | todo |

**T17 to T19 Dashboard features.** As listed under "Product requirements" in the brief.
- AC (T17): 24 h, 7 d and 30 d windows of five-minute averages; min, mean and max for the window;
  selecting a point fills the trace panel with that roll-up window.
- AC (T18): bounded inputs, transport toggle, "send malformed" checkbox, readings list with ACK
  code, three trace tabs; the trace shows the commit-level acknowledgment and the application-level
  outcome as two separately labelled items.
- AC (T19): each v2 row in the messages log shows the commit-level acknowledgment and the
  application-level outcome with its reason.
- AC (T19): share acts on the current selection and shows its trace; messages log lists v2 messages
  and FHIR transactions.

**T20 Module `ingestion`.** Cloud Run services `health-sync` and `v2-to-fhir`, Scheduler job,
OIDC invoker bindings.
- AC: services scale to zero and are not publicly invocable; images are referenced by digest.

**T21 Module `edge-vm`.** e2-micro, startup script, swap file, persistent disk, MLLP adapter and
MariaDB containers, firewall rule limited to the Cloud Run egress address.
- AC: no public MLLP port; machine type is a variable defaulting to the free-tier size.
- AC: the adapter image is pinned by digest and started with the flags recorded in
  `docs/spikes/s4.md` (binary path as the command, `--hl7_v2_store_id`, `--export_stats=false`,
  content logging off, `--restart unless-stopped`).
- AC: the startup script checks the adapter container is up a few seconds after start and logs
  if it is crash-looping.
- AC: the VM runs as a dedicated service account whose only Healthcare permission is HL7v2
  ingest on the store (not the default Compute Engine account, which has project-wide Editor).
- AC: 2 GB swap file that survives a reboot; the compose file and low-memory settings from
  `docs/spikes/s2.md` are reused; containers restart unattended after a reboot.
- AC: the startup script is safe to run on every boot.
- AC: the database and OpenEMR site data live on a separate persistent disk, so replacing the VM
  does not destroy the clinic data; `terraform destroy` still removes that disk.
- AC: anything that waits for the stack on first apply tolerates about 15 minutes.

**T22 Module `phr`.** Dashboard hosting, `fhir-proxy` and cuff simulator Cloud Run services.
- AC: hosting choice (D4) recorded with its idle cost, including that Firebase Hosting rewrites
  would put the page and the proxy on the same origin.
- AC: `fhir-proxy` is publicly invocable, mounts the reviewer password secret as a file (so a
  new version takes effect without a redeploy), and is capped at two instances.
- AC: the cuff simulator is invocable only by the proxy's service account.
- AC: the hosting layer sets a Content-Security-Policy header.

**T23 Module `openemr`.** OpenEMR container on the edge VM, database connection, OAuth2 client
registration script.
- AC: the OAuth2 client's private key goes to Secret Manager, not to state or logs; the client
  ID is plain configuration.
- AC: OpenEMR is configured without a browser: `rest_fhir_api`, `rest_system_scopes_api` and
  `site_addr_oath` set through `OPENEMR_SETTING_*` variables; the standard `rest_api` stays off.
- AC: OpenEMR starts only after MariaDB is healthy (it exits if the database is not ready).
- AC: the registration script follows the recipe in `docs/spikes/s1.md`, including enabling the
  newly registered client, and is safe to run twice.
- AC: the OpenEMR image is pinned by digest.
- AC: Apache is capped at four worker processes (required, per spike S2).
- AC: readiness is checked by polling OpenEMR's `/meta/health/readyz`, allowing about 15 minutes
  on first boot.
- AC: confirm on the VM that the `OPENEMR_SETTING_*` variables take effect (spike S1 verified
  them; spike S2 used database writes instead); fall back to database writes if they do not.
- AC: `site_addr_oath` is the URL the FHIR proxy actually calls, and the README states how the
  proxy verifies OpenEMR's certificate (it is self-signed by default).

**T24 Container build pipeline.** Dockerfiles and a Cloud Build config that builds on tag and
reports image digests.
- AC: images run as a non-root user; a build prints the digest to put in `tfvars`.

## Wave 4: integration, CI/CD and documentation

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T25 | Env roots `dev` and `prod` | T02 to T05, T20 to T23 | `infra/envs/` | todo |
| T26 | Terraform CI/CD | T02, T25 | `.github/workflows/terraform-*.yml`, `.tflint.hcl` | todo |
| T27 | Secret version scripts | T04, T25 | `scripts/secrets/` | todo |
| T28 | Setup guide and README | T25, T11 | `README.md`, `docs/setup.md` | todo |
| T29 | End-to-end smoke test | T25 | `tests/e2e/` | todo |
| T30 | Public demo domain | T25 | `infra/envs/prod/dns.tf` | todo |

**T25 Env roots.** `dev` and `prod` wire the modules; each has `backend.tf` and
`terraform.tfvars.example`; `use_synthetic_data` switches the wearable source.
- AC: `terraform validate` passes in both; a clean project reaches a working stack from `dev`.
- AC: `terraform destroy` leaves nothing billable.

**T26 Terraform CI/CD.** `fmt -check`, `validate`, `tflint`, `trivy config`, plan posted on pull
requests, apply to dev on merge, manual gate for prod, nightly destroy test for dev (see D1).
- AC: authenticates with Workload Identity Federation only; no key in GitHub secrets.

**T27 Secret version scripts.** Add secret versions after apply.
- AC: values are read from a prompt or stdin and never echoed or written to disk.
- AC: includes `set-reviewer-password`, which enforces a 16-character minimum and can generate
  a random password.

**T28 Setup guide and README.** New-user guide: prerequisites, create or adopt a project, billing,
bootstrap, register a Google Health OAuth app, connect an account, deploy, destroy. README: pitch,
GIF, architecture, standards map, deploy, what this is not.
- AC: no step relies on the author's accounts; every command uses a placeholder or a variable.
- AC: the guide explains the OAuth app's Testing status (test users only, refresh token expires
  after 7 days) and how to move it to "In production".

**T29 End-to-end smoke test.** Simulated cuff reading through MLLP to the FHIR store, then shared
to OpenEMR, asserted from the APIs.
- AC: runs against a deployed dev stack with one command.

**T30 Public demo domain.** Map the dashboard to a subdomain; the domain is a variable.
- AC: TLS works; nothing about the author's domain is hardcoded outside `tfvars`.
