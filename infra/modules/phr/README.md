# Module `phr`

The dashboard's front door (task T22): the **Firebase Hosting site** that serves the page, the
**`fhir-proxy`** Cloud Run service behind it (the only public service, D10), and the
**`cuff-sim`** Cloud Run service that only the proxy may call.

```
browser ──HTTPS──> Firebase Hosting  https://<site>.web.app      (static page: web/dist; security headers, CSP)
                     │ rewrite /api/** (same origin, no CORS)      Hosting ends a forwarded request at 60 s
                     ▼
                   fhir-proxy  (Cloud Run, public, max 2 instances, concurrency 8, request timeout 70 s)
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
  # Firebase APIs want a quota project; the provider docs ask for these two settings.
  user_project_override = true
  billing_project       = var.project_id
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

  patient_id                  = var.patient_id                  # ONE root variable, also module.ingestion's
  patient_assigning_authority = var.patient_assigning_authority # ONE root variable, also module.ingestion's
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

  trusted_proxy_hops = var.trusted_proxy_hops # 2 behind Firebase Hosting (measured); see "The client address ..."

  # Optional (T41): CI's page deployer (bootstrap output page_deployer_service_account_email) gets
  # roles/run.viewer on fhir-proxy, which `firebase deploy` needs for the /api rewrite.
  page_deployer_service_account_email = var.page_deployer_service_account_email

  # project-base enables the Cloud Run, Firebase Hosting and Compute APIs; identity's secret
  # accessor grants must exist first, because Cloud Run checks at deploy time that the service
  # account may read every mounted secret.
  depends_on = [module.project_base, module.identity]
}

module "ingestion" {
  # ...
  patient_id                  = var.patient_id
  patient_assigning_authority = var.patient_assigning_authority # NOT null: cuff-sim always sends it
}

# The cuff path breaks silently if the two modules disagree: v2-to-fhir refuses messages whose
# PID-3 does not match its patient (and, when set, its assigning authority).
check "one_patient_for_the_cuff_path" {
  assert {
    condition     = var.patient_id != "" && var.patient_assigning_authority != null
    error_message = "patient_id and patient_assigning_authority must be set once and passed to both phr and ingestion; ingestion's authority must not be null while phr is deployed."
  }
}
```

Both modules take the same root variables, so they cannot drift. The `check` block runs on every
plan and apply and warns (it does not stop the run): a `null` authority would leave v2-to-fhir
accepting any PID-3.4 while cuff-sim sends one.

## What each resource is, and why

| Resource | What and why |
| --- | --- |
| `google_cloud_run_v2_service.fhir_proxy` | The dashboard's API (T14). Runs as `fhir-proxy`; public (invoker check off, ingress all; see "Public access and ingress"); `min 0`, **`max 2`**; concurrency **8** (= gunicorn's threads); request timeout **70 s** (= `REQUEST_TIMEOUT_SECONDS`); Direct VPC egress `ALL_TRAFFIC`; `reviewer-password` as a file at `latest`; TCP startup probe. |
| `google_cloud_run_v2_service.cuff_sim` | The cuff simulator (T13). Runs as `cuff-sim`; internal-only, invoker check on; `min 0`, `max cuff_sim_max_instances` (1); concurrency 4; request timeout **60 s**; Direct VPC egress `PRIVATE_RANGES_ONLY`; `HTTPS_TIMEOUT_SECONDS=6`; HTTP startup probe on `GET /cuff/samples`. |
| `google_cloud_run_v2_service_iam_member.invoker` | `roles/run.invoker` on cuff-sim for `fhir-proxy`'s account: the only invoker binding (`local.service_invokers`). |
| `google_cloud_run_v2_service_iam_member.page_deployer_viewer` | Only with `page_deployer_service_account_email` (T41): `roles/run.viewer` on fhir-proxy for CI's page deployer. Firebase Hosting checks `run.services.get` on a rewrite's Cloud Run target when `firebase deploy` finalizes a version; without it the deploy fails after the upload ("Deploying the page"). Read-only, and on this one service. |
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
| `Cross-Origin-Resource-Policy` | `same-origin` | Another site cannot embed the page's scripts, styles or images. |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | The page uses none of them; nothing injected could ask for them. |
| `Strict-Transport-Security` | `max-age=31536000` | HTTPS only, remembered for a year; effective on a custom domain only (below). |

