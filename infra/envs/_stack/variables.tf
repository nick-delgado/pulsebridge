# Inputs of the stack composition. The env roots pass their own variables through unchanged and
# add the per-environment settings (environment, protect_from_destroy,
# allow_reviewer_password_from_tfvars) from their env.tf.
#
# The user-facing rules (the SYN- patient rules, the synthetic name, an alert destination, the
# zone inside the region) are validated at the ROOT, where a stranger meets them first
# (infra/envs/<env>/variables.tf); the seven modules validate their own inputs again as a
# backstop. This file validates only what neither does: the per-environment switches.

# ---------------------------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------------------------

variable "environment" {
  description = "Which environment root calls the stack: \"dev\" or \"prod\". Used for labels and in the outputs; the settings that differ by environment come in as their own variables below."
  type        = string
  nullable    = false

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be \"dev\" or \"prod\"."
  }
}

variable "protect_from_destroy" {
  description = "true (prod): the Healthcare dataset and stores get deletion_policy PREVENT, the Secret Manager secrets deletion_protection, and the Cloud Run services and job deletion_protection, so a stray `terraform destroy` fails instead of deleting real data or taking the demo down. false (dev): everything can be destroyed, which the nightly destroy test needs."
  type        = bool
  nullable    = false
}

variable "allow_reviewer_password_from_tfvars" {
  description = "true (dev only): reviewer_password and reviewer_password_wo_version may write the reviewer-password secret version from tfvars (module identity, write-only, never in state). false (prod): the password is set only with scripts/secrets/set-reviewer-password (T27) and rotated there."
  type        = bool
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------------------------

variable "project_id" {
  description = "ID of the GCP project the whole stack is created in (bootstrap's project_id output)."
  type        = string
  nullable    = false
}

variable "region" {
  description = "Region of every regional resource: subnets, Artifact Registry, the Healthcare dataset, Cloud Run, Scheduler."
  type        = string
  nullable    = false
}

variable "zone" {
  description = "Zone of the edge VM and its data disk, inside var.region."
  type        = string
  nullable    = false
}

variable "name_prefix" {
  description = "Prefix of every resource name in project-base, healthcare, ingestion, edge-vm and phr (one value for all five, so the names line up)."
  type        = string
  nullable    = false
}

variable "identity_name_prefix" {
  description = "Prefix of the service account and secret IDs (module identity's name_prefix, at most 12 characters because a service account ID is at most 30). The connect script's PULSEBRIDGE_NAME_PREFIX must equal it."
  type        = string
  nullable    = false
}

variable "labels" {
  description = "Extra labels for the resources that take labels. The stack adds app = \"pulsebridge\" and environment = var.environment."
  type        = map(string)
  default     = {}
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# The two switches
# ---------------------------------------------------------------------------------------------

variable "use_synthetic_data" {
  description = "true: the synthetic-data seeder replaces health-sync (no Google Health account needed). false: real Google Health data through health-sync."
  type        = bool
  nullable    = false
}

variable "ingest_enabled" {
  description = "Whether the Scheduler job and the HL7v2 push subscription exist / are on. false until the setup step has written the Patient (D11)."
  type        = bool
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Images by digest
# ---------------------------------------------------------------------------------------------

# The images live in project-base's Artifact Registry repository, which the first (targeted)
# apply creates, so they cannot exist before it: the three images every deployment needs are
# optional here, and a FULL plan without them stops with the message below. A plan targeted at
# module.stack.module.project_base or module.stack.module.identity (runbook steps 3 and 4) does
# not evaluate these validations, because nothing it plans depends on the images.
variable "v2_to_fhir_image" {
  description = "v2-to-fhir image by digest (module ingestion validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.v2_to_fhir_image != null
    error_message = "v2_to_fhir_image is not set. Build the images first (infra/envs/README.md, step 6) and paste the build's tfvars block; only the targeted applies of steps 3 and 4 run without images."
  }
}

variable "fhir_proxy_image" {
  description = "fhir-proxy image by digest (module phr validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.fhir_proxy_image != null
    error_message = "fhir_proxy_image is not set. Build the images first (infra/envs/README.md, step 6) and paste the build's tfvars block; only the targeted applies of steps 3 and 4 run without images."
  }
}

variable "cuff_sim_image" {
  description = "cuff-sim image by digest (module phr validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.cuff_sim_image != null
    error_message = "cuff_sim_image is not set. Build the images first (infra/envs/README.md, step 6) and paste the build's tfvars block; only the targeted applies of steps 3 and 4 run without images."
  }
}

variable "health_sync_image" {
  description = "health-sync image by digest; used only without synthetic data."
  type        = string
  default     = null
}

