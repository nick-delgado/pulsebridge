# Module `identity`

Who each PulseBridge workload is, and which secrets it may read (task T04).

This module creates:

- **one service account per workload** (seven, plus an optional one for the synthetic-data
  seeder and an optional one for image builds, T26);
- **eight Secret Manager secrets without values**, each readable only by the account that needs
  it;
- the IAM bindings on resources it owns itself: those secrets and its own service accounts; plus
  the project-level grants that have no narrower resource: Cloud Logging for the edge VM (the
  same role for the build account when it exists), and reCAPTCHA assessments for fhir-proxy
  (T44, D16).

It writes **no secret value**: since D16 (no reviewer password) every version is added by a
script after apply, and `tests/static_checks.sh` refuses a secret version in this module.

No service account keys are created: every workload gets short-lived tokens from Google (Cloud
Run, Compute Engine, Cloud Scheduler and Pub/Sub all do this), so no key file exists to leak.
No Identity Platform resources (decisions D9 and D16).

## Design: who creates which binding

A binding grants a role to an account **on a resource**. Many of the resources these accounts
need (the FHIR store, the HL7v2 store, the Cloud Run services) belong to other modules and may not
exist yet. Two options were considered:

| Option | How | Problem |
| --- | --- | --- |
| (a) This module takes store and service IDs as inputs and creates every binding | `identity` depends on `healthcare`, `ingestion`, `phr` | `healthcare` also needs `identity`'s emails (the Pub/Sub push identity), so the modules depend on each other both ways. The binding also sits far from the resource it protects. |
| **(b) Chosen:** this module creates accounts and secrets and outputs the emails; the module that owns a resource creates the bindings on it | `healthcare`, `ingestion`, `phr`, `edge-vm` depend on `identity`; `identity` depends on nothing | None of the above. Each owning module needs a few `_iam_member` resources, listed below. |

So the dependency graph is one-way: `identity` -> everything else. The rule is applied everywhere:
**a binding lives in the module that owns the resource it is on.** That is why this module
creates the secret bindings (it owns the secrets) and the Pub/Sub Token Creator binding (it owns
the push account).

**Each grant has exactly one owner.** Two modules must never both manage the same grant (same
role, member and resource). With additive `_iam_member` resources, both would apply happily, and
then destroying either module removes the grant the other still relies on. The Pub/Sub Token
Creator grant is owned here; `healthcare` must not also create it.

Every binding in PulseBridge uses the additive `_iam_member` flavour (one role, one member, one
resource). The authoritative `_iam_binding` (owns the full member list of one role) and
`_iam_policy` (owns the whole policy) would silently remove grants other modules make on the same
resource, for example `edge-vm` and `cuff-sim` both needing `hl7V2Ingest` on the one HL7v2 store.

## Least privilege: every role, on which resource, created where

This table is the acceptance criterion "each with only the roles its service needs". "This
module" bindings are created and tested here; the others must be created by the named module,
on exactly that resource, for the account `module.identity.service_account_emails["<workload>"]`.

