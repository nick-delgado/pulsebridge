# Module `phr`

The dashboard's front door (task T22): the **Firebase Hosting site** that serves the page, the
**`fhir-proxy`** Cloud Run service behind it (the only public service, D10), and the
**`cuff-sim`** Cloud Run service that only the proxy may call.

```
browser ──HTTPS──> Firebase Hosting  https://<site>.web.app      (static page: web/dist; security headers, CSP)
                     │ rewrite /api/** (same origin, no CORS)      Hosting ends a forwarded request at 60 s
                     ▼
                   fhir-proxy  (Cloud Run, public, max 2 instances, concurrency 8, request timeout 120 s)
                     │ session token checked here (D9)             reviewer-password mounted as a FILE (latest)
                     │ Direct VPC egress, ALL_TRAFFIC, serverless subnet (Private Google Access, no NAT)
                     ├──> Cloud Healthcare API: FHIR store, audit FHIR store, HL7v2 store   (read only)
                     ├──OIDC──> cuff-sim  (internal only; invoker: fhir-proxy's account only)
                     │            │ Direct VPC egress, PRIVATE_RANGES_ONLY
                     │            ├──MLLP──> edge VM <mllp_host>:2575 ──> HL7v2 store
                     │            └──> Cloud Healthcare API: messages:ingest, AuditEvent PUT (audit store)
                     ├──OIDC──> health-sync /status  (module ingestion; internal only; real data only)
                     └──> edge VM OpenEMR HTTPS (T15, later)
```

## Terms used here

- **Firebase Hosting site**: a static-file host with a CDN, at `https://<site_id>.web.app` (and
  `<site_id>.firebaseapp.com`). Each deploy uploads a **version** (the files plus its
  configuration) and **releases** it, which makes it live.
- **`firebase.json`**: the file the Firebase CLI reads when it deploys. Its `hosting` section says
  which directory to upload (`public`), which files to leave out (`ignore`), which paths go
  elsewhere (`rewrites`) and which response headers to add (`headers`). The configuration
  travels *with each version*: it is not a separate setting of the site.
- **Rewrite to Cloud Run**: Hosting answers a matching path (here `/api/**`) by forwarding the
  request to a Cloud Run service and returning its answer, on the page's own address.
- **Origin** and **same origin**: an origin is scheme + host + port (`https://x.web.app`). A page
  calling `/api/...` calls its own origin. **CORS** (cross-origin resource sharing) is the set of
  `Access-Control-*` headers a server must send before a browser lets a page read an answer from
  a *different* origin; same-origin calls do not need them.
- **Content-Security-Policy (CSP)**: a response header that tells the browser what the page may
  load and connect to. A strict one blocks injected scripts even if an attacker gets text onto
  the page.
- **Ingress** and **invoker**: two separate locks on a Cloud Run service. Ingress is *which
  network sources* may reach it at all; the invoker IAM check is *which identities* may call it
  (`roles/run.invoker`). A request must pass both, unless the invoker check is disabled.
- **Direct VPC egress**: a Cloud Run instance gets addresses in a VPC subnet and sends its
  outbound traffic through the VPC: all of it (`ALL_TRAFFIC`) or only traffic to private
  addresses (`PRIVATE_RANGES_ONLY`). It is free; the older alternative, a Serverless VPC Access
  connector, runs VMs and is billed hourly.
- **Secret volume**: a Secret Manager secret mounted as a file. Cloud Run reads Secret Manager
  each time the file is read, so version `latest` means "the newest version, now".
- **Startup probe**: a check Cloud Run runs on a new instance before sending it traffic: a TCP
  connect to the port, or an HTTP GET that must answer 2xx or 3xx. A revision whose instances
  never pass it never takes traffic.
- **`X-Forwarded-For` (XFF)**: a request header each proxy appends the address it saw to. Only
  the right-hand entries, added by proxies you trust, are reliable; the rest is whatever the
  caller sent.
- **Image by digest**: `…/fhir-proxy@sha256:<64 hex>` names the image's exact bytes; a tag can be
  moved.

## Usage

How T25's env root wires it (output names are the real ones of the other modules):

```hcl
provider "google-beta" { # this module's Hosting resources exist only in the beta provider
  project = var.project_id
  region  = var.region
}

module "phr" {
  source     = "../../modules/phr"
  project_id = var.project_id
  region     = var.region # = module.project_base's region and the dataset's

  use_synthetic_data = var.use_synthetic_data

  # By digest, as the image build prints them (services/README.md, "Container images").
  fhir_proxy_image = var.fhir_proxy_image
  cuff_sim_image   = var.cuff_sim_image

  healthcare_project_id = module.healthcare.project_id
  healthcare_location   = module.healthcare.location
  healthcare_dataset_id = module.healthcare.dataset_name
  fhir_store_id         = module.healthcare.fhir_store_name
  audit_fhir_store_id   = module.healthcare.audit_fhir_store_name
  hl7v2_store_id        = module.healthcare.hl7v2_store_name

  patient_id                  = var.patient_id                  # SYN-... exactly when use_synthetic_data
  patient_assigning_authority = var.patient_assigning_authority # ALSO module.ingestion's
  patient_family_name         = var.patient_family_name         # null with synthetic data
  patient_given_name          = var.patient_given_name          # null with synthetic data

  service_account_emails = module.identity.service_account_emails
  secret_ids             = module.identity.secret_ids

  network_name           = module.project_base.network_name
  serverless_subnet_name = module.project_base.serverless_subnet_name
  mllp_host              = module.edge_vm.mllp_host
  mllp_port              = module.edge_vm.mllp_port

  health_sync_url = module.ingestion.health_sync_url # null with synthetic data

  # Optional: module openemr's outputs once T23 is merged and T15 needs them.
  # openemr = {
  #   site_address     = module.openemr.site_address
  #   token_url        = module.openemr.token_url
  #   fhir_base_url    = module.openemr.fhir_base_url
  #   scopes           = module.openemr.scopes
  #   client_secret_id = module.openemr.client_secret_id
  # }

  trusted_proxy_hops = var.trusted_proxy_hops # 1 until measured (see "Verify on the first real deploy")

  # Secret accessor grants (identity) before the proxy deploys: Cloud Run checks at deploy time
  # that the service account may read every mounted secret.
  depends_on = [module.identity]
}

module "ingestion" {
  # ...
  patient_assigning_authority = var.patient_assigning_authority # the same value as module.phr's
}
```

