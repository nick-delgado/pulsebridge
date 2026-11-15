# Module `identity`

Who each PulseBridge workload is, and which secrets it may read (task T04).

This module creates:

- **one service account per workload** (seven, plus an optional eighth for the synthetic-data
  seeder);
- **eight Secret Manager secrets without values**, each readable only by the account that needs
  it;
- the IAM bindings on resources it owns itself: those secrets and its own service accounts; plus
  one project-level grant (Cloud Logging for the edge VM);
- optionally, for dev only, a `reviewer-password` version, written through the write-only
  argument `secret_data_wo` so the value never enters Terraform state.

No service account keys are created: every workload gets short-lived tokens from Google (Cloud
Run, Compute Engine, Cloud Scheduler and Pub/Sub all do this), so no key file exists to leak.
No Identity Platform resources (decision D9).

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
| `cuff-sim` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | Only for the HTTPS-ingest transport (T13); the default MLLP transport needs no IAM, only the firewall rule in `edge-vm`. |
| `fhir-proxy` | `roles/secretmanager.secretAccessor` | secrets `reviewer-password`, `openemr-client-private-key` (each) | **this module** | D9 login check; S1 `private_key_jwt` assertion for OpenEMR (T14, T15). |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | the FHIR store | `healthcare` (T05) | Read API, trace lookups, share source data. Read only. |
| `fhir-proxy` | `roles/healthcare.hl7V2Consumer` | the HL7v2 store | `healthcare` (T05) | Trace panel and messages log: original v2 message and its outcome labels. |
| `fhir-proxy` | `roles/run.invoker` | Cloud Run service `cuff-sim` | `phr` (T22) | D10: the proxy forwards cuff requests with its own identity. |
| `edge-vm` | `roles/secretmanager.secretAccessor` | secrets `db-root-password`, `openemr-db-password`, `openemr-admin-password` (each) | **this module** | The startup script writes them into the compose `.env` (S2). |
| `edge-vm` | `roles/secretmanager.secretVersionAdder` (if `edge_vm_writes_openemr_key`, default true) | secret `openemr-client-private-key` only | **this module** | The T23 registration script runs on the VM and uploads the private key it generates there. |
| `edge-vm` | `roles/secretmanager.viewer` (same switch) | secret `openemr-client-private-key` only | **this module** | Lets that script see whether a key version already exists, so it is safe to run twice. Metadata only: viewer has no `secretmanager.versions.access`. |
| `edge-vm` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | The MLLP adapter forwards messages to the store (S4). Its only Healthcare permission. |
| `edge-vm` | `roles/logging.logWriter` | the project | **this module** | Startup-script output (including the adapter crash-loop check, T21) reaches Cloud Logging. Log writing has no narrower resource. |
| `scheduler-invoker` | `roles/run.invoker` | Cloud Run service `health-sync` | `ingestion` (T20) | The identity in the Scheduler job's OIDC token. It can do nothing else. |
| `pubsub-push` | `roles/run.invoker` | Cloud Run service `v2-to-fhir` | `ingestion` (T20) | The identity in the push subscription's OIDC token (`push_service_account_email` in T05). It can do nothing else. |
| `seeder` (only if `create_seeder_account`) | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | T09 writes synthetic Observations. |
| Pub/Sub service agent `service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com` (`grant_pubsub_token_creator`, default true) | `roles/iam.serviceAccountTokenCreator` | the `pubsub-push` account only | **this module** (single owner) | Lets Pub/Sub sign the push OIDC token. Required in projects created before 8 April 2021; harmless in newer ones, and scoped to one account. |

In particular: no project-level `roles/owner`, `roles/editor`, admin, IAM, Secret Manager,
Healthcare or Cloud Run role; `cuff-sim`, `v2-to-fhir`, `scheduler-invoker`, `pubsub-push` and
`seeder` read no secret; the only write access to any secret is `edge-vm` adding versions to the
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
searches FHIR resources; `fhirResourceReader` reads and searches; `hl7V2Consumer` reads and lists
(and, per the summaries, updates labels); `hl7V2Ingest` only ingests; `secretVersionAdder` adds
versions; `viewer` reads metadata of secrets and versions but not payloads. **To confirm on first
apply:** `fhirResourceEditor` includes `healthcare.fhirStores.executeBundle`; `hl7V2Ingest` alone
lets the MLLP adapter ingest (spike S4 ran with project Editor); `hl7V2Consumer` can update labels.

### Not yet designed, may need grants

These are open in other tasks. Each may add a row to the table above; none is granted today.

- **Where `health-sync` records "reconnect needed"** for the proxy's status strip (T10, T14): if
  it is a label, a FHIR resource or an object in a bucket, `health-sync` needs write and
  `fhir-proxy` read access to that one place.
