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
| A Workload Identity pool and GitHub OIDC provider, restricted to **one** repository, plus bindings that let that repository impersonate the deployer (and, opt-in, a read-only planner and a page deployer), each from its own CI contexts | CI authenticates with short-lived tokens. **No service account key is created anywhere.** |

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

On a brand-new project, a freshly enabled API can take a minute to become usable. If the first
apply fails with "API has not been used in project ... or it is disabled", wait and run
`terraform apply` again; it continues from where it stopped.

### Running it from a service account in another project

The procedure above assumes **a person with their own Google login**. A person never meets what
follows. An automated session that holds a **service account key** whose account lives in a
different project (its "home project"; a cloud agent session is in this situation) does: Google
charges API quota, and the "is this API enabled?" check, to the **caller's** project unless the
call names another one. On the first dev bootstrap (16 November 2026) the first apply stopped
with `Identity and Access Management (IAM) API has not been used in project <home project
number> before or it is disabled`: the number was the key's home project, not the new one, so
waiting and re-running (the advice above) does not help.

What worked: tell the `google` provider to bill its calls to the project being bootstrapped. The
beta provider already does (`billing_project`, see "Firebase"); the `google` provider reads the
same two settings from the environment:

```bash
USER_PROJECT_OVERRIDE=true GOOGLE_BILLING_PROJECT=<project-id> terraform plan
USER_PROJECT_OVERRIDE=true GOOGLE_BILLING_PROJECT=<project-id> terraform apply
```

This sends the header `x-goog-user-project: <project-id>` on each call, so the caller needs
`serviceusage.services.use` on that project (Owner has it) and the API must be enabled **there**.
For a project that does not exist yet (`create_project = true`), the very first calls (creating
the project, linking billing) cannot be billed to it; give the key's home project the Cloud
Billing and Service Usage APIs, or create the project first and adopt it. Not verified: the
dev bootstrap adopted an existing project.

