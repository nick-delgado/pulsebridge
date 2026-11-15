# Environment roots: `dev` and `prod`

This directory is where PulseBridge is deployed from (task T25). It holds two environment
**roots** and the one **composition** both of them call:

```
infra/envs/
  _stack/      the composition: wires the seven modules (identity, project-base, healthcare,
               ingestion, edge-vm, openemr, phr) exactly as each module's README says
  dev/         a root: the quick start (synthetic data by default); destroyed nightly by CI (T26)
  prod/        a root: real Google Health data; the public demo; never destroyed by CI
  README.md    this runbook (T28 carries it into docs/setup.md)
```

This README is also the **runbook**: the ordered procedure that takes an empty Google Cloud
project to a working stack, who runs each step, and how to tell it worked. Nothing in it has run
against a real project yet (T25 was built offline): the [master checklist](#master-checklist) at
the end collects every assumption the modules could not verify, in the order a first deployment
meets them, with a column to record the result.

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
  saves in the plan or state (the dev reviewer password).

## Why one composition (`_stack`) and two thin roots

The brief asks for two environment roots that share the modules. The two options were:

| Option | For | Against |
| --- | --- | --- |
| **(a) one composition, thin roots** (chosen) | The seven module calls exist once, so dev and prod cannot be wired differently. A test proves the roots differ only in `env.tf` and `backend.tf`. | One more directory to read. |
| (b) two full roots | Each root reads top to bottom on its own. | About 250 lines of wiring twice; every module change must be made twice and the two copies drift. |

Terraform's provider rules do not get in the way of (a). A child module may not configure
providers when it is called with `count`/`for_each`, and needs `configuration_aliases` only for
**aliased** providers. Here every module uses the **default** `google` configuration, and module
phr also uses the default `google-beta` one, so the roots configure both providers once and
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
| `allow_reviewer_password_from_tfvars` | `true` | `false` | The brief's table: dev has a "default credential from tfvars", prod a credential in Secret Manager, rotated. In dev the reviewer password may come from tfvars through module identity's write-only version; prod refuses it (a validation) and uses `scripts/secrets/set-reviewer-password`. |
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
fallback only), `trusted_proxy_hops` (step 12), `openemr_scopes` (step 6), `public_origin`
(T30), `name_prefix` / `identity_name_prefix`, the two ingress fallbacks.

Rules the root enforces before anything is created: a `SYN-` patient ID is **required** with
synthetic data and **refused** without it; a real name is refused with synthetic data (the cuff
simulator always sends the seeder's synthetic name, Synthetic / Demo, so the seeder accepts a
Patient the cuff path created first) and required without it; an alert destination is
required; the zone must be in the region; images must be pinned by digest; prod refuses a
reviewer password from tfvars.

**One patient for everything.** `patient_id` and `patient_assigning_authority` are one variable
each. `_stack` passes the same values to module ingestion (v2-to-fhir, health-sync or the
seeder) and module phr (cuff-sim, fhir-proxy), and the `setup_env` output hands `PATIENT_ID` to
the connect script. `checks.tf` compares what each module received; `_stack/tests/plan_scan.py`
compares the planned `PATIENT_ID` and `PATIENT_ASSIGNING_AUTHORITY` of every service.

## What a plan shows

A plan (and the plan CI posts on a pull request, T26) prints every planned value that is not
marked **sensitive**. `patient_id`, `patient_family_name` and `patient_given_name` are sensitive
variables in both roots and in `_stack`, so plans show `(sensitive value)` for them and for every
Cloud Run environment block that carries them (`_stack/tests/plan_scan.py` checks a
human-readable plan for the synthetic values; `check_env_roots.py` checks the declarations).
Sensitive values are still **stored in the state in clear**; treat the state bucket as personal
data.

**Known limit for T26:** `alert_email` is not sensitive and appears in plan output. Module
ingestion decides with `alert_email != null` how many alert policies and channels exist
(`for_each`), and Terraform refuses a sensitive value there; marking it sensitive would need a
module change (a separate bool). A plan posted on a pull request therefore shows the alert
address, and the notification channel's labels show it in the state. Use an address you do not
mind appearing in CI output, or a notification channel created outside Terraform
(`alert_notification_channels`), whose name carries no address. The patient's name, gender and
birth date that the connect script takes are never in Terraform at all.

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
| `secret_name_prefix`, `secret_ids` | secret names (never values) for T27's scripts and the connect script |
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
by holding the same roles. Run Terraform commands from `infra/envs/<env>`.

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

### 4. Second targeted apply: identities and empty secrets (deployer)

```bash
terraform apply -target=module.stack.module.identity
```

