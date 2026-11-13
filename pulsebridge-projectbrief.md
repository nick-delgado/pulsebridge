# PulseBridge — Project Brief

Nov 11, 2026 · @Nick

## Summary

PulseBridge is a personal health record (PHR) on GCP that ingests heart-rate data from a Pixel Watch and from a simulated blood pressure cuff, stores it as FHIR R4, charts it, and shares it with a clinic by writing to OpenEMR's FHIR API. Everything is provisioned with Terraform, costs near zero when idle, and is built to be read by a hiring manager in thirty seconds and by an engineer in an afternoon.

The demo surface is a single-page PHR dashboard: a heart-rate chart, a trace panel that shows the raw HL7v2 message, the FHIR Observation and the EMR's response, and one Share with clinic button. The under-the-hood work is a JSON-to-FHIR mapper, an HL7v2 ORU ingestion path through the Cloud Healthcare API, and an authenticated FHIR client that pushes vitals into a certified EHR.

## Portfolio goals

A reviewer should leave with one sentence: he can take consumer and device health data, make it standards-compliant, and land it in an enterprise EMR, with the infrastructure as code.

| Posting keyword | Where it shows up in this project |
| --- | --- |
| Terraform / IaC | Every resource, including the VM, provisioned from modules with remote state and plan-on-PR CI |
| FHIR R4 | Observation, Patient and Device resources written to a Cloud Healthcare FHIR store; conditional creates; validator pass |
| HL7v2 | ORU^R01 messages from the device simulator, ingested over MLLP and mapped to FHIR with your own parser |
| SMART on FHIR / OAuth2 | Registered OAuth2 client against OpenEMR; scoped token flow visible in the trace panel |
| EHR integration | Vitals written into OpenEMR (ONC-certified) and visible in the patient chart |
| Serverless / cost | Cloud Run scale-to-zero, Scheduler, pay-per-use Healthcare API; idle cost near $0 apart from the VM |
| Security / PHI | Private networking, Secret Manager, least-privilege service accounts, documented data classification (real PHI: the author's own) |

Secondary goal: the repo is cloneable. Someone who finds it on GitHub can run one quick-start path and have the stack up in under an hour.

## Data flows

&#91;embedded content: architecture · 4 data flows, 1 FHIR store\]

The top row is the wearable path, the middle row the device path; both end in the same FHIR store that the dashboard reads and the share button forwards.

1. **Ingestion, wearable.** Cloud Scheduler fires `health-sync` every 15 minutes. It pulls new heart-rate samples from the Google Health API (`dataPoints` list with a time filter and page token), maps each to a FHIR `Observation` (LOINC 8867-4, UCUM `/min`, category vital-signs, `Device` reference for the watch), and writes them to the FHIR store as a transaction `Bundle` using conditional updates keyed on the Google data-point `name` so re-delivery is idempotent.
2. **Ingestion, device.** The cuff simulator's backend emits an HL7v2 `ORU^R01` over MLLP to the adapter on the VM. The adapter ingests into the HL7v2 store and returns the ACK to the simulator. Ingest publishes to Pub/Sub; `v2-to-fhir` parses PID/OBR/OBX from the stored message (own parser, not a managed mapping) and writes the same `Observation` shape to the FHIR store with provenance pointing at the v2 message.
3. **Presentation.** The PHR dashboard is a static single-page app. It authenticates the viewer, then calls `Observation?patient=&code=8867-4&date=` on the FHIR store through a thin Cloud Run proxy that holds the service credentials. Selecting a point opens the trace panel: the FHIR JSON and, for device-sourced points, the original v2 message and its ACK.
4. **Share with clinic.** One button sends the `Patient` (conditional create on an identifier) and the selected `Observation`s to OpenEMR's FHIR API using a registered OAuth2 client. The trace panel shows the token exchange and the `201 Created` responses. The reading then appears in the patient's chart in OpenEMR.

## Components

Fourteen pieces, all on GCP, all created by Terraform except the Google Health OAuth app, which is registered by hand in the Google Cloud console.

| Component | GCP service | Role | Idle cost |
| --- | --- | --- | --- |
| Google Health API client | OAuth consent screen + client (manual, one per deployment) | Authorizes `health-sync` to read the deployer's own heart-rate samples; the deployer connects their account through an OAuth flow, so no client ID, secret or token is hardcoded | $0 |
| health-sync | Cloud Run service (Python) | Pulls Google Health JSON, maps to FHIR, writes Bundle | $0 (scale to zero) |
| Sync trigger | Cloud Scheduler | Invokes `health-sync` every 15 min with an OIDC token | \~$0 |
| FHIR R4 store | Cloud Healthcare API dataset + FHIR store | System of record for Patient, Device, Observation, Provenance | cents (storage + ops) |
| HL7v2 store | Cloud Healthcare API HL7v2 store | Receives ORU^R01 from the MLLP adapter, parses, notifies | cents |
| Ingest topic | Pub/Sub topic + push subscription | Fans HL7v2 ingest events out to `v2-to-fhir` | \~$0 |
| v2-to-fhir | Cloud Run service (Python, hl7apy) | Reads the stored v2 message, parses OBX, writes Observation | $0 |
| MLLP adapter | Compute Engine e2-micro (new, clean), container from Google's open-source mllp-adapter | Terminates MLLP (TCP) from the simulator and forwards to the HL7v2 store; also hosts OpenEMR and MariaDB | free tier |
| Cuff simulator | Cloud Run service | Backend for the left side of the page: builds an ORU^R01 (two OBX) from entered BP values, sends it over MLLP to the VM, returns the ACK | $0 |
| PHR dashboard | Cloud Storage bucket + Cloud CDN (or Firebase Hosting) | Static SPA: chart, trace panel, share button | cents |
| FHIR proxy | Cloud Run service | Holds service-account credentials; exposes a narrow read API to the SPA; executes the share action against OpenEMR | $0 |
| OpenEMR | Docker on the e2-micro (official OpenEMR image + MariaDB) | The clinic. Receives shared vitals via its FHIR API; reviewer account for chart view | rides the VM |
| Secrets | Secret Manager | Google Health refresh token, OpenEMR client secret, DB password | \~$0 |
| Identity | Service accounts, IAM bindings, Identity Platform (reviewer login) | Least-privilege per service; password-protected demo | $0 |

The e2-micro is the only always-on piece and the only one that costs money if it leaves the free tier. It carries three jobs, OpenEMR, MariaDB and MLLP termination, because each needs a long-lived process or persistent local state and none fits Cloud Run cleanly. Add a 2 GB swap file and a low-memory MariaDB config; if page loads are unacceptable, move to e2-small, which leaves the free tier.

## Product requirements (MVP)

The MVP is done when a reviewer can open the dashboard, see real heart-rate data, send a simulated device reading through HL7v2, and share a reading to OpenEMR, all without instructions.

**Single page**

One page, no navigation. A line-art silhouette of the patient sits in the centre with a cuff marked on the upper arm and a watch on the wrist; arrows leave the body outward to each side. Left is episodic (blood pressure cuff simulator), right is continuous (Pixel Watch heart rate). Trace panels sit under their side; one Share with clinic button sits under the silhouette where both paths converge.

- [ ] Reviewer login (single shared credential handed to recruiters; no self-signup)
- [ ] Silhouette in muted line art only; no fills, no organs; small enough that the panels and chart dominate
- [ ] Status strip in the header: last sync, Observation count per source, FHIR store and OpenEMR reachability

**Left: blood pressure cuff simulator**

- [ ] Systolic and diastolic inputs with sensible bounds; no free text reaches the message
- [ ] Transport toggle: MLLP through the adapter (default) or HTTPS direct to the HL7v2 store's ingest endpoint
- [ ] Take reading sends an ORU^R01 with two OBX segments (LOINC 8480-6, 8462-4); rate-limited
- [ ] Readings list below the form: time, value, ACK code; selecting a row fills the left trace panel
- [ ] Each reading lands in the FHIR store as one panel Observation (LOINC 85354-9) with two components

**Right: heart-rate chart**

- [ ] Line chart of watch heart rate, last 24 h by default, selectable 7 d / 30 d
- [ ] Min, mean and max for the window under the chart
- [ ] Selecting a point fills the right trace panel and shows time, value and source below the chart

**Trace panels, share action, messages log**

- [ ] Left panel tabs: HL7v2 as sent, ACK as received, FHIR as stored
- [ ] Right panel tabs: FHIR as stored, Google Health JSON it came from
- [ ] Share with clinic acts on whatever is selected on either side; its own trace (token request, Bundle sent, 201 Created responses, link to the OpenEMR chart) appears under the button
- [ ] Messages log as a collapsible strip at the bottom: one row per HL7v2 message (type, ACK, store id, resulting Observation) and per FHIR transaction (Bundle, HTTP status, resource ids)

**Ingestion**

- [ ] `health-sync` is idempotent (conditional update on Google data-point id); backfill command for the last N days
- [ ] `v2-to-fhir` handles a malformed message by writing an error record and acking AE, never crashing the subscription
- [ ] Every Observation carries `device` and a `Provenance` naming the source system

**Standards conformance**

- [ ] All written resources pass the HL7 FHIR validator in CI (R4, base profiles; US Core Vital Signs as a stretch)
- [ ] The proxy publishes a `CapabilityStatement` listing only what it supports
- [ ] OpenEMR writes use a registered OAuth2 client with scoped tokens, no admin API keys

**Operations**

- [ ] `terraform apply` from a clean project to a working stack in one quick-start path
- [ ] `terraform destroy` leaves nothing billable behind
- [ ] README: pitch, GIF, architecture, standards map, deploy, what this is not
- [ ] Setup guide for a new user (`docs/setup.md`): prerequisites, creating or adopting a GCP project, linking billing, running bootstrap, registering their own Google Health OAuth app, connecting their own Google Health account, deploy, destroy. No step depends on the author's accounts or credentials

## Terraform structure

One root module per environment composes seven reusable modules; modules own resources by lifecycle and blast radius, not by GCP product.

```markdown
infra/
  modules/
    project-base/      # APIs enabled, VPC, subnet, Cloud NAT (off by default), firewall, Artifact Registry
    identity/          # service accounts, IAM bindings, Identity Platform tenant, Secret Manager secrets (values set out of band)
    healthcare/        # Healthcare dataset, FHIR R4 store, HL7v2 store, Pub/Sub topic + subscription, store notification config
    ingestion/         # Cloud Run: health-sync, v2-to-fhir; Cloud Scheduler job; OIDC invoker bindings
    edge-vm/           # e2-micro, startup script, mllp-adapter + MariaDB containers, persistent disk, firewall rule for MLLP from Cloud Run NAT IP only
    phr/               # bucket + CDN (or Firebase Hosting), fhir-proxy Cloud Run, cuff simulator Cloud Run
    openemr/           # OpenEMR container on the edge VM, DB connection, registered OAuth2 client (via provisioning script)
  envs/
    dev/               # main.tf wires the modules; terraform.tfvars; backend.tf (GCS state bucket, prefix dev)
    prod/
  bootstrap/           # one-time: create a project and link billing, or adopt an existing project; state bucket; deployer service account; Workload Identity Federation for CI
```

Conventions that make it read as mature work:

- Remote state in a GCS bucket with versioning; one prefix per environment; state locking is native to the GCS backend.
- Every module has `variables.tf` with descriptions and validation blocks, `outputs.tf`, and a `README.md` (terraform-docs generated).
- Provider and module versions pinned; `.terraform.lock.hcl` committed.
- CI on pull request: `terraform fmt -check`, `validate`, `tflint`, `checkov` or `trivy config`, then `plan` for each env posted as a PR comment. `apply` on merge to `main` for dev; manual approval gate for prod.
- Secrets never in state as plain values where avoidable: Secret Manager secrets are created by Terraform, versions are added by a script after apply.
- Container images built by Cloud Build on tag and referenced by digest in `tfvars`, so an apply is reproducible.
- `*.tfvars` files are git-ignored; each env commits a `terraform.tfvars.example` with placeholders.
- GitHub Actions authenticates to GCP with Workload Identity Federation; no service account keys are stored in GitHub.
- Bootstrap is run once by a human with their own Google credentials. It either creates a new project (the user needs a billing account, and an organization is not required) or adopts an existing project ID.
- `terraform destroy` tested in CI nightly against dev to prove the stack tears down clean.

The modules are provider-specific by nature (see Decisions log); the portability claim is at the module interface, not the resources.

## Environments

Keep two environment roots, `dev` and `prod`, but make `dev` the quick-start: a cloner runs one env and never has to know the other exists.

The two goals don't conflict because they live at different layers. The enterprise signal is in the *structure*: modules shared across roots, per-env `tfvars`, separate state prefixes, a promotion path (plan on PR, apply to dev on merge, gated apply to prod). The quick-start signal is in the *path*: `cd infra/envs/dev && terraform init && terraform apply`, with a `Makefile` or `task` wrapper that does the bootstrap first.

|  | dev | prod |
| --- | --- | --- |
| Who uses it | anyone who clones the repo; nightly CI | the public demo at a `tinkeringnick.com` subdomain |
| OpenEMR | included | included |
| Reviewer login | default credential from `tfvars` | credential in Secret Manager, rotated |
| e2-micro | created | created |
| Google Health | optional: a `use_synthetic_data` flag seeds the FHIR store from Synthea-style samples so a cloner never needs a Pixel Watch or OAuth setup | real token |
| Deletion | `terraform destroy` nightly in CI | manual |

The `use_synthetic_data` flag is what makes the repo useful to a stranger: without it, every cloner would need their own Google Health app registration before seeing anything. In dev the cuff simulator still works end to end, so the HL7v2 path is demonstrable with zero external accounts.

What to avoid: Terraform workspaces for environments (they share one backend and are easy to apply to the wrong one), and a third env for the sake of it.

## Decisions log

| Decision | Choice | Why | Alternatives considered |
| --- | --- | --- | --- |
| Cloud | GCP only | One cloud done thoroughly beats two done thinly; the Cloud Healthcare API gives managed FHIR and HL7v2 stores at pay-per-use, which AWS HealthLake does not (hourly billing, \~$194/mo idle) | Dual AWS+GCP module contract |
| Terraform portability | Module interfaces, not resources | Terraform resources are provider-specific; portability lives in the variable/output contract of each module | Placeholder-templating script (rejected: reinvents modules) |
| Last-mile EMR | OpenEMR | ONC-certified, PHP/MariaDB (own stack), FHIR R4 API with OAuth2; forces real auth, conformance and patient matching | hapi.fhir.org (no auth, public, PHI risk); Epic sandbox (stretch) |
| FHIR store | Cloud Healthcare API FHIR store | Managed, R4, pay-per-op, first-class Terraform resource; leaves compute free for the app | HAPI (2 GB RAM), Medplum (4 GB), WSO2 FHIR Server (Go; viable fallback) |
| HL7v2 path | Simulator → MLLP → Google mllp-adapter → HL7v2 store → Pub/Sub → own parser | Puts v2 where it lives in the real world; own parser is the showcase; managed store gives ACK and notifications for free | Client-side v2 formatting (rejected: never parsed or acked); Dataflow + Whistle (rejected: always-on cost, hides the mapping) |
| Wearable source | Google Health API (cloud) | Legacy Fitbit Web API turned off Nov 2, 2026; Health API has \~5 s heart-rate samples by default and webhook subscriptions | Health Connect (on-device, needs an Android app) |
| Compute for the VM | Fresh e2-micro via Terraform, hosting OpenEMR, MariaDB and the MLLP adapter | Clean lifecycle, reproducible, free tier; the existing instance stays untouched | Reuse existing e2-micro (rejected: mixed workloads) |
| Environments | dev + prod roots, dev is the quick-start | Shows enterprise structure without making a cloner learn it | Single env; workspaces |
| Wearable account | Per deployment: each deployer registers their own OAuth app and connects their own Google Health account | Keeps the PHR single-patient and the repo free of the author's credentials | Hosted multi-user connect (rejected: strangers' PHI, scope verification) |
| CI authentication | Workload Identity Federation | No long-lived keys in GitHub | Service account key secret |
| Product name | PulseBridge | PulseBridge; repo slug pulsebridge; demo at pulsebridge.tinkeringnick.com |  |