| Workload (account `<prefix>-<key>`) | Role | On resource | Created in | Why |
| --- | --- | --- | --- | --- |
| `health-sync` | `roles/secretmanager.secretAccessor` | secrets `google-health-client-id`, `google-health-client-secret`, `google-health-refresh-token` (each) | **this module** | D2: reads the OAuth client and refresh token at run time (T10). |
| `health-sync` | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | Writes the heart-rate transaction Bundle with conditional updates. |
| `v2-to-fhir` | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | Writes Observation, Patient, Device, Provenance by conditional update. |
| `v2-to-fhir` | `roles/healthcare.hl7V2Consumer` | the HL7v2 store | `healthcare` (T05) | Reads the ingested message and writes the `application_outcome` labels on it (T12, D7). **Unconfirmed:** that Consumer includes updating message labels comes only from search-result summaries of Google's roles page; confirm on first apply, else T05 moves to `hl7V2Editor`. |
| `cuff-sim` | `roles/healthcare.fhirResourceEditor` | the **audit** FHIR store only | `healthcare` (T32) | D12, D15: writes one `AuditEvent` per send attempt (`PUT AuditEvent/<id>`). The role also lets it read, search and delete audit records; it reaches no health data. `cuff-sim` has **no** binding on the main FHIR store. The audit store exists so that this write access does not extend to health data, which the editor role on the main store would have (read and delete included); see the healthcare README. |
| `cuff-sim` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | Only for the HTTPS-ingest transport (T13); the default MLLP transport needs no IAM, only the firewall rule in `edge-vm`. |
| `fhir-proxy` | `roles/secretmanager.secretAccessor` | secrets `session-signing-key`, `openemr-client-private-key` (each) | **this module** | D16 demo session tokens (HS256 key); S1 `private_key_jwt` assertion for OpenEMR (T14, T15). |
| `fhir-proxy` | `roles/recaptchaenterprise.agent` | the project | **this module** | D16 bot check: `projects.assessments.create` (T43). An assessment's parent is the project, so there is no narrower resource. `CreateAssessment` needs `recaptchaenterprise.assessments.create` ([access control](https://docs.cloud.google.com/recaptcha/docs/access-control), read 19 November 2026); Agent is the narrowest predefined role with it: assessments create and annotate, three `list` permissions on firewall policies and related account groups (Account Defender, unused here) and project get/list. No key permission. A custom role with only `assessments.create` would be narrower still, but the deployer cannot create custom roles (bootstrap would have to); not done. |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | the FHIR store | `healthcare` (T05) | Read API, trace lookups, share source data. Read and search only, no writes. |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | the audit FHIR store | `healthcare` (T32) | D15: reads the send-attempt AuditEvents for the messages log (T14, T19). Read and search only. The healthcare module's tests and static check assert that its role table gives `fhir-proxy` only the reader role on both FHIR stores. |
| `fhir-proxy` | `roles/healthcare.hl7V2Consumer` | the HL7v2 store | `healthcare` (T05) | Trace panel and messages log: original v2 message and its outcome labels. |
| `fhir-proxy` | `roles/run.invoker` | Cloud Run service `cuff-sim` | `phr` (T22) | D10: the proxy forwards cuff requests with its own identity. |
| `edge-vm` | `roles/secretmanager.secretAccessor` | secrets `db-root-password`, `openemr-db-password`, `openemr-admin-password` (each) | **this module** | The startup script writes them into the compose `.env` (S2). |
| `edge-vm` | `roles/secretmanager.secretVersionAdder` (if `edge_vm_writes_openemr_key`, default true) | secret `openemr-client-private-key` only | **this module** | The T23 registration script runs on the VM and uploads the private key it generates there. |
| `edge-vm` | `roles/secretmanager.viewer` (same switch) | secret `openemr-client-private-key` only | **this module** | Lets that script see whether a key version already exists, so it is safe to run twice. Metadata only: viewer has no `secretmanager.versions.access`. |
| `edge-vm` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | The MLLP adapter forwards messages to the store (S4). Its only Healthcare permission. |
| `edge-vm` | `roles/logging.logWriter` | the project | **this module** | Startup-script output (including the adapter crash-loop check, T21) reaches Cloud Logging. Log writing has no narrower resource. |
| `edge-vm` | `roles/artifactregistry.reader` | the Docker Hub mirror repository only | `project-base` (T03, extended in T21) | The edge VM pulls MariaDB and OpenEMR through the pull-through cache (no internet route). Created when `project-base`'s `edge_vm_service_account_email` is set. |
| `scheduler-invoker` | `roles/run.invoker` | Cloud Run service `health-sync` | `ingestion` (T20) | The identity in the Scheduler job's OIDC token. It can do nothing else. |
| `scheduler-invoker` (synthetic data) | `roles/run.invoker` | Cloud Run job `seed` | `ingestion` (T20) | The identity in the OAuth token Scheduler uses to call `jobs.run` on the seeder job (`run.jobs.run`). health-sync and the seeder never exist together, so it holds one of the two grants. |
| `pubsub-push` | `roles/run.invoker` | Cloud Run service `v2-to-fhir` | `ingestion` (T20) | The identity in the push subscription's OIDC token (`push_service_account_email` in T05). It can do nothing else. |
| `seeder` (only if `create_seeder_account`) | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | T09 writes synthetic Observations. |
| `build` (only if `create_build_account`, T26) | `roles/logging.logWriter` | the project | **this module** | Cloud Build with a user-specified service account must log to Cloud Logging (`cloudbuild.yaml`: `logging: CLOUD_LOGGING_ONLY`). |
| `build` | `roles/artifactregistry.writer` | the services repository only | `project-base` (T26) | Push the images, read existing tags, add a tag to an existing image. |
| `build` | `roles/artifactregistry.reader` | the Docker Hub mirror only | `project-base` (T26) | Pull (and cache) the base image. |
| Pub/Sub service agent `service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com` (`grant_pubsub_token_creator`, default true) | `roles/iam.serviceAccountTokenCreator` | the `pubsub-push` account only | **this module** (single owner) | Lets Pub/Sub sign the push OIDC token. Required in projects created before 8 April 2021; harmless in newer ones, and scoped to one account. The agent itself is created first by `google_project_service_identity.pubsub` (below). |

