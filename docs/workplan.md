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
| D11 | Who owns the Patient record | A setup step (the connect script or the seeder) writes the full Patient once with a conditional update. Ingest Bundles from `health-sync` and `v2-to-fhir` include Patient and Device as conditional creates (`POST` with `ifNoneExist`), so they never overwrite. Fallback if the store does not resolve in-Bundle references to the existing resource: a fixed Patient ID with direct references. |
| D12 | Send attempts | Every cuff send attempt is recorded as a FHIR `AuditEvent` in the FHIR store, written by the cuff simulator (it holds the raw ACK and is not public; `fhir-proxy` stays read-only). The messages log lists attempts, including rejected and unacknowledged ones. |
| D13 | OpenEMR chart view | The dashboard shows an "As stored in OpenEMR" panel, filled by `fhir-proxy` reading the Patient, Encounter and Observations back from OpenEMR's FHIR API. OpenEMR's screens are not exposed to the internet; the README carries a screenshot of the real chart. |
| D14 | Cloud Run to the VM | Direct VPC egress into a dedicated subnet (built in T03). The firewall rule for MLLP and OpenEMR HTTPS lives in `project-base`, sourced from that subnet; `edge-vm` adds no rule and needs no public port. |
| D4 | Dashboard hosting | Firebase Hosting. It has no hourly charge, unlike a bucket behind a load balancer, and its rewrites can put the page and `fhir-proxy` on one origin. Consequences: T22 adds the Firebase Hosting API to `project-base`'s API list and a hosting role to the deployer's roles (both are variables). |
| D5 | Dashboard framework | Vite and TypeScript with a small chart library; no heavy framework. |
| D15 | Audit data in its own FHIR store | Send-attempt AuditEvents are written to a second FHIR store. The cuff simulator can write there and has no access to the store with health data; `fhir-proxy` reads both. Chosen over giving the simulator the editor role on the main store, which would also let it read and delete health data. |
| D6 | Task branches | Builders push `task/*` branches to GitHub so work survives a lost container. |
| D7 | Two acknowledgment levels | Modelled on HL7 enhanced acknowledgment mode (MSH-15 accept, MSH-16 application). The HL7v2 store's ACK is the **commit-level acknowledgment**; the mapper's result is the **application-level outcome**. Both are labelled that way in the messages log and trace panel. See "Malformed message handling" below. |

## Open decisions (need the user)

None at present.

## Malformed message handling (D7)

Each layer rejects a different class of bad message, and the tests cover each class.

| Class | Example | Caught by | What the user sees |
| --- | --- | --- | --- |
| Structurally invalid | missing required segment or field, wrong data type | Cuff simulator, `hl7apy` validation, before sending | Validation error; nothing is sent |
| Unparseable | no MSH segment, wrong segment terminator, separators that disagree with MSH-1, empty payload | HL7v2 store at ingest; the adapter relays the store's reply | Commit-level `AE` with the store's reason from MSA-3 |
| No acknowledgment | bad MLLP framing (adapter waits silently); Healthcare API error such as a missing store or denied permission (adapter closes the connection) | MLLP adapter | "No acknowledgment received", with timeout or connection closed as the reason |
| Parses but cannot be mapped | unknown OBX-3 code, missing OBX-14, non-numeric OBX-5, empty or unknown MSH-9 or MSH-12, MSH-only message | `v2-to-fhir` mapping | Commit-level `AA`, application-level outcome `error` with a reason |

- The mapper records `application_outcome: accepted | error` plus a `reason` for every message,
  as labels on the stored HL7v2 message (spike S5: updating labels publishes no notification, so
  there is no loop, and messages can be listed by label).
- A third state exists: stored with `AA` but no outcome label yet (mapper not run, failed, or
  dead-lettered). The log shows it as "pending".
- Writing an `ACK^R01` back into the store is not built: labels carry the same information, and
  a stored ACK publishes a notification that would re-trigger the mapper.
- The store keeps neither the commit-level ACK bytes nor rejected messages; they exist only in
  the ingest response or on the MLLP socket. So the cuff simulator records every send attempt as
  an `AuditEvent` (D12).
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
| T02 | Terraform bootstrap | T01 | `infra/bootstrap/` | done |
| T03 | Module `project-base` | T01 | `infra/modules/project-base/` | done |
| T04 | Module `identity` | T01 | `infra/modules/identity/` | done |
| T05 | Module `healthcare` | T01 | `infra/modules/healthcare/` | done |
| T06 | FHIR mapping library | T01 | `libs/pulsebridge_fhir/` | done |
| T07 | HL7v2 helpers (hl7apy) | T01 | `libs/pulsebridge_hl7v2/` | done |

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
- The rule "only the proxy may invoke the cuff simulator" (D10) is enforced in T22, which owns the
  Cloud Run service; this module supplies the identity and documents the rule.
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
| T08 | FHIR validator in CI | T06 | `.github/workflows/fhir-validate.yml`, `scripts/fhir-validate.sh` | done |
| T09 | Synthetic data seeder | T06 | `services/seed/` | done |
| T10 | `health-sync` service | T06, S3 | `services/health-sync/` | done |
| T11 | Google Health connect flow | S3, T04 | `scripts/connect-google-health/` | done |
| T12 | `v2-to-fhir` service | T06, T07, S5 | `services/v2-to-fhir/`, `libs/pulsebridge_gcp/` | done |
| T13 | Cuff simulator backend | T07, T31 | `services/cuff-sim/` | done |
| T14 | FHIR proxy: read API and reviewer login | T06, S6, T13, T33 | `services/fhir-proxy/` (except `share/`) | done |
| T15 | FHIR proxy: share to OpenEMR | T14, T19, T23, S1 | `services/fhir-proxy/fhir_proxy/share/`, `services/fhir-proxy/tests/share/`, `fhir_proxy/extensions.py` (trace redaction and `request.query`), the proxy's configuration and README for the OpenEMR settings, the `openemr` wiring in `infra/modules/phr/` if variable names change, and, to keep the page in step with the route as built, `web/src/api/`, `web/src/share/`, the status strip in `web/src/shell/` and `web/README.md` | done |
| T16 | Dashboard shell | none | `web/`, a `web` job in `.github/workflows/ci.yml`, `web-*` targets in `Makefile` | done |
| T31 | AuditEvent builder for send attempts | T06 | `libs/pulsebridge_fhir/` | done |
| T32 | Audit FHIR store for send attempts | T04, T05 | `infra/modules/healthcare/`, `infra/modules/identity/README.md` | done |
| T33 | Library follow-ups from wave 3 reviews | T08, T10, T13 | `libs/pulsebridge_fhir/`, `libs/pulsebridge_hl7v2/`, `libs/pulsebridge_gcp/`, one marked line each in `services/cuff-sim/` and `services/health-sync/`, `scripts/fhir-validate-expected-warnings.json` | done |

**T08 FHIR validator in CI.** Runs the HL7 FHIR validator (R4, base profiles) over the example
resources from T06.
- AC: CI fails when an example resource is made invalid (demonstrated in the task summary).
- The examples are all the files in `libs/pulsebridge_fhir/examples/` (including three Bundles:
  Patient setup, heart-rate ingest, blood-pressure ingest; and the send-attempt AuditEvents). Expected validator warnings, not errors: the
  heart-rate minimum and maximum component codes and the local `urn:pulsebridge:*` code systems.