Separate from step 3 because the secrets need the Secret Manager API that step 3 enabled
(module identity does not depend on project-base). In dev, a reviewer password from tfvars is
written here too (write-only).

Worked when: `gcloud secrets list --project <project>` shows eight `<prefix>-...` secrets.

### 5. Secret versions (deployer; T27's scripts)

Add a version to each of `<prefix>-db-root-password`, `<prefix>-openemr-db-password`,
`<prefix>-openemr-admin-password` and `<prefix>-reviewer-password` (prod; in dev it may come
from tfvars instead, never both). Values are read from a prompt, never echoed, and stored
**without a trailing newline** (the VM reads them into password files). The reviewer password
needs at least 16 characters. Never write `<prefix>-openemr-client-private-key`: the VM does,
and never the three Google Health secrets: the connect script does (step 9).

T27 builds `scripts/secrets/`. Until then, the same rules by hand (no echo, no trailing newline,
nothing on disk or in history; `IFS=` keeps leading and trailing spaces, `-r` keeps
backslashes, and the variable is removed whether or not gcloud succeeds):

```bash
IFS= read -rs value
printf '%s' "$value" | gcloud secrets versions add <prefix>-db-root-password \
  --data-file=- --project <project>; rc=$?; unset value; echo "gcloud exit code: $rc"
```

Worked when: `gcloud secrets versions list <secret> --project <project>` shows version 1
`enabled` for each.

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
exists. Whether Cloud Run refuses to deploy a service whose volume secret has no version yet is
**not verified** (Cloud Run documents no check at instance start-up for volume secrets; the
deploy-time check is undocumented); this order avoids depending on the answer.

First boot takes about 15 minutes (OpenEMR's 3.6 GB image pull is most of it). Work through
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
If registration failed and you go on anyway, step 8 may stop at
`module.stack.module.phr.google_cloud_run_v2_service.fhir_proxy` on that secret: finish this
step, then run step 8's apply again.

### 7. Build the images (owner, until T26 adds a build account)

```bash
gcloud builds submit --config cloudbuild.yaml --project <project> \
  --substitutions TAG_NAME=v0.1.0,COMMIT_SHA="$(git rev-parse HEAD)" .
gcloud builds log <BUILD_ID> --project <project> | sed -n '/BEGIN tfvars/,/END tfvars/{//!p}'
```

(services/README.md, "Build in Cloud Build".) The `sed` prints the lines between the markers,
without them. If `gcloud builds log` prefixes each line with the build step, as in
`Step #0 - "release": cuff_sim_image = ...`, delete that prefix before pasting (not verified
whether it does). Paste the block into `terraform.tfvars` unchanged: it names all five images; the
one your mode does not use is ignored. The deployer's roles do not include Cloud Build, so a
person runs this until T26's dedicated build account and trigger exist. Note that `gcloud builds
submit` creates a source bucket `gs://<project>_cloudbuild` that `terraform destroy` does not
remove.

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

Worked when: the apply ends without error, `terraform output scheduler_job_names` is `[]` and
`push_enabled` is `false`.

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
`setup_env` says so in its first line). Worked when: it ends `Done.` with "Patient record
created", and exit code 0.

