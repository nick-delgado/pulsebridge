# Root variables of an environment. THIS FILE IS IDENTICAL IN infra/envs/dev AND infra/envs/prod
# (a test fails if they differ). What differs by environment lives in env.tf and backend.tf only.
#
# Set the values in terraform.tfvars (git-ignored; start from terraform.tfvars.example). Nothing
# author-specific has a default here: the project, the patient and the alert address always come
# from the person deploying.
#
# The rules a stranger meets first (the SYN- patient rules, the synthetic name, an alert
# destination, the zone inside the region) are validated here; the modules check their own
# inputs again as a backstop.

# ---------------------------------------------------------------------------------------------
# Project and credentials
# ---------------------------------------------------------------------------------------------

variable "project_id" {
  description = "ID of the GCP project this environment deploys into: bootstrap's `project_id` output. dev and prod are separate projects (decision D1), each bootstrapped once."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "deployer_service_account_email" {
  description = "Optional: the deployer service account to impersonate (bootstrap's `deployer_service_account_email` output). Set it when a PERSON runs Terraform with their own login (`gcloud auth application-default login`) and holds roles/iam.serviceAccountTokenCreator on the deployer: every call is then made as the deployer, with exactly its roles. Leave null in CI, where Workload Identity Federation already yields deployer credentials."
  type        = string
  default     = null

  validation {
    condition     = var.deployer_service_account_email == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$", coalesce(var.deployer_service_account_email, "x")))
    error_message = "deployer_service_account_email must be a service account email such as terraform-deployer@<project>.iam.gserviceaccount.com, or null."
  }
}

# ---------------------------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------------------------

variable "region" {
  description = "Region of everything regional (subnets, Artifact Registry, the Healthcare dataset, Cloud Run, Scheduler). Two constraints: (1) the e2-micro free tier exists only in us-west1, us-central1 and us-east1 (elsewhere the edge VM is billed by the hour; a check warns); (2) Firebase Hosting can rewrite /api to Cloud Run only in some regions (module phr validates against Firebase's list). us-central1 satisfies both and is Firebase's recommendation."
  type        = string
  default     = "us-central1"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+$", var.region))
    error_message = "region must be a region name such as \"us-central1\", not a zone."
  }
}

variable "zone" {
  description = "Zone of the edge VM and its data disk. Must be inside region (the edge subnet is regional, the VM zonal)."
  type        = string
  default     = "us-central1-a"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+)+[0-9]+-[a-z]$", var.zone)) && startswith(var.zone, "${var.region}-")
    error_message = "zone must be a zone of region, such as \"us-central1-a\" for region \"us-central1\"."
  }
}

variable "name_prefix" {
  description = "Prefix of every resource name in project-base, healthcare, ingestion, edge-vm and phr, one value for all five (for example pulsebridge-v2-to-fhir, pulsebridge-edge-vm). 3-20 characters: the strictest module (ingestion, phr) allows 20, healthcare needs 3."
  type        = string
  default     = "pulsebridge"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-20 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "identity_name_prefix" {
  description = "Prefix of the service account and Secret Manager secret IDs (module identity), at most 12 characters because a service account ID is at most 30 (\"pb\" gives pb-health-sync, pb-reviewer-password). It is separate from name_prefix only because of that length limit. The connect script's PULSEBRIDGE_NAME_PREFIX must equal it (output secret_name_prefix)."
  type        = string
  default     = "pb"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,10}[a-z0-9])?$", var.identity_name_prefix))
    error_message = "identity_name_prefix must be 1-12 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "labels" {
  description = "Extra labels for every resource that takes labels. app = \"pulsebridge\" and environment = dev|prod are always added."
  type        = map(string)
  default     = {}
  nullable    = false

  validation {
    condition = alltrue([
      for k, v in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", k)) && can(regex("^[a-z0-9_-]{0,63}$", v))
    ])
    error_message = "Label keys must start with a lowercase letter; keys and values may contain only lowercase letters, digits, hyphens and underscores (max 63 characters)."
  }
}

# ---------------------------------------------------------------------------------------------
# Ingest switch (use_synthetic_data is in env.tf: its default differs by environment)
# ---------------------------------------------------------------------------------------------

