# Bootstrap

The one Terraform root a **human** runs **once**, with their **own** Google login, before anything
else in `infra/` exists. It prepares a GCP project so the environment roots (`infra/envs/dev`,
`infra/envs/prod`) and GitHub Actions can take over.

It creates:

| What | Why |
| --- | --- |
| A project with billing linked (`create_project = true`), or nothing (`create_project = false`, adopt an existing project) | Somewhere to put the stack. A personal Google account can create a project with no organization. |
| The six APIs bootstrap itself calls (Resource Manager, Service Usage, IAM, IAM Credentials, STS, Storage) | The rest of the stack's APIs are enabled later by module `project-base`. |
| A versioned, private GCS bucket for remote Terraform state | Every env root keeps its state here, one prefix per environment. |
| A `terraform-deployer` service account with a reasoned set of roles | The identity env roots and CI deploy as. |
| A Workload Identity pool and GitHub OIDC provider, restricted to **one** repository, plus the binding that lets that repository impersonate the deployer | CI authenticates with short-lived tokens. **No service account key is created anywhere.** |

## Prerequisites

- Terraform >= 1.11 and the `gcloud` CLI.
- Your own Google login as Application Default Credentials (not a service account key):
  `gcloud auth application-default login`.
- To create a project: a billing account you can link (`roles/billing.user` on it;
  `gcloud billing accounts list`). To adopt one: Owner on that project, and billing already linked.
- Your ADC needs a quota project with the Cloud Billing and Service Usage APIs enabled for the
  first calls against a brand-new project. `gcloud auth application-default set-quota-project
  <an-existing-project>` sets one.