**T09 Synthetic data seeder.** Backs the `use_synthetic_data` flag: generates plausible heart-rate
roll-up windows (the same five-minute shape the Google Health API returns) for a synthetic patient
and writes them through the T06 mapper.
- AC: output is deterministic for a given seed; re-running does not duplicate Observations.
- AC: no real health data is used.

**T10 `health-sync` service.** Pulls five-minute heart-rate roll-ups from the Google Health API
(`dataPoints:rollUp`, see D8 and `docs/spikes/s3.md`), maps them with T06, writes a transaction
Bundle.
- AC: idempotent on re-delivery (conditional update keyed on patient, code and window start).
- AC: writes with `heart_rate_transaction` from T06 (an ingest Bundle: Patient and Device are
  created only if absent, never overwritten, D11), passes an IANA time zone from configuration,
  and passes a cutoff so a window is `preliminary` until 60 minutes after it ended and `final`
  after that. Any run 60 to 175 minutes after a window ended finalises it, so about seven
  missed runs are tolerated; a longer outage is recovered with the backfill.
- AC: each run reads what is stored once and writes only windows that are new or changed; if
  the read fails or time is short it writes everything. A window is never skipped wrongly.
- The check that the store resolves in-Bundle references to a Patient that already existed
  needs a deployed stack; it moved to T29.
- AC: a day of data is about 580 Bundle entries; the backfill batches its writes.
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
  prompts for them without echo and stores them. The client secret is never committed,
  printed or logged. The client ID is not committed or logged, and is printed only inside
  Google's authorization address in `--paste` mode, where the user must open it by hand (a
  client ID is public under RFC 6749 section 2.2).
- AC: runs the OAuth authorization-code flow with PKCE and a `state` check, requesting offline
  access and a consent prompt (without them Google returns no refresh token), and stores the
  refresh token as a new Secret Manager version; prints only a success message.
- AC: listens on a loopback port for the redirect when run on the deployer's machine, and
  accepts a pasted redirect address when it cannot.
- AC: reports when the refresh token will expire (7 days while the OAuth app is in Testing status).
- AC: the scope requested is `googlehealth.health_metrics_and_measurements.readonly`, as a variable.
- AC: after connecting, it writes the full Patient once with T06's setup Bundle (D11), using the
  Google Health user ID from the identity call and demographics from configuration, and uses the
  device name exactly as Google reports it or writes no Device.
- AC: `health-sync` needs exactly these three secrets and nothing else from the deployer.
- AC: its README is written in terms a new user can follow; carrying it into
  `docs/setup.md` is T28's.
- Nothing in T11 was run against Google or GCP. The assumptions to confirm on the first real
  run are listed in its README.

**T12 `v2-to-fhir` service.** Also creates `libs/pulsebridge_gcp/`, a thin shared client for the
Cloud Healthcare API (FHIR `executeBundle` and resource reads and writes; HL7v2 message get,
list, label patch and ingest) that later services reuse, and sets the layout later services
follow. Pub/Sub push handler plus the v2-to-FHIR mapping, which is the
showcase code. The mapping lives in a single module (`services/v2-to-fhir/mapping.py`) with no
GCP calls, so it can be read and tested on its own. Input is the raw ER7, fetched with
`view=RAW_ONLY` and parsed with `hl7apy` (spike S5: the store's `parsedData` numbers MSH fields
one lower than HL7, changes key shape when a field repeats, and cannot be produced without GCP);
the service README says why. Handler steps: read the message name from the notification; fetch;
parse; map; write to FHIR; patch the outcome labels; acknowledge.
- AC: OBX-3 LOINC codes map to `Observation.code` (panel 85354-9) and `component` (8480-6, 8462-4);
  OBX-5 and OBX-6 map to the component value and UCUM unit.
- AC: OBX-14 maps to `effectiveDateTime`, with the time zone handled explicitly.
- AC: MSH and PID identifiers map to the `Patient` and `Device` references.
- AC: writes with `blood_pressure_transaction` from T06 (an ingest Bundle, D11). The Patient is
  built from PID-3.1 and PID-5 only, and an empty name part does not fail the message. A PID-3.1
  that is not the configured patient is an application-level `error`.
- AC: a `Provenance` points back to the HL7v2 store message name.
- AC: the write is a conditional update keyed on the message control ID, so a redelivered push
  does not create a second Observation.
- AC: every message gets the labels `application_outcome` (`accepted` or `error`) and `reason`
  (a short lowercase code such as `missing-obx-14`) on the stored HL7v2 message, patched with
  `updateMask=labels`. Both keys are written in one patch so a stale reason cannot remain, and
  other labels are left alone. Values stay inside the documented label limits.
- AC: a notification whose `msgType` attribute is `ACK` is acknowledged and ignored.
- AC: every `hl7apy` exception, including an unsupported version, becomes an `error` outcome, and
  a message that no longer exists (404) is acknowledged, not retried.
- AC: the `Provenance` source entity is the message name from the notification (its target is
  the Observation).
- AC: unknown OBX-3 code, missing OBX-14 and non-numeric OBX-5 each give outcome `error` with a
  distinct reason and write no Observation.
- AC: on any mapping error the push is still acknowledged; the handler never raises to Pub/Sub
  and the subscription never stalls (see D7).
- AC: unit tests run the mapping against the T07 sample messages, with at least one fixture per
  malformed class.

**T13 Cuff simulator backend.** Not publicly invocable: only `fhir-proxy` calls it (D10). Builds an ORU^R01 with the T07 builder from bounded systolic and
diastolic values, validates it with `hl7apy` before sending, and sends it by MLLP (default) or
HTTPS ingest; returns the ACK.
- AC: a message that fails validation is never sent, unless "send malformed" is set.
- AC: a `send_malformed` option bypasses validation and sends a chosen malformed sample from T07
  (never free text); it is still rate-limited.
- AC: both transports return one result shape: acknowledged with code, control ID, reason and
  raw ACK, or not acknowledged with why. Over HTTPS, a 200 with `hl7Ack` is `AA`; a 400 or 409
  carrying `hl7Nack` is a NACK (`AE`), not a transport failure; any other error, or a network
  error, is "no acknowledgment received" with the HTTP status. The ACK bytes over HTTPS have no
  MLLP frame and no trailing carriage return.
- AC: the stored message's name from the ingest response is returned to the caller, and HTTPS
  sends carry a `transport` label; "send malformed" over HTTPS offers only the samples that apply
  (there is no HTTPS equivalent of bad framing).
- AC: a NACK from the store is returned to the caller as the commit-level acknowledgment with
  the reason from MSA-3, not turned into a generic error. MSA-2 is not required to match on a NACK
  (it is empty when the header could not be read), and the reply type `ACK` is accepted.
- AC: the MLLP client has a read timeout and reports three distinct outcomes: an ACK frame,
  connection closed with no data, and timeout. The last two are "no acknowledgment received".
- AC: segments end with a carriage return all the way to the wire; nothing converts them to line feeds.
- AC: inputs outside bounds are rejected; no free text reaches the message.
- AC: every send attempt, including a rejected or unacknowledged one and a "send malformed"
  one, is recorded as an `AuditEvent` built with T31 and written to the audit FHIR store (D12,
  D15). The simulator builds that resource itself from validated values and never forwards FHIR
  supplied by a caller. A
  failure to record is logged and reported in the response, but never hides the send's result.
- AC: uses the shared `pulsebridge_gcp` client (T12) for HTTPS ingest and for writing the
  AuditEvent. A 401, 403 or 404 from the API and a network failure are recorded as "no
  acknowledgment" with one of T31's reasons, never raised to the caller.
