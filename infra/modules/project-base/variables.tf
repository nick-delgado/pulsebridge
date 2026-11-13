# Inputs for the project-base module. Every value that identifies a deployment (project, region,
# names) comes in here, so nothing author-specific is hardcoded in the module.

variable "project_id" {
  description = "ID of the existing GCP project to configure (created or adopted by infra/bootstrap)."
  type        = string

  validation {
    # GCP project ID rules: 6 to 30 characters, lower-case letters, digits and hyphens, starting
    # with a letter and not ending with a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "region" {
  description = "Region for the subnets, Cloud Router/NAT and Artifact Registry repositories. Use the same region as the edge VM and the Cloud Run services; us-central1 is one of the e2-micro free-tier regions."
  type        = string
  default     = "us-central1"

  validation {
    # Region names look like `us-central1` or `europe-west4`: two or more hyphen-separated words,
    # the last ending in a number. A zone such as `us-central1-a` is rejected.
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+$", var.region))
    error_message = "region must be a region name such as \"us-central1\", not a zone such as \"us-central1-a\"."
  }
}

variable "name_prefix" {
  description = "Prefix for the names of every resource this module creates, so two deployments in one project do not collide."
  type        = string
  default     = "pulsebridge"

  validation {
    # The longest derived name is "<prefix>-allow-serverless-to-edge": the suffix is 25
    # characters. Compute Engine names are limited to 63 characters, so the prefix may use 38.
    condition     = can(regex("^[a-z][a-z0-9-]{0,36}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-38 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "enabled_apis" {
  description = <<-EOT
    Google APIs (service names) to enable in the project. The default covers every component in
    the brief. Identity Platform (identitytoolkit.googleapis.com) is deliberately absent and is
    rejected: decision D9 replaced it with a shared password checked by fhir-proxy. Add
    firebasehosting.googleapis.com here if decision D4 picks Firebase Hosting.
  EOT
  type        = set(string)
  default = [
    "artifactregistry.googleapis.com",     # Docker repositories for the service images and the Docker Hub mirror
    "cloudbuild.googleapis.com",           # builds the service images on tag (T24)
    "cloudresourcemanager.googleapis.com", # project-level IAM bindings made by Terraform (identity module)
    "cloudscheduler.googleapis.com",       # fires health-sync every 15 minutes
    "compute.googleapis.com",              # VPC, subnets, firewall, Cloud NAT and the edge VM
    "health.googleapis.com",               # Google Health API that health-sync reads (the deployer's OAuth app lives in this project)
    "healthcare.googleapis.com",           # Cloud Healthcare API: FHIR R4 store and HL7v2 store
    "iam.googleapis.com",                  # service accounts
    "iamcredentials.googleapis.com",       # short-lived tokens: OIDC for Scheduler/Pub/Sub push, Workload Identity Federation
    "logging.googleapis.com",              # Cloud Run and VM logs
    "pubsub.googleapis.com",               # HL7v2 ingest notifications to v2-to-fhir
    "run.googleapis.com",                  # Cloud Run services
    "secretmanager.googleapis.com",        # OAuth client, refresh token, reviewer password, DB and OpenEMR keys
    "serviceusage.googleapis.com",         # lets Terraform list and enable services
    "storage.googleapis.com",              # Cloud Build sources and the dashboard bucket (if D4 picks a bucket)
  ]

  validation {
    condition     = alltrue([for api in var.enabled_apis : can(regex("^[a-z0-9-]+\\.googleapis\\.com$", api))])
    error_message = "Each entry in enabled_apis must be a service name such as \"run.googleapis.com\"."
  }

  validation {
    condition     = !contains(var.enabled_apis, "identitytoolkit.googleapis.com")
    error_message = "identitytoolkit.googleapis.com (Identity Platform) must not be enabled: decision D9 uses a shared password checked by fhir-proxy instead."
  }
}

variable "edge_subnet_cidr" {
  description = "Primary IPv4 range of the edge subnet, where the e2-micro VM (OpenEMR, MariaDB, MLLP adapter) gets its internal address. Must not overlap serverless_subnet_cidr."
  type        = string
  default     = "10.10.0.0/24"

  validation {
    # cidrhost() fails on anything that is not a valid CIDR block; GCP subnets must be /29 or larger.
    condition     = can(cidrhost(var.edge_subnet_cidr, 0)) && can(regex("^[0-9.]+/([89]|1[0-9]|2[0-9])$", var.edge_subnet_cidr))
    error_message = "edge_subnet_cidr must be a valid IPv4 CIDR block between /8 and /29, such as \"10.10.0.0/24\"."
  }

  validation {
    # GCP rejects a subnet range with host bits set (10.10.0.5/24). cidrsubnet(x, 0, 0) returns
    # the block with host bits cleared, so it equals x only when x is already a network address.
    # try() makes a malformed value pass here; the rule above reports it.
    condition     = try(cidrsubnet(var.edge_subnet_cidr, 0, 0) == var.edge_subnet_cidr, true)
    error_message = "edge_subnet_cidr must be a network address with no host bits set, such as \"10.10.0.0/24\" (not \"10.10.0.5/24\")."
  }
}

variable "serverless_subnet_cidr" {
  description = "Primary IPv4 range of the subnet that Cloud Run services join through Direct VPC egress (cuff simulator, fhir-proxy). Each Cloud Run instance holds several addresses from it, and Google requires /26 or larger. Must not overlap edge_subnet_cidr."
  type        = string
  default     = "10.10.1.0/24"

  validation {
    # Direct VPC egress requires a subnet of at least /26 (64 addresses).
    condition     = can(cidrhost(var.serverless_subnet_cidr, 0)) && can(regex("^[0-9.]+/([89]|1[0-9]|2[0-6])$", var.serverless_subnet_cidr))
    error_message = "serverless_subnet_cidr must be a valid IPv4 CIDR block between /8 and /26 (Direct VPC egress needs at least 64 addresses), such as \"10.10.1.0/24\"."
  }

  validation {
    condition     = try(cidrsubnet(var.serverless_subnet_cidr, 0, 0) == var.serverless_subnet_cidr, true)
    error_message = "serverless_subnet_cidr must be a network address with no host bits set, such as \"10.10.1.0/24\" (not \"10.10.1.5/24\")."
  }

  validation {
    # Two CIDR blocks overlap exactly when they share the same network address once both are cut
    # to the shorter of their two prefix lengths. If either value is malformed the expression
    # errors, try() turns that into true, and that variable's own format rule reports it instead
    # of a crash such as "Invalid index".
    condition = try(
      cidrhost(format("%s/%d", split("/", var.serverless_subnet_cidr)[0], min(tonumber(split("/", var.serverless_subnet_cidr)[1]), tonumber(split("/", var.edge_subnet_cidr)[1]))), 0)
      !=
      cidrhost(format("%s/%d", split("/", var.edge_subnet_cidr)[0], min(tonumber(split("/", var.serverless_subnet_cidr)[1]), tonumber(split("/", var.edge_subnet_cidr)[1]))), 0),
      true
    )
    error_message = "serverless_subnet_cidr must not overlap edge_subnet_cidr."
  }
}

variable "mllp_port" {
  description = "TCP port the MLLP adapter on the edge VM listens on. 2575 is the port registered with IANA for HL7 over MLLP."
  type        = number
  default     = 2575

  validation {
    condition     = var.mllp_port >= 1 && var.mllp_port <= 65535 && floor(var.mllp_port) == var.mllp_port
    error_message = "mllp_port must be a whole number between 1 and 65535."
  }
}

variable "openemr_https_port" {
  description = "TCP port OpenEMR serves HTTPS on at the edge VM; fhir-proxy calls OpenEMR's FHIR API there for the Share action."
  type        = number
  default     = 443

  validation {
    condition     = var.openemr_https_port >= 1 && var.openemr_https_port <= 65535 && floor(var.openemr_https_port) == var.openemr_https_port
    error_message = "openemr_https_port must be a whole number between 1 and 65535."
  }

  validation {
    # The MLLP adapter and OpenEMR both publish on the VM's interface; one port cannot serve both.
    condition     = var.openemr_https_port != var.mllp_port
    error_message = "openemr_https_port must differ from mllp_port."
  }
}

variable "edge_vm_service_account_email" {
  description = "Optional email of the edge VM's service account (created by the identity module). When set, firewall rules target that service account instead of the edge network tag, which is stronger: anyone allowed to edit an instance can add a tag, but attaching a service account needs IAM permission on it. Leave null to target the network tag."
  type        = string
  default     = null

  validation {
    condition     = var.edge_vm_service_account_email == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\\.iam\\.gserviceaccount\\.com$", var.edge_vm_service_account_email))
    error_message = "edge_vm_service_account_email must be a service account email such as \"edge-vm@my-project.iam.gserviceaccount.com\", or null."
  }
}

variable "enable_iap_ssh" {
  description = "Allow SSH (TCP 22) to the edge VM from Identity-Aware Proxy's TCP forwarding range only (35.235.240.0/20), for `gcloud compute ssh --tunnel-through-iap`. Off by default: nothing else in the design needs SSH. When true, the module also enables iap.googleapis.com, which IAP TCP forwarding is understood to require (not confirmed against Google's documentation)."
  type        = bool
  default     = false
}

variable "enable_cloud_nat" {
  description = "Create a Cloud Router and Cloud NAT gateway so the edge VM can reach the public internet (apt, Docker Hub) without an external IP. Off by default because it is billed by the hour: about $0.0014 per VM per hour using the gateway, plus $0.005 per hour per NAT IP address, plus $0.045 per GiB processed (about $4.70 a month for one VM and one IP before data: 730 h x ($0.0014 + $0.005))."
  type        = bool
  default     = false
}

variable "docker_repository_id" {
  description = "ID of the Artifact Registry Docker repository that holds the service images built by Cloud Build."
  type        = string
  default     = "pulsebridge-services"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.docker_repository_id))
    error_message = "docker_repository_id must be 2-63 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "create_dockerhub_mirror" {
  description = "Create an Artifact Registry remote repository that proxies and caches Docker Hub, so the edge VM can pull the OpenEMR and MariaDB images through Private Google Access without Cloud NAT or an external IP. No hourly cost; cached images are billed as Artifact Registry storage."
  type        = bool
  default     = true
}

variable "dockerhub_mirror_repository_id" {
  description = "ID of the Docker Hub remote (mirror) repository, used when create_dockerhub_mirror is true."
  type        = string
  default     = "pulsebridge-dockerhub"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.dockerhub_mirror_repository_id))
    error_message = "dockerhub_mirror_repository_id must be 2-63 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "labels" {
  description = "Labels added to the resources that support them (Artifact Registry repositories)."
  type        = map(string)
  default     = {}
}