### Outputs for module healthcare (T38)

- **`project_number`**: read by `data.google_project.this` at plan time. Module healthcare builds
  the Healthcare agent's address from it. Healthcare used to read the project itself, inside a
  module call with `depends_on = [module.project_base]`; that deferred its read to apply whenever
  project-base had a change, and planned its three agent grants for replacement. This module's
  call has no `depends_on`, so its data source is always read at plan, and the stack's
  `tests/check_env_roots.py` keeps it so: a module-level `depends_on` on identity (or healthcare)
  fails it.
- **`pubsub_service_agent_email`**: the same string the Token Creator grant uses, so it is known at
  plan; the output has `depends_on = [google_project_service_identity.pubsub]`, an ordering-only
  edge, so whatever uses it waits for the agent to exist without the value becoming unknown.

### The Pub/Sub service agent must exist first (T36)

A **service agent** is a Google-managed service account that a Google service uses to act inside
your project (Pub/Sub's signs push tokens and publishes to dead-letter topics). Its address is
fixed, `service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com`, but Google creates some
agents **lazily**, on first use, not when the API is enabled. IAM refuses a grant to an account
that does not exist yet. On the first dev deploy (16 November 2026, a new project) step 4 failed
exactly so: `Error 400: Service account service-<number>@gcp-sa-pubsub.iam.gserviceaccount.com
does not exist`, until the agent was created by hand.

So the module now creates it: `google_project_service_identity.pubsub` (beta provider only; the
env roots configure `google-beta` and Terraform passes it down) calls Service Usage's
`services.generateServiceIdentity`, which creates the agent if missing and returns it if it
exists. Then the Token Creator grant is made (`depends_on`). Details:

- **Created whatever `grant_pubsub_token_creator` says.** The variable still means only "make the
  Token Creator grant"; module healthcare also grants this agent roles (publisher on the dead-letter
  topic, subscriber on the ingest subscription) and needs it to exist too. It costs nothing.
- **The member string is still built from the project number**, not taken from the resource's
  `member` attribute. The number is read at plan time, so the member is known in the plan. The
  resource's attribute is "(known after apply)" while the resource is new, and `member` forces
  replacement: on a project that already has the grant, the next plan would destroy and
  re-create it for nothing. A **postcondition** on the service identity checks that the address
  Google returns is the one the grant names (an empty answer passes, a different one stops the
  apply).
- **Lifecycle.** The resource cannot be updated, imported or really destroyed: `terraform
  destroy` only drops it from state, and the agent stays, as every service agent does.
- **Not verified:** which permission `generateServiceIdentity` checks (Google's reference names
  only the OAuth scope). The deployer holds `roles/serviceusage.serviceUsageAdmin`, which is
  expected to cover it; on the dev deploy the agent was created by hand outside Terraform, so the
  next apply as the deployer is the first real test (checklist A22). If it is refused, run step 4
  of the runbook as the owner once.