- AC: every reading gets a new, unique control ID (MSH-10): the mapper keys the Observation on
  it, so a reused ID would overwrite an earlier reading.
- AC: rate-limited; both transports covered by tests against a fake listener.
- AC: it has no login of its own and relies on Cloud Run's invoker check; the README says so.

**T14 FHIR proxy: read API.** Narrow read API for the dashboard: Observation search by patient,
code and date; trace lookups (v2 message, commit-level ACK, application-level outcome and reason,
source JSON); status strip data; messages log; `CapabilityStatement`; and the reviewer login from
D9 (`docs/spikes/s6.md`): a login endpoint that checks the shared password and issues a signed
bearer token, the token check on every other route, CORS for the dashboard's origin, and a login
rate limit. It also forwards cuff requests to the cuff simulator (D10).
- AC: the messages log lists stored HL7v2 messages newest first by send time and filters by the
  outcome labels; a message with no outcome label is reported as "pending". For a stored message
  the commit-level acknowledgment is `AA` by definition.
- AC: the messages log is built from the send-attempt `AuditEvent`s in the audit FHIR store
  (D12, D15), newest first, joined
  to the stored message and its outcome labels where the send was accepted. Rejected and
  unacknowledged attempts appear with their reason and raw ACK; a stored message with no
  AuditEvent (for example one sent straight to the adapter) is still listed.
- AC: forwards cuff requests unchanged (the simulator refuses unknown fields) and passes
  through 400, 413, 422, 429 and 500. The forward timeout defaults to 55 s, because Firebase
  Hosting ends a forwarded request at 60 s; T22 lowers the simulator's timeouts to fit (at
  least 90 s applies only when Hosting is not in front). It joins an accepted MLLP
  attempt to its stored message by the derived message name in the AuditEvent (T33), confirms
  it with a `get`, and shows it as derived; a control ID is not a key (samples share them).
- AC: `health-sync`'s `/status` always answers 200; any other status means that service
  failed. `googleHealth: "unknown"` is a state, not an outage.
- AC: the routes implement, or deliberately amend, the contract the dashboard shell proposes in
  `web/README.md` and `web/src/api/contract.ts`, and settle its open questions (whether searches
  are pinned to the configured patient; the date-boundary rule for heart-rate windows; where the
  heart-rate source JSON and the raw HL7v2 text of a valid send come from; where FHIR
  transaction rows come from). Any change to the contract is reported so the dashboard's client
  and mock can follow.
- AC: only the login route is unauthenticated; the cuff route can answer 429 with `Retry-After`.
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
- AC: implements the share route as the dashboard defines it (`web/README.md`, "For T15"):
  the request carries the Observation id only; refusals before OpenEMR is called answer an
  OperationOutcome (400, 404, 422, 503); failures after it was called, including the request
  budget running out, answer 502 or 504 with `{operationOutcome, steps}`, the one documented
  exception to "every error is an OperationOutcome". Shares are serialised per instance.
- AC: trace steps use `extensions.trace_step`, with its redaction corrected (owned here: it
  lives in `fhir_proxy/extensions.py`): OAuth parameter names are hidden only in the token
  step's bodies; in FHIR bodies only names that are never FHIR elements are hidden, so
  elements named `code` or `token` are shown unchanged (test: an Observation step keeps
  `code.coding[0].code` = `8867-4` and `valueQuantity.code` = `/min`; a token step hides
  `client_assertion` and `access_token`). Search parameters go in `request.query`; the url
  stays path-only.
- AC: the Encounter is reused per patient and UTC calendar day of the reading
  (`effectiveDateTime` or `effectivePeriod.start`), found with `Encounter?patient=&date=`. A
  repeat share of a blood-pressure reading and of a heart-rate reading (`effectivePeriod` with
  components) is checked against a live OpenEMR at T25: it must give `already-shared` with no
  second Patient, Encounter or Observation. Spike S1 verified this for blood pressure only; if
  a heart-rate repeat is not refused, that is reported and the page's wording changes.
- AC: reads the OpenEMR client document the `openemr` module stores (its README, "For T15"):
  RS384 assertion with the document's key ID, the scopes the document lists, TLS trusting only
  the document's certificate with host checking on, one re-read on `invalid_client`. The
  default scopes also permit update and delete in OpenEMR, so the proxy itself only creates,
  reads and searches. The document, the assertion and the token never appear in a trace.
- AC: the Observation sent carries no reference to our own store: `subject` and `encounter`
  are OpenEMR's, and `performer`, `device`, `meta` and `identifier` are dropped (OpenEMR does not
  keep `performer`; see spike S1 and the T06 README). Only `final` Observations are shared; an
  `amended` one is not.
- AC: the Patient sent has a `PT`-typed identifier, a name with `use: official`, gender and birth date.
- AC: the Patient sent is built with T06's `patient_for_sharing`, so the Google Health user ID,
  `id` and `meta` never go to the clinic; a Patient that is not complete is refused with what is
  missing. Only `final` Observations are shared.
- AC: OpenEMR's "already exists" `400` for a repeated reading is reported as already shared, not
  as a failure.
- AC: after a share, the proxy reads the Patient, the Encounter and each shared Observation
  back from OpenEMR's FHIR API by id and returns them for the "As stored in OpenEMR" panel
  (D13). It reads by id, not by search: spike S1 found the patient-and-category search returns
  placeholder Observations. The client's scopes include the reads this needs and nothing more.
- AC: no link to OpenEMR's screens is returned; they are not reachable from the internet.
- AC: failures follow the dashboard contract: a share refused before OpenEMR is called (for
  example an incomplete Patient, with what is missing) is a 422 with an `OperationOutcome`;
  OpenEMR unreachable is a 502; a failure part-way returns the steps completed so far.
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

**T31 AuditEvent builder for send attempts.** Adds to the T06 library a builder for the FHIR R4
`AuditEvent` that records one cuff send attempt (D12), and a function that wraps it in a write
the simulator can send. Pure Python, no GCP calls.
- AC: the AuditEvent has the R4 required elements (`type`, `recorded`, at least one `agent` with
  `requestor`, `source.observer`) with codes from the standard value sets, chosen and explained.
- AC: it records the transport (MLLP or HTTPS), the outcome (accepted, rejected with the reason
  from MSA-3, or no acknowledgment with why), the message control ID, the stored message name
  when there is one, the sample name when "send malformed" was used, and the raw ACK bytes when
  one was received. The R4 `outcome` code is set consistently with that result.
- AC: it never contains free text from the caller, and the raw message is included only for
  the synthetic malformed samples.
- AC: writing the same attempt twice does not create two AuditEvents (deterministic ID).
- AC: a function reads an AuditEvent back into the fields the messages log needs.
- AC: an example is added to `libs/pulsebridge_fhir/examples/` for the validator (T08).

**T32 Audit FHIR store for send attempts.** The simulator writes AuditEvents (D12). So that it
has no access to the store holding health data, they go to a second, separate FHIR store (D15).
- AC: the healthcare module creates a second FHIR R4 store for audit data, with the same
  deletion behaviour as the first and its IDs and paths as outputs.
- AC: `cuff-sim` can write to the audit store and has no binding at all on the health-data
  store; `fhir-proxy` is read-only on both stores; no other workload has access to the audit
  store. Tests assert each of these.
- AC: a static check fails if the module declares any FHIR or HL7v2 store IAM resource other
  than the ones driven by the role table, or any authoritative binding or policy.
- AC: the module READMEs say plainly what each role on each store allows, including reading.

