# PulseBridge

**Consumer and device health data, made standards-compliant, landed in a clinic's EMR, with all
the infrastructure as code.**

PulseBridge is a single-patient personal health record (PHR) on Google Cloud. It reads heart rate
from a Pixel Watch through the Google Health API and blood pressure from a simulated cuff that
speaks **HL7v2** over **MLLP**, stores both as **FHIR R4** in a Cloud Healthcare API FHIR store,
charts them on a one-page dashboard, and shares a selected reading with a clinic by writing it
to **OpenEMR**'s FHIR API with an OAuth2 client. Every cloud resource is created by **Terraform**,
and the stack costs close to nothing when idle.

![The PulseBridge dashboard in mock mode, with synthetic data](docs/images/dashboard-mock.png)

*The dashboard in mock mode (synthetic data, no server). See [Status](#status) for what has and
has not run against real services.*

## What to read first

If you have ten minutes, read these. They are the parts written by hand on purpose, rather than
delegated to a managed mapping service:

1. **The HL7v2-to-FHIR mapping**: [`services/v2-to-fhir/v2_to_fhir/mapping.py`](services/v2-to-fhir/v2_to_fhir/mapping.py),
   with its field-by-field table in [`services/v2-to-fhir/README.md`](services/v2-to-fhir/README.md#the-mapping).
   An ORU^R01 (MSH, PID, OBR, two OBX) becomes a blood-pressure panel Observation, with every
   refusal named.
2. **The Google-Health-to-FHIR mapping**: [`libs/pulsebridge_fhir/pulsebridge_fhir/google_health.py`](libs/pulsebridge_fhir/pulsebridge_fhir/google_health.py)
   and the resource builders beside it ([`resources.py`](libs/pulsebridge_fhir/pulsebridge_fhir/resources.py),
   [`bundle.py`](libs/pulsebridge_fhir/pulsebridge_fhir/bundle.py)). Five-minute roll-ups become
   heart-rate Observations written idempotently; the design notes are in
   [`libs/pulsebridge_fhir/README.md`](libs/pulsebridge_fhir/README.md#design-decisions).
3. **The Terraform modules**: [`infra/modules/`](infra/modules/), one README each, composed once in
   [`infra/envs/_stack`](infra/envs/_stack/) and deployed by the runbook in
   [`infra/envs/README.md`](infra/envs/README.md).

## Architecture

```mermaid
flowchart LR
  subgraph wearable["Wearable path"]
    watch["Pixel Watch"] --> gh["Google Health API"]
    sched["Cloud Scheduler"] -->|"every 15 min, OIDC"| hs["health-sync<br/>Cloud Run"]
    gh -->|"five-minute roll-ups"| hs
    goauth["Google OAuth<br/>token endpoint"] -->|"access token from<br/>the refresh token"| hs
    sched -.->|"hourly, synthetic data only"| seed["seed job<br/>Cloud Run"]
  end

  subgraph device["Device path"]
    cuff["cuff-sim<br/>Cloud Run, internal"] -->|"ORU^R01 over MLLP :2575"| mllp["MLLP adapter<br/>e2-micro VM"]
    cuff -.->|"HTTPS messages:ingest"| hl7["HL7v2 store"]
    mllp -->|"ingest, ACK back"| hl7
    hl7 -->|"notification"| ps["Pub/Sub topic"]
    ps -->|"push, OIDC"| v2f["v2-to-fhir<br/>Cloud Run"]
    v2f -->|"GET raw message,<br/>PATCH outcome labels"| hl7
    cuff -->|"AuditEvent per send"| audit["Audit FHIR store"]
  end

  subgraph record["System of record"]
    fhir[("FHIR R4 store")]
  end
  hs -->|"transaction Bundle"| fhir
  v2f -->|"transaction Bundle"| fhir
  seed -.->|"synthetic windows"| fhir

  subgraph front["Presentation and sharing"]
    browser["Browser"] --> hosting["Firebase Hosting<br/>static page"]
    browser -.->|"reCAPTCHA script, token"| fd["Fraud Defense<br/>(reCAPTCHA)"]
    hosting -->|"rewrite /api/**,<br/>__session cookie"| proxy["fhir-proxy<br/>Cloud Run, public"]
    proxy -->|"assessment"| fd
    proxy -->|"daily count"| counter[("demo counter bucket")]
    proxy -->|"read"| fhir
    proxy -->|"read"| audit
    proxy -->|"read"| hl7
    proxy -->|"OIDC"| cuff
    proxy -->|"OIDC, GET /status"| hs
    proxy -->|"OAuth2 client credentials,<br/>FHIR over HTTPS :443"| emr["OpenEMR + MariaDB<br/>same e2-micro VM"]
  end
```

- **Wearable path.** Cloud Scheduler calls `health-sync` every 15 minutes. It asks the Google
  Health API for five-minute heart-rate roll-ups (minimum, average, maximum), maps each window
  to a FHIR `Observation` with a `Provenance`, and writes a transaction `Bundle` of conditional
  updates, so a repeated window rewrites the same Observation. It gets each access token from
  Google's OAuth token endpoint with the stored refresh token. In a synthetic deployment (the
  dev default) there is no `health-sync`: Scheduler runs the seeder job (`services/seed`) hourly
  instead, which writes invented windows (dashed in the diagram).
- **Device path.** The cuff simulator builds an HL7v2 ORU^R01 with `hl7apy`, sends it over MLLP
  to Google's MLLP adapter on the VM (or straight to the store over HTTPS), and returns the store's
  ACK. The HL7v2 store announces each stored message on Pub/Sub; `v2-to-fhir` fetches the raw
  message, maps it, writes the Observation, and labels the message with its outcome. Every send
  attempt, including rejected and unacknowledged ones, is an `AuditEvent` in a separate audit
  store that the simulator can write but that holds no health data.
- **Presentation.** The page and `fhir-proxy` share one origin through a Hosting rewrite.
  `fhir-proxy` is the only public service. It runs the public demo's bot check (a reCAPTCHA
  assessment, counted against a daily cap in a small bucket) and then sets a signed 24-hour
  `__session` cookie; it exposes a narrow read API with its own CapabilityStatement, showing only
  the last 7 days; it forwards cuff readings, asks `health-sync` for its status (real data) for
  the status strip, runs the share, and rate-limits all of it
  ([The public demo](#the-public-demo)).
- **Sharing.** The share searches for or creates the Patient and an Encounter in OpenEMR, creates
  the Observation, and reads all three back for the "As stored in OpenEMR" panel. OpenEMR's
  screens are not exposed to the internet; only Cloud Run's subnet reaches the VM, which has no
  public IP.

The Terraform that builds this: `project-base` (APIs, VPC, firewall, Artifact Registry),
`identity` (service accounts, secrets), `healthcare` (dataset, FHIR and HL7v2 stores, Pub/Sub),
`ingestion` (health-sync or the seeder, v2-to-fhir, Scheduler, alerts), `edge-vm` (the e2-micro
and the MLLP adapter), `openemr` (OpenEMR on that VM and its OAuth client registration) and `phr`
(Hosting, fhir-proxy, cuff-sim).

## The public demo

Anyone with the address can open the dashboard: no password, nothing to sign up for (decision
D16). **View the live demo** runs an invisible **reCAPTCHA** score check (Google Cloud Fraud
Defense: the page gets a one-time token, the proxy asks Google to assess it and gets a score
from 0.0, likely a bot, to 1.0, likely a person). On a pass the proxy sets a signed 24-hour
session cookie, `__session` (`HttpOnly`, `Secure`, `SameSite=Strict`, plus an `Origin` check on
every write, against cross-site request forgery).

- **Limits.** Per session: 120 requests a minute other than cuff sends and shares (the reads), 3 cuff sends and 2 shares in any 10 minutes;
  30 shares an hour for everyone; 300 requests a minute per address without a session; 5
  bot-check attempts an hour per address. Each is kept in memory **per instance**, so with the
  proxy's two instances it is up to twice that. At most 300 bot checks a UTC day, counted in a
  Cloud Storage bucket so the cap holds across instances and restarts; then the page says "The
  demo is busy, try again tomorrow."
- **What is shown.** Only the last 7 days; older data is hidden by the proxy, never deleted. On
  a real-data deployment that window is the deployer's **own heart-rate data, public** to anyone
  with the address: the author accepted that for their demo.
- **Cost.** Fraud Defense gives 10,000 assessments a month free per organisation, then charges an
  $8 flat fee for 10,001 to 100,000 in a month
  ([billing information](https://docs.cloud.google.com/recaptcha/docs/billing-information)); the
  daily cap of 300 keeps one deployment under the free 10,000.
- **Privacy.** The page links a short privacy note ([`web/privacy.html`](web/privacy.html)): the
  two cookies, what goes to Google (the site owner is the data controller, Google the
  processor), what is logged and for how long (30 days).
- **Known gap.** The proxy's own `run.app` address is public too, and a caller of it can choose
  which client address it is counted as; the fix belongs at the edge and is a recorded
  follow-up ([runbook](infra/envs/README.md#the-public-demo-what-is-public-and-its-known-limits)).

## Standards map

| Standard | What PulseBridge uses | Where in the code |
| --- | --- | --- |
| FHIR R4 resources | `Patient`, `Device`, `Observation`, `Provenance`, transaction `Bundle`; `AuditEvent` for cuff send attempts; `Encounter` in OpenEMR; `OperationOutcome` errors; a `CapabilityStatement` for the proxy | [`libs/pulsebridge_fhir/`](libs/pulsebridge_fhir/README.md), [`services/fhir-proxy/fhir_proxy/capability.py`](services/fhir-proxy/fhir_proxy/capability.py) |
| FHIR profiles | R4 Vital Signs: `heartrate` and `bp`, category `vital-signs`; deviations documented | [`libs/pulsebridge_fhir/README.md`](libs/pulsebridge_fhir/README.md#conformance-to-the-r4-vital-signs-profiles) |
| FHIR writes | conditional update (`PUT Observation?identifier=...`) for idempotency; conditional create (`ifNoneExist`) for Patient and Device from ingest; one setup write of the full Patient | [`libs/pulsebridge_fhir/pulsebridge_fhir/bundle.py`](libs/pulsebridge_fhir/pulsebridge_fhir/bundle.py) |
| LOINC | `8867-4` heart rate (min `96935-2`, max `55426-1` as components); `85354-9` BP panel; `8480-6` systolic; `8462-4` diastolic | [`libs/pulsebridge_fhir/pulsebridge_fhir/codes.py`](libs/pulsebridge_fhir/pulsebridge_fhir/codes.py) |
| UCUM | `/min`, `mm[Hg]` | the same, and OBX-6 in the HL7v2 builder |
| FHIR validation | the official HL7 FHIR Validator over example resources, in CI | [`scripts/fhir-validate.sh`](scripts/fhir-validate.sh), `make fhir-validate` |
| HL7v2 2.5.1 message | ORU^R01 with MSH, PID (PID-3 identifier and authority), OBR (OBR-4 `85354-9`), two OBX (OBX-3 LOINC, OBX-5 value, OBX-6 unit, OBX-11 status, OBX-14 time with offset) | [`libs/pulsebridge_hl7v2/pulsebridge_hl7v2/oru.py`](libs/pulsebridge_hl7v2/pulsebridge_hl7v2/oru.py), [`libs/pulsebridge_hl7v2/README.md`](libs/pulsebridge_hl7v2/README.md#what-the-builder-writes-hl7-v251) |
| HL7v2 acknowledgment | the store's ACK in original mode: `AA` stored, `AE` rejected with MSA-3; "no acknowledgment" as its own outcome; labelled commit-level, with the mapper's result as the application-level outcome (modelled on enhanced mode, MSH-15/16) | [`libs/pulsebridge_hl7v2/pulsebridge_hl7v2/ack.py`](libs/pulsebridge_hl7v2/pulsebridge_hl7v2/ack.py), [`services/cuff-sim/README.md`](services/cuff-sim/README.md#mllp) |
| MLLP | start block `0x0B`, end `0x1C 0x0D`, one frame per connection; Google's mllp-adapter on the VM | [`libs/pulsebridge_hl7v2/pulsebridge_hl7v2/mllp.py`](libs/pulsebridge_hl7v2/pulsebridge_hl7v2/mllp.py), [`services/cuff-sim/cuff_sim/mllp_client.py`](services/cuff-sim/cuff_sim/mllp_client.py) |
| HL7v2 parsing | `hl7apy` builds, validates and parses; the raw ER7 is mapped, not the store's `parsedData` | [`services/v2-to-fhir/README.md`](services/v2-to-fhir/README.md#input-the-raw-er7-not-parseddata) |
| OAuth 2.0, Google | authorization code flow with PKCE (S256), `state`, loopback redirect, offline access; refresh tokens in Secret Manager | [`scripts/connect-google-health/`](scripts/connect-google-health/README.md) |
| OAuth 2.0, OpenEMR | client credentials grant with `private_key_jwt` (RS384 signed assertion), scoped `system/*` tokens; client registered on the VM | [`services/fhir-proxy/fhir_proxy/share/oauth.py`](services/fhir-proxy/fhir_proxy/share/oauth.py), [`infra/modules/openemr/README.md`](infra/modules/openemr/README.md) |
| OIDC service-to-service | Scheduler, Pub/Sub push and fhir-proxy call Cloud Run with Google-signed identity tokens | [`infra/modules/identity/README.md`](infra/modules/identity/README.md#who-invokes-whom-decision-d10) |
| Public demo access | a reCAPTCHA score assessment (`projects.assessments.create`); an HS256 JWT (RFC 7519) in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie; an `Origin` check on writes; `429` with `Retry-After` (RFC 9110) | [`services/fhir-proxy/README.md`](services/fhir-proxy/README.md#demo-sessions-d16-t43) |
| Workload Identity Federation | GitHub Actions to a read-only planner, the deployer and a page deployer that can change only Hosting, restricted to one repository by numeric ID and each to its own CI contexts (pull requests, main, a protected GitHub Environment); no keys | [`infra/bootstrap/wif.tf`](infra/bootstrap/wif.tf) |
| Terraform CI/CD | plan on pull requests, apply dev on merge, prod after an approval, the page deployed after every apply | [`.github/workflows/`](.github/workflows/), [CI/CD in the runbook](infra/envs/README.md#cicd-t26) |

## Repository layout

```text
infra/
  bootstrap/        one-time: project, state bucket, deployer, Workload Identity Federation, Firebase
  modules/          project-base, identity, healthcare, ingestion, edge-vm, openemr, phr
  envs/             _stack (the composition), dev and prod roots, and the runbook (README.md)
libs/
  pulsebridge_fhir/   FHIR R4 builders and the Google Health mapping (no network)
  pulsebridge_hl7v2/  HL7v2 ORU^R01 builder, ACK parser, MLLP framing (hl7apy)
  pulsebridge_gcp/    thin Cloud Healthcare API client
services/
  health-sync/      Google Health to FHIR (Cloud Run)
  v2-to-fhir/       HL7v2 store to FHIR (Cloud Run, Pub/Sub push)
  cuff-sim/         the cuff simulator backend (Cloud Run, internal only)
  fhir-proxy/       the dashboard's only backend: demo sessions, read API, share to OpenEMR
  seed/             the synthetic-data seeder (Cloud Run job)
scripts/
  connect-google-health/   connect your own Google Health account (OAuth, PKCE)
web/                the dashboard and its privacy note: Vite and TypeScript, with a mock client
docs/               the setup guide, spike notes, the work plan
tests/tooling/      repository-wide checks (Makefile, CI, images, these docs)
```

## Quick start

The full walk-through, from a fresh clone to a running stack and back to nothing, is
**[docs/setup.md](docs/setup.md)**. In short:

1. Run [`infra/bootstrap`](infra/bootstrap/README.md) once with your own Google login: it
   creates or adopts a project, a state bucket, a deployer service account and Workload Identity
   Federation.
2. Use `infra/envs/dev`, which defaults to **synthetic data** (a `SYN-` patient): no Pixel Watch
   and no Google Health OAuth app needed; the cuff simulator works end to end.
3. Follow the [runbook](infra/envs/README.md#the-runbook): targeted applies, secret values, the
   VM's first boot, the image build, the first apply with ingest off, the first seeding, the
   second apply with ingest on, the page deploy.
4. For your own watch data (prod, or dev with `use_synthetic_data = false`), register a Google
   Health OAuth app in your project and run the connect script on your own computer
   ([setup guide, Step 2 and 3.8](docs/setup.md#step-2-register-a-google-health-oauth-app-real-data-only)).

To see the dashboard right now with no cloud at all:

```bash
cd web
npm ci
npm run dev       # mock mode on http://localhost:5173 ("View the live demo"; no Google script)
```

## OpenEMR chart

Reviewers cannot open OpenEMR themselves (its screens are deliberately not exposed, decision
D13), so this is a shared reading in the real OpenEMR chart on the `dev` deployment. The patient
is synthetic (`SYN-0001`, Synthetic / Demo). The blood-pressure reading came from the simulated
cuff as an HL7v2 message and the pulse from the seeded heart-rate data; both were sent with
**Share with clinic**, which writes them to OpenEMR's FHIR API.

![OpenEMR's Vitals screen for the synthetic patient: BP systolic 128 and diastolic 82 mmHg in one column, pulse 91 per minute in another](docs/images/openemr-chart-synthetic.png)

How it was captured, **with synthetic data only**:

1. Deploy `dev` with `use_synthetic_data = true`, following [docs/setup.md](docs/setup.md).
2. In the dashboard, take a cuff reading, wait for the outcome "accepted", select it and
   **Share with clinic**; share one final heart-rate point too.
3. Set `enable_iap_ssh = true` and apply. Your account needs `roles/iap.tunnelResourceAccessor`,
   `roles/compute.osAdminLogin`, and `roles/iam.serviceAccountUser` on the VM's service account
   ([edge-vm README](infra/modules/edge-vm/README.md#looking-at-the-vm-without-public-access));
   a project Owner already has them. Then open a temporary tunnel through IAP to OpenEMR's HTTPS
   port (`openemr_https_port`, 443 by default):
   `gcloud compute ssh <name_prefix>-edge-vm --zone <zone> --tunnel-through-iap --project <project-id> -- -N -L 8443:localhost:<openemr_https_port>`
4. Open `https://localhost:8443`, accept the self-signed certificate and log in as `admin`. The
   password is the `openemr-admin-password` secret's value: open it in the Google Cloud console
   (Secret Manager, the secret, the version's "View secret value") rather than printing it in a
   terminal, where it would land in scrollback and logs. Open the synthetic patient and its
   **Vitals**.
5. Capture the vitals with the reading visible. Check the image shows only the synthetic
   patient, no address bar or host name, and no other data; then close the tunnel and set
   `enable_iap_ssh = false` again.

The dashboard's own "As stored in OpenEMR" panel, shown here against the page's mock OpenEMR,
is the PulseBridge side of the same exchange:

![The share trace in mock mode, against the page's mock OpenEMR, for a synthetic patient](docs/images/share-trace-mock.png)

## Testing, and how this was built

- `make lint` and `make test` (ruff and pytest over the libraries, services, scripts and tooling),
  `make tf-validate` and `make tf-test` (`terraform validate` and `terraform test` for every
  module and root against **mocked providers**, with Google credentials removed from the
  environment, plus static scans: no service account keys, no authoritative IAM, no secret value
  in state, no author-specific identifier), `make web-check` (the dashboard's type check, unit
  tests and Playwright browser tests against the mock build), `make fhir-validate` (the HL7 FHIR
  Validator, in its own CI workflow), `make secret-scan` and `make pre-commit`. `make check` runs
  the Python and Terraform gates together.
- `make smoke` is the **end-to-end smoke test** of a deployed stack
  ([`tests/e2e/`](tests/e2e/README.md)): through the public front door only, it sends a
  simulated cuff reading over MLLP, waits for the FHIR Observation and its Provenance, shares it
  to OpenEMR (twice: the second must be `already-shared`), checks that malformed readings create
  nothing, and on a real-data stack runs health-sync's first-sync checks from its logs. It is
  not part of `make check` (it needs a deployment); its own tests run in `make test` against an
  in-process stack.
- **Tests are break-checked.** For every behaviour under test, the implementation was
  deliberately broken (a flipped condition, a wrong code or unit, a removed field) to see the test
  fail for the right reason, then restored. A test that cannot fail does not count.
- **Every task was reviewed independently.** The work was planned as small tasks
  ([`docs/workplan.md`](docs/workplan.md)), each built on its own branch with its acceptance
  criteria, then reviewed by a separate reviewer against those criteria before merging.
- **Spikes first.** The risky assumptions (OpenEMR's FHIR write support, OpenEMR on an e2-micro,
  the Google Health API, the MLLP adapter, HL7v2 store notifications, reviewer login) were tried
  against real services before the design was fixed; the notes are in [`docs/spikes/`](docs/spikes/).
- **No real health data** appears in the repository, its tests, fixtures or images: everything
  uses synthetic values.

## What this is not

- **Not a medical device and not for clinical use.** It makes no diagnosis, raises no clinical
  alert, and nothing it shows should inform a medical decision.
- **Not HIPAA-compliant, and makes no such claim.** It applies sensible controls (private
  networking, least-privilege service accounts, Secret Manager, no public VM) and states its data
  classification; that is all.
- **Single patient.** One deployment holds one person's record, connected with the deployer's
  own Google Health account. There is no sign-in at all: every visitor of the public demo gets
  the same anonymous access after the bot check, and nothing asks who they are (Cloud Run's
  request log does keep each request's network address and URL for 30 days).
- **Screenshots use synthetic data.** The cuff is simulated; every image here shows the synthetic
  patient. The live demo on prod shows the deployer's real heart rate for the last 7 days.
- **Small by design.** The OpenEMR "clinic" runs on a free-tier e2-micro: it handles shares one
  at a time and slows down under sustained load, so the public demo's limits keep the load small
  (2 shares per session in 10 minutes, at most 30 an hour per proxy instance for everyone). Only the FHIR searches the dashboard needs are supported.
  OpenEMR receives vitals only (no orders, medications or scheduling).
- **Not a managed mapping pipeline.** The mappings are our own code on purpose; there is no
  Whistle, Dataflow or managed HL7v2-to-FHIR conversion.

## Status

**Built and verified offline:** every module, service, script and the dashboard, with unit,
integration and browser tests against fakes and mocks (`make check`, `make web-check`); the
Terraform roots `validate` and plan against mocked providers in every scenario (real or
synthetic data, ingest off or on, dev and prod). The spikes ran against real Google, OpenEMR and
MLLP services before the build.

**Deployed once, to dev, with synthetic data (16 November 2026).** A new dev project was
bootstrapped and taken through the runbook's steps 1 to 11. That confirmed, on real Google
services and a real OpenEMR 8.5.0: the edge VM's first boot (every step `ok`, about 20 minutes),
the image build, the targeted and full applies, the first seeding (8,365 synthetic heart-rate
windows), ingest on, the page on Firebase Hosting with the reviewer login and the chart (no
console errors), cuff readings over MLLP and over HTTPS (`AA`, mapped to final Observations), and
shares of a blood pressure and a heart rate to OpenEMR (`shared`, then `already-shared` on
repeat). The deploy found four code problems, each now fixed with a test (a gcloud warning the
secret script misread, alert filters without a resource type, a FHIR count Cloud Healthcare
refuses, the Pub/Sub service agent missing on a new project), and gaps in the runbook, now
documented ([findings](infra/envs/README.md#findings-for-the-modules)).

**Checked further on dev (17 and 18 November):** a reboot and patching the VM by replacement
(the data disk and address kept), the seeder's and Scheduler's alerts firing by email, password
rotation without a restart, the hop count behind Firebase Hosting (2), replay from the
dead-letter subscription, cold-start timings, and the end-to-end smoke test (`make smoke`,
[`tests/e2e/`](tests/e2e/README.md)). The checks found and fixed more problems, each with a test:
the stored-window read asked Cloud Healthcare for element names it ignores (every hourly run
rewrote everything), and the CI planner and page deployer each lacked a permission.

**CI/CD runs on dev** (18 November): pull requests are planned by a read-only planner that posts
the plan as a comment; a merge to `main` applies dev and deploys its page, through Workload
Identity Federation with no stored keys ([CI/CD](infra/envs/README.md#cicd-t26)).

**Deployed to prod with real data (18 November 2026):** the owner's existing project, adopted by
bootstrap, then the runbook with the owner's own Google Health account connected by the connect
script. health-sync's first sync wrote the heart-rate windows and matched the Patient the connect
script wrote; its second run found them all unchanged. `make smoke` passed in full against prod,
including the cuff path, the share to OpenEMR and the health-sync checks. Prod's first boot also
found a fault: on the small VM, MariaDB's first-time setup can take longer than its image allows,
which left the database half-made; the startup script now allows longer and recovers a half-made
directory ([edge-vm README](infra/modules/edge-vm/README.md#first-initialisation-of-mariadb)).

Prod's first apply through CI followed on 18 November: the plan waited for approval, then the
apply and the page deploy passed. On 19 November CI destroyed dev: nothing was left in its state,
and the default Hosting site was then taken offline. A day later the billing report showed no new
charges for the dev project.

**The public demo (D16, 19 to 20 November 2026)** replaced the reviewer password with the bot
check, the session cookie, the rate limits and the 7-day window (tasks T43 to T48). It is built
and tested offline only, against fakes of Fraud Defense and Cloud Storage and the page's mock
client, and its rollout to prod has not happened yet.

**Not yet verified:** everything about the public demo that needs a real deployment (the
assessment call, the scores real browsers get, the page's Content-Security-Policy with Google's
script, the cookie and the `Origin` header through Hosting, the counter with two instances, the
limits on a normal visit, the 7-day searches against Cloud Healthcare, `make smoke` with its
locally signed sessions: checklist rows P17 to P26), a forced slow cuff send, and a few inspections that need a shell on the
VM. The
runbook's [master checklist](infra/envs/README.md#master-checklist) records each item's result and
lists what is still open. The demo is shown at the Firebase Hosting address of the prod project;
a custom domain (T30) is deferred ([`docs/workplan.md`](docs/workplan.md)).
