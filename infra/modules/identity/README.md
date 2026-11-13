# Module `identity`

Who each PulseBridge workload is, and which secrets it may read (task T04).

This module creates:

- **one service account per workload** (seven, plus an optional eighth for the synthetic-data
  seeder);
- **eight Secret Manager secrets without values**, each readable only by the account that needs
  it;
- the IAM bindings on resources it owns itself: those secrets and its own service accounts; plus
  one project-level grant (Cloud Logging for the edge VM);
- optionally, for dev only, the first `reviewer-password` version, written through the
  write-only argument `secret_data_wo` so the value never enters Terraform state.

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
**a binding lives in the module that owns the resource it is on.** That is why this module does
create the secret accessor bindings (it owns the secrets) and the Pub/Sub Token Creator binding
(it owns the push account).

Every binding in PulseBridge uses the additive `_iam_member` flavour (one role, one member, one
resource). The authoritative `_iam_binding` (owns the full member list of one role) and
`_iam_policy` (owns the whole policy) would silently remove grants other modules make on the same
resource, for example `edge-vm` and `cuff-sim` both needing `hl7V2Ingest` on the one HL7v2 store
from two different modules.

## Least privilege: every role, on which resource, created where

This table is the acceptance criterion "each with only the roles its service needs". "This
module" bindings are created and tested here; the others must be created by the named module,
on exactly that resource, with `module.identity.service_account_members["<workload>"]` as the
member.

| Workload (account `<prefix>-<key>`) | Role | On resource | Created in | Why |
| --- | --- | --- | --- | --- |
| `health-sync` | `roles/secretmanager.secretAccessor` | secrets `google-health-client-id`, `google-health-client-secret`, `google-health-refresh-token` (each) | **this module** | D2: reads the OAuth client and refresh token at run time (T10). |
| `health-sync` | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | Writes the heart-rate transaction Bundle with conditional updates. |
| `v2-to-fhir` | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | Writes Observation, Patient, Device, Provenance by conditional update. |
| `v2-to-fhir` | `roles/healthcare.hl7V2Editor` | the HL7v2 store | `healthcare` (T05) | Reads the ingested message and writes the `application_outcome` labels on it (T12, D7); `hl7V2Consumer` would be enough if T12 stores the outcome elsewhere. |
| `cuff-sim` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | Only for the HTTPS-ingest transport (T13); the default MLLP transport needs no IAM, only the firewall rule in `edge-vm`. |
| `fhir-proxy` | `roles/secretmanager.secretAccessor` | secrets `reviewer-password`, `openemr-client-private-key` (each) | **this module** | D9 login check; S1 `private_key_jwt` assertion for OpenEMR (T14, T15). |
| `fhir-proxy` | `roles/healthcare.fhirResourceReader` | the FHIR store | `healthcare` (T05) | Read API, trace lookups, share source data. Read only. |
| `fhir-proxy` | `roles/healthcare.hl7V2Consumer` | the HL7v2 store | `healthcare` (T05) | Trace panel and messages log: original v2 message and its outcome labels. Read only. |
| `fhir-proxy` | `roles/run.invoker` | Cloud Run service `cuff-sim` | `phr` (T22) | D10: the proxy forwards cuff requests with its own identity. |
| `edge-vm` | `roles/secretmanager.secretAccessor` | secrets `db-root-password`, `openemr-db-password`, `openemr-admin-password` (each) | **this module** | The startup script writes them into the compose `.env` (S2). |
| `edge-vm` | `roles/healthcare.hl7V2Ingest` | the HL7v2 store | `healthcare` (T05) | The MLLP adapter forwards messages to the store (S4). Its only Healthcare permission. |
| `edge-vm` | `roles/logging.logWriter` | the project | **this module** | Startup-script output (including the adapter crash-loop check, T21) reaches Cloud Logging. Log writing has no narrower resource. |
| `scheduler-invoker` | `roles/run.invoker` | Cloud Run service `health-sync` | `ingestion` (T20) | The identity in the Scheduler job's OIDC token. It can do nothing else. |
| `pubsub-push` | `roles/run.invoker` | Cloud Run service `v2-to-fhir` | `ingestion` (T20) | The identity in the push subscription's OIDC token (`oidc_token.service_account_email` on the subscription in T05). It can do nothing else. |
| `seeder` (only if `create_seeder_account`) | `roles/healthcare.fhirResourceEditor` | the FHIR store | `healthcare` (T05) | T09 writes synthetic Observations. |
| Pub/Sub service agent `service-<project number>@gcp-sa-pubsub` (only if `grant_pubsub_token_creator`) | `roles/iam.serviceAccountTokenCreator` | the `pubsub-push` account | **this module** | Lets Pub/Sub sign the push OIDC token. Needed only in projects created before 8 April 2021. |

Nothing else. In particular: no project-level `roles/owner`, `roles/editor`, admin, IAM,
Secret Manager, Healthcare or Cloud Run role; `cuff-sim`, `v2-to-fhir`, `scheduler-invoker`,
`pubsub-push` and `seeder` read no secret; no account may add secret versions.

Role contents (from Google's predefined-role documentation; the project's IAM API is disabled, so
they could not be listed here): `fhirResourceEditor` creates, reads, updates, deletes and searches
FHIR resources; `fhirResourceReader` reads and searches; `hl7V2Editor` reads, lists, creates,
patches (labels) and deletes HL7v2 messages; `hl7V2Consumer` reads and lists; `hl7V2Ingest`
only ingests. **To confirm on first apply:** that `fhirResourceEditor` includes
`healthcare.fhirStores.executeBundle` (needed for the transaction Bundle) and that
`hl7V2Ingest` alone lets the MLLP adapter ingest (spike S4 ran with project Editor).

