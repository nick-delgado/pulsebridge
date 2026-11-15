# project-base: what every other module stands on. It enables the project's APIs, builds a
# private network for the edge VM and the Cloud Run services that call it, sets the firewall
# posture, and creates the Artifact Registry repositories the images come from.
#
# Network design (see README.md, "How Cloud Run reaches the edge VM"):
#
#   Cloud Run (cuff simulator, fhir-proxy)
#     -- Direct VPC egress, private ranges only -->  serverless subnet  (10.10.1.0/24 by default)
#     -- firewall: allow TCP mllp_port + openemr_https_port from the serverless subnet only -->
#   edge subnet (10.10.0.0/24)  -->  e2-micro VM, no public MLLP or HTTPS port
#
# Nothing here runs or bills by the hour unless enable_cloud_nat is true.

locals {
  # The APIs actually enabled: the caller's list, plus the IAP API when IAP SSH is switched on,
  # so the flag works without the caller also having to edit the list.
  enabled_apis = setunion(var.enabled_apis, var.enable_iap_ssh ? ["iap.googleapis.com"] : [])

  network_name           = "${var.name_prefix}-vpc"
  edge_subnet_name       = "${var.name_prefix}-edge"
  serverless_subnet_name = "${var.name_prefix}-serverless"

  # Network tag the edge VM (T21) must carry. Firewall rules target it unless a service account
  # email is supplied, in which case they target the service account instead (a firewall rule can
  # use one kind of target, not both).
  edge_vm_network_tag = "${var.name_prefix}-edge-vm"

  target_tags             = var.edge_vm_service_account_email == null ? [local.edge_vm_network_tag] : null
  target_service_accounts = var.edge_vm_service_account_email == null ? null : [var.edge_vm_service_account_email]

  # Identity-Aware Proxy's TCP forwarding range. `gcloud compute ssh --tunnel-through-iap` connects
  # from these addresses, so SSH never needs to be open to the internet.
  iap_tcp_forwarding_range = "35.235.240.0/20"

  # Every ingress rule that ALLOWS traffic. Kept in one map so the rules are created with one
  # for_each and the tests can check all of them at once (no allow rule may have 0.0.0.0/0).
  allow_rules = merge(
    {
      # The only path into the edge VM that the design needs: Cloud Run services on the serverless
      # subnet send HL7v2 over MLLP (cuff simulator) and call OpenEMR's FHIR API over HTTPS
      # (fhir-proxy). Cloud Run's Direct VPC egress addresses cannot be matched by network tag or
      # service account in an ingress rule, so the source is the subnet range.
      "allow-serverless-to-edge" = {
        description   = "Cloud Run (Direct VPC egress on the serverless subnet) to the edge VM: MLLP and OpenEMR HTTPS."
        source_ranges = [var.serverless_subnet_cidr]
        ports         = [tostring(var.mllp_port), tostring(var.openemr_https_port)]
      }
    },
    var.enable_iap_ssh ? {
      "allow-iap-ssh-to-edge" = {
        description   = "SSH to the edge VM through Identity-Aware Proxy TCP forwarding only."
        source_ranges = [local.iap_tcp_forwarding_range]
        ports         = ["22"]
      }
    } : {}
  )
}

# ---------------------------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------------------------

# One google_project_service per API. for_each over a set gives each API its own resource address
# (google_project_service.this["run.googleapis.com"]), so adding or removing one API from the list
# touches only that API instead of re-creating the others, as count with list indices would.
resource "google_project_service" "this" {
  for_each = local.enabled_apis

  project = var.project_id
  service = each.value

  # Leave APIs enabled when this module is destroyed. Disabling an API on destroy can fail while
  # other resources still use it, and disabling compute or serviceusage would break the bootstrap
  # state bucket and deployer identity that outlive this module. An enabled API with nothing in
  # it costs nothing, so `terraform destroy` still leaves nothing billable behind.
  disable_on_destroy         = false
  disable_dependent_services = false
}