**On a project that is already deployed** (the dev project of 16 November, where the agent was
created by hand and the grant exists), the next apply shows **one resource to add**,
`module.stack.module.identity.google_project_service_identity.pubsub` (it returns the existing
agent), and **no change** to `google_service_account_iam_member.pubsub_token_creator[0]`: its
member string is the same. The data source `google_project.this` lost its `count` (its address
changes from `this[0]` to `this`); data sources hold no lasting state, so the plan shows nothing for it.

**Other service agents the stack grants roles to** (checked in T36): module healthcare grants the
**Healthcare** agent publisher on the ingest topic and the **Pub/Sub** agent publisher on the
dead-letter topic and subscriber on the ingest subscription, each with the address built from the
project number. On the dev deploy the Healthcare grant worked (step 6), and the two Pub/Sub ones
worked only because the agent had by then been created by hand. Since T38 healthcare takes the
agent from this module's `pubsub_service_agent_email` output, which is declared to depend on
`google_project_service_identity.pubsub`, so a single full apply on a new project creates the agent
before healthcare's two grants too (the runbook's order, step 4 before steps 6 and 8, still works).
Healthcare also creates the Healthcare agent itself before granting it (healthcare README,
"Service agents: known at plan, created first"). No module grants anything to the Cloud Run,
Cloud Scheduler, Artifact Registry or Cloud Build agents, so those cannot fail this way (on the dev
deploy the Cloud Run services, the Scheduler job and the build ran without any grant to them).

In particular: no project-level `roles/owner`, `roles/editor`, admin, IAM, Secret Manager,
Healthcare or Cloud Run role; `cuff-sim`, `v2-to-fhir`, `scheduler-invoker`, `pubsub-push` and
`seeder` (and `build`) read no secret; the only write access to any secret is `edge-vm` adding versions to the
OpenEMR key.

**The risk of the OpenEMR key grant, plainly.** The VM generates the key pair and uploads the
private key, so whoever controls the VM can replace the key `fhir-proxy` signs with (and could
then register a matching public key with OpenEMR, which runs on the same VM anyway). The VM cannot
read the key back: it has no `secretAccessor` on that secret, and `viewer` covers metadata only.
Set `edge_vm_writes_openemr_key = false` if T23 ends up running the script as the deployer
instead; a local marker on the VM's persistent disk is the fallback for the "already registered?"
check if `viewer` turns out to be unsuitable.

Role contents come from Google's documentation, not a live lookup (the project's IAM API is
disabled, and `docs.cloud.google.com` is not reachable from the build session, so the latest
checks used search-result summaries): `fhirResourceEditor` creates, reads, updates, deletes and
searches FHIR resources in the store it is granted on; `fhirResourceReader` reads and searches;
`hl7V2Consumer` reads and lists (and, per the summaries, updates labels); `hl7V2Ingest` only
ingests; `secretVersionAdder` adds versions; `viewer` reads metadata of secrets and versions but
not payloads. **To confirm on first apply:** `fhirResourceEditor` includes
`healthcare.fhirStores.executeBundle` and allows a `PUT` that creates a resource
(update-create); `hl7V2Ingest` alone lets the MLLP adapter ingest (spike S4 ran with project
Editor); `hl7V2Consumer` can update labels.

### Not yet designed, may need grants

These are open in other tasks. Each may add a row to the table above; none is granted today.

- **Where `health-sync` records "reconnect needed"** for the proxy's status strip (T10, T14): if
  it is a label, a FHIR resource or an object in a bucket, `health-sync` needs write and
  `fhir-proxy` read access to that one place.
- **Where the FHIR transaction list is stored** (T14, T19): its writer and reader
  (`fhir-proxy`) need access to it. The messages log's send attempts are settled by D12: they
  are AuditEvents in a separate audit FHIR store (D15), written by `cuff-sim` and read by
  `fhir-proxy` (rows above).
- **Cloud Build's build identity** (T24): without a dedicated service account, builds run as the
  Compute Engine default account, which has project Editor in older projects. T24 should create
  a build account with only Artifact Registry write and log writing.