### Who invokes whom (decision D10)

| Caller | Target | How the call is authorised | Binding created in |
| --- | --- | --- | --- |
| The browser (anyone) | `fhir-proxy` | Public: `roles/run.invoker` for `allUsers`; the reviewer password and signed token (D9) are the gate | `phr` (T22) |
| `fhir-proxy` | `cuff-sim` | OIDC token for `fhir-proxy`; `cuff-sim` is **not** public and has no other invoker | `phr` (T22) |
| Cloud Scheduler job | `health-sync` | OIDC token for `scheduler-invoker` | `ingestion` (T20) |
| Pub/Sub push subscription | `v2-to-fhir` | OIDC token for `pubsub-push` | `ingestion` (T20) |
| `cuff-sim` | MLLP adapter on the edge VM | Network only: firewall rule from the Cloud Run egress address (no IAM) | `edge-vm` (T21) |
| MLLP adapter (as `edge-vm`) | HL7v2 store | `roles/healthcare.hl7V2Ingest` on the store | `healthcare` (T05) |

`health-sync` and `v2-to-fhir` are not public; nobody else may invoke them.

## Secrets

Created empty. A secret is a named container; its value is a **version**, added after apply by
the script named below, so no value is in code or state.

| Secret ID (`<prefix>-<key>`) | Readable by | Version written by |
| --- | --- | --- |
| `google-health-client-id` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `google-health-client-secret` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `google-health-refresh-token` | `health-sync` | `scripts/connect-google-health` (T11), run by the deployer |
| `openemr-client-private-key` | `fhir-proxy` | OpenEMR client registration script (T23), run by the deployer |
| `reviewer-password` | `fhir-proxy` | `scripts/secrets/set-reviewer-password` (T27); in dev optionally this module (below) |
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

## Usage from another module

```hcl
# In the healthcare module (T05), on resources it owns:
resource "google_healthcare_fhir_store_iam_member" "health_sync" {
  fhir_store_id = google_healthcare_fhir_store.this.id
  role          = "roles/healthcare.fhirResourceEditor"
  member        = var.service_account_members["health-sync"] # from module.identity
}
```

## Tests

Run from this directory; no credentials are needed:

```bash
terraform init -backend=false
terraform test                       # 18 runs, mock Google provider
python3 tests/state_scan.py          # reviewer password absent from state; no SA keys
```

- `tests/service_accounts.tftest.hcl`: exactly one account per workload; the only project-level
  grant is the edge VM's log writer; Token Creator is opt-in and on the push account only.
- `tests/secrets.tftest.hcl`: the eight secrets; no versions by default; the accessor bindings
  compared as a whole table; `cuff-sim` and the others read nothing.
- `tests/reviewer_password.tftest.hcl`: the write-only version and the input rules.
- `tests/state_scan.py` with `tests/state_scan/`: applies the module with a random dummy password
  and searches the resulting state. `terraform test` keeps state in memory, so the script takes
  the state from `terraform test -json -verbose` (the `terraform show -json` format, which shows
  sensitive values in clear), writes it to files and searches them. A helper module
  (`tests/control/`) stores the same dummy through `secret_data`; the scan must find it there,
  which proves it would find a leak.

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
| [google_secret_manager_secret_version.reviewer_password](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/secret_manager_secret_version) | resource |
| [google_service_account.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account_iam_member.pubsub_token_creator](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project that holds the service accounts and secrets. | `string` | n/a | yes |
| <a name="input_create_seeder_account"></a> [create\_seeder\_account](#input\_create\_seeder\_account) | Create a service account for the synthetic-data seeder (task T09). Set it to the env root's use\_synthetic\_data flag, so a deployment that uses real watch data has no spare identity that can write to the FHIR store. | `bool` | `false` | no |
| <a name="input_grant_pubsub_token_creator"></a> [grant\_pubsub\_token\_creator](#input\_grant\_pubsub\_token\_creator) | Let the project's Pub/Sub service agent mint OIDC tokens for the pubsub-push account. Only projects created before 8 April 2021 need this; newer projects already allow it. The grant is on the pubsub-push account only, not project-wide. | `bool` | `false` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every service account ID and secret ID this module creates, for example "pb" gives "pb-health-sync". Keeps PulseBridge's identities recognisable in a shared project. | `string` | `"pb"` | no |
| <a name="input_reviewer_password"></a> [reviewer\_password](#input\_reviewer\_password) | Dev only: the shared reviewer password, written as the first version of the reviewer-password secret through the write-only argument secret\_data\_wo, so it never enters Terraform state or plan files. Used only when reviewer\_password\_wo\_version > 0. Leave unset in prod; scripts/secrets/set-reviewer-password (T27) sets it there. At least 16 characters. | `string` | `null` | no |
| <a name="input_reviewer_password_wo_version"></a> [reviewer\_password\_wo\_version](#input\_reviewer\_password\_wo\_version) | Dev only: 0 (the default) creates no reviewer-password version. 1 or more creates one from reviewer\_password; increase it to write a new password, because Terraform cannot see a write-only value change and only sends it when this number changes. | `number` | `0` | no |
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