## Usage

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # git-ignored; fill in your values
terraform init
terraform plan                                 # read it: this is a real project
terraform apply
terraform output                               # values for the env roots and GitHub
```

Then:

- Env roots: put `state_bucket_name` in each root's `backend "gcs"` block as `bucket`, with a
  per-environment `prefix` (`dev`, `prod`).
- GitHub Actions: give `google-github-actions/auth` the two outputs below (they are identifiers,
  not secrets, so repository variables are fine):

  ```yaml
  permissions:
    id-token: write   # lets the job request GitHub's OIDC token
    contents: read
  steps:
    - uses: google-github-actions/auth@<pinned sha>
      with:
        workload_identity_provider: ${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }} # output workload_identity_provider
        service_account: ${{ vars.GCP_DEPLOYER_SA }}                           # output deployer_service_account_email
  ```

## State: the chicken-and-egg

Terraform records what it created in a **state** file. A **remote backend** keeps that file in
shared storage (here a GCS bucket) instead of on one laptop, so CI and other people see the same
state, and the backend locks it so two applies cannot run at once.

Bootstrap *creates* that bucket, so on its first run there is nowhere remote to store bootstrap's
own state. That is why `versions.tf` has no `backend` block: bootstrap starts with a local
`terraform.tfstate` file (git-ignored; never commit it).

Bootstrap's state holds only identifiers (no keys or secrets), but losing it means Terraform no
longer knows it owns these resources. Once the bucket exists, move bootstrap's state into it:

```bash
cd infra/bootstrap
# backend_override.tf matches the *_override.tf pattern in .gitignore, so the bucket name stays out of git.
cat > backend_override.tf <<'EOF'
terraform {
  backend "gcs" {
    prefix = "bootstrap"
  }
}
EOF
terraform init -migrate-state -backend-config="bucket=$(terraform output -raw state_bucket_name)"
# Answer "yes" to copy the state, check `terraform plan` shows no changes, then delete the local
# terraform.tfstate and terraform.tfstate.backup.
```

Keep `backend_override.tf` for later runs. To go back to local state, delete it and run
`terraform init -migrate-state` again.

## Create or adopt

`create_project` has no default, so the choice is always explicit.

- `true`: creates `google_project.this[0]` with `billing_account` linked, under `org_id` or
  `folder_id` if given, otherwise with no parent. The auto-created `default` network is deleted
  (`auto_create_network = false`) because its firewall rules allow SSH and RDP from anywhere; to do
  that the provider enables the Compute Engine API, which the stack needs anyway. The project has
  `deletion_policy = "PREVENT"`, so `terraform destroy` will not delete it.
- `false`: reads the existing project with a data source and creates nothing at project level.
  It does not touch billing, the parent, or existing IAM: role grants are additive
  (`google_project_iam_member`), never authoritative.

## Deployer roles

The deployer must later create the whole stack: APIs, VPC and firewall, an e2-micro, Artifact
Registry, service accounts and their IAM bindings, Secret Manager secrets, Healthcare dataset and
stores, Pub/Sub, Cloud Run, Cloud Scheduler and a bucket. The default `deployer_roles` is one
predefined admin role per product the stack uses, rather than `roles/owner` or `roles/editor`
(which the variable's validation rejects):

| Role | Needed for |
| --- | --- |
| `roles/serviceusage.serviceUsageAdmin` | enabling the stack's APIs (project-base) |
| `roles/compute.networkAdmin` | VPC, subnet, Cloud NAT, static IP |
| `roles/compute.securityAdmin` | firewall rules |
| `roles/compute.instanceAdmin.v1` | the e2-micro and its disk |
| `roles/artifactregistry.admin` | the container repository |
| `roles/iam.serviceAccountAdmin` | one service account per service |
| `roles/iam.serviceAccountUser` | attaching those accounts to Cloud Run, the VM, Scheduler |
| `roles/resourcemanager.projectIamAdmin` | project-level role grants for those accounts |
| `roles/secretmanager.admin` | secrets (no values) and their accessor bindings |
| `roles/healthcare.datasetAdmin`, `fhirStoreAdmin`, `hl7V2StoreAdmin` | dataset, FHIR R4 store, HL7v2 store and their IAM |
| `roles/pubsub.admin` | topic, push subscription, dead-letter topic |
| `roles/run.admin` | Cloud Run services and invoker bindings |
| `roles/cloudscheduler.admin` | the health-sync schedule |
| `roles/storage.admin` | the dashboard bucket |

The deployer also gets `roles/storage.objectAdmin` on the state bucket itself, so it can always
read and write state even if the list above is trimmed.

**The trade-off.** "Least privilege" for an account whose job is to create IAM is limited:

- `roles/resourcemanager.projectIamAdmin` lets the deployer grant any role on the project,
  including to itself. Whoever controls the deployer (the one GitHub repository) can therefore
  escalate to Owner-equivalent. That is inherent to a deployer that manages IAM; the protection is
  in who can become the deployer (WIF, below), not in the role list. A further step, not taken
  here, is an IAM condition on that grant that allows only an explicit list of roles to be granted
  (`api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly([...])`); it is worth
  adding once the identity module's role list is stable.
- `roles/secretmanager.admin` can also read secret values. There is no predefined role that can
  create secrets and set their IAM without access.
- What the list does buy over Owner: the deployer cannot delete the project, change billing, or
  use products the stack does not use, and every grant is visible and reviewable.

Change the list with `deployer_roles` in `terraform.tfvars`.

## Workload Identity Federation

GitHub Actions gets a short-lived token for the deployer instead of a stored key:

1. The workflow (with `permissions: id-token: write`) asks GitHub for a signed OIDC token whose
   claims describe the run, for example `repository`, `repository_id`, `ref`, `sub`.
2. `google-github-actions/auth` sends it to Google's Security Token Service (STS). STS checks the
   signature against GitHub's issuer `https://token.actions.githubusercontent.com`, maps the
   claims (`attribute_mapping`) and evaluates `attribute_condition`. If the condition is false,
   the exchange is refused.
3. STS returns a federated token for the principal
   `principalSet://.../attribute.repository/<owner>/<name>`.
4. That principal holds `roles/iam.workloadIdentityUser` on the deployer, so the IAM Credentials
   API issues a deployer access token valid for about an hour.

All GitHub repositories share the same issuer, so without the attribute condition **any**
repository could complete step 2. The condition pins `assertion.repository` to
`github_repository`; setting `github_repository_id` additionally pins the numeric ID, which
survives renames and is never reused, so a repository later created under the same name cannot
match. The service-account binding checks the repository again, independently.

A deleted pool or provider is kept for 30 days and its ID cannot be reused in that time.

