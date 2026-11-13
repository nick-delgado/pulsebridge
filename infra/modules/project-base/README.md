# Module `project-base`

The ground every other PulseBridge module stands on. It:

- enables the project's Google APIs (a variable with a default covering the stack);
- creates a custom VPC with two subnets: `edge` for the e2-micro VM and `serverless` for Cloud Run
  Direct VPC egress;
- sets a deny-by-default firewall with one allow rule the design needs (Cloud Run to the VM on the
  MLLP and OpenEMR HTTPS ports), plus an optional IAP-only SSH rule;
- optionally creates Cloud Router and Cloud NAT (off by default, billed hourly);
- creates an Artifact Registry Docker repository for the service images and, by default, a Docker
  Hub pull-through cache for the VM.

It does not create service accounts or IAM bindings (module `identity`), and contains no provider
or backend configuration: the env root configures those.

## Usage

```hcl
module "project_base" {
  source = "../../modules/project-base"

  project_id = var.project_id
  region     = var.region

  # Optional: target firewall rules at the edge VM's service account (from module identity)
  # instead of the network tag.
  # edge_vm_service_account_email = module.identity.edge_vm_service_account_email
}
```

Consumers:

- `edge-vm` (T21): put the VM's interface on `edge_subnet_id`, give it no external IP, tag it with
  `edge_vm_network_tag` (or run it as the service account passed in), publish MLLP on `mllp_port`
  and OpenEMR on `openemr_https_port` on the VM's interface, and pull images from
  `dockerhub_mirror_url` (OpenEMR, MariaDB) and Artifact Registry. T21 does not need its own MLLP
  firewall rule; this module owns it.
- `phr` (T22): for the cuff simulator and `fhir-proxy`, set Direct VPC egress with
  `network_name`, `serverless_subnet_name` and `egress = "PRIVATE_RANGES_ONLY"`.
- `ingestion` (T20): needs nothing from the network; `health-sync` and `v2-to-fhir` call only
  Google APIs.

## How Cloud Run reaches the edge VM

