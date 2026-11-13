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
  for_each = var.enabled_apis

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