Plus `Cache-Control: no-cache` on `/` and `/index.html` (a stale `index.html` would name assets a
new deploy removed) and `public, max-age=31536000, immutable` on `/assets/**` (Vite names them
by content hash). "The URL pattern matching for custom headers is done before any rewrite rules
are applied" (Hosting configuration page), so `/api/**` answers get the security headers too;
for JSON that changes nothing, and the proxy sends its own stricter `default-src 'none'`.
**HSTS** (`Strict-Transport-Security: max-age=31536000`, no `includeSubDomains`, which would pin
sibling hosts of a custom domain) takes effect only on a custom domain (T30): "Firebase Hosting
overwrites the Strict-Transport-Security configuration on default Hosting subdomains (like
*.web.app). However, any connected custom domains will serve the configured value."

**What the browser tests covered.** The page's browser tests (web/, Playwright) ran the MOCK
build, whose `<meta>` policy has `connect-src 'none'`. The real build's connections to `/api`
under this header are therefore first exercised on the deploy: step 7 of the checklist.

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
far broader than Hosting. So **bootstrap does it** (`add_firebase`, default `true`), once, with
the owner's credentials (infra/bootstrap/README.md, "Firebase"), and this module's
`manage_firebase_project` stays `false`. Set it `true` only for a project bootstrap did not set
up, with a deployer that holds `roles/firebase.admin`. The Firebase Management API
(`firebase.googleapis.com`) is enabled by bootstrap and is also in project-base's list.

**An existing site is adopted, not duplicated.** The provider's create first reads the site and,
if it exists, takes it over ("Firebase hosting site already exists", google-beta v8.11.0
`resource_firebase_hosting_site.go`), so a default site created earlier (for example by opening
Hosting in the Firebase console) needs no `terraform import`.

**Destroy leaves the default site, and its last page, public.** The same file's delete returns
early for the project's default site (`if siteType == "DEFAULT_SITE"`: "Skip deleting default
hosting side"). After `terraform destroy` the last deployed page stays online at
`https://<project>.web.app`, with every `/api` call failing because fhir-proxy is gone. If no page
should stay live, the teardown runs `firebase hosting:disable --project <project-id>` (it stops
serving the site; the site itself remains). A non-default site (`hosting_site_id` set to another
ID) is deleted with its releases.

### Deploying the page (CI since T26, or the deployer by hand)

Run after `terraform apply` (the rewrite needs the service to exist), from the repository root,
as the deployer (it holds `roles/firebasehosting.admin`, and through
`roles/serviceusage.serviceUsageAdmin` and `roles/run.admin` the two reads listed below):

```bash
terraform -chdir=infra/envs/<env> output -raw firebase_json > web/firebase.json   # not committed
(cd web && npm ci && npm run build)            # VITE_FHIR_PROXY_URL unset: the page calls /api
(cd web && npx --yes firebase-tools@<pinned version> deploy --only hosting \
    --project "<project-id>" --non-interactive)
```

- `web/firebase.json` is generated and is git-ignored (`web/.gitignore`, with the CLI's
  `.firebase/` cache). It holds no secret.
- The CLI uploads `web/dist` (minus the ignore list) as a new version with this configuration
  and releases it. A deploy that changes nothing in Terraform still takes the current
  `firebase.json`; a Terraform change to the headers or the rewrite takes effect only on the
  next page deploy, so CI deploys the page after every apply.
- In CI (infra/envs/README.md, "CI/CD") the page is built in a job with no cloud credential, and
  deployed by a separate job as the **page deployer** through Workload Identity Federation, with
  the `firebase-tools` tree locked in `infra/envs/ci/deploy-tools/package-lock.json`. The CLI uses
  Application Default Credentials and accepted the external-account credential file in the first
  CI run on dev (18 November 2026; checklist CI6).
- **What `firebase deploy` needs** (measured in that run; `roles/firebasehosting.admin` alone was
  refused twice):
  1. `serviceusage.services.get` on the project: the CLI first checks that the Hosting API is
     enabled; without it, 403 "Permission denied to get service". Bootstrap grants the page
     deployer `roles/serviceusage.serviceUsageViewer`.
  2. `run.services.get` on fhir-proxy: after the files are uploaded, finalizing the version
     (`PATCH .../versions/<v>?updateMask=status,config`) makes Hosting check the rewrite's Cloud
     Run target; without it, 403 "Permission 'run.services.get' denied on resource
     'namespaces/<number>/services/<prefix>-fhir-proxy'". This module grants `roles/run.viewer`
     on that service only when `page_deployer_service_account_email` is set.

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
use only the rewrite, and why the hop count chosen for Hosting has a known limit for direct
callers (next section). Restricting the proxy to Hosting alone would need a load balancer in
front (billed hourly, against D4).

