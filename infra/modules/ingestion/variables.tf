# ---------------------------------------------------------------------------------------------
# Where and what to call things
# ---------------------------------------------------------------------------------------------

variable "project_id" {
  description = "ID of the GCP project the Cloud Run services, the job, the Scheduler jobs and the log-based metrics are created in. Comes from the env root; never hardcoded."
  type        = string

  validation {
    # GCP project ID rules: 6 to 30 characters, lowercase letters, digits and hyphens,
    # starting with a letter and not ending with a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID: 6-30 characters, lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "region" {
  description = "Region for the Cloud Run services, the Cloud Run job and the Scheduler jobs, such as \"us-central1\". Use the region of the Healthcare dataset, so requests to the store stay in one region."
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "region must look like a GCP region, such as \"us-central1\"."
  }
}

variable "name_prefix" {
  description = "Prefix for every resource name in this module: \"<prefix>-v2-to-fhir\", \"<prefix>-health-sync\", \"<prefix>-seed\" and so on. Lets two stacks share one project without colliding."
  type        = string
  default     = "pulsebridge"

  validation {
    # Cloud Run service and job names: lowercase letters, digits and hyphens, starting with a
    # letter, at most 49 characters. The longest suffix here is "-health-sync-reconnect-needed"
    # (29 characters, a log metric name), so 20 leaves every name valid.
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-20 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "labels" {
  description = "Labels added to the Cloud Run services and the job (Scheduler jobs and log metrics take no labels)."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------------------------
# The two switches
# ---------------------------------------------------------------------------------------------

variable "use_synthetic_data" {
  description = <<-EOT
    Which wearable source this deployment has. false (default): the real Google Health
    connection, so health-sync is deployed with its Scheduler job and alerts, and the seeder
    is not. true: the synthetic-data seeder (T09) runs as a Cloud Run job instead, and nothing
    of health-sync is created. The two never exist together. The cuff path (v2-to-fhir) is
    deployed either way. patient_id must start with "SYN-" when this is true and must not
    when it is false.
  EOT
  type        = bool
  default     = false
  nullable    = false
}

variable "ingest_enabled" {
  description = <<-EOT
    Whether anything may start ingestion on a schedule. false (default): no Scheduler job
    exists, so neither health-sync nor the seeder ever runs by itself. Turn it on only after
    the setup step has written the Patient (D11): the connect script (T11) for a real
    deployment, or the first seeding (`--days 30`) for a synthetic one. Both run outside
    `terraform apply`; see the README, "First run". The cuff path's push subscription lives in
    module healthcare: feed this module's `ingest_enabled` output to its push_enabled so one
    switch controls both.
  EOT
  type        = bool
  default     = false
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Images (printed by the build, services/README.md "Container images")
# ---------------------------------------------------------------------------------------------

locals {
  # An Artifact Registry (or any OCI) image by digest only: registry host, path, then
  # "@sha256:" and 64 hex digits, with no ":tag". A digest names exact bytes; a tag can be moved
  # to other bytes, which would make an apply deploy something nobody tested.
  image_by_digest_regex = "^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
}

variable "v2_to_fhir_image" {
  description = "v2-to-fhir image by digest, as the build prints it: \"<region>-docker.pkg.dev/<project>/<repository>/v2-to-fhir@sha256:<64 hex>\". A tag (\":v1\") is refused, with or without a digest."
  type        = string

  validation {
    condition     = can(regex(local.image_by_digest_regex, var.v2_to_fhir_image))
    error_message = "v2_to_fhir_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "health_sync_image" {
  description = "health-sync image by digest (same form as v2_to_fhir_image). Required when use_synthetic_data is false; ignored, and may be null, when it is true."
  type        = string
  default     = null

  validation {
    condition     = var.health_sync_image == null || can(regex(local.image_by_digest_regex, coalesce(var.health_sync_image, "x")))
    error_message = "health_sync_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
  validation {
    condition     = var.use_synthetic_data || var.health_sync_image != null
    error_message = "health_sync_image is required when use_synthetic_data is false."
  }
}

variable "seed_image" {
  description = "Seeder image by digest (same form as v2_to_fhir_image). Required when use_synthetic_data is true; ignored, and may be null, when it is false."
  type        = string
  default     = null

  validation {
    condition     = var.seed_image == null || can(regex(local.image_by_digest_regex, coalesce(var.seed_image, "x")))
    error_message = "seed_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
  validation {
    condition     = !var.use_synthetic_data || var.seed_image != null
    error_message = "seed_image is required when use_synthetic_data is true."
  }
}

# ---------------------------------------------------------------------------------------------
# Healthcare coordinates (module healthcare's outputs) and the patient
# ---------------------------------------------------------------------------------------------

variable "healthcare_location" {
  description = "Location of the Healthcare dataset (module.healthcare.location). Becomes HEALTHCARE_LOCATION."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+[0-9]+)?$", var.healthcare_location))
    error_message = "healthcare_location must look like a GCP region (\"us-central1\") or multi-region (\"us\")."
  }
}

variable "healthcare_dataset_id" {
  description = "Short dataset ID (module.healthcare.dataset_name), not the full resource path. Becomes HEALTHCARE_DATASET_ID."
  type        = string

  validation {
    # Healthcare IDs: letters, digits, '_', '-' and '.'. A '/' means a full path was passed.
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.healthcare_dataset_id))
    error_message = "healthcare_dataset_id must be the short dataset ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
}

variable "fhir_store_id" {
  description = "Short ID of the main (health-data) FHIR store (module.healthcare.fhir_store_name), not the audit store. Becomes FHIR_STORE_ID."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.fhir_store_id))
    error_message = "fhir_store_id must be the short FHIR store ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
}

variable "hl7v2_store_id" {
  description = "Short ID of the HL7v2 store (module.healthcare.hl7v2_store_name). Becomes v2-to-fhir's HL7V2_STORE_ID."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.hl7v2_store_id))
    error_message = "hl7v2_store_id must be the short HL7v2 store ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
}