The env roots then need one more step for this kind of caller (infra/envs/README.md, "Running
the env roots from a service account in another project").

Then:

- Env roots: copy the root's `backend.hcl.example` to `backend.hcl` (git-ignored), set `bucket =
  "<state_bucket_name>"` in it, and run `terraform init -backend-config=backend.hcl`. The prefix
  (`dev`, `prod`) is fixed in each root's committed `backend.tf` (infra/envs/README.md, step 2).
- GitHub Actions: the outputs `workload_identity_provider`, `state_bucket_name` and the account
  emails become repository variables (they are identifiers, not secrets). The exact names and the
  rest of the setup are in infra/envs/README.md, "CI/CD"; the identities CI uses are below, "CI
  identities".

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

Know the trade-off: the deployer has object admin on this bucket, so once bootstrap's state
lives here, the identity that bootstrap governs can rewrite or delete bootstrap's state. Keeping bootstrap's state local
(and backed up) avoids that, at the cost of it living on one machine.

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

## Firebase

`add_firebase` (default `true`) adds Firebase to the project (`google_firebase_project`, in the
`google-beta` provider), after enabling the Firebase Management API (`firebase.googleapis.com`).
The dashboard is served by Firebase Hosting (D4, module phr), and a Hosting site can only exist
in a Firebase project.

- **Why here.** Adding Firebase needs `firebase.projects.update`. Among predefined roles only
  owner, editor, `roles/firebase.admin` and `roles/firebase.editor` hold it, and the last two
  cover every Firebase product. Bootstrap already runs once with the owner's credentials, so it
  does this step, and the deployer keeps only `roles/firebasehosting.admin`.
- **It cannot be undone.** The provider's documentation says: "Once Firebase has been added to a
  Google Project it cannot be removed." `terraform destroy` forgets the resource; Firebase stays.
- **It costs nothing.** Firebase itself is free; Hosting bills only storage above 10 GB and
  transfer above 10 GB a month.
- **Opting out.** Set `add_firebase = false`. Then Firebase Hosting is unavailable, and module
  phr's Hosting site cannot be created, unless Firebase is added some other way later (for example
  `firebase projects:addfirebase <project-id>` by the owner, or module phr's
  `manage_firebase_project = true` with a deployer that holds `roles/firebase.admin`).
- **The beta provider** is configured with `user_project_override = true` and
  `billing_project = var.project_id`: the Firebase Management API refuses user credentials that
  carry no quota project, so its calls are billed to the project being bootstrapped. Only
  `firebase.tf` uses that provider.

## Deployer roles

The deployer must later create the whole stack: APIs, VPC and firewall, an e2-micro, Artifact
Registry, service accounts and their IAM bindings, Secret Manager secrets, Healthcare dataset and
stores, Pub/Sub, Cloud Run, Cloud Scheduler, log-based metrics and alerting, the Firebase Hosting site and, optionally, the image build trigger. The default `deployer_roles` is one
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
| `roles/logging.configWriter` | log-based metrics for the ingestion alerts (created on every apply) |
| `roles/monitoring.alertPolicyEditor` | alert policies on those metrics (only with an alert address or channel) |
| `roles/monitoring.notificationChannelEditor` | the optional email notification channel (module ingestion's `alert_email`) |
| `roles/firebasehosting.admin` | module phr's Firebase Hosting site, and deploying the page to it (T26) |
| `roles/cloudbuild.builds.editor` (only with `allow_build_trigger = true`) | project-base's opt-in Cloud Build trigger on release tags (T26): creating, changing and deleting a trigger needs `cloudbuild.builds.create`, which also lets the holder start builds, so it is not in the default list |

The deployer also gets `roles/storage.objectAdmin` on the state bucket itself: its only Cloud
Storage access (the stack creates no bucket), so it can always read and write state.

**What the role list does and does not limit.** For an account whose job is to create IAM,
"least privilege" has a hard ceiling:

- `roles/resourcemanager.projectIamAdmin` lets the deployer grant **any** role on the project,
  including to itself: Owner, `roles/resourcemanager.projectDeleter`, or a billing role. So a
  compromised deployer, or a malicious workflow in the trusted repository, can do anything an
  Owner can, including deleting the project or changing its billing. `deletion_policy =
  "PREVENT"` only stops *Terraform's own* `destroy` of a project bootstrap created; it is not a
  permission and stops nobody else.
- `roles/secretmanager.admin` can also read secret values. There is no predefined role that can
  create secrets and set their IAM without access.
- What the list does buy: the deployer does not, *by accident*, delete the project, change
  billing or touch products the stack does not use, because it would first have to grant itself
  a role to do so, and every grant is visible and reviewable in the IAM policy and audit logs.
- The real security boundary is who can become the deployer: Workload Identity Federation,
  below, and the repository's own branch protection and workflow review.
- Future hardening, not implemented yet: an IAM condition on the `projectIamAdmin` grant that
  allows only an explicit list of roles to be granted
  (`api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly([...])`). It is worth
  adding once the identity and other modules' role lists are stable.

Change the list with `deployer_roles` in `terraform.tfvars`.

**What the deployer cannot do, on purpose: read logs.** None of the roles above includes
`logging.logEntries.list`, so the deployer cannot read Cloud Logging (the runbook's checks that
read a log line, such as the seeder's `seed-run` outcome or fhir-proxy's `login-failed` lines,
need the owner or `roles/logging.viewer`; the first dev deploy confirmed it). It is left out
deliberately: CI's deployer has no need to read logs, and an apply identity that cannot read
them is one less way for request details to leak from a CI job. A person who wants to run those checks
as the deployer adds the role for that session (`deployer_roles`, a real IAM change at the next
bootstrap apply) or runs them as the owner. The VM's first-boot lines need no log access:
`gcloud compute instances get-serial-port-output` works with the deployer's compute roles.

**Service agents.** Module identity creates the Pub/Sub service agent with
`google_project_service_identity` (Service Usage `services.generateServiceIdentity`). Google's
reference names no IAM permission for that method; the deployer's
`roles/serviceusage.serviceUsageAdmin` is expected to cover it, but that is **not verified** (on
the first dev deploy the agent was created by hand, outside Terraform, before this resource
existed). If the deployer is refused, run that one
step as the owner: `terraform apply -target=module.stack.module.identity` (infra/envs/README.md,
step 4).

## Workload Identity Federation

GitHub Actions gets a short-lived token for the deployer instead of a stored key:

1. The workflow (with `permissions: id-token: write`) asks GitHub for a signed OIDC token whose
   claims describe the run, for example `repository`, `repository_id`, `repository_owner_id`,
   `ref`, `sub`.
2. `google-github-actions/auth` sends it to Google's Security Token Service (STS). STS checks the
   signature against GitHub's issuer `https://token.actions.githubusercontent.com`, maps the
   claims (`attribute_mapping`) and evaluates `attribute_condition`. If the condition is false,
   the exchange is refused.
3. STS returns a federated token whose attributes include `ci_context` (T26: where the job runs,
   below), for example `principalSet://.../attribute.ci_context/main`.
4. If that principal holds `roles/iam.workloadIdentityUser` on the account the job asks for (the
   deployer, the planner or the page deployer), the IAM Credentials API issues an access token for
   it, valid for about an hour.

### Why the repository's numeric ID is required

All GitHub repositories share the same issuer, so without the attribute condition **any**
repository could complete step 2. Matching the repository **name** is not enough: names are
reusable. If the repository is deleted or renamed, or its owner account is renamed, someone else
can create a new repository with the old `owner/name`, and its tokens carry exactly the same
`repository` claim. GitHub's numeric `repository_id` belongs to one repository forever, so the
condition requires both:

```
assertion.repository == "<owner>/<name>" && assertion.repository_id == "<id>"
```

`github_repository_owner_id` (optional) adds `assertion.repository_owner_id`, the same idea for
the owning account. Find the IDs with `gh api repos/<owner>/<name> --jq '.id, .owner.id'`, or in
the `id` and `owner.id` fields of `https://api.github.com/repos/<owner>/<name>`.

The impersonation bindings are keyed on `attribute.ci_context`, whose expression starts with the
numeric ID: a token from any other repository maps to `other-repository`, which nothing is bound
to. A `principalSet` can match only one attribute; folding the ID into the context keeps every
binding at least as strict as the condition, so a re-created repository stays out even if the
condition were loosened.

### What the condition does not restrict

By default the pool's condition says nothing about the branch (`ref`), the GitHub environment
(`environment`) or the triggering event (`event_name`): any workflow of the repository can get a
federated token. What it may then impersonate is decided per account by the bindings below (T26).
Before T26 the deployer was bound to the whole repository, so any workflow on any branch or tag,
including a `pull_request_target` workflow (which runs with the repository's permissions on
events from forks), could get deployer tokens, and a "manual approval before prod" that existed
only in workflow YAML could be bypassed by a workflow that skipped it.

What this root offers, and what T26's workflows use:

- **Per-account contexts** (T26). The provider maps one more attribute, `attribute.ci_context`, a
  single word for where a job runs (wif.tf has the expression):

  | Context | A job that is |
  | --- | --- |
  | `main` | on `refs/heads/main`, run by `push`, `schedule` or `workflow_dispatch`, without an environment (the branch name is fixed, as in the workflows) |
  | `pull-request` | a `pull_request` run without an environment |
  | `env-<name>-main` | in GitHub Environment `<name>`, on main, run by `push`, `schedule` or `workflow_dispatch` |
  | `env-<name>-other`, `other` | anything else: branch pushes, tags, `pull_request_target` (which runs on the base branch for fork events, hence the event check) |
  | `other-repository` | from another repository (the numeric ID differs) |

  Each account's `roles/iam.workloadIdentityUser` binding names its contexts
  (`principalSet://.../attribute.ci_context/<context>`), so the three CI identities are reachable
  from different places: `deployer_wif_contexts` (dev default `["main"]`), `planner_wif_contexts`
  (dev default `["pull-request"]`) and `page_deployer_wif_contexts` (dev default
  `["env-dev-page-main"]`: the page deployer always has its own GitHub Environment). Validations
  keep them apart: the three lists may share no context (a page deploy job, which runs npm
  packages, could otherwise ask for a token and become the deployer), "pull-request" is refused for
  the deployer and the page deployer, "main" for the page deployer, and with `github_environments`
  set (prod) every context must be one of those environments, so dev's defaults cannot pass there.
  Because the context starts with the repository check, a binding is never weaker than the pool's
  condition. `tests/tooling/test_wif_truth_table.py` evaluates the real mapping, condition and
  bindings for every event, ref, environment and repository (the table is in
  infra/envs/README.md, "Who can reach what").
- `github_environments` (optional) adds `assertion.environment in [...]` to the pool's condition.
  GitHub puts the `environment` claim into a job's OIDC token only when the job references a
  **GitHub Environment**, and only after that environment's protection rules (required reviewers,
  allowed branches) have passed. A token without the claim fails the condition. The field is also
  mapped (`attribute.environment`), because Google requires a mapping for every condition field.