**cuff-sim** is `INGRESS_TRAFFIC_INTERNAL_ONLY` with the invoker check on. fhir-proxy's calls
count as internal because they go through the VPC: "When calling from Cloud Run ... to a Cloud
Run service that's set to "Internal" ..., traffic must route through a VPC network that's
considered internal" (ingress page) — hence `ALL_TRAFFIC` on the proxy (the proxy README and
module ingestion's README, "For T22").

## The client address and the direct run.app URL

The login limit (services/fhir-proxy/README.md, "Reviewer login") counts failed logins per
**client address**: the rate-limit **key**, and each key's count is its **bucket**. The proxy
takes the key from `X-Forwarded-For`, counting `TRUSTED_PROXY_HOPS` entries from the right
(`client_address.py`). Each proxy in front of the service is one **hop**. The hop count says how
many right-hand entries were written by Google's proxies; the key is the entry just left of
them. Anything further left was written by the caller and can be anything.

**What the proxy receives, and the key it takes.** All rows measured on dev on 17 November 2026
(infra/envs/README.md, checklist P1). The Hosting rows were measured from a machine with no proxy
of its own. The direct rows with a header were measured with 2 hops configured from a session
behind an egress proxy, so the right-most entry there is not the session's own address; the
entries the caller wrote were logged exactly as sent.

| Path | `X-Forwarded-For` at the proxy | Key with 2 hops | Key with 1 hop |
| --- | --- | --- | --- |
| through Hosting | `<visitor>, <Hosting>` | the visitor | a Hosting address (it changed between two requests; how many there are was not measured) |
| through Hosting, sending `X-Forwarded-For: 192.0.2.1` | `<visitor>, <Hosting>`: Hosting **replaced** the header | the visitor | a Hosting address |
| straight to `run.app` | `<caller>` (1 entry, appended by Cloud Run) | the caller (fewer entries than hops: the left-most is used, and every entry present was appended by Cloud Run) | the caller |
| straight to `run.app`, sending `X-Forwarded-For: 192.0.2.1` | `192.0.2.1, <caller>`: Cloud Run **appended** | `192.0.2.1`, **chosen by the caller** (logged as such) | the caller |
| straight to `run.app`, sending `X-Forwarded-For: 192.0.2.1, 198.51.100.9` | `192.0.2.1, 198.51.100.9, <caller>` (3 entries) | `198.51.100.9`, **chosen by the caller** (logged as such) | the caller |

**1 hop or 2: what each gives up.** The env roots and this module default to 2; fhir-proxy's own
default stays 1, because the service alone, called at `run.app` with nothing in front, has one
hop (a deployment of the service without Hosting should keep 1).