variable "healthcare_project_id" {
  description = "Project of the Healthcare dataset (module.healthcare.project_id). null (default) means project_id. Becomes GCP_PROJECT_ID."
  type        = string
  default     = null

  validation {
    condition     = var.healthcare_project_id == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", coalesce(var.healthcare_project_id, "x")))
    error_message = "healthcare_project_id must be a valid GCP project ID."
  }
}

variable "patient_id" {
  description = <<-EOT
    The one configured patient identifier (value in the urn:pulsebridge:patient system), the
    same one the setup step writes (D11) and every service uses. Becomes PATIENT_ID. Letters,
    digits, '.', '_' and '-', at most 64 characters (the services' own rule). With
    use_synthetic_data it must start with "SYN-" (the seeder refuses anything else); without
    it, it must not, so a real deployment can never be mistaken for a synthetic one (T25).
    It is an identifier, not a secret, but never commit a real one: it comes from tfvars.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.patient_id))
    error_message = "patient_id must be 1-64 characters of letters, digits, '.', '_' and '-'."
  }
  validation {
    # Same rule as services/seed/pulsebridge_seed/config.py: "SYN-" and at least one more
    # character.
    condition     = !var.use_synthetic_data || (startswith(var.patient_id, "SYN-") && length(var.patient_id) > 4)
    error_message = "With use_synthetic_data = true, patient_id must start with \"SYN-\" (for example \"SYN-0001\")."
  }
  validation {
    condition     = var.use_synthetic_data || !startswith(var.patient_id, "SYN-")
    error_message = "A \"SYN-\" patient_id is reserved for synthetic data: with use_synthetic_data = false it is refused. Never connect a real Google Health account to a synthetic patient."
  }
}

variable "patient_assigning_authority" {
  description = "Optional. If set, v2-to-fhir also requires PID-3.4.1 of the matching PID-3 to equal it (PATIENT_ASSIGNING_AUTHORITY). null (default) leaves the variable unset."
  type        = string
  default     = null

  validation {
    condition     = var.patient_assigning_authority == null || can(regex("^[A-Za-z0-9._-]{1,64}$", coalesce(var.patient_assigning_authority, "x")))
    error_message = "patient_assigning_authority must be 1-64 characters of letters, digits, '.', '_' and '-'."
  }
}

variable "time_zone" {
  description = "The user's IANA time zone, such as \"America/New_York\" or \"Europe/Amsterdam\" (TIME_ZONE for health-sync and the seeder). Fixed offsets (\"UTC-5\", \"Etc/GMT+5\", \"+02:00\") are refused, as the services refuse them: they ignore daylight saving time. \"UTC\" itself is allowed."
  type        = string

  validation {
    # Shape of a tz database name: one or more '/'-separated parts of letters, digits, '_', '+'
    # and '-', starting with a letter.
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$", var.time_zone))
    error_message = "time_zone must be an IANA time zone name such as \"America/New_York\"."
  }
  validation {
    # The services' _FIXED_OFFSET pattern (health_sync/config.py, pulsebridge_seed/config.py).
    condition     = !can(regex("(?i)^(Etc/)?(GMT|UTC)?[+-][0-9]{1,2}(:?[0-9]{2})?$", var.time_zone))
    error_message = "time_zone must be a named zone, not a fixed offset such as \"UTC-5\" or \"Etc/GMT+5\"."
  }
}

