# Inputs for the edge-vm module. Every value that identifies a deployment (project, zone, network,
# service account, store, secrets) comes in here, so nothing author-specific is hardcoded. No input
# carries a password or key: the one database password the VM needs is read from Secret Manager
# by the VM itself at boot (see `secret_files`).

variable "project_id" {
  description = "ID of the GCP project the VM, its disk and its internal address are created in."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "name_prefix" {
  description = "Prefix for the names of every resource this module creates (<prefix>-edge-vm, <prefix>-edge-data, <prefix>-edge-internal). Use the same prefix as project-base."
  type        = string
  default     = "pulsebridge"

  validation {
    # The longest derived name is "<prefix>-edge-internal" (14 characters of suffix); Compute
    # Engine names are limited to 63 characters.
    condition     = can(regex("^[a-z][a-z0-9-]{0,47}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-49 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "zone" {
  description = "Zone for the VM and its data disk, inside the region of the edge subnet. The e2-micro free tier applies only in us-west1, us-central1 and us-east1 (one VM per billing account)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+-[a-z]$", var.zone))
    error_message = "zone must be a zone name such as \"us-central1-a\", not a region."
  }
}

variable "edge_subnet_id" {
  description = "ID of the edge subnet from project-base (output `edge_subnet_id`, projects/<project>/regions/<region>/subnetworks/<name>). The subnet must have Private Google Access on: the VM has no external IP and pulls its images from Artifact Registry and gcr.io through it."
  type        = string

  validation {
    condition     = can(regex("^projects/[^/]+/regions/[^/]+/subnetworks/[^/]+$", var.edge_subnet_id))
    error_message = "edge_subnet_id must be a subnet ID of the form projects/<project>/regions/<region>/subnetworks/<name>."
  }
}

variable "internal_ip" {
  description = "Optional fixed internal IPv4 address for the VM inside the edge subnet. Null lets Google pick a free one, which is then reserved so it survives VM replacement (the cuff simulator's MLLP_HOST and OpenEMR's site address stay valid)."
  type        = string
  default     = null

  validation {
    condition     = var.internal_ip == null || can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}$", var.internal_ip))
    error_message = "internal_ip must be an IPv4 address such as \"10.10.0.10\", or null."
  }
}

variable "network_tag" {
  description = "Network tag the project-base firewall rules target (project-base output `edge_vm_network_tag`). Always attached; harmless when project-base targets the service account instead."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.network_tag))
    error_message = "network_tag must be 2-63 characters of lower-case letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "service_account_email" {
  description = "Email of the VM's dedicated service account (identity output `service_account_emails[\"edge-vm\"]`). Never the Compute Engine default account: in older projects that account holds project-wide Editor."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\\.iam\\.gserviceaccount\\.com$", var.service_account_email))
    error_message = "service_account_email must be a user-managed service account email (<name>@<project>.iam.gserviceaccount.com). The Compute Engine default account (<number>-compute@developer.gserviceaccount.com) is refused."
  }
}

variable "machine_type" {
  description = "Machine type of the VM. e2-micro (2 shared vCPUs, 1 GB) is the free-tier size that spike S2 measured as enough for one reviewer at a time; e2-small eases the CPU-burst limit but is outside the free tier and billed by the hour."
  type        = string
  default     = "e2-micro"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*-[a-z0-9-]+$", var.machine_type))
    error_message = "machine_type must be a Compute Engine machine type name such as \"e2-micro\"."
  }
}

variable "boot_image" {
  description = "Boot image: a Container-Optimized OS image or image family from the cos-cloud project. The startup script relies on COS (Docker and docker-credential-gcr preinstalled, read-only root, stateless /etc), so other images are refused."
  type        = string
  default     = "projects/cos-cloud/global/images/family/cos-stable"

  validation {
    condition     = can(regex("^projects/cos-cloud/global/images/(family/)?cos-[a-z0-9-]+$", var.boot_image))
    error_message = "boot_image must be a Container-Optimized OS image or family in projects/cos-cloud, such as \"projects/cos-cloud/global/images/family/cos-stable\"."
  }
}

variable "boot_disk_size_gb" {
  description = "Boot disk size in GB. It holds the COS system partitions, Docker's image store (OpenEMR is about 3.6 GB unpacked, spike S2) and the swap file. 20 GB boot plus 10 GB data stays inside the free tier's 30 GB-months of standard persistent disk."
  type        = number
  default     = 20

  validation {
    condition     = var.boot_disk_size_gb >= 15 && floor(var.boot_disk_size_gb) == var.boot_disk_size_gb
    error_message = "boot_disk_size_gb must be a whole number of at least 15 (images plus a 2 GB swap file)."
  }
}