variable "seed_image" {
  description = "Seeder image by digest; used only with synthetic data."
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------------------------
# The patient (ONE value each, fed to every module that needs it)
# ---------------------------------------------------------------------------------------------

variable "patient_id" {
  description = "The one configured patient identifier (urn:pulsebridge:patient). Fed to ingestion (v2-to-fhir, health-sync or the seeder) and phr (fhir-proxy, cuff-sim), and printed in the setup_env output for the connect script and the seeder."
  type        = string
  nullable    = false
}

variable "patient_assigning_authority" {
  description = "PID-3.4 of every cuff message (cuff-sim) and the authority v2-to-fhir requires. One value, fed to both."
  type        = string
  nullable    = false
}

variable "patient_family_name" {
  description = "PID-5.1 the cuff simulator sends. Real data only; with synthetic data the stack passes null and module phr uses the seeder's synthetic name."
  type        = string
  default     = null
}

variable "patient_given_name" {
  description = "PID-5.2 the cuff simulator sends. Real data only, as patient_family_name."
  type        = string
  default     = null
}

variable "time_zone" {
  description = "The user's IANA time zone (health-sync and the seeder)."
  type        = string
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------------------------

variable "alert_email" {
  description = "Email address module ingestion's alerts go to (an email notification channel is created)."
  type        = string
  default     = null
}

variable "alert_notification_channels" {
  description = "Existing Cloud Monitoring notification channels the alerts also go to."
  type        = list(string)
  default     = []
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Reviewer password (dev only; see allow_reviewer_password_from_tfvars)
# ---------------------------------------------------------------------------------------------

variable "reviewer_password" {
  description = "Dev only: the shared reviewer password, passed to module identity's write-only secret version. Ephemeral: never in plan or state."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true
}

variable "reviewer_password_wo_version" {
  description = "Dev only: 0 writes no version; 1 or more writes reviewer_password; raise it to send a new one."
  type        = number
  default     = 0
  nullable    = false

  validation {
    # Prod rotates the password with T27's script; a tfvars-managed version would be partly undone
    # by the script and partly undo it (module identity README, "Dev only").
    condition     = var.reviewer_password_wo_version == 0 || var.allow_reviewer_password_from_tfvars
    error_message = "The reviewer password may come from tfvars only in dev. In prod leave reviewer_password and reviewer_password_wo_version unset and use scripts/secrets/set-reviewer-password."
  }
}

# ---------------------------------------------------------------------------------------------
# Network and VM
# ---------------------------------------------------------------------------------------------

variable "mllp_port" {
  description = "MLLP port: project-base opens it in the firewall, edge-vm publishes the adapter on it, cuff-sim sends to it (one value for all three)."
  type        = number
  nullable    = false
}

variable "openemr_https_port" {
  description = "OpenEMR's HTTPS port: project-base opens it, module openemr publishes it, fhir-proxy calls it."
  type        = number
  nullable    = false
}

variable "enable_iap_ssh" {
  description = "project-base's IAP-only SSH rule to the edge VM (for the first-boot checklist)."
  type        = bool
  nullable    = false
}

variable "enable_cloud_nat" {
  description = "project-base's Cloud NAT (billed hourly). Only the fallback if Private Google Access does not reach Artifact Registry."
  type        = bool
  nullable    = false
}

variable "openemr_scopes" {
  description = "OAuth2 scopes for the OpenEMR client. The default is module openemr's own default, spike S1's six SMART v1 scopes (the only set verified end to end). It is repeated here because module openemr's scopes variable is nullable: passing null would set it to null, not to its default. null from the root means this default (nullable = false)."
  type        = list(string)
  nullable    = false
  default = [
    "system/Patient.read",
    "system/Patient.write",
    "system/Encounter.read",
    "system/Encounter.write",
    "system/Observation.read",
    "system/Observation.write",
  ]
}

# ---------------------------------------------------------------------------------------------
# Front door and ingress fallbacks
# ---------------------------------------------------------------------------------------------

variable "trusted_proxy_hops" {
  description = "fhir-proxy's TRUSTED_PROXY_HOPS (module phr); measure on the first deploy."
  type        = number
  nullable    = false
}

variable "public_origin" {
  description = "The origin reviewers open; null is the Hosting site's https://<site>.web.app."
  type        = string
  default     = null
}

variable "hosting_site_id" {
  description = "Firebase Hosting site ID; null is the project's default site (the project ID)."
  type        = string
  default     = null
}

variable "extra_allowed_origins" {
  description = "Extra CORS origins for fhir-proxy (for example a local development page)."
  type        = list(string)
  default     = []
  nullable    = false
}

variable "v2_to_fhir_ingress" {
  description = "v2-to-fhir ingress (module ingestion); the fallback INGRESS_TRAFFIC_ALL only if pushes are refused at the network level."
  type        = string
  nullable    = false
}

variable "health_sync_ingress" {
  description = "health-sync ingress (module ingestion); the fallback INGRESS_TRAFFIC_ALL only if fhir-proxy's /status call is refused."
  type        = string
  nullable    = false
}