## What each resource is, and why

| Resource | What and why |
| --- | --- |
| `google_cloud_run_v2_service.fhir_proxy` | The dashboard's API (T14). Runs as `fhir-proxy`; public (invoker check off, ingress all; see "Public access and ingress"); `min 0`, **`max 2`**; concurrency **8** (= gunicorn's threads); request timeout **120 s** (= `REQUEST_TIMEOUT_SECONDS`); Direct VPC egress `ALL_TRAFFIC`; `reviewer-password` as a file at `latest`; TCP startup probe. |
| `google_cloud_run_v2_service.cuff_sim` | The cuff simulator (T13). Runs as `cuff-sim`; internal-only, invoker check on; `min 0`, `max cuff_sim_max_instances` (1); concurrency 4; request timeout **60 s**; Direct VPC egress `PRIVATE_RANGES_ONLY`; `HTTPS_TIMEOUT_SECONDS=6`; HTTP startup probe on `GET /cuff/samples`. |
| `google_cloud_run_v2_service_iam_member.invoker` | `roles/run.invoker` on cuff-sim for `fhir-proxy`'s account: the only invoker binding (`local.service_invokers`). |
| `google_firebase_hosting_site.dashboard` | The Hosting site (`hosting_site_id`, default the project ID: the project's default site). |
| `google_firebase_project.this` | Only with `manage_firebase_project = true`: adds Firebase to the project, which Hosting needs. |

**Not created here, on purpose:**

- **A Hosting version or release.** Terraform cannot upload the page (next section), so the
  page and its `firebase.json` are deployed by the Firebase CLI. A release made here would have
  no files: on the first apply it would publish an empty site, and on a later configuration
  change it would replace the live page with an empty one.
- **fhir-proxy's invoker right on health-sync.** Module ingestion owns health-sync and already
  grants it (`google_cloud_run_v2_service_iam_member.invoker["health-sync/fhir-proxy"]`).
- **Firewall rules.** project-base allows the serverless subnet to reach the VM on the MLLP port
  and OpenEMR's HTTPS port (D14); nothing here opens anything.
- **Secret values.** Only secret *names* and the word `latest` are here (module identity creates
  the secrets; T27's scripts add versions).

## Hosting: what Terraform manages, and what it cannot

**Which provider.** Firebase Hosting has Terraform resources only in the beta provider:
`terraform providers schema` for `hashicorp/google` 8.11.0 lists no `google_firebase_hosting_*`
resource; `hashicorp/google-beta` 8.11.0 has `google_firebase_hosting_site`, `_version`,
`_release`, `_channel` and `_custom_domain`, and `google_firebase_project`.

**Terraform cannot upload files.** The provider's own page for `google_firebase_hosting_version`
(google-beta v8.11.0, `website/docs/r/firebase_hosting_version.html.markdown`):

> A `Version` is a configuration which determine how a site is displayed. Static files are not
> supported at the moment.

So this module manages the **site** (and optionally Firebase on the project) and **renders**
`firebase.json` (output `firebase_json`) from its own values: the site ID, the rewrite's service
name and region, the security headers and the ignore list. Nothing about the site's behaviour is
written by hand in a second place. Uploading `web/dist` with that file is the deploy step's job
(below).

**The rewrite** (`firebase.json`, from [Hosting: Cloud Run](https://firebase.google.com/docs/hosting/cloud-run)):

```json
"rewrites": [{ "source": "/api/**",
               "run": { "serviceId": "<prefix>-fhir-proxy", "region": "<region>", "pinTag": false } }]
```

- `pinTag` is off. With it, each deploy would tag the current Cloud Run revision ("This feature
  relies on Cloud Run tags"); Terraform does not manage those tags and would remove them.
- **Region.** "Rewrites to Cloud Run from Hosting are supported in the following regions" (the
  page lists 39); `var.region` is validated against that list. Firebase recommends
  `us-central1`, `us-east1`, `us-west1`, `europe-west1` or `asia-east1` for speed.
- **Methods.** "supported HTTP request methods are GET, POST, HEAD, PUT, DELETE, PATCH, and
  OPTIONS" (Hosting configuration page): enough for every proxy route.
- **The 60 s limit.** "Firebase Hosting is subject to a 60-second request timeout. If your app
  requires more than 60 seconds to run, you'll receive an HTTPS status code 504 (request
  timeout)." See "Timeouts end to end".
- **Cookies are stripped.** "When using Firebase Hosting together with Cloud Functions or Cloud
  Run, cookies are generally stripped from incoming requests ... Only the specially-named
  `__session` cookie is permitted to pass through" ([Manage cache behavior](https://firebase.google.com/docs/hosting/manage-cache)).
  The proxy uses no cookie (D9: the token travels in `Authorization: Bearer`), so nothing is lost.
- **`Authorization`.** "Hosting adds Cookie and Authorization to the Vary header by default when
  a request is made for dynamic content" (same page): the header is part of the CDN cache key,
  which implies it is forwarded. No page says so outright: step 2 of the first-deploy checklist
  confirms it. The proxy also sends `Cache-Control: no-store`, so nothing is cached anyway.
- **`X-Forwarded-For`.** No Firebase page documents what Hosting puts in it. Step 1 of the
  checklist measures it.

**The headers** (`hosting/security-headers.json`, on every response, `source: "**"`):

| Header | Value | Why |
| --- | --- | --- |
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'` | Exactly the policy the page's build puts in its `<meta>` tag for a same-origin build (`web/vite.config.ts`, checked by `web/scripts/check-bundle.mjs`), plus `frame-ancestors 'none'`, which only a header can set (web/README.md, "Security basics"). No `unsafe-inline`, no `unsafe-eval`, no wildcard. `tests/check_hosting_csp.py` fails if the three files drift apart. |
| `X-Content-Type-Options` | `nosniff` | The browser must not guess a script out of a non-script file. |
| `X-Frame-Options` | `DENY` | `frame-ancestors` for older browsers. |
| `Referrer-Policy` | `no-referrer` | The page also sets it in a `<meta>`; the header covers every file. |
| `Cross-Origin-Opener-Policy` | `same-origin` | Another site that opens the page cannot keep a handle on its window. |

Plus `Cache-Control: no-cache` on `/` and `/index.html` (a stale `index.html` would name assets a
new deploy removed) and `public, max-age=31536000, immutable` on `/assets/**` (Vite names them
by content hash). "The URL pattern matching for custom headers is done before any rewrite rules
are applied" (Hosting configuration page), so `/api/**` answers get the security headers too;
for JSON that changes nothing, and the proxy sends its own stricter `default-src 'none'`.
**HSTS** is not set: "Firebase Hosting overwrites the Strict-Transport-Security configuration on
default Hosting subdomains (like *.web.app)".

**Vite's build manifest stays off the site.** `dist/.vite/manifest.json` exists for the build
check only. Firebase's default `ignore` list contains `"**/.*"` ("files with a leading period
should be hidden from the system"), which skips the `.vite/` directory; the rendered list keeps
the defaults and adds `"**/.vite/**"` explicitly.

### Same origin: what it changes for CORS

The rewrite puts the page and the proxy on **one origin**: the page calls `/api/...` on
`https://<site>.web.app` itself (`VITE_FHIR_PROXY_URL` unset, web/README.md). The browser sends no
CORS preflight and needs no `Access-Control-Allow-Origin` for same-origin calls. The proxy still
requires `ALLOWED_ORIGINS` to name at least one origin, so it is set to the page's origin
(`public_origin`, default `https://<site_id>.web.app`), plus `extra_allowed_origins` (empty by
default; for example `http://localhost:5173` for a local page against a deployed proxy). A
request from any other origin is never given CORS headers, so another site's script cannot read
an answer. The `connect-src 'self'` CSP matches this shape: the page cannot connect anywhere but
its own origin.

### Adding Firebase to the project

A Hosting site needs a Firebase project. Adding Firebase to a Google Cloud project is free and
**one-way** ("Once Firebase has been added to a Google Project it cannot be removed", provider
docs, `google_firebase_project`). It needs `firebase.projects.update`, which among predefined
roles only `roles/firebase.admin` and `roles/firebase.editor` (and service agents) hold, both
far broader than Hosting. So by default (`manage_firebase_project = false`) it is a one-time
step outside this module, by the project owner, before the first apply of the env root:

```bash
firebase projects:addfirebase "<project-id>"   # or: bootstrap adds google_firebase_project (requested below)
```

With `manage_firebase_project = true` this module adds it instead, and the deployer then also
needs `roles/firebase.admin`. The Firebase Management API (`firebase.googleapis.com`) must be
enabled for either path.

If the project's default site already exists (it is created when Hosting is first opened in the
Firebase console), import it rather than creating it:
`terraform import 'module.phr.google_firebase_hosting_site.dashboard' projects/<project>/sites/<site_id>`.

### Deploying the page (T26, or the deployer by hand)

Run after `terraform apply` (the rewrite needs the service to exist), from the repository root,
as the deployer (`roles/firebasehosting.admin`):

```bash
terraform -chdir=infra/envs/<env> output -raw firebase_json > web/firebase.json   # not committed
(cd web && npm ci && npm run build)            # VITE_FHIR_PROXY_URL unset: the page calls /api
(cd web && npx --yes firebase-tools@<pinned version> deploy --only hosting \
    --project "<project-id>" --non-interactive)
```

- `web/firebase.json` is generated and must not be committed (T26 writes it in CI; a
  `.gitignore` line is a small follow-up for the web owner). It holds no secret.
- The CLI uploads `web/dist` (minus the ignore list) as a new version with this configuration
  and releases it. A deploy that changes nothing in Terraform still takes the current
  `firebase.json`; a Terraform change to the headers or the rewrite takes effect only on the
  next page deploy, so T26 deploys the page after every apply.
- In CI, the Firebase CLI uses Application Default Credentials (the deployer through Workload
  Identity Federation). That the CLI accepts WIF external-account credentials is not verified.

## Public access and ingress

**fhir-proxy must be public.** Firebase's guide deploys the service with "Respond Y to allow
unauthenticated invocations" before adding the rewrite: Hosting calls the service without an
identity token. The session token (D9) is the gate; the proxy refuses every route but
`POST /auth/login` without it.

**How it is made public: the invoker check is disabled, not an `allUsers` binding.** Google's
"Allowing public (unauthenticated) access" (docs.cloud.google.com/run/docs/authenticating/public):

> There are two ways to create a public Cloud Run service: Disable the Cloud Run Invoker IAM
> check (recommended). Assign the Cloud Run Invoker IAM role to the allUsers member type.

and its Terraform form is `invoker_iam_disabled = true` on `google_cloud_run_v2_service`
(provider 8.11: "Disables IAM permission check for run.routes.invoke for callers of this
service"). This module follows the recommendation, which departs from the proxy README's "(`allUsers`
invoker)". Why: with the check off Cloud Run does not interpret the `Authorization` header at
all, so the reviewer's own `Bearer` session token (not a Google ID token) reaches the container
as sent, and no public IAM binding exists anywhere (`tests/static_checks.sh` refuses `allUsers`
and allows `invoker_iam_disabled = true` only on fhir-proxy). It also works under the "domain
restricted sharing" organization policy, which refuses `allUsers`.

**Ingress must be "all".** Google's ingress page (docs.cloud.google.com/run/docs/securing/ingress)
lists what "Internal" admits (internal load balancers, the same project's VPC networks, VPC
Service Controls, and Cloud Scheduler, Cloud Tasks, Dialogflow CX, Eventarc, Gemini Enterprise,
Pub/Sub, synthetic monitors, Workflows, BigQuery) and what "Internal and Cloud Load Balancers"
adds (the external Application Load Balancer). Firebase Hosting is in neither list. The same page
lists it among the services that call the default URL: "Disabling the default run.app URL prevents
these services ... from working as expected: ... Firebase Hosting". So Hosting reaches
`*.run.app` as an outside caller, and only `INGRESS_TRAFFIC_ALL` admits it. A consequence: the
proxy's `*.run.app` URL is reachable directly from the internet too, which is why the page must
use only the rewrite and why `TRUSTED_PROXY_HOPS` must be measured (a direct caller and a
Hosting-forwarded caller put different entries at the right of `X-Forwarded-For`). Restricting
the proxy to Hosting alone would need a load balancer in front (billed hourly, against D4).

**cuff-sim** is `INGRESS_TRAFFIC_INTERNAL_ONLY` with the invoker check on. fhir-proxy's calls
count as internal because they go through the VPC: "When calling from Cloud Run ... to a Cloud
Run service that's set to "Internal" ..., traffic must route through a VPC network that's
considered internal" (ingress page) — hence `ALL_TRAFFIC` on the proxy (the proxy README and
module ingestion's README, "For T22").

## Who may invoke what

| Caller | Role | On | Where |
| --- | --- | --- | --- |
| anyone (through the Hosting rewrite) | none needed: invoker check disabled | fhir-proxy | this module |
| `fhir-proxy` | `roles/run.invoker` | cuff-sim | this module (`local.service_invokers`) |
| `fhir-proxy` | `roles/run.invoker` | health-sync (real data) | module ingestion |
| `scheduler-invoker` | `roles/run.invoker` | health-sync | module ingestion |

**Data access** (module healthcare and identity, not here): `fhir-proxy` reads both FHIR stores
and the HL7v2 store and is the only accessor of `reviewer-password` and
`openemr-client-private-key`; `cuff-sim` may ingest into the HL7v2 store and edit the audit FHIR
store only (D15), and reads no secret.

## Network and egress

| Service | Egress | Why |
| --- | --- | --- |
| fhir-proxy | `ALL_TRAFFIC` | Its calls to cuff-sim's and health-sync's `*.run.app` URLs must go through the VPC to count as internal. Google APIs are reached through the subnet's Private Google Access; the VM (T15's OpenEMR calls) through the project-base firewall rule. No Cloud NAT is needed: the proxy talks only to Google services and the VM. |
| cuff-sim | `PRIVATE_RANGES_ONLY` | Its MLLP connection to the VM's private address goes through the VPC (the only path the firewall allows); its Cloud Healthcare calls take Cloud Run's normal path to Google's public endpoints. It calls no internal-only service, so routing everything through the VPC would add a dependency (Private Google Access) for nothing. `ALL_TRAFFIC` would work too. |

Both use project-base's `serverless` subnet in `var.region`, through `network_interfaces`
(`network` + `subnetwork`, no network tags: the firewall rule's source is the subnet's range).
Google: "Each Cloud Run instance consumes four IP addresses", with "2 times (2X) as many IP
addresses as the number of container instances" at steady state and a /26 minimum
(docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc); at most 3 instances here, against a
/24. Also from that page, for T25's destroy: "To delete the subnet, you must first delete or
redeploy your Cloud Run services ... and then wait 1-2 hours."

## Environment and secrets

From each service's `config.py` (`VARIABLES`), variable by variable. "Module sets" is what this
module passes; a blank means the service's default applies.

### fhir-proxy

| Variable | Service requires | Module sets |
| --- | --- | --- |
| `GCP_PROJECT_ID` | yes | `healthcare_project_id` (default `project_id`) |
| `HEALTHCARE_LOCATION` | yes | `healthcare_location` |
| `HEALTHCARE_DATASET_ID` | yes | `healthcare_dataset_id` |
| `FHIR_STORE_ID` | yes | `fhir_store_id` |
| `AUDIT_FHIR_STORE_ID` | yes | `audit_fhir_store_id` (validated to differ) |
| `HL7V2_STORE_ID` | yes | `hl7v2_store_id` |
| `PATIENT_ID` | yes | `patient_id` |
| `REVIEWER_PASSWORD_FILE` | yes | `/secrets/reviewer-password/value` (the secret volume) |
| `ALLOWED_ORIGINS` | yes | `public_origin` + `extra_allowed_origins` |
| `CUFF_SIM_URL` | yes | cuff-sim's `uri` (also the identity token's audience) |
| `PUBLIC_BASE_URL` | yes | `public_origin` + `/api` |
| `HEALTH_SYNC_URL` | no | `health_sync_url`, only when not null |
| `PATIENT_ASSIGNING_AUTHORITY`, `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | no (all or none) | all three: cuff-sim's values, so traces show a sent sample's exact bytes |
| `PATH_PREFIX` | no | `/api` |
| `TRUSTED_PROXY_HOPS` | no (1) | `trusted_proxy_hops` (default 1) |
| `TOKEN_ISSUER` | no | `pulsebridge-fhir-proxy:<project_id>:<name_prefix>` (deployment-specific: salts the key derivation) |
| `REQUEST_BUDGET_SECONDS` | no (50) | `50` |
| `CUFF_FORWARD_TIMEOUT_SECONDS` | no (55) | `55` |
| `REQUEST_TIMEOUT_SECONDS` | no (120) | `120`, the same as the Cloud Run request timeout |
| `SESSION_TTL_SECONDS`, `PASSWORD_CACHE_SECONDS`, `LOGIN_*`, `FHIR_TIMEOUT_SECONDS`, `SAMPLES_FORWARD_TIMEOUT_SECONDS`, `HEALTH_SYNC_TIMEOUT_SECONDS`, `STATUS_CACHE_SECONDS` | no | service defaults (8 h tokens, 30 s rotation delay, 5/60/60 login limits, ...) |
| `OPENEMR_SITE_ADDRESS`, `OPENEMR_TOKEN_URL`, `OPENEMR_FHIR_BASE_URL`, `OPENEMR_SCOPES`, `OPENEMR_CLIENT_FILE` | not read yet (T15) | only when `openemr` is given; the file is `/secrets/openemr-client/value` |

**Secrets are files, never environment variables.** Each is a secret volume at version `latest`
in its own directory: `reviewer-password` → `/secrets/reviewer-password/value`, and with
`openemr` the client document → `/secrets/openemr-client/value`. Google: "When reading a
volume, Cloud Run always fetches the secret value from the Secret Manager to use the value with
the latest version", whereas "Environment variables are resolved at instance startup time"
(docs.cloud.google.com/run/docs/configuring/services/secrets). The proxy re-reads the file after
`PASSWORD_CACHE_SECONDS` (30 s), so `set-reviewer-password` (T27) takes effect within 30 s with
no redeploy, and every session ends at once (the token key derives from the password). "During
service deployment, Cloud Run checks all the secrets you use", so the accessor grant (module
identity) must exist first; a secret with no version yet does not stop the deploy ("If you mount
the secret as a volume, Cloud Run doesn't perform any checks during instance startup"): login
answers 503 until a version exists.

### cuff-sim

| Variable | Service requires | Module sets |
| --- | --- | --- |
| `GCP_PROJECT_ID` | yes | `healthcare_project_id` (default `project_id`) |
| `HEALTHCARE_LOCATION` | yes | `healthcare_location` |
| `HEALTHCARE_DATASET_ID` | yes | `healthcare_dataset_id` |
| `HL7V2_STORE_ID` | yes | `hl7v2_store_id` |
| `AUDIT_FHIR_STORE_ID` | yes | `audit_fhir_store_id` (validated to differ from the HL7v2 store) |
| `MLLP_HOST` | yes | `mllp_host`: the VM's reserved internal IPv4 address (an address: a host name lookup would have no time limit) |
| `MLLP_PORT` | no (2575) | `mllp_port` (module edge-vm's, the port project-base's firewall opens) |
| `MLLP_TIMEOUT_SECONDS` | no (5) | `5` |
| `HTTPS_TIMEOUT_SECONDS` | no (10) | **`6`**, so the HTTPS worst case fits under Hosting's 60 s |
| `PATIENT_ID` | yes | `patient_id`: the same value as v2-to-fhir's (module ingestion) |
| `PATIENT_ASSIGNING_AUTHORITY` | yes | `patient_assigning_authority`: pass the same value to module ingestion, so v2-to-fhir accepts PID-3.4 |
| `PATIENT_FAMILY_NAME`, `PATIENT_GIVEN_NAME` | yes | with synthetic data `Synthetic` / `Demo` (enforced); otherwise the variables (required) |
| `SENDING_APPLICATION`, `SENDING_FACILITY`, `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` | no | service defaults (`CUFFSIM`, `PULSEBRIDGE`, 6, 3) |
| `PORT` | no | Cloud Run sets it |

No `FHIR_STORE_ID`: cuff-sim has no access to the health data (D15).

**The synthetic-name rule.** In a synthetic deployment the first cuff reading may create the
Patient by conditional create (D11), with the name cuff-sim sends in PID-5. The seeder refuses
to work on a Patient whose name is not its synthetic one (family `Synthetic`, given `Demo`;
services/seed/README.md), so with `use_synthetic_data = true` the module sends exactly that name
and refuses any other at plan time. In a real deployment the names are required, come from
tfvars, and end up in Cloud Run's configuration and in the Terraform state (as the patient ID
does): treat the state as sensitive.

## Timeouts end to end

| Hop | Limit | Set by |
| --- | --- | --- |
| Browser → Hosting → fhir-proxy | **60 s**, not settable; afterwards 504 | Firebase Hosting |
| fhir-proxy, every route but the cuff send | **50 s** budget for all upstream calls (`REQUEST_BUDGET_SECONDS`) | this module |
| fhir-proxy → cuff-sim, `POST /cuff/readings` | **55 s** (`CUFF_FORWARD_TIMEOUT_SECONDS`); then 504 "may have been sent" | this module |
| fhir-proxy's Cloud Run request timeout | **120 s** (= `REQUEST_TIMEOUT_SECONDS`; the proxy requires both budgets ≥ 10 s below it) | this module |
| cuff-sim worst case, MLLP | `MLLP_TIMEOUT + 4 × T` = 5 + 4 × 6 = **29 s** | cuff-sim README, "Time limits"; T = `HTTPS_TIMEOUT_SECONDS` = 6 |
| cuff-sim worst case, HTTPS | `8 × T` = **48 s** (ingest and audit write, each doubled by a token refresh and 401 retry) | same |
| cuff-sim's Cloud Run request timeout | **60 s** | this module |

The relations, asserted by `tests/real_data.tftest.hcl` from the deployed values:
**29 s and 48 s < 55 s < 60 s (Hosting)**, and **55 s < 60 s (cuff-sim's own timeout)**.

- **Is Hosting's limit shorter than cuff-sim's documented worst case?** With cuff-sim's default
  `HTTPS_TIMEOUT_SECONDS=10` the HTTPS worst case is 80 s, and 90 s was the request timeout its
  README asked for: both are longer than Hosting's 60 s. So the proxy's own forward timeout is
  set **below** 60 s (55 s), and cuff-sim's timeouts are lowered to fit under it (T = 6). The
  page then always gets the proxy's clear answer (its 504 `upstream-timeout` with "may have been
  sent") rather than Hosting's bare 504.
- **cuff-sim's request timeout is 60 s, not the work plan's older 90 s.** It must be *above* the
  proxy's 55 s, so that a send the proxy has already given up on still finishes and records its
  AuditEvent (D12), and 60 s is above its 48 s worst case. 90 s was sized for T = 10 with no
  Hosting in front; the later acceptance criterion about fitting under Hosting's 60 s
  supersedes it.
- The worst cases are cuff-sim's documented derivation, not measurements (its README).

## Instances, concurrency and the rate limits

- **fhir-proxy**: `max_instance_count = 2`, concurrency **8** = gunicorn's 8 threads (one
  worker). The login limit is in memory per process, so with two instances the real limits are
  up to 10 failed logins a minute per address and 120 in total.
- **cuff-sim**: one gunicorn worker per instance with its own token bucket (6 sends a minute,
  bursts of 3). With `cuff_sim_max_instances = 1` (default) that is the service-wide limit
  (output `cuff_sim_rate_limit_per_minute` = 6); with 3 instances it would be 18.

## Cost

**Idle cost: $0.** Nothing here is always on or billed by the hour.

| Item | Billed for | At PulseBridge's volume |
| --- | --- | --- |
| Firebase Hosting | storage above 10 GB ("Storage for your Hosting content is at no cost up to 10 GB") and transfer above 10 GB a month ("Data transfer from the CDN to your end users is at no cost up to 10 GB/month"), [Hosting usage and pricing](https://firebase.google.com/docs/hosting/usage-quotas-pricing) | the page is about 120 kB; each kept release adds that again: $0 |
| Cloud Run (both services) | request time while serving; `min_instance_count = 0`, `cpu_idle = true` | nothing while nobody uses the dashboard; inside Cloud Run's free tier for a demo (general knowledge, not checked here) |
| Direct VPC egress | nothing beyond normal network egress; no connector VMs | $0 |
| Cloud Healthcare API calls | per request (module healthcare) | small |

**An open tab.** The dashboard shell refreshes `/status` every 5 minutes (`STATUS_REFRESH_MS`,
web/src/shell/app.ts), and after a cuff send it polls every 3 s for at most 2 minutes, only
while the tab is visible (web/src/state/poll.ts). Each `/status` is one proxy request (a few
Cloud Healthcare reads, cached 60 s, plus health-sync's `/status`): about 288 requests a day per
tab left open, each billed for its own seconds. A cold start may happen on each one, because an
instance idles out between them. That is cents a month at most, not an always-on instance.

A billing account linked to the project puts Firebase on its pay-as-you-go plan, which rewrites
to Cloud Run need; the free quotas above still apply.

## For T25: what to wire

**Outputs consumed from other modules** (exact names):

| This module's variable | From |
| --- | --- |
| `healthcare_project_id`, `healthcare_location`, `healthcare_dataset_id` | `module.healthcare.project_id`, `.location`, `.dataset_name` |
| `fhir_store_id`, `audit_fhir_store_id`, `hl7v2_store_id` | `module.healthcare.fhir_store_name`, `.audit_fhir_store_name`, `.hl7v2_store_name` |
| `service_account_emails` | `module.identity.service_account_emails` (keys `fhir-proxy`, `cuff-sim`) |
| `secret_ids` | `module.identity.secret_ids` (key `reviewer-password`) |
| `network_name`, `serverless_subnet_name` | `module.project_base.network_name`, `.serverless_subnet_name` |
| `mllp_host`, `mllp_port` | `module.edge_vm.mllp_host`, `.mllp_port` |
| `health_sync_url` | `module.ingestion.health_sync_url` (null with synthetic data) |
| `openemr` (optional) | `module.openemr.site_address`, `.token_url`, `.fhir_base_url`, `.scopes`, `.client_secret_id` (T23, in review) |

**Variables T25 sets from tfvars:** `project_id`, `region`, `use_synthetic_data`,
`fhir_proxy_image`, `cuff_sim_image`, `patient_id`, `patient_assigning_authority` (and the same
value to module ingestion), the two patient names (real data only); optionally `name_prefix`,
`labels`, `hosting_site_id`, `public_origin` (T30), `extra_allowed_origins`,
`trusted_proxy_hops`, `manage_firebase_project`, `cuff_sim_max_instances`, `deletion_protection`.

**Ordering.** Nothing here waits for `ingest_enabled`: a cuff reading before the setup step has
written the Patient (D11) creates an identifier-only Patient by conditional create, which the
setup's conditional update then completes. `depends_on = [module.identity]` makes the secret
accessor grants exist before the proxy deploys. The proxy's revision waits for its invoker grant
on cuff-sim (inside this module). The `google-beta` provider must be configured in the env root.

**`terraform destroy`**: both services go (`deletion_protection = false` by default). The Hosting
site is deleted with its releases; Firebase stays on the project (it cannot be removed, and
costs nothing). The serverless subnet cannot be deleted for 1-2 hours after the services that
used it (see "Network and egress").

### Changes requested in other modules (not made here)

The acceptance criterion asks for the Firebase Hosting API and the deployer's hosting role to be
added "through the existing variables of project-base and bootstrap, not hardcoded". Both
variables (`enabled_apis`, `deployer_roles`) already exist, with defaults. Passing a full list
from T25 would replace the default and copy every other entry into the env root, so the minimal
change is one line in each default (owners: project-base and bootstrap):

- `infra/modules/project-base/variables.tf`, `enabled_apis` default, two lines:
  ```hcl
  "firebase.googleapis.com",        # Firebase Management API: adding Firebase to the project (module phr, D4)
  "firebasehosting.googleapis.com", # Firebase Hosting: the dashboard site and its deploys (module phr, D4)
  ```
  and replace the description's sentence "Add firebasehosting.googleapis.com here if decision D4
  picks Firebase Hosting." (D4 has picked it) and the matching README bullet.
- `infra/bootstrap/variables.tf`, `deployer_roles` default, replacing the stale
  `# Dashboard bucket (module phr).` / `"roles/storage.admin",` pair only if nothing else needs
  storage admin (the state bucket is bootstrap's own), else adding:
  ```hcl
  # Firebase Hosting site and page deploys (module phr, D4; T26's deploy step).
  "roles/firebasehosting.admin",
  ```
- Optional, bootstrap: add Firebase to the project once with the owner's credentials
  (`google_firebase_project`, google-beta), so the deployer never needs `roles/firebase.admin`.
  Until then the owner runs `firebase projects:addfirebase` once (above).

## For T26: the page deploy

After each successful apply of an env root: write `firebase_json` to `web/firebase.json`, build
the page, `firebase deploy --only hosting --project <project>` (commands in "Deploying the page").
Identity: the deployer service account through Workload Identity Federation, with
`roles/firebasehosting.admin`. Pin the `firebase-tools` version. No key in GitHub.

## For T15: the share to OpenEMR

Pass `openemr` (module openemr's five outputs) when T15's code reads them: fhir-proxy then has
`OPENEMR_SITE_ADDRESS`, `OPENEMR_TOKEN_URL`, `OPENEMR_FHIR_BASE_URL`, `OPENEMR_SCOPES` and the
client document as a file at `OPENEMR_CLIENT_FILE` (`/secrets/openemr-client/value`, version
`latest`, so a re-registration on the VM takes effect without a redeploy). The names are module
openemr's suggestions; if T15 chooses others, change the map in `fhir_proxy.tf` and the test.
The calls go through the proxy's Direct VPC egress to the VM's port 443, which project-base's
firewall allows from the serverless subnet.

## Verify on the first real deploy

Nothing in this module has been applied; every test runs against mocked providers. In order:

1. **`TRUSTED_PROXY_HOPS`.** Through the Hosting domain, make two failed logins
   (`POST https://<site>.web.app/api/auth/login` with a wrong password), one plain and one with a
   made-up `X-Forwarded-For: 192.0.2.1`. Read the two `login-failed` log lines of fhir-proxy:
   `forwarded_for_entries` (plain request) and `client_address`. If the plain request has 1
   entry and `client_address` is your own address, keep `1`. If it has 2 and the right one is a
   Google/Firebase address, set `trusted_proxy_hops = 2` and re-apply; then check that the
   made-up entry never becomes `client_address`.
2. **`Authorization` reaches the proxy.** Log in through the page, then load the dashboard: a
   `200` from `/api/status` (not 401) proves the header was forwarded.
3. **The rewrite path and prefix.** `GET https://<site>.web.app/api/fhir/metadata` with the token
   answers the CapabilityStatement; `/api/unknown` answers the proxy's 401/404 (an
   OperationOutcome), not Hosting's 404 page, and search `link` URLs start with `/api/`.
4. **Internal-only reachability.** From outside, `curl` cuff-sim's and health-sync's
   `*.run.app` URLs: both must be refused (403/404 at the network edge) even with a valid
   identity token. Through the page, a cuff send succeeds (proxy → cuff-sim through the VPC)
   and the status strip shows health-sync's state (real data), not
   `health-sync-unreachable`.
5. **MLLP through the VPC.** A cuff send over MLLP returns an `AA` acknowledgment (cuff-sim →
   VM `mllp_host:2575` through the serverless subnet and project-base's firewall).
6. **The secret mount.** Before any version exists, login answers 503; after
   `set-reviewer-password`, login works within about 30 s with no redeploy; after a second new
   version, the old token is refused (401) and the new password works.
7. **The CSP in the browser.** Open the page with the developer tools console: zero CSP
   violations while logging in, opening every tab, drawing the chart and sending a cuff reading.
   Check the response headers of `/` include the policy above and `frame-ancestors 'none'`, and
   that `/.vite/manifest.json` is 404.
8. **The 60 s limit.** A forced slow cuff send (for example with the VM's adapter stopped) gets
   the proxy's own 504 OperationOutcome within about 55 s, not Hosting's bare 504, and its
   AuditEvent appears in the messages log afterwards.
9. **Startup probes.** A revision whose configuration is bad never opens its port (`--preload`;
   `make images-smoke` proves it inside the container), so its TCP probe fails. Confirm once that
   Cloud Run then keeps the previous revision serving and the apply reports the failure: deploy
   a copy of the proxy by hand with `gcloud run deploy <name>-probe-check --no-traffic` and one
   required variable missing, watch it fail, delete it.

## Not verified

- What Firebase Hosting puts in `X-Forwarded-For`, and that it forwards `Authorization` and the
  full `/api/...` path (documented only indirectly; steps 1 to 3).
- That a request through Hosting reaches a service whose invoker check is disabled with no
  further setting (Firebase's guide uses "allow unauthenticated invocations", which in current
  `gcloud` is the same switch or an `allUsers` binding).
- That the Firebase CLI accepts Workload Identity Federation credentials in CI, and that
  `roles/firebasehosting.admin` is enough for `firebase deploy --only hosting` with a Cloud Run
  rewrite (the CLI may read the service; the deployer has `roles/run.admin` anyway).
- That creating the default site (`site_id = project_id`) through the API works on a project
  where it does not exist yet, and how the 409 looks when it does (then import).
- cuff-sim's worst-case timings (derived, not measured), and that Hosting's limit is exactly 60 s
  in practice.
- Direct VPC egress with `ALL_TRAFFIC` reaching Google APIs through Private Google Access, and
  the internal-only services accepting the proxy's calls as internal.

## Tests

`terraform test` in this directory runs `tests/*.tftest.hcl` against mocked `google` and
`google-beta` providers: no credentials, nothing created. `make tf-test` runs it with Google
credentials removed from the environment, then `tests/static_checks.sh` and
`tests/check_hosting_csp.py`.

| File | Checks |
| --- | --- |
| `real_data.tftest.hcl` | only fhir-proxy public (invoker check off, ingress all); cuff-sim internal with the check on; the invoker bindings are exactly fhir-proxy on cuff-sim; no public member; both scale to zero; max 2 and 1; concurrency 8 and 4; the timeout relations read from the deployed values (proxy timeout = `REQUEST_TIMEOUT_SECONDS`, budgets < 60, forward < cuff-sim's timeout, cuff-sim's worst cases < forward) and the documented numbers; Direct VPC egress settings of both; images, accounts, probe types and limits; `deletion_protection`; the reviewer password as a volume at `latest`, its mount, no secret in any environment variable; the exact environment of both services; custom origin, dev origin, hop count; OpenEMR settings and mount when given and absent by default; the site, `firebase_json`'s rewrite, ignore list and headers; the CSP has no unsafe source; `manage_firebase_project` |
| `synthetic.tftest.hcl` | the synthetic name by default and given explicitly, for both services; no `HEALTH_SYNC_URL`; the rewrite's region |
| `validation.tftest.hcl` | tag, tag-plus-digest and short-digest images refused; `http`, path and synthetic-mode health-sync URLs; public origin with a path or `http`; wildcard and trailing-slash origins; an `http` OpenEMR URL; hop counts −1, 6 and 1.5 refused and 5 allowed; a real name (family or given) with synthetic data; missing names without; `SYN-` rules both ways; a backslash in a name; a long assigning authority; audit store equal to the FHIR store; missing account or secret keys; a host name as `mllp_host`; a region Hosting cannot rewrite to; a subnet path; too many cuff-sim instances |
| `static_checks.sh` | no public principal; `invoker_iam_disabled = true` and `INGRESS_TRAFFIC_ALL` exactly once, on fhir-proxy; every `min_instance_count` 0 and no manual scaling; no authoritative or project IAM; no service account key; the one reviewed IAM member resource with members from `var.service_account_emails`; no secret in an environment variable and every secret version `latest`; no hardcoded project, service account, registry path, `https://<host>` (outside variable examples), site or service ID; no child module; no Hosting version, release or channel; fhir-proxy's `depends_on` on the invoker grant |
| `check_hosting_csp.py` | the header CSP equals `web/vite.config.ts`'s same-origin policy plus `frame-ancestors 'none'`, agrees with `web/scripts/check-bundle.mjs`'s required directives, has one CSP header only, and allows no `unsafe-*`, wildcard or scheme source |

```bash
make tf-test                      # from the repository root (credential-free)
tests/static_checks.sh            # from this directory, or pass the module path as $1
```

## Documentation relied on

Read directly (`curl`, 2026-11-15) unless marked otherwise:

- Terraform providers `hashicorp/google` and `hashicorp/google-beta` 8.11.0: `terraform providers
  schema` (which Firebase resources exist in which provider; `google_cloud_run_v2_service`
  `invoker_iam_disabled`, `vpc_access.network_interfaces`, `egress` values,
  `max_instance_request_concurrency`, `startup_probe.tcp_socket`/`http_get`, secret volume
  `items.version`), and the docs on GitHub at tag `v8.11.0`: `firebase_hosting_version`
  ("Static files are not supported at the moment"), `firebase_hosting_site`,
  `firebase_hosting_release`, `firebase_project` ("cannot be removed"), `cloud_run_v2_service`.
- Firebase: [Hosting: Cloud Run](https://firebase.google.com/docs/hosting/cloud-run) (regions,
  60-second timeout, unauthenticated invocations, `pinTag`),
  [Configure Hosting behavior](https://firebase.google.com/docs/hosting/full-config) (priority
  order, `public`, `ignore` defaults, headers matched before rewrites, methods, HSTS overwritten),
  [Manage cache behavior](https://firebase.google.com/docs/hosting/manage-cache) (cookies
  stripped, `Vary: Authorization`), [Usage, quotas and pricing](https://firebase.google.com/docs/hosting/usage-quotas-pricing),
  [Deploy with the REST API](https://firebase.google.com/docs/hosting/api-deploy) (the upload flow
  the CLI performs), `projects.sites.create` (`siteId` rules).
- Cloud Run: [ingress](https://docs.cloud.google.com/run/docs/securing/ingress),
  [public access](https://docs.cloud.google.com/run/docs/authenticating/public),
  [private networking](https://docs.cloud.google.com/run/docs/securing/private-networking),
  [Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc),
  [secrets](https://docs.cloud.google.com/run/docs/configuring/services/secrets),
  [health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks) (default TCP
  probe 240 s; 2XX/3XX; the 600 s bound),
  [service-to-service](https://docs.cloud.google.com/run/docs/authenticating/service-to-service).
- IAM: [Firebase Hosting roles](https://docs.cloud.google.com/iam/docs/roles-permissions/firebasehosting),
  [Firebase roles](https://docs.cloud.google.com/iam/docs/roles-permissions/firebase)
  (`firebase.projects.update` holders).
- The services' READMEs and `config.py` (fhir-proxy, cuff-sim, seed), web/README.md,
  web/vite.config.ts, web/scripts/check-bundle.mjs, and modules ingestion, identity, healthcare,
  project-base, edge-vm, bootstrap, and openemr on branch `task/t23-tf-openemr` (in review).

## Inputs

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `project_id` | `string` | required | Project of the services and the site. |
| `region` | `string` | `"us-central1"` | Region of both services; must be one Hosting rewrites support. |
| `name_prefix` | `string` | `"pulsebridge"` | `<prefix>-fhir-proxy`, `<prefix>-cuff-sim`. |
| `labels` | `map(string)` | `{}` | Labels on both services. |
| `deletion_protection` | `bool` | `false` | Refuse to destroy the services. |
| `use_synthetic_data` | `bool` | `false` | Synthetic deployment: `SYN-` patient, synthetic name, no health-sync. |
| `fhir_proxy_image`, `cuff_sim_image` | `string` | required | Images by digest. |
| `healthcare_project_id` | `string` | `null` (= `project_id`) | Project of the dataset. |
| `healthcare_location`, `healthcare_dataset_id` | `string` | required | Dataset coordinates. |
| `fhir_store_id`, `audit_fhir_store_id`, `hl7v2_store_id` | `string` | required | Short store IDs. |
| `patient_id` | `string` | required | The configured patient (same as module ingestion's). |
| `patient_assigning_authority` | `string` | required | PID-3.4 (same value to module ingestion). |
| `patient_family_name`, `patient_given_name` | `string` | `null` | PID-5; required without synthetic data, `Synthetic`/`Demo` with it. |
| `service_account_emails` | `map(string)` | required | Module identity's map (keys `fhir-proxy`, `cuff-sim`). |
| `secret_ids` | `map(string)` | required | Module identity's map (key `reviewer-password`). |
| `network_name`, `serverless_subnet_name` | `string` | required | project-base's VPC and serverless subnet. |
| `mllp_host` | `string` | required | The VM's internal IPv4 address. |
| `mllp_port` | `number` | `2575` | The MLLP adapter's port. |
| `health_sync_url` | `string` | `null` | health-sync's URL; null with synthetic data. |
| `openemr` | `object` | `null` | Module openemr's outputs (T15), all or none. |
| `manage_firebase_project` | `bool` | `false` | Add Firebase to the project here. |
| `hosting_site_id` | `string` | `null` (= `project_id`) | The Hosting site. |
| `public_origin` | `string` | `null` (= `https://<site>.web.app`) | The origin reviewers open. |
| `extra_allowed_origins` | `list(string)` | `[]` | Further CORS origins. |
| `trusted_proxy_hops` | `number` | `1` | `TRUSTED_PROXY_HOPS`, 0 to 5; measure on the first deploy. |
| `cuff_sim_max_instances` | `number` | `1` | 1 to 3; the cuff rate limit is per instance. |

## Outputs

| Name | Description |
| --- | --- |
| `fhir_proxy_url`, `fhir_proxy_service_name` | The proxy's run.app URL (not for the page) and name. |
| `cuff_sim_url`, `cuff_sim_service_name` | cuff-sim's URL and name. |
| `region` | Region of both services. |
| `hosting_site_id`, `hosting_default_url` | The site. |
| `public_origin`, `public_base_url` | The page's origin; the proxy's `PUBLIC_BASE_URL`. |
| `firebase_json` | The `firebase.json` the page deploy writes to `web/`. |
| `content_security_policy` | The CSP header. |
| `timeouts` | Every timeout in the path and cuff-sim's worst cases. |
| `cuff_sim_rate_limit_per_minute` | 6 × `cuff_sim_max_instances`. |
