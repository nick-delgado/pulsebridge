# Module `project-base`

The ground every other PulseBridge module stands on. It:

- enables the project's Google APIs (a variable with a default covering the stack);
- creates a custom VPC with two subnets: `edge` for the e2-micro VM and `serverless` for Cloud Run
  Direct VPC egress;
- sets a deny-by-default firewall with one allow rule the design needs (Cloud Run to the VM on the
  MLLP and OpenEMR HTTPS ports), plus an optional IAP-only SSH rule;
- optionally creates Cloud Router and Cloud NAT (off by default, billed hourly);
- creates an Artifact Registry Docker repository for the service images, with a cleanup policy
  (T26), and, by default, a Docker Hub pull-through cache for the VM;
- optionally (T26) gives the image-build account its two repository grants and creates a Cloud
  Build trigger that builds the images on release tags.

It does not create service accounts (module `identity`). Its IAM bindings are on the two
repositories it owns, and only those: `roles/artifactregistry.reader` on the Docker Hub mirror for
the edge VM's account, when `grant_edge_vm_mirror_reader` is true (the default; then
`edge_vm_service_account_email` must be set, which a precondition checks), and, when
`grant_build_account_registry_access` is true, writer on the services repository and reader on
the mirror for the build account (see "Image builds").
It contains no provider or backend configuration: the env root configures those.

## APIs

`enabled_apis` is a set with a default covering every component in the brief; each entry becomes
one `google_project_service` through `for_each`. Notes on what is and is not in it:

- `identitytoolkit.googleapis.com` (Identity Platform) is rejected by a validation: decision D16
  (which replaced D9's shared password) lets visitors in with a reCAPTCHA bot check and a
  session cookie set by `fhir-proxy`.
- `recaptchaenterprise.googleapis.com` (reCAPTCHA, now part of Google Cloud Fraud Defense) is in
  the list for D16: module phr creates the score-based site key and fhir-proxy creates an
  assessment per "View the live demo" click. Enabling it costs nothing; assessments are free up
  to 10,000 a month per organisation (T48 documents the cost).
- `storage.googleapis.com` also serves module phr's demo counter bucket (D16, the daily bot-check
  cap).
- `iap.googleapis.com` is added automatically only when `enable_iap_ssh = true`. IAP TCP
  forwarding is understood to need this API; that is not confirmed against Google's documentation.
- `sts.googleapis.com` (Security Token Service, used by Workload Identity Federation) is not in
  the list: the bootstrap root (T02) enables it, because CI needs it before any env root runs.