# ---------------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------------

# A custom-mode VPC. The project's `default` network is not used: it has a subnet in every region
# and stock firewall rules that allow SSH (22) and RDP (3389) from 0.0.0.0/0.
resource "google_compute_network" "this" {
  project                 = var.project_id
  name                    = local.network_name
  description             = "PulseBridge private network: edge VM and Cloud Run Direct VPC egress."
  auto_create_subnetworks = false # custom mode: only the subnets declared below exist
  routing_mode            = "REGIONAL"

  # Keep the default route to the internet gateway. Private Google Access and Cloud NAT both send
  # traffic along it; without an external IP or NAT, the VM still cannot reach the internet.
  delete_default_routes_on_create = false

  depends_on = [google_project_service.this]
}

# Subnet for the e2-micro VM. Private Google Access lets a VM with no external IP reach Google APIs
# (the Healthcare API for the MLLP adapter, Artifact Registry for images, Secret Manager, Logging).
# It does not reach anything outside Google: no apt mirror, no Docker Hub.
resource "google_compute_subnetwork" "edge" {
  project                  = var.project_id
  name                     = local.edge_subnet_name
  description              = "Edge VM (OpenEMR, MariaDB, MLLP adapter)."
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.edge_subnet_cidr
  private_ip_google_access = true
  # VPC flow logs are left off: they bill per GB of log volume and the demo does not need them.
}

# Subnet that Cloud Run services join through Direct VPC egress. Kept separate from the edge
# subnet so its range identifies Cloud Run traffic exactly and can be the firewall source.
resource "google_compute_subnetwork" "serverless" {
  project                  = var.project_id
  name                     = local.serverless_subnet_name
  description              = "Cloud Run Direct VPC egress (cuff simulator, fhir-proxy)."
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.serverless_subnet_cidr
  private_ip_google_access = true
}

# ---------------------------------------------------------------------------------------------
# Firewall: deny by default
# ---------------------------------------------------------------------------------------------
#
# A VPC firewall rule matches packets by direction, source, target and protocol/port, and either
# allows or denies them. Lower priority numbers win. Every VPC also has two implied rules at the
# lowest priority (65535): deny all ingress, allow all egress.

resource "google_compute_firewall" "allow" {
  for_each = local.allow_rules

  project     = var.project_id
  name        = "${var.name_prefix}-${each.key}"
  description = each.value.description
  network     = google_compute_network.this.id
  direction   = "INGRESS"
  priority    = 1000

  source_ranges           = each.value.source_ranges
  target_tags             = local.target_tags
  target_service_accounts = local.target_service_accounts

  allow {
    protocol = "tcp"
    ports    = each.value.ports
  }
}

# An explicit deny-all for ingress, one step above the implied rule. It denies the same traffic
# the implied rule does, but it is visible in the console and in `terraform plan`, documents the
# posture in code, and can have logging turned on while debugging (logging is off: it bills per GB).
resource "google_compute_firewall" "deny_all_ingress" {
  project     = var.project_id
  name        = "${var.name_prefix}-deny-all-ingress"
  description = "Deny every inbound connection not allowed by a higher-priority rule."
  network     = google_compute_network.this.id
  direction   = "INGRESS"
  priority    = 65534

  source_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }
}

# ---------------------------------------------------------------------------------------------
# Cloud NAT (optional, billed hourly)
# ---------------------------------------------------------------------------------------------

