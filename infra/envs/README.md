# Environment roots: `dev` and `prod`

This directory is where PulseBridge is deployed from (task T25). It holds two environment
**roots** and the one **composition** both of them call:

```
infra/envs/
  _stack/      the composition: wires the seven modules (identity, project-base, healthcare,
               ingestion, edge-vm, openemr, phr) exactly as each module's README says
  dev/         a root: the quick start (synthetic data by default); destroyed nightly by CI (T26)
  prod/        a root: real Google Health data; the public demo; never destroyed by CI
  ci/          tf_ci.py: the Terraform steps the CI/CD workflows run (T26; see "CI/CD")
  README.md    this runbook (T28 carries it into docs/setup.md)
```

This README is also the **runbook**: the ordered procedure that takes an empty Google Cloud
project to a working stack, who runs each step, and how to tell it worked. T25 built it offline;
on **16 November 2026 it was run once, steps 1 to 11, on a new dev project with synthetic data**
(no real watch data, no prod). That run reached a working stack: the edge VM's first boot, the
image build, the full apply, the first seeding, ingest on, the page, the reviewer login, cuff
readings over MLLP and HTTPS, and shares to OpenEMR. Its corrections are in the steps below (T36).
The [master checklist](#master-checklist) at the end collects every assumption the modules could
not verify, in the order a first deployment meets them; its last column records what that run
confirmed, and the items it did not reach are still open (listed at the top of the checklist).

New to the project? Start with the walk-through in [docs/setup.md](../../docs/setup.md), which
follows this runbook step by step with more explanation. Where the two differ, this runbook is
authoritative.

## Terms used here

- **Root module (root)**: the directory you run `terraform init/plan/apply` in. It configures
  the **providers** (the plugins that talk to Google's APIs) and the **backend** (where the
  state is kept), and pins their versions. `dev` and `prod` are roots.
- **Child module**: a directory of Terraform code called from another with a `module` block.
  It gets its providers from the caller and has no backend. `_stack` and the seven modules under
  `infra/modules/` are child modules.
- **State**: Terraform's record of what it created (IDs, attributes). It is how a later `apply`
  knows what to change. It can contain identifiers and personal data (the patient's name in a
  real deployment), so treat it as sensitive; it never contains a secret *value* here, because
  secret versions are added outside Terraform (or, in dev, write-only).
- **Remote state / backend**: the state kept in a shared GCS bucket instead of a file on one
  laptop, so CI and people see the same state. The GCS backend also **locks** it, so two applies
  cannot run at once. Bootstrap creates the bucket; each root uses its own **prefix** (`dev`,
  `prod`) inside its project's bucket.
- **Partial backend configuration**: `backend.tf` holds the backend block without the bucket
  name (which names your project); the bucket is given at `terraform init -backend-config=...`
  from a git-ignored file. That is why `backend.tf` is safe to commit.
- **Lock file** (`.terraform.lock.hcl`): records the exact provider versions and their checksums
  for each platform, so every run downloads the same provider bytes. Committed for roots only.
- **tfvars**: `terraform.tfvars` holds a root's input values. It names your project and, in
  prod, you, so it is git-ignored; `terraform.tfvars.example` is the committed template.
- **`check` block**: a condition Terraform evaluates on every plan and apply; when it is false
  Terraform prints a **warning** and carries on (a variable `validation` instead stops the run).
- **`-target`**: plan or apply only the named resources or modules (and what they depend on).
  Used here twice, deliberately, before the container images exist (steps 3, 4 and 6).
- **`-replace`**: destroy and re-create one resource on the next apply (used to patch the VM).
- **Write-only argument / ephemeral variable**: a value Terraform sends to the API but never
  saves in the plan or state. Until T44 the dev reviewer password used one; since D16 no secret
  value passes through Terraform at all (scripts add every version).
- **`terraform state rm`**: makes Terraform forget a resource without touching it in Google
  Cloud. Used once, in "Rolling out the public demo", for a protected secret that must leave the
  configuration.
- **The public demo (D16)**: anyone can open the dashboard, with no password. A visitor selects
  "View the live demo"; an invisible **bot check** runs: Google's **reCAPTCHA** script (part of
  **Google Cloud Fraud Defense**, formerly reCAPTCHA Enterprise) gives the page a one-time token,
  and fhir-proxy asks Google to **assess** it, getting a score from 0.0 (likely a bot) to 1.0
  (likely a person). On a pass the proxy sets a signed **session cookie**, `__session`, for 24
  hours: the only cookie Firebase Hosting passes on to Cloud Run. Each assessment is counted in
  a small **counter bucket** so the daily cap holds across instances and restarts. Module phr's
  README, "The public demo (D16)", has the detail.
- **Rate limit**: how many requests of a kind one session, one client address or everyone
  together may make in a time window; above it the proxy answers `429` with `Retry-After`. The
  proxy keeps them in memory, **per instance**, so with its two instances each real limit is up
  to twice the number ("The public demo's settings" below).

## Why one composition (`_stack`) and two thin roots

The brief asks for two environment roots that share the modules. The two options were:

| Option | For | Against |
| --- | --- | --- |
| **(a) one composition, thin roots** (chosen) | The seven module calls exist once, so dev and prod cannot be wired differently. A test proves the roots differ only in `env.tf` and `backend.tf`. | One more directory to read. |
| (b) two full roots | Each root reads top to bottom on its own. | About 250 lines of wiring twice; every module change must be made twice and the two copies drift. |

Terraform's provider rules do not get in the way of (a). A child module may not configure
providers when it is called with `count`/`for_each`, and needs `configuration_aliases` only for
**aliased** providers. Here every module uses the **default** `google` configuration, and modules
phr and identity also use the default `google-beta` one, so the roots configure both providers once and
Terraform passes them down through `_stack` to every module automatically. No alias, no
`providers = {...}` map.

Inside each root:

| File | Identical in dev and prod? | What it holds |
| --- | --- | --- |
| `versions.tf` | yes | Terraform `>= 1.11`, `google` and `google-beta` `~> 8.11` (the modules' constraint) |
| `providers.tf` | yes | both providers with `project`, `region`, `zone` set explicitly; `google-beta` with `user_project_override = true` and `billing_project = var.project_id`; optional impersonation of the deployer |
| `variables.tf` | yes | every input, with descriptions and validations |
| `main.tf` | yes | the one `module "stack"` call |
| `checks.tf` | yes | the patient check |
| `outputs.tf` | yes | the stack's outputs |
| `.terraform.lock.hcl` | yes | google and google-beta 8.11.0 for linux, darwin (amd64, arm64) and windows amd64, the same hashes as bootstrap's |
| `env.tf` | **no** | the per-environment settings below |
| `backend.tf` | **no** | `backend "gcs" { prefix = "<env>" }` |
| `terraform.tfvars.example`, `tests/` | no | the template and the tests for that environment |

## dev and prod: what differs, and why

Everything else is the same code with the same defaults. `_stack/tests/check_env_roots.py`
fails if any other file differs or if `env.tf` holds anything else.

| Setting (`env.tf`) | dev | prod | Why |
| --- | --- | --- | --- |
| `environment` | `"dev"` | `"prod"` | The `environment` label on every resource, and the outputs. |
| `use_synthetic_data` (variable default) | `true` | `false` | dev is the quick start: a stranger can run it without a Pixel Watch or a Google Health OAuth app (the seeder fills the store). prod shows the real data. Either can be overridden in tfvars. |
| `protect_from_destroy` | `false` | `true` | prod holds real health data and is the public demo: the Healthcare dataset and stores get `deletion_policy = "PREVENT"`, the secrets `deletion_protection`, and the Cloud Run services and job `deletion_protection`, so a stray `terraform destroy` fails; and the edge VM's data disk (the OpenEMR database) gets a daily snapshot kept 7 days. dev is destroyed nightly and keeps no snapshots. |
| `backend.tf` prefix | `dev` | `prod` | One state per environment. Fixed in the committed file (not author-specific), so a `backend.hcl` copied from the other environment cannot point one root at the other's state. |

Deliberately **not** different:

- **Cost settings.** Both use the e2-micro, `pd-standard` disks inside the free tier, no Cloud
  NAT, scale-to-zero Cloud Run. There is no cheaper dev setting to offer. The one cost prod adds
  is the snapshots that come with `protect_from_destroy` (cents a month).
- **The project.** dev and prod are separate projects (D1), each bootstrapped once, so the
  same resource names do not collide.
- **The free tier is per billing account, not per project.** Two deployments on one billing
  account run two e2-micro VMs, and only one is free: the second is billed (about $6 a month,
  edge-vm README). Destroy dev when you are not using it, or link it to another billing account.

## What a stranger sets

`terraform.tfvars` (from the `.example`) needs, at the least:

| Variable | Where the value comes from |
| --- | --- |
| `project_id` | bootstrap's `project_id` output |
| `patient_id` | you choose it: `SYN-0001` style with synthetic data; anything **not** starting with `SYN-` with real data |
| `patient_assigning_authority` | you choose it: any short code, 1-20 characters (PID-3.4 of the cuff messages) |
| `patient_family_name`, `patient_given_name` | real data only: your name (refused with synthetic data) |
| `time_zone` | your IANA zone, for example `America/New_York` |
| `alert_email` (or `alert_notification_channels`) | where alerts go; one is required |
| the five `*_image` variables | the image build's `BEGIN tfvars` block (step 7), pasted unchanged |
| `ingest_enabled` | `false` for the first apply, `true` for the second (step 10) |

Optional: `deployer_service_account_email` (impersonate the deployer), `region`/`zone` (default
`us-central1`/`us-central1-a`: inside the e2-micro free tier and a region Firebase Hosting can
rewrite to; a `check` warns outside the free tier, module phr refuses a region Hosting cannot
reach), `enable_iap_ssh` (for the first-boot checklist), `enable_cloud_nat` (billed hourly;
fallback only), `trusted_proxy_hops` (default 2, measured behind Firebase Hosting; step 12),
`openemr_scopes` (step 6), `public_origin` (a custom domain; T30, deferred), `name_prefix` /
`identity_name_prefix`, the two ingress fallbacks, and the public demo's settings below.

Rules the root enforces before anything is created: a `SYN-` patient ID is **required** with
synthetic data and **refused** without it; a real name is refused with synthetic data (the cuff
simulator always sends the seeder's synthetic name, Synthetic / Demo, so the seeder accepts a
Patient the cuff path created first) and required without it; an alert destination is
required; the zone must be in the region; images must be pinned by digest. (Until T44 prod
also refused a reviewer password from tfvars; D16 removed the password and that switch,
`allow_reviewer_password_from_tfvars`: both environments now set the session signing key with
the same script.)

**One patient for everything.** `patient_id` and `patient_assigning_authority` are one variable
each. `_stack` passes the same values to module ingestion (v2-to-fhir, health-sync or the
seeder) and module phr (cuff-sim, fhir-proxy), and the `setup_env` output hands `PATIENT_ID` to
the connect script. `checks.tf` compares what each module received; `_stack/tests/plan_scan.py`
compares the planned `PATIENT_ID` and `PATIENT_ASSIGNING_AUTHORITY` of every service.

### The public demo's settings

All optional: leave them unset (`null`) and module phr's defaults apply, which are
fhir-proxy's own (`services/fhir-proxy/fhir_proxy/config.py`). `terraform output demo_settings`
shows what the proxy gets.

| Variable | Default | Range | What it does |
| --- | --- | --- | --- |
| `recaptcha_min_score` | 0.5 | above 0, at most 1 | the lowest bot-check score that gets a session; tune it from the scores real visitors get (checklist P19) |
| `bot_checks_per_day` | 300 | 1 to 3000 | assessments per UTC day, across all instances (the counter bucket); past it the page says "The demo is busy, try again tomorrow." |
| `session_ttl_seconds` | 86400 | 300 to 86400 | how long a session lasts (24 hours) |
| `sessions_per_address_per_hour` | 5 | 1 to 100 | bot-check attempts per client address an hour, checked before any assessment is made |
| `requests_per_address_per_minute` | 300 | 60 to 6000 | requests without a valid session per client address a minute |
| `reads_per_session_per_minute` | 120 | 60 to 1200 | a session's requests other than cuff sends and shares (every read, logout) a minute; the dashboard's busiest measured minute is 37 |
| `cuff_sends_per_session_per_10_minutes` | 3 | 1 to 30 | cuff readings per session in any 10 minutes |
| `shares_per_session_per_10_minutes` | 2 | 1 to 20 | shares to OpenEMR per session in any 10 minutes |
| `shares_per_instance_per_hour` | 30 | 1 to 600 | shares by everyone together in any hour |
| `history_days` | 7 | 1 to 30 | how many days back the demo shows; older data is hidden, never deleted |

**Every rate limit is per fhir-proxy instance.** The proxy keeps its counts in memory (no shared
store, so nothing is billed while the demo is idle), and Cloud Run runs up to two instances, so
each real limit is up to twice its number: 10 bot-check attempts an hour per address, 6 cuff
sends and 4 shares in 10 minutes per session, 60 shares an hour for everyone. Only
`bot_checks_per_day` is exact: it is counted in the bucket both instances share.

`history_days` and `session_ttl_seconds` are stated on the page's privacy note
(`web/privacy.html`: "the last 7 days", "24 hours"); if you change them, change the note too
(`infra/modules/phr/tests/check_proxy_settings.py` fails until you do).

## What a plan shows

A plan (and the plan CI posts on a pull request, see "CI/CD") prints every planned value that is
not marked **sensitive**. `patient_id`, `patient_family_name`, `patient_given_name` and
`alert_email` are sensitive variables in both roots and in `_stack`, so plans show `(sensitive
value)` for them, for every Cloud Run environment block that carries the patient, and for the
alert channel's address label (`labels = { "email_address" = (sensitive value) }`).
`_stack/tests/plan_scan.py` checks a human-readable plan for the synthetic values (the channel's
display name and its redacted label are the positive controls); `check_env_roots.py` checks the
declarations; and in CI, `ci/tf_ci.py` checks every plan for the actual sensitive tfvars values
before anything is posted (a match fails the job and posts nothing).

How the alert address became sensitive (T26): module ingestion used to decide with `alert_email !=
null` how many alert channels exist, and Terraform refuses a sensitive value (or anything computed
from one) in `count` or `for_each`. The module now has a separate bool,
`create_alert_email_channel`, and `_stack` passes it `nonsensitive(var.alert_email != null)`:
whether an address is set reveals nothing about the address.

What a plan still shows, by design: the project ID, region, resource names, service account
emails, Cloud Run URLs, image digests and the OpenEMR VM's internal address. On a **public**
repository a plan comment is public too. None of it is a secret, but it does tell readers which
project the demo runs in. Sensitive values are still **stored in the state in clear**; treat the
state bucket as personal data. The patient's name, gender and birth date that the connect script
takes are never in Terraform at all.

## Outputs

`terraform output` lists them; `terraform output -raw <name>` prints one for a script. None is a
secret value.

| Output | For |
| --- | --- |
| `next_steps` | the ordered post-apply procedure with this deployment's names filled in |
| `dashboard_url`, `hosting_site_id` | the page; `firebase deploy --project` |
| `firebase_json` | step 11: written to `web/firebase.json` before the page deploy (T26) |
| `fhir_proxy_url` | step 12's hop measurement only (never for the page) |
| `healthcare` | project, location, dataset, FHIR store, audit store, HL7v2 store (short IDs), FHIR base URL, ingest topic and subscriptions (replay commands) |
| `setup_env` (sensitive) | `export` lines for the connect script: `GCP_PROJECT_ID`, `HEALTHCARE_LOCATION`, `HEALTHCARE_DATASET_ID`, `FHIR_STORE_ID`, `PATIENT_ID`, `PULSEBRIDGE_NAME_PREFIX`. Sensitive because it carries the patient ID (see "What a plan shows"). Your name, gender and birth date are never in it |
| `secret_name_prefix`, `secret_ids` | secret names (never values) for the secret scripts (scripts/secrets/) and the connect script |
| `seed_job_name`, `region` | step 9's first seeding |
| `health_sync_service_name`, `v2_to_fhir_service_name` | logs and checks |
| `ingest_enabled`, `push_enabled`, `scheduler_job_names` | where the ingest switch stands |
| `alert_policy_names` | step 13 |
| `edge_vm` | name, zone, internal IP, MLLP port, boot image, data disk, hook metadata keys (step 6, patching) |
| `openemr` | OpenEMR's internal site, FHIR and token URLs, registered scopes, image |
| `image_variable_names` | the tfvars names the build prints |

## The runbook

Each step says **who** runs it with **which role**, and **how to tell it worked**. "Owner" is a
person with the Owner role on the project. "Deployer" is bootstrap's `terraform-deployer` service
account: CI uses it through Workload Identity Federation; a person uses it by setting
`deployer_service_account_email` (which needs `roles/iam.serviceAccountTokenCreator` on it) or
by holding the same roles. Run Terraform commands from `infra/envs/<env>`. An automated session
whose credentials are a service account key from **another** project cannot use
`deployer_service_account_email`; see [Running the env roots from a service account in another
project](#running-the-env-roots-from-a-service-account-in-another-project).

**The deployer cannot read logs** (bootstrap README, "Deployer roles"). Where a "worked when"
below reads a log line (steps 9 and 13, the front-door checks of step 12), run that check as
the owner or with `roles/logging.viewer`. The VM's serial port output (step 6) needs no log role.

### 1. Bootstrap the project (once per project; owner)

`infra/bootstrap/README.md`. Creates or adopts the project, the state bucket, the deployer and
Workload Identity Federation, and adds Firebase (`add_firebase = true`, the default; Hosting
needs it and the deployer cannot do it). dev and prod are separate projects: bootstrap each.

Worked when: `terraform -chdir=infra/bootstrap output` shows `state_bucket_name`,
`deployer_service_account_email` and `firebase_added = true`.

### 2. Configure the backend and initialise (deployer or owner)

```bash
cd infra/envs/dev                                  # or prod
cp backend.hcl.example backend.hcl                 # git-ignored; set bucket = <state_bucket_name>
cp terraform.tfvars.example terraform.tfvars       # git-ignored; fill in (images come in step 7)
terraform init -backend-config=backend.hcl
```

Worked when: init says "Successfully configured the backend "gcs"" and the lock file is
unchanged (`git status` shows no change to `.terraform.lock.hcl`).

### 3. First targeted apply: APIs, network, registry (deployer)

```bash
terraform apply -target=module.stack.module.project_base
```

The images (step 7) are pushed to project-base's Artifact Registry repository, so it must exist
first. It also creates module identity's service accounts (not its secrets): project-base's
mirror grant and firewall rule name the edge VM's account, and Terraform pulls in what a
targeted module depends on. The image variables may stay unset: nothing targeted depends on them.
Terraform warns that targeting is in effect; that is expected here.

Worked when: the apply ends without error and `gcloud artifacts repositories list --project
<project>` shows `pulsebridge-services` and `pulsebridge-dockerhub`. On a brand-new project an
API can take a minute after enabling: re-run on "API has not been used ... or it is disabled".
If the project number in that message is **not** your project's, the call was billed to the
caller's own project: see "Running the env roots from a service account in another project"
(16 November: provider impersonation failed here with `impersonate: status code 403: IAM Service
Account Credentials API has not been used in project <the caller's home project>`; with a minted
token the same step created 34 resources).

### 4. Second targeted apply: identities and empty secrets (deployer)

```bash
terraform apply -target=module.stack.module.identity
```

Separate from step 3 because the secrets need the Secret Manager API that step 3 enabled
(module identity does not depend on project-base). It also grants fhir-proxy
`roles/recaptchaenterprise.agent` on the project (D16: creating bot-check assessments).

This step also creates the **Pub/Sub service agent** (`google_project_service_identity.pubsub`,
T36) before granting it Token Creator on the push account. On 16 November, before that resource
existed, this step failed on a new project with `Error 400: Service account
service-<number>@gcp-sa-pubsub.iam.gserviceaccount.com does not exist` and the agent had to be
created by hand (module identity README, "The Pub/Sub service agent must exist first"). Creating
it here also puts it in place before module healthcare grants it roles (steps 6 and 8). If the
deployer is refused on that resource (its permission is not verified, A22), run this step once as
the owner. By hand, if ever needed, use the REST call rather than `gcloud beta services identity
create`, which on 16 November **hung** in a non-interactive shell waiting to install the `beta`
component (install it first with `gcloud components install beta`, where the installation allows
it, or set `CLOUDSDK_CORE_DISABLE_PROMPTS=1` so it fails instead of waiting):

```bash
# As the owner (or any account allowed to). No output but the operation; nothing secret printed.
curl -sS -X POST -H @<(printf 'Authorization: Bearer %s\n' "$(gcloud auth print-access-token)") \
  -H "x-goog-user-project: <project-id>" \
  "https://serviceusage.googleapis.com/v1beta1/projects/<project-id>/services/pubsub.googleapis.com:generateServiceIdentity"
```

(`-H @<(...)` hands curl the header through a file descriptor, so the token is not in the process
list.)

Worked when: `gcloud secrets list --project <project>` shows eight `<prefix>-...` secrets and
the apply ends without error (it created `google_service_account_iam_member.pubsub_token_creator`).

### 5. Secret versions (deployer; scripts/secrets)

Add a version to the three edge-VM passwords before the VM's first boot (step 6), and to the
**session signing key** before step 8 (D16, both environments): Cloud Run refuses to create
fhir-proxy while a secret it mounts has no version (checklist A8 and A19), so step 8 stops at
fhir-proxy without it. The scripts read the
value from a hidden prompt (typed twice) or from standard input, or generate it; store it without
a trailing newline; and refuse the secrets that something else writes
(`<prefix>-openemr-client-private-key`: the VM; the three Google Health secrets: the connect
script, step 9). Details: scripts/secrets/README.md.

From the repository root:

```bash
export CLOUDSDK_CORE_PROJECT=<project-id>
export SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/<env> output -raw secret_name_prefix)"
# or pass --project <project-id> --prefix <identity_name_prefix> to each command ("pb" by default)
scripts/secrets/set-secret.sh --generate db-root-password
scripts/secrets/set-secret.sh --generate openemr-db-password
scripts/secrets/set-secret.sh openemr-admin-password      # typed twice; or --generate (not shown)
scripts/secrets/set-session-signing-key.sh                 # 32 random bytes; never shown
```

Typed edge-VM passwords: 16 to 128 letters, digits and `!#$%&()*+,-./:;<>?@[]^_{|}~`, not
starting with `-` (OpenEMR's first-time setup splits on spaces, cuts at `=`, and writes the
database password unescaped into `sqlconf.php`). The session signing key is not typed: the script
pipes 32 bytes from `/dev/urandom` straight into `gcloud`, so the value is in no variable, file or
terminal. A generated admin password is never shown; it is the only OpenEMR admin login, and
root on the VM can read it from `/mnt/disks/edge/secrets/openemr-admin-password`
(scripts/secrets/README.md, "Reading a generated admin password back").

Worked when: `gcloud secrets versions list <secret> --project <project-id>` shows version 1
`enabled` for each, and
`gcloud secrets versions access latest --secret <secret> --project <project-id> | wc -c`
prints the value's length (no extra byte for a newline; 32 for the session signing key).

### 6. Targeted apply of the edge VM, and its first boot (deployer, with a shell on the VM)

```bash
terraform apply -target=module.stack.module.edge_vm
```

This creates the VM before anything that serves the public, and needs no images. It pulls in
what the VM depends on: the Healthcare dataset and HL7v2 store (the adapter's flags), the
secrets it reads, project-base and identity. **Why this order:** fhir-proxy mounts the
`<prefix>-openemr-client-private-key` secret at version `latest`, and that secret's first version
is written by the VM itself when it registers fhir-proxy's OAuth client on first boot. Booting
the VM first means the version exists before the apply that creates fhir-proxy (step 8). The
alternative, one full apply and "apply again if fhir-proxy stops on that secret", leaves a failed
apply as the normal path. The first-boot checklist also runs here before any public service
exists. On 16 November this order worked: the VM stored version 1, and step 8 created fhir-proxy
without error. Whether Cloud Run refuses to deploy a service whose volume secret has **no**
version yet is still **not verified** (Cloud Run documents no check at instance start-up for
volume secrets; the deploy-time check is undocumented); this order avoids depending on the answer.

First boot takes about 20 minutes: on 16 November the startup script ran from 12:04 to 12:24 UTC,
of which the OpenEMR image pull through the mirror took about 12.5 minutes and OpenEMR's
first-time setup about 5 (both inside the 40-minute hook limit). Work through
**edge-vm's checklist first, then openemr's**: each step depends on the ones before. To get a
shell, set `enable_iap_ssh = true` and run this step's apply again, and give the person
`roles/iap.tunnelResourceAccessor`, `roles/compute.osAdminLogin` and `roles/iam.serviceAccountUser`
on the VM's account (edge-vm README, "Looking at the VM without public access"); set it back to
`false` afterwards.

```bash
VM=<name_prefix>-edge-vm          # pulsebridge-edge-vm with the default name_prefix
ZONE=<zone>                       # us-central1-a by default
gcloud compute instances get-serial-port-output "$VM" --zone "$ZONE" --project <project>
gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --project <project>
# on the VM:
sudo journalctl -u google-startup-scripts.service
sudo docker ps
sudo docker logs mllp-adapter
sudo docker logs openemr
```

The ordered checks are items E1 to E19 (edge-vm) and O1 to O21 (openemr) of the
[master checklist](#master-checklist), except those that need the full apply: E8's `AA` test
(the VM's `hl7V2Ingest` grant on the store is created in step 8) and the items that need
fhir-proxy (O13's token request, O15), which wait for step 12. If a secret version was missing at
boot, the log says `ERROR: secret ... 404`; add it and run `sudo google_metadata_script_runner
startup` on the VM. If the pulls fail (no route to Artifact Registry over Private Google Access),
set `enable_cloud_nat = true` (billed hourly) and apply again.

Worked when: the log shows `pulsebridge-edge: edge VM startup: done, all steps ok`, with `hook
openemr: ok` and `hook openemr-oauth-client: ok`, and `gcloud secrets versions list
<prefix>-openemr-client-private-key` shows one version (the VM registered fhir-proxy's client).
The serial port output is enough to follow it (`get-serial-port-output` above, no shell and no
log role needed); the 16 November first boot was checked that way.
If registration failed and you go on anyway, step 8 may stop at
`module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy` on that secret: finish this
step, then run step 8's apply again.

### 7. Build the images (the build trigger, or the owner by hand)

**With the trigger (T26, opt-in).** In bootstrap set `allow_build_trigger = true` (the deployer's
`roles/cloudbuild.builds.editor`) and apply it. In the env root set `create_build_service_account
= true` and, after linking GitHub to Cloud Build once in the console, `build_trigger_repository`
(project-base README, "Image builds"), and apply (a targeted apply of
`module.stack.module.project_base` and `module.stack.module.identity` is enough at this point).
Then push a release tag:

```bash
git tag v0.1.0 && git push origin v0.1.0     # starts the trigger in every project that has one
gcloud builds list --region <region> --project <project> --limit 1   # find the BUILD_ID
gcloud builds log <BUILD_ID> --region <region> --project <project> | sed -n '/BEGIN tfvars/,/END tfvars/{//!p}'
```

Each triggered build **waits for an approval** (`build_trigger_approval_required`, default
true): open Cloud Build, Build history, and approve it (needs `roles/cloudbuild.builds.approver`;
the project Owner has it). Without the approval, anyone who can push a `v*` tag could publish
images. Better still, add a GitHub tag ruleset (Settings, Rules, Rulesets, New tag ruleset,
target `v*`, restrict creations, updates and deletions to the maintainers); then the approval
can be turned off. The build runs as `<identity_name_prefix>-build`, which can push to the
services repository and pull through the mirror and do nothing else. A trigger is regional, so
`gcloud builds` needs `--region`. Nothing is deployed by the build: paste the block into the tfvars as below (for CI, into
the tfvars object in the state bucket, "CI/CD").

**By hand (the owner).**

```bash
gcloud builds submit --config cloudbuild.yaml --project <project> \
  --substitutions TAG_NAME=v0.1.0,COMMIT_SHA="$(git rev-parse HEAD)" .
gcloud builds log <BUILD_ID> --project <project> | sed -n '/BEGIN tfvars/,/END tfvars/{//!p}'
```

(services/README.md, "Build in Cloud Build".) The `sed` prints the lines between the markers,
without them. `gcloud builds log` prints the lines **without** a step prefix (seen on 16
November), so the block pastes as it is. Paste the block into `terraform.tfvars` unchanged: it
names all five images; the one your mode does not use is ignored. A manual `gcloud builds submit`
runs as Cloud Build's default account, which in a new project is the **Compute Engine default
service account** (`<project-number>-compute@developer.gserviceaccount.com`; seen on 16
November, and that account may hold project Editor), unless you add `--service-account
projects/<project>/serviceAccounts/$(terraform output -raw build_service_account_email)`; the
build account then also needs read access to the source bucket (not verified; services/README.md). Note that `gcloud builds submit` creates a source
bucket `gs://<project>_cloudbuild` that `terraform destroy` does not remove; the trigger reads
from GitHub and creates none.

Worked when: you have five `<name>_image = "...@sha256:<64 hex>"` lines.

### 8. First full apply, ingest off (deployer)

```bash
terraform apply            # ingest_enabled = false
terraform output -raw next_steps
```

Creates everything else: the Healthcare FHIR stores, v2-to-fhir, health-sync or the seeder job,
fhir-proxy, cuff-sim, the Hosting site, the alerts, the remaining grants. **No Scheduler job
exists and the HL7v2 subscription does not push** (`ingest_enabled = false`), so nothing writes
to the FHIR store by itself before the setup step (D11).

If it stops at `google_cloud_run_v2_service.health_sync` on a secret error (real data:
health-sync mounts the three Google Health secrets, which have no version until the connect
script runs; whether Cloud Run allows that at deploy is not verified), run step 9's connect
script now (it needs only the FHIR store and the secrets, which this apply has created), then
`terraform apply` again (ingestion README, "First run"). Now run E8's `AA` test.

**Seen on 18 November in prod: it does stop there** (A8), and the failed create leaves health-sync
in the state **tainted** with `deletion_protection` on, so the next apply refuses to replace it
("cannot destroy service without setting deletion_protection=false"), and `terraform untaint`
does not help (the failed service has no URL, which fhir-proxy's `health_sync_url` refuses). What
worked: after the connect script, delete the failed service, which never served, and drop it
from the state, then apply:

```bash
gcloud run services delete <name_prefix>-health-sync --region <region> --project <project> --quiet
terraform state rm 'module.stack.module.ingestion.google_cloud_run_v2_service.health_sync[0]'
terraform apply
```

The connect script cannot simply run before this step: the FHIR store it writes the Patient to
is created here, not in step 6 (step 6 creates the dataset and the HL7v2 store only). A targeted
`terraform apply -target=module.stack.module.healthcare` before the connect script, then this
step, should avoid the failed create; not tried.

Worked when: the apply ends without error, `terraform output scheduler_job_names` is `[]` and
`push_enabled` is `false`. (16 November: the first run stopped only at the two alert policies,
"must specify a restriction on resource.type in the filter"; fixed in module ingestion before
T36, after which the second run created them.)

### 9. The setup step: write the Patient (one of the two, never both)

**Real data (`use_synthetic_data = false`): connect Google Health.** You, with your own
credentials (`gcloud auth application-default login`), after registering your own Google Health
OAuth app (a Desktop-app client; yourself as a test user): scripts/connect-google-health/README.md.

```bash
terraform output -raw setup_env > ~/pulsebridge-prod.env    # OUTSIDE the repository
# add PATIENT_FAMILY_NAME, PATIENT_GIVEN_NAME, PATIENT_GENDER, PATIENT_BIRTH_DATE to that file
source ~/pulsebridge-prod.env && ../../../scripts/connect-google-health/connect.sh   # from infra/envs/prod
```

**Never run the connect script with a `SYN-` patient ID** (the root refuses `SYN-` without
synthetic data, so `setup_env` of a real deployment never carries one; a synthetic deployment's
`setup_env` says so in its second line, right under the header). Worked when: it ends `Done.` with "Patient record
created", and exit code 0.

**Synthetic data (`use_synthetic_data = true`): the first seeding.** Deployer (`roles/run.admin`
includes overriding the job's arguments):

```bash
gcloud run jobs execute "$(terraform output -raw seed_job_name)" \
  --region "$(terraform output -raw region)" --project <project> \
  --args=--days,30 --task-timeout=1800s --wait
```

Worked when: the execution succeeds and its `seed-run` log line has outcome `seeded`
(services/seed/README.md). Reading that line needs the owner or `roles/logging.viewer`; the
deployer sees only that the execution completed. If it was stopped, run it again: every write is
keyed. (16 November: outcome `seeded`, 8,365 windows in 84 bundles, `Patient 201` once and then
`200`, safety check `no-patient`.)

### 10. Second apply: ingest on (deployer)

Set `ingest_enabled = true` in `terraform.tfvars`, then `terraform apply`. One switch does both:
module ingestion creates the Scheduler job (health-sync every 15 minutes, or the seeder hourly)
and module healthcare turns the ingest subscription into a push subscription to v2-to-fhir.
HL7v2 notifications that arrived while it was off wait in the subscription (up to 7 days) and
are pushed now.

Worked when: `terraform output scheduler_job_names` lists one job and `push_enabled` is `true`.
(16 November: one resource added, the Scheduler job, and one changed, the subscription.)

### 11. Deploy the page (deployer, after every apply)

```bash
terraform -chdir=infra/envs/<env> output -raw firebase_json > web/firebase.json   # git-ignored
(cd web && npm ci && npm run build)          # VITE_FHIR_PROXY_URL unset: the page calls /api
(cd web && npx --yes firebase-tools@<pinned version> deploy --only hosting \
    --project "<project>" --non-interactive)
```

(phr README, "Deploying the page".) A Terraform change to the rewrite or the headers reaches the
site only with this deploy. Once CI has taken over an environment ("CI/CD"), every apply is
followed by a page deploy job that does this itself, with the `firebase-tools` locked in
`ci/deploy-tools/package-lock.json` (`ci/tf_ci.py deploy-page`).

Worked when: `dashboard_url` shows the landing view with "View the live demo" (before T45, the
login page). (16 November: the locked firebase-tools
15.34.2 from `ci/deploy-tools` deployed the page, but with an Owner's service account key, not
the deployer. 18 November, P12: `roles/firebasehosting.admin` alone is **not** enough; the CI
page deployer also needs `serviceusage.services.get` on the project and `run.services.get` on
fhir-proxy, bootstrap README, "The page deployer's three grants". The deployer has both through
its other roles.)

### 12. The front door's first-deploy checks (deployer)

Items P1 to P10 of the master checklist, **hop measurement first**, then the public demo's
P17 to P26 (a new deployment, or the first after "Rolling out the public demo"). `trusted_proxy_hops`
defaults to 2, the value measured behind Firebase Hosting on dev (P1); the measurement confirms
it on your deployment (through Hosting: 2 entries and your own address as `client_address`,
never the made-up `192.0.2.1`). Set another value and re-apply only if it shows a different
shape. Why 2, and its known limit for callers of the proxy's public `run.app` URL: module phr's
README, "The client address and the direct run.app URL". The direct proxy URL is `terraform
output -raw fhir_proxy_url`.

### 13. The ingestion checks (deployer)

Items I1 to I13: the first sync or top-up (the Patient entry answers `200`, not `201`), the
second run (`stored_read: ok`, `windows_unchanged > 0`), `/status` timing, the alerts.

### 14. End to end (T29)

`make smoke` ([`tests/e2e/README.md`](../../tests/e2e/README.md)): through the front door only,
a simulated cuff reading over MLLP to the FHIR store (Observation and Provenance checked
against the message sent), shared to OpenEMR and read back, shared again (`already-shared`),
plus malformed readings that must create nothing and the `/status` strip. Run it with your own
`gcloud` login: it signs its own demo sessions with `<prefix>-session-signing-key`, read from
Secret Manager into memory (a command line cannot pass the browser's bot check, and the proxy
has no bypass), so your account needs `roles/secretmanager.secretAccessor` on that secret:

```bash
PULSEBRIDGE_BASE_URL="$(terraform -chdir=infra/envs/<env> output -raw dashboard_url)/api" \
SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/<env> output -raw secret_name_prefix)" \
PULSEBRIDGE_TOKEN_ISSUER="$(terraform -chdir=infra/envs/<env> output -raw token_issuer)" \
make smoke
```

`PULSEBRIDGE_TOKEN_ISSUER` is fhir-proxy's exact `TOKEN_ISSUER` (`pulsebridge-fhir-proxy:<project_id>:<name_prefix>`;
output `token_issuer`, not a secret): the smoke test signs its sessions with it (T43). It is
built from `name_prefix`, not from `secret_name_prefix` (that is `identity_name_prefix`).
It uses two sessions, one for the reading and the share, one for the negatives, because the
proxy allows each session 3 cuff sends and 2 shares in any 10 minutes (T46; T48 found that one
session would be refused at N4). Its junk-token check (S1b) costs one real assessment of the
day's `bot_checks_per_day`.

On a real-data stack it refuses to write a test reading into the patient's record unless
`SMOKE_ARGS=--allow-real-data-writes` is given, and it runs I4's and I6's health-sync checks
from the `sync-run` log lines: add
`PULSEBRIDGE_HEALTH_SYNC_SERVICE="$(terraform -chdir=infra/envs/<env> output -raw health_sync_service_name)"`
and run it as someone with `roles/logging.viewer`. On a synthetic stack those checks are
reported as skipped. It prints what it added (synthetic values, attempt and Observation ids);
nothing it adds can be removed through the API.

Its `/status` check (S3, 5 s by default) times fhir-proxy's `/status` through the front door, not
health-sync's own, so it does not settle I7.

Worked when: the last line is `SMOKE RESULT: PASSED` (or `PASSED WITH 4 SKIPPED` on synthetic
data, the four being health-sync's checks).

**Result of the first run** (17 November 2026, dev, synthetic data, run as a project Owner):
exit 0, `SMOKE RESULT: PASSED WITH 4 SKIPPED (pass=33 fail=0 skip=4 warn=0)`. The cuff path
(Part A) ran live and passed, including the share and the repeat share; fhir-proxy's `/status`
answered in 0.85 s. Part B (I4, I6) was skipped: no health-sync on synthetic data. It has never
run live.

### Running the env roots from a service account in another project

**A person with their own Google login does not meet any of this**: run the steps as written,
with `deployer_service_account_email` set or with the roles yourself. It is for an automated
session (a cloud agent, a script on a server) whose credentials are a **service account key**
that belongs to a different project, its "home project". It was what the 16 November deploy ran
into, and what worked there.

- **Why.** Google bills API quota, and checks "is the API enabled?", against the **caller's**
  project unless the request names another one with the header `x-goog-user-project`. A key from
  another project therefore needs every API enabled in its home project, or the header.
- **Bootstrap**: `USER_PROJECT_OVERRIDE=true GOOGLE_BILLING_PROJECT=<project-id>` in the
  environment of `terraform plan/apply` (bootstrap README, "Running it from a service account in
  another project"). Worked on 16 November.
- **Impersonating the deployer did not work**, both ways: the provider's
  `impersonate_service_account` (`deployer_service_account_email`) and `gcloud
  --impersonate-service-account`. In both, the IAM Credentials API's `generateAccessToken`
  call that mints the deployer's token was billed to the home project, where that API was not
  enabled (`impersonate: status code 403 ... IAM Service Account Credentials API has not been
  used in project <home project>`).
- **What worked: mint the deployer's token yourself, with the header, and hand it to Terraform
  as `GOOGLE_OAUTH_ACCESS_TOKEN`.** Leave `deployer_service_account_email` unset for this. The
  token lasts one hour (enough for every step but a slow first boot, which needs no Terraform),
  and is passed only in the command's environment, never printed or written to a file:

```bash
# From infra/envs/<env>. <project-id> and <deployer email> are bootstrap's outputs.
mint_deployer_token() {
  curl -sS --fail-with-body -X POST \
    -H @<(printf 'Authorization: Bearer %s\n' "$(gcloud auth print-access-token)") \
    -H "x-goog-user-project: <project-id>" -H "Content-Type: application/json" \
    -d '{"scope":["https://www.googleapis.com/auth/cloud-platform"],"lifetime":"3600s"}' \
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/<deployer email>:generateAccessToken" |
    python3 -c 'import json, sys
d = json.load(sys.stdin)
if not d.get("accessToken"):
    sys.exit("mint failed: " + str(d.get("error", {}).get("message", ""))[:300])
sys.stdout.write(d["accessToken"])'
}
# Safe to paste into an interactive shell: a failed mint returns from the function, it does not exit.
tf_as_deployer() {
  local tok
  tok="$(mint_deployer_token)" || { echo "could not mint a deployer token" >&2; return 1; }
  env -u GOOGLE_APPLICATION_CREDENTIALS GOOGLE_OAUTH_ACCESS_TOKEN="$tok" terraform "$@"
}
tf_as_deployer plan
# gcloud as the deployer, the same way:
gcloud_as_deployer() {
  local tok
  tok="$(mint_deployer_token)" || { echo "could not mint a deployer token" >&2; return 1; }
  CLOUDSDK_AUTH_ACCESS_TOKEN="$tok" gcloud "$@"
}
gcloud_as_deployer run jobs list --project <project-id> --region <region>
```

  What the caller needs: `roles/iam.serviceAccountTokenCreator` on the deployer account, and
  `serviceusage.services.use` on `<project-id>` for the header (the project's Owner has both).
  The IAM Credentials API must be enabled in `<project-id>` (bootstrap enables it). `--fail-with-body`
  needs curl 7.76 or later. On a failure the
  function prints Google's error message (which names no token) and returns non-zero. This is
  documented commands, not a committed script: a helper would have to be tested against a fake
  token endpoint, and these few lines are clearer to read than a wrapper.
- **Not verified** for this kind of caller: CI (it uses Workload Identity Federation, not a key,
  and is unaffected) and prod.

## Operating it

### Patching the VM (monthly)

The VM does not update itself (COS in-place updates are off and the boot image is in
`ignore_changes`). Patch by replacing it:

```bash
terraform apply -replace='module.stack.module.edge_vm.google_compute_instance.this'
```

The data disk and the reserved internal address survive; the new VM boots from the newest
`cos-stable` image, mounts the existing disk without formatting, pulls the images again and
starts OpenEMR on its existing site files (about 10 minutes of downtime on 17 November: the
replace itself a minute, then the startup script, most of it the OpenEMR image pull). Do it
monthly, and whenever the COS release notes announce a security fix. Afterwards run
`terraform apply -refresh-only`: until then `terraform output edge_vm` shows the image family
it asked for, not the image the VM booted from. Check with `terraform output edge_vm`
(`boot_image`) and the startup log (`edge VM startup: done, all steps ok`).

### Rolling out the public demo (D16) on an existing stack

A stack built before T44 has the reviewer password; this changes it to the bot check and the
session cookie. A new stack needs none of this: the runbook's steps already include the
signing key (step 5) and the new permissions (step 1). Order matters because of three rules:

- **A Cloud Run service whose mounted secret has no version cannot be deployed** (checklist A8,
  A19). So the signing-key secret must exist and hold a version before fhir-proxy is changed to
  mount it: create the secret (targeted apply), add the version (script), then deploy the proxy.
- **A protected secret cannot be destroyed by Terraform.** In prod the old
  `<prefix>-reviewer-password` has `deletion_protection = true` (`protect_from_destroy`), so an
  apply that drops it from the configuration fails. Terraform is told to forget it first, and the
  secret is deleted by hand once the demo works.
- **The deployer and the planner need new permissions** (reCAPTCHA keys, the counter bucket), and
  only the owner can grant them, by re-applying bootstrap. CI cannot do it.

A short break of the live demo between steps 4 and 6 is expected (the running revision still
mounts the old secret, whose read grant step 4 removes) and accepted for prod (D16 rollout).

1. **Images.** Build and push the T43 fhir-proxy image (and the T45 page is built by the page
   deploy); put the new `fhir_proxy_image` digest in the tfvars. The old proxy image cannot run
   with the new environment (it requires `REVIEWER_PASSWORD_FILE`).
2. **Bootstrap, by hand, as the owner** (infra/bootstrap/README.md, "Re-applying for the public
   demo"): `terraform -chdir=infra/bootstrap apply`. It adds the deployer's two custom roles
   (reCAPTCHA keys, buckets), and the planner's read permissions. Do this **before**
   CI plans or applies the stack, or the planner's refresh and the deployer's create are refused.
3. **tfvars.** Remove `reviewer_password` and `reviewer_password_wo_version` from the local
   `terraform.tfvars` and from the CI tfvars object ("Where the tfvars come from"): the variables
   no longer exist.
4. **Forget the old secret, then the targeted apply** (deployer, from `infra/envs/<env>`):

   ```bash
   terraform state rm 'module.stack.module.identity.google_secret_manager_secret.this["reviewer-password"]'
   terraform apply -target=module.stack.module.project_base -target=module.stack.module.identity
   ```

   The apply enables `recaptchaenterprise.googleapis.com`, creates `<prefix>-session-signing-key`
   (empty), grants fhir-proxy `roles/recaptchaenterprise.agent`, and removes fhir-proxy's read
   grant on the old secret (the only destroy in the plan; if it plans to destroy a secret, stop).
5. **The signing key's version** (deployer, from the repository root):
   `scripts/secrets/set-session-signing-key.sh --project <project-id> --prefix <identity_name_prefix>`.
   Check: `gcloud secrets versions list <prefix>-session-signing-key` shows version 1 `enabled`.
6. **The rest of the stack** (CI, or `terraform apply` by hand): creates the reCAPTCHA key, the
   counter bucket and its grant (before the proxy, which depends on the grant), then deploys
   fhir-proxy with the new image, the signing-key volume and the `RECAPTCHA_*` settings. Then the
   page deploy (step 11) publishes the T45 page with the new Content-Security-Policy.
7. **Check** (README step 12 and module phr's "Verify on the first real deploy", items 6 to 8):
   "View the live demo" works on `<site>.web.app`; today's counter object appears in
   `gcloud storage ls gs://<project-id>-<name_prefix>-demo-counters`; no CSP error in the console;
   then `make smoke` (step 14) and the checklist's rows P17 to P26.
8. **Delete the old secret by hand**, once step 7 passes:
   `gcloud secrets delete <prefix>-reviewer-password --project <project-id>`. Until then it is
   only unused (nothing can read it: step 4 removed the one grant).

### The public demo: what is public, and its known limits

Since D16 anyone with the address can open the demo, with no password and nothing to sign up
for. Decide with that in mind:

- **Your real data is public within the window.** On a real-data stack (prod) every visitor sees
  your heart rate for the last `history_days` (7) days, and the blood-pressure readings and
  shares of that window. Older data stays in the stores and in OpenEMR; the proxy only hides it
  (a read of something older answers like a missing resource). This was the owner's decision
  (D16). A synthetic stack (dev) shows invented data only.
- **Visitors can write.** A session may send cuff readings and share readings to OpenEMR, within
  the limits above; what they send stays in the record (nothing the API offers removes it) and
  is shown to the next visitors while it is in the window.
- **What visitors are told.** The landing view says the page uses reCAPTCHA and links to the
  privacy note (`web/privacy.html`, also linked from the dashboard's header): the cookies, what
  the bot check sends to Google (since 2 April 2026 you, the site owner, are the data controller
  for it and Google the processor), what the proxy and Cloud Run log (the client address only in
  sampled refusals; Cloud Run's request log of every API call), and that logs are kept 30 days
  (Cloud Logging's default for the `_Default` bucket, which nothing here changes). It names
  nobody: no contact line, since that would need a configured address the repository does not
  have.
- **The direct `run.app` URL.** fhir-proxy must accept requests from Firebase Hosting, so its
  `*.run.app` URL is public too. A caller of that URL can write its own `X-Forwarded-For` and so
  choose which address it is counted as: it can use up another address's 5 bot-check attempts an
  hour and its budget for requests without a session (a visitor who already has a session is
  not affected, because sessions are counted by their own ID). What still bounds it is the daily
  bot-check cap. The fix belongs at the edge (trust the forwarded address only from Hosting, or
  make the URL reachable from Hosting only) and is a recorded follow-up of T46 (module phr's
  README, "The client address and the direct run.app URL").
- **When the cap is reached** the page says "The demo is busy, try again tomorrow." until UTC
  midnight. Assessments that fail count too, so a scripted flood can use the day up; it costs
  nothing beyond the cap.

### Rotating the session signing key

`scripts/secrets/set-session-signing-key.sh --rotate` adds a new random 32-byte version.
fhir-proxy mounts the secret at `latest`, so the new key applies without a redeploy once the
proxy reads the file again, and **every open demo session ends** (tokens signed with the old key
no longer verify; visitors click "View the live demo" again). Without `--rotate` the script
refuses to replace an existing key, so running it twice by mistake ends nobody's session. Older
versions stay enabled; disable them with `gcloud secrets versions disable` when you like
(scripts/secrets/README.md).

### Reconnecting Google Health (real data)

While the OAuth app is in "Testing" status, its refresh token expires after 7 days: the
`health-sync-not-connected` alert fires and the status strip shows "reconnect needed". Run the
connect script again (step 9; it reuses the stored client and the Patient write is idempotent).

### `terraform destroy`, and what it leaves behind

dev: `terraform destroy`. prod refuses by design (`protect_from_destroy`): to really remove it,
set `protect_from_destroy = false` in `prod/env.tf`, `terraform apply` (the Healthcare deletion
policy is read from state, so it must be applied before the destroy), then `terraform destroy`.

What remains afterwards:

| Left behind | Why | What to do |
| --- | --- | --- |
| The default Firebase Hosting site **and its last deployed page**, public at `https://<project>.web.app`, every `/api` call failing | The provider skips deleting a project's default site | `firebase hosting:disable --project <project>` if no page should stay live |
| Firebase on the project | It cannot be removed | Nothing; it costs nothing |
| Possibly the serverless subnet (and the VPC) on the first try | Google: after the Cloud Run services that used Direct VPC egress are deleted, "wait 1-2 hours" before the subnet can be deleted | Run `terraform destroy` again after one to two hours |
| The enabled APIs | `disable_on_destroy = false` on purpose | Nothing; an enabled API costs nothing |
| The state bucket, deployer and WIF pool | They are bootstrap's | infra/bootstrap/README.md, "Teardown" |
| `gs://<project>_cloudbuild` | Created by `gcloud builds submit`, not by Terraform | `gcloud storage rm -r gs://<project>_cloudbuild` |
| Logs in Cloud Logging | Kept for their retention period (30 days by default), free | Nothing |
| prod only: the data disk's daily snapshots (up to 7) | The schedule keeps them when the disk is deleted (`KEEP_AUTO_SNAPSHOTS`), so an accidental destroy can be undone; they are **billed as snapshot storage until deleted** | `gcloud compute snapshots list --filter="sourceDisk~<name_prefix>-edge-data" --project <project>`, then `gcloud compute snapshots delete <names> --project <project>` |

The secrets go with their versions, the Healthcare dataset with every stored resource and
message (`deletion_policy = "DELETE"` in dev), the VM with both disks and its address (in dev
there is no snapshot: a destroyed data disk is gone), the Artifact Registry repositories with their
images. A repository holding images deletes without error (checklist T2, verified on dev on
19 November).

### Cost

From the module READMEs, with their caveats (prices "not confirmed" there are not confirmed
here either).

| Item | Idle | In use (one reviewer at a time) |
| --- | --- | --- |
| e2-micro VM, 20 + 10 GB `pd-standard` | **$0 inside the free tier** (one e2-micro per billing account, us-west1/us-central1/us-east1, 30 GB-months of standard disk). A second e2-micro on the same billing account (dev + prod) is billed: about $6 a month, not confirmed | the same |
| Artifact Registry | $0.10 per GB-month beyond 0.5 GB free: the cached OpenEMR image likely passes 0.5 GB, so cents to a few tens of cents a month | the same |
| Healthcare API | storage of a few MB: cents | per request: small |
| Cloud Run (4 services with real data; 3 services and the seeder job with synthetic data) | $0 (scale to zero) | inside the free tier for a demo (not checked) |
| Cloud Scheduler | $0: 3 jobs free per billing account (one per deployment) | the same |
| Pub/Sub, Secret Manager, log-based metrics, IAM, VPC, firewall | about $0 | about $0 |
| Alert policies | $0 until 1 September 2027; then $0.35 per metric reference a month: about $1.05 (real data, 3 policies) or $0.70 (synthetic, 2) | the same |
| Firebase Hosting | $0 under 10 GB stored and 10 GB a month transferred | the same |
| reCAPTCHA assessments (Fraud Defense) | $0 | 10,000 a month free per organisation, then an $8 flat fee for 10,001 to 100,000 in a month ([Billing information](https://docs.cloud.google.com/recaptcha/docs/billing-information), read 20 November 2026). The default cap of 300 a day is at most 9,300 a month: $0, unless the same organisation's other keys, or a second deployment's 300 a day, push the month past 10,000 |
| The demo counter bucket | $0: at most 7 tiny objects (one per day, deleted after 7 days) | one read and one write per bot check: fractions of a cent |
| Data disk snapshots (prod only, 7 daily, incremental) | snapshot storage of a fraction of a GB: cents a month, not confirmed | the same |
| Cloud NAT (off) | **billed hourly** if turned on: about $4.70 a month plus $0.045 per GiB | |

So **near $0 a month idle** for one deployment on a fresh billing account, plus cents of
storage; about $6 more for a second deployment's VM; about $1 more for alerts from September
2027; $8 in a month when the organisation's reCAPTCHA assessments pass 10,000 (two deployments
at the full default cap every day would). The only always-on resource is the VM. A project with
a billing account is on Fraud Defense's Premium tier, where assessments past the free 10,000
keep working and are billed; without billing they would be refused (Essentials).

## CI/CD (T26)

Everything above can be done by hand. This section is how GitHub Actions takes it over. Nothing
here has run against a real project or a real GitHub repository yet (it was built offline); the
[first-run checklist](#cicds-first-real-run) lists what must be confirmed.

### Terms

- **CI/CD** (continuous integration / continuous delivery): every change is checked
  automatically when it is proposed (CI), and delivered to the environments automatically, or at
  the press of an approval button, once it is merged (CD). Here: checks and a dev plan on every
  pull request, a dev apply on every merge, prod after an approval.
- **Workflow, job, runner**: a workflow is a YAML file in `.github/workflows/`; it has jobs, and
  each job runs on a fresh virtual machine (a runner) that is thrown away afterwards.
- **`GITHUB_TOKEN` permissions**: every job gets a token for GitHub's own API; `permissions:`
  says what it may do. Each job here gets only what it needs (`contents: read`, plus `id-token:
  write` to ask for an OIDC token, or `pull-requests: write` to comment, or `actions: read` to read
  the Environments' settings).
- **OIDC token**: a short-lived signed statement from GitHub about the job ("repository X,
  branch main, environment prod, event push"). A job can ask for one only with `id-token: write`.
- **Workload Identity Federation (WIF)**: Google's way to trust such a token instead of a stored
  key. The job sends its token to Google's Security Token Service and gets back an access token
  for a service account that works for about an hour. **No service account key exists, in GitHub
  or anywhere**, so there is nothing long-lived to leak (infra/bootstrap/README.md).
- **CI context**: one word bootstrap computes from the token, saying where the job runs:
  `pull-request`, `main` (push, schedule or manual run on main), `env-<name>-main` (a job in
  GitHub Environment `<name>`, on main), or `other`. Each service account is bound to its own
  contexts, so a job can only become the accounts its context allows.
- **GitHub Environment** (Settings, Environments): a named deployment target with **protection
  rules** GitHub enforces before a job that names it may start: **required reviewers** (the job
  waits until one of them approves) and **deployment branches** (only jobs from `main`). Only after
  that does GitHub put `environment: <name>` into the job's OIDC token, which prod's bindings
  require. So the approval gate is enforced by GitHub and Google, not just by YAML.
- **Repository variable**: a named, non-secret setting under Settings, Secrets and variables,
  Actions, Variables, read in a workflow as `vars.NAME`. The values are identifiers (a provider's
  resource name, an account email, a bucket name), not secrets; the workflows use no `secrets.*`.
- **Artifact**: a file a job uploads for later jobs of the same run. Used here only for things
  that hold no secret (below).
- **Concurrency group**: GitHub runs at most one job of a group at a time. Every job that writes
  an environment's state is in `terraform-state-<env>`, with `cancel-in-progress: false` (an apply
  is never stopped half-way). A newer waiting run replaces an older waiting one in the same group,
  which is fine: the newer one applies newer code. The GCS backend's state lock is a second guard.

### The three identities

| Account (bootstrap) | Can do | Used by |
| --- | --- | --- |
| **planner** (`create_planner`) | read the resources' configuration and IAM policies, secret metadata, the **state and the CI tfvars** (which hold the patient's identifier, in prod the patient's name, and the alert address, in clear); no FHIR resource, HL7v2 message, log entry or secret value; no write anywhere, not even the state lock (bootstrap README, "CI identities", lists its custom role) | the pull-request plan (dev), `prod-plan` |
| **deployer** | everything an apply needs (bootstrap's "Deployer roles") | the dev apply, `prod-apply`, the nightly destroy |
| **page deployer** (`create_page_deployer`) | deploy the page to Hosting: `roles/firebasehosting.admin` and `roles/serviceusage.serviceUsageViewer` on the project (bootstrap), `roles/run.viewer` on fhir-proxy's service only (the env root's `page_deployer_service_account_email`); `firebase deploy` needs all three (T41) | the page deploy jobs |

Each is reachable only from its contexts, and no two share one: dev's planner from
`pull-request`, dev's deployer from `main`, dev's page deployer from `env-dev-page-main` (the
`dev-page` Environment); prod's planner from `env-prod-plan-main`, prod's deployer from
`env-prod-main`, prod's page deployer from `env-prod-page-main` (the `prod-page` Environment). The
jobs that hold a deployer run no npm code (no `npm ci`, no Vite, no Firebase CLI); the page is
built in a job with **no** cloud credential; and the page deploy job runs in its own Environment,
so the npm packages there could ask GitHub for a token but could only become the page deployer,
which can only change Hosting (it can also read which APIs are enabled and fhir-proxy's
configuration, whose environment holds the patient ID; the truth table below).

### What runs when

| Workflow | When | Identity | What it does |
| --- | --- | --- | --- |
| `terraform-checks.yml` | every pull request (forks too) and push to main | none | `make tf-validate`, `make tf-test` (mocked providers), `make tf-lint`, `make tf-scan` |
| `terraform-plan.yml` | pull requests from branches of this repository | dev planner | plans dev (`-lock=false`); a second job without credentials posts the redacted plan as a comment |
| `terraform-apply.yml` | push to main (a merge); or by hand from main (dev only, unless "prod" is ticked) | see below | builds the page (no credential); applies dev (deployer) and deploys its page (page deployer); checks prod's Environments; plans prod (planner, `prod-plan`); after approval applies prod (deployer, `prod`) and then deploys prod's page (page deployer, `prod-page`) |
| `terraform-destroy-dev.yml` | 03:17 UTC and a retry at 06:17 UTC, if opted in; or by hand from main | dev deployer | destroys dev; the first run may leave the serverless subnet and the VPC, the retry nothing |
| `ci.yml` (T01) | every pull request | none | Python, web and secret checks; its former Terraform job moved to `terraform-checks.yml` |

All four `terraform-*` workflows call `infra/envs/ci/tf_ci.py`, so the steps read top to bottom in
one file and are tested (`tests/tooling/test_tf_ci.py`). No workflow has a `paths` filter: a
path-filtered workflow reports no status on pull requests it skips (so it can never be a required
check), and a page or Terraform change must reach every environment on its merge.

### Merging this changes nothing until you set it up

Every job that authenticates is skipped, not failed, while its repository variables are unset;
the plan needs the **planner** variables and never falls back to the deployer. With the variables
set, CI plans and applies an environment only after a person has done its first full apply
(runbook step 8): `tf_ci.py stack-is-up` looks for fhir-proxy's Cloud Run service in the state.
Before that (steps 3 to 7) and after the nightly destroy, the job summary and the plan comment say
in a heading **"dev is not deployed: nothing was planned or applied"**.

prod follows dev: `prod-plan` runs only if dev was applied in the same run, or dev is not
configured at all (dev's project does not exist yet, D1), or the repository variable
`PROD_WITHOUT_DEV` is `true`. After the nightly destroy dev is down, so prod waits too: set
`PROD_WITHOUT_DEV = true` for that time, or bring dev back up.

Never from a fork: the plan job runs only when the pull request's branch is in this repository,
and not for Dependabot.

### Where the tfvars come from

A root's `terraform.tfvars` names the project, the patient and the alert address, so it is not
in git and not in GitHub. CI reads it from the environment's own state bucket, object
`ci/<env>.tfvars`, with a 5-minute access token, writes it with mode 600, registers the
sensitive values with the runner's log masking, and deletes it at the end of the job. The
object's GCS **generation** (a number that changes on every write) is recorded with each plan;
`prod-apply` fetches and applies exactly that generation, so an edit made after the review is not
applied by that run: it waits for the next run's plan and review. Upload or
update it yourself, as the owner or the deployer, for example after pasting new image digests:

```bash
gcloud storage cp infra/envs/prod/terraform.tfvars "gs://<prod state_bucket_name>/ci/prod.tfvars"
gcloud storage cp infra/envs/dev/terraform.tfvars  "gs://<dev state_bucket_name>/ci/dev.tfvars"
```

Rules for a CI-managed environment's tfvars: the sensitive variables (`patient_id`,
`patient_family_name`, `patient_given_name`, `alert_email`) as plain quoted strings on one line
(a heredoc or an expression is refused: CI could not mask it or check plans for it);
`ingest_enabled` as you want it to stay; `deployer_service_account_email` unset;
`page_deployer_service_account_email` set to bootstrap's output of that name (without it the
page deploy job is refused when it finalizes the version);
no `reviewer_password` or `reviewer_password_wo_version` (removed by T44: delete them from the
object, "Rolling out the public demo"). **Whoever can write
that object controls what CI applies**: keep write access to the state bucket to the owner and
the deployer.

### Setting it up (once), in this order

1. **Branch protection for `main`** (Settings, Rules, Rulesets, New branch ruleset, target the
   default branch): require a pull request with **at least one approving review**, dismiss stale
   approvals, require the status checks `Terraform fmt, validate, test, tflint and trivy`, `Lint
   and test (Python)`, `Secret scan (gitleaks over commits)`, `Web (lint, test, build)` and
   `Pre-commit hooks (all files)`, and block force pushes. Add a `CODEOWNERS` file that names you
   for `infra/` and `.github/`, and require code-owner review. Then what reaches main, and so
   everything the deployer and `prod-plan` run, has been reviewed. With a single maintainer,
   GitHub does not let you approve your own pull request; another reviewer, or a temporary
   bypass for yourself, is needed.
2. **The GitHub Environments**, Settings, Environments, New environment, created **before**
   step 3 (prod) and step 4 (dev):
   - `prod-plan`: Deployment branches and tags, **Selected branches and tags**, add the branch
     rule `main` (and nothing else). No reviewers (it only plans, as the read-only planner).
     Leave "Allow administrators to bypass configured protection rules" **off**.
   - `prod`: **Required reviewers**, add yourself (up to six; one approval is enough). Deployment
     branches: Selected branches and tags, `main` only. Administrator bypass off. "Prevent
     self-review" stays off if you are the only maintainer, or you could never approve.
   - `prod-page` and `dev-page`: Selected branches and tags, `main` only; administrator bypass
     off; **no reviewers**: the page job runs only after a successful, approved apply of the same
     run (prod) or a successful apply (dev). Their own Environments give the page jobs a CI
     context only the page deployer is bound to.
   - Not "Protected branches only": with no classic branch protection rule, GitHub lets every
     branch deploy then. `prod-preflight` (for `prod-plan`, `prod`, `prod-page`) and
     `dev-preflight` (for `dev-page`) refuse anything but exactly the settings above (custom
     policy, the single branch `main`, no admin bypass, reviewers on `prod`), checked
     type-exactly, and fail on any API error.
   - Required reviewers on Environments are available on GitHub Free, Pro and Team only for
     **public** repositories.
   - **Why first:** once prod's bootstrap (step 3) trusts these names, a workflow that names an
     environment that does not exist yet would create it, with no rules, and its job would get
     prod tokens. Creating and configuring them first closes that window.
3. **prod's bootstrap** (the existing project, D1), as the owner, with:

   ```hcl
   github_allowed_refs        = ["refs/heads/main"]
   github_environments        = ["prod-plan", "prod", "prod-page"]
   deployer_wif_contexts      = ["env-prod-main"]
   create_planner             = true
   planner_wif_contexts       = ["env-prod-plan-main"]
   create_page_deployer       = true
   page_deployer_wif_contexts = ["env-prod-page-main"]
   ```

   Read the plan: these are **real IAM changes** (listed under "IAM changes" below). The planner's
   custom role needs `iam.roles.create`, which you have as the owner. The dev defaults of the
   three context lists are refused here (a validation), so none can slip through.
4. **dev** (when you want it): create and bootstrap the second project (`create_project =
   true`) with `create_planner = true` and `create_page_deployer = true` and the default contexts
   (deployer `main`, planner `pull-request`, page deployer `env-dev-page-main`; create the
   `dev-page` Environment first), then bring dev up by hand with steps 2 to 10.
5. **Upload each environment's tfvars** as above, with `page_deployer_service_account_email` set
   to that environment's bootstrap output `page_deployer_service_account_email` (`terraform
   -chdir=infra/bootstrap output -raw page_deployer_service_account_email`). The first CI apply
   then grants the page deployer `roles/run.viewer` on fhir-proxy, which `firebase deploy` needs
   for the `/api` rewrite (T41). Without it the page deploy job fails with 403 "Permission
   'run.services.get' denied".
6. **Repository variables** (Settings, Secrets and variables, Actions, Variables, Repository
   variables). Set an environment's variables last: setting them turns its jobs on.

   | Variable | Value (bootstrap output of that environment's project) |
   | --- | --- |
   | `PROD_GCP_WORKLOAD_IDENTITY_PROVIDER` | `workload_identity_provider` |
   | `PROD_TF_STATE_BUCKET` | `state_bucket_name` |
   | `PROD_GCP_DEPLOYER_SA` | `deployer_service_account_email` |
   | `PROD_GCP_PLANNER_SA` | `planner_service_account_email` |
   | `PROD_GCP_PAGE_DEPLOYER_SA` | `page_deployer_service_account_email` |
   | `DEV_GCP_WORKLOAD_IDENTITY_PROVIDER`, `DEV_TF_STATE_BUCKET`, `DEV_GCP_DEPLOYER_SA`, `DEV_GCP_PLANNER_SA`, `DEV_GCP_PAGE_DEPLOYER_SA` | the same five outputs of dev's bootstrap |
   | `DEV_NIGHTLY_DESTROY` | `true` to turn on the nightly destroy of dev (optional) |
   | `PROD_WITHOUT_DEV` | `true` to plan and apply prod while dev is not deployed (optional) |

7. **Image builds** (optional): step 7, "With the trigger", and a tag ruleset for `v*`.

### Day to day

1. Open a pull request: the checks run; a comment shows dev's plan, made by the planner. The
   comment is updated on each push: `gh pr comment --edit-last` edits the newest comment the
   workflow's token wrote on that pull request, so if another workflow also comments as
   `github-actions`, the two would overwrite each other.
2. Merge it: dev is applied, then its page deployed. `Plan prod` runs; its job summary shows
   prod's redacted plan. The run waits at `Apply prod (after approval)`; open the run, read the
   plan, "Review deployments", approve (or reject: nothing is applied). The apply refuses if main
   has moved on (a newer run applies the newer commit) or if the plan differs from the reviewed
   one; it applies the reviewed tfvars generation, so a later edit waits for the next run. `Deploy
   prod's page` then runs in `prod-page` without a further approval, after checking that main has
   not moved on.
3. A manual run (Actions, Terraform apply, Run workflow, from main) applies dev and deploys its
   page; tick "prod" to include prod.
4. Nightly (if opted in), dev is destroyed with its images and secret values. CI then does nothing
   for dev, and prod waits for dev unless `PROD_WITHOUT_DEV`, until you bring dev up with steps 3
   to 8. Turn `DEV_NIGHTLY_DESTROY` off while you do: the destroy leaves a partly built dev alone,
   with a warning, but a finished step 8 the evening before would be destroyed that night.

### Who can reach what (the threat statement)

After these settings, and assuming branch protection as in step 1:

| Who | dev | prod |
| --- | --- | --- |
| Anyone (forks, the public) | nothing: fork pull requests get no OIDC token and the plan job does not run for them | nothing |
| A collaborator with **write** access (or an agent that pushes `task/*` branches) | through a pull request, a token for dev's **read-only planner**: the pull request's code can read dev's resource configuration, IAM policies, and **dev's state and CI tfvars** (a synthetic patient, but dev's project ID and the alert address in clear); it cannot change anything. Pushing a branch alone gives nothing (`other` context). Reaching dev's deployer needs code on main, so an approved merge | nothing until a reviewed merge to main |
| Code merged to main (reviewed) | the dev deployer (apply, destroy) and dev's page deployer | **the prod planner in `prod-plan`, before any approval**: it can read prod's state and CI tfvars, which hold the **patient's real identifier and name** and the alert address in clear, and every resource configuration. What stands in front of it is the ruleset's approving review on main (setup step 1). Stricter option: add required reviewers to `prod-plan` too (then every prod plan waits for an approval, and `prod-preflight` still passes). The prod deployer only after an approval in `prod` |
| A compromised npm package of the page build | nothing: the build job has no credential | nothing |
| A compromised package of the Firebase CLI (locked, install scripts off) | dev's Hosting site: the page job's context is `env-dev-page-main`, bound only to the page deployer; it can also read which APIs are enabled and fhir-proxy's configuration (T41's two read grants), whose environment holds the patient ID | prod's Hosting site, in the page job after an approved apply (`env-prod-page-main`); the same two reads in prod, so prod's **patient identifier** (and, in Cloud Run's configuration, the patient's name) is readable from that job |
| A compromised GitHub action (pinned by commit SHA) | whatever the job that uses it holds | the same |
| Whoever can write `ci/<env>.tfvars` | the inputs of every dev apply | the inputs of the next prod plan (prod-apply applies the reviewed generation) |

**Which job can become which account** (the truth table). Computed by evaluating bootstrap's real
`ci_context` mapping, prod's pool condition and the bindings (from the mocked plans, with
cel-python); `tests/tooling/test_wif_truth_table.py` checks, for dev and for prod, all 1,008 combinations of
9 events, 7 refs, 8 environments and 2 repositories against an independently written expectation, and that no token ever
reaches two accounts. Representative rows:

| Job (token claims) | `ci_context` | dev: account | prod: pool condition | prod: account |
| --- | --- | --- | --- | --- |
| pull_request (same repository) | `pull-request` | planner | refused | none |
| push to main | `main` | deployer | refused | none |
| schedule / manual run on main | `main` | deployer | refused | none |
| push to a branch | `other` | none | refused | none |
| manual run on a branch | `other` | none | refused | none |
| tag push | `other` | none | refused | none |
| pull_request_target | `other` | none | refused | none |
| merge queue (merge_group) | `other` | none | refused | none |
| env dev-page, push to main | `env-dev-page-main` | page-deployer | refused | none |
| env dev-page, push to a branch | `env-dev-page-other` | none | refused | none |
| env prod-plan, push to main | `env-prod-plan-main` | none | admitted | planner |
| env prod, push to main | `env-prod-main` | none | admitted | deployer |
| env prod, push to a branch | `env-prod-other` | none | refused | none |
| env prod, pull_request | `env-prod-other` | none | refused | none |
| env prod-page, push to main | `env-prod-page-main` | none | admitted | page-deployer |
| env prod-page, pull_request_target | `env-prod-page-other` | none | admitted | none |
| env staging, push to main | `env-staging-main` | none | refused | none |
| another repository, push to main | `other-repository` | none | refused | none |

Google's CEL implementation is not cel-python; that they agree is checklist item CI4.

**Billing in dev.** The dev deployer can create any billable resource the stack's roles allow,
and `roles/resourcemanager.projectIamAdmin` lets it grant itself more. Set a **budget with an
alert** on dev's billing account (Billing, Budgets and alerts, for example $10 a month with alerts
at 50, 90 and 100 percent); a budget alerts, it does not stop spending.

**Logs and summaries are public on a public repository.** The plan comment, the job summaries
(prod's redacted plan included) and the job logs show project IDs, service URLs and account
emails. They show no sensitive value: Terraform redacts them, CI masks the tfvars values in logs,
and the redaction check refuses a plan that still shows one.

### Choices made, and why

- **Binary plans are never uploaded.** A binary plan (`-out`) holds every value in clear. Each
  apply job plans for itself; the binary plan stays in the runner's temp directory and is deleted.
  The artifacts are: the redacted plan text with its metadata (digest, change count, tfvars
  generation, commit), written only after the redaction check passed; the built page (`web/dist`:
  it holds no secret and is public once deployed); and the page deploy's configuration
  (`firebase.json`, which is the site, the rewrite and the headers, and the project ID). All are
  kept one day. Files rather than step outputs, because the runner drops an output that contains
  a masked value, and a short masked name could appear inside the plan text.
- **The prod plan is pinned three ways.** The digest is a SHA-256 of the plan's resource changes
  and output changes (addresses, actions, and the values they will have afterwards, `after` and
  `after_unknown`) with every sensitive value replaced by a placeholder, so it cannot be used to
  test guesses of the patient ID. `before`, the refreshed current values, is left out: a value that
  drifted between review and apply and that the plan sets back anyway would change the digest
  without changing the reviewed outcome, while a change to what the apply will set still does. A
  change in a sensitive value alone does not change it; that comes from the tfvars, which are
  pinned by generation. And the commit must be the tip of main.
- **If the digest never matches** (some resource reports a different planned value on every
  plan, so every prod apply refuses with "the plan differs from the one reviewed" although nothing
  changed): compare the `plan-prod` artifact's plan text with the apply job's plan in its log; the
  resource whose values differ is the cause. Apply that run by hand as the deployer with the
  runbook (`terraform plan`, read it, `terraform apply`), record the resource in CI9, and fix it in
  code (for example `lifecycle { ignore_changes = [...] }` on the noisy attribute) rather than
  loosening the digest.
- **The redaction check is a backstop.** Terraform's `sensitive` is the protection. The check
  looks for each sensitive tfvars value as a whole word in the plan text; it misses a value
  changed in case, encoded (base64, URL encoding) or embedded in a longer identifier, and a short
  name (`Ann`) can only be found as a whole word, never inside another. Heredoc and expression
  values are refused rather than guessed at.
- **The deployer is impersonated, not used directly by the pool** (`service_account` in the auth
  step). Direct federation gives a federated token that not every Google API accepts; an
  impersonated account's access token works with Terraform, Cloud Storage and the Firebase CLI.
- **The Firebase CLI and federated credentials.** Firebase's CLI documentation: "The Firebase CLI
  will detect and use Application Default Credentials if they're set", and it recommends them for
  CI. The auth step writes an external-account credential file, one of Google's documented forms
  of Application Default Credentials. The CLI's source (`src/requireAuth.ts`, `autoAuth`) uses
  `google-auth-library`'s `GoogleAuth`; the version firebase-tools 15.34.2 locks (9.15.1) handles
  `external_account` files. An offline experiment confirmed the flow: with such a file whose
  endpoints were a local stand-in server, the CLI fetched the OIDC token, exchanged it at the STS
  endpoint, then called `generateAccessToken` for the account. **Not verified live** (CI6). The
  auth action's README warns that WIF is not supported by the Firebase **Admin SDK**, a different
  library. Fallback if the CLI refuses: the Hosting REST API with the page deployer's token.
- **The Firebase CLI's whole tree is locked**: `ci/deploy-tools/package.json` pins
  `firebase-tools` 15.34.2 and the committed `package-lock.json` pins all 628 packages with sha512
  integrity hashes from registry.npmjs.org (`check_env_roots.py` checks it). The deploy job runs
  `npm ci --ignore-scripts` there and the CLI with `npx --no-install`.
- **Credentials last as long as the job.** The credential file tells the client library to fetch
  a fresh GitHub OIDC token whenever it needs a new Google token, so a long apply or destroy keeps
  working (read from the auth action's source).
- **tflint and Trivy are binaries with pinned SHA-256 sums** in the Makefile, not GitHub actions:
  Trivy's own actions and release tags were hijacked in 2026, and a checksum pinned here refuses a
  replaced asset (a test proves it).
- **The planner plans with `-lock=false`.** Taking the GCS lock means writing an object; the
  planner is read-only. A plan made while an apply runs may be stale; nothing is applied from it.
- **The nightly destroy is two runs, three hours apart**, not one run that sleeps.
- **No `queue: max` on the concurrency groups.** The pinned actionlint (v1.7.12) rejects it.

### What the checks accept

`make tf-scan` fails on HIGH and CRITICAL findings; the repository has none. It lists, and
accepts, these lower ones:

| Check | Where | Why accepted |
| --- | --- | --- |
| GCP-0011 (MEDIUM): service account access at project level | bootstrap `deployer_roles` (`roles/iam.serviceAccountUser`) | the deployer attaches accounts to Cloud Run, the VM and Scheduler; infra/bootstrap/README.md, "Deployer roles" |
| GCP-0076 (MEDIUM), GCP-0029 (LOW): subnet flow logs off | project-base subnets | flow logs bill per GB; project-base README, "Idle cost" |
| GCP-0079 (LOW): IAM audit config | bootstrap project | Data Access audit logs bill per GB; Admin Activity logs are always on |
| GCP-0066 (LOW): bucket without a customer-managed key | bootstrap state bucket | Google-managed encryption is on by default; a KMS key costs per key-month |
| GCP-0033, GCP-0034 (LOW): disks without customer keys | edge-vm disks | the same |

`make tf-lint` reports nothing today. To silence a finding on purpose, add an inline
`# trivy:ignore:<ID>` or `# tflint-ignore: <rule>` comment with the reason next to it.

### What CI costs

- **GitHub Actions minutes**: free for public repositories. On a private one, each merge runs about
  six jobs (a few minutes each; the applies longest), each pull request two plus the checks, and
  the nightly destroy two runs: plausibly 300 to 800 minutes a month for steady work, inside
  GitHub Free's 2,000 for private repositories. Not measured.
- **Cloud Build**: per build-minute beyond the free allowance, only when a tag is pushed and the
  build approved (services/README.md, "Cost").
- **Artifact Registry**: about 100 MB per release; the cleanup policy keeps the newest ten
  versions of each image and deletes older ones after 90 days, so storage stays near 1 GB per
  image at most (about $0.10 a GB-month beyond 0.5 GB free). The policy cannot tell which image a
  Cloud Run revision still uses: a deployed image older than 90 days and outside the newest ten
  would be deleted, and whether Cloud Run then still starts new instances is not verified (CI14).
- **Workload Identity Federation, the service accounts, the bindings**: free.

### IAM changes, all opt-in unless marked

| Change | Account | Where | Default? |
| --- | --- | --- | --- |
| The deployer's WIF binding changes from "any workflow of the repository" to its contexts (`main`, or `env-prod-main` for prod) | deployer | bootstrap | **default**, at the next bootstrap apply in each project |
| `roles/cloudbuild.builds.editor` | deployer | bootstrap `allow_build_trigger` | opt-in |
| The pool's condition requires `ref` main and the two Environments | the pool | bootstrap `github_allowed_refs`, `github_environments` | opt-in (prod) |
| The planner account; its custom role `pulsebridgeTerraformPlanner` (get and getIamPolicy of the stack's resource types, `serviceusage.services.use`; bootstrap README, "CI identities"); object viewer on the state bucket; its WIF binding (`pull-request` in dev, `env-prod-plan-main` in prod) | planner | bootstrap `create_planner` | opt-in |
| The page deployer account, `roles/firebasehosting.admin`, `roles/serviceusage.serviceUsageViewer` (T41: the Firebase CLI checks the Hosting API with `serviceusage.services.get`), its WIF binding (`env-dev-page-main` in dev, `env-prod-page-main` in prod) | page deployer | bootstrap `create_page_deployer` | opt-in |
| `roles/run.viewer` on the fhir-proxy Cloud Run service only (T41: Hosting checks `run.services.get` on the rewrite's target when a version is finalized) | page deployer | env roots `page_deployer_service_account_email` (module phr) | opt-in |
| The build account: log writer (project), writer (services repository), reader (Docker Hub mirror) | build | env roots `create_build_service_account` | opt-in |

## Master checklist

Every "Not verified" / first-boot / first-deploy item of the modules, in the order a first
deployment meets them. Fill in the last column (`YYYY-MM-DD: ok`, or what happened and what was
changed).

**Recorded so far: the first dev deploy, 16 November 2026** (a new dev project, synthetic data,
runbook steps 1 to 11, checked from the apply logs, the VM's serial port output, the seeder's
execution, the build log and the front-door smoke checks). An item says `ok` only where that
evidence shows exactly what the item asks; `partly` says which half was seen; `no` means it was
seen to be false; an empty cell, or "not checked", means it is still open. Still open after that
run, and needing a session with the user's go-ahead (a second session on 17 November did the
reboot, patching, seeder-alert and Scheduler items; what it left is listed here):

- The rest of the front door: the forced slow cuff send (P8). (The hop measurement, P1, the
  rotation, P6, and P9 to P11 and P16 were done on 17 and 18 November.)
- The remaining ingestion checks: I5, `/status` timing for health-sync (I7), dead-lettering
  (I21). (The Scheduler alert and replay were done on 18 November.)
- What needs a shell on the VM: `TimeoutStopUSec` (E12), the crash-loop check (E16), and the
  inspections in E4, E7, O2, O4 to O6, O8 to O10, O21. (Logging, E15 and E19, was checked from
  Cloud Logging on 18 November.)
- **Destroy** (T1 to T4).
- **CI/CD**: the first real run on dev (18 November; T41) answered P12, CI6, CI15 and CI16 and
  parts of CI2 to CI5, CI7, CI8, CI12, CI17 and A14. Still open: everything prod (CI1, CI3's
  prod half, CI9, CI17's prod half), the refusals and the branch-push context (CI4), the
  generation pin (CI5), the state lock (CI7), the comment update on a second push (CI8), a
  sensitive value in a posted plan (CI12), CI10, CI11, CI13, CI14, a foreign repository (A14),
  and the first plan and apply with T41's grants in code (P12).
- **The real-data path**: connect script and health-sync (I1, I4, I14 to I20, I23, A8), and prod
  (A20).
- **The public demo (D16)**, built offline by T43 to T48 and never deployed: P17 to P26, at its
  rollout to prod ("Rolling out the public demo").
- The deployer creating the Pub/Sub service agent itself (A22), prices (A16), and every item
  marked `partly`. Source abbreviations: **B** bootstrap, **PB** project-base, **ID** identity, **HC**
healthcare, **IN** ingestion, **EV** edge-vm, **OE** openemr, **PHR** phr, **HS** health-sync,
**SEED** services/seed, **SVC** services/README.md, **CG** connect-google-health, **CS**
cuff-sim, **V2F** v2-to-fhir, **FP** fhir-proxy, **web** web/README.md, **T25** this task, **T29** the smoke test
(`make smoke`, tests/e2e/README.md).

### Before and during the applies (steps 1 to 5, 7 and 8)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| A1 | Step 3's targeted apply creates project-base (and identity's service accounts) with no image variables set | T25 | 2026-11-16 dev: ok. 34 resources (project-base, identity's accounts), no image variables; needed a minted token ("Running the env roots from a service account in another project") |
| A2 | Step 4's targeted apply creates the eight empty secrets once Secret Manager is enabled | T25 | 2026-11-16 dev: ok. The eight secrets were created; the run then stopped on the Pub/Sub agent grant (A5) |
| A3 | The full plan on a clean project has no "Invalid count argument" for project-base's mirror grant (its count is now a plain bool; see "Findings") | T25 | 2026-11-16 dev: ok. Step 8's full apply planned and applied with no "Invalid count argument" |
| A4 | `health.googleapis.com` is the Google Health API's service name (it enables) | PB | 2026-11-16 dev: ok. `health.googleapis.com` enabled in step 3 |
| A5 | Service agents exist once the APIs are enabled; if an IAM binding to one fails, re-running the apply fixes it | HC | 2026-11-16 dev: **no** for Pub/Sub: the agent did not exist after the API was enabled, a re-run would not help (inferred: the apply failed once and the agent was then created by hand, without a second try) (`Service account service-<number>@gcp-sa-pubsub... does not exist`). Created by hand; since T36 module identity creates it (step 4). The Healthcare agent existed (A6) |
| A6 | The Healthcare topic-level publisher binding is accepted (S5 could not show it is needed) | HC | 2026-11-16 dev: ok. The Healthcare agent's publisher binding on the ingest topic was created (step 6) |
| A7 | The image build runs from a clean checkout and prints the tfvars block; the source bucket note (build account read access) | SVC | 2026-11-16 dev: partly. A by-hand build from the working tree (not a clean checkout) printed the tfvars block with five digests; the source-bucket note not tried (C7) |
| A8 | health-sync deploys while its secrets have no version yet (else: connect first, apply again) | IN | 2026-11-18 prod: **no**. The first full apply created 25 resources, then Cloud Run refused health-sync: "Error code 9 … spec.template.spec.volumes[0].secret.items[0].key: Secret …/pb-google-health-client-id/versions/latest was not found" (and the same for the client secret and the refresh token). With real data, run the connect script before step 8 or right after this error, then apply again (step 8) |
| A9 | A Cloud Run revision that never becomes Ready makes `terraform apply` fail and keeps the previous revision serving | IN, PHR | |
| A10 | Creating the default Hosting site (`site_id = project_id`) through the API works on a project that has none yet | PHR | 2026-11-16 dev: ok. Step 8 created the default site; the page deployed to `<project>.web.app` |
| A11 | The deployer can create a push subscription with an OIDC token (`iam.serviceAccounts.actAs` on pubsub-push) | HC | 2026-11-16 dev: ok. Step 10, as the deployer, turned the ingest subscription into a push subscription |
| A12 | ~~dev: the reviewer password from tfvars creates a version~~ Removed by T44 (D16): no secret value comes from tfvars any more | ID | n/a |
| A13 | Bootstrap on a brand-new project: the first apply may stop on "API has not been used ... or it is disabled"; a re-run continues; `add_firebase` adds Firebase with the owner's ADC (`billing_project` set); `firebase_added = true` | B | 2026-11-16 dev: partly. The first apply stopped on "API has not been used", but for the **caller's** project (a service account key from another project); `USER_PROJECT_OVERRIDE`/`GOOGLE_BILLING_PROJECT` fixed it; then 24 resources and `firebase_added = true`. An existing project was adopted; not run with a person's own login 2026-11-18 prod (adopting an existing project, the caller's own): the first apply stopped after 40 of 42 resources on the planner's custom role, "Identity and Access Management (IAM) API has not been used in project … or it is disabled" (it was being enabled in the same apply); a second apply 30 s later added the last 2. Firebase was added with the owner's credentials. |
| A14 | Workload Identity Federation: a workflow of the configured repository gets deployer tokens; one from another repository is refused; `google.subject` stays under 127 bytes for the branch names used | B | 2026-11-18 dev: partly. Workflows of the configured repository got tokens through the pool: the deployer (apply on main), the planner (pull request) and the page deployer (`dev-page`). A workflow from another repository not tried. `google.subject` length not inspected; the exchange succeeded for the refs used |
| A15 | IAP TCP forwarding works once `enable_iap_ssh = true` enabled `iap.googleapis.com` (understood to be required) | PB | 2026-11-16 dev: ok. With `enable_iap_ssh = true` (the firewall rule and `iap.googleapis.com`), an SSH tunnel through IAP reached OpenEMR on the VM (the README screenshot was taken through it); set back to `false` the same day |
| A16 | Prices against Google's pricing pages: Cloud NAT, the rejected Serverless VPC Access connector, Artifact Registry storage, Cloud Build minutes, snapshot storage, the e2-micro list price | PB, SVC, EV | |
| A17 | Cloud Scheduler in a project that first enabled the Scheduler API before 19 March 2019: its service agent may lack `roles/cloudscheduler.serviceAgent` (OIDC jobs fail); new projects are unaffected | ID | |
| A18 | Step 6's targeted apply creates the VM (and the dataset and HL7v2 store it needs) with no images and nothing of phr or ingestion; the OpenEMR client version exists before step 8 | T25 | 2026-11-16 dev: ok. 7 resources (topic and its publisher binding, dataset, HL7v2 store, data disk, address, VM), no images, nothing of phr or ingestion; the client key's version 1 existed before step 8 |
| A19 | Step 8 does not stop at fhir-proxy on `openemr-client-private-key` (and, for the record, whether Cloud Run refuses a volume secret with no version at all) | PHR, T25 | 2026-11-16 dev: partly. Step 8 created fhir-proxy without stopping (version 1 present); whether Cloud Run refuses a volume secret with no version is still open 2026-11-18 prod: the open half answered: Cloud Run **refuses** to create a service whose volume secret has no version (A8's error); fhir-proxy, created after the VM had stored version 1, deployed without error. |
| A20 | prod: the deployer (`roles/compute.instanceAdmin.v1`) can create the data disk's snapshot schedule and attach it; the first daily snapshot appears | EV, T25 | 2026-11-18 prod: partly. The deployer created the data disk's snapshot schedule and attached it (step 6, and again when the empty disk was replaced); the first daily snapshot not yet checked |
| A21 | The HL7v2 store parses with parser V1 as the spikes saw (V2 and V3 were never tried; the setting cannot be changed later) | HC | 2026-11-16 dev: ok. The store parsed the cuff's ORU^R01 (MLLP and HTTPS ingest) and v2-to-fhir mapped it to final Observations |
| A22 | The deployer can create the Pub/Sub service agent (`google_project_service_identity`, Service Usage `generateServiceIdentity`; Google names no permission for it); if refused, the owner runs step 4 once. On the deployed dev project the next apply adds only this resource and leaves the Token Creator grant unchanged | ID, T36 | 2026-11-16 dev: ok. The deployer's apply created `google_project_service_identity.pubsub` (1 added); on 17 November the same for the Healthcare agent (T38) 2026-11-18 prod: ok too (step 4 created it as the deployer, 21 added). |

### The image build (step 7)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| C1 | `automapSubstitutions` exposes `PROJECT_ID`, `TAG_NAME` and `COMMIT_SHA` to the step's `script` as well as the `_` substitutions | SVC | 2026-11-16 dev: ok. The by-hand build's script used `PROJECT_ID`, `TAG_NAME` (`v0.1.0`) and `COMMIT_SHA` (image paths, the tfvars header) |
| C2 | The step's docker client is already authenticated to `<region>-docker.pkg.dev` for the push and `docker buildx imagetools inspect` | SVC | 2026-11-16 dev: ok. Pushes to the services repository and `imagetools inspect` (the reported digests) worked in the step |
| C3 | The default `MUST_MATCH` substitution check accepts substitutions only the script uses | SVC | 2026-11-16 dev: ok. The build was accepted and ran to `DONE` |
| C4 | `gcloud builds submit` accepts `TAG_NAME` and `COMMIT_SHA` as `--substitutions` | SVC | 2026-11-16 dev: ok. `gcloud builds submit --substitutions TAG_NAME=...,COMMIT_SHA=...` was accepted |
| C5 | `roles/artifactregistry.reader` is enough to pull through the remote repository and cache new images there (also E6) | SVC, PB | 2026-11-16 dev: ok for pulls (E6): the VM's account, reader on the mirror only, pulled MariaDB and OpenEMR through it |
| C6 | Artifact Registry accepts `docker buildx imagetools create --prefer-index=false` (a tag on an existing manifest) on the immutable-tag repository and keeps the digest | SVC | 2026-11-16 dev: not exercised: every image was new, so no tag was added to an existing image |
| C7 | A manual `gcloud builds submit` with a user-specified build account needs read access to the source bucket; whether `gcloud builds log` prefixes lines with `Step #N - "...": ` | SVC, T25 | 2026-11-16 dev: partly. No step prefix (the block pastes unchanged); a by-hand build runs as the Compute Engine default account; the build account's source-bucket access not tried |

### The VM's first boot (step 6): edge-vm first

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| E1 | The VM boots COS with Shielded VM on an e2-micro; serial port shows `pulsebridge-edge: edge VM startup: begin`; metadata shows `cos-update-strategy=update_disabled` | EV | 2026-11-16 dev: partly. COS booted with UEFI Secure Boot enabled; `edge VM startup: begin` on the serial port; the `cos-update-strategy` metadata not inspected |
| E2 | Every command the script needs exists on COS (the preflight step logs any missing: `blkid`, `mkfs.ext4`, `fsck.ext4`, `mkswap`, `swapon`, `curl`, `dd`, `timeout`, `tr`, `wc`, `sha256sum`, `stat`, `flock`) | EV | 2026-11-16 dev: ok. `step preflight: ok` |
| E3 | The format decision on a real disk: the new disk reads as zeros, `blkid -p` exits 2, `wipefs --no-act` prints nothing, it is formatted once; after a reboot "has an ext4 filesystem; not formatting" | EV | 2026-11-16 dev: partly. "data disk /dev/sdb is blank (no signature, reads as zeros): formatting it", once; the reboot half not done (E13) 2026-11-17 dev: the reboot half ok. After a stop and start, and again on the replaced VM: "data disk /dev/sdb has an ext4 filesystem; not formatting". |
| E4 | Swap: `swapon --show` lists the file (swap on COS is not documented for Compute Engine) | EV | 2026-11-16 dev: partly. The script logged "swap file ... enabled"; `swapon --show` not run by hand 2026-11-17: "enabled" again after the reboot and on the replaced VM; `swapon --show` still not run by hand. |
| E5 | Artifact Registry and gcr.io over Private Google Access: both pulls succeed with no NAT (else `enable_cloud_nat = true`) | EV, PB | 2026-11-16 dev: ok. The adapter (gcr.io) and MariaDB and OpenEMR (Artifact Registry) pulled with no Cloud NAT |
| E6 | `roles/artifactregistry.reader` on the mirror is enough to pull through the remote repository | EV, PB | 2026-11-16 dev: ok. MariaDB and OpenEMR pulled through the mirror with the VM's reader grant |
| E7 | docker-credential-gcr writes under `/var/lib/pulsebridge-edge` and `docker pull` uses it | EV | 2026-11-16 dev: partly. The mirror pulls authenticated, so the helper works; where it writes not checked |
| E8 | (after step 8, which creates the store grant) The adapter stays up (`mllp-adapter is up and stayed up`), its port is reachable from cuff-sim through both firewalls, and a test message gets `AA` with only `roles/healthcare.hl7V2Ingest` on the store | EV, ID | 2026-11-16 dev: ok. `mllp-adapter is up and stayed up for 15s (port 2575)`; after step 8 cuff-sim's MLLP sends got `AA` |
| E9 | `--logtostderr`: a second adapter started by hand with it stays up (then consider adding it) | EV | |
| E10 | The MariaDB secret file appears with mode 600; the `sed` parsing of Secret Manager's real response works | EV | 2026-11-16 dev: ok. Three secret files "written ... (mode 600)"; MariaDB started with the parsed value |
| E11 | MariaDB initialises on the bind mount, becomes `healthy`, `--health-start-interval` is accepted; a configuration change replaces it with "Shutdown complete". Since T42 a first initialisation runs through the entrypoint wrapper (`[PulseBridge]: first initialisation` in `docker logs mariadb`; `first initialisation of a new database completed` in the startup log), and a half-initialised data directory is moved aside to `mariadb.incomplete-<time>` (edge-vm README, "First initialisation of MariaDB") | EV | 2026-11-16 dev: partly. `MariaDB is healthy`; replacement on a configuration change not tried 2026-11-18 prod: **the first initialisation failed twice** on new disks (the image's temporary server took over 33 s; its entrypoint waits 30 s; the database was left half-made and never healthy) and passed with T42: "MariaDB data directory … is HALF-INITIALISED …: moving it aside", then "MariaDB is healthy: first initialisation of a new database completed (52s into this run)". Edge-vm README, "First initialisation of MariaDB". |
| E12 | Graceful shutdown on VM stop: MariaDB's log on the next boot; `systemctl show docker -p TimeoutStopUSec` | EV | 2026-11-17 dev: partly. `gcloud compute instances stop` returned after 35 s and MariaDB came back healthy 42 s after its container was started; MariaDB's own shutdown log and `TimeoutStopUSec` not read (they need a shell) 2026-11-18: MariaDB's restart log (shipped to Cloud Logging) shows a clean start after that stop: no crash recovery, "End of log at LSN …", the buffer pool loaded from `ib_buffer_pool` (written at a clean shutdown), "ready for connections" at 03:20:02. Its shutdown lines themselves were not shipped; `TimeoutStopUSec` still not read. |
| E13 | After a reboot both containers come back and the script reports all steps ok | EV | 2026-11-17 dev: ok. After a stop and start the script ran from 03:19:14 to 03:21:30 UTC (2 min 16 s) and ended "done, all steps ok": the adapter "running with the current configuration; nothing to do", mariadb and openemr "exited; starting it". `make smoke` passed afterwards |
| E14 | `timeout` and curl's time limits behave on COS as in the sandbox | EV | |
| E15 | The logging agent ships the startup script's lines to Cloud Logging | EV | 2026-11-18 dev: ok. The startup script's lines reach Cloud Logging (`google_metadata_script_runner` on `gce_instance`), from the first boot to the replaced VM |
| E16 | The crash-loop check on a real crash (restart back-off, `restarting` status) | EV | |
| E17 | Patching: `apply -replace` keeps the data disk and the address; `boot_image` reports the new image | EV | 2026-11-17 dev: ok. `apply -replace` took 63 s (1 added, 1 destroyed); the data disk and the address were kept; the new VM's script ran from 03:40:00 to 03:48:38 UTC (the OpenEMR pull 7 min 10 s), did not format or seed, and found the client registered: about 10 minutes of downtime. The boot image moved to a newer `cos-stable` build, but `terraform output edge_vm` showed the image **family** until `terraform apply -refresh-only` (added to "Patching the VM"). `make smoke` passed afterwards |
| E18 | The e2-micro free tier applies (billing report after a month); the e2-micro list price outside it | EV | |
| E19 | Docker `--log-opt max-size` is compatible with the COS logging agent | EV | 2026-11-18 dev: ok. With `--log-opt max-size`, container output reaches Cloud Logging (`cos_containers`): over three days openemr, mariadb and mllp-adapter lines on stdout and stderr |

### The VM's first boot (step 6): then openemr

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| O1 | The hooks run: `pulsebridge-openemr[openemr]: begin` after `MariaDB is healthy`, then `hook openemr: ok` and `hook openemr-oauth-client: ok`; metadata has both `pulsebridge-hook-*` keys | OE | 2026-11-16 dev: ok. `[openemr]: begin` after `MariaDB is healthy`, then `hook openemr: ok` and `hook openemr-oauth-client: ok` |
| O2 | Host commands on COS the hooks use (`awk`, `cat`, `chmod`, `curl`, `date`, `docker`, `grep`, `head`, `iptables`, `ls`, `mkdir`, `mktemp`, `mv`, `rm`, `rmdir`, `sed`, `sha256sum`, `sleep`, `timeout`); `/run` writable and memory-backed | OE | 2026-11-16 dev: partly. Both hooks ran to `ok`, so the commands they use exist; `/run` not inspected |
| O3 | The OpenEMR pull through the mirror finishes within the deadline (record the time) | OE | 2026-11-16 dev: ok. The pull took about 12.5 minutes (12:05:38 to 12:18:06 UTC); the deadline is 39 |
| O4 | PHP in the image: `docker run --rm --entrypoint php <image> -m` lists `openssl`; it runs as `--user 0:0 --network none` | OE | |
| O5 | The sites seed keeps owner `1000:101` and mode `dr-x------` (`stat /mnt/disks/edge/openemr`); `docker logs openemr` shows "Setup Complete!" | OE | 2026-11-16 dev: partly. "sites directory seeded"; owner, mode and "Setup Complete!" not inspected 2026-11-18: "Setup Complete!" seen in the shipped container log (16 Nov, 12:23 UTC); owner and mode still not inspected. |
| O6 | The setup check: `first-time setup is complete (setup marker present, <n> access-control rules)`; `sites/default/docker-version` exists; `SELECT COUNT(*) FROM gacl_acl` > 0; `readyz` answers through `--cacert` (our certificate is served) | OE | 2026-11-16 dev: partly. "first-time setup is complete (setup marker present, 18 access-control rules)" and `readyz` 200 through the hook's `--cacert`; `docker-version` and the SQL count not inspected |
| O7 | The published port is reachable from the VM itself at `https://<internal_ip>` | OE | 2026-11-16 dev: ok. "FHIR API answers at https://<internal_ip>/apis/default/fhir (metadata 200)", from the VM |
| O8 | The metadata block: `sudo iptables -S DOCKER-USER` shows the rule; `docker run --rm --network pulsebridge-edge <image with curl> curl -m 5 -H 'Metadata-Flavor: Google' http://169.254.169.254/computeMetadata/v1/` times out; the adapter still gets `AA`; the startup script still reads secrets; `getent hosts mariadb` answers and `getent hosts example.com` times out in a container on the edge network; optionally `--publish <internal_ip>:443:443` | OE | 2026-11-16 dev: partly. "blocked the metadata server (169.254.169.254) for containers on pulsebridge-edge"; the adapter still got `AA` and the script still read secrets; the container-side curl and `getent` checks not run |
| O9 | Apache capped: `docker exec openemr ps` shows at most 5 `httpd` under a burst; `zz-lowmem.conf` loads after `mpm.conf` | OE | |
| O10 | Memory: `free -m` shows about 200 MB or more available at idle | OE | 2026-11-16 dev: not checked; journald reported "Under memory pressure" several times during the pull and setup |
| O11 | The settings: `OPENEMR_SETTING_* took effect`; `docker logs openemr` shows `Set rest_fhir_api to 1`; `GET /apis/default/fhir/metadata` is 200 | OE | 2026-11-16 dev: partly. The hook's checks were seen ("OPENEMR_SETTING_* took effect", FHIR metadata 200); the `docker logs openemr` line `Set rest_fhir_api to 1` was not looked at 2026-11-18: `Set rest_fhir_api to 1` seen in the shipped openemr container log on every boot (16 and 17 Nov), so this row is ok. |
| O12 | The database connection as `openemr` over TCP to 127.0.0.1 works; `oauth_clients` has `client_id`, `client_name`, `is_enabled` | OE | 2026-11-16 dev: ok. The hook registered and enabled the client in `oauth_clients` |
| O13 | The registration: 200 with a `client_id`; the client enabled; a token request from fhir-proxy (T15) succeeds with the RS384 assertion. Scopes: the six v1 scopes in the token; then try `openemr_scopes = ["system/Patient.crs", "system/Encounter.crs", "system/Observation.cr"]`, re-run the startup script, repeat a share (search, create, read work; `PUT`/`DELETE` refused); keep v2 if it works | OE | 2026-11-16 dev: partly. Registration and enable ok, and fhir-proxy's token requests worked (the shares, P-items); the v2-scope trial not done |
| O14 | Secret Manager: `addVersion` with `secretVersionAdder`; `GET versions/latest` with `viewer` reports `state` and `name` | OE, ID | 2026-11-16 dev: partly. `addVersion` with `secretVersionAdder` stored version 1; the `viewer` read of `versions/latest` not exercised on a first boot |
| O15 | fhir-proxy's TLS from Cloud Run: Python's default context with the certificate as `cadata`, hostname checking on, accepts `https://<internal_ip>` | OE | 2026-11-16 dev: ok. fhir-proxy's shares reached `https://<internal_ip>` (TLS with the stored certificate) |
| O16 | A reboot: the registration hook logs `nothing to do`; nothing is pulled, created or registered | OE | 2026-11-17 dev: ok. After the stop and start: images "already present", "sites directory ... has files; not seeding", the TLS certificate kept, and the registration hook: "registered, enabled, and its key is version 1 of the secret; nothing to do" |
| O17 | Time: first boot within `hook_timeout_minutes = 40`; record pull and setup times | OE | 2026-11-16 dev: ok. The openemr hook took about 18 minutes, the registration about 40 s; the whole startup about 20 minutes |
| O18 | A database wipe (dev only): a new client is registered and fhir-proxy recovers after re-reading its secret file | OE | |
| O19 | OpenEMR's first-time setup accepts the `openemr-admin-password` value | OE | 2026-11-16 dev: ok. First-time setup completed with the stored (generated) admin password |
| O20 | The password exposure (process arguments during setup and on every start, `getenv()`, `docker inspect`) is acceptable for this deployment | OE | |
| O21 | No dangling anonymous volumes after a container replacement (`docker volume ls -qf dangling=true`) | OE | |
| O22 | (dev) An interrupted first-time setup: reset the VM during setup; both hooks then fail with the recovery message, and nothing is wiped | OE | |
| O23 | COS's Docker uses the iptables (not nftables) backend and creates `DOCKER-USER`; the edge network's bridge is named `br-<12 characters of the network ID>` | OE | 2026-11-16 dev: ok. The hook's `DOCKER-USER` rule went onto the edge network's `br-<12 characters>` bridge |
| O24 | What Secret Manager's `versions/latest` resolves to when the newest version is disabled (either way the registration hook re-registers) | OE | |

### The setup step and ingestion (steps 9, 10 and 13)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| I1 | Connect script: Google spells the scope as expected; `access_secret_version` on `versions/latest` answers `NOT_FOUND` for a secret with no version; the deployer's ADC works with Secret Manager without extra quota-project setup | CG | 2026-11-18 prod: ok as far as seen. The connect script stored version 1 of the client ID, the client secret and the refresh token (15:55 to 15:56 UTC) with the owner's ADC; the scope and the empty-secret answer worked (the script ran to "Done.") |
| I2 | First seeding: `entry_statuses` shows `Patient 201` once (fresh store) and `Patient 200` for every data Bundle | SEED | 2026-11-16 dev: ok. Outcome `seeded`, 8,365 windows in 84 bundles, `Patient 201` once and then `200`, safety check `no-patient` |
| I3 | `roles/run.invoker` on the job lets Scheduler's OAuth call to `jobs.run` through | IN | 2026-11-17 dev: ok. Cloud Scheduler started the seed job every hour and every execution succeeded |
| I4 | First real sync: the Patient entry answers `200`, not `201` (else switch to the fixed-ID fallback in the T06 README and record it) | HS, T29 | 2026-11-18 prod: **ok**. health-sync's first run (16:06 UTC) wrote 35 windows and its Patient entry answered `200`: it matched the Patient the connect script wrote, so no fixed-ID fallback is needed. `make smoke` B2 passed |
| I5 | `GET Observation?identifier=urn:pulsebridge:observation\|<key>` returns one Observation whose `subject.reference` is the setup Patient; `GET Patient?identifier=urn:pulsebridge:patient\|<PATIENT_ID>` returns exactly one Patient | HS | |
| I6 | Second run: `stored_read: "ok"` and `windows_unchanged > 0` (health-sync and seeder); `stranded_preliminary` is a number (health-sync counts with `_total=accurate&_count=0` since T39, not `_summary=count`) | HS, SEED, T29 | 2026-11-17 dev: **failed for the seeder**. Every hourly run reported `stored_read: "ok"` but `windows_unchanged 0, windows_new 12, windows_changed 12, windows_amended 24, windows_written 48`: `_elements` named `effectivePeriod,valueQuantity`, which Cloud Healthcare silently leaves out (choice elements go by base name: `effective,value`). Fixed in T39; expected after it: new 12, finalised 12, unchanged 24, amended 0 (seed README). Dev's earlier synthetic final windows stay `amended`. Since T39 a read that returns windows without their end and values stops the run (`fhir-rejected`, `stored-elements-missing`, `stored_read: "failed:elements-missing"`, nothing written) instead of reporting `ok`; if that appears, the search is wrong, not the data. Not checked for health-sync (synthetic data; the smoke test's Part B skipped); it shares the fixed search **Redeployed and confirmed the same day:** with the fixed image, a run 13 minutes after the previous one reported `windows_unchanged 42, windows_new 3, windows_finalised 3, windows_changed 0, windows_amended 0, windows_written 6`, `stored_read: "ok"` (seeder only; health-sync has not run) 2026-11-18 prod: **ok for health-sync** (real data): the second run 44 s later reported `stored_read: "ok"`, `windows_unchanged 35`, `windows_written 0`, `stranded_preliminary 0` (a number). `make smoke` B3 and B4 passed. |
| I7 | `/status` answers within a second or two | HS | |
| I8 | Pub/Sub push and Cloud Scheduler reach the internal-only services (else the ingress fallbacks) | IN | 2026-11-16 dev: partly. Pub/Sub push reached internal-only v2-to-fhir (cuff messages became Observations); Scheduler's call not checked |
| I9 | The OIDC audience (the bare service URL) is accepted | IN | 2026-11-16 dev: partly. The push subscription's OIDC audience was accepted; health-sync's (real data) not reached |
| I10 | v2-to-fhir's `hl7V2Consumer` can patch message labels (else healthcare moves to `hl7V2Editor`); `fhirResourceEditor` includes `executeBundle` and update-create `PUT` | ID, HC | 2026-11-16 dev: partly. `executeBundle` worked for the seeder and v2-to-fhir; the label patch not checked on its own |
| I11 | The log filters match real entries (`jsonPayload.event`, `http_status`, `outcome`, the `bug` line, the seeder's stderr lines); a failed Scheduler attempt is logged at ERROR | IN | 2026-11-17 dev: partly. The seeder filter matched a real entry: one execution with an invalid `SYNTHETIC_SEED` override logged `event: "seed-run"` at WARNING (`outcome: not-started`, exit 2) and the metric counted 1. The Scheduler filter was not exercised (no failed attempt) 2026-11-18 dev: the Scheduler half ok. With the Scheduler account's `run.invoker` on the seed job removed, a forced attempt (`gcloud scheduler jobs run`) logged `AttemptFinished` at ERROR, status `PERMISSION_DENIED`, HTTP 403, and the metric counted 1. The removal took between one and eight minutes to take effect: an attempt one minute after it still succeeded. Restored afterwards; a plan showed no changes. |
| I12 | The email channel delivers without further confirmation, and a policy fires as intended | IN | 2026-11-17 dev: ok for the seeder policy. The email arrived within minutes of the failed execution, with no confirmation step for the channel. The Scheduler policy has not fired 2026-11-18 dev: the Scheduler policy ok too: its email arrived (confirmed by the owner). |
| I13 | Replay: `gcloud pubsub subscriptions pull --format=json` returns `message.data` base64-encoded; the `messages.list` label filter works | IN | 2026-11-18 dev: ok. `gcloud pubsub subscriptions pull --format=json` returns `message.data` base64-encoded (a harmless check message on the dead-letter topic, then acknowledged), so the replay loop's `base64 -d` is right. The REST `messages.list` filter `NOT labels."application_outcome":*` works on the server and matched an in-memory check of the labels (8 messages, all labelled: 7 `accepted`, 1 `error`; none pending), and `labels."application_outcome":*` returned all 8 |
| I14 | Connect script: the consent flow with a Desktop-app client and a random loopback port returns to `http://127.0.0.1:<port>/?state=...&code=...` | CG | 2026-11-18 prod: ok. The owner ran the connect script on their own computer with a Desktop-app client; the consent flow returned to the loopback listener and the script ended "Done." |
| I15 | Google accepts `access_type=offline` and `prompt=consent` with PKCE for a Desktop client; the token response carries `refresh_token` (and, in Testing, `refresh_token_expires_in`) | CG | 2026-11-18 prod: ok. The token response carried a refresh token (stored as version 1); `refresh_token_expires_in` not recorded here |
| I16 | `access_secret_version` answers `FAILED_PRECONDITION` when the latest version is disabled (treated as absent) | CG | |
| I17 | The identity call answers with `healthUserId` for the new access token | CG | |
| I18 | The FHIR store accepts the setup Bundle with the deployer's credentials; its response entry status starts `201` or `200` | CG | 2026-11-18 prod: ok. The connect script's Patient Bundle was accepted with the owner's credentials (the deployer's own roles were not used for it) |
| I19 | `connect.sh` runs on the deployer's own macOS or Linux machine (wheels for other platforms are in the lock but were not installed) | CG | 2026-11-18: ok on the owner's machine (operating system not recorded) |
| I20 | Whether publishing the OAuth app ("In production") requires verification for the health scope | CG | |
| I21 | Pub/Sub retries a failing push with backoff (10 s to 600 s) and after 10 failed deliveries moves the notification to the dead-letter topic, where the pull subscription keeps it | HC | |
| I22 | Cloud Healthcare keeps `meta.security` on conditional writes (informational: the seeder's safety check does not use it) | SEED | |
| I23 | health-sync: decimals round-trip exactly through the FHIR store; Google's 401/403/429 error bodies; whether a refresh ever returns a new refresh token; whether the token response always carries `scope` | HS | |
| I24 | v2-to-fhir: the real push body shape, and the status codes `executeBundle` returns for a refused transaction | V2F | 2026-11-16 dev: partly. v2-to-fhir accepted the real push body; the refused-transaction status codes not seen |
| I25 | cuff-sim: the derived MLLP message name matches the stored message (list the store); `hl7V2Ingest` alone allows `messages:ingest` with labels | CS | |

### The front door (steps 11 and 12)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| P1 | `TRUSTED_PROXY_HOPS`: two failed logins through Hosting (one plain, one with `X-Forwarded-For: 192.0.2.1`), directly at `fhir_proxy_url` one plain and one with `X-Forwarded-For: 192.0.2.1`; read `forwarded_for_entries` and `client_address` in the four `login-failed` lines; confirm the default 2 (through Hosting: 2 entries, your own address, never `192.0.2.1`), else set the measured value and re-apply | PHR | 2026-11-17 dev: ok, **2 hops**. Measured from a machine with no proxy in front. Through Hosting the proxy received 2 `X-Forwarded-For` entries both plain and with a made-up `X-Forwarded-For: 192.0.2.1`, so the count alone could not tell whether Hosting replaces the caller's header or drops the client. With `trusted_proxy_hops = 2` applied, the same made-up request logged the measuring machine's own public address as `client_address` (confirmed by its owner), not `192.0.2.1`: Hosting replaces a caller's header with the client it saw, and the second entry from the right is the visitor. With 1 hop the key was a Google (Hosting) address that changed between requests, so visitors shared buckets. dev now runs with 2. (An earlier attempt from a session behind an egress proxy was not conclusive; directly at `fhir_proxy_url` there is 1 entry, so 2 hops is right only for the Hosting path, which is the only one the page uses). Directly at `fhir_proxy_url` with 2 hops configured (same day, from a session behind an egress proxy, so the right-most entry was not that session's own address): plain, 1 entry; with `X-Forwarded-For: 192.0.2.1`, 2 entries and `client_address` `192.0.2.1`; with `X-Forwarded-For: 192.0.2.1, 198.51.100.9`, 3 entries and `client_address` `198.51.100.9`. Cloud Run appends to a caller's header, so under 2 hops a direct caller chooses its key (module phr README, "The client address and the direct run.app URL"). Since T40, 2 is the default of the env roots and module phr, so a deployment that sets nothing gets it |
| P2 | `Authorization` reaches the proxy: after login, `/api/status` is 200, not 401 | PHR | 2026-11-16 dev: ok. After login through Hosting, `/api/status` answered and the status strip showed |
| P3 | The rewrite path: `/api/fhir/metadata` with the token gives the CapabilityStatement; `/api/unknown` gives the proxy's OperationOutcome; search links start with `/api/` | PHR | 2026-11-17 dev: ok. `/api/fhir/metadata` 200 with a `CapabilityStatement` (4.0.1); `/api/unknown` 404 with an `OperationOutcome`; the search's `self` and `next` links start with `/api/` |
| P4 | cuff-sim's and health-sync's `run.app` URLs refuse outside callers even with a valid token; a cuff send through the page works; the status strip shows health-sync's state | PHR, IN | 2026-11-16 dev: partly. A cuff send over HTTPS through fhir-proxy worked; outside calls to the `run.app` URLs not tried; no health-sync (synthetic) |
| P5 | MLLP through the VPC: a cuff send over MLLP returns `AA` | PHR, EV | 2026-11-16 dev: ok. A cuff send over MLLP returned `AA` |
| P6 | The secret mount (D9's reviewer password, which T44 replaced by the session signing key; module phr's "Verify on the first real deploy", item 6, is the new check): login 503 before a version; works within about 30 s after `scripts/secrets/set-reviewer-password.sh`; an old token is refused after a second new version | PHR | 2026-11-18 dev: ok for rotation. The owner set a new password with `scripts/secrets/set-reviewer-password.sh` (version 3, 11:16:18 UTC); 37 s later the new password logged in (200) and the previous one was refused (401), with no restart; the owner's open dashboard session was signed out. Not checked: login 503 before any version exists. Note: the script leaves earlier versions enabled (they no longer log in, but stay readable to anyone with access to the secret). **T44 (D16) replaced the reviewer password**: the same mount now carries the session signing key; module phr's "Verify on the first real deploy", item 6, is the new check |
| P7 | CSP in the browser: zero violations through login, every tab, the chart, a cuff send; `/` carries the policy and `frame-ancestors 'none'`; `/.vite/manifest.json` is 404 | PHR | 2026-11-16 dev: partly. Login, status strip and chart in Chromium with no console errors; the other tabs, a cuff send from the page, the headers and the manifest 404 not checked |
| P8 | The 60 s limit: a forced slow cuff send gets the proxy's own 504 within about 55 s, and its AuditEvent appears | PHR | |
| P9 | Direct VPC egress cold starts: time the first `/api/status` and the first MLLP send after 20 idle minutes | PHR | 2026-11-18 dev: ok. After 20 minutes with no request reaching fhir-proxy (checked in its request log), from 11:38 UTC: first request (login) 3.78 s, first `/api/status` 0.94 s, first cuff send over MLLP 12.16 s with ACK `AA` (cold proxy, cuff-sim and adapter path), then a warm `/api/status` 0.22 s |
| P10 | Startup probes: a deliberately broken copy (`gcloud run deploy <name>-probe-check --no-traffic`, one variable missing) fails and the previous revision keeps serving | PHR | 2026-11-18 dev: ok, as a separate copy. A throwaway internal-only copy of fhir-proxy's service with `FHIR_STORE_ID` removed (`gcloud run services replace`) failed to deploy: the container exited with code 1 and the startup probe refused it ("failed to start and listen on the port"); the copy was deleted and the real service kept serving. Not tried as a new revision of the managed service itself |
| P11 | A request through Hosting reaches a service whose invoker check is disabled, with no further setting | PHR | 2026-11-16 to 18 dev: ok. Every `/api` request through Hosting (login, search, traces, cuff sends, shares, `make smoke`) reached fhir-proxy, which has the invoker check disabled, with no further setting |
| P12 | `roles/firebasehosting.admin` is enough for `firebase deploy --only hosting` with the Cloud Run rewrite; the Firebase CLI accepts WIF credentials in CI (T26) | PHR | 2026-11-16 dev: not checked: the page was deployed with an Owner's key, not the deployer. 2026-11-18 dev (first CI run): **no, then ok after two grants**. The Firebase CLI accepted the page deployer's WIF credentials. With `roles/firebasehosting.admin` only, `firebase deploy` was refused twice: (1) 403 "Permission denied to get service" on `serviceusage.googleapis.com/v1/projects/<p>/services/firebasehosting.googleapis.com`, fixed with `roles/serviceusage.serviceUsageViewer` on the project; (2) after uploading the 5 files, finalizing the version (`PATCH .../versions/<v>?updateMask=status,config`) gave 403 "Permission 'run.services.get' denied on resource 'namespaces/<number>/services/<prefix>-fhir-proxy'", fixed with `roles/run.viewer` on the fhir-proxy service only. Both were granted by hand (gcloud, as Owner); the third attempt passed and the site served the new page. T41 puts both grants in code (bootstrap; the env roots' `page_deployer_service_account_email`) |
| P13 | `ALL_TRAFFIC` egress reaches Google APIs through Private Google Access, and the internal-only services accept the proxy's calls as internal | PHR, IN | 2026-11-16 dev: ok. fhir-proxy reached the Healthcare API and the internal-only cuff-sim |
| P14 | Network: Cloud Run Direct VPC egress traffic is admitted by the subnet-range firewall rule (tags and service identity cannot be the source); a /24 is plenty | PB | 2026-11-16 dev: ok. cuff-sim's MLLP sends and fhir-proxy's shares reached the VM from the serverless subnet |
| P15 | fhir-proxy's Cloud Healthcare searches: `gt`/`lt` on `effectivePeriod`, `_sort=date`, `_page_token` reuse, `_summary=count`, `_sort=-_lastUpdated` with `_elements=id,meta`, identifier OR lists, AuditEvent `type`/`subtype`/`date`/`_sort=-date`, Provenance `recorded` and `_include=Provenance:target`; the indexing delay for non-identifier searches | FP | 2026-11-16 dev: partly. `_summary=count` is **refused** (fhir-proxy now counts with `_total=accurate&_count=0`, fixed before T36); a heart-rate search sorted by date works; the others not checked |
| P16 | fhir-proxy: HL7v2 `messages.list` with a `create_time` filter and `view=RAW_ONLY`; the identity-token audience; a mounted secret file reflects a new `latest` without a restart; `fhirResourceReader` allows every search above | FP | 2026-11-18 dev: mostly ok. The messages log (`messages.list` with a `create_time` filter, `view=RAW_ONLY` reads) works through `make smoke` and the dashboard; fhir-proxy's identity token to cuff-sim is accepted (every cuff send); the mounted reviewer-password file reflected a new `latest` without a restart (P6); `fhirResourceReader` allowed every search the smoke test and dashboard make. The audience value itself was not inspected |
| P17 | The `__session` cookie through Hosting's rewrite, both ways: after "View the live demo" the browser holds `__session` with `Path=/api`, `HttpOnly`, `Secure`, `SameSite=Strict` and `Max-Age=86400` (Hosting passed the proxy's `Set-Cookie` back); `/api/status` then answers 200, not 401 (Hosting passed `Cookie` on); "Log out" removes it; a visit to `/privacy.html` and back keeps it; no `/api` answer is cached (`Cache-Control: no-store`) | PHR, FP | 2026-11-20 prod: ok, mostly. Through Hosting the proxy's `Set-Cookie` arrives intact (seen on `POST /api/auth/logout`: `__session=…; Path=/api; Max-Age=0; HttpOnly; Secure; SameSite=Strict`, with `Cache-Control: no-store`), and Hosting passes `Cookie` on (the owner's browser sessions and the smoke test's S1 were accepted). The owner's browser opened the dashboard after "View the live demo", logged out back to the landing view, and opened the privacy page. The attributes of the cookie set on a pass were not read in the browser |
| P18 | The assessment call: a click gives 200 and a `session-created` line with a score and `checks_today`; `curl` with a made-up token gives 403 and `bot-check-failed` with `reason=invalid-token`, `invalid_reason=MALFORMED` (the endpoint, the request body, `roles/recaptchaenterprise.agent` and the answer's field names); `tokenProperties.hostname` is the bare host; the key accepts both `<site>.web.app` and `<site>.firebaseapp.com`; the assessment is in `GCP_PROJECT_ID` | FP, PHR | 2026-11-20 prod: ok. A made-up token gave `403` and a `bot-check-failed` line with `reason=invalid-token`, `invalid_reason=MALFORMED` (so a junk token is a 200 assessment with `valid: false`, as assumed); two clicks in the owner's browser gave `session-created` lines, so the endpoint, request body, `roles/recaptchaenterprise.agent`, the field names and the bare-host hostname check all work. Only the `web.app` host was tried |
| P19 | The score real browsers get: the `score` of a few `session-created` lines (a desktop browser, a phone, a private window, a browser with a content blocker); none of them refused at `recaptcha_min_score` 0.5, else lower it and record why | FP, PHR | 2026-11-20 prod: partly. The owner's browser: score 0.9 on both sessions (threshold 0.5). A phone, a private window and a content blocker not yet tried |
| P20 | `make smoke` (step 14) with `PULSEBRIDGE_TOKEN_ISSUER` from the `token_issuer` output: `SMOKE RESULT: PASSED`, the locally signed sessions accepted (S1), the junk token refused after a real assessment (S1b), the negatives in their second session not rate-limited | T29, FP | 2026-11-20 prod: ok. `make smoke --allow-real-data-writes` with `PULSEBRIDGE_TOKEN_ISSUER` from the `token_issuer` output: PASSED, 39 passed, 0 failed, 1 skipped (A5, synthetic patient, as expected on real data); S1 accepted the locally signed session, S1b's junk token was refused after a real assessment, and the negatives in their second session were not rate-limited |
| P21 | The CSP with reCAPTCHA in the browser: zero violations while `enterprise.js` and its iframe load, the click, the dashboard and a cuff send; whether reCAPTCHA adds an inline style the policy (`style-src 'self'`) refuses, and if so whether anything visible breaks (the badge is hidden anyway); `/privacy.html` loads with no violation and no request off the site | PHR, web | 2026-11-20 prod: ok as far as seen. The owner's browser console showed no CSP violation through the landing view, the click, the dashboard and the privacy page; its one red line is the expected `401` of the start-up session check (`GET /api/status` before any session; T45's design). Whether reCAPTCHA adds an inline style that `style-src 'self'` refuses was not looked for; nothing visible broke |
| P22 | The `Origin` header through Hosting: a `POST` through Hosting arrives with the page's origin and passes; `curl -X POST` with `Origin: https://example.org` gets `403 origin-refused` and an `origin-refused` log line | FP | 2026-11-20 prod: ok. The page's `POST /api/auth/session` through Hosting passed the Origin check (sessions were created); `curl -X POST` with `Origin: https://example.org` got `403` and an `origin-refused` log line |
| P23 | The counter bucket under two instances: the first check of the day creates `bot-checks/<YYYY-MM-DD>` and later checks count up (`metadata.count`, which needs `storage.objects.delete` as well as `create`); under a burst of sessions that wakes the second instance, the count equals the number of `session-created` plus `bot-check-failed` lines (no increment lost to a race); `gcloud storage buckets describe` shows public access prevention `enforced` and the 7-day delete rule | FP, PHR | 2026-11-20 prod: partly. The first check of the day created `bot-checks/2026-11-20` and later checks counted up (3 after one junk token and two sessions), so the bucket role covers the overwrite. The burst that wakes a second instance was not tried |
| P24 | The rate limits on a normal visit: page load, the 7-day chart, three cuff sends, two shares and an idle hour log no `rate-limited` line; with two instances running, each counts on its own (the limits double, as documented) | FP | 2026-11-20 prod: partly. The owner's visit and a full `make smoke` run logged no `rate-limited` line. Three cuff sends and two shares in one browser session, an idle hour and two instances not tried |
| P25 | T47's window against Cloud Healthcare: `date=ge<boundary>` on `effectivePeriod` matches a heart-rate window that overlaps the boundary; `date` beside `_total=accurate&_count=0` (the status counts) and beside `_sort=-_lastUpdated`; a `_page_token` sent with a `date` a few seconds later than it was issued with; `recorded=ge` with `_include=Provenance:target`; nothing older than 7 days in the chart, the log or the counts | FP | 2026-11-20 prod: partly. The 7-day searches work against Cloud Healthcare (the dashboard showed the owner's data; `/status` counted 583 watch and 1 cuff Observations, so `date` beside `_total=accurate&_count=0` is accepted). A window overlapping the boundary, `_sort=-_lastUpdated` with `date`, a later `_page_token` and `recorded=ge` with `_include` not checked separately |
| P26 | What the privacy note says about logs: Cloud Run's request log for an `/api` call through Hosting (which `remoteIp` it records, the visitor's or Hosting's, and that it holds no cookie); the proxy's lines carry a `client_address` only in `rate-limited`, `session-rate-limited` and `origin-refused`; `gcloud logging buckets describe _Default --location=global` shows 30 days' retention; Hosting is not linked to Cloud Logging | PHR, FP, web | 2026-11-20 prod: mostly ok. `_Default` keeps logs 30 days. Cloud Run's request log for `/api/status` through Hosting records the visitor's own address for the owner's browser requests (classified, not printed: outside Google's and Fastly's ranges), and no cookie in `httpRequest`. An `origin-refused` line carries `client_address`, as documented. Hosting's own request logs: not linked to Cloud Logging (not changed) |

No row for a quota override: Fraud Defense has no per-day assessments quota that Terraform could
lower (T44 read Google's "Quotas and limits" page), so the bucket counter is the only daily cap.

### Teardown (dev)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| T1 | `terraform destroy` removes everything listed above; the serverless subnet fails at first and goes on a retry 1-2 hours later | PHR, T25 | 2026-11-19 dev: ok, with one difference. The destroy (CI, see CI11) removed all 95 resources in under 2 minutes, and the state was empty afterwards. The serverless subnet did **not** fail: it was deleted in 13 seconds on the first try, shortly after its Cloud Run services were deleted earlier in the same run, then the edge subnet and the VPC. So the 1-2 hour wait may not apply every time; the retry stays as a safety net. Left afterwards, as listed above: Google's automatic `default` network (not ours), the enabled APIs, the default Hosting site, `gs://<project>_cloudbuild` and bootstrap's state bucket |
| T2 | The Artifact Registry repositories delete while holding images | T25 | 2026-11-19 dev: ok. Both repositories (the services repository with the images dev ran, and the Docker Hub mirror holding the cached OpenEMR and MariaDB images) deleted without error, in 0 and 1 seconds |
| T3 | After destroy the default Hosting site still serves the last page; `firebase hosting:disable` stops it | PHR | 2026-11-19 dev: ok. After the destroy the site still answered 200 with the last page, and `/api/status` answered 404. A `SITE_DISABLE` release (what `firebase hosting:disable` creates) took it offline: the site then answered 404 |
| T4 | Nothing billable is left: the billing report shows no charges for the project after a day (AC "destroy leaves nothing billable") | T25 | 2026-11-20 dev: ok. The billing report for the dev project shows no new charges after 19 November, the day of the destroy (checked by the owner in the console, Billing → Reports, filtered to the project). Left afterwards: bootstrap's state bucket (a few KB), which goes with bootstrap's teardown |

### CI/CD's first real run

In this order (each needs the ones before). "CI" is T26; "B" bootstrap; "PB" project-base.

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| CI1 | prod's bootstrap plan shows exactly the listed changes: the condition (ref and environments), the `ci_context` mapping, the deployer binding moving to `env-prod-main`, the planner and page deployer with their roles and bindings | CI, B | |
| CI2 | With the variables unset, every authenticating job shows as skipped and each workflow run is green | CI | 2026-11-18 dev: partly. Before the `DEV_` variables were set, the Terraform apply runs on main (runs 7 and 8) concluded **skipped**; in the 18 November apply run every prod job skipped (no `PROD_` variables) while dev's jobs ran. The plan workflow before the variables was not checked |
| CI3 | `prod-preflight` passes with the three prod Environments (and `dev-preflight` with `dev-page`) configured as in step 2, and fails with a clear error when `prod` has no reviewer or uses "Protected branches only" (try both once). The GET answer carries `can_admins_bypass` (GitHub's schema does not list it; without it the preflight always fails) and each branch policy's `type` | CI | 2026-11-18 dev: partly. `dev-preflight` passed with `dev-page` configured as in step 2 (main only, admin bypass off, no reviewers), so (inferred: the preflight fails without them) the GET answer carried `can_admins_bypass` and the branch policy's `type` for that Environment. `prod-preflight` and the two failure cases not tried. 2026-11-18 prod: `prod-preflight` passed with the three prod Environments configured as in step 2 (the failure cases still not tried) |
| CI4 | WIF: the `ci_context` mapping is accepted by Google (CEL `"environment" in assertion`, `in` lists, nested ternaries) and evaluates as documented: a pull request is `pull-request`, a push to main `main`, a `prod-plan` job on main `env-prod-plan-main`, a branch push `other`; the planner cannot be reached from `main`, the deployer not from `pull-request` | CI, B | 2026-11-18 dev: partly. Google accepted the mapping, and three contexts evaluated as documented: the pull request's plan job reached the planner (`pull-request`), the apply on merge the deployer (`main`), the page job in `dev-page` the page deployer (`env-dev-page-main`). A branch push (`other`), `env-prod-plan-main`, and the refusals (planner from `main`, deployer from `pull-request`) not tested |
| CI5 | `fetch-tfvars`: the 5-minute access token reads `ci/<env>.tfvars`; the answer carries `x-goog-generation`; `&generation=` returns that version; the masked values do not appear in the log | CI | 2026-11-18 dev: partly. The 5-minute token read `ci/dev.tfvars` in the plan job and the apply job; the access token minted from the deployer's credentials showed masked as `***`, and the logs showed no access token, patient identifier or personal email (they do show the project ID, project number and service account emails). `x-goog-generation` and `&generation=` not checked: the generation pin is used only by `prod-apply` |
| CI6 | The Firebase CLI deploys with the page deployer's external-account credentials, from `ci/deploy-tools` with `--config <checkout>/web/firebase.json` (an absolute path; `public: dist` must resolve next to firebase.json), and `roles/firebasehosting.admin` alone is enough with the Cloud Run rewrite (also P12). `npm ci --ignore-scripts` skips the install scripts of `protobufjs` and `re2` (optional), the two packages of the locked tree that have one: the CLI must still work without them | CI, PHR | 2026-11-18 dev: **ok after the two grants of P12**. The Firebase CLI from `ci/deploy-tools` (npm installed the deploy tools) deployed with the page deployer's external-account credentials and the absolute `--config` path: the second attempt already uploaded the 5 files, the third passed, and the site served the new page (last-modified at the deploy time). `roles/firebasehosting.admin` alone was **not** enough (P12). Whether the CLI needs the skipped install scripts of `protobufjs` and `re2` was not checked separately: only that the deploy worked with `npm ci --ignore-scripts` as `tf_ci.py` runs it |
| CI7 | `terraform init` with `-backend-config=bucket=...` and the external-account credentials; the deployer's plan takes and releases the state lock; the planner's `-lock=false` plan works | CI | 2026-11-18 dev: partly. `tf_ci.py init` with the backend config and the external-account credentials worked for the planner and the deployer; the planner's `-lock=false` plan completed ("No changes"); the deployer's apply completed in 16 s with no changes. Whether the apply took and released the state lock was not inspected |
| CI8 | The plan comment appears on the pull request and is updated, not duplicated, on the next push (`gh pr comment --edit-last --create-if-none`) | CI | 2026-11-18 dev: partly. The "Post the plan on the pull request" job posted one comment ("### Terraform plan: dev ... 0 resource change(s). Planned by the read-only planner ...", with "No changes." in a details block). Only one push followed the fix, so update-not-duplicate was not observed |
| CI9 | The prod plan's job summary is readable before approving; approving runs the apply; the digest matches when nothing changed (if refreshed attributes make it differ every time, record which); rejecting applies nothing; a second merge during the wait makes the first apply refuse (not tip) | CI | 2026-11-18 prod: partly. The `Plan prod` job summary was readable before approving; approving ran `prod-apply`, which re-planned and found the same digest (`No changes`, the digest matched, so refreshed attributes do not make it differ). Rejecting and a second merge during the wait not tried |
| CI10 | An apply longer than an hour (or the destroy) keeps working: the credential file fetches fresh OIDC tokens | CI | |
| CI11 | The nightly destroy: the 03:17 run leaves at most the serverless subnet and the VPC; the 06:17 run removes them; a partly built dev is left alone; afterwards T1 to T4 | CI, T25 | 2026-11-19 dev: partly. GitHub started the scheduled runs hours late (the 03:17 UTC run started at about 11:40 UTC on 16 to 18 November, the 06:17 run at about 15:50; a schedule is not a promise), so the destroy was started by hand (`workflow_dispatch` on main, the first run's rules). It passed: everything was destroyed, nothing was left, so the retry had nothing to do. The retry run's `--final` check and the partly-built guard were not exercised. T1 to T3 ok; T4 open |
| CI12 | The redaction check: the posted plan shows `(sensitive value)` for the patient and the alert address, and no tfvars value | CI | 2026-11-18 dev: partly. The comment says sensitive values show as `(sensitive value)`, but the plan had no changes, so no sensitive value appeared to check; no tfvars value was seen in it |
| CI13 | Trigger: the deployer (with `allow_build_trigger`) can create it on a 2nd-gen repository (if refused on `cloudbuild.repositories.get`, add `roles/cloudbuild.connectionViewer`); a tag push starts a build that waits for approval, then runs as the build account; then C1 to C6 | CI, PB, SVC | |
| CI14 | The cleanup policy is accepted with `older_than` in seconds and `keep_count`; the newest ten versions are never deleted; whether Cloud Run still starts instances of a revision whose image the policy deleted | CI, PB | |
| CI15 | The auth action's `access_token_lifetime: 300s` is accepted, and `token_format: access_token` and the credential file work together in one step | CI | 2026-11-18 dev: ok. `access_token_lifetime: 300s` with `token_format: access_token` was accepted in the two jobs that use it (Plan dev, Apply dev): the same step's token fetched the tfvars and its credential file ran `terraform`. The page job's auth step (no access token) also passed |
| CI16 | The planner's custom role suffices for a full `terraform plan` of dev and of prod (if refused, the error names the permission: add it to `planner_extra_permissions` if it is a get or getIamPolicy that reads no data) | CI, B | 2026-11-18 dev: **no at first, then ok**. The first CI plan was refused on `serviceusage.services.list` for every `google_project_service` (the provider refreshes them by listing the enabled services). That is a `.list`, which `planner_extra_permissions` does not accept, so it was added to the built-in list. With it, a full plan of dev as the planner completes: "No changes". It was the only permission missing for dev (synthetic data); prod and a real-data stack are not yet checked |
| CI17 | Artifacts: `web-dist`, `page-config-<env>` and `plan-<env>` upload and download within the run; `prod-page` starts without a further approval after `prod-apply`, in its own Environment, and its token reaches only the page deployer | CI | 2026-11-18 dev: partly. `web-dist` and `page-config-dev` uploaded and downloaded within the apply run; `plan-dev` within the pull request's run (the comment job downloads it and posted the comment). `plan-prod`, `page-config-prod` and `prod-page` not exercised (no `PROD_` variables). 2026-11-18 prod: ok. `plan-prod` and `page-config-prod` uploaded and downloaded within the apply run; `prod-page` started without a further approval after `prod-apply`, in its own Environment, and deployed the page on the first attempt (the site's last-modified time matched the run) |

## Findings for the modules

Found while wiring T25; the first four were fixed in the modules in T25 (each module's README
and tests updated), the last two in T26:

1. **project-base: the mirror grant's `count` depended on an unknown value** (`... &&
   var.edge_vm_service_account_email != null`). A service account's email is unknown until the
   account exists, so the first plan of a root failed with "Invalid count argument". Fixed: the
   new bool `grant_edge_vm_mirror_reader` (default true) decides the count, and a precondition
   requires the email. `tests/unknown_email/` in project-base plans the module with an email
   that is unknown at plan; `_stack/tests/stack.tftest.hcl` applies the whole stack the same way.
2. **openemr / phr: `client_secret_id` formats disagreed** (full path versus phr's short ID).
   Fixed: openemr outputs `client_secret_name` (the short ID); the stack passes it.
3. **openemr: `scopes` was nullable**, so null overrode its default. Fixed: `nullable = false`.
4. **edge-vm: no protection for prod's data disk.** Fixed: `data_disk_snapshot_days`; the stack
   passes 7 in prod (a daily snapshot schedule kept 7 days) and 0 in dev.
5. **ingestion: `alert_email` could not be sensitive**, because it decided a `for_each`. Fixed in
   T26: the bool `create_alert_email_channel` decides it, and the address is sensitive
   ("What a plan shows").
6. **Cloud Build had no build account or trigger.** Fixed in T26 (opt-in): module identity's
   build account, project-base's grants, cleanup policy and trigger, and
   `roles/cloudbuild.builds.editor` in bootstrap's `deployer_roles` (step 7).

Found on the first dev deploy (16 November 2026); 7 to 9 were fixed on the way, each with a test,
10 in T36 and T38, and 11 in T38:

7. **scripts/secrets: a gcloud warning read as an existing version.** For a secret with no
   versions, real gcloud prints a filter warning on standard error, which `set-secret.sh` took
   for an enabled version. Fixed in `lib.sh`.
8. **ingestion: alert policies refused.** Cloud Monitoring requires a `resource.type` restriction
   in a log-based metric's filter. Fixed in `alerts.tf`.
9. **fhir-proxy: `_summary=count` refused by Cloud Healthcare.** `/status` now counts with
   `_total=accurate&_count=0`.
10. **identity: the Pub/Sub service agent did not exist on a new project**, so the Token Creator
    grant failed (step 4). Fixed in T36: module identity creates it first (A5, A22). Since T38
    module healthcare takes the agent from identity's `pubsub_service_agent_email` output, which
    depends on that resource, so a single full apply on a new project also creates the agent
    before healthcare's two grants to it. Healthcare creates the Healthcare agent itself
    (`google_project_service_identity.healthcare`) before granting it; on dev that agent existed
    already after the API was enabled (A6), so this is a safeguard (healthcare README).
11. **healthcare: enabling an unrelated API replaced the three agent grants.** With
    `enable_iap_ssh = true` (one more API and one firewall rule in project-base), a full plan
    said `module.stack.module.healthcare.data.google_project.this will be read during apply` and
    `must be replaced` for `google_pubsub_topic_iam_member.healthcare_agent_publisher`,
    `google_pubsub_topic_iam_member.pubsub_agent_dead_letter_publisher` and
    `google_pubsub_subscription_iam_member.pubsub_agent_ingest_subscriber`. Cause: the stack gave
    module healthcare `depends_on = [module.project_base]`. A module-level `depends_on` makes every
    data source in the module depend on everything in project-base, and Terraform reads a data
    source only at apply while anything it depends on has a pending change; the project number,
    and so the grants' `member`, became "(known after apply)", and a changed-or-unknown `member`
    forces replacement. On dev it was avoided with a targeted apply of the two IAP resources.
    Fixed in T38: healthcare has no data source (the number comes from module identity, whose
    data source nothing defers), the module-level `depends_on` is gone, and an `enabled_apis` input
    orders only the resources that need the APIs. The first full plan on the deployed dev project
    after T38 shows **1 to add** (`module.stack.module.healthcare.google_project_service_identity.healthcare`),
    **0 to change, 0 to destroy**: the members are byte-identical (the same
    `serviceAccount:service-<number>@gcp-sa-...` strings, the number now read by identity's data
    source for the same project). Toggling `enable_iap_ssh` afterwards plans only project-base's
    two IAP resources (2 to add, or 2 to destroy when turning it off) and no read during apply; a
    targeted apply is no longer needed. Tests: `_stack/tests/stack.tftest.hcl` (members known at
    plan on a new project and after the IAP and Cloud NAT changes), `plan_scan.py` (those plans
    change nothing outside project-base and defer no read), `check_agent_ordering.sh`.

Found on the first CI run on dev (18 November 2026), fixed in T41:

12. **bootstrap / phr: the page deployer could not deploy the page** with
    `roles/firebasehosting.admin` alone (P12, CI6): the Firebase CLI checks the Hosting API with
    `serviceusage.services.get`, and finalizing a version with a Cloud Run rewrite needs
    `run.services.get` on the target. Both were granted by hand on dev, then put in code: bootstrap
    grants `roles/serviceusage.serviceUsageViewer`, and module phr grants `roles/run.viewer` on
    fhir-proxy when the env root's `page_deployer_service_account_email` is set.

## Tests

```bash
make tf-validate   # fmt -check and validate in _stack, dev, prod (init -backend=false)
make tf-test       # everything below, with Google credentials removed from the environment
make tf-lint       # T26: tflint over infra/ (.tflint.hcl)
make tf-scan       # T26: Trivy's Terraform checks over infra/; HIGH and CRITICAL fail
```

| File | Checks |
| --- | --- |
| `_stack/tests/stack.tftest.hcl` | the composition under mocked providers: four scenarios (real or synthetic, ingest off or on) and a prod one, applied in the mock with the service accounts' emails unknown at plan (as on a real first plan, so "Invalid count argument" would show); push and Scheduler follow `ingest_enabled`; the right workloads per mode; project-base gets the edge VM's account; the mirror stays on; edge-vm gets openemr's two hooks and 40 minutes; openemr gets the VM's reserved address; one patient for ingestion and phr; the `setup_env` and `next_steps` outputs; a full plan needs the images; each targeted step (3, 4, 6) plans without them; T26: an alert channel only with an address, image builds off by default, the build account's two grants and the trigger when enabled, the trigger refused without the account; T38: healthcare's three agent grants' members are known at plan on a new project, and stay known and unchanged when project-base enables an API (IAP SSH) or adds Cloud NAT on top of an applied state |
| `_stack/tests/plan_scan.py` | the scenario runs' full state (every resource in every module), with the scenario table required to equal the state-producing runs: no `allUsers`/`allAuthenticatedUsers`, no key, no authoritative IAM, one project-level grant; exactly one public service; every Cloud Run service and the job run as their own account and get the literal project, location, dataset and store IDs they should (and not the ones they should not: cuff-sim has no `FHIR_STORE_ID`); Scheduler jobs and push config per `ingest_enabled`; `PATIENT_ID` and the authority equal everywhere; the synthetic name; firewall target, ports and the mirror grant; the VM's account, address, subnet, no external IP, hooks, hook limit, secret files and adapter flags; cuff-sim's MLLP host and port; fhir-proxy's settings and mounts; the alert channel; PREVENT, deletion protection and the 7-day snapshot schedule in prod only; and a human-readable plan shows no patient value and no alert address (T26; the alert channel's display name and its redacted `email_address` label are the positive controls); T38: the plans of the IAP and Cloud NAT runs change only project-base (the IAP one exactly its API and firewall rule) and no plan defers a data source read to apply (the mock provider shows a forced replacement as an update, so "nothing outside project-base" is the check that matters); CI's page deployer: exactly one `roles/run.viewer` grant, on fhir-proxy, in the scenario that sets `page_deployer_service_account_email` (real_ingest_on), and no Cloud Run grant to it in the others (T41); T44: fhir-proxy mounts `session-signing-key` and no reviewer password, its only project grant is `roles/recaptchaenterprise.agent`, `DEMO_COUNTER_BUCKET` names the private counter bucket that only fhir-proxy writes, and the bucket's `force_destroy` and the key's `deletion_policy` follow the environment |
| `_stack/tests/check_agent_ordering.sh` | T38, from `terraform graph`: healthcare's two Pub/Sub agent grants come after identity's `google_project_service_identity.pubsub`, its Healthcare agent grant after its own service identity, and the dataset, topics and Healthcare agent after project-base's APIs; healthcare does not depend on project-base's network or firewall (what a module-level `depends_on` adds) |
| `_stack/tests/check_env_roots.py` | dev and prod differ only in `env.tf`, `backend.tf`, the example and the tests, and `env.tf` holds exactly the documented settings; git's file list is non-empty and holds the known files; no author-specific project (in a path or assigned to any `project*` key), number (also before a full stop), bucket, email or host (also inside a URL) in any file under `infra/envs` except the deploy tools' generated lockfile, which is checked instead for registry URLs, sha512 integrity hashes and the exact version of `package.json`; no secret-version data source or `secret_data`; `terraform.tfvars` and `backend.hcl` ignored, the examples and lock files not; the examples hold placeholders only, set every required variable, and plan; lock files consistent with the modules and with bootstrap's platforms; both roots' `versions.tf` constraints; provider settings; the `depends_on` edges (ingestion on identity and project-base; phr on project-base and identity) and none on identity or healthcare (T38: a module-level `depends_on` defers their data sources); the patient variables and `alert_email` declared sensitive |
| `dev/tests`, `prod/tests` | T26 adds: the build trigger is refused without the build account; triggered builds wait for an approval by default |
| `tests/tooling/test_tf_ci.py` (run by `make test`) | `ci/tf_ci.py` against fake `terraform`, `npm` and `npx` and in-process HTTP: tfvars parsing (heredoc and expressions refused), the redaction check (whole words; messages name the variable, never the value); the plan record (redacted text, digest, count, tfvars generation, commit; no binary plan in it or in the checkout; `-lock=false` for the planner); a leaking or empty plan records nothing; the digest (order-free, no-ops ignored, changes with a value, not with a sensitive value); apply with the reviewed record, refused for a changed value, another generation, another commit or a broken record; an unreadable state is an error; destroy (empty state, a bring-up in progress, leftovers until the retry, other leftovers, a failed destroy, prod refused); the page configuration and deploy (locked CLI, install scripts off, no Terraform); fetching the tfvars (URL, generation asked and checked, mode 600 also for an existing file, masking, errors without a traceback or the token); cleanup; the Environments check for prod (`prod-plan`, `prod`, `prod-page`) and dev (`dev-page`), type-exact, failing closed on the review's fuzz cases (policy, admin bypass, protection rules and reviewers of the wrong type or shape, branch policy lists that are long, mislabelled, paginated or of the wrong type) for each Environment; the tip-of-main check; the digest ignoring drift in `before` |
| `tests/tooling/test_terraform_workflows.py` | the four `terraform-*.yml` workflows: SHA-pinned actions, no `${{ }}` in a script, minimal and separated permissions, no `secrets.*`, every authenticating job gated on its identity's variables and on main or a same-repository pull request, the narrowest identity per job (planner jobs only plan, deployer jobs run no npm, page jobs no Terraform of any kind), each identity in its own Environment (none for dev's planner and deployer, `dev-page`, `prod-plan`, `prod`, `prod-page`), the dev and prod preflights, page deploys in their own concurrency groups with `prod-page` checking the tip of main, prod only from its Environments, prod-apply pinned to the reviewed plan, prod following a deployed dev, the preflight, manual runs dev-only by default, a shared non-cancelling group per environment, the page built without credentials and deployed after every apply, only the known artifacts, the tfvars deleted at the end, the opt-in nightly destroy and its retry, the checks job unconditional, tool versions and checksums pinned, the deploy tools' lockfile |
| `tests/tooling/test_wif_truth_table.py` | bootstrap's real `ci_context` mapping, prod's pool condition and the bindings, from the mocked plans, evaluated with cel-python for 1,008 token combinations per project: each reaches exactly the expected account, none reaches two, page jobs only the page deployer, and every account is reachable (not vacuous) |
| `tests/tooling/test_terraform_lint_scan.py` | `make tf-lint` passes clean code and fails on a missing `required_version`, an invalid machine type (Google ruleset) and a problem in a subdirectory; `make tf-scan` fails on SSH open to `0.0.0.0/0`; a download whose checksum does not match is refused and nothing is installed |
| `dev/tests/root.tftest.hcl` | dev's defaults (synthetic, ingest off); the `SYN-` rules both ways; the name rules; the alert destination; zone in region; images by digest; the patient check fires (`override_module`) on a mismatch and on a missing authority; `page_deployer_service_account_email` null by default, a user email refused, a service account email planned (T41) |
| `prod/tests/root.tftest.hcl` | prod's defaults (real data, ingest off); a `SYN-` patient with real data is refused; synthetic data still possible; the patient check |