**Synthetic data (`use_synthetic_data = true`): the first seeding.** Deployer (`roles/run.admin`
includes overriding the job's arguments):

```bash
gcloud run jobs execute "$(terraform output -raw seed_job_name)" \
  --region "$(terraform output -raw region)" --project <project> \
  --args=--days,30 --task-timeout=1800s --wait
```

Worked when: the execution succeeds and its `seed-run` log line has outcome `seeded`
(services/seed/README.md). If it was stopped, run it again: every write is keyed.

### 10. Second apply: ingest on (deployer)

Set `ingest_enabled = true` in `terraform.tfvars`, then `terraform apply`. One switch does both:
module ingestion creates the Scheduler job (health-sync every 15 minutes, or the seeder hourly)
and module healthcare turns the ingest subscription into a push subscription to v2-to-fhir.
HL7v2 notifications that arrived while it was off wait in the subscription (up to 7 days) and
are pushed now.

Worked when: `terraform output scheduler_job_names` lists one job and `push_enabled` is `true`.

### 11. Deploy the page (deployer, after every apply)

```bash
terraform -chdir=infra/envs/<env> output -raw firebase_json > web/firebase.json   # git-ignored
(cd web && npm ci && npm run build)          # VITE_FHIR_PROXY_URL unset: the page calls /api
(cd web && npx --yes firebase-tools@<pinned version> deploy --only hosting \
    --project "<project>" --non-interactive)
```

(phr README, "Deploying the page"; T26 automates it with a pinned `firebase-tools`.) A Terraform
change to the rewrite or the headers reaches the site only with this deploy.

Worked when: `dashboard_url` shows the login page.

### 12. The front door's first-deploy checks (deployer)

Items P1 to P10 of the master checklist, **hop measurement first** (`trusted_proxy_hops` is
chosen from it; re-apply after a change). The direct proxy URL is `terraform output -raw
fhir_proxy_url`.

### 13. The ingestion checks (deployer)

Items I1 to I13: the first sync or top-up (the Patient entry answers `200`, not `201`), the
second run (`stored_read: ok`, `windows_unchanged > 0`), `/status` timing, the alerts.

### 14. End to end (T29)

`tests/e2e/` (T29): a simulated cuff reading through MLLP to the FHIR store, then shared to
OpenEMR, asserted from the APIs, with one command against the dev stack.

## Operating it

### Patching the VM (monthly)

The VM does not update itself (COS in-place updates are off and the boot image is in
`ignore_changes`). Patch by replacing it:

```bash
terraform apply -replace='module.stack.module.edge_vm.google_compute_instance.this'
```

The data disk and the reserved internal address survive; the new VM boots from the newest
`cos-stable` image, mounts the existing disk without formatting, pulls the images again and
starts OpenEMR on its existing site files (about 15 minutes of downtime). Do it monthly, and
whenever the COS release notes announce a security fix. Check with `terraform output edge_vm`
(`boot_image`) and the startup log (`edge VM startup: done, all steps ok`).

### Rotating the reviewer password

prod (and dev if not set from tfvars): `scripts/secrets/set-reviewer-password` (T27) adds a new
version; fhir-proxy reads `latest` within about 30 seconds and every session ends at once (its
token key derives from the password). dev from tfvars: raise `reviewer_password_wo_version` and
apply with the new `TF_VAR_reviewer_password` (read with `IFS= read -rs`, unset after the apply
whatever its result; dev's `terraform.tfvars.example`); there may be a moment with no enabled version
(login answers 503). Never mix the two ways in one environment.

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
| The serverless subnet (and the VPC) on the first try | Google: after the Cloud Run services that used Direct VPC egress are deleted, "wait 1-2 hours" before the subnet can be deleted | Run `terraform destroy` again after one to two hours |
| The enabled APIs | `disable_on_destroy = false` on purpose | Nothing; an enabled API costs nothing |
| The state bucket, deployer and WIF pool | They are bootstrap's | infra/bootstrap/README.md, "Teardown" |
| `gs://<project>_cloudbuild` | Created by `gcloud builds submit`, not by Terraform | `gcloud storage rm -r gs://<project>_cloudbuild` |
| Logs in Cloud Logging | Kept for their retention period (30 days by default), free | Nothing |
| prod only: the data disk's daily snapshots (up to 7) | The schedule keeps them when the disk is deleted (`KEEP_AUTO_SNAPSHOTS`), so an accidental destroy can be undone; they are **billed as snapshot storage until deleted** | `gcloud compute snapshots list --filter="sourceDisk~<name_prefix>-edge-data" --project <project>`, then `gcloud compute snapshots delete <names> --project <project>` |

The secrets go with their versions, the Healthcare dataset with every stored resource and
message (`deletion_policy = "DELETE"` in dev), the VM with both disks and its address (in dev
there is no snapshot: a destroyed data disk is gone), the Artifact Registry repositories with their
images. Not verified: that a repository holding images deletes without error (D21 in the
checklist).

### Cost

From the module READMEs, with their caveats (prices "not confirmed" there are not confirmed
here either).

| Item | Idle | In use (one reviewer at a time) |
| --- | --- | --- |
| e2-micro VM, 20 + 10 GB `pd-standard` | **$0 inside the free tier** (one e2-micro per billing account, us-west1/us-central1/us-east1, 30 GB-months of standard disk). A second e2-micro on the same billing account (dev + prod) is billed: about $6 a month, not confirmed | the same |
| Artifact Registry | $0.10 per GB-month beyond 0.5 GB free: the cached OpenEMR image likely passes 0.5 GB, so cents to a few tens of cents a month | the same |
| Healthcare API | storage of a few MB: cents | per request: small |
| Cloud Run (4 services, 1 job) | $0 (scale to zero) | inside the free tier for a demo (not checked) |
| Cloud Scheduler | $0: 3 jobs free per billing account (one per deployment) | the same |
| Pub/Sub, Secret Manager, log-based metrics, IAM, VPC, firewall | about $0 | about $0 |
| Alert policies | $0 until 1 September 2027; then $0.35 per metric reference a month: about $1.05 (real data, 3 policies) or $0.70 (synthetic, 2) | the same |
| Firebase Hosting | $0 under 10 GB stored and 10 GB a month transferred | the same |
| Data disk snapshots (prod only, 7 daily, incremental) | snapshot storage of a fraction of a GB: cents a month, not confirmed | the same |
| Cloud NAT (off) | **billed hourly** if turned on: about $4.70 a month plus $0.045 per GiB | |

So **near $0 a month idle** for one deployment on a fresh billing account, plus cents of
storage; about $6 more for a second deployment's VM; about $1 more for alerts from September
2027. The only always-on resource is the VM.

## Master checklist

Every "Not verified" / first-boot / first-deploy item of the modules, in the order a first
deployment meets them. Fill in the last column (`YYYY-MM-DD: ok`, or what happened and what was
changed). Source abbreviations: **B** bootstrap, **PB** project-base, **ID** identity, **HC**
healthcare, **IN** ingestion, **EV** edge-vm, **OE** openemr, **PHR** phr, **HS** health-sync,
**SEED** services/seed, **SVC** services/README.md, **CG** connect-google-health, **CS**
cuff-sim, **V2F** v2-to-fhir, **FP** fhir-proxy, **T25** this task.

### Before and during the applies (steps 1 to 5, 7 and 8)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| A1 | Step 3's targeted apply creates project-base (and identity's service accounts) with no image variables set | T25 | |
| A2 | Step 4's targeted apply creates the eight empty secrets once Secret Manager is enabled | T25 | |
| A3 | The full plan on a clean project has no "Invalid count argument" for project-base's mirror grant (its count is now a plain bool; see "Findings") | T25 | |
| A4 | `health.googleapis.com` is the Google Health API's service name (it enables) | PB | |
| A5 | Service agents exist once the APIs are enabled; if an IAM binding to one fails, re-running the apply fixes it | HC | |
| A6 | The Healthcare topic-level publisher binding is accepted (S5 could not show it is needed) | HC | |
| A7 | The image build runs from a clean checkout and prints the tfvars block; the source bucket note (build account read access) | SVC | |
| A8 | health-sync deploys while its secrets have no version yet (else: connect first, apply again) | IN | |
| A9 | A Cloud Run revision that never becomes Ready makes `terraform apply` fail and keeps the previous revision serving | IN, PHR | |
| A10 | Creating the default Hosting site (`site_id = project_id`) through the API works on a project that has none yet | PHR | |
| A11 | The deployer can create a push subscription with an OIDC token (`iam.serviceAccounts.actAs` on pubsub-push) | HC | |
| A12 | dev: the reviewer password from tfvars creates a version; raising `reviewer_password_wo_version` replaces it (and whether there is a moment with no enabled version) | ID | |
| A13 | Bootstrap on a brand-new project: the first apply may stop on "API has not been used ... or it is disabled"; a re-run continues; `add_firebase` adds Firebase with the owner's ADC (`billing_project` set); `firebase_added = true` | B | |
| A14 | Workload Identity Federation: a workflow of the configured repository gets deployer tokens; one from another repository is refused; `google.subject` stays under 127 bytes for the branch names used | B | |
| A15 | IAP TCP forwarding works once `enable_iap_ssh = true` enabled `iap.googleapis.com` (understood to be required) | PB | |
| A16 | Prices against Google's pricing pages: Cloud NAT, the rejected Serverless VPC Access connector, Artifact Registry storage, Cloud Build minutes, snapshot storage, the e2-micro list price | PB, SVC, EV | |
| A17 | Cloud Scheduler in a project that first enabled the Scheduler API before 19 March 2019: its service agent may lack `roles/cloudscheduler.serviceAgent` (OIDC jobs fail); new projects are unaffected | ID | |
| A18 | Step 6's targeted apply creates the VM (and the dataset and HL7v2 store it needs) with no images and nothing of phr or ingestion; the OpenEMR client version exists before step 8 | T25 | |
| A19 | Step 8 does not stop at fhir-proxy on `openemr-client-private-key` (and, for the record, whether Cloud Run refuses a volume secret with no version at all) | PHR, T25 | |
| A20 | prod: the deployer (`roles/compute.instanceAdmin.v1`) can create the data disk's snapshot schedule and attach it; the first daily snapshot appears | EV, T25 | |
| A21 | The HL7v2 store parses with parser V1 as the spikes saw (V2 and V3 were never tried; the setting cannot be changed later) | HC | |

### The image build (step 7)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| C1 | `automapSubstitutions` exposes `PROJECT_ID`, `TAG_NAME` and `COMMIT_SHA` to the step's `script` as well as the `_` substitutions | SVC | |
| C2 | The step's docker client is already authenticated to `<region>-docker.pkg.dev` for the push and `docker buildx imagetools inspect` | SVC | |
| C3 | The default `MUST_MATCH` substitution check accepts substitutions only the script uses | SVC | |
| C4 | `gcloud builds submit` accepts `TAG_NAME` and `COMMIT_SHA` as `--substitutions` | SVC | |
| C5 | `roles/artifactregistry.reader` is enough to pull through the remote repository and cache new images there (also E6) | SVC, PB | |
| C6 | Artifact Registry accepts `docker buildx imagetools create --prefer-index=false` (a tag on an existing manifest) on the immutable-tag repository and keeps the digest | SVC | |
| C7 | A manual `gcloud builds submit` with a user-specified build account needs read access to the source bucket; whether `gcloud builds log` prefixes lines with `Step #N - "...": ` | SVC, T25 | |

### The VM's first boot (step 6): edge-vm first

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| E1 | The VM boots COS with Shielded VM on an e2-micro; serial port shows `pulsebridge-edge: edge VM startup: begin`; metadata shows `cos-update-strategy=update_disabled` | EV | |
| E2 | Every command the script needs exists on COS (the preflight step logs any missing: `blkid`, `mkfs.ext4`, `fsck.ext4`, `mkswap`, `swapon`, `curl`, `dd`, `timeout`, `tr`, `wc`, `sha256sum`, `stat`, `flock`) | EV | |
| E3 | The format decision on a real disk: the new disk reads as zeros, `blkid -p` exits 2, `wipefs --no-act` prints nothing, it is formatted once; after a reboot "has an ext4 filesystem; not formatting" | EV | |
| E4 | Swap: `swapon --show` lists the file (swap on COS is not documented for Compute Engine) | EV | |
| E5 | Artifact Registry and gcr.io over Private Google Access: both pulls succeed with no NAT (else `enable_cloud_nat = true`) | EV, PB | |
| E6 | `roles/artifactregistry.reader` on the mirror is enough to pull through the remote repository | EV, PB | |
| E7 | docker-credential-gcr writes under `/var/lib/pulsebridge-edge` and `docker pull` uses it | EV | |
| E8 | (after step 8, which creates the store grant) The adapter stays up (`mllp-adapter is up and stayed up`), its port is reachable from cuff-sim through both firewalls, and a test message gets `AA` with only `roles/healthcare.hl7V2Ingest` on the store | EV, ID | |
| E9 | `--logtostderr`: a second adapter started by hand with it stays up (then consider adding it) | EV | |
| E10 | The MariaDB secret file appears with mode 600; the `sed` parsing of Secret Manager's real response works | EV | |
| E11 | MariaDB initialises on the bind mount, becomes `healthy`, `--health-start-interval` is accepted; a configuration change replaces it with "Shutdown complete" | EV | |
| E12 | Graceful shutdown on VM stop: MariaDB's log on the next boot; `systemctl show docker -p TimeoutStopUSec` | EV | |
| E13 | After a reboot both containers come back and the script reports all steps ok | EV | |
| E14 | `timeout` and curl's time limits behave on COS as in the sandbox | EV | |
| E15 | The logging agent ships the startup script's lines to Cloud Logging | EV | |
| E16 | The crash-loop check on a real crash (restart back-off, `restarting` status) | EV | |
| E17 | Patching: `apply -replace` keeps the data disk and the address; `boot_image` reports the new image | EV | |
| E18 | The e2-micro free tier applies (billing report after a month); the e2-micro list price outside it | EV | |
| E19 | Docker `--log-opt max-size` is compatible with the COS logging agent | EV | |

### The VM's first boot (step 6): then openemr

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| O1 | The hooks run: `pulsebridge-openemr[openemr]: begin` after `MariaDB is healthy`, then `hook openemr: ok` and `hook openemr-oauth-client: ok`; metadata has both `pulsebridge-hook-*` keys | OE | |
| O2 | Host commands on COS the hooks use (`awk`, `cat`, `chmod`, `curl`, `date`, `docker`, `grep`, `head`, `iptables`, `ls`, `mkdir`, `mktemp`, `mv`, `rm`, `rmdir`, `sed`, `sha256sum`, `sleep`, `timeout`); `/run` writable and memory-backed | OE | |
| O3 | The OpenEMR pull through the mirror finishes within the deadline (record the time) | OE | |
| O4 | PHP in the image: `docker run --rm --entrypoint php <image> -m` lists `openssl`; it runs as `--user 0:0 --network none` | OE | |
| O5 | The sites seed keeps owner `1000:101` and mode `dr-x------` (`stat /mnt/disks/edge/openemr`); `docker logs openemr` shows "Setup Complete!" | OE | |
| O6 | The setup check: `first-time setup is complete (setup marker present, <n> access-control rules)`; `sites/default/docker-version` exists; `SELECT COUNT(*) FROM gacl_acl` > 0; `readyz` answers through `--cacert` (our certificate is served) | OE | |
| O7 | The published port is reachable from the VM itself at `https://<internal_ip>` | OE | |
| O8 | The metadata block: `sudo iptables -S DOCKER-USER` shows the rule; `docker run --rm --network pulsebridge-edge <image with curl> curl -m 5 -H 'Metadata-Flavor: Google' http://169.254.169.254/computeMetadata/v1/` times out; the adapter still gets `AA`; the startup script still reads secrets; `getent hosts mariadb` answers and `getent hosts example.com` times out in a container on the edge network; optionally `--publish <internal_ip>:443:443` | OE | |
| O9 | Apache capped: `docker exec openemr ps` shows at most 5 `httpd` under a burst; `zz-lowmem.conf` loads after `mpm.conf` | OE | |
| O10 | Memory: `free -m` shows about 200 MB or more available at idle | OE | |
| O11 | The settings: `OPENEMR_SETTING_* took effect`; `docker logs openemr` shows `Set rest_fhir_api to 1`; `GET /apis/default/fhir/metadata` is 200 | OE | |
| O12 | The database connection as `openemr` over TCP to 127.0.0.1 works; `oauth_clients` has `client_id`, `client_name`, `is_enabled` | OE | |
| O13 | The registration: 200 with a `client_id`; the client enabled; a token request from fhir-proxy (T15) succeeds with the RS384 assertion. Scopes: the six v1 scopes in the token; then try `openemr_scopes = ["system/Patient.crs", "system/Encounter.crs", "system/Observation.cr"]`, re-run the startup script, repeat a share (search, create, read work; `PUT`/`DELETE` refused); keep v2 if it works | OE | |
| O14 | Secret Manager: `addVersion` with `secretVersionAdder`; `GET versions/latest` with `viewer` reports `state` and `name` | OE, ID | |
| O15 | fhir-proxy's TLS from Cloud Run: Python's default context with the certificate as `cadata`, hostname checking on, accepts `https://<internal_ip>` | OE | |
| O16 | A reboot: the registration hook logs `nothing to do`; nothing is pulled, created or registered | OE | |
| O17 | Time: first boot within `hook_timeout_minutes = 40`; record pull and setup times | OE | |
| O18 | A database wipe (dev only): a new client is registered and fhir-proxy recovers after re-reading its secret file | OE | |
| O19 | OpenEMR's first-time setup accepts the `openemr-admin-password` value | OE | |
| O20 | The password exposure (process arguments during setup and on every start, `getenv()`, `docker inspect`) is acceptable for this deployment | OE | |
| O21 | No dangling anonymous volumes after a container replacement (`docker volume ls -qf dangling=true`) | OE | |
| O22 | (dev) An interrupted first-time setup: reset the VM during setup; both hooks then fail with the recovery message, and nothing is wiped | OE | |
| O23 | COS's Docker uses the iptables (not nftables) backend and creates `DOCKER-USER`; the edge network's bridge is named `br-<12 characters of the network ID>` | OE | |
| O24 | What Secret Manager's `versions/latest` resolves to when the newest version is disabled (either way the registration hook re-registers) | OE | |

### The setup step and ingestion (steps 9, 10 and 13)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| I1 | Connect script: Google spells the scope as expected; `access_secret_version` on `versions/latest` answers `NOT_FOUND` for a secret with no version; the deployer's ADC works with Secret Manager without extra quota-project setup | CG | |
| I2 | First seeding: `entry_statuses` shows `Patient 201` once (fresh store) and `Patient 200` for every data Bundle | SEED | |
| I3 | `roles/run.invoker` on the job lets Scheduler's OAuth call to `jobs.run` through | IN | |
| I4 | First real sync: the Patient entry answers `200`, not `201` (else switch to the fixed-ID fallback in the T06 README and record it) | HS, T29 | |
| I5 | `GET Observation?identifier=urn:pulsebridge:observation\|<key>` returns one Observation whose `subject.reference` is the setup Patient; `GET Patient?identifier=urn:pulsebridge:patient\|<PATIENT_ID>` returns exactly one Patient | HS | |
| I6 | Second run: `stored_read: "ok"` and `windows_unchanged > 0` (health-sync and seeder); `stranded_preliminary` is a number | HS, SEED | |
| I7 | `/status` answers within a second or two | HS | |
| I8 | Pub/Sub push and Cloud Scheduler reach the internal-only services (else the ingress fallbacks) | IN | |
| I9 | The OIDC audience (the bare service URL) is accepted | IN | |
| I10 | v2-to-fhir's `hl7V2Consumer` can patch message labels (else healthcare moves to `hl7V2Editor`); `fhirResourceEditor` includes `executeBundle` and update-create `PUT` | ID, HC | |
| I11 | The log filters match real entries (`jsonPayload.event`, `http_status`, `outcome`, the `bug` line, the seeder's stderr lines); a failed Scheduler attempt is logged at ERROR | IN | |
| I12 | The email channel delivers without further confirmation, and a policy fires as intended | IN | |
| I13 | Replay: `gcloud pubsub subscriptions pull --format=json` returns `message.data` base64-encoded; the `messages.list` label filter works | IN | |
| I14 | Connect script: the consent flow with a Desktop-app client and a random loopback port returns to `http://127.0.0.1:<port>/?state=...&code=...` | CG | |
| I15 | Google accepts `access_type=offline` and `prompt=consent` with PKCE for a Desktop client; the token response carries `refresh_token` (and, in Testing, `refresh_token_expires_in`) | CG | |
| I16 | `access_secret_version` answers `FAILED_PRECONDITION` when the latest version is disabled (treated as absent) | CG | |
| I17 | The identity call answers with `healthUserId` for the new access token | CG | |
| I18 | The FHIR store accepts the setup Bundle with the deployer's credentials; its response entry status starts `201` or `200` | CG | |
| I19 | `connect.sh` runs on the deployer's own macOS or Linux machine (wheels for other platforms are in the lock but were not installed) | CG | |
| I20 | Whether publishing the OAuth app ("In production") requires verification for the health scope | CG | |
| I21 | Pub/Sub retries a failing push with backoff (10 s to 600 s) and after 10 failed deliveries moves the notification to the dead-letter topic, where the pull subscription keeps it | HC | |
| I22 | Cloud Healthcare keeps `meta.security` on conditional writes (informational: the seeder's safety check does not use it) | SEED | |
| I23 | health-sync: decimals round-trip exactly through the FHIR store; Google's 401/403/429 error bodies; whether a refresh ever returns a new refresh token; whether the token response always carries `scope` | HS | |
| I24 | v2-to-fhir: the real push body shape, and the status codes `executeBundle` returns for a refused transaction | V2F | |
| I25 | cuff-sim: the derived MLLP message name matches the stored message (list the store); `hl7V2Ingest` alone allows `messages:ingest` with labels | CS | |

### The front door (steps 11 and 12)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| P1 | `TRUSTED_PROXY_HOPS`: two failed logins through Hosting (one plain, one with `X-Forwarded-For: 192.0.2.1`), one directly at `fhir_proxy_url`; read `forwarded_for_entries` and `client_address` in the three `login-failed` lines; choose 1 or 2 and re-apply | PHR | |
| P2 | `Authorization` reaches the proxy: after login, `/api/status` is 200, not 401 | PHR | |
| P3 | The rewrite path: `/api/fhir/metadata` with the token gives the CapabilityStatement; `/api/unknown` gives the proxy's OperationOutcome; search links start with `/api/` | PHR | |
| P4 | cuff-sim's and health-sync's `run.app` URLs refuse outside callers even with a valid token; a cuff send through the page works; the status strip shows health-sync's state | PHR, IN | |
| P5 | MLLP through the VPC: a cuff send over MLLP returns `AA` | PHR, EV | |
| P6 | The secret mount: login 503 before a version; works within about 30 s after `set-reviewer-password`; an old token is refused after a second new version | PHR | |
| P7 | CSP in the browser: zero violations through login, every tab, the chart, a cuff send; `/` carries the policy and `frame-ancestors 'none'`; `/.vite/manifest.json` is 404 | PHR | |
| P8 | The 60 s limit: a forced slow cuff send gets the proxy's own 504 within about 55 s, and its AuditEvent appears | PHR | |
| P9 | Direct VPC egress cold starts: time the first `/api/status` and the first MLLP send after 20 idle minutes | PHR | |
| P10 | Startup probes: a deliberately broken copy (`gcloud run deploy <name>-probe-check --no-traffic`, one variable missing) fails and the previous revision keeps serving | PHR | |
| P11 | A request through Hosting reaches a service whose invoker check is disabled, with no further setting | PHR | |
| P12 | `roles/firebasehosting.admin` is enough for `firebase deploy --only hosting` with the Cloud Run rewrite; the Firebase CLI accepts WIF credentials in CI (T26) | PHR | |
| P13 | `ALL_TRAFFIC` egress reaches Google APIs through Private Google Access, and the internal-only services accept the proxy's calls as internal | PHR, IN | |
| P14 | Network: Cloud Run Direct VPC egress traffic is admitted by the subnet-range firewall rule (tags and service identity cannot be the source); a /24 is plenty | PB | |
| P15 | fhir-proxy's Cloud Healthcare searches: `gt`/`lt` on `effectivePeriod`, `_sort=date`, `_page_token` reuse, `_summary=count`, `_sort=-_lastUpdated` with `_elements=id,meta`, identifier OR lists, AuditEvent `type`/`subtype`/`date`/`_sort=-date`, Provenance `recorded` and `_include=Provenance:target`; the indexing delay for non-identifier searches | FP | |
| P16 | fhir-proxy: HL7v2 `messages.list` with a `create_time` filter and `view=RAW_ONLY`; the identity-token audience; a mounted secret file reflects a new `latest` without a restart; `fhirResourceReader` allows every search above | FP | |

### Teardown (dev)

| # | Check | Source | Verified on / result |
| --- | --- | --- | --- |
| T1 | `terraform destroy` removes everything listed above; the serverless subnet fails at first and goes on a retry 1-2 hours later | PHR, T25 | |
| T2 | The Artifact Registry repositories delete while holding images | T25 | |
| T3 | After destroy the default Hosting site still serves the last page; `firebase hosting:disable` stops it | PHR | |
| T4 | Nothing billable is left: the billing report shows no charges for the project after a day (AC "destroy leaves nothing billable") | T25 | |

## Findings for the modules

Found while wiring T25; the first four are fixed in the modules in this task (each module's
README and tests updated), the last two remain:

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
5. **ingestion: `alert_email` cannot be sensitive**, because it decides a `for_each`. It
   therefore shows in plans ("What a plan shows"). Fix would be a separate bool in ingestion.
6. **Cloud Build has no deployer role.** Step 7 is run by the owner until T26's build account
   and trigger exist (bootstrap's `deployer_roles` has no Cloud Build role, by design).

## Tests

```bash
make tf-validate   # fmt -check and validate in _stack, dev, prod (init -backend=false)
make tf-test       # everything below, with Google credentials removed from the environment
```

| File | Checks |
| --- | --- |
| `_stack/tests/stack.tftest.hcl` | the composition under mocked providers: four scenarios (real or synthetic, ingest off or on) and a prod one, applied in the mock with the service accounts' emails unknown at plan (as on a real first plan, so "Invalid count argument" would show); push and Scheduler follow `ingest_enabled`; the right workloads per mode; project-base gets the edge VM's account; the mirror stays on; edge-vm gets openemr's two hooks and 40 minutes; openemr gets the VM's reserved address; one patient for ingestion and phr; the `setup_env` and `next_steps` outputs; prod refuses a tfvars reviewer password; a full plan needs the images; each targeted step (3, 4, 6) plans without them |
| `_stack/tests/plan_scan.py` | the scenario runs' full state (every resource in every module), with the scenario table required to equal the state-producing runs: no `allUsers`/`allAuthenticatedUsers`, no key, no authoritative IAM, one project-level grant; exactly one public service; every Cloud Run service and the job run as their own account and get the literal project, location, dataset and store IDs they should (and not the ones they should not: cuff-sim has no `FHIR_STORE_ID`); Scheduler jobs and push config per `ingest_enabled`; `PATIENT_ID` and the authority equal everywhere; the synthetic name; firewall target, ports and the mirror grant; the VM's account, address, subnet, no external IP, hooks, hook limit, secret files and adapter flags; cuff-sim's MLLP host and port; fhir-proxy's settings and mounts; the alert channel; PREVENT, deletion protection and the 7-day snapshot schedule in prod only; and a human-readable plan shows no patient value (the alert address, not sensitive, is the positive control) |
| `_stack/tests/check_env_roots.py` | dev and prod differ only in `env.tf`, `backend.tf`, the example and the tests, and `env.tf` holds exactly the documented settings; git's file list is non-empty and holds the known files; no author-specific project (in a path or assigned to any `project*` key), number, bucket, email or host in any file under `infra/envs`; no secret-version data source or `secret_data`; `terraform.tfvars` and `backend.hcl` ignored, the examples and lock files not; the examples hold placeholders only, set every required variable, and plan; lock files consistent with the modules and with bootstrap's platforms; both roots' `versions.tf` constraints; provider settings; the `depends_on` edges (healthcare on project-base; ingestion on identity and project-base; phr on project-base and identity); the patient variables declared sensitive |
| `dev/tests/root.tftest.hcl` | dev's defaults (synthetic, ingest off); the `SYN-` rules both ways; the name rules; the alert destination; zone in region; images by digest; the patient check fires (`override_module`) on a mismatch and on a missing authority |
| `prod/tests/root.tftest.hcl` | prod's defaults (real data, ingest off); prod refuses a tfvars reviewer password, with or without a version, and a version alone; a `SYN-` patient with real data is refused; synthetic data still possible; the patient check |