# Cloud NAT gives VMs without external IPs outbound access to the internet. It is a managed
# configuration on a Cloud Router (the router itself is free). count = 0 or 1 makes both
# resources exist only when the flag is on.
resource "google_compute_router" "nat" {
  count = var.enable_cloud_nat ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-router"
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  count = var.enable_cloud_nat ? 1 : 0

  project = var.project_id
  name    = "${var.name_prefix}-nat"
  region  = var.region
  router  = google_compute_router.nat[0].name

  nat_ip_allocate_option = "AUTO_ONLY"

  # Only the edge subnet is translated. Cloud Run uses Direct VPC egress for private ranges only,
  # so its internet traffic never reaches this gateway.
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.edge.self_link
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# ---------------------------------------------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------------------------------------------

# Docker repository for the images Cloud Build builds (T24). Cloud Run references them by digest.
resource "google_artifact_registry_repository" "services" {
  project       = var.project_id
  location      = var.region
  repository_id = var.docker_repository_id
  description   = "PulseBridge service images (health-sync, v2-to-fhir, fhir-proxy, cuff simulator)."
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"
  labels        = var.labels

  docker_config {
    # A tag cannot be moved to a different image once pushed, so a tag always names one build.
    immutable_tags = true
  }

  # Cleanup policy (T26). Immutable tags mean nothing ever replaces an image, so without a policy
  # every release stays forever (about 100 MB each, services/README.md "Cost"). Artifact Registry
  # applies the policies to each package (image) separately, and a KEEP policy wins over a
  # DELETE policy: a version is deleted only when it is older than the limit AND not among the
  # newest services_keep_recent_versions of its image. The newest ones must stay because Cloud Run
  # pulls the image by digest again whenever it starts an instance.
  cleanup_policy_dry_run = var.services_cleanup_dry_run

  cleanup_policies {
    id     = "delete-older-than-limit"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "${var.services_delete_older_than_days * 86400}s"
    }
  }

  cleanup_policies {
    id     = "keep-most-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.services_keep_recent_versions
    }
  }

  depends_on = [google_project_service.this]
}

# Remote repository: a pull-through cache of Docker Hub. The VM pulls
# <region>-docker.pkg.dev/<project>/<repo>/library/mariadb@sha256:... from Artifact Registry over
# Private Google Access; Artifact Registry fetches the image from Docker Hub on Google's side.
# This is what lets the edge VM run without Cloud NAT or an external IP.
resource "google_artifact_registry_repository" "dockerhub" {
  count = var.create_dockerhub_mirror ? 1 : 0

  project       = var.project_id
  location      = var.region
  repository_id = var.dockerhub_mirror_repository_id
  description   = "Pull-through cache of Docker Hub for the edge VM (OpenEMR, MariaDB)."
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  labels        = var.labels

  remote_repository_config {
    description = "Docker Hub"
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
  }

  depends_on = [google_project_service.this]
}

# Read access to the Docker Hub mirror for the edge VM's service account (T21), so the VM can pull
# MariaDB and OpenEMR through it. The grant lives here because this module owns the repository
# (a binding lives in the module that owns the resource). It is on this one repository only, and
# it exists when the mirror exists and grant_edge_vm_mirror_reader is true. (The build account's
# two grants, at the end of this file, follow the same pattern.) Additive `_iam_member`, so other readers are left alone.
#
# The count is decided by two plain bools, never by "is the email set": a service account's
# email is unknown until the account is created, so `email != null` would be unknown on a root's
# first plan and Terraform would refuse it ("Invalid count argument"). The precondition checks
# the email instead; an unknown email is checked at apply.
resource "google_artifact_registry_repository_iam_member" "dockerhub_mirror_edge_vm_reader" {
  count = var.create_dockerhub_mirror && var.grant_edge_vm_mirror_reader ? 1 : 0

  project    = var.project_id
  location   = google_artifact_registry_repository.dockerhub[0].location
  repository = google_artifact_registry_repository.dockerhub[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.edge_vm_service_account_email}"

  lifecycle {
    precondition {
      condition     = var.edge_vm_service_account_email != null
      error_message = "grant_edge_vm_mirror_reader is true but edge_vm_service_account_email is null: pass the edge VM's account (module identity), or set grant_edge_vm_mirror_reader = false when no edge VM is deployed."
    }
  }
}

# ---------------------------------------------------------------------------------------------
# Image builds (T26)
# ---------------------------------------------------------------------------------------------