variable "data_disk_size_gb" {
  description = "Size in GB of the separate persistent disk for MariaDB and OpenEMR site data. A fresh OpenEMR database is a few hundred MB. Growing it later is done in place (the startup script grows the filesystem on the next boot); shrinking is not possible."
  type        = number
  default     = 10

  validation {
    condition     = var.data_disk_size_gb >= 10 && floor(var.data_disk_size_gb) == var.data_disk_size_gb
    error_message = "data_disk_size_gb must be a whole number of at least 10."
  }
}

variable "disk_type" {
  description = "Persistent disk type for both disks. pd-standard is the only type the free tier covers; pd-balanced is faster (spike S2 suspected the slow disk for the 5-minute OpenEMR first boot) but billed."
  type        = string
  default     = "pd-standard"

  validation {
    condition     = contains(["pd-standard", "pd-balanced", "pd-ssd"], var.disk_type)
    error_message = "disk_type must be pd-standard, pd-balanced or pd-ssd."
  }
}

variable "swap_size_mb" {
  description = "Size of the swap file in MiB (spike S2: 2 GB, cheap insurance against a memory spike; the tuned stack also survived without it)."
  type        = number
  default     = 2048

  validation {
    condition     = var.swap_size_mb >= 0 && var.swap_size_mb <= 8192 && floor(var.swap_size_mb) == var.swap_size_mb
    error_message = "swap_size_mb must be a whole number from 0 (no swap) to 8192."
  }
}

variable "dockerhub_mirror_url" {
  description = "Registry path of the Docker Hub pull-through cache from project-base (output `dockerhub_mirror_url`, <location>-docker.pkg.dev/<project>/<repository>). MariaDB (and OpenEMR, T23) are pulled from here. Read access for the VM's account is granted by project-base, which owns the repository (set its `edge_vm_service_account_email`)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+-docker\\.pkg\\.dev/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.dockerhub_mirror_url))
    error_message = "dockerhub_mirror_url must look like <location>-docker.pkg.dev/<project>/<repository>."
  }
}

variable "mariadb_image" {
  description = "MariaDB image path inside the Docker Hub mirror, pinned by digest (an optional tag before the digest is kept for readability only). Default: mariadb 11.8 as tested in spikes S1 and S2; the digest was confirmed current on Docker Hub on 2026-11-15."
  type        = string
  default     = "library/mariadb:11.8@sha256:d36f18d65a0459510b8b0f643b226a6d299e3206486e2b64ff20754a6d291707"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$", var.mariadb_image))
    error_message = "mariadb_image must be a path inside the mirror pinned by digest, such as \"library/mariadb:11.8@sha256:<64 hex>\"; a tag alone is refused because tags move."
  }
}

variable "mllp_adapter_image" {
  description = "Google's MLLP adapter image, pinned by digest. The image has no version tags, only `latest` (spike S4), so a digest is the only pin. To check for a newer build without pulling: `crane digest gcr.io/cloud-healthcare-containers/mllp-adapter:latest`, or a HEAD request for /v2/cloud-healthcare-containers/mllp-adapter/manifests/latest on gcr.io and read Docker-Content-Digest. Default: the digest spike S4 tested, still `latest` on 2026-11-15."
  type        = string
  default     = "gcr.io/cloud-healthcare-containers/mllp-adapter@sha256:92eaa0fc5fcd0eb7ff62bd28c9140dcd7582bfec20790e5b203ca8dc75c4a90f"

  validation {
    condition     = can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.mllp_adapter_image))
    error_message = "mllp_adapter_image must be pinned by digest (<registry>/<path>@sha256:<64 hex>); a tag such as :latest is refused."
  }
}

variable "hl7v2_store_flags" {
  description = "The four store coordinates the MLLP adapter takes as flags (healthcare output `mllp_adapter_store_flags`). Each is a short name, not a resource path: --hl7_v2_store_id=<store name>, as spike S4 recorded."
  type = object({
    hl7_v2_project_id  = string
    hl7_v2_location_id = string
    hl7_v2_dataset_id  = string
    hl7_v2_store_id    = string
  })

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.hl7v2_store_flags.hl7_v2_project_id))
    error_message = "hl7v2_store_flags.hl7_v2_project_id must be a project ID."
  }

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)*[0-9]*$", var.hl7v2_store_flags.hl7_v2_location_id))
    error_message = "hl7v2_store_flags.hl7_v2_location_id must be a location such as \"us-central1\"."
  }

  validation {
    # Healthcare dataset and store IDs: 1-256 of letters, digits, underscores, hyphens and periods.
    # A slash means a resource path was passed where the adapter wants the short name.
    condition = alltrue([
      for v in [var.hl7v2_store_flags.hl7_v2_dataset_id, var.hl7v2_store_flags.hl7_v2_store_id] :
      can(regex("^[A-Za-z0-9_.-]{1,256}$", v))
    ])
    error_message = "hl7v2_store_flags.hl7_v2_dataset_id and hl7_v2_store_id must be short names (letters, digits, _ . -), not paths such as projects/.../hl7V2Stores/<name>."
  }
}