variable "ingest_enabled" {
  description = "false (default) on the first apply: no Cloud Scheduler job exists and the HL7v2 ingest subscription does not push, so nothing writes to the FHIR store by itself. Set true for a SECOND apply only after the setup step has written the full Patient (decision D11): the connect script (real data) or the first --days 30 seeding (synthetic). Otherwise two ingest services could each create a Patient at first contact. infra/envs/README.md, steps 9 and 10."
  type        = bool
  default     = false
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Images, by digest. Paste the image build's "BEGIN tfvars" block (services/README.md) unchanged.
# They are optional here only because the first, targeted applies (README steps 3 and 4) create
# the registry they are pushed to; a full plan without them stops with a clear error.
# ---------------------------------------------------------------------------------------------

variable "v2_to_fhir_image" {
  description = "v2-to-fhir image by digest, as the build prints it: <region>-docker.pkg.dev/<project>/<repository>/v2-to-fhir@sha256:<64 hex>. Required for a full apply."
  type        = string
  default     = null

  validation {
    condition     = var.v2_to_fhir_image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$", coalesce(var.v2_to_fhir_image, "x")))
    error_message = "v2_to_fhir_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "fhir_proxy_image" {
  description = "fhir-proxy image by digest (same form). Required for a full apply."
  type        = string
  default     = null

  validation {
    condition     = var.fhir_proxy_image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$", coalesce(var.fhir_proxy_image, "x")))
    error_message = "fhir_proxy_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "cuff_sim_image" {
  description = "cuff-sim image by digest (same form). Required for a full apply."
  type        = string
  default     = null

  validation {
    condition     = var.cuff_sim_image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$", coalesce(var.cuff_sim_image, "x")))
    error_message = "cuff_sim_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "health_sync_image" {
  description = "health-sync image by digest (same form). Required with use_synthetic_data = false; ignored with synthetic data (the build prints it anyway, so the pasted block can stay whole)."
  type        = string
  default     = null

  validation {
    condition     = var.health_sync_image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$", coalesce(var.health_sync_image, "x")))
    error_message = "health_sync_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "seed_image" {
  description = "Seeder image by digest (same form). Required with use_synthetic_data = true; ignored with real data."
  type        = string
  default     = null

  validation {
    condition     = var.seed_image == null || can(regex("^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$", coalesce(var.seed_image, "x")))
    error_message = "seed_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

# ---------------------------------------------------------------------------------------------
# The patient: ONE variable each, fed to every module and script that needs it
# ---------------------------------------------------------------------------------------------

variable "patient_id" {
  description = "The one patient identifier (value in the urn:pulsebridge:patient system). Every service gets it (v2-to-fhir, health-sync or the seeder, fhir-proxy, cuff-sim) and the setup_env output hands it to the connect script, so there is one source. With use_synthetic_data it must start with \"SYN-\" (for example SYN-0001); without it, it must NOT, so a real Google Health account can never be connected to a synthetic patient. An identifier, not a secret, but never commit a real one: it lives in terraform.tfvars (git-ignored) and in the state."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.patient_id))
    error_message = "patient_id must be 1-64 characters of letters, digits, '.', '_' and '-'."
  }
  validation {
    condition     = !var.use_synthetic_data || (startswith(var.patient_id, "SYN-") && length(var.patient_id) > 4)
    error_message = "use_synthetic_data = true needs a synthetic patient: patient_id must start with \"SYN-\" (for example \"SYN-0001\")."
  }
  validation {
    condition     = var.use_synthetic_data || !startswith(var.patient_id, "SYN-")
    error_message = "A \"SYN-\" patient_id is reserved for synthetic data and is refused with use_synthetic_data = false. Never run the Google Health connect script against a SYN- patient."
  }
}

variable "patient_assigning_authority" {
  description = "The assigning authority of the patient identifier: PID-3.4 of every message the cuff simulator builds, and the value v2-to-fhir requires in PID-3.4.1. One variable feeds both, so they cannot disagree. 1-20 characters (the cuff simulator's limit; v2-to-fhir allows 64). Not author-specific: any short code, for example PULSEBRIDGE."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,20}$", var.patient_assigning_authority))
    error_message = "patient_assigning_authority must be 1-20 characters of letters, digits, '.', '_' and '-'."
  }
}

variable "patient_family_name" {
  description = "Real data only: the patient's family name, PID-5.1 of the cuff simulator's messages. Required with use_synthetic_data = false; REFUSED with synthetic data, where the cuff simulator always sends the seeder's synthetic name (Synthetic / Demo) so the seeder accepts a Patient the cuff path created first. Personal data: it lives in terraform.tfvars (git-ignored), Cloud Run's configuration and the state."
  type        = string
  default     = null

  validation {
    condition     = var.use_synthetic_data ? var.patient_family_name == null : var.patient_family_name != null
    error_message = "patient_family_name is required with use_synthetic_data = false and must be left unset with use_synthetic_data = true (the synthetic name Synthetic / Demo is used)."
  }
  validation {
    condition     = var.patient_family_name == null || can(regex("^[\\x20-\\x5b\\x5d-\\x7e]{1,80}$", coalesce(var.patient_family_name, "x")))
    error_message = "patient_family_name must be 1-80 printable ASCII characters without a backslash (HL7's escape character)."
  }
}

variable "patient_given_name" {
  description = "Real data only: the patient's given name, PID-5.2. Same rules as patient_family_name."
  type        = string
  default     = null

  validation {
    condition     = var.use_synthetic_data ? var.patient_given_name == null : var.patient_given_name != null
    error_message = "patient_given_name is required with use_synthetic_data = false and must be left unset with use_synthetic_data = true (the synthetic name Synthetic / Demo is used)."
  }
  validation {
    condition     = var.patient_given_name == null || can(regex("^[\\x20-\\x5b\\x5d-\\x7e]{1,80}$", coalesce(var.patient_given_name, "x")))
    error_message = "patient_given_name must be 1-80 printable ASCII characters without a backslash (HL7's escape character)."
  }
}

variable "time_zone" {
  description = "Your IANA time zone, such as \"America/New_York\" or \"Europe/Amsterdam\": health-sync and the seeder cut days and five-minute windows in it. A named zone, not a fixed offset (\"UTC-5\" ignores daylight saving time). Only the shape is checked; an unknown name makes health-sync's deploy fail at its startup probe."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$", var.time_zone)) && !can(regex("(?i)^(Etc/)?(GMT|UTC)?[+-][0-9]{1,2}(:?[0-9]{2})?$", var.time_zone))
    error_message = "time_zone must be a named IANA zone such as \"America/New_York\", not a fixed offset."
  }
}

# ---------------------------------------------------------------------------------------------
# Alerts: at least one destination, so a failed sync and "reconnect needed" reach someone
# ---------------------------------------------------------------------------------------------

variable "alert_email" {
  description = "Email address the ingestion alerts go to (failed sync, reconnect needed, failed seeding, failed Scheduler attempt). Creates a Cloud Monitoring email channel. Set this or alert_notification_channels; with neither the plan stops, because the alerts would then be metrics nobody sees. Stored in the state; never commit it (terraform.tfvars is git-ignored)."
  type        = string
  default     = null

  validation {
    condition     = var.alert_email == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", coalesce(var.alert_email, "x")))
    error_message = "alert_email must be an email address."
  }
  validation {
    condition     = var.alert_email != null || length(var.alert_notification_channels) > 0
    error_message = "Set alert_email or alert_notification_channels: without a destination nobody is told when ingestion breaks or the Google Health connection needs renewing."
  }
}

variable "alert_notification_channels" {
  description = "Existing Cloud Monitoring notification channels (projects/<project>/notificationChannels/<id>) the alerts also go to."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for c in var.alert_notification_channels : can(regex("^projects/[^/]+/notificationChannels/[^/]+$", c))])
    error_message = "Each notification channel must be a full name: projects/<project>/notificationChannels/<id>."
  }
}