- **Where the messages log's send attempts and the FHIR transaction list are stored** (T14, T19):
  the writer (`cuff-sim` or `fhir-proxy`) and the reader (`fhir-proxy`) need access to it.
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
| The browser (anyone) | `fhir-proxy` | Public: `roles/run.invoker` for `allUsers`; the reviewer password and signed token (D9) are the gate | `phr` (T22) |
| `fhir-proxy` | `cuff-sim` | OIDC token for `fhir-proxy`; `cuff-sim` is **not** public and has no other invoker | `phr` (T22) |
| Cloud Scheduler job | `health-sync` | OIDC token for `scheduler-invoker` | `ingestion` (T20) |
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
| `openemr-client-private-key` | `fhir-proxy` | OpenEMR client registration script (T23), run **on the edge VM as `edge-vm`** |
| `reviewer-password` | `fhir-proxy` | `scripts/secrets/set-reviewer-password` (T27), or in dev this module (below), not both |
| `db-root-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |
| `openemr-db-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |
| `openemr-admin-password` | `edge-vm` | `scripts/secrets` (T27), run by the deployer |

`health-sync` gets read access only. Google keeps the same refresh token when an access token is
refreshed, so the service never needs to write one; reconnecting is the T11 script's job, run by
the deployer with their own credentials. If T10 finds Google does return a new refresh token,
add `roles/secretmanager.secretVersionAdder` on that one secret here.

The OpenEMR client ID is plain configuration, not a secret (spike S1).

### Dev only: reviewer password from tfvars (D9)

```hcl
module "identity" {
  source     = "../../modules/identity"
  project_id = var.project_id

  reviewer_password            = var.reviewer_password # declare it `sensitive` and `ephemeral` in the env root too
  reviewer_password_wo_version = 1                     # 0 = no version; raise to write a new password
}
```

`reviewer_password` is `ephemeral`: Terraform does not save it in the plan or state, and it can
only flow into write-only arguments. Because an ephemeral value cannot decide whether a resource
exists (`count`), the non-secret `reviewer_password_wo_version` is the switch. Terraform keeps no
copy of a write-only value, so it cannot notice a new password; raise the number to send one.
Prod leaves both unset; the secret stays empty until T27's script sets it, and the proxy answers
`503` until then.

Rules for dev (not verified against a real apply):

- Set the password **either** through Terraform (these two variables) **or** with the T27
  script, never both. While Terraform manages a version, a script that adds a newer version and
  disables the older ones is partly undone by the next apply, which re-enables or re-creates the
  version Terraform manages.
- Raising `reviewer_password_wo_version` may replace the version (the old one destroyed, then
  the new one created). If it does, there is a moment with no enabled version, and the proxy's
  login answers `503` until the new one exists. The mock provider used in the tests does not run
  the real provider's planning logic, so the tests cannot show which happens.

## Usage from other modules

`service_account_emails` is a map keyed by the workload names (`health-sync`, `v2-to-fhir`,
`cuff-sim`, `fhir-proxy`, `edge-vm`, `scheduler-invoker`, `pubsub-push`, and `seeder` when
created). Its keys are known at plan time, so it can be passed whole to a module that uses
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
provider could not reach a project. `state_scan.py` removes them itself.

```bash
env -u GOOGLE_APPLICATION_CREDENTIALS -u CLOUDSDK_CORE_PROJECT -u GOOGLE_CLOUD_PROJECT \
    -u GOOGLE_PROJECT -u GOOGLE_CREDENTIALS HOME="$(mktemp -d)" GCE_METADATA_HOST=127.0.0.1:9 \
    sh -c 'terraform init -backend=false && terraform test'   # 27 runs, mock Google provider
python3 tests/state_scan.py                                    # reviewer password absent from state
```

- `tests/service_accounts.tftest.hcl`: exactly one account per workload; the email keys are
  known at plan time; the only project-level grant is the edge VM's log writer; Token Creator is
  on by default, can be turned off and is on the push account only; `project_id` and
  `name_prefix` validation.
- `tests/secrets.tftest.hcl`: the eight secrets; no versions by default; the accessor bindings
  compared as a whole table; `cuff-sim` and the others read nothing; the only write access is
  `edge-vm` on the OpenEMR key, which it cannot read; `secret_labels` validation.
- `tests/reviewer_password.tftest.hcl`: the write-only version and the input rules.
- `tests/state_scan.py` with `tests/state_scan/`: checks that `reviewer_password` is declared
  `sensitive` and `ephemeral`, then applies the module with a random dummy password and searches
  the resulting state for it, verbatim, hex-encoded and base64-encoded (standard and URL-safe, at
  any byte alignment). `terraform test` keeps state in memory, so the script takes the state from
  `terraform test -json -verbose` (the `terraform show -json` format, which shows sensitive values
  in clear), writes it to files and searches them. A helper module (`tests/control/`) stores the
  dummy verbatim and base64-encoded; the scan must find both there, which proves it would find
  such a leak. It cannot prove that no other transformation of the value (a hash, a substring)
  reaches state, nor what a real provider does.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.11 |