variable "mllp_adapter_log_error_messages" {
  description = "Pass --log_error_msg=true to the MLLP adapter, so it logs the store's error text for each NACK. Off by default: upstream's flag help says \"These error logs will contain sensitive data\", and the logs reach Cloud Logging. Spike S4 ran with it on. Off loses only the NACK reason in the adapter's own log; the cuff simulator still receives the NACK and records its MSA-3 reason in the AuditEvent."
  type        = bool
  default     = false
}

variable "mllp_port" {
  description = "Port the MLLP adapter is published on at the VM's address. Must equal project-base's `mllp_port`, which is the port its firewall rule opens from the serverless subnet."
  type        = number
  default     = 2575

  validation {
    condition     = var.mllp_port >= 1 && var.mllp_port <= 65535 && floor(var.mllp_port) == var.mllp_port
    error_message = "mllp_port must be a whole number between 1 and 65535."
  }
}

variable "secret_files" {
  description = <<-EOT
    Secret Manager secrets the VM copies to files on its data disk at boot, as
    { <file name> = "projects/<project>/secrets/<secret id>" } (identity output `secret_resource_ids`).
    The VM reads them with its own account; the values never pass through Terraform, metadata or
    logs. `mariadb-root-password` is required (identity secret `db-root-password`). T23 adds the
    OpenEMR passwords here. A file is fetched only when it does not exist yet, so it stays equal to
    what the database was initialised with.
  EOT
  type        = map(string)

  validation {
    condition     = contains(keys(var.secret_files), "mariadb-root-password")
    error_message = "secret_files must contain \"mariadb-root-password\"."
  }

  validation {
    condition     = alltrue([for k in keys(var.secret_files) : can(regex("^[a-z0-9][a-z0-9-]{0,62}$", k))])
    error_message = "secret_files keys are file names: lower-case letters, digits and hyphens."
  }

  validation {
    condition     = alltrue([for v in values(var.secret_files) : can(regex("^projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]{1,255}$", v))])
    error_message = "secret_files values must be secret resource IDs: projects/<project>/secrets/<secret id> (no version: the VM reads `latest`)."
  }
}

variable "hooks" {
  description = <<-EOT
    Extra boot steps, as { <name> = <bash script> }. Each is stored in instance metadata as
    `pulsebridge-hook-<name>` and run by the startup script on every boot, in name order, after
    MariaDB is healthy. This is the seam for T23 (OpenEMR container, client registration). A hook
    must be idempotent and must contain no secret (metadata is readable by anyone who can view the
    instance); it reads secrets from the files listed in `secret_files`.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.hooks) : can(regex("^[a-z0-9][a-z0-9-]{0,39}$", k))])
    error_message = "hooks keys must be 1-40 lower-case letters, digits and hyphens."
  }
}

variable "enable_cloud_logging" {
  description = "Turn on Container-Optimized OS's logging agent (metadata google-logging-enabled), so startup-script and container logs reach Cloud Logging. The VM's account has roles/logging.logWriter from the identity module. The agent uses some of the 1 GB of RAM; off still leaves the serial console."
  type        = bool
  default     = true
}

variable "boot_wait_minutes" {
  description = "One shared deadline, counted from the start of the startup script, for everything it waits on that can be slow on first boot: image pulls, secret versions not yet added, MariaDB becoming healthy. When it passes, the script logs an error and gives up for this boot. Every network call also has its own timeout, so the deadline is overshot by at most about a minute. Hooks have their own limit (hook_timeout_minutes)."
  type        = number
  default     = 15

  validation {
    condition     = var.boot_wait_minutes >= 1 && var.boot_wait_minutes <= 60 && floor(var.boot_wait_minutes) == var.boot_wait_minutes
    error_message = "boot_wait_minutes must be a whole number from 1 to 60."
  }
}

variable "hook_timeout_minutes" {
  description = "Longest one hook (T23) may run on one boot before it is stopped and logged as failed. OpenEMR's first boot takes about 15 minutes (spike S2), so the default leaves room. Hooks run one after another, so the worst case for all hooks is this times their number."
  type        = number
  default     = 30

  validation {
    condition     = var.hook_timeout_minutes >= 1 && var.hook_timeout_minutes <= 120 && floor(var.hook_timeout_minutes) == var.hook_timeout_minutes
    error_message = "hook_timeout_minutes must be a whole number from 1 to 120."
  }
}

variable "labels" {
  description = "Labels added to the VM, its disks and its address."
  type        = map(string)
  default     = {}
}