## Cost

Near zero when idle: a few kilobytes of state in a free-tier region, and no hourly resources.
Service accounts, IAM and Workload Identity Federation are free.

## Testing

No test needs credentials or touches a project:

```bash
cd infra/bootstrap
terraform init -backend=false
terraform validate
terraform test              # tests/*.tftest.hcl: mocked google provider, plan only
tests/static_checks.sh      # no key resources, no module calls, no backend block
```

`terraform test` uses `mock_provider "google"` (Terraform loads the provider's schema but makes no
API calls) with `override_during = plan`, so the mocked computed values in `tests/mocks/` are
known at plan time. The tests cover create vs adopt mode, the bucket's versioning and public
access prevention, the WIF attribute condition and binding, the deployer's roles, the outputs,
and that each variable validation rejects bad input.

## Reference

The section below is generated by [terraform-docs](https://terraform-docs.io/) (v0.20.0):
`terraform-docs markdown table --output-file README.md --output-mode inject .`

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
| [google_iam_workload_identity_pool.github](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool) | resource |
| [google_iam_workload_identity_pool_provider.github](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool_provider) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project) | resource |
| [google_project_iam_member.deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_service.bootstrap](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_service) | resource |
| [google_service_account.deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account_iam_member.deployer_github_wif](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_storage_bucket.tfstate](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/storage_bucket) | resource |
| [google_storage_bucket_iam_member.deployer_state](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/storage_bucket_iam_member) | resource |
| [google_project.existing](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_auto_create_network"></a> [auto\_create\_network](#input\_auto\_create\_network) | Only for a newly created project: keep the 'default' VPC network GCP creates automatically. Default false, because that network ships with firewall rules open to the internet (SSH, RDP, ICMP) and the stack builds its own VPC (module project-base). With false, the provider enables the Compute Engine API to delete the default network. | `bool` | `false` | no |
| <a name="input_billing_account"></a> [billing\_account](#input\_billing\_account) | Billing account ID (format XXXXXX-XXXXXX-XXXXXX) to link to a newly created project. Required when create\_project = true; must be unset when adopting, because bootstrap does not change an existing project's billing. | `string` | `null` | no |
| <a name="input_create_project"></a> [create\_project](#input\_create\_project) | true: create a new GCP project with var.project\_id and link var.billing\_account to it. false: adopt an existing project with var.project\_id and create nothing at project level. No default, so the choice is always explicit. | `bool` | n/a | yes |
| <a name="input_deployer_account_id"></a> [deployer\_account\_id](#input\_deployer\_account\_id) | account\_id (the part before @) of the deployer service account that env roots and CI run as. 6-30 characters. | `string` | `"terraform-deployer"` | no |
| <a name="input_deployer_roles"></a> [deployer\_roles](#input\_deployer\_roles) | Project-level IAM roles granted to the deployer service account. The default is the smallest set of predefined roles that can create the whole PulseBridge stack; see README.md ("Deployer roles") for the reasoning. Basic roles (owner, editor) are rejected. | `list(string)` | <pre>[<br/>  "roles/serviceusage.serviceUsageAdmin",<br/>  "roles/compute.networkAdmin",<br/>  "roles/compute.securityAdmin",<br/>  "roles/compute.instanceAdmin.v1",<br/>  "roles/artifactregistry.admin",<br/>  "roles/iam.serviceAccountAdmin",<br/>  "roles/iam.serviceAccountUser",<br/>  "roles/resourcemanager.projectIamAdmin",<br/>  "roles/secretmanager.admin",<br/>  "roles/healthcare.datasetAdmin",<br/>  "roles/healthcare.fhirStoreAdmin",<br/>  "roles/healthcare.hl7V2StoreAdmin",<br/>  "roles/pubsub.admin",<br/>  "roles/run.admin",<br/>  "roles/cloudscheduler.admin",<br/>  "roles/storage.admin"<br/>]</pre> | no |
| <a name="input_folder_id"></a> [folder\_id](#input\_folder\_id) | Optional numeric folder ID to create the new project under. Leave unset for a personal account. Mutually exclusive with org\_id; only used when create\_project = true. | `string` | `null` | no |
| <a name="input_github_repository"></a> [github\_repository](#input\_github\_repository) | The ONE GitHub repository, as "owner/name", whose Actions workflows may impersonate the deployer service account. Matched exactly (case-sensitive) against the `repository` claim of GitHub's OIDC token. | `string` | n/a | yes |
| <a name="input_github_repository_id"></a> [github\_repository\_id](#input\_github\_repository\_id) | Optional numeric ID of the GitHub repository (`gh api repos/OWNER/NAME --jq .id`). When set, the attribute condition also pins this ID, so a different repository later created under the same owner/name (after a rename or deletion) cannot match. Recommended. | `string` | `null` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the project (when created) and the state bucket, for example { app = "pulsebridge" }. Keys and values: lowercase letters, digits, hyphens and underscores; keys start with a letter. | `map(string)` | `{}` | no |
| <a name="input_org_id"></a> [org\_id](#input\_org\_id) | Optional numeric organization ID to create the new project under. Leave unset (the default) for a personal Google account with no organization. Mutually exclusive with folder\_id; only used when create\_project = true. | `string` | `null` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the GCP project to create (create\_project = true) or adopt (create\_project = false). Project IDs are globally unique, 6 to 30 characters, lowercase letters, digits and hyphens, starting with a letter. | `string` | n/a | yes |
| <a name="input_project_name"></a> [project\_name](#input\_project\_name) | Human-readable display name for a newly created project. Only used when create\_project = true. Defaults to project\_id. | `string` | `null` | no |
| <a name="input_state_bucket_location"></a> [state\_bucket\_location](#input\_state\_bucket\_location) | Location of the state bucket. Default us-central1: a single region inside the Cloud Storage always-free tier (us-central1, us-east1, us-west1), which is plenty for a few kilobytes of state. | `string` | `"us-central1"` | no |
| <a name="input_state_bucket_name"></a> [state\_bucket\_name](#input\_state\_bucket\_name) | Name of the GCS bucket that holds Terraform state for every root (env roots use one prefix each). Bucket names are global. Defaults to "<project\_id>-tfstate". | `string` | `null` | no |
| <a name="input_state_noncurrent_versions_to_keep"></a> [state\_noncurrent\_versions\_to\_keep](#input\_state\_noncurrent\_versions\_to\_keep) | How many older (noncurrent) versions of each state file the bucket keeps for recovery. Older ones are deleted by a lifecycle rule so the bucket does not grow forever. | `number` | `10` | no |
| <a name="input_wif_pool_id"></a> [wif\_pool\_id](#input\_wif\_pool\_id) | ID of the Workload Identity pool. 4-32 lowercase letters, digits and hyphens; the "gcp-" prefix is reserved. A deleted pool's ID cannot be reused for 30 days. | `string` | `"github-actions"` | no |
| <a name="input_wif_provider_id"></a> [wif\_provider\_id](#input\_wif\_provider\_id) | ID of the OIDC provider inside the pool. 4-32 lowercase letters, digits and hyphens; the "gcp-" prefix is reserved. | `string` | `"github-oidc"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_deployer_service_account_email"></a> [deployer\_service\_account\_email](#output\_deployer\_service\_account\_email) | Email of the deployer service account. GitHub Actions passes it as `service_account` to google-github-actions/auth. |
| <a name="output_project_id"></a> [project\_id](#output\_project\_id) | ID of the created or adopted project. Env roots pass it as their project\_id variable. |
| <a name="output_project_number"></a> [project\_number](#output\_project\_number) | Numeric project number. Used in Workload Identity principal strings and some service agent emails. |
| <a name="output_state_bucket_name"></a> [state\_bucket\_name](#output\_state\_bucket\_name) | Name of the GCS bucket for remote state. Goes in each env root's backend "gcs" block as `bucket` (with a per-env `prefix`). |
| <a name="output_workload_identity_provider"></a> [workload\_identity\_provider](#output\_workload\_identity\_provider) | Full resource name of the Workload Identity provider, projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>. GitHub Actions passes it as `workload_identity_provider` to google-github-actions/auth. |
<!-- END_TF_DOCS -->