- `github_allowed_refs` (optional) adds `assertion.ref in [...]`. Both options together are ANDed:
  for **prod**, set `github_allowed_refs = ["refs/heads/main"]` and `github_environments =
  ["prod-plan", "prod", "prod-page"]` (a test checks the combined condition). Pull-request workflows run on
  `refs/pull/<n>/merge`, so restricting refs blocks plan-on-PR, which prod does not do.
- Values of these options are validated to contain no quotes or backslashes, so they cannot inject
  CEL.
- **dev** keeps no pool-level ref or environment restriction (its plans run on pull requests and its
  applies without an environment, so a condition on either would refuse them); the bindings do the
  narrowing: pull requests reach only the read-only planner, jobs on main only the deployer, and
  jobs in the `dev-page` Environment on main only the page deployer.

### Limits and lifecycle

- `google.subject` is limited to 127 bytes. It is mapped from GitHub's `sub` claim
  (`repo:<owner>/<name>:ref:refs/heads/<branch>`, or `...:environment:<name>`), which can exceed
  that with long owner, repository or branch names; the token exchange then fails. If that
  happens, map `google.subject` to a shorter claim combination.
- A deleted pool or provider is soft-deleted for 30 days and its ID cannot be reused in that time.

## CI identities (T26)

Three accounts, each reachable only from its own CI contexts (above). The planner and the page
deployer are opt-in (`create_planner`, `create_page_deployer`); with neither, CI's plan and page
deploy jobs skip (they never fall back to the deployer).