| | 1 hop | 2 hops (the default) |
| --- | --- | --- |
| Key through Hosting (the page's only path) | a Hosting address, shared by visitors | the visitor; a header the visitor sends cannot move it (Hosting replaces it) |
| Key straight to `run.app` | the caller's own address | the caller's own address without a header; with one, a value the caller chose |
| Who evades the per-address limit | nobody | a caller of the `run.app` URL that sends its own header |
| Who can lock whom out | anyone, through the public page and knowing nothing: five wrong guesses fill one Hosting key, so failures spread over Hosting's addresses (about 5 × their number a minute; the number was not measured) keep every visitor at `429` | a `run.app` caller that knows a visitor's public address can keep that visitor at `429`, even with the right password, because the address's slot is reserved before the password is read; it ends within 60 s of the attacker stopping |
| What bounds guessing | 5 a minute per key (per instance) | for `run.app` callers with a header, only throughput, so the password's strength |

Two hops is chosen because the first column's lockout needs no knowledge and hits everyone, while
the second column's cost is carried by the password. That holds only for a strong password:
**store it with `scripts/secrets/set-reviewer-password.sh --generate`** (24 random letters and
digits). A password a person chose is only as strong as that choice; the script's 16-character
minimum is a length rule, not a randomness check. The decision between 1 and 2 is the deployer's;
the default can be overridden per environment with `trusted_proxy_hops`.

**The run.app URL is public in this design, and must be.** fhir-proxy has
`ingress = "INGRESS_TRAFFIC_ALL"` and `invoker_iam_disabled = true` (`fhir_proxy.tf`; tested by
`real_data.tftest.hcl` and `tests/static_checks.sh`): Hosting calls the default `run.app` URL as an
outside caller and does not authenticate to it (Firebase's guide has the service allow
unauthenticated invocations), so neither lock can be closed without breaking the rewrite
("Public access and ingress"). The URL (output `fhir_proxy_url`) is not a secret; treat it as
known.

**What a direct caller can do under 2 hops** (the last two rows of the first table; tested in
`services/fhir-proxy/tests/test_login.py`, "two hops ... run.app"):

- **Escape the per-address limit.** Writing a different address on each request gives each
  guess a fresh bucket (any IPv4 address, or one per IPv6 /64). Text in that position is no help
  to it: it falls into one shared `unparseable` bucket.
- **Fill a known visitor's bucket.** Five wrong guesses claiming a reviewer's public address
  make that reviewer's next attempts, through Hosting and with the right password, get `429`
  until 60 s after the attacker stops. It needs the reviewer's address, and it works per
  instance (the counts are in memory; there are up to two).
- Not lock everyone out: filling the address table (10,000 live entries) makes a new address
  count as at its limit, but its password is still checked, so a right password still gets in.

**What protects the login from it.** Not the total limit: by design (fhir-proxy README, "What
bounds guessing") once the total of 60 failures a window per instance is exceeded the password is
still checked, answering `200` for the right one and `429` for a wrong one, so the total does not
bound how many passwords a caller can try. Such a caller is bounded only by throughput: two
instances × eight concurrent requests, each a digest comparison (the rate this allows was not
measured). What protects the login is the password's strength: `--generate` stores about 143 bits
(62^24), which no such rate exhausts; even 10,000 guesses a second would take about 3 × 10^25
years to try one millionth of them. The costs of a flood are Cloud Run usage (capped by
`max_instance_count = 2`) and log volume (the proxy's log sampler writes the first event of at
most 100 addresses per window, plus the failures below the total).

**What could close it, and why none is done here.** None is both cheap and clearly right, so
this is a **known limit**:

| Option | Gains | Costs, or why not now |
| --- | --- | --- |
| `TRUSTED_PROXY_CIDRS` (fhir-proxy README, "Possible follow-up"): trust the second entry only when the right-most one is a Hosting address, and otherwise key on the right-most | Direct callers could no longer choose their key; Hosting visitors keep their own buckets | Needs a range list that holds Hosting's addresses and not addresses any Google Cloud customer can send from. The candidate worth testing: Google's published service ranges minus the ranges Google Cloud customers use (`goog.json` minus `cloud.json`), falling back to the right-most entry on a miss. Unverified: whether Hosting's addresses are in that set was not checked. A possible follow-up |
| Count the second and the right-most entry, with the same limit, both checked before the password | Holds direct callers to 5 a minute | Through Hosting the right-most is a shared Hosting address: the everyone-lockout of 1 hop comes back |
| The same with a higher limit on the right-most entry | Holds one direct attacker to that higher rate | A different trade-off: a distributed attack through Hosting's shared addresses could again lock everyone out, at a higher cost to the attacker |
| A secret header that only Hosting adds | It would mark Hosting's requests | Hosting's configuration (`firebase.json` `headers`) sets response headers; it documents no way to add a header to the forwarded request (not tried) |
| Recognise Hosting by a header it adds | | A direct caller can send the same header; Cloud Run passes request headers through |
| Turn off the default `run.app` URL, or ingress "internal and Cloud Load Balancing" | Closes the direct path | Breaks the rewrite; the second needs an external Application Load Balancer in front (billed hourly, against D4), which with Cloud Armor would also close this |
| Hop count 1 | Direct callers cannot choose their key: no targeted lockout, and the per-address limit holds for them | Visitors through Hosting share keys, and anyone can lock every visitor out (the comparison table above) |

## Who may invoke what

| Caller | Role | On | Where |
| --- | --- | --- | --- |
| anyone (through the Hosting rewrite) | none needed: invoker check disabled | fhir-proxy | this module |
| CI's page deployer (optional) | `roles/run.viewer` (read the configuration; **not** invoke) | fhir-proxy | this module (`page_deployer_service_account_email`) |
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
For services Google says "At steady state, Cloud Run uses 2 times (2X) as many IP addresses as
the number of container instances", that it "reserves IP addresses in blocks of 16 (28 subnet
mask) at a time", and that the subnet must be /26 or larger
(docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc). Here at most 3 instances run (2 +
1), so a few /28 blocks of project-base's /24. Also from that page, for T25's destroy: "To delete
the subnet, you must first delete or redeploy your Cloud Run services ... and then wait 1-2
hours."

**Cold starts with Direct VPC egress.** The same page: "You might experience connection
establishment delays of a minute or more on instance startup when using Direct VPC egress. We
recommend configuring a HTTP startup probe that tests a connection to an egress destination used
by the application before the application accepts requests." Neither service has a route that
tests an egress destination without revealing anything (the proxy's routes all need a token;
cuff-sim's `/cuff/samples` is offline), so their probes do not wait for the network. On a cold
instance the first `/api/status` or the first MLLP send could then run into that delay, and a
delay over about 5 s on a worst-case cuff send ends in Hosting's bare 504 (next section). It is
measured on the first deploy (checklist, step 9). If it bites, the fix is a follow-up in the
services: a startup route that opens one egress connection (the proxy to the Healthcare API,
cuff-sim a TCP connect to the VM's MLLP port) and answers only 200 or 503, used as the HTTP
startup probe.

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
| `TRUSTED_PROXY_HOPS` | no (1) | `trusted_proxy_hops` (default 2: measured behind Hosting; "The client address and the direct run.app URL") |
| `TOKEN_ISSUER` | no | `pulsebridge-fhir-proxy:<project_id>:<name_prefix>` (deployment-specific: salts the key derivation) |
| `REQUEST_BUDGET_SECONDS` | no (50) | `50` |
| `CUFF_FORWARD_TIMEOUT_SECONDS` | no (55) | `55` |
| `REQUEST_TIMEOUT_SECONDS` | no (120) | `70`, the same as the Cloud Run request timeout ("Timeouts end to end") |
| `SESSION_TTL_SECONDS`, `PASSWORD_CACHE_SECONDS`, `LOGIN_*`, `FHIR_TIMEOUT_SECONDS`, `SAMPLES_FORWARD_TIMEOUT_SECONDS`, `HEALTH_SYNC_TIMEOUT_SECONDS`, `STATUS_CACHE_SECONDS` | no | service defaults (8 h tokens, 30 s rotation delay, 5/60/60 login limits, ...) |
| `OPENEMR_SITE_ADDRESS`, `OPENEMR_TOKEN_URL`, `OPENEMR_FHIR_BASE_URL`, `OPENEMR_SCOPES`, `OPENEMR_CLIENT_FILE` | no (all or none; read by T15's share, services/fhir-proxy/README.md "Share to OpenEMR") | only when `openemr` is given; the file is `/secrets/openemr-client/value` |

**Secrets are files, never environment variables.** Each is a secret volume at version `latest`
in its own directory: `reviewer-password` → `/secrets/reviewer-password/value`, and with
`openemr` the client document → `/secrets/openemr-client/value`. Google: "When reading a
volume, Cloud Run always fetches the secret value from the Secret Manager to use the value with
the latest version", whereas "Environment variables are resolved at instance startup time"
(docs.cloud.google.com/run/docs/configuring/services/secrets). The proxy re-reads the file after
`PASSWORD_CACHE_SECONDS` (30 s), so `scripts/secrets/set-reviewer-password.sh` (T27) takes effect within 30 s with
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
| fhir-proxy's Cloud Run request timeout | **70 s** (= `REQUEST_TIMEOUT_SECONDS`; the proxy requires both budgets ≥ 10 s below it: 55 + 10 = 65) | this module |
| cuff-sim worst case, MLLP | `MLLP_TIMEOUT + 4 × T` = 5 + 4 × 6 = **29 s** | cuff-sim README, "Time limits"; T = `HTTPS_TIMEOUT_SECONDS` = 6 |
| cuff-sim worst case, HTTPS | `8 × T` = **48 s** (ingest and audit write, each doubled by a token refresh and 401 retry) | same |
| cuff-sim's Cloud Run request timeout | **60 s** | this module |

The relations, asserted by `tests/real_data.tftest.hcl` from the deployed values:
**29 s and 48 s < 55 s < 60 s (Hosting)**, **55 s < 60 s (cuff-sim's own timeout)**, and **50 s
and 55 s each + 10 s ≤ 70 s (the proxy's request timeout)**.

- **Hosting's 60 s includes the proxy's cold start.** Hosting starts counting when it forwards the
  request; a proxy instance that has to start first spends part of the 60 s before the proxy's
  own 55 s forward budget begins. A cold start longer than about 5 s on a worst-case cuff send
  therefore ends in Hosting's bare 504, not the proxy's explained one (Direct VPC egress can add
  to cold starts: "Network and egress"). The attempt is still recorded by cuff-sim.
- **Why 70 s and not the proxy's default 120 s.** Behind Hosting no caller waits longer than
  60 s, so a longer Cloud Run timeout only serves direct `run.app` callers, and it lets a slow
  client trickling a request body hold one of the 8 threads for that long. 70 s is the smallest
  round value the proxy accepts (each budget at least 10 s below it).

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
`fhir_proxy_image`, `cuff_sim_image`, `patient_id` and `patient_assigning_authority` (each ONE
root variable feeding both this module and module ingestion, with the `check` block in "Usage"),
the two patient names (real data only); optionally `name_prefix`,
`labels`, `hosting_site_id`, `public_origin` (T30), `extra_allowed_origins`,
`trusted_proxy_hops`, `manage_firebase_project`, `cuff_sim_max_instances`, `deletion_protection`,
`page_deployer_service_account_email` (bootstrap's output of that name, T41).

**Ordering.** Nothing here waits for `ingest_enabled`: a cuff reading before the setup step has
written the Patient (D11) creates an identifier-only Patient by conditional create, which the
setup's conditional update then completes. `depends_on = [module.project_base, module.identity]`
makes the APIs (Cloud Run, Firebase Hosting) and the secret accessor grants exist before the
services and the site are created. The proxy's revision waits for its invoker grant
on cuff-sim (inside this module). The `google-beta` provider must be configured in the env root.

**`terraform destroy`**:

- Both services go (`deletion_protection = false` by default).
- **The default Hosting site stays**, and so does its last deployed page, public at
  `https://<project>.web.app`, with every `/api` call failing (the provider skips deleting a
  `DEFAULT_SITE`; "Adding Firebase to the project"). Teardown runs
  `firebase hosting:disable --project <project-id>` if no page should stay live. Firebase stays on
  the project (it cannot be removed, and costs nothing).
- **The serverless subnet cannot be deleted at once**: "To delete the subnet, you must first
  delete or redeploy your Cloud Run services ... and then wait 1-2 hours" (Direct VPC egress
  page). A destroy of project-base right after this module's fails on the subnet; retry later.

### What the other modules provide for Hosting

The Firebase Hosting API and the deployer's hosting role come through the existing variables'
defaults, not through anything hardcoded here:

- project-base `enabled_apis` includes `firebase.googleapis.com` and
  `firebasehosting.googleapis.com`.
- bootstrap `deployer_roles` includes `roles/firebasehosting.admin` (and no longer the unused
  `roles/storage.admin`: the stack creates no bucket; state access is a bucket-scoped grant).
- bootstrap adds Firebase to the project (`add_firebase`, default `true`), so the deployer never
  needs `roles/firebase.admin`.

## The page deploy and the nightly destroy (built in T26)

- **The page is deployed after every apply** of an env root by CI: `firebase_json` is written to
  `web/firebase.json`, the page built, and `firebase deploy --only hosting --project <project>` run
  with the locked `firebase-tools` (infra/envs/README.md, "CI/CD"). A Terraform change to the
  rewrite or the headers reaches the site only with that deploy.
- **Identity**: the page deployer service account through Workload Identity Federation (no key in
  GitHub): `roles/firebasehosting.admin` and `roles/serviceusage.serviceUsageViewer` on the
  project (bootstrap), `roles/run.viewer` on fhir-proxy only (this module, T41). The Firebase CLI
  accepted the WIF (external-account) credentials in the first CI run on dev (checklist CI6).
- **The nightly destroy of dev runs twice**: at 03:17 UTC, when the serverless subnet and the VPC
  may remain ("wait 1-2 hours" after the services that used the subnet are gone), and a retry at
  06:17 UTC that must leave nothing.
- After the destroy, the default site still serves the last page (see "For T25"); the nightly job
  leaves it, knowing `/api` fails there.

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

1. **`TRUSTED_PROXY_HOPS`: confirm the default of 2.** Measured on dev on 17 November 2026
   (infra/envs/README.md, checklist P1: 2 hops; "The client address and the direct run.app URL"
   above), and now the default. On a new deployment, confirm it: through the Hosting domain,
   make two failed logins (`POST https://<site>.web.app/api/auth/login` with a wrong password),
   one plain and one with a made-up `X-Forwarded-For: 192.0.2.1`; then one plain failed login
   directly at the proxy's `run.app` URL, and one there with `X-Forwarded-For: 192.0.2.1`. Read
   the four `login-failed` log lines: `forwarded_for_entries` and `client_address`. Through
   Hosting both must show 2 entries and your own address, never `192.0.2.1`; directly at
   `run.app`, 1 entry plain, and with the header 2 entries and `192.0.2.1` as `client_address`
   (the known limit: a direct caller chooses its key under 2 hops).
   - Hosting gives 2 entries (as measured) and the made-up entry never becomes
     `client_address`: keep the default 2. Its known limit for direct `run.app` callers is in
     "The client address and the direct run.app URL".
   - Hosting gives 1 entry and `client_address` is your own address: set
     `trusted_proxy_hops = 1`.
   - `192.0.2.1` becomes `client_address` through Hosting: the second entry from the right is
     not the visitor, and no hop count makes the key trustworthy. Record it in P1 and treat the
     per-address limit as void.
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
   `scripts/secrets/set-reviewer-password.sh`, login works within about 30 s with no redeploy; after a second new
   version, the old token is refused (401) and the new password works.
7. **The CSP in the browser.** The browser tests ran the mock build (`connect-src 'none'`), so
   this is the first run of the real connections under the header. Open the page with the
   developer tools console: zero CSP violations while logging in, opening every tab, drawing the
   chart and sending a cuff reading.
   Check the response headers of `/` include the policy above and `frame-ancestors 'none'`, and
   that `/.vite/manifest.json` is 404.
8. **The 60 s limit.** A forced slow cuff send (for example with the VM's adapter stopped) gets
   the proxy's own 504 OperationOutcome within about 55 s, not Hosting's bare 504, and its
   AuditEvent appears in the messages log afterwards.
9. **Direct VPC egress cold starts.** Leave the dashboard idle for 20 minutes (instances scale to
   zero), then time the first `/api/status` (fhir-proxy's Healthcare and health-sync calls
   through the VPC) and, after another 20 idle minutes, the first MLLP cuff send (cuff-sim to the
   VM). Google warns of "connection establishment delays of a minute or more". A first call that
   takes more than a few seconds, or a bare Hosting 504 on a cuff send, means the services need
   an egress-testing startup probe (a follow-up; "Network and egress").
10. **Startup probes.** A revision whose configuration is bad never opens its port (`--preload`;
   `make images-smoke` proves it inside the container), so its TCP probe fails. Confirm once that
   Cloud Run then keeps the previous revision serving and the apply reports the failure: deploy
   a copy of the proxy by hand with `gcloud run deploy <name>-probe-check --no-traffic` and one
   required variable missing, watch it fail, delete it.

## Not verified

- What Firebase Hosting and `run.app` put in `X-Forwarded-For` was measured on dev on 17
  November 2026 (step 1; checklist P1): Hosting replaces a visitor's header, Cloud Run appends
  to a direct caller's. Neither is documented by Google, so either could change; step 1
  re-checks both.
- That Hosting forwards `Authorization` and the full `/api/...` path (documented only
  indirectly; steps 2 and 3).
- That a request through Hosting reaches a service whose invoker check is disabled with no
  further setting (Firebase's guide uses "allow unauthenticated invocations", which in current
  `gcloud` is the same switch or an `allUsers` binding).
- That the code-managed grant (`page_deployer_viewer`) replaces the hand-made one on dev without
  a gap: the first CI run (18 November 2026) passed with the same role granted by hand; the
  plan and apply with `page_deployer_service_account_email` set have not run yet. (Measured in
  that run: the Firebase CLI accepts WIF credentials; `roles/firebasehosting.admin` alone is
  NOT enough, see "Deploying the page".)
- That creating the default site (`site_id = project_id`) through the API works on a project
  where it does not exist yet (adopting an existing one is the provider's documented code path).
- Direct VPC egress connection delays on cold starts ("a minute or more" possible): neither
  service's startup probe tests an egress destination (step 9).
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
| `real_data.tftest.hcl` | only fhir-proxy public (invoker check off, ingress all); cuff-sim internal with the check on; the invoker bindings are exactly fhir-proxy on cuff-sim; no public member; no page deployer grant by default, and with `page_deployer_service_account_email` exactly `roles/run.viewer` on fhir-proxy for that account, invokers unchanged; both scale to zero; max 2 and 1; concurrency 8 and 4; the timeout relations read from the deployed values (proxy timeout = `REQUEST_TIMEOUT_SECONDS` ≤ 70, budgets < 60, each budget + 10 ≤ the request timeout, forward < cuff-sim's timeout, cuff-sim's worst cases < forward) and the documented numbers; Direct VPC egress settings of both; images, accounts, probe types and limits; `deletion_protection`; the reviewer password as a volume at `latest`, its mount, no secret in any environment variable; the exact environment of both services; custom origin, dev origin, the hop count (default 2, an override of 1); OpenEMR settings and mount when given and absent by default; the site, `firebase_json`'s rewrite, ignore list and headers; the CSP has no unsafe source; `manage_firebase_project` |
| `synthetic.tftest.hcl` | the synthetic name by default and given explicitly, for both services; no `HEALTH_SYNC_URL`; the rewrite's region |
| `validation.tftest.hcl` | tag, tag-plus-digest and short-digest images refused; `http`, path and synthetic-mode health-sync URLs; public origin with a path or `http`; wildcard, trailing-slash and hostless (`https://.`, `https://`, `https://-`, `https://.example.org`) origins; an `http` OpenEMR URL; hop counts −1, 6 and 1.5 refused and 5 allowed; a real name (family or given) with synthetic data; missing names without; `SYN-` rules both ways; a backslash in a name; a long assigning authority; audit store equal to the FHIR store; missing account or secret keys; a host name as `mllp_host`; a region Hosting cannot rewrite to; a subnet path; too many cuff-sim instances; a page deployer that is a user email or carries the `serviceAccount:` prefix (a service account email is accepted) |
| `static_checks.sh` | no public principal; `invoker_iam_disabled = true` and `INGRESS_TRAFFIC_ALL` exactly once, on fhir-proxy; every `min_instance_count` 0 and no manual scaling; no authoritative or project IAM; no service account key; the two reviewed IAM member resources (`invoker` with members from `var.service_account_emails`; `page_deployer_viewer`, the only use of `var.page_deployer_service_account_email`, `roles/run.viewer` on fhir-proxy); no secret in an environment variable and every secret version `latest`; no hardcoded project, service account, registry path, `https://<host>` (outside variable examples), site or service ID; no child module; no Hosting version, release or channel; fhir-proxy's `depends_on` on the invoker grant |
| `tests/tooling/test_phr_service_config.py` (run by `make test`) | the exact environment maps `real_data.tftest.hcl` asserts, read from that file and parsed by the services' REAL `fhir_proxy.config` and `cuff_sim.config`: each starts, every key is one the service declares (so a misspelled optional variable is not silently ignored), the parsed values are the intended ones, and both agree on the patient and stores |
| `check_hosting_csp.py` | the header CSP equals `web/vite.config.ts`'s same-origin policy plus `frame-ancestors 'none'`, agrees with `web/scripts/check-bundle.mjs`'s required directives, has one CSP header only, allows no `unsafe-*`, wildcard or scheme source, and requires every other security header with its documented value |

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
| `trusted_proxy_hops` | `number` | `2` | `TRUSTED_PROXY_HOPS`, 0 to 5; 2 measured behind Firebase Hosting, 1 with nothing in front of `run.app`. |
| `page_deployer_service_account_email` | `string` | `null` | CI's page deployer; set: `roles/run.viewer` on fhir-proxy for it (`firebase deploy` checks `run.services.get` on the rewrite's target). |
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