**T33 Library follow-ups from wave 3 reviews.** Small changes to the shared libraries that the
reviews of T08, T10 and T13 asked for, each with tests and break-checks.
- AC (`pulsebridge_hl7v2`): `parse_ack` raises only `AckParseError` for any input bytes (today
  `MSH^~MSH|` raises `IndexError`); a seeded fuzz test proves it.
- AC (`pulsebridge_hl7v2`): a sample that has a PID segment can be returned with another patient
  (`with_patient`), built with `hl7apy` at segment level (no ER7 field splitting, D3); only PID
  changes and the sample's deliberate defect is untouched. The cuff simulator uses it (the line
  marked `FOLLOW-UP` in `cuff_sim/service.py`), so the "parses but cannot be mapped" samples reach
  their intended reason on any deployment.
- AC (`pulsebridge_fhir`): the send-attempt AuditEvent can carry a message name that was derived
  rather than returned by the store, marked as derived, and `parse_send_attempt` returns it with
  that marker. The cuff simulator records it for an accepted MLLP send (the other `FOLLOW-UP`
  line). The AuditEvent key gains whatever is needed so two samples sent in the same millisecond
  do not collide.
- AC (`pulsebridge_fhir`): `heart_rate_transaction` can write named windows as `amended`
  (R4: modified after being final); `health-sync` uses it when a stored `final` window's values
  changed.