# The build account (module identity's "build") may push to the services repository and pull the
# base image through the Docker Hub mirror: those two repositories, nothing else (services/README.md,
# "Who runs builds"). Like the edge VM's grant above, the count is a plain bool and a precondition
# checks the email, which is unknown on a root's first plan.
resource "google_artifact_registry_repository_iam_member" "services_build_writer" {
  count = var.grant_build_account_registry_access ? 1 : 0

  project    = var.project_id
  location   = google_artifact_registry_repository.services.location
  repository = google_artifact_registry_repository.services.repository_id
  # writer: push, read tags (the build's plan phase) and add a tag to an existing manifest. It
  # cannot delete images or change the repository; the cleanup policy deletes.
  role   = "roles/artifactregistry.writer"
  member = "serviceAccount:${var.build_service_account_email}"

  lifecycle {
    precondition {
      condition     = var.build_service_account_email != null
      error_message = "grant_build_account_registry_access is true but build_service_account_email is null: pass module identity's build account, or set the bool to false."
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "dockerhub_mirror_build_reader" {
  count = var.create_dockerhub_mirror && var.grant_build_account_registry_access ? 1 : 0

  project    = var.project_id
  location   = google_artifact_registry_repository.dockerhub[0].location
  repository = google_artifact_registry_repository.dockerhub[0].repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${var.build_service_account_email}"

  lifecycle {
    precondition {
      condition     = var.build_service_account_email != null
      error_message = "grant_build_account_registry_access is true but build_service_account_email is null."
    }
  }
}

# A Cloud Build trigger: on every git tag matching build_trigger_tag_pattern, Cloud Build fetches
# that commit from GitHub through the 2nd-gen repository connection and runs cloudbuild.yaml AS
# THE BUILD ACCOUNT (service_account), not as Cloud Build's default account. Opt-in: the
# connection is made once by hand (it needs the Cloud Build GitHub App), so with
# build_trigger_repository = null nothing is created.
resource "google_cloudbuild_trigger" "images_on_tag" {
  count = var.build_trigger_repository == null ? 0 : 1

  project     = var.project_id
  location    = var.region # a 2nd-gen trigger lives in its repository's region
  name        = "${var.name_prefix}-images-on-tag"
  description = "Builds and pushes every services/*/Dockerfile image on a release tag (cloudbuild.yaml). Created by project-base (T26)."

  repository_event_config {
    repository = var.build_trigger_repository
    push {
      tag = var.build_trigger_tag_pattern
    }
  }

  filename = "cloudbuild.yaml"

  # The build account, as projects/<project>/serviceAccounts/<email>. With a user-specified
  # account the build log must go to Cloud Logging only, which cloudbuild.yaml sets.
  service_account = "projects/${var.project_id}/serviceAccounts/${var.build_service_account_email}"

  # cloudbuild.yaml's own defaults are this module's defaults; passing this module's actual values
  # keeps the build right when a root changes the region or a repository ID. An empty mirror means
  # "pull the base image from Docker Hub directly" (cloudbuild.yaml, _DOCKERHUB_MIRROR).
  substitutions = {
    _REGION           = var.region
    _REPOSITORY       = google_artifact_registry_repository.services.repository_id
    _DOCKERHUB_MIRROR = var.create_dockerhub_mirror ? google_artifact_registry_repository.dockerhub[0].repository_id : ""
  }

  # The build must be able to push and pull from its first run.
  depends_on = [
    google_artifact_registry_repository_iam_member.services_build_writer,
    google_artifact_registry_repository_iam_member.dockerhub_mirror_build_reader,
    google_project_service.this,
  ]

  lifecycle {
    precondition {
      condition     = var.build_service_account_email != null && var.grant_build_account_registry_access
      error_message = "build_trigger_repository is set, but the build account or its registry grants are not: set build_service_account_email and grant_build_account_registry_access = true (module identity's create_build_account)."
    }
  }
}