# ---------------------------------------------------------------------------------------------
# Identities and secrets (module identity's outputs, passed whole)
# ---------------------------------------------------------------------------------------------

variable "service_account_emails" {
  description = <<-EOT
    Module identity's map of workload name to service account email, passed as it is
    (module.identity.service_account_emails). Keys used:
      v2-to-fhir        runs v2-to-fhir                              (always)
      pubsub-push       the only invoker of v2-to-fhir               (always)
      health-sync       runs health-sync                             (real data)
      scheduler-invoker invokes health-sync /sync, or runs the seeder job (Scheduler's token)
      fhir-proxy        the only other invoker of health-sync (/status) (real data)
      seeder            runs the seeder job                          (synthetic data)
    Other keys are ignored. Bindings are chosen by key name only, so the plan knows which exist
    even while the emails are unknown.
  EOT
  type        = map(string)

  validation {
    condition = alltrue([
      for k, v in var.service_account_emails :
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\\.iam\\.gserviceaccount\\.com$", v))
      if contains(["v2-to-fhir", "pubsub-push", "health-sync", "scheduler-invoker", "fhir-proxy", "seeder"], k)
    ])
    error_message = "service_account_emails values for the keys this module uses must be service account emails (<name>@<project>.iam.gserviceaccount.com)."
  }
  validation {
    condition = alltrue([
      for k in concat(
        ["v2-to-fhir", "pubsub-push", "scheduler-invoker"],
        var.use_synthetic_data ? ["seeder"] : ["health-sync", "fhir-proxy"],
      ) : contains(keys(var.service_account_emails), k)
    ])
    error_message = "service_account_emails must contain v2-to-fhir, pubsub-push and scheduler-invoker, plus health-sync and fhir-proxy (use_synthetic_data = false) or seeder (use_synthetic_data = true; set identity's create_seeder_account to the same flag)."
  }
}

variable "secret_ids" {
  description = "Module identity's map of secret key to Secret Manager secret ID (module.identity.secret_ids), passed as it is. health-sync mounts google-health-client-id, google-health-client-secret and google-health-refresh-token; other keys are ignored. Not used with synthetic data."
  type        = map(string)
  default     = {}

  validation {
    condition = var.use_synthetic_data || alltrue([
      for k in ["google-health-client-id", "google-health-client-secret", "google-health-refresh-token"] :
      contains(keys(var.secret_ids), k)
    ])
    error_message = "secret_ids must contain google-health-client-id, google-health-client-secret and google-health-refresh-token when use_synthetic_data is false."
  }
  validation {
    # Secret Manager secret IDs: letters, digits, '_' and '-', at most 255.
    condition = alltrue([
      for k, v in var.secret_ids : can(regex("^[A-Za-z0-9_-]{1,255}$", v))
      if startswith(k, "google-health-")
    ])
    error_message = "The google-health-* secret_ids must be short secret IDs (letters, digits, '_', '-'), not projects/... paths."
  }
}

# ---------------------------------------------------------------------------------------------
# Service settings
# ---------------------------------------------------------------------------------------------

variable "v2_to_fhir_ingress" {
  description = "Ingress of v2-to-fhir. \"INGRESS_TRAFFIC_INTERNAL_ONLY\" (default): only internal sources reach it, and a Pub/Sub push from a subscription in the same project counts as internal (README, \"Ingress\"). \"INGRESS_TRAFFIC_ALL\" is the fallback if the first apply shows pushes refused at the network level; IAM still lets only pubsub-push invoke it."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  validation {
    condition     = contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_ALL"], var.v2_to_fhir_ingress)
    error_message = "v2_to_fhir_ingress must be \"INGRESS_TRAFFIC_INTERNAL_ONLY\" or \"INGRESS_TRAFFIC_ALL\"."
  }
}

variable "health_sync_ingress" {
  description = "Ingress of health-sync. \"INGRESS_TRAFFIC_INTERNAL_ONLY\" (default): Cloud Scheduler in the same project counts as internal; fhir-proxy's /status call counts only if fhir-proxy sends it through the VPC (Direct VPC egress with egress \"ALL_TRAFFIC\"; see the README, \"Ingress\", which T22 must follow). \"INGRESS_TRAFFIC_ALL\" if T22 keeps \"PRIVATE_RANGES_ONLY\"; IAM still lets only scheduler-invoker and fhir-proxy invoke it."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  validation {
    condition     = contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_ALL"], var.health_sync_ingress)
    error_message = "health_sync_ingress must be \"INGRESS_TRAFFIC_INTERNAL_ONLY\" or \"INGRESS_TRAFFIC_ALL\"."
  }
}

variable "v2_to_fhir_max_instances" {
  description = "Upper bound on v2-to-fhir instances. Each handles 4 concurrent pushes (gunicorn's 4 threads); one cuff reading is one push, so 2 is plenty and caps the bill if something floods the topic."
  type        = number
  default     = 2

  validation {
    condition     = var.v2_to_fhir_max_instances >= 1 && var.v2_to_fhir_max_instances <= 10 && floor(var.v2_to_fhir_max_instances) == var.v2_to_fhir_max_instances
    error_message = "v2_to_fhir_max_instances must be a whole number from 1 to 10."
  }
}

variable "v2_to_fhir_request_timeout" {
  description = "Cloud Run request timeout for v2-to-fhir (a duration such as \"60s\"). Match module healthcare's ack_deadline_seconds (60): Pub/Sub treats a push unanswered by then as failed and retries anyway."
  type        = string
  default     = "60s"

  validation {
    condition     = can(regex("^[0-9]+s$", var.v2_to_fhir_request_timeout)) && try(tonumber(trimsuffix(var.v2_to_fhir_request_timeout, "s")) >= 10 && tonumber(trimsuffix(var.v2_to_fhir_request_timeout, "s")) <= 600, false)
    error_message = "v2_to_fhir_request_timeout must be whole seconds with an 's' suffix, from \"10s\" to \"600s\" (Pub/Sub's longest ack deadline)."
  }
}

variable "health_sync_request_timeout" {
  description = "Cloud Run request timeout for health-sync. At least 180 s, the Scheduler attempt deadline, so Cloud Run never cuts a run that Scheduler is still waiting for; a run budgets itself to 150 s (RUN_DEADLINE_SECONDS)."
  type        = string
  default     = "300s"

  validation {
    condition     = can(regex("^[0-9]+s$", var.health_sync_request_timeout)) && try(tonumber(trimsuffix(var.health_sync_request_timeout, "s")) >= 180 && tonumber(trimsuffix(var.health_sync_request_timeout, "s")) <= 3600, false)
    error_message = "health_sync_request_timeout must be whole seconds with an 's' suffix, from \"180s\" to \"3600s\"."
  }
}

variable "seed_task_timeout" {
  description = "Time limit of one seeder job execution (a duration such as \"1800s\"). The hourly top-up (--hours 4) is one Bundle and takes seconds; the first --days 30 seeding is about 85 Bundles, roughly 15 minutes at an assumed 10 s per Bundle, so the default 1800 s leaves twice that. A longer first seeding sets its own limit with --task-timeout (README, \"First run\"). Only seconds used are billed."
  type        = string
  default     = "1800s"

  validation {
    # Cloud Run jobs allow up to 24 hours per task.
    condition     = can(regex("^[0-9]+s$", var.seed_task_timeout)) && try(tonumber(trimsuffix(var.seed_task_timeout, "s")) >= 60 && tonumber(trimsuffix(var.seed_task_timeout, "s")) <= 86400, false)
    error_message = "seed_task_timeout must be whole seconds with an 's' suffix, from \"60s\" to \"86400s\"."
  }
}

variable "deletion_protection" {
  description = "Refuse to destroy the Cloud Run services and the job (the provider's deletion_protection, which defaults to true). false (default) here, because they hold no data and dev is destroyed nightly; the data lives in module healthcare, which has its own deletion_policy."
  type        = bool
  default     = false
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------------------------

variable "notification_channels" {
  description = "Cloud Monitoring notification channels (\"projects/<project>/notificationChannels/<id>\", created by hand or in the env root) for the health-sync alerts. Empty (default): the log-based metrics are created and can be charted, but no alert policy exists, so nothing is sent and nothing is billed for alerting. Non-empty needs the Monitoring API (monitoring.googleapis.com) enabled, which module project-base does not enable by default."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for c in var.notification_channels : can(regex("^projects/[^/]+/notificationChannels/[^/]+$", c))])
    error_message = "Each notification channel must be a full name: projects/<project>/notificationChannels/<id>."
  }
}