## Open questions and spikes

Run these before building; each is a half-day and any one of them can change the design.

- [ ] **OpenEMR FHIR write support.** Confirm that `POST /apis/default/fhir/Observation` with category vital-signs is accepted in the current release, and whether it lands in the vitals form or elsewhere. Fallback: the standard REST API's vitals endpoint. This decides what the Share button actually calls.
- [ ] **OpenEMR on the e2-micro.** Prove OpenEMR, MariaDB and the mllp-adapter run together in 1 GB plus swap; measure page load and the FHIR API's response time. Fallback: e2-small.
- [ ] **Google Health API registration.** Register the OAuth app, obtain a token, pull one day of `heart-rate` data points; confirm the exact field names and whether webhook payloads carry samples or only notifications.
- [ ] **mllp-adapter on e2-micro.** Confirm Google's open-source adapter image runs within 1 GB alongside MariaDB; otherwise write a 100-line Python MLLP listener that calls `messages.ingest`.
- [ ] **HL7v2 store notification.** Confirm `ingest` (not `import`) publishes to Pub/Sub, and the message body format the push subscription delivers.
- [ ] **Reviewer auth.** Identity Platform vs a static password in the fhir-proxy; pick the simpler one that still looks deliberate.
- [ ] **Name.** Decided: PulseBridge, slug pulsebridge, demo at pulsebridge.tinkeringnick.com. Check slug availability on GitHub.

## Out of scope

- Any clinical workflow in OpenEMR beyond receiving vitals (no orders, meds, scheduling, billing)
- Multi-user PHR; the record is the author's, plus synthetic patients in dev
- Full FHIR search; the proxy supports only the parameters the dashboard uses, declared in its CapabilityStatement
- SMART Health Links / QR sharing (the CMS Kill the Clipboard flow); noted as a natural next step in the README
- Epic sandbox target for the share action; a stretch after MVP
- AWS deployment; the module contract is documented so a port is a known quantity, not a mystery
- HIPAA compliance claims; the README states the data classification and the controls applied, nothing more