- **Cloud Scheduler in old projects** (T20): if the Cloud Scheduler API was first enabled in the
  project before 19 March 2019, its service agent
  (`service-<project number>@gcp-sa-cloudscheduler.iam.gserviceaccount.com`) may lack the
  Cloud Scheduler Service Agent role (`roles/cloudscheduler.serviceAgent`, a project-level role),
  and jobs that send OIDC tokens fail. Google's fix is to grant that role on the project; it is
  not a Token Creator grant on `scheduler-invoker`, so this module does not add one. Evidence:
  search-result summaries of Google's "Use authentication with HTTP targets" page, not read in
  full. New projects are unaffected.

### Who invokes whom (decision D10)

| Caller | Target | How the call is authorised | Binding created in |
| --- | --- | --- | --- |
| The browser (anyone) | `fhir-proxy` | Public: Cloud Run invoker IAM check disabled (`invoker_iam_disabled = true`); no `allUsers` binding; the bot check and the signed `__session` cookie (D16) are the gate | `phr` (T22) |
| `fhir-proxy` | `cuff-sim` | OIDC token for `fhir-proxy`; `cuff-sim` is **not** public and has no other invoker | `phr` (T22) |
| Cloud Scheduler job | `health-sync` | OIDC token for `scheduler-invoker` | `ingestion` (T20) |
| Cloud Scheduler job (synthetic data) | seeder Cloud Run job, via the Cloud Run Admin API `jobs.run` | OAuth token for `scheduler-invoker` | `ingestion` (T20) |
| Pub/Sub push subscription | `v2-to-fhir` | OIDC token for `pubsub-push` | `ingestion` (T20) |
| `cuff-sim` | MLLP adapter on the edge VM | Network only: firewall rule from the Cloud Run egress address (no IAM) | `edge-vm` (T21) |
| MLLP adapter (as `edge-vm`) | HL7v2 store | `roles/healthcare.hl7V2Ingest` on the store | `healthcare` (T05) |

`health-sync` and `v2-to-fhir` are not public; nobody else may invoke them. The acceptance
criterion "only the proxy may invoke the cuff simulator" is enforced where that binding lives,
in `phr` (T22); this module supplies the proxy's identity.

## Secrets

Created empty. A secret is a named container; its value is a **version**, added after apply by
the script named below, so no value is in code or state.

| Secret ID (`<prefix>-<key>`) | Readable by | Version written by |
| --- | --- | --- |
| `google-health-client-id` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `google-health-client-secret` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `google-health-refresh-token` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `openemr-client-private-key` | `fhir-proxy` | OpenEMR client registration script (T23), run **on the edge VM as `edge-vm`**. The value is the OpenEMR client document (JSON, format `pulsebridge.openemr-client.v1`): the client's private key with its client ID, key ID and OpenEMR's certificate (module `openemr` README) |
| `session-signing-key` | `fhir-proxy` | `scripts/secrets/set-session-signing-key.sh` (T44): 32 random bytes, generated by the script and never shown. A new version ends every demo session |
| `db-root-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |
| `openemr-db-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |
| `openemr-admin-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |

`health-sync` gets read access only. Google keeps the same refresh token when an access token is
refreshed, so the service never needs to write one; reconnecting is the T11 script's job, run by
the deployer with their own credentials. If T10 finds Google does return a new refresh token,
add `roles/secretmanager.secretVersionAdder` on that one secret here.

The OpenEMR client ID is plain configuration, not a secret (spike S1). It is generated by OpenEMR at registration on the VM and travels in the same secret version as the key, so the two always match.

### No secret value from Terraform (D16)

Until T44, dev could write the reviewer password from tfvars through the write-only argument
`secret_data_wo`. D16 removed the password; the session signing key is random bytes nobody needs
to type, so the script generates it in both environments, and the two dev-only variables
(`reviewer_password`, `reviewer_password_wo_version`) are gone. One way to set each secret means
an apply can never undo a rotation done by the script.

## Usage from other modules

`service_account_emails` is a map keyed by the workload names (`health-sync`, `v2-to-fhir`,
`cuff-sim`, `fhir-proxy`, `edge-vm`, `scheduler-invoker`, `pubsub-push`, and `seeder` and
`build` when created). Its keys are known at plan time, so it can be passed whole to a module that uses
`for_each` over the keys it needs (a test checks this). The healthcare module (T05) takes it
like this:

```hcl
module "healthcare" {
  source = "../../modules/healthcare"
  # ...