# ---------------------------------------------------------------------------------------------
# Reviewer password from tfvars: DEV ONLY (prod refuses it; use scripts/secrets instead)
# ---------------------------------------------------------------------------------------------

variable "reviewer_password" {
  description = "Dev only: the shared reviewer password, written as a version of the reviewer-password secret through a write-only argument, so it is never in the plan or the state. Used only when reviewer_password_wo_version > 0. At least 16 characters. Set it EITHER this way OR with scripts/secrets/set-reviewer-password, never both. Prefer passing it as TF_VAR_reviewer_password from a password manager over writing it into terraform.tfvars."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.reviewer_password == null ? true : length(var.reviewer_password) >= 16
    error_message = "reviewer_password must be at least 16 characters."
  }
}

variable "reviewer_password_wo_version" {
  description = "Dev only: 0 (default) writes no reviewer-password version from tfvars. 1 or more writes reviewer_password; raise the number to send a new password (Terraform cannot see a write-only value change). In prod a value above 0 is refused."
  type        = number
  default     = 0
  nullable    = false

  validation {
    condition     = var.reviewer_password_wo_version >= 0 && floor(var.reviewer_password_wo_version) == var.reviewer_password_wo_version
    error_message = "reviewer_password_wo_version must be a whole number, 0 or more."
  }
  validation {
    # local.allow_reviewer_password_from_tfvars comes from env.tf: true in dev, false in prod.
    condition     = var.reviewer_password_wo_version == 0 || local.allow_reviewer_password_from_tfvars
    error_message = "This environment does not take the reviewer password from tfvars (prod): leave reviewer_password and reviewer_password_wo_version unset and use scripts/secrets/set-reviewer-password."
  }
  validation {
    condition     = var.reviewer_password_wo_version == 0 || var.reviewer_password != null
    error_message = "reviewer_password must be set when reviewer_password_wo_version is greater than 0."
  }
}

# ---------------------------------------------------------------------------------------------
# Network and the edge VM
# ---------------------------------------------------------------------------------------------