- AC (`pulsebridge_fhir`): heart-rate and blood-pressure Observations set `performer` to the
  Patient; the README documents the `PT` identifier type (used because OpenEMR searches by it),
  the `R01` AuditEvent subtype and the absence of narrative as deliberate. The examples are
  regenerated and the validator's expected-warnings file is updated to match (`make
  fhir-validate` passes).
- AC (`pulsebridge_gcp`): a request that timed out is distinguishable from other network
  failures without inspecting `__cause__`.

## Wave 3: dashboard features and infrastructure for services

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T17 | Dashboard: heart-rate chart and right trace panel | T16 | `web/src/heart-rate/`, and `web/package.json` and `web/package-lock.json` only to add the chart library | done |
| T18 | Dashboard: cuff simulator and left trace panel | T16, T13, T34 | `web/src/cuff/` | done |
| T19 | Dashboard: share action and messages log | T16, T34, T14, T18 | `web/src/share/`, `web/src/messages/`, and, to follow the proxy's contract, `web/src/api/`, `web/README.md` and the minimum needed in `web/src/cuff/` and `web/src/shell/` | done |
| T20 | Module `ingestion` | T04, T05 | `infra/modules/ingestion/` | done |
| T21 | Module `edge-vm` | T03, S2, S4 | `infra/modules/edge-vm/` | done |
| T22 | Module `phr` | T03, T04, T14, T20 | `infra/modules/phr/`, `services/fhir-proxy/Dockerfile` and the proxy's entries in `tests/tooling/` and `.dockerignore` | done |
| T23 | Module `openemr` | T21, S1 | `infra/modules/openemr/` | done |
| T24 | Container build pipeline | T01 | `cloudbuild.yaml`, `services/*/Dockerfile` | done |
| T34 | Dashboard shell follow-ups from T13 and T17 | T13, T17 | `web/` except `web/src/heart-rate/` | done |
| T36 | First-deploy follow-ups | T25 | `infra/modules/identity/`, `infra/bootstrap/README.md`, `infra/envs/README.md`, `docs/setup.md`, `README.md` | done |
| T37 | Dashboard browser tests independent of the time of day | T12, T14 | `web/` | done |
| T38 | Healthcare agent grants and the edge-VM script test | T36 | `infra/modules/healthcare/`, `infra/modules/identity/` (outputs only), `infra/envs/_stack/`, `infra/modules/edge-vm/tests/` | done |
| T39 | Cloud Healthcare searches: stranded count and the stored-window read | T06, T29 | `services/health-sync/`, `libs/pulsebridge_fhir/`, `libs/pulsebridge_gcp/`, `services/seed/` | done |
| T40 | Default `trusted_proxy_hops` to 2 behind Firebase Hosting | T22, T25 | `infra/modules/phr/`, `infra/envs/`, `services/fhir-proxy/` (README, tests), `docs/setup.md` | done |
| T41 | Page deployer grants in code; CI checklist from the first real run | T26 | `infra/bootstrap/`, `infra/modules/phr/`, `infra/envs/`, `.github/workflows/` (comments only), `docs/setup.md` | done |
| T42 | Edge VM: survive a slow MariaDB first initialisation | T21 | `infra/modules/edge-vm/` | in-progress (reviewed, merged into the working branch, applied to prod; first boot on it under way) |
| T35 | Seeder image and shared stored-window logic | T09, T10, T24 | `services/seed/Dockerfile`, `libs/pulsebridge_fhir/` or a new library for the stored-window logic, the `stored.py` of `services/seed/` and `services/health-sync/`, the seeder's entries in `tests/tooling/` | done |

**T17 to T19 Dashboard features.** As listed under "Product requirements" in the brief. Each
task fills its own directory through the mount function and slots defined by the shell (T16) and
follows `web/README.md`: the ownership table, the typed client, the selection store, the
data-changed notification and the polling convention for pending outcomes. Only T17 may add a
dependency (the chart library). A task that needs a change to the shared client, the mock, the
contract or the shell reports it to the orchestrator instead of editing those files.
- AC (T17): 24 h, 7 d and 30 d windows of five-minute averages; min, mean and max for the window;
  selecting a point fills the trace panel with that roll-up window. A window that is not `final`
  is shown as provisional and is selected as not shareable, with the reason.
- AC (T18): bounded inputs, transport toggle, "send malformed" checkbox, readings list with ACK
  code, three trace tabs; the trace shows the commit-level acknowledgment and the application-level
  outcome as two separately labelled items.
- AC (T19): the dashboard's contract, client, parser and mock follow the proxy as built: every
  item in the "amendments" list of `services/fhir-proxy/README.md` (for example `messageLookup`,
  `possibleDuplicateOf` on store-only rows, nullable counts, `partial` pages, 400 for a refused
  search, 422 as a final answer for an unreadable trace, pages followed until `nextCursor` is
  null). An attempt whose stored message was not found shows no outcome and stops polling.
- AC (T19): each v2 row in the messages log shows the commit-level acknowledgment and the
  application-level outcome with its reason, with "pending" for a message not yet mapped; reason
  codes are shown as readable text.
- AC (T19): share acts on the current selection and shows its trace, followed by an "As stored
  in OpenEMR" panel with what was read back (D13); messages log lists cuff send attempts,
  including rejected and unacknowledged ones (D12), and FHIR transactions.

**T20 Module `ingestion`.** Cloud Run services `health-sync` and `v2-to-fhir`, Scheduler job,
OIDC invoker bindings.
- AC: services scale to zero and are not publicly invocable; images are referenced by digest
  in the variables `v2_to_fhir_image` and `health_sync_image` (the names the build prints).
- AC: `v2-to-fhir` gets the environment variables listed in its README, and Pub/Sub pushes to
  its `/pubsub/hl7v2` route; a bad store ID stops the service starting, so it fails the deploy.
- AC: `health-sync` is configured as its README's "For T20" section says: the three Google
  Health secrets mounted as files (version `latest`, one directory each), `max_instance_count
  = 1`, Scheduler `retry_count = 0` and `attempt_deadline = 180s`, a request timeout of at
  least 180 s, `fhir-proxy` allowed to invoke it (for `/status`), and log-based alerts on a
  failed run and on "reconnect needed".
- AC: when `use_synthetic_data` is true, the seeder (T09) runs instead of `health-sync`: a
  Cloud Run job as the `seeder` account, image `seed_image` by digest, no retries, started
  hourly by Scheduler with `--hours 4`; the README says how to run the first `--days 30`
  seeding and how to size the job timeout. Nothing is created for it otherwise.
- AC: there is a documented way to replay the dead-letter subscription. A message whose
  notification was dead-lettered (for example because a role was missing) stays "pending"
  until it is replayed; the README says how to find and replay such messages.

**T21 Module `edge-vm`.** e2-micro, startup script, swap file, persistent disk, MLLP adapter and
MariaDB containers. The firewall rule that lets Cloud Run reach the VM lives in `project-base`
(D14); this module adds none.
- AC: no public ports and no external IP (D13, D14); machine type is a variable defaulting to the
  free-tier size.
- AC: the VM has no route to the internet, so Docker and the images cannot come from it. Use
  an image with Docker preinstalled (Container-Optimized OS is the first choice) and pull the
  OpenEMR and MariaDB images through the Docker Hub mirror from T03. First confirm that
  Artifact Registry is reachable over Private Google Access; if it is not, the fallback is
  Cloud NAT (about $4.70 a month), stated in the README.
- AC: the VM's service account can read from the Docker Hub mirror repository.
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
- AC: the dashboard is served by Firebase Hosting (D4), with its idle cost stated; the README
  says whether a rewrite puts the page and the proxy on one origin and what that changes for CORS.
- AC: the Firebase Hosting API and the deployer's hosting role are added through the existing
  variables of `project-base` and bootstrap, not hardcoded.
- AC: `fhir-proxy` is publicly invocable, mounts the reviewer password secret as a file (so a
  new version takes effect without a redeploy), and is capped at two instances.
- AC: the cuff simulator is invocable only by the proxy's service account, and is configured
  as its README says: `MLLP_TIMEOUT_SECONDS`, `MLLP_HOST` as the VM's IP address, one gunicorn
  worker (the rate limit is per instance), a 90 s request timeout, and the same patient
  identifier and authority as `v2-to-fhir`. In a synthetic deployment its patient name is the
  seeder's synthetic name (family `Synthetic`, given `Demo`), or the seeder refuses the
  Patient the cuff path creates.
- AC: the hosting layer sets a Content-Security-Policy header.
- AC: `fhir-proxy` uses Direct VPC egress on the serverless subnet with all traffic routed
  through the VPC (`ALL_TRAFFIC`), so its calls to `health-sync` and the cuff simulator count
  as internal and both stay internal-only (see the ingestion module's README, "For T22"). The
  proxy talks only to Google services and the VM, so this needs no NAT.
- AC: `fhir-proxy` gets its container image (Dockerfile following "Adding an image" in
  `services/README.md`, started with `--preload`, one worker and eight threads; it leaves the
  no-image allow-list and gains a smoke case) and is configured as its README's "For T22"
  section says: `PUBLIC_BASE_URL`, `PATH_PREFIX`, the allowed origin, per-instance concurrency
  equal to the thread count, `TRUSTED_PROXY_HOPS` for the one reachable path (the page reaches
  the proxy only through the hosting rewrite; the value is measured on the first deploy), and
  invoker rights on both `health-sync` and the cuff simulator.
- AC: the cuff simulator's timeouts are lowered so its worst case fits under the hosting
  layer's 60 s limit (`HTTPS_TIMEOUT_SECONDS=6`: 29 s over MLLP, 48 s over HTTPS), and its own
  request timeout is above the proxy's forward timeout so a send the proxy gave up on is still
  recorded.
- AC: images are given by digest in the variables `fhir_proxy_image` and `cuff_sim_image`
  (the names the build prints). The README states the hosting layer's request timeout for
  forwarded requests and whether it is shorter than the cuff simulator's documented worst
  case (80 to 90 s); if it is, the proxy's own timeout is set below it so the page gets a
  clear answer.

**T23 Module `openemr`.** OpenEMR container on the edge VM, database connection, OAuth2 client
registration script.
- AC: the OAuth2 client's private key exists only in memory on the VM and in Secret Manager
  (never in state, metadata or logs). The client ID, key ID and OpenEMR's certificate are not
  secret and travel with the key in the same secret version, so `fhir-proxy` reads one
  consistent document (the ID is generated by OpenEMR on the VM, so it cannot be a variable).
- AC: no reviewer account is created in OpenEMR and its screens are not exposed (D13); the
  registered client's scopes cover the writes and the read-back and nothing else.
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
- AC: builds on the seam in the edge-vm README: a boot hook that starts OpenEMR after MariaDB
  is healthy, bind mounts with `--mount type=bind` (never `-v`), extra secrets through
  `secret_files` (a missing one must not stop MariaDB), a graceful stop, and the metadata
  server blocked for the OpenEMR container so it cannot obtain the VM account's token.

**T24 Container build pipeline.** Dockerfiles and a Cloud Build config that builds on tag and
reports image digests.
- AC: images run as a non-root user; a build prints the digest to put in `tfvars`.
- AC: each image installs the local libraries it needs from `libs/` before the service; they
  are declared by name only and are not on PyPI. `v2-to-fhir` starts with the gunicorn command
  in its README and honours `PORT`.

**T35 Seeder image and shared stored-window logic.** Two follow-ups from T09 and T24.
- AC: `services/seed/Dockerfile` follows "Adding an image" in `services/README.md`; its command
  is the `pulsebridge-seed` console script (a Cloud Run job, not gunicorn); `seed` leaves the
  no-image allow-list and gains a smoke case (`--dry-run`); the build prints `seed_image`.
- AC: the "read what is stored once, write only what changed" logic exists once, in a library,
  used by both `health-sync` and the seeder with no change in either service's behaviour
  (their existing tests pass unchanged apart from imports).

**T34 Dashboard shell follow-ups.** Changes to shell-owned files that T13 and T17 showed are
needed before T18 and T19 start.
- AC: the status strip announces `sync` (`notifyDataChanged('sync')`) when the watch's last sync
  time changes, so features re-fetch and a provisional window can become final during a session.
- AC: the bundle check fails if the chart library is in the main chunk.
- AC: the trace contract says whether the Google Health JSON is `reconstructed` or `original`;
  the heart-rate trace uses it. The mock has a data set in a fixed IANA zone that crosses a
  daylight-saving change, with a browser test.
- AC: the cuff contract matches the service (`services/cuff-sim/README.md`): response fields
  `result`, `framing` and `messageNameSource`; `messageName` over MLLP is derived and shown as
  such; `attemptId` may be null; `systolic` and `diastolic` are optional with `sendMalformed`;
  bounds are the service's (1 to 400 mm[Hg], systolic above diastolic); responses 400, 413, 422,
  429 and 500 are listed. The mock follows.
- AC: `web/README.md` lists uPlot under runtime dependencies, explains that the client converts
  the wire shape into the parsed shape, and requires feature-prefixed element ids.

## Wave 4: integration, CI/CD and documentation

| ID | Task | Depends on | Owns | Status |
| --- | --- | --- | --- | --- |
| T25 | Env roots `dev` and `prod` | T02 to T05, T20 to T23 | `infra/envs/`, and the module fixes its review asked for in `infra/modules/project-base/`, `infra/modules/openemr/`, `infra/modules/edge-vm/` and `scripts/connect-google-health/` | in-progress (dev deployed 16 Nov; 17 Nov: reboot, patching, seeder alert, Scheduler and rewrite-path items done on dev, and the hop measurement (2 hops behind Hosting; dev runs with 2); password rotation, the shell-only items, destroy test and prod remain) |
| T26 | Terraform CI/CD | T02, T25 | `.github/workflows/terraform-*.yml` and `ci.yml`'s Terraform job, `.tflint.hcl`, the Makefile's `tf-lint`/`tf-scan`, `infra/envs/ci/`, and what its ACs needed in `infra/bootstrap/`, `infra/modules/identity/`, `infra/modules/project-base/`, `infra/modules/ingestion/`, `infra/envs/`, plus README corrections from the T28 review | in-progress (offline part merged; 18 Nov: CI switched on for dev: ruleset, `dev-page` Environment, planner and page deployer in dev's bootstrap, `ci/dev.tfvars`, the five `DEV_` variables; first real run under way; prod not set up) |
| T27 | Secret version scripts | T04, T25 | `scripts/secrets/` (`set-secret.sh`, `set-reviewer-password.sh`), root `pyproject.toml` `testpaths`, a shellcheck pre-commit hook for `scripts/secrets/` | done |
| T28 | Setup guide and README | T25, T11 | `README.md`, `docs/setup.md`, `docs/images/` | done |
| T29 | End-to-end smoke test | T25 | `tests/e2e/` | in-progress (merged; cuff path run live on dev; the health-sync checks need a real-data stack) |
| T30 | Public demo domain | T25 | `infra/envs/prod/dns.tf` | deferred (17 Nov: the demo uses prod's Firebase Hosting address) |

**First dev deploy (16 November).** A new dev project was bootstrapped and runbook steps 1 to 11
were run with synthetic data. Working end to end: edge VM first boot (all steps ok, about 13
minutes), image build, full apply, first seeding (8,365 windows), ingest on, page deploy, reviewer
login, heart-rate search and trace, cuff readings over MLLP and HTTPS (`AA`, mapped to final
Observations), share of a blood pressure and a heart rate to the real OpenEMR 8.5.0 (`shared`,
then `already-shared` on repeat). Fixed on the way, each with a test: `set-secret.sh` read a
gcloud warning as an existing version; alert policies needed `resource.type` in the metric
filter; fhir-proxy counted with `_summary`, which Cloud Healthcare refuses.

**T36 First-deploy follow-ups.** From the dev deploy; each needs code or docs:
- AC: module `identity` creates the Pub/Sub service agent (`google_project_service_identity`)
  before granting it token-creator; on a new project the grant failed until the agent was
  created by hand.
- AC: bootstrap and the env roots document running from a service account in another project:
  API quota and `iamcredentials` are charged to the key's home project, so bootstrap needed
  `USER_PROJECT_OVERRIDE`/`GOOGLE_BILLING_PROJECT`, and provider impersonation
  (`deployer_service_account_email`) did not work; a token minted with `x-goog-user-project` did.
- AC: the runbook says the deployer cannot read logs (its "worked when" checks that read logs
  need the owner or `roles/logging.viewer`), that `gcloud builds log` has no step prefix, and
  that a by-hand build runs as the Compute default service account.
- AC: the master checklist records what the dev deploy confirmed (E, O, I, P and C items) and
  what is still unchecked (hop measurement, alerts firing, destroy, patching, rotation).
- AC: the README's OpenEMR screenshot (T28) is taken from the dev deployment.

T36 outcome: reviewed (pass with notes) and merged. Module identity now creates the Pub/Sub
service agent before the Token Creator grant; the runbook, checklist and READMEs record what the
dev deploy showed. The OpenEMR screenshot is not part of T36: it needs a person and stays with T28.
Applied to dev on 16 November as the deployer: one resource added, nothing changed or destroyed,
so the deployer can create the agent (checklist A22). Follow-ups not yet assigned to a task: module healthcare grants roles to the Pub/Sub
agent without depending on its creation (a single full apply on a new project can race; a re-run
fixes it), and because healthcare reads the project inside a module that depends on project-base,
enabling any new API (seen with `enable_iap_ssh = true` on dev, 16 November) makes a full plan
replace healthcare's three agent grants although nothing about them changed (avoided on dev with a
targeted apply of the two IAP resources; IAP SSH was turned off again the same day, after the
OpenEMR screenshot was taken); `infra/modules/edge-vm/tests/startup_script.sh` pipes `printf` into `grep -q` under
`pipefail`, which can fail falsely and, in its negative checks, pass falsely.

**T38 Healthcare agent grants and the edge-VM script test.** Two follow-ups from T36's review and
the dev deploy.

1. Module healthcare grants roles to two Google-managed service agents (Healthcare, Pub/Sub) whose
   addresses it builds from `data.google_project` read inside the module. Two faults: nothing
   orders the Pub/Sub grants after identity's `google_project_service_identity.pubsub`, so a single
   full apply on a new project can reach them before the agent exists; and when anything
   healthcare depends on changes (seen on dev when an API was enabled), the data source is read
   only at apply, the members become unknown and the plan replaces all three grants.
2. `infra/modules/edge-vm/tests/startup_script.sh` pipes `printf` into `grep -q` under
   `pipefail`: when grep exits early the pipeline can return 141, so positive checks can fail
   falsely and negative checks can pass falsely.

- AC: healthcare's grants to the Pub/Sub agent are ordered after the agent's creation in a single
  apply, without a module-level `depends_on` that defers healthcare's data sources.
- AC: the members of healthcare's agent grants are known at plan time and do not change when an
  unrelated API is enabled or another project-base resource changes; a test proves it and fails
  when the fix is removed.
- AC: on the deployed dev state the change must not replace the three existing grants: the member
  strings stay byte-identical (state it, with the reasoning; the orchestrator plans against dev).
- AC: the Healthcare service agent's existence on a new project is handled the same way or the
  README says why it does not need it.
- AC: no `printf | grep -q` (or equivalent early-exit pipe) remains in the edge-VM script test;
  a demonstration shows the old form failing or passing falsely and the new form not.
- AC: `make tf-validate tf-test tf-lint tf-scan lint test pre-commit secret-scan` pass without
  credentials; READMEs and the runbook's finding are updated.

T38 outcome: reviewed (pass with notes) and merged. Cause: the stack's module-level `depends_on`
on healthcare made its project lookup wait for any pending change in project-base. Healthcare now
takes the project number and the Pub/Sub agent's address as inputs, creates the Healthcare
service agent itself, and is ordered after the APIs through preconditions on `enabled_apis`.
Planned against dev on 16 November (read-only): one resource to add, nothing replaced; with
`enable_iap_ssh = true`, only the two IAP resources in addition. Applied to dev on 17 November as the deployer: one
resource added, nothing changed or destroyed, and a plan afterwards shows no changes. Notes
left open: `check_agent_ordering.sh` says more broadly than is true that healthcare does not
reach project-base's network (two resources do, through ingestion, harmlessly); its required
line for the subscriber grant passes without the fix (the dead-letter line is the guard); static
check 11 misses a pipe split across lines; the same `printf | grep -q` form remains in the
static checks of ingestion, phr and project-base; identity's Pub/Sub agent is not ordered after
the Pub/Sub API on a new project.

**T39 health-sync: count stranded windows without `_summary`.** `count_stranded`
(`services/health-sync/health_sync/stored.py`) counts preliminary windows older than the cut-off
with `_summary=count`, which Cloud Healthcare refuses (seen on dev for fhir-proxy, fixed there with
`_total=accurate&_count=0`). On real data every run would log `stranded-count-failed`,
`stranded_preliminary` would stay null and `/status` would never report `backfill-needed`.
- AC: the count uses a search Cloud Healthcare supports (`_total=accurate` with `_count=0`), and
  reads `total` from the searchset Bundle; a missing or non-numeric `total` is a failure, not zero.
- AC: health-sync's fake FHIR store refuses `_summary` as the real store does, so a regression
  fails a test; audit every other search the service sends against what the fake accepts and what
  fhir-proxy's fake (which follows the real store) refuses.
- AC: tests break-checked; README "Not verified" list updated; nothing run against GCP.

T39 outcome: reviewed (pass with notes; fixes made) and merged. Scope grew on 17 November when the
dev checklist showed every hourly seeder run rewriting all 48 windows of its range
(`windows_unchanged: 0`, 24 `amended`): the shared stored-window read asked for
`_elements=...effectivePeriod,valueQuantity...`, and Cloud Healthcare returns a choice element only
when asked by its base name (`effective`, `value`), so the stored end and average read as missing
and nothing ever compared equal. Fixed in `libs/pulsebridge_fhir` (shared by health-sync and the
seeder); the fakes now filter `_elements` and refuse `_summary` as the real store does; a read whose
windows all lack end or values now stops the run without writing (`stored_read:
failed:elements-missing`). Deployed to dev the same day (images v0.1.2): the next seeder run reported 42 unchanged, 3 new, 3
finalised, 0 amended. Left on dev: its earlier synthetic final windows carry status `amended`
and extra versions, and cannot be shared (T15 shares only `final`). Open design question, not
changed: after any other failed read the services write every window, which overwrites withdrawn
windows, writes `amended` back as `final` and can write a held final as `preliminary`.

**T40 Default `trusted_proxy_hops` to 2 behind Firebase Hosting.** The hop measurement on dev
(17 November, checklist P1) showed that through Hosting the visitor is the second
`X-Forwarded-For` entry from the right and that Hosting replaces a caller's own header; with 1
hop the login limit was keyed on Hosting's addresses.
- AC: the Terraform default is 2 wherever a deployment default is declared; descriptions say why.
- AC: the service's own default and the docs are consistent, and the READMEs state what a direct
  call to the proxy's `run.app` URL means for the per-address limit under 2 hops (the caller
  writes the entry that is then trusted), what still protects the login, and what would close it.
- AC: tests pin the default and an override, and the client-address behaviour for fewer entries
  than hops and for a direct call with a made-up left entry; break-checked; all gates pass.

T40 outcome: reviewed (pass with notes; documentation fixes made) and merged; the user chose 2
hops on 17 November knowing the limit below. Module phr and both env roots default to 2; the
service's own default stays 1 (alone at `run.app` it has one hop). Known limit, measured on dev
and written up in the phr README: the proxy's `run.app` URL must stay public for Hosting's
rewrite, Cloud Run appends to a caller's `X-Forwarded-For`, so under 2 hops a direct caller
chooses its key: it can evade the per-address login limit and keep a visitor whose public address
it knows at 429 for up to a minute at a time. The login is protected by the password's strength
(generate it). Possible follow-up, unverified: trust the second entry only when the right-most
one is in Google's published service ranges, else use the right-most.

**T41 Page deployer grants in code; CI checklist from the first real run.** The first real CI run
on dev (18 November, pull request 15 and the apply run on its merge) showed that
`roles/firebasehosting.admin` alone does not let the page deployer publish the page: the Firebase
CLI first checks the Hosting API with `serviceusage.services.get`, and finalizing a version whose
config rewrites `/api` to Cloud Run needs `run.services.get` on that service. Both were granted on
dev by hand (Service Usage Viewer on the project; Cloud Run Viewer on the proxy service only).
- AC: bootstrap gives the page deployer `roles/serviceusage.serviceUsageViewer` with
  `create_page_deployer`; tests pin it and the page deployer has no other project role.
- AC: the env roots grant the page deployer `roles/run.viewer` on fhir-proxy's service only,
  from an optional input (null: no grant); tests pin the member and that nothing else is granted.
- AC: on dev, a plan with the input set adopts or matches the hand-made grants without replacing
  anything (state how; the orchestrator plans against dev).
- AC: the checklist rows the run answered are filled in from the evidence (P12, CI2, CI4 to CI8,
  CI12, CI15, CI16, CI17 as far as seen), and the docs no longer say Hosting admin is enough.

T41 outcome: reviewed (pass with notes; wording fixes made) and merged. Dev's bootstrap applied
by hand on 18 November (1 added: the Service Usage Viewer grant adopted; 1 description updated);
`page_deployer_service_account_email` added to `ci/dev.tfvars`, so the next CI apply adopts the
run.viewer grant on fhir-proxy (a read-only plan showed 1 to add, nothing else). Known exposure,
low per the review: run.viewer lets the page deploy job read fhir-proxy's configuration, which
holds the patient ID (and with real data the name) in plain environment variables; the planner
already reads the same. Possible follow-up: move the patient's ID and name out of plain
environment variables into mounted secrets.

**T42 Edge VM: survive a slow MariaDB first initialisation.** Prod's first boot failed twice on
18 November: on a new, empty data disk the official MariaDB image initialises the database, then
starts a temporary server to set the root password and create its users, and waits for it only
30 seconds (`for i in {30..0}` in `docker_temp_server_start`, not configurable). On prod's
e2-micro that server took over 33 seconds both times, so the entrypoint stopped with "Unable to
start server"; Docker restarted the container on a half-initialised data directory without the
health-check user, every health check was refused ("Access denied for user 'mysql'@'localhost'"),
and the startup script waited out its deadline. Measured on the VM afterwards in throwaway
containers: the same step took 1 to 16 seconds with nothing else running, on the data disk and in
memory, with and without the module's server arguments; no single setting explains it (load and
CPU contention on a shared-core VM at boot are the likely cause, not proven). On dev on 16
November it took 3 seconds.
- AC: a first initialisation that takes longer than 30 seconds completes (the temporary server's
  wait is long enough for a slow boot, documented and bounded by the boot deadline).
- AC: a data directory left half-initialised (by this failure or an interrupted first boot) is
  recognised by a reliable marker and re-initialised once, never a directory that holds a
  completed database; the startup log says which happened.
- AC: tests in `infra/modules/edge-vm/tests/` cover both paths with a fake docker and fail when
  either is removed; the README's first-boot and "Not verified" sections are updated.

**T37 Dashboard browser tests independent of the time of day.** Three browser tests failed in CI
between about 12:17 and 13:10 UTC: the in-page mock follows the page clock, has no heart-rate
windows from 12:00 to 12:40 UTC (a modelled charging gap), and marks a window preliminary only
within 30 minutes of a sync run. Acceptance: every browser test runs on a controlled page clock
(`web/e2e/clock.ts`); the suites pass at any instant and zone; explicit tests cover "no provisional
window" and "a sync run that wrote nothing"; the mock agrees with `contract.ts` (no transaction row
for a run that wrote nothing; `lastSyncAt` is the newest heart-rate write). Reviewed (pass with
notes) and merged. Notes left open: the mock keeps a window in the row of the run that first wrote
it after a later run finalised it, which the real proxy would not; `mock-dst.e2e.ts` pins its own
clock instead of using the shared fixture.

**T25 status.** The offline part is merged: the roots, the shared composition `infra/envs/_stack`,
the runbook and the 116-item first-deploy checklist in `infra/envs/README.md`, all tested against
mocked providers only. Open, and needing a session with credentials and the user's go-ahead:
every AC that says "reaches a working stack", "destroy", "first boot" or "checklist".

**T25 Env roots.** `dev` and `prod` wire the modules; each has `backend.tf` and
`terraform.tfvars.example`; `use_synthetic_data` switches the wearable source.
- AC: `terraform validate` passes in both; a clean project reaches a working stack from `dev`.
- AC: `terraform destroy` leaves nothing billable.
- AC: setup (writing the Patient, D11) runs before the ingest services are enabled: the Scheduler
  job and the push subscription are switched on only after it, so two services cannot both
  create the Patient at first contact. The connect script (T11) and the first seeding (T09)
  are steps run outside `terraform apply`, so this needs an "ingest enabled" variable that
  defaults to off, or a documented second apply.
- AC: `use_synthetic_data` and a real Google Health connection are exclusive: a `SYN-` patient
  ID is required with synthetic data and refused without it, and the setup guide says never
  to run the connect script against a synthetic patient ID.
- AC: wires the `openemr` and `phr` modules as their READMEs say ("For T25"): OpenEMR's boot
  hooks, extra secrets and 40-minute hook limit into the edge VM; the `google-beta` provider;
  one root variable each for the patient ID and assigning authority, with a check that
  ingestion and `phr` agree. The READMEs' first-boot and first-deploy checklists are worked
  through in order, and teardown notes are carried into the setup guide (the default Hosting
  site and its last page stay up after destroy; the serverless subnet cannot be deleted for
  one to two hours).
- AC: wires the edge VM as its README says: project-base's `edge_vm_service_account_email` is
  set (it grants the VM's account read access to the Docker Hub mirror and targets the firewall
  rule), the mirror stays enabled, and `mllp_port` and `name_prefix` match project-base. The
  first real boot works through the ordered checklist in the edge-vm README ("Not verified"),
  and the README's "Patching the VM" routine is carried into the setup guide.
- AC: wires the ingestion module as its README's "For T25" section says (push endpoint and
  `ingest_enabled` into the healthcare module, the seeder account only with synthetic data,
  the same project for healthcare) and sets an alert email or channel, so a failed sync and
  "reconnect needed" reach someone.
- AC: outputs give everything the connect script and the seeder need (FHIR location, dataset
  and store IDs, the secret name prefix), and one `patient_id` variable feeds every service
  and both scripts.

**T26 status.** The offline part is merged: four workflows, three CI identities per project
(read-only planner with a custom role, deployer, Hosting-only page deployer) each tied to its own
Workload Identity context, a strict Environment pre-check, a value-aware plan digest, opt-in image
builds. Every deploying job is skipped until the repository variables are set. Nothing has run
on GitHub or GCP: the first-run checklist is in `infra/envs/README.md` (CI/CD). The next bootstrap
apply narrows the deployer's trust to `main` by default.

**T26 carried from T25.** Plans show `alert_email` (module `ingestion` uses it in a `for_each`,
so it cannot be sensitive): give `ingestion` a separate bool so the address can be sensitive, or
do not post it in pull-request plans. The patient ID and name are sensitive already.

**T26 Terraform CI/CD.** `fmt -check`, `validate`, `tflint`, `trivy config`, plan posted on pull
requests, apply to dev on merge, manual gate for prod, nightly destroy test for dev (see D1).
- AC: authenticates with Workload Identity Federation only; no key in GitHub secrets.
- AC: deploys the dashboard after every apply (`firebase.json` from module `phr`'s output, a
  pinned `firebase-tools`), having first confirmed the Firebase CLI accepts federated
  credentials; the nightly destroy test allows for the subnet deletion delay.
- AC: owns what T24 left open for image builds: a dedicated build service account (in the
  `identity` module: writer on the services repository only, reader on the Docker Hub mirror
  only, log writer; no access to secrets, health data, Cloud Run or IAM), a Cloud Build
  trigger on release tags that runs `cloudbuild.yaml` as that account, and an Artifact
  Registry cleanup policy in `project-base` (tags are immutable, so images otherwise
  accumulate). The first real build confirms the assumptions listed as unverified in
  `services/README.md`.

**T27 Secret version scripts.** Add secret versions after apply.
- AC: values are read from a prompt or stdin and never echoed or written to disk, and are
  stored without a trailing newline (the edge VM reads them into password files).
- AC: the three Google Health secrets are written by the connect script (T11), not here. If a
  script uses the Python Secret Manager client it switches off Google SDK payload logging
  first (reuse `quiet_sdk_logging` from T11), and no script uses `gcloud --log-http`.
- AC: includes `set-reviewer-password`, which enforces a 16-character minimum and can generate
  a random password.

**T28 Setup guide and README.** New-user guide: prerequisites, create or adopt a project, billing,
bootstrap, register a Google Health OAuth app, connect an account, deploy, destroy. README: pitch,
GIF, architecture, standards map, deploy, what this is not.
- AC: no step relies on the author's accounts; every command uses a placeholder or a variable.
- AC: carries over the connect script's README: a Desktop-app OAuth client, adding yourself
  as a test user, the roles the deployer needs, keeping demographics out of shell history,
  what to do when the refresh token expires, and that secrets must be named as the identity
  module names them.
- AC: the README shows a screenshot or GIF of a shared reading in the real OpenEMR chart,
  taken with synthetic data, since reviewers cannot open OpenEMR themselves (D13).
- AC: the guide explains the OAuth app's Testing status (test users only, refresh token expires
  after 7 days) and how to move it to "In production".

**T29 End-to-end smoke test.** Simulated cuff reading through MLLP to the FHIR store, then shared
to OpenEMR, asserted from the APIs.
- AC: runs against a deployed dev stack with one command.
- AC: runs the first-sync checks in the `health-sync` README ("For T25"): the Patient entry
  answers 200, not 201 (the store resolved the existing Patient; if not, switch to the
  fixed-ID fallback in the T06 README and record the result); the second run reports
  `stored_read: ok` and `windows_unchanged > 0` (so Cloud Healthcare supports the searches
  the service relies on); `/status` answers quickly.

T29 outcome so far: `make smoke` (tests/e2e/) reviewed (pass with notes; fixes made) and merged.
Run live against dev (synthetic data) on 17 November, twice: 33 checks passed, none failed, the
four health-sync checks skipped and reported as skipped; `/status` answered in under a second.
Each run adds one synthetic reading to the FHIR store and OpenEMR and two rejected messages to
the log; nothing can be removed through the API. Still open: the health-sync first-sync checks
(Patient 200 not 201; `stored_read: ok`, `windows_unchanged > 0`) are built and unit-tested but
have never run live, because no stack with real Google Health data exists. Found on the way, not
assigned: `services/health-sync` counts stranded preliminary windows with `_summary=count`, which
Cloud Healthcare refuses, so on real data the count stays null and `/status` never reports
`backfill-needed`; its fake accepts `_summary` and should refuse it.

**T30 Public demo domain.** Map the dashboard to a subdomain; the domain is a variable.
**Deferred on 17 November 2026 by the user's decision:** the demo is presented at the Firebase
Hosting address of the prod project (`https://<project-id>.web.app`); no custom domain for now.
`public_origin` stays as an optional variable. If taken up later:
- AC: TLS works; nothing about the author's domain is hardcoded outside `tfvars`.