| <a name="requirement_google"></a> [google](#requirement\_google) | ~> 8.11 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_google"></a> [google](#provider\_google) | 8.11.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [google_project_iam_member.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_secret_manager_secret.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret) | resource |
| [google_secret_manager_secret_iam_member.accessor](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_iam_member) | resource |
| [google_secret_manager_secret_iam_member.writer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_iam_member) | resource |
| [google_secret_manager_secret_version.reviewer_password](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_version) | resource |
| [google_service_account.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account_iam_member.pubsub_token_creator](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project that holds the service accounts and secrets. | `string` | n/a | yes |
| <a name="input_create_seeder_account"></a> [create\_seeder\_account](#input\_create\_seeder\_account) | Create a service account for the synthetic-data seeder (task T09). Set it to the env root's use\_synthetic\_data flag, so a deployment that uses real watch data has no spare identity that can write to the FHIR store. | `bool` | `false` | no |
| <a name="input_edge_vm_writes_openemr_key"></a> [edge\_vm\_writes\_openemr\_key](#input\_edge\_vm\_writes\_openemr\_key) | Let the edge VM's account add versions to the openemr-client-private-key secret (roles/secretmanager.secretVersionAdder) and see whether one exists (roles/secretmanager.viewer, metadata only), both on that one secret. The OpenEMR client registration script (T23) runs on the VM, generates the key pair there and uploads the private key. The VM still cannot read the key back: it is not an accessor of that secret. | `bool` | `true` | no |
| <a name="input_grant_pubsub_token_creator"></a> [grant\_pubsub\_token\_creator](#input\_grant\_pubsub\_token\_creator) | Let the project's Pub/Sub service agent (service-<project number>@gcp-sa-pubsub.iam.gserviceaccount.com) mint OIDC tokens for the pubsub-push account. Projects created before 8 April 2021 need it; newer ones already allow it, so granting it is harmless there. On by default because it is scoped to the pubsub-push account only, never the project. This module is the only owner of this grant; the healthcare module must not also create it. | `bool` | `true` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every service account ID and secret ID this module creates, for example "pb" gives "pb-health-sync". Keeps PulseBridge's identities recognisable in a shared project. | `string` | `"pb"` | no |
| <a name="input_reviewer_password"></a> [reviewer\_password](#input\_reviewer\_password) | Dev only: the shared reviewer password, written as a version of the reviewer-password secret through the write-only argument secret\_data\_wo, so it never enters Terraform state or plan files. Used only when reviewer\_password\_wo\_version > 0. At least 16 characters. Set the password EITHER this way OR with scripts/secrets/set-reviewer-password (T27), never both: while Terraform manages a version, the next apply undoes a script that disabled it. Leave unset in prod. | `string` | `null` | no |
| <a name="input_reviewer_password_wo_version"></a> [reviewer\_password\_wo\_version](#input\_reviewer\_password\_wo\_version) | Dev only: 0 (the default) creates no reviewer-password version. 1 or more creates one from reviewer\_password; increase it to write a new password, because Terraform cannot see a write-only value change and only sends it when this number changes. Raising it may replace the version (old one destroyed, then the new one created); if so, for a moment the secret has no enabled version and the proxy's login answers 503. Not verified against a real apply: the mock provider used in tests does not run the real provider's planning logic. | `number` | `0` | no |
| <a name="input_secret_deletion_protection"></a> [secret\_deletion\_protection](#input\_secret\_deletion\_protection) | Refuse to destroy the secrets (Secret Manager deletion\_protection). Leave false in dev, which is destroyed nightly; consider true in prod so a stray destroy cannot drop the refresh token or the OpenEMR key. | `bool` | `false` | no |
| <a name="input_secret_labels"></a> [secret\_labels](#input\_secret\_labels) | Extra labels for every Secret Manager secret, merged over the module's own labels (component, managed-by). | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_reviewer_password_version_created"></a> [reviewer\_password\_version\_created](#output\_reviewer\_password\_version\_created) | True when this module wrote a reviewer-password version from tfvars (dev). False means the secret is empty until scripts/secrets/set-reviewer-password runs, and the proxy's login answers 503. |
| <a name="output_secret_ids"></a> [secret\_ids](#output\_secret\_ids) | Secret ID (the short name, e.g. "pb-reviewer-password") per secret key. Cloud Run secret references and the scripts that add versions use this. |
| <a name="output_secret_readers"></a> [secret\_readers](#output\_secret\_readers) | Which workloads may read each secret (the accessor bindings this module creates). Informational, for READMEs and checks in other modules. |
| <a name="output_secret_resource_ids"></a> [secret\_resource\_ids](#output\_secret\_resource\_ids) | Full secret resource ID (projects/<project>/secrets/<secret\_id>) per secret key. |
| <a name="output_service_account_emails"></a> [service\_account\_emails](#output\_service\_account\_emails) | Service account email per workload, keyed health-sync, v2-to-fhir, cuff-sim, fhir-proxy, edge-vm, scheduler-invoker, pubsub-push (and seeder when created). Other modules use these for resource-level bindings and as the identity their resources run as. |
| <a name="output_service_account_members"></a> [service\_account\_members](#output\_service\_account\_members) | IAM member string ("serviceAccount:<email>") per workload, ready for the `member` argument of a `google_*_iam_member` resource. |
| <a name="output_service_account_names"></a> [service\_account\_names](#output\_service\_account\_names) | Fully qualified service account name (projects/<project>/serviceAccounts/<email>) per workload, for bindings on the account itself such as google\_service\_account\_iam\_member. |
<!-- END_TF_DOCS -->