variable "mllp_port" {
  description = "TCP port of the MLLP adapter on the edge VM (2575 is IANA's port for HL7 over MLLP). One value for the firewall rule (project-base), the published port (edge-vm) and the cuff simulator's MLLP_PORT (phr)."
  type        = number
  default     = 2575
  nullable    = false

  validation {
    condition     = var.mllp_port >= 1 && var.mllp_port <= 65535 && floor(var.mllp_port) == var.mllp_port && var.mllp_port != var.openemr_https_port
    error_message = "mllp_port must be a whole number from 1 to 65535 and differ from openemr_https_port."
  }
}

variable "openemr_https_port" {
  description = "TCP port OpenEMR's HTTPS is published on at the edge VM (firewall rule, OpenEMR's site address, fhir-proxy's calls)."
  type        = number
  default     = 443
  nullable    = false

  validation {
    condition     = var.openemr_https_port >= 1 && var.openemr_https_port <= 65535 && floor(var.openemr_https_port) == var.openemr_https_port
    error_message = "openemr_https_port must be a whole number from 1 to 65535."
  }
}

variable "enable_iap_ssh" {
  description = "Allow SSH to the edge VM from Identity-Aware Proxy's range only (gcloud compute ssh --tunnel-through-iap). Off by default; turn it on for the first-boot checklist (which needs a shell on the VM) and off again afterwards. Costs nothing."
  type        = bool
  default     = false
  nullable    = false
}

variable "enable_cloud_nat" {
  description = "Cloud Router and Cloud NAT for the edge VM. BILLED BY THE HOUR (about $4.70 a month plus $0.045 per GiB). Only the fallback if the first boot shows the VM cannot pull its images through Private Google Access (edge-vm README, \"Network and images\")."
  type        = bool
  default     = false
  nullable    = false
}

variable "openemr_scopes" {
  description = "OAuth2 scopes of the OpenEMR client fhir-proxy uses. null (default): spike S1's six SMART v1 scopes, the only set verified end to end. The narrower SMART v2 set to try on the first boot: [\"system/Patient.crs\", \"system/Encounter.crs\", \"system/Observation.cr\"] (openemr README, \"Scopes\"). A change registers a new client on the VM's next boot."
  type        = list(string)
  default     = null
}

# ---------------------------------------------------------------------------------------------
# The front door (module phr)
# ---------------------------------------------------------------------------------------------

variable "trusted_proxy_hops" {
  description = "fhir-proxy's TRUSTED_PROXY_HOPS: how many X-Forwarded-For entries, from the right, Google's proxies added. 1 until measured on the first deploy (phr README, \"Verify on the first real deploy\", step 1; infra/envs/README.md, step 12)."
  type        = number
  default     = 1
  nullable    = false

  validation {
    condition     = var.trusted_proxy_hops >= 0 && var.trusted_proxy_hops <= 5 && floor(var.trusted_proxy_hops) == var.trusted_proxy_hops
    error_message = "trusted_proxy_hops must be a whole number from 0 to 5."
  }
}

variable "public_origin" {
  description = "The origin reviewers open, https://<host> with no path. null (default): the Hosting site's https://<site>.web.app. A custom domain (T30) sets it."
  type        = string
  default     = null
}

variable "hosting_site_id" {
  description = "Firebase Hosting site ID. null (default): the project's default site (the project ID), which `terraform destroy` leaves in place (README, \"What destroy leaves behind\")."
  type        = string
  default     = null
}

variable "extra_allowed_origins" {
  description = "Further origins allowed to call fhir-proxy cross-origin, for example http://localhost:5173 for a local page against a deployed proxy. Empty by default: the page itself is same-origin."
  type        = list(string)
  default     = []
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Ingress fallbacks (module ingestion). Change only if the first deploy shows a refusal.
# ---------------------------------------------------------------------------------------------

variable "v2_to_fhir_ingress" {
  description = "v2-to-fhir's ingress. INGRESS_TRAFFIC_INTERNAL_ONLY (default). INGRESS_TRAFFIC_ALL only if the first deploy shows Pub/Sub pushes refused at the network level (IAM still admits only the pubsub-push account)."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  nullable    = false

  validation {
    condition     = contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_ALL"], var.v2_to_fhir_ingress)
    error_message = "v2_to_fhir_ingress must be INGRESS_TRAFFIC_INTERNAL_ONLY or INGRESS_TRAFFIC_ALL."
  }
}

variable "health_sync_ingress" {
  description = "health-sync's ingress. INGRESS_TRAFFIC_INTERNAL_ONLY (default). INGRESS_TRAFFIC_ALL only if the status strip shows health-sync unreachable from fhir-proxy (IAM still admits only scheduler-invoker and fhir-proxy)."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  nullable    = false

  validation {
    condition     = contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_ALL"], var.health_sync_ingress)
    error_message = "health_sync_ingress must be INGRESS_TRAFFIC_INTERNAL_ONLY or INGRESS_TRAFFIC_ALL."
  }
}
