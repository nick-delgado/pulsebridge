# Inputs of the stack composition. The env roots pass their own variables through unchanged and
# add the per-environment settings (environment, protect_from_destroy) from their env.tf.
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
# module.stack.module.project_base or module.stack.module.identity (runbook steps 3, 4 and 6) does
# not evaluate these validations, because nothing it plans depends on the images.
variable "v2_to_fhir_image" {
  description = "v2-to-fhir image by digest (module ingestion validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.v2_to_fhir_image != null
    error_message = "v2_to_fhir_image is not set. Build the images first (infra/envs/README.md, step 7) and paste the build's tfvars block; only the targeted applies of steps 3, 4 and 6 run without images."
  }
}

variable "fhir_proxy_image" {
  description = "fhir-proxy image by digest (module phr validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.fhir_proxy_image != null
    error_message = "fhir_proxy_image is not set. Build the images first (infra/envs/README.md, step 7) and paste the build's tfvars block; only the targeted applies of steps 3, 4 and 6 run without images."
  }
}

variable "cuff_sim_image" {
  description = "cuff-sim image by digest (module phr validates the form)."
  type        = string
  default     = null

  validation {
    condition     = var.cuff_sim_image != null
    error_message = "cuff_sim_image is not set. Build the images first (infra/envs/README.md, step 7) and paste the build's tfvars block; only the targeted applies of steps 3, 4 and 6 run without images."
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
  sensitive   = true
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
  sensitive   = true
  default     = null
}

variable "patient_given_name" {
  description = "PID-5.2 the cuff simulator sends. Real data only, as patient_family_name."
  type        = string
  sensitive   = true
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
  description = "Email address module ingestion's alerts go to (an email notification channel is created). Sensitive: plans, including the one CI posts on a pull request, show \"(sensitive value)\" (T26)."
  type        = string
  default     = null
  sensitive   = true
}

variable "alert_notification_channels" {
  description = "Existing Cloud Monitoring notification channels the alerts also go to."
  type        = list(string)
  default     = []
  nullable    = false
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
  description = "OAuth2 scopes for the OpenEMR client; null keeps module openemr's default (spike S1's six SMART v1 scopes)."
  type        = list(string)
  default     = null
}

# ---------------------------------------------------------------------------------------------
# Front door and ingress fallbacks
# ---------------------------------------------------------------------------------------------

variable "trusted_proxy_hops" {
  description = "fhir-proxy's TRUSTED_PROXY_HOPS (module phr). No default here: the env roots pass theirs (2, measured behind Firebase Hosting; infra/envs/README.md, checklist P1)."
  type        = number
  nullable    = false
}

variable "page_deployer_service_account_email" {
  description = "CI's page deployer (bootstrap output page_deployer_service_account_email), or null: module phr then grants it roles/run.viewer on fhir-proxy only (T41). No default here: the env roots pass theirs (null by default), so a root that drops it fails validation rather than silently losing the grant."
  type        = string
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

# ---------------------------------------------------------------------------------------------
# Image builds (T26)
# ---------------------------------------------------------------------------------------------

variable "create_build_service_account" {
  description = "Create the dedicated image-build service account (module identity) with its grants: log writer on the project, writer on the services repository and reader on the Docker Hub mirror (module project-base). Needed by the build trigger; also usable for a manual `gcloud builds submit --service-account`."
  type        = bool
  nullable    = false
}

variable "build_trigger_repository" {
  description = "Opt-in Cloud Build trigger on release tags (module project-base): the 2nd-gen repository's full resource name, or null for no trigger. Requires create_build_service_account."
  type        = string
  default     = null

  validation {
    condition     = var.build_trigger_repository == null || var.create_build_service_account
    error_message = "build_trigger_repository needs create_build_service_account = true: the trigger runs the build as that account."
  }
}

variable "build_trigger_approval_required" {
  description = "Triggered builds wait for a manual approval in the Cloud Build console (module project-base)."
  type        = bool
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# The public demo (D16, T44): optional; null keeps module phr's default
# ---------------------------------------------------------------------------------------------

variable "recaptcha_min_score" {
  description = "fhir-proxy's RECAPTCHA_MIN_SCORE: the lowest reCAPTCHA score (0.0 likely a bot, 1.0 likely a person) that gets a demo session, above 0 and at most 1. null (default): module phr's 0.5. Tune it after the first real deploy, from the scores real visitors get."
  type        = number
  default     = null
}

variable "bot_checks_per_day" {
  description = "fhir-proxy's BOT_CHECKS_PER_DAY: the most reCAPTCHA assessments in one UTC day across all instances (the counter bucket), 1 to 3000; past it the page says the demo is busy. null (default): module phr's 300 (about 9,000 a month, under the 10,000 free per organisation)."
  type        = number
  default     = null
}

variable "session_ttl_seconds" {
  description = "fhir-proxy's SESSION_TTL_SECONDS: how long a demo session lasts, 300 to 86400. null (default): module phr's 86400 (24 hours, D16)."
  type        = number
  default     = null
}

variable "sessions_per_address_per_hour" {
  description = "fhir-proxy's SESSIONS_PER_ADDRESS_PER_HOUR: session attempts per client address an hour, per instance, checked before any assessment, 1 to 100. null (default): module phr's 5."
  type        = number
  default     = null
}