- `firebase.googleapis.com` and `firebasehosting.googleapis.com` are in the list for Firebase
  Hosting (D4): module phr's site and the page deploys, and adding Firebase to the project
  (bootstrap's `add_firebase`). Enabling them costs nothing.
- `monitoring.googleapis.com` is in the list for module ingestion: its alert policies, and
  charting its log-based metrics, need the Monitoring API. Enabling it costs nothing.
- APIs stay enabled on destroy (`disable_on_destroy = false`), see "Idle cost".

## Usage

```hcl
module "project_base" {
  source = "../../modules/project-base"

  project_id = var.project_id
  region     = var.region

  # The edge VM's account: firewall rules target it instead of the network tag, and it gets
  # read access to the Docker Hub mirror (the edge VM cannot pull its images without this).
  edge_vm_service_account_email = module.identity.service_account_emails["edge-vm"]
}
```

Consumers:

- `edge-vm` (T21): put the VM's interface on `edge_subnet_id`, tag it with `edge_vm_network_tag`
  (or run it as the service account passed in), publish MLLP on `mllp_port` and OpenEMR on
  `openemr_https_port` on the VM's interface, and pull images from `dockerhub_mirror_url`
  (OpenEMR, MariaDB) and Artifact Registry. T21 does not need its own MLLP firewall rule; this
  module owns it. This module's posture needs no external IP on the VM for the Cloud Run paths
  (MLLP and the FHIR calls from `fhir-proxy`). Whether the VM gets one anyway, for the OpenEMR
  chart link reviewers open or for image pulls, is decided in T21/T23.
- `phr` (T22): for the cuff simulator and `fhir-proxy`, set Direct VPC egress with
  `network_name`, `serverless_subnet_name` and `egress = "PRIVATE_RANGES_ONLY"`, except
  `fhir-proxy`, which uses `egress = "ALL_TRAFFIC"`: its calls to internal-only Cloud Run services
  (`health-sync` `/status`, the cuff simulator) count as internal only when routed through the VPC,
  and this subnet has Private Google Access, so no NAT is needed (module ingestion's README,
  "Ingress").
- `ingestion` (T20): needs nothing from the network; `health-sync` and `v2-to-fhir` call only
  Google APIs.
- Container builds (T24): the services repository sets `immutable_tags = true`, so a tag can be
  pushed once and never moved. Builds must push a unique tag per build (for example the git tag or
  commit) and Cloud Run must deploy by digest. Never re-push a moving tag such as `latest`: the
  push is refused.

## How Cloud Run reaches the edge VM

```
cuff simulator / fhir-proxy (Cloud Run)
   | Direct VPC egress, PRIVATE_RANGES_ONLY: only 10.x traffic enters the VPC
   v
serverless subnet 10.10.1.0/24 ---- firewall: allow tcp 2575, 443 from 10.10.1.0/24 ---->
edge subnet 10.10.0.0/24 -> e2-micro (no external IP needed for this path)
```

Options considered:

| Option | What it needs | Idle cost | Verdict |
| --- | --- | --- | --- |
| **Direct VPC egress** (chosen) | A subnet of /26 or larger that Cloud Run instances take addresses from | None: billed only for network egress | Chosen |
| Serverless VPC Access connector | A /28 subnet and at least 2 connector VMs (e2-micro by default) that run all the time | The connector VMs bill as Compute Engine around the clock (third-party estimates about $15-20/month for 2 e2-micro; not confirmed against Google's pricing page) | Rejected: always-on cost |
| Public IP on the VM, firewall limited to the Cloud Run NAT IP (the brief's first wording) | Cloud Run must still route its egress into a VPC (Direct VPC or a connector) to get a fixed address, plus Cloud NAT with a static IP, plus a public MLLP port | Cloud NAT hourly fees plus a static IP (about $4.70/month) on top of whichever VPC path is used | Rejected: costs more, needs everything option 1 needs, and exposes MLLP publicly |

Why the firewall source is a subnet range: Google's Direct VPC page says network tags and service
identity are not supported as the *source* of ingress firewall rules for Cloud Run traffic, and
recommends the Cloud Run subnet range instead. A dedicated `serverless` subnet makes that range
mean "the Cloud Run services we attached", nothing else. Any Cloud Run service later attached to
that subnet can also reach the VM, so attach only the cuff simulator and `fhir-proxy`.

## How the VM gets images without NAT

With no external IP and no Cloud NAT, the VM can reach Google APIs only, through Private Google
Access on the edge subnet. It cannot reach Docker Hub, apt mirrors or any other internet host.
Spike S2 pulled images over an external IP; that path does not exist here by default.

- OpenEMR and MariaDB images come from Docker Hub. The Docker Hub remote repository
  (`create_dockerhub_mirror = true`) is a pull-through cache: the VM pulls from
  `<region>-docker.pkg.dev/...` and Artifact Registry fetches from Docker Hub on Google's side.
  Pin by digest as before; the digest of a cached image is the upstream digest.
- The MLLP adapter image is on `gcr.io`, which is served by Artifact Registry and should be
  reachable through Private Google Access (not confirmed against primary documentation).
- **Consequence for T21:** without an external IP or NAT, the VM cannot `apt-get install
  docker.io` as the spikes did. Use an image with Docker preinstalled (Container-Optimized OS), or
  turn on `enable_cloud_nat` (about $4.70/month plus data), or give the VM an external IP (billed
  per hour; T21's choice).
- The VM's service account needs `roles/artifactregistry.reader` on the Docker Hub mirror. This
  module grants it, on that repository only, when `grant_edge_vm_mirror_reader` is true (the
  default), and a precondition requires `edge_vm_service_account_email` then (T25 sets it). The
  grant's `count` uses only the two bools, never "is the email set": the email comes from module
  identity and is unknown until the account exists, so a count on it fails a root's first plan
  with "Invalid count argument" (found in T25; `tests/unknown_email/` is the harness that proves
  the count is known). The `gcr.io` adapter image is public and needs no grant.

## Image builds (T26)

`cloudbuild.yaml` builds every `services/*/Dockerfile` and pushes the images here
(services/README.md, "Container images"). This module holds the parts of that which live on its
repositories:

- **Cleanup policy on the services repository.** The repository has immutable tags, so nothing
  ever replaces an image and every release would otherwise stay forever. Two policies, which
  Artifact Registry applies to each image (package) separately:
  `delete-older-than-limit` (DELETE versions older than `services_delete_older_than_days`, 90 by
  default, tagged or not) and `keep-most-recent` (KEEP the newest `services_keep_recent_versions`,
  10 by default). A KEEP policy wins over DELETE, so the newest ten of each image are never
  deleted, however old. That matters because Cloud Run pulls the image by digest again whenever it
  starts an instance: an image a revision still uses must stay. A cleanup policy cannot exclude the
  digests Cloud Run uses (its conditions are tag state, tag and package name prefixes, age and
  version count, nothing about who pulls an image), so a deployed image that is older than the
  limit and outside the newest ten would be deleted; whether Cloud Run then still starts new
  instances of that revision is not verified (infra/envs/README.md, CI14). Deploy a release at
  least every 90 days, or raise `services_delete_older_than_days`. `services_cleanup_dry_run =
  true` only logs what would be deleted. Artifact Registry runs cleanup about once a day (not
  verified).
- **The build account's grants** (`build_service_account_email`, module identity's `build`
  account, with `grant_build_account_registry_access = true`): `roles/artifactregistry.writer` on
  the services repository (push, read tags, add a tag to an existing manifest; it cannot delete
  images or change the repository) and `roles/artifactregistry.reader` on the Docker Hub mirror
  (pull, and cache, the base image). Same pattern as the edge VM's grant: a plain bool decides the
  count, a precondition requires the email.
- **The trigger** (`build_trigger_repository`, opt-in, default null). A Cloud Build **trigger** is a
  rule "when this happens in the repository, run this build". This one fires on every git tag
  matching `build_trigger_tag_pattern` (release tags `v1.2.3` by default), runs `cloudbuild.yaml`
  from that commit **as the build account** (`service_account`), and passes this module's region
  and repository IDs as the `_REGION`, `_REPOSITORY` and `_DOCKERHUB_MIRROR` substitutions. It
  reads the source through a **2nd-gen repository connection**: a link between the project and
  GitHub made with the Cloud Build GitHub App. Making it needs a person with admin rights on the
  GitHub repository and Owner on the project, in the console, so Terraform does not create it:

  1. Console, Cloud Build, Repositories, **2nd gen**, Create host connection: GitHub, region =
     this module's `region`, name for example `github`; authorise and install the Cloud Build
     GitHub App on your repository only.
  2. Link repository: pick the repository. Its resource name is
     `projects/<project>/locations/<region>/connections/<connection>/repositories/<repository>`.
  3. Set that as `build_trigger_repository` in the env root's tfvars and apply. The validations
     require it to be in this project and in `region` (a 2nd-gen trigger lives in its
     repository's region).

  Each triggered build **waits for an approval** (`approval_config`, on by default with
  `build_trigger_approval_required = true`) by someone with `roles/cloudbuild.builds.approver`
  (the project Owner has it): without it, anyone who may push a `v*` tag publishes images. With a
  GitHub tag ruleset that limits who may create, move or delete `v*` tags, the approval can be
  turned off.

  The deployer needs `roles/cloudbuild.builds.editor` to create the trigger (bootstrap's
  `allow_build_trigger = true`, off by default) and `iam.serviceAccounts.actAs` on the build
  account (its project-level `roles/iam.serviceAccountUser`). Whether it also needs read access to the
  connection's repository (`cloudbuild.repositories.get`, in `roles/cloudbuild.connectionViewer`)
  is **not verified**; if the first apply is refused on that permission, add that role to
  `deployer_roles`.

A tag push then builds in Cloud Build; the build log ends with the `BEGIN tfvars` block to paste
into the env root's tfvars (infra/envs/README.md, step 7). Nothing deploys automatically.

## Idle cost

| Resource | Idle cost |
| --- | --- |
| Enabled APIs, VPC, subnets, firewall rules, Cloud Router | $0 |
| Artifact Registry | Storage only: $0.10/GB-month beyond 0.5 GB free (cached OpenEMR image is the largest item); not confirmed against primary documentation. The cleanup policy caps the services repository at about ten releases per image |
| Cloud Build trigger | $0: a trigger costs nothing; each build is billed per build-minute beyond the free allowance (services/README.md, "Cost") |
| Cloud NAT (off by default) | $0.0014 per VM-hour + $0.005 per NAT IP-hour + $0.045/GiB processed |
| Firewall and VPC flow logging | Off (bills per GB of logs) |

`terraform destroy` leaves the APIs enabled on purpose (`disable_on_destroy = false`): disabling
them can fail while other resources use them, and an enabled API with nothing in it costs nothing.

## Tests

```bash
cd infra/modules/project-base
terraform init -backend=false
terraform test
```

The tests use `mock_provider "google"` and `command = plan`: no credentials, no API calls.
`override_resource` blocks give the VPC and subnets known ids at plan time, so the tests can
check that every subnet, firewall rule and the Cloud Router is attached to this VPC.

A static check covers what `terraform test` cannot (a test cannot list every resource of a type):

```bash
infra/modules/project-base/tests/static_checks.sh
```

It fails if any `google_compute_firewall` other than the `allow` map and `deny_all_ingress` is
declared, or if the module calls another module. `make tf-test` runs both.

T26 added: the cleanup policy's defaults and variables; no build grant or trigger by default; the
build account as writer on the services repository and reader on the mirror, nothing more; the
grants refused without an account; the trigger (location, `cloudbuild.yaml`, the build account,
the repository, tags only, the substitutions, an empty mirror substitution without the mirror),
refused without the build account and with a repository in another region or project or of the
wrong form; the approval on by default and switchable off; `tests/unknown_email/` also feeds an unknown build account email with the trigger on,
so their counts are known on a first plan.

## Not confirmed against primary documentation

Google's documentation hosts could not be reached while this module was written; these points
come from search results and must be confirmed:

- Network tags and service identity cannot be the source of an ingress rule for Cloud Run Direct
  VPC egress traffic (why the source is the subnet range).
- Direct VPC egress needs a /26 or larger subnet and about four addresses per instance.
- Cloud NAT, Serverless VPC Access connector and Artifact Registry storage prices.
- Artifact Registry (`*.pkg.dev`) is reachable over Private Google Access. The Docker Hub mirror
  path depends on this, so **T21 must confirm it** on the VM before relying on the mirror.
- `gcr.io` (the MLLP adapter image) is reachable over Private Google Access.
- `health.googleapis.com` is the Google Health API's service name.
- IAP TCP forwarding requires `iap.googleapis.com`.

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
| [google_artifact_registry_repository.dockerhub](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository) | resource |
| [google_artifact_registry_repository.services](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository) | resource |
| [google_artifact_registry_repository_iam_member.dockerhub_mirror_build_reader](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository_iam_member) | resource |
| [google_artifact_registry_repository_iam_member.dockerhub_mirror_edge_vm_reader](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository_iam_member) | resource |
| [google_artifact_registry_repository_iam_member.services_build_writer](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository_iam_member) | resource |
| [google_cloudbuild_trigger.images_on_tag](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloudbuild_trigger) | resource |
| [google_compute_firewall.allow](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_firewall) | resource |
| [google_compute_firewall.deny_all_ingress](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_firewall) | resource |
| [google_compute_network.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_network) | resource |
| [google_compute_router.nat](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_router) | resource |
| [google_compute_router_nat.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_router_nat) | resource |
| [google_compute_subnetwork.edge](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_subnetwork) | resource |
| [google_compute_subnetwork.serverless](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_subnetwork) | resource |
| [google_project_service.this](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/project_service) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_build_service_account_email"></a> [build\_service\_account\_email](#input\_build\_service\_account\_email) | Email of the image-build service account (module identity's "build" account, T26). With grant\_build\_account\_registry\_access it gets roles/artifactregistry.writer on the services repository and roles/artifactregistry.reader on the Docker Hub mirror, those two repositories only; the build trigger runs as it. null: no build account. | `string` | `null` | no |
| <a name="input_build_trigger_approval_required"></a> [build\_trigger\_approval\_required](#input\_build\_trigger\_approval\_required) | Triggered builds wait for a manual approval in the Cloud Build console (roles/cloudbuild.builds.approver) before they run. Default true: anyone who can push a v* tag can otherwise publish images. Set false once a GitHub tag ruleset limits who may create v* tags. | `bool` | `true` | no |
| <a name="input_build_trigger_repository"></a> [build\_trigger\_repository](#input\_build\_trigger\_repository) | Opt-in Cloud Build trigger (T26): the full resource name of a 2nd-gen Cloud Build repository, projects/<project>/locations/<region>/connections/<connection>/repositories/<repository>, which you create once in the console by connecting GitHub (it needs the Cloud Build GitHub App and your GitHub login, so Terraform cannot do it). Its location must be var.region. When set, a trigger runs cloudbuild.yaml as the build account on every tag matching build\_trigger\_tag\_pattern. null (default): no trigger. | `string` | `null` | no |
| <a name="input_build_trigger_tag_pattern"></a> [build\_trigger\_tag\_pattern](#input\_build\_trigger\_tag\_pattern) | RE2 regular expression of the git tags that start a build (cloudbuild.yaml: "Meant to run on a git tag"). Default: release tags like v1.2.3. | `string` | `"^v[0-9]+\\.[0-9]+\\.[0-9]+$"` | no |
| <a name="input_create_dockerhub_mirror"></a> [create\_dockerhub\_mirror](#input\_create\_dockerhub\_mirror) | Create an Artifact Registry remote repository that proxies and caches Docker Hub, so the edge VM can pull the OpenEMR and MariaDB images through Private Google Access without Cloud NAT or an external IP. No hourly cost; cached images are billed as Artifact Registry storage. | `bool` | `true` | no |
| <a name="input_docker_repository_id"></a> [docker\_repository\_id](#input\_docker\_repository\_id) | ID of the Artifact Registry Docker repository that holds the service images built by Cloud Build. | `string` | `"pulsebridge-services"` | no |
| <a name="input_dockerhub_mirror_repository_id"></a> [dockerhub\_mirror\_repository\_id](#input\_dockerhub\_mirror\_repository\_id) | ID of the Docker Hub remote (mirror) repository, used when create\_dockerhub\_mirror is true. | `string` | `"pulsebridge-dockerhub"` | no |
| <a name="input_edge_subnet_cidr"></a> [edge\_subnet\_cidr](#input\_edge\_subnet\_cidr) | Primary IPv4 range of the edge subnet, where the e2-micro VM (OpenEMR, MariaDB, MLLP adapter) gets its internal address. Must not overlap serverless\_subnet\_cidr. | `string` | `"10.10.0.0/24"` | no |
| <a name="input_edge_vm_service_account_email"></a> [edge\_vm\_service\_account\_email](#input\_edge\_vm\_service\_account\_email) | Email of the edge VM's service account (created by the identity module). When set, (1) firewall rules target that service account instead of the edge network tag, which is stronger: anyone allowed to edit an instance can add a tag, but attaching a service account needs IAM permission on it; and (2) the account gets roles/artifactregistry.reader on the Docker Hub mirror repository, which the edge VM needs to pull MariaDB and OpenEMR. Leave null only when no edge VM is deployed: the VM cannot pull its images without it. | `string` | `null` | no |
| <a name="input_enable_cloud_nat"></a> [enable\_cloud\_nat](#input\_enable\_cloud\_nat) | Create a Cloud Router and Cloud NAT gateway so the edge VM can reach the public internet (apt, Docker Hub) without an external IP. Off by default because it is billed by the hour: about $0.0014 per VM per hour using the gateway, plus $0.005 per hour per NAT IP address, plus $0.045 per GiB processed (about $4.70 a month for one VM and one IP before data: 730 h x ($0.0014 + $0.005)). | `bool` | `false` | no |
| <a name="input_enable_iap_ssh"></a> [enable\_iap\_ssh](#input\_enable\_iap\_ssh) | Allow SSH (TCP 22) to the edge VM from Identity-Aware Proxy's TCP forwarding range only (35.235.240.0/20), for `gcloud compute ssh --tunnel-through-iap`. Off by default: nothing else in the design needs SSH. When true, the module also enables iap.googleapis.com, which IAP TCP forwarding is understood to require (not confirmed against Google's documentation). | `bool` | `false` | no |
| <a name="input_enabled_apis"></a> [enabled\_apis](#input\_enabled\_apis) | Google APIs (service names) to enable in the project. The default covers every component in<br/>the brief. Identity Platform (identitytoolkit.googleapis.com) is deliberately absent and is<br/>rejected: decision D16 lets visitors in with a reCAPTCHA bot check and a session cookie<br/>from fhir-proxy instead (Identity Platform's config cannot be destroyed by Terraform).<br/>Firebase Hosting (D4) and reCAPTCHA / Fraud Defense (D16) are included. | `set(string)` | <pre>[<br/>  "artifactregistry.googleapis.com",<br/>  "cloudbuild.googleapis.com",<br/>  "cloudresourcemanager.googleapis.com",<br/>  "cloudscheduler.googleapis.com",<br/>  "compute.googleapis.com",<br/>  "firebase.googleapis.com",<br/>  "firebasehosting.googleapis.com",<br/>  "health.googleapis.com",<br/>  "healthcare.googleapis.com",<br/>  "iam.googleapis.com",<br/>  "iamcredentials.googleapis.com",<br/>  "logging.googleapis.com",<br/>  "monitoring.googleapis.com",<br/>  "pubsub.googleapis.com",<br/>  "recaptchaenterprise.googleapis.com",<br/>  "run.googleapis.com",<br/>  "secretmanager.googleapis.com",<br/>  "serviceusage.googleapis.com",<br/>  "storage.googleapis.com"<br/>]</pre> | no |
| <a name="input_grant_build_account_registry_access"></a> [grant\_build\_account\_registry\_access](#input\_grant\_build\_account\_registry\_access) | Grant build\_service\_account\_email writer on the services repository and reader on the Docker Hub mirror (when it exists). A plain bool, like grant\_edge\_vm\_mirror\_reader, so the grants' count is known at plan time while the account's email is not; a precondition requires the email. Default false. | `bool` | `false` | no |
| <a name="input_grant_edge_vm_mirror_reader"></a> [grant\_edge\_vm\_mirror\_reader](#input\_grant\_edge\_vm\_mirror\_reader) | Grant edge\_vm\_service\_account\_email roles/artifactregistry.reader on the Docker Hub mirror (when create\_dockerhub\_mirror is true). Default true: the edge VM cannot pull MariaDB and OpenEMR without it, and then edge\_vm\_service\_account\_email must be set (a precondition checks it). Set false only when no edge VM is deployed. A plain bool, so the grant's count is known at plan time even while the account's email is not (it is unknown until the account exists). | `bool` | `true` | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the resources that support them (Artifact Registry repositories). | `map(string)` | `{}` | no |
| <a name="input_mllp_port"></a> [mllp\_port](#input\_mllp\_port) | TCP port the MLLP adapter on the edge VM listens on. 2575 is the port registered with IANA for HL7 over MLLP. | `number` | `2575` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for the names of every resource this module creates, so two deployments in one project do not collide. | `string` | `"pulsebridge"` | no |
| <a name="input_openemr_https_port"></a> [openemr\_https\_port](#input\_openemr\_https\_port) | TCP port OpenEMR serves HTTPS on at the edge VM; fhir-proxy calls OpenEMR's FHIR API there for the Share action. | `number` | `443` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the existing GCP project to configure (created or adopted by infra/bootstrap). | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | Region for the subnets, Cloud Router/NAT and Artifact Registry repositories. Use the same region as the edge VM and the Cloud Run services; us-central1 is one of the e2-micro free-tier regions. | `string` | `"us-central1"` | no |
| <a name="input_serverless_subnet_cidr"></a> [serverless\_subnet\_cidr](#input\_serverless\_subnet\_cidr) | Primary IPv4 range of the subnet that Cloud Run services join through Direct VPC egress (cuff simulator, fhir-proxy). Each Cloud Run instance holds several addresses from it, and Google requires /26 or larger. Must not overlap edge\_subnet\_cidr. | `string` | `"10.10.1.0/24"` | no |
| <a name="input_services_cleanup_dry_run"></a> [services\_cleanup\_dry\_run](#input\_services\_cleanup\_dry\_run) | true: Artifact Registry only logs what the cleanup policy would delete (useful the first week, to check the policy); false (default): it deletes. | `bool` | `false` | no |
| <a name="input_services_delete_older_than_days"></a> [services\_delete\_older\_than\_days](#input\_services\_delete\_older\_than\_days) | Cleanup policy of the services repository: versions older than this many days are deleted, unless the KEEP policy keeps them. | `number` | `90` | no |
| <a name="input_services_keep_recent_versions"></a> [services\_keep\_recent\_versions](#input\_services\_keep\_recent\_versions) | Cleanup policy of the services repository: the newest N versions of each image are always kept (a KEEP policy, which wins over DELETE). Keep it above the number of releases you might roll back to: Cloud Run pulls an image again when it starts a new instance, so an image a revision still uses must not be deleted. | `number` | `10` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_build_account_registry_grants"></a> [build\_account\_registry\_grants](#output\_build\_account\_registry\_grants) | The build account's repository grants (T26), as "<repository>=<role>": empty without grant\_build\_account\_registry\_access. |
| <a name="output_build_trigger_approval_required"></a> [build\_trigger\_approval\_required](#output\_build\_trigger\_approval\_required) | Whether the image build trigger's builds wait for an approval (T26), or null without a trigger. |
| <a name="output_build_trigger_name"></a> [build\_trigger\_name](#output\_build\_trigger\_name) | Name of the Cloud Build trigger that builds the images on a release tag, or null when build\_trigger\_repository is not set (T26). |
| <a name="output_cloud_nat_enabled"></a> [cloud\_nat\_enabled](#output\_cloud\_nat\_enabled) | Whether Cloud NAT exists. When false, the edge VM has no internet access unless it gets an external IP. |
| <a name="output_docker_repository_url"></a> [docker\_repository\_url](#output\_docker\_repository\_url) | Registry path for service images: <region>-docker.pkg.dev/<project>/<repository>. |
| <a name="output_dockerhub_mirror_url"></a> [dockerhub\_mirror\_url](#output\_dockerhub\_mirror\_url) | Registry path of the Docker Hub pull-through cache, or null when it is not created. Official images live under `library/`, e.g. <url>/library/mariadb@sha256:... |
| <a name="output_edge_subnet_cidr"></a> [edge\_subnet\_cidr](#output\_edge\_subnet\_cidr) | IPv4 range of the edge subnet. |
| <a name="output_edge_subnet_id"></a> [edge\_subnet\_id](#output\_edge\_subnet\_id) | ID of the edge subnet, for the edge VM's network interface (T21). |
| <a name="output_edge_subnet_name"></a> [edge\_subnet\_name](#output\_edge\_subnet\_name) | Name of the edge subnet. |
| <a name="output_edge_vm_network_tag"></a> [edge\_vm\_network\_tag](#output\_edge\_vm\_network\_tag) | Network tag the edge VM must carry for the firewall rules to apply (when edge\_vm\_service\_account\_email is null). |
| <a name="output_enabled_apis"></a> [enabled\_apis](#output\_enabled\_apis) | Service names of the APIs this module enabled. |
| <a name="output_firewall_rule_names"></a> [firewall\_rule\_names](#output\_firewall\_rule\_names) | Names of the firewall rules this module created (allow rules and the explicit deny-all). |
| <a name="output_firewall_target"></a> [firewall\_target](#output\_firewall\_target) | What the allow rules target: {type = "network\_tag" or "service\_account", value = tag or email}. |
| <a name="output_mllp_port"></a> [mllp\_port](#output\_mllp\_port) | TCP port opened from the serverless subnet to the edge VM for MLLP. |
| <a name="output_network_id"></a> [network\_id](#output\_network\_id) | ID of the VPC network (projects/<project>/global/networks/<name>). |
| <a name="output_network_name"></a> [network\_name](#output\_network\_name) | Name of the VPC network. Cloud Run's Direct VPC egress (`vpc_access.network_interfaces.network`) takes this. |
| <a name="output_network_self_link"></a> [network\_self\_link](#output\_network\_self\_link) | Self link of the VPC network, for resources that require a URL. |
| <a name="output_openemr_https_port"></a> [openemr\_https\_port](#output\_openemr\_https\_port) | TCP port opened from the serverless subnet to the edge VM for OpenEMR HTTPS. |
| <a name="output_serverless_subnet_cidr"></a> [serverless\_subnet\_cidr](#output\_serverless\_subnet\_cidr) | IPv4 range of the serverless subnet: the only source allowed to reach the edge VM's MLLP and HTTPS ports. |
| <a name="output_serverless_subnet_id"></a> [serverless\_subnet\_id](#output\_serverless\_subnet\_id) | ID of the subnet Cloud Run services join with Direct VPC egress (T22: cuff simulator and fhir-proxy). |
| <a name="output_serverless_subnet_name"></a> [serverless\_subnet\_name](#output\_serverless\_subnet\_name) | Name of the serverless subnet. Cloud Run's `vpc_access.network_interfaces.subnetwork` takes this; set `vpc_access.egress = "PRIVATE_RANGES_ONLY"`, except for fhir-proxy, which uses "ALL\_TRAFFIC": its calls to internal-only Cloud Run services (health-sync, cuff-sim) count as internal only when routed through the VPC (the subnet has Private Google Access; no NAT is needed). |
<!-- END_TF_DOCS -->