| Account | dev contexts | prod contexts | Roles |
| --- | --- | --- | --- |
| deployer (`terraform-deployer`) | `main` (apply on merge, nightly destroy) | `env-prod-main` (the `prod` Environment) | `deployer_roles` (above), object admin on the state bucket |
| planner (`terraform-planner`) | `pull-request` | `env-prod-plan-main` (the `prod-plan` Environment) | one custom role (below); object **viewer** on the state bucket |
| page deployer (`page-deployer`) | `env-dev-page-main` (the `dev-page` Environment) | `env-prod-page-main` (the `prod-page` Environment) | `roles/firebasehosting.admin` and `roles/serviceusage.serviceUsageViewer` on the project; `roles/run.viewer` on fhir-proxy's Cloud Run service only, granted by the env root (below) |

**The page deployer's three grants** (T41). `roles/firebasehosting.admin` alone is not enough for
`firebase deploy --only hosting`; the first CI run on dev (18 November 2026) was refused twice
before it passed:

| Grant | Where | Why `firebase deploy` needs it |
| --- | --- | --- |
| `roles/firebasehosting.admin` | project (`google_project_iam_member.page_deployer`) | create a Hosting version, upload the files, release it |
| `roles/serviceusage.serviceUsageViewer` | project (`google_project_iam_member.page_deployer_service_usage`) | before deploying, the CLI checks that the Hosting API is enabled (`serviceusage.services.get` on `firebasehosting.googleapis.com`); without it: 403 "Permission denied to get service". The role reads which APIs are enabled, plus quota and operation metadata; no data and no changes |
| `roles/run.viewer` | fhir-proxy's Cloud Run service only (module phr, from the env roots' `page_deployer_service_account_email`) | finalizing a version whose config rewrites `/api` to Cloud Run makes Hosting check `run.services.get` on the rewrite's target; without it: 403 "Permission 'run.services.get' denied" on the proxy service. Granted on that one service, not the project, so the account cannot read the other services' configuration (their environment holds the patient ID) |

The third grant is not in bootstrap because the service is not: the env root creates it, and
bootstrap runs before it exists. Copy the output `page_deployer_service_account_email` into the
env root's tfvars (infra/envs/README.md, "Setting it up (once)").

**The planner's custom role** (`planner_role_id`, default `pulsebridgeTerraformPlanner`). Predefined
viewer roles read data: `roles/iam.securityReviewer` includes `healthcare.hl7V2Messages.list` (whose
full view returns message contents) and `logging.privateLogEntries.list`, `roles/compute.viewer`
the VM's serial port output and screenshots, `roles/logging.viewer` log entries. So the planner
gets exactly the read of each resource type the modules manage (what a `terraform plan` refresh
calls) and the IAM-policy read of each type that has `*_iam_member` resources:

| Permissions | For |
| --- | --- |
| `resourcemanager.projects.get`, `.getIamPolicy` | `data "google_project"`, `google_project_iam_member` |
| `serviceusage.services.get`, `serviceusage.services.list`, `serviceusage.services.use` | `google_project_service` (the provider refreshes it by listing the project's enabled services: without `.list` the first CI plan on dev was refused on every one); the beta provider bills its calls to the project (`user_project_override`) |
| `iam.serviceAccounts.get`, `.getIamPolicy` | the service accounts and their bindings |
| `secretmanager.secrets.get`, `.getIamPolicy`, `secretmanager.versions.get` | secrets' metadata, their bindings, the dev version's state; not `secretmanager.versions.access` |
| `artifactregistry.repositories.get`, `.getIamPolicy` | the two repositories (with the cleanup policy) and their bindings; no image pull |
| `compute.networks.get`, `subnetworks.get`, `firewalls.get`, `routers.get`, `addresses.get`, `disks.get`, `instances.get`, `resourcePolicies.get` | the network, the VM, its disks, address and snapshot schedule; not `getSerialPortOutput` or `getScreenshot` |
| `healthcare.datasets.get`, `fhirStores.get`, `fhirStores.getIamPolicy`, `hl7V2Stores.get`, `hl7V2Stores.getIamPolicy` | the dataset and the stores' configuration and bindings; no `fhirResources.*` or `hl7V2Messages.*` |
| `pubsub.topics.get`, `.getIamPolicy`, `pubsub.subscriptions.get`, `.getIamPolicy` | topic, subscriptions, bindings; no message pull |
| `run.services.get`, `.getIamPolicy`, `run.jobs.get`, `.getIamPolicy` | Cloud Run services, the seeder job, their bindings |
| `cloudscheduler.jobs.get` | the Scheduler job |
| `logging.logMetrics.get` | the log-based metrics' definitions, not log entries |
| `monitoring.alertPolicies.get`, `monitoring.notificationChannels.get` | alert policies and the email channel |
| `firebasehosting.sites.get`, `firebase.projects.get` | the Hosting site (and Firebase, when phr manages it) |
| `cloudbuild.builds.get` | the build trigger (`triggers.get` needs it) |

**What the planner can still read, plainly:** the Terraform **state** and the CI tfvars (object
viewer on the state bucket), which hold in clear the patient's identifier and, in prod, the
patient's name, and the alert address; Cloud Run services' configuration (their environment holds
the patient ID too); the alert channel's address; every IAM policy and resource configuration. It
reads no FHIR resource, HL7v2 message, log entry or secret value. A test checks the role for the
data-reading permissions (`healthcare.hl7V2Messages.*`, `healthcare.fhirResources.*`, `export`,
`import`, `ingest`, `logging.privateLogEntries.*`, `logging.logEntries.*`,
`secretmanager.versions.access`, `storage.objects.*`, `getSerialPortOutput`, `getScreenshot`), and a
static check that the planner and the page deployer are granted nothing else through any resource.

`planner_extra_permissions` adds to the role if a real plan is refused on one the list lacks; only
`<service>.<resource>.get` or `.getIamPolicy` that read no data are accepted. Creating a custom
role needs `iam.roles.create`, which the owner (who runs bootstrap) has; a deleted custom role
keeps its ID reserved for about 37 days.

The planner plans with `-lock=false`: taking the GCS backend's lock means writing and deleting a
lock object, and read-only is the point. A plan made while an apply runs may be stale; nothing is
applied from it (prod's apply plans again). **Whether this set suffices for a full plan of the
stack is not verified** (infra/envs/README.md, checklist CI16).

## Teardown

Bootstrap is built to be hard to destroy by accident. To really remove it, in this order:

1. Destroy every env root first (`infra/envs/*`), since their state lives in the bucket.
2. If bootstrap's own state is in the bucket, move it back to local state: delete
   `backend_override.tf` and run `terraform init -migrate-state`.
3. Remove `prevent_destroy = true` from `state.tf`. While it is there, `terraform destroy` fails
   at plan time.
4. Empty the bucket, including noncurrent versions (`gcloud storage rm --all-versions
   "gs://<bucket>/**"`). With `force_destroy = false`, deleting a bucket that still holds objects
   fails.
5. Run `terraform destroy`.
   - Adopt mode: this removes the deployer account, its grants, the pool and provider and the
     bucket. The project and its APIs stay (`disable_on_destroy = false`), and so does Firebase
     (it cannot be removed from a project). The pool and provider IDs stay reserved for 30 days.
   - Create mode: the destroy stops at the project, because `deletion_policy = "PREVENT"`
     refuses it. Either delete the project in the console (`gcloud projects delete`), or set
     `deletion_policy = "DELETE"`, apply, then destroy.

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
tests/static_checks.sh      # what terraform test cannot see (below)
```

`terraform test` uses `mock_provider "google"` (Terraform loads the provider's schema but makes no
API calls) with `override_during = plan`, so the mocked computed values in `tests/mocks/` are
known at plan time. The tests cover create vs adopt mode, the bucket's versioning and public
access prevention, the WIF attribute condition and binding, the deployer's roles, the outputs,
and that each variable validation rejects bad input.

`tests/static_checks.sh` scans every `*.tf` and `*.tf.json` file for things a test cannot assert:
no key resources, no module calls, no backend block, no authoritative IAM resources
(`*_iam_binding`, `*_iam_policy`, which would remove existing members of an adopted project), and
`prevent_destroy = true` on the state bucket.

## Reference

The section below is generated by [terraform-docs](https://terraform-docs.io/) (v0.20.0):
`terraform-docs markdown table --output-file README.md --output-mode inject .`

<!-- BEGIN_TF_DOCS -->
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
| [google-beta_google_firebase_project.this](https://registry.terraform.io/providers/hashicorp/google-beta/latest/docs/resources/google_firebase_project) | resource |
| [google_iam_workload_identity_pool.github](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool) | resource |
| [google_iam_workload_identity_pool_provider.github](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/iam_workload_identity_pool_provider) | resource |
| [google_project.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project) | resource |
| [google_project_iam_custom_role.planner](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_custom_role) | resource |
| [google_project_iam_member.deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_iam_member.page_deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_iam_member.page_deployer_service_usage](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_iam_member.planner](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_iam_member) | resource |
| [google_project_service.bootstrap](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_service) | resource |
| [google_service_account.deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account.page_deployer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account.planner](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account) | resource |
| [google_service_account_iam_member.github_wif](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/service_account_iam_member) | resource |
| [google_storage_bucket.tfstate](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/storage_bucket) | resource |
| [google_storage_bucket_iam_member.deployer_state](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/storage_bucket_iam_member) | resource |
| [google_storage_bucket_iam_member.planner_state](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/storage_bucket_iam_member) | resource |
| [google_project.existing](https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/project) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_add_firebase"></a> [add\_firebase](#input\_add\_firebase) | Add Firebase to the project (google\_firebase\_project), with the Firebase Management API (firebase.googleapis.com) enabled first. Firebase Hosting, which serves the dashboard (D4, module phr), needs it. Default true. Bootstrap runs once with the owner's credentials, and adding Firebase needs firebase.projects.update (owner or editor), so doing it here means the deployer never needs roles/firebase.admin. It CANNOT BE UNDONE: "Once Firebase has been added to a Google Project it cannot be removed" (provider docs). It costs nothing. false: no Firebase; then Hosting is unavailable and module phr's site cannot be created, unless Firebase is added another way (README, "Firebase"). | `bool` | `true` | no |
| <a name="input_allow_build_trigger"></a> [allow\_build\_trigger](#input\_allow\_build\_trigger) | Grant the deployer roles/cloudbuild.builds.editor, which creating project-base's Cloud Build trigger needs (T26). Default false: set it only together with build\_trigger\_repository in an env root. The role also lets the holder start builds. | `bool` | `false` | no |
| <a name="input_auto_create_network"></a> [auto\_create\_network](#input\_auto\_create\_network) | Only for a newly created project: keep the 'default' VPC network GCP creates automatically. Default false, because that network ships with firewall rules open to the internet (SSH, RDP, ICMP) and the stack builds its own VPC (module project-base). With false, the provider enables the Compute Engine API to delete the default network. | `bool` | `false` | no |
| <a name="input_billing_account"></a> [billing\_account](#input\_billing\_account) | Billing account ID (format XXXXXX-XXXXXX-XXXXXX) to link to a newly created project. Required when create\_project = true; must be unset when adopting, because bootstrap does not change an existing project's billing. | `string` | `null` | no |
| <a name="input_create_page_deployer"></a> [create\_page\_deployer](#input\_create\_page\_deployer) | Create the page deployer service account (ci\_identities.tf, T26) used by CI's page deploy jobs: roles/firebasehosting.admin and roles/serviceusage.serviceUsageViewer on the project, nothing else here (the env roots add roles/run.viewer on fhir-proxy's service only; T41). Default false. | `bool` | `false` | no |
| <a name="input_create_planner"></a> [create\_planner](#input\_create\_planner) | Create the read-only planner service account (ci\_identities.tf, T26) that CI's terraform plan runs as. Default false. Opt in for CI. | `bool` | `false` | no |
| <a name="input_create_project"></a> [create\_project](#input\_create\_project) | true: create a new GCP project with var.project\_id and link var.billing\_account to it. false: adopt an existing project with var.project\_id and create nothing at project level. No default, so the choice is always explicit. | `bool` | n/a | yes |
| <a name="input_deployer_account_id"></a> [deployer\_account\_id](#input\_deployer\_account\_id) | account\_id (the part before @) of the deployer service account that env roots and CI run as. 6-30 characters. | `string` | `"terraform-deployer"` | no |
| <a name="input_deployer_roles"></a> [deployer\_roles](#input\_deployer\_roles) | Project-level IAM roles granted to the deployer service account. The default is the smallest set of predefined roles that can create the whole PulseBridge stack; see README.md ("Deployer roles") for the reasoning. Basic roles (owner, editor) are rejected. | `list(string)` | <pre>[<br/>  "roles/serviceusage.serviceUsageAdmin",<br/>  "roles/compute.networkAdmin",<br/>  "roles/compute.securityAdmin",<br/>  "roles/compute.instanceAdmin.v1",<br/>  "roles/artifactregistry.admin",<br/>  "roles/iam.serviceAccountAdmin",<br/>  "roles/iam.serviceAccountUser",<br/>  "roles/resourcemanager.projectIamAdmin",<br/>  "roles/secretmanager.admin",<br/>  "roles/healthcare.datasetAdmin",<br/>  "roles/healthcare.fhirStoreAdmin",<br/>  "roles/healthcare.hl7V2StoreAdmin",<br/>  "roles/pubsub.admin",<br/>  "roles/run.admin",<br/>  "roles/cloudscheduler.admin",<br/>  "roles/logging.configWriter",<br/>  "roles/monitoring.alertPolicyEditor",<br/>  "roles/monitoring.notificationChannelEditor",<br/>  "roles/firebasehosting.admin"<br/>]</pre> | no |
| <a name="input_deployer_wif_contexts"></a> [deployer\_wif\_contexts](#input\_deployer\_wif\_contexts) | The CI contexts (wif.tf) whose jobs may impersonate the deployer. Default ["main"] is dev's: jobs on main run by push, schedule or workflow\_dispatch (the dev apply and the nightly destroy). prod: ["env-prod-main"] (only jobs in the prod GitHub Environment on main); with github\_environments set, "main" is refused. Never "pull-request". | `list(string)` | <pre>[<br/>  "main"<br/>]</pre> | no |
| <a name="input_folder_id"></a> [folder\_id](#input\_folder\_id) | Optional numeric folder ID to create the new project under. Leave unset for a personal account. Mutually exclusive with org\_id; only used when create\_project = true. | `string` | `null` | no |
| <a name="input_github_allowed_refs"></a> [github\_allowed\_refs](#input\_github\_allowed\_refs) | Optional list of git refs (for example ["refs/heads/main"]) allowed to obtain deployer tokens. Empty (the default) allows every branch, tag and pull-request ref in the repository; see README.md ("What the condition does not restrict"). Note that pull-request workflows run on refs/pull/N/merge, so restricting refs also blocks plan-on-PR with this identity. | `list(string)` | `[]` | no |
| <a name="input_github_environments"></a> [github\_environments](#input\_github\_environments) | Optional list of GitHub Environment names (for example ["prod-plan", "prod"]) whose jobs alone may obtain deployer tokens. Empty (the default) adds nothing. When set, the attribute condition also requires `assertion.environment in [...]`: only a job that references one of these environments gets a token, and GitHub applies the environment's protection rules (required reviewers, allowed branches) BEFORE such a job starts, so the gate is enforced by GitHub rather than by workflow YAML. Use it for the prod project (infra/envs/README.md, "CI/CD"). Pull-request plans reference no environment, so a project with this set cannot be planned from pull requests. | `list(string)` | `[]` | no |
| <a name="input_github_repository"></a> [github\_repository](#input\_github\_repository) | The ONE GitHub repository, as "owner/name", whose Actions workflows may impersonate the deployer service account. Matched exactly (case-sensitive) against the `repository` claim of GitHub's OIDC token. | `string` | n/a | yes |
| <a name="input_github_repository_id"></a> [github\_repository\_id](#input\_github\_repository\_id) | Numeric ID of the same GitHub repository: the `id` field of https://api.github.com/repos/OWNER/NAME, or `gh api repos/OWNER/NAME --jq .id`. Required. Names can be reused after a repository is deleted or renamed, or its owner account is renamed; the numeric ID never is. Both the attribute condition and the impersonation binding match on it. | `string` | n/a | yes |
| <a name="input_github_repository_owner_id"></a> [github\_repository\_owner\_id](#input\_github\_repository\_owner\_id) | Optional numeric ID of the repository's owner (user or organization): the `owner.id` field of https://api.github.com/repos/OWNER/NAME, or `gh api repos/OWNER/NAME --jq .owner.id`. When set, the attribute condition also requires `assertion.repository_owner_id` to match. | `string` | `null` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the project (when created) and the state bucket, for example { app = "pulsebridge" }. Keys and values: lowercase letters, digits, hyphens and underscores; keys start with a letter. | `map(string)` | `{}` | no |
| <a name="input_org_id"></a> [org\_id](#input\_org\_id) | Optional numeric organization ID to create the new project under. Leave unset (the default) for a personal Google account with no organization. Mutually exclusive with folder\_id; only used when create\_project = true. | `string` | `null` | no |
| <a name="input_page_deployer_account_id"></a> [page\_deployer\_account\_id](#input\_page\_deployer\_account\_id) | account\_id of the page deployer service account. | `string` | `"page-deployer"` | no |
| <a name="input_page_deployer_wif_contexts"></a> [page\_deployer\_wif\_contexts](#input\_page\_deployer\_wif\_contexts) | The CI contexts whose jobs may impersonate the page deployer: its own GitHub Environment on main, never a context of the deployer or the planner (a page job runs npm packages, which could otherwise ask for their token). Default ["env-dev-page-main"] is dev's; prod: ["env-prod-page-main"]. | `list(string)` | <pre>[<br/>  "env-dev-page-main"<br/>]</pre> | no |
| <a name="input_planner_account_id"></a> [planner\_account\_id](#input\_planner\_account\_id) | account\_id of the planner service account. | `string` | `"terraform-planner"` | no |
| <a name="input_planner_extra_permissions"></a> [planner\_extra\_permissions](#input\_planner\_extra\_permissions) | Permissions added to the planner's custom role (ci\_identities.tf), if a real plan is refused on one the built-in list lacks. Only `<service>.<resource>.get` or `.getIamPolicy` are accepted, and never one that reads data (FHIR resources, HL7v2 messages, log entries, secret values, objects). | `list(string)` | `[]` | no |
| <a name="input_planner_role_id"></a> [planner\_role\_id](#input\_planner\_role\_id) | ID of the planner's custom project role. A deleted custom role keeps its ID reserved for about 37 days. | `string` | `"pulsebridgeTerraformPlanner"` | no |
| <a name="input_planner_wif_contexts"></a> [planner\_wif\_contexts](#input\_planner\_wif\_contexts) | The CI contexts whose jobs may impersonate the planner. Default ["pull-request"] is dev's. prod: ["env-prod-plan-main"] (the prod-plan Environment on main); with github\_environments set, "pull-request" is refused. | `list(string)` | <pre>[<br/>  "pull-request"<br/>]</pre> | no |
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
| <a name="output_firebase_added"></a> [firebase\_added](#output\_firebase\_added) | Whether bootstrap added Firebase to the project (add\_firebase). module phr's Hosting site needs Firebase; with false, add it another way or leave the dashboard unhosted. |
| <a name="output_page_deployer_service_account_email"></a> [page\_deployer\_service\_account\_email](#output\_page\_deployer\_service\_account\_email) | Email of the page deployer (create\_page\_deployer), or null. GitHub repository variable <ENV>\_GCP\_PAGE\_DEPLOYER\_SA. |
| <a name="output_planner_service_account_email"></a> [planner\_service\_account\_email](#output\_planner\_service\_account\_email) | Email of the read-only planner (create\_planner), or null. GitHub repository variable <ENV>\_GCP\_PLANNER\_SA (infra/envs/README.md, "CI/CD"). |
| <a name="output_project_id"></a> [project\_id](#output\_project\_id) | ID of the created or adopted project. Env roots pass it as their project\_id variable. |
| <a name="output_project_number"></a> [project\_number](#output\_project\_number) | Numeric project number. Used in Workload Identity principal strings and some service agent emails. |
| <a name="output_state_bucket_name"></a> [state\_bucket\_name](#output\_state\_bucket\_name) | Name of the GCS bucket for remote state. Each env root takes it in its git-ignored backend.hcl (copied from backend.hcl.example: bucket = "<this>"), given to `terraform init -backend-config=backend.hcl`; the prefix is fixed in the root's backend.tf. CI passes it as TF\_STATE\_BUCKET (infra/envs/README.md, "CI/CD"). |
| <a name="output_workload_identity_provider"></a> [workload\_identity\_provider](#output\_workload\_identity\_provider) | Full resource name of the Workload Identity provider, projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>. GitHub Actions passes it as `workload_identity_provider` to google-github-actions/auth. |
<!-- END_TF_DOCS -->