```
cuff simulator / fhir-proxy (Cloud Run)
   | Direct VPC egress, PRIVATE_RANGES_ONLY: only 10.x traffic enters the VPC
   v
serverless subnet 10.10.1.0/24 ---- firewall: allow tcp 2575, 443 from 10.10.1.0/24 ---->
edge subnet 10.10.0.0/24 -> e2-micro (no external IP)
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
- **Consequence for T21:** the VM cannot `apt-get install docker.io` as the spikes did. Use an image
  with Docker preinstalled (Container-Optimized OS), or turn on `enable_cloud_nat` (about
  $4.70/month plus data), or give the VM an external IP (billed per hour; T21's choice).
- The VM's service account needs `roles/artifactregistry.reader` on the repositories (module
  `identity`).

## Idle cost

| Resource | Idle cost |
| --- | --- |
| Enabled APIs, VPC, subnets, firewall rules, Cloud Router | $0 |
| Artifact Registry | Storage only: $0.10/GB-month beyond 0.5 GB free (cached OpenEMR image is the largest item); not confirmed against primary documentation |
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
| <a name="input_create_dockerhub_mirror"></a> [create\_dockerhub\_mirror](#input\_create\_dockerhub\_mirror) | Create an Artifact Registry remote repository that proxies and caches Docker Hub, so the edge VM can pull the OpenEMR and MariaDB images through Private Google Access without Cloud NAT or an external IP. No hourly cost; cached images are billed as Artifact Registry storage. | `bool` | `true` | no |
| <a name="input_docker_repository_id"></a> [docker\_repository\_id](#input\_docker\_repository\_id) | ID of the Artifact Registry Docker repository that holds the service images built by Cloud Build. | `string` | `"pulsebridge-services"` | no |
| <a name="input_dockerhub_mirror_repository_id"></a> [dockerhub\_mirror\_repository\_id](#input\_dockerhub\_mirror\_repository\_id) | ID of the Docker Hub remote (mirror) repository, used when create\_dockerhub\_mirror is true. | `string` | `"pulsebridge-dockerhub"` | no |
| <a name="input_edge_subnet_cidr"></a> [edge\_subnet\_cidr](#input\_edge\_subnet\_cidr) | Primary IPv4 range of the edge subnet, where the e2-micro VM (OpenEMR, MariaDB, MLLP adapter) gets its internal address. Must not overlap serverless\_subnet\_cidr. | `string` | `"10.10.0.0/24"` | no |
| <a name="input_edge_vm_service_account_email"></a> [edge\_vm\_service\_account\_email](#input\_edge\_vm\_service\_account\_email) | Optional email of the edge VM's service account (created by the identity module). When set, firewall rules target that service account instead of the edge network tag, which is stronger: anyone allowed to edit an instance can add a tag, but attaching a service account needs IAM permission on it. Leave null to target the network tag. | `string` | `null` | no |
| <a name="input_enable_cloud_nat"></a> [enable\_cloud\_nat](#input\_enable\_cloud\_nat) | Create a Cloud Router and Cloud NAT gateway so the edge VM can reach the public internet (apt, Docker Hub) without an external IP. Off by default because it is billed by the hour: about $0.0014 per VM per hour using the gateway, plus $0.005 per hour per NAT IP address, plus $0.045 per GiB processed (about $4.70 a month for one VM and one IP before data: 730 h x ($0.0014 + $0.005)). | `bool` | `false` | no |
| <a name="input_enable_iap_ssh"></a> [enable\_iap\_ssh](#input\_enable\_iap\_ssh) | Allow SSH (TCP 22) to the edge VM from Identity-Aware Proxy's TCP forwarding range only (35.235.240.0/20), for `gcloud compute ssh --tunnel-through-iap`. Off by default: nothing else in the design needs SSH. | `bool` | `false` | no |
| <a name="input_enabled_apis"></a> [enabled\_apis](#input\_enabled\_apis) | Google APIs (service names) to enable in the project. The default covers every component in<br/>the brief. Identity Platform (identitytoolkit.googleapis.com) is deliberately absent and is<br/>rejected: decision D9 replaced it with a shared password checked by fhir-proxy. Add<br/>firebasehosting.googleapis.com here if decision D4 picks Firebase Hosting. | `set(string)` | <pre>[<br/>  "artifactregistry.googleapis.com",<br/>  "cloudbuild.googleapis.com",<br/>  "cloudresourcemanager.googleapis.com",<br/>  "cloudscheduler.googleapis.com",<br/>  "compute.googleapis.com",<br/>  "health.googleapis.com",<br/>  "healthcare.googleapis.com",<br/>  "iam.googleapis.com",<br/>  "iamcredentials.googleapis.com",<br/>  "logging.googleapis.com",<br/>  "pubsub.googleapis.com",<br/>  "run.googleapis.com",<br/>  "secretmanager.googleapis.com",<br/>  "serviceusage.googleapis.com",<br/>  "storage.googleapis.com"<br/>]</pre> | no |
| <a name="input_labels"></a> [labels](#input\_labels) | Labels added to the resources that support them (Artifact Registry repositories). | `map(string)` | `{}` | no |
| <a name="input_mllp_port"></a> [mllp\_port](#input\_mllp\_port) | TCP port the MLLP adapter on the edge VM listens on. 2575 is the port registered with IANA for HL7 over MLLP. | `number` | `2575` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for the names of every resource this module creates, so two deployments in one project do not collide. | `string` | `"pulsebridge"` | no |
| <a name="input_openemr_https_port"></a> [openemr\_https\_port](#input\_openemr\_https\_port) | TCP port OpenEMR serves HTTPS on at the edge VM; fhir-proxy calls OpenEMR's FHIR API there for the Share action. | `number` | `443` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | ID of the existing GCP project to configure (created or adopted by infra/bootstrap). | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | Region for the subnets, Cloud Router/NAT and Artifact Registry repositories. Use the same region as the edge VM and the Cloud Run services; us-central1 is one of the e2-micro free-tier regions. | `string` | `"us-central1"` | no |
| <a name="input_serverless_subnet_cidr"></a> [serverless\_subnet\_cidr](#input\_serverless\_subnet\_cidr) | Primary IPv4 range of the subnet that Cloud Run services join through Direct VPC egress (cuff simulator, fhir-proxy). Each Cloud Run instance holds several addresses from it, and Google requires /26 or larger. Must not overlap edge\_subnet\_cidr. | `string` | `"10.10.1.0/24"` | no |

## Outputs

| Name | Description |
|------|-------------|
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
| <a name="output_serverless_subnet_name"></a> [serverless\_subnet\_name](#output\_serverless\_subnet\_name) | Name of the serverless subnet. Cloud Run's `vpc_access.network_interfaces.subnetwork` takes this; set `vpc_access.egress = "PRIVATE_RANGES_ONLY"`. |
<!-- END_TF_DOCS -->