  # Store-level bindings; healthcare ignores the keys it does not use.
  store_iam_service_accounts = module.identity.service_account_emails

  # Identity in the push subscription's OIDC token (and the account whose Token Creator grant
  # this module owns).
  push_service_account_email = module.identity.service_account_emails["pubsub-push"]
}
```

## Tests

Run from this directory. No credentials are needed, and none should be present: run with Google
credentials removed from the environment so that, if a mock ever failed to apply, the real
provider could not reach a project.

```bash
env -u GOOGLE_APPLICATION_CREDENTIALS -u CLOUDSDK_CORE_PROJECT -u GOOGLE_CLOUD_PROJECT \
    -u GOOGLE_PROJECT -u GOOGLE_CREDENTIALS HOME="$(mktemp -d)" GCE_METADATA_HOST=127.0.0.1:9 \
    sh -c 'terraform init -backend=false && terraform test'   # mock google and google-beta
bash tests/static_checks.sh                                    # no secret version, key or authoritative IAM
```

- `tests/service_accounts.tftest.hcl`: exactly one account per workload; the email keys are
  known at plan time; the only project-level grants are the edge VM's log writer and fhir-proxy's
  `roles/recaptchaenterprise.agent` (its only project role; nobody else holds a reCAPTCHA role); no build account by
  default, and with `create_build_account` exactly one (`<prefix>-build`) whose only project grant
  is the log writer and which reads and writes no secret; Token Creator is
  on by default, can be turned off and is on the push account only; the Pub/Sub service agent is
  created in the project (also with the grant off); the grant's member is the literal
  `serviceAccount:service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com` and known at
  plan time (so an existing grant is not replaced); an agent with another address stops the apply; `project_id` and
  `name_prefix` validation.
- `tests/secrets.tftest.hcl`: the eight secrets (`session-signing-key`, no
  `reviewer-password`); the accessor bindings compared as a whole table; `cuff-sim` and the others read nothing; the only write access is
  `edge-vm` on the OpenEMR key, which it cannot read; `secret_labels` validation.
- `tests/static_checks.sh`: no secret version resource, no `secret_data` or `secret_data_wo`,
  no data source reading a secret version, no service account key, no authoritative IAM, and no
  leftover reviewer password. It replaces `state_scan.py`, which proved dev's write-only password
  stayed out of state: with no value written by Terraform there is nothing left to scan for.

## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.11 |
| <a name="requirement_google"></a> [google](#requirement\_google) | ~> 8.11 |
| <a name="requirement_google-beta"></a> [google-beta](#requirement\_google-beta) | ~> 8.11 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_google"></a> [google](#provider\_google) | 8.11.0 |
| <a name="provider_google-beta"></a> [google-beta](#provider\_google-beta) | 8.11.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [google_project_iam_member.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_service_identity.pubsub](https://registry.terraform.io/providers/hashicorp/google-beta/latest/docs/resources/project_service_identity) | resource |
| [google_secret_manager_secret.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret) | resource |
| [google_secret_manager_secret_iam_member.accessor](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_iam_member) | resource |
| [google_secret_manager_secret_iam_member.writer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_iam_member) | resource |
| [google_service_account.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account_iam_member.pubsub_token_creator](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_create_build_account"></a> [create\_build\_account](#input\_create\_build\_account) | Create the dedicated image-build service account ("<prefix>-build", key "build", T26) that Cloud Build runs cloudbuild.yaml as. This module grants it only roles/logging.logWriter on the project (the build log, logging: CLOUD\_LOGGING\_ONLY); module project-base grants it writer on the services repository and reader on the Docker Hub mirror, on those repositories only. Nothing else: no secrets, health data, Cloud Run or IAM. Default false: no build account. | `bool` | `false` | no |
| <a name="input_create_seeder_account"></a> [create\_seeder\_account](#input\_create\_seeder\_account) | Create a service account for the synthetic-data seeder (task T09). Set it to the env root's use\_synthetic\_data flag, so a deployment that uses real watch data has no spare identity that can write to the FHIR store. | `bool` | `false` | no |
| <a name="input_edge_vm_writes_openemr_key"></a> [edge\_vm\_writes\_openemr\_key](#input\_edge\_vm\_writes\_openemr\_key) | Let the edge VM's account add versions to the openemr-client-private-key secret (roles/secretmanager.secretVersionAdder) and see whether one exists (roles/secretmanager.viewer, metadata only), both on that one secret. The OpenEMR client registration script (T23) runs on the VM, generates the key pair there and uploads the private key. The VM still cannot read the key back: it is not an accessor of that secret. | `bool` | `true` | no |
| <a name="input_grant_pubsub_token_creator"></a> [grant\_pubsub\_token\_creator](#input\_grant\_pubsub\_token\_creator) | Let the project's Pub/Sub service agent (service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com) mint OIDC tokens for the pubsub-push account. Projects created before 8 April 2021 need it; newer ones already allow it, so granting it is harmless there. On by default because it is scoped to the pubsub-push account only, never the project. This module is the only owner of this grant; the healthcare module must not also create it. | `bool` | `true` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every service account ID and secret ID this module creates, for example "pb" gives "pb-health-sync". Keeps PulseBridge's identities recognisable in a shared project. | `string` | `"pb"` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project that holds the service accounts and secrets. | `string` | n/a | yes |
| <a name="input_secret_deletion_protection"></a> [secret\_deletion\_protection](#input\_secret\_deletion\_protection) | Refuse to destroy the secrets (Secret Manager deletion\_protection). Leave false in dev, which is destroyed nightly; consider true in prod so a stray destroy cannot drop the refresh token or the OpenEMR key. | `bool` | `false` | no |
| <a name="input_secret_labels"></a> [secret\_labels](#input\_secret\_labels) | Extra labels for every Secret Manager secret, merged over the module's own labels (component, managed-by). | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_project_number"></a> [project\_number](#output\_project\_number) | The project's number, read at plan time by data.google\_project. Module healthcare builds the Healthcare service agent's address from it. |
| <a name="output_pubsub_service_agent_email"></a> [pubsub\_service\_agent\_email](#output\_pubsub\_service\_agent\_email) | The project's Pub/Sub service agent, service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com. Known at plan (built from the number), and declared to depend on google\_project\_service\_identity.pubsub, so any resource that uses it, such as module healthcare's dead-letter and subscriber grants, is created after the agent exists. |
| <a name="output_secret_ids"></a> [secret\_ids](#output\_secret\_ids) | Secret ID (the short name, e.g. "pb-session-signing-key") per secret key. Cloud Run secret references and the scripts that add versions use this. |
| <a name="output_secret_readers"></a> [secret\_readers](#output\_secret\_readers) | Which workloads may read each secret (the accessor bindings this module creates). Informational, for READMEs and checks in other modules. |
| <a name="output_secret_resource_ids"></a> [secret\_resource\_ids](#output\_secret\_resource\_ids) | Full secret resource ID (projects/<project>/secrets/<secret\_id>) per secret key. |
| <a name="output_service_account_emails"></a> [service\_account\_emails](#output\_service\_account\_emails) | Service account email per workload, keyed health-sync, v2-to-fhir, cuff-sim, fhir-proxy, edge-vm, scheduler-invoker, pubsub-push (and seeder and build when created). Other modules use these for resource-level bindings and as the identity their resources run as. |
| <a name="output_service_account_members"></a> [service\_account\_members](#output\_service\_account\_members) | IAM member string ("serviceAccount:<email>") per workload, ready for the `member` argument of a `google_*_iam_member` resource. |
| <a name="output_service_account_names"></a> [service\_account\_names](#output\_service\_account\_names) | Fully qualified service account name (projects/<project>/serviceAccounts/<email>) per workload, for bindings on the account itself such as google\_service\_account\_iam\_member. |
<!-- END_TF_DOCS -->
