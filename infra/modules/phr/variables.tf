# ---------------------------------------------------------------------------------------------
# Where and what to call things
# ---------------------------------------------------------------------------------------------

variable "project_id" {
  description = "ID of the GCP project the two Cloud Run services and the Firebase Hosting site are created in. Comes from the env root; never hardcoded."
  type        = string

  validation {
    # GCP project ID rules: 6 to 30 characters, lowercase letters, digits and hyphens,
    # starting with a letter and not ending with a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID: 6-30 characters, lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "region" {
  description = "Region of both Cloud Run services, such as \"us-central1\". Use the region of the Healthcare dataset and of project-base's serverless subnet (Direct VPC egress needs a subnet in the service's region). It must be one of the regions Firebase Hosting can rewrite to (README, \"Hosting\"); us-central1, us-east1, us-west1, europe-west1 and asia-east1 are the ones Firebase recommends for speed."
  type        = string
  default     = "us-central1"

  validation {
    # "Rewrites to Cloud Run from Hosting are supported in the following regions"
    # (firebase.google.com/docs/hosting/cloud-run, read 2026-11-15). A rewrite to any other
    # region would deploy, then fail every /api request.
    condition = contains([
      "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2", "asia-northeast3",
      "asia-south1", "asia-south2", "asia-southeast1", "asia-southeast2", "asia-southeast3",
      "australia-southeast1", "australia-southeast2", "europe-central2", "europe-north1",
      "europe-southwest1", "europe-west1", "europe-west12", "europe-west2", "europe-west3",
      "europe-west4", "europe-west6", "europe-west8", "europe-west9", "me-central1",
      "me-central2", "me-west1", "northamerica-northeast1", "northamerica-northeast2",
      "southamerica-east1", "southamerica-west1", "us-central1", "us-east1", "us-east4",
      "us-east5", "us-south1", "us-west1", "us-west2", "us-west3", "us-west4",
    ], var.region)
    error_message = "region must be a Cloud Run region that Firebase Hosting rewrites support (see firebase.google.com/docs/hosting/cloud-run), such as \"us-central1\"."
  }
}

variable "name_prefix" {
  description = "Prefix for the Cloud Run service names: \"<prefix>-fhir-proxy\" and \"<prefix>-cuff-sim\". Use the same prefix as the other modules."
  type        = string
  default     = "pulsebridge"

  validation {
    # Cloud Run service names: lowercase letters, digits and hyphens, starting with a letter, at
    # most 49 characters. The longest suffix here is "-fhir-proxy" (11), so 20 leaves room.
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 2-20 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "labels" {
  description = "Labels added to both Cloud Run services (the Hosting site takes none)."
  type        = map(string)
  default     = {}
}

variable "deletion_protection" {
  description = "Refuse to destroy the two Cloud Run services (the provider's deletion_protection, which defaults to true). false (default) here: they hold no data, and dev is destroyed nightly. The Hosting site is not covered: see the README, \"Destroy\"."
  type        = bool
  default     = false
  nullable    = false
}

# ---------------------------------------------------------------------------------------------
# Which wearable source (decides the patient rules and whether health-sync exists)
# ---------------------------------------------------------------------------------------------

variable "use_synthetic_data" {
  description = "The same switch as module ingestion's. true: a synthetic deployment, so patient_id must start with \"SYN-\" and the cuff simulator's patient name is the seeder's synthetic name (family \"Synthetic\", given \"Demo\"), and there is no health-sync (health_sync_url stays null). false (default): a real Google Health deployment."
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
  # to other bytes, which would make an apply deploy something nobody tested. The same rule as
  # module ingestion.
  image_by_digest_regex = "^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
}

variable "fhir_proxy_image" {
  description = "fhir-proxy image by digest, as the build prints it: \"<region>-docker.pkg.dev/<project>/<repository>/fhir-proxy@sha256:<64 hex>\". A tag (\":v1\") is refused, with or without a digest."
  type        = string

  validation {
    condition     = can(regex(local.image_by_digest_regex, var.fhir_proxy_image))
    error_message = "fhir_proxy_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

variable "cuff_sim_image" {
  description = "cuff-sim image by digest (same form as fhir_proxy_image), as the build prints it."
  type        = string

  validation {
    condition     = can(regex(local.image_by_digest_regex, var.cuff_sim_image))
    error_message = "cuff_sim_image must be an image by digest with no tag: <host>/<path>@sha256:<64 lowercase hex>."
  }
}

# ---------------------------------------------------------------------------------------------
# Healthcare coordinates (module healthcare's outputs)
# ---------------------------------------------------------------------------------------------

variable "healthcare_project_id" {
  description = "Project of the Healthcare dataset (module.healthcare.project_id). null (default) means project_id. Becomes GCP_PROJECT_ID for both services."
  type        = string
  default     = null

  validation {
    condition     = var.healthcare_project_id == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", coalesce(var.healthcare_project_id, "x")))
    error_message = "healthcare_project_id must be a valid GCP project ID."
  }
}

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
  description = "Short ID of the main (health-data) FHIR store (module.healthcare.fhir_store_name). fhir-proxy reads it (FHIR_STORE_ID); cuff-sim has no access to it (D15)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.fhir_store_id))
    error_message = "fhir_store_id must be the short FHIR store ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
}

variable "audit_fhir_store_id" {
  description = "Short ID of the audit FHIR store (module.healthcare.audit_fhir_store_name, D15): cuff-sim writes its send-attempt AuditEvents there, fhir-proxy reads them. Becomes AUDIT_FHIR_STORE_ID for both. Must differ from fhir_store_id and hl7v2_store_id (both services refuse to start otherwise)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.audit_fhir_store_id))
    error_message = "audit_fhir_store_id must be the short FHIR store ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
  validation {
    condition     = var.audit_fhir_store_id != var.fhir_store_id && var.audit_fhir_store_id != var.hl7v2_store_id
    error_message = "audit_fhir_store_id must differ from fhir_store_id (D15: the audit data has its own store) and from hl7v2_store_id (cuff-sim refuses that)."
  }
}

variable "hl7v2_store_id" {
  description = "Short ID of the HL7v2 store (module.healthcare.hl7v2_store_name). Becomes HL7V2_STORE_ID for both services."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,256}$", var.hl7v2_store_id))
    error_message = "hl7v2_store_id must be the short HL7v2 store ID (letters, digits, '_', '-', '.'), not a projects/... path."
  }
}

# ---------------------------------------------------------------------------------------------
# The patient (the same values every service and v2-to-fhir use)
# ---------------------------------------------------------------------------------------------

variable "patient_id" {
  description = <<-EOT
    The one configured patient identifier (urn:pulsebridge:patient), the same value module
    ingestion's patient_id gets. Becomes PATIENT_ID for both services and PID-3.1 of every
    message cuff-sim builds. Letters, digits, '.', '_' and '-', at most 64 characters. With
    use_synthetic_data it must start with "SYN-"; without it, it must not (the same rules as
    module ingestion). Never commit a real one: it comes from tfvars.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.patient_id))
    error_message = "patient_id must be 1-64 characters of letters, digits, '.', '_' and '-'."
  }
  validation {
    condition     = !var.use_synthetic_data || (startswith(var.patient_id, "SYN-") && length(var.patient_id) > 4)
    error_message = "With use_synthetic_data = true, patient_id must start with \"SYN-\" (for example \"SYN-0001\")."
  }
  validation {
    condition     = var.use_synthetic_data || !startswith(var.patient_id, "SYN-")
    error_message = "A \"SYN-\" patient_id is reserved for synthetic data: with use_synthetic_data = false it is refused."
  }
}

variable "patient_assigning_authority" {
  description = "PID-3.4 of every message cuff-sim builds (PATIENT_ASSIGNING_AUTHORITY, required by cuff-sim). Pass the SAME value to module ingestion's patient_assigning_authority, so v2-to-fhir accepts the messages (it then requires PID-3.4.1 to equal it). Letters, digits, '.', '_' and '-', at most 20 characters (cuff-sim's limit; v2-to-fhir allows 64)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,20}$", var.patient_assigning_authority))
    error_message = "patient_assigning_authority must be 1-20 characters of letters, digits, '.', '_' and '-'."
  }
}

variable "patient_family_name" {
  description = "PID-5.1 of the messages cuff-sim builds (PATIENT_FAMILY_NAME). With use_synthetic_data: leave null (the seeder's synthetic name \"Synthetic\" is used) or set exactly \"Synthetic\"; any other name is refused, because the seeder refuses to work on a Patient whose name is not its synthetic one (services/seed/README.md). Without synthetic data it is required: the real patient's family name, from tfvars, never committed. It is stored in Cloud Run's configuration and in the Terraform state."
  type        = string
  default     = null

  validation {
    condition     = var.patient_family_name == null || can(regex("^[\\x20-\\x5b\\x5d-\\x7e]{1,80}$", coalesce(var.patient_family_name, "x")))
    error_message = "patient_family_name must be 1-80 printable ASCII characters without a backslash (HL7's escape character)."
  }
  validation {
    condition     = !var.use_synthetic_data || var.patient_family_name == null || var.patient_family_name == "Synthetic"
    error_message = "With use_synthetic_data = true the patient's family name must be the seeder's synthetic name, \"Synthetic\" (or null for that default)."
  }
  validation {
    condition     = var.use_synthetic_data || var.patient_family_name != null
    error_message = "patient_family_name is required when use_synthetic_data is false."
  }
}

variable "patient_given_name" {
  description = "PID-5.2 of the messages cuff-sim builds (PATIENT_GIVEN_NAME). With use_synthetic_data: null (the seeder's \"Demo\") or exactly \"Demo\". Without synthetic data it is required. Same storage note as patient_family_name."
  type        = string
  default     = null

  validation {
    condition     = var.patient_given_name == null || can(regex("^[\\x20-\\x5b\\x5d-\\x7e]{1,80}$", coalesce(var.patient_given_name, "x")))
    error_message = "patient_given_name must be 1-80 printable ASCII characters without a backslash (HL7's escape character)."
  }
  validation {
    condition     = !var.use_synthetic_data || var.patient_given_name == null || var.patient_given_name == "Demo"
    error_message = "With use_synthetic_data = true the patient's given name must be the seeder's synthetic name, \"Demo\" (or null for that default)."
  }
  validation {
    condition     = var.use_synthetic_data || var.patient_given_name != null
    error_message = "patient_given_name is required when use_synthetic_data is false."
  }
}

# ---------------------------------------------------------------------------------------------
# Identities and secrets (module identity's outputs, passed whole)
# ---------------------------------------------------------------------------------------------

variable "service_account_emails" {
  description = "Module identity's map of workload name to service account email (module.identity.service_account_emails), passed as it is. Keys used: \"fhir-proxy\" (runs fhir-proxy; the only invoker of cuff-sim) and \"cuff-sim\" (runs cuff-sim). Other keys are ignored."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["fhir-proxy", "cuff-sim"] : contains(keys(var.service_account_emails), k)])
    error_message = "service_account_emails must contain the keys fhir-proxy and cuff-sim (module identity creates both)."
  }
  validation {
    condition = alltrue([
      for k, v in var.service_account_emails :
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\\.iam\\.gserviceaccount\\.com$", v))
      if contains(["fhir-proxy", "cuff-sim"], k)
    ])
    error_message = "service_account_emails values for fhir-proxy and cuff-sim must be service account emails (<name>@<project>.iam.gserviceaccount.com)."
  }
}

variable "secret_ids" {
  description = "Module identity's map of secret key to Secret Manager secret ID (module.identity.secret_ids), passed as it is. fhir-proxy mounts \"session-signing-key\" (D16: the HS256 key of the demo session cookie) as a file; other keys are ignored (the OpenEMR client secret comes through var.openemr)."
  type        = map(string)

  validation {
    condition     = contains(keys(var.secret_ids), "session-signing-key")
    error_message = "secret_ids must contain the key session-signing-key (module identity creates it)."
  }
  validation {
    # Secret Manager secret IDs: letters, digits, '_' and '-', at most 255.
    condition     = can(regex("^[A-Za-z0-9_-]{1,255}$", lookup(var.secret_ids, "session-signing-key", "")))
    error_message = "secret_ids[\"session-signing-key\"] must be a short secret ID (letters, digits, '_', '-'), not a projects/... path."
  }
}

# ---------------------------------------------------------------------------------------------
# Network (module project-base and module edge-vm outputs)
# ---------------------------------------------------------------------------------------------

variable "network_name" {
  description = "Name of the VPC network (module.project_base.network_name). Both services join it with Direct VPC egress."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.network_name))
    error_message = "network_name must be a VPC network name (lowercase letters, digits and hyphens), not a projects/... path."
  }
}

variable "serverless_subnet_name" {
  description = "Name of project-base's serverless subnet (module.project_base.serverless_subnet_name), in this module's region. Both services take their Direct VPC egress addresses from it; it is the only source project-base's firewall lets reach the edge VM (MLLP and OpenEMR HTTPS), and it has Private Google Access."
  type        = string

  validation {
    condition     = can(regex("^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$", var.serverless_subnet_name))
    error_message = "serverless_subnet_name must be a subnet name (lowercase letters, digits and hyphens), not a projects/... path."
  }
}

variable "mllp_host" {
  description = "The edge VM's reserved internal IPv4 address (module.edge_vm.mllp_host), cuff-sim's MLLP_HOST. An address, not a host name: cuff-sim's name lookup would have no time limit."
  type        = string

  validation {
    condition     = can(regex("^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])$", var.mllp_host))
    error_message = "mllp_host must be an IPv4 address such as \"10.10.0.2\" (the edge VM's internal address)."
  }
}

variable "mllp_port" {
  description = "Port of the MLLP adapter on the edge VM (module.edge_vm.mllp_port; the same port project-base's firewall opens from the serverless subnet). Becomes MLLP_PORT."
  type        = number
  default     = 2575

  validation {
    condition     = var.mllp_port >= 1 && var.mllp_port <= 65535 && floor(var.mllp_port) == var.mllp_port
    error_message = "mllp_port must be a whole number from 1 to 65535."
  }
}

# ---------------------------------------------------------------------------------------------
# Other services fhir-proxy calls
# ---------------------------------------------------------------------------------------------

variable "health_sync_url" {
  description = "health-sync's base URL (module.ingestion.health_sync_url), or null when there is none (synthetic data). fhir-proxy calls <url>/status with an OIDC token whose audience is this URL; module ingestion already grants fhir-proxy roles/run.invoker on health-sync. null leaves HEALTH_SYNC_URL unset, and the status strip then reports health-sync-not-deployed."
  type        = string
  default     = null

  validation {
    condition     = var.health_sync_url == null || can(regex("^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", coalesce(var.health_sync_url, "x")))
    error_message = "health_sync_url must be an https URL with no path, such as https://pulsebridge-health-sync-abc123-uc.a.run.app."
  }
  validation {
    # With synthetic data module ingestion creates no health-sync, so a URL here is a wiring
    # mistake (and the proxy would report a service that does not exist as failing).
    condition     = !var.use_synthetic_data || var.health_sync_url == null
    error_message = "health_sync_url must be null with use_synthetic_data = true (module ingestion creates no health-sync then)."
  }
}

variable "openemr" {
  description = <<-EOT
    Optional, for the share to OpenEMR (T15): module openemr's outputs, all together or none
    (null, the default). When set, fhir-proxy gets OPENEMR_SITE_ADDRESS, OPENEMR_TOKEN_URL,
    OPENEMR_FHIR_BASE_URL and OPENEMR_SCOPES as plain environment variables, and the client
    secret (module openemr's client_secret_id: the private key, client ID and OpenEMR's
    certificate in one JSON document) mounted as a FILE at version latest, its path in
    OPENEMR_CLIENT_FILE. fhir-proxy as built by T14 ignores these; T15 reads them (the names
    are the openemr module's suggestions). fhir-proxy's account already has secretAccessor on
    that secret (module identity).
  EOT
  type = object({
    site_address     = string
    token_url        = string
    fhir_base_url    = string
    scopes           = string
    client_secret_id = string
  })
  default = null

  validation {
    condition = var.openemr == null || alltrue([
      for u in [try(var.openemr.site_address, ""), try(var.openemr.token_url, ""), try(var.openemr.fhir_base_url, "")] :
      can(regex("^https://[^/?#\\s]+(/[^?#\\s]*)?$", u))
    ])
    error_message = "openemr.site_address, token_url and fhir_base_url must be https URLs without a query or fragment."
  }
  validation {
    condition     = var.openemr == null || can(regex("^[A-Za-z0-9_-]{1,255}$", try(var.openemr.client_secret_id, "")))
    error_message = "openemr.client_secret_id must be a short Secret Manager secret ID, not a projects/... path."
  }
  validation {
    condition     = var.openemr == null || can(regex("^[!-~]+( [!-~]+)*$", try(var.openemr.scopes, "")))
    error_message = "openemr.scopes must be space-separated scope strings."
  }
}

# ---------------------------------------------------------------------------------------------
# Hosting and the public address
# ---------------------------------------------------------------------------------------------

variable "manage_firebase_project" {
  description = "Whether this module adds Firebase to the project (google_firebase_project), which Firebase Hosting needs. false (default): bootstrap added Firebase once with the owner's credentials (its add_firebase, default true; README, \"Adding Firebase to the project\"), so the deployer needs only roles/firebasehosting.admin. true: this module adds it, and the deployer also needs firebase.projects.update (roles/firebase.admin). Firebase cannot be removed from a project again, so destroying this resource only forgets it."
  type        = bool
  default     = false
  nullable    = false
}

variable "hosting_site_id" {
  description = "Firebase Hosting site ID: the page is served at https://<site_id>.web.app. null (default) uses project_id, the project's default site. Site IDs are globally unique DNS labels. An existing site with this ID is adopted on create (the provider reads it first), so no import is needed. Destroy: the provider SKIPS deleting a default site (type DEFAULT_SITE), so after `terraform destroy` the last deployed page stays public at <site_id>.web.app with every /api call failing; run `firebase hosting:disable` if no page should stay live (README, \"Destroy\")."
  type        = string
  default     = null

  validation {
    condition     = var.hosting_site_id == null || can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", coalesce(var.hosting_site_id, "x")))
    error_message = "hosting_site_id must be a DNS label: lowercase letters, digits and hyphens, at most 63 characters."
  }
}

variable "public_origin" {
  description = "The origin the page is served from and so the one origin the page reaches fhir-proxy through (\"https://<host>\", no path). null (default) is the Hosting site's https://<site_id>.web.app. Set it when a custom domain (T30) is the address reviewers use. It becomes PUBLIC_BASE_URL (plus \"/api\") and the first entry of ALLOWED_ORIGINS."
  type        = string
  default     = null

  validation {
    condition     = var.public_origin == null || can(regex("^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", coalesce(var.public_origin, "x")))
    error_message = "public_origin must be https://<lowercase host name> with no port, path or trailing slash."
  }
}

variable "extra_allowed_origins" {
  description = "Further entries for fhir-proxy's Origin check (ALLOWED_ORIGINS), after the page's own origins (public_origin, https://<site>.web.app, https://<site>.firebaseapp.com). Since D16 there is no cross-origin use: the session cookie is SameSite=Strict and the proxy sends no CORS credentials, so a page must reach the proxy on its own origin. An entry here only lets a same-origin setup elsewhere pass the Origin check on POST, for example \"http://localhost:5173\" for Vite's dev-server proxy (web/README.md, PULSEBRIDGE_DEV_PROXY_TARGET). Empty (default) in prod. Each entry exactly as a browser sends Origin."
  type        = list(string)
  default     = []
  nullable    = false

  validation {
    condition = alltrue([
      for o in var.extra_allowed_origins :
      can(regex("^(https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?|http://(localhost|127\\.0\\.0\\.1)(:[0-9]{1,5})?)$", o))
    ])
    error_message = "Each extra_allowed_origins entry must be an origin: https://<host>[:port], or http://localhost[:port] / http://127.0.0.1[:port], with no path or trailing slash."
  }
}

variable "trusted_proxy_hops" {
  description = <<-EOT
    TRUSTED_PROXY_HOPS for fhir-proxy: how many X-Forwarded-For entries, counted from the
    right, were appended by proxies Google runs in front of it. The per-address demo session
    limit (SESSIONS_PER_ADDRESS_PER_HOUR, D16) keys on the entry just left of them. 0 to 5.
    Default 2, MEASURED (dev, 17 November 2026; infra/envs/README.md, checklist P1): the page
    reaches the proxy only through Firebase Hosting's rewrite, and there the proxy receives two
    entries, "<visitor>, <Hosting's address>". Hosting replaces any X-Forwarded-For the visitor
    sent, so the second entry from the right is the visitor. With 1 the key would be a
    Hosting address shared by visitors, so anyone could keep every visitor from starting a
    demo session through the public page (5 attempts an hour fill one such key; how many
    Hosting addresses there are was not measured).
    Use another value only for another front door: 1 if the page called the run.app URL
    directly with nothing in front (the service's own default), 0 never on Cloud Run (local
    use: the header is ignored). The known cost of 2: the run.app URL stays publicly reachable
    (Hosting needs it), and a direct caller there can write the entry used as the key, so the
    per-address limit does not hold for it (README, "The client address and the direct
    run.app URL").
  EOT
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = var.trusted_proxy_hops >= 0 && var.trusted_proxy_hops <= 5 && floor(var.trusted_proxy_hops) == var.trusted_proxy_hops
    error_message = "trusted_proxy_hops must be a whole number from 0 to 5."
  }
}

variable "page_deployer_service_account_email" {
  description = <<-EOT
    Email of CI's page deployer service account (bootstrap output
    page_deployer_service_account_email), or null (default). When set, this module grants it
    roles/run.viewer on the fhir-proxy service, and on nothing else. Why: when `firebase deploy`
    finalizes a Hosting version whose config rewrites /api to a Cloud Run service, Firebase
    Hosting checks that the deploying account has run.services.get on that service; without it
    the deploy fails with 403 "Permission 'run.services.get' denied" after the files are uploaded
    (first CI run on dev, 18 November 2026). run.viewer on this one service reads its
    configuration only; it cannot change, invoke or delete it. That configuration includes the
    service's environment, so the page deploy job can read the patient's ID (and, with real
    data, name), as the planner can; no narrower predefined role holds run.services.get (a custom role with
    only it would expose the same configuration). null: no grant (no
    CI page deploy, or the page is deployed by hand by someone who already holds the permission).
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.page_deployer_service_account_email == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\\.iam\\.gserviceaccount\\.com$", coalesce(var.page_deployer_service_account_email, "x")))
    error_message = "page_deployer_service_account_email must be a service account email, <account_id>@<project_id>.iam.gserviceaccount.com (bootstrap's output page_deployer_service_account_email), or null."
  }
}

# ---------------------------------------------------------------------------------------------
# Service settings
# ---------------------------------------------------------------------------------------------

variable "cuff_sim_max_instances" {
  description = "Upper bound on cuff-sim instances. Its rate limit (6 sends a minute, bursts of 3) is per instance, because each instance runs one gunicorn worker with its own token bucket, so the service-wide limit is that times this number. 1 (default) makes the limit service-wide: 6 a minute. At most 3."
  type        = number
  default     = 1
  nullable    = false

  validation {
    condition     = var.cuff_sim_max_instances >= 1 && var.cuff_sim_max_instances <= 3 && floor(var.cuff_sim_max_instances) == var.cuff_sim_max_instances
    error_message = "cuff_sim_max_instances must be a whole number from 1 to 3."
  }
}

# ---------------------------------------------------------------------------------------------
# The public demo (D16, T44): bot check and session settings. The names and defaults are the T43
# to T45 contract (docs/workplan.md, "The contract T43 to T45 share"); fhir-proxy checks them
# again at start.
# ---------------------------------------------------------------------------------------------

variable "recaptcha_min_score" {
  description = "RECAPTCHA_MIN_SCORE: the lowest reCAPTCHA score (0.0 likely a bot, 1.0 likely a person) that gets a demo session. 0.5 (default) is Google's suggested starting threshold for score keys. Above 0 and at most 1."
  type        = number
  default     = 0.5
  nullable    = false

  validation {
    condition     = var.recaptcha_min_score > 0 && var.recaptcha_min_score <= 1
    error_message = "recaptcha_min_score must be greater than 0 and at most 1."
  }
}

variable "session_ttl_seconds" {
  description = "SESSION_TTL_SECONDS: how long a demo session (the __session cookie and the token's exp) lasts. 86400 (default, 24 hours, D16). From 300 to 86400."
  type        = number
  default     = 86400
  nullable    = false

  validation {
    condition     = var.session_ttl_seconds >= 300 && var.session_ttl_seconds <= 86400 && floor(var.session_ttl_seconds) == var.session_ttl_seconds
    error_message = "session_ttl_seconds must be a whole number from 300 to 86400."
  }
}

variable "sessions_per_address_per_hour" {
  description = "SESSIONS_PER_ADDRESS_PER_HOUR: how many POST /auth/session attempts one client address may make in an hour, checked BEFORE any assessment is created, so a flood from one address costs nothing. Per proxy instance (in memory), so the real limit is this times the instance cap (2). 5 (default). From 1 to 100."
  type        = number
  default     = 5
  nullable    = false

  validation {
    condition     = var.sessions_per_address_per_hour >= 1 && var.sessions_per_address_per_hour <= 100 && floor(var.sessions_per_address_per_hour) == var.sessions_per_address_per_hour
    error_message = "sessions_per_address_per_hour must be a whole number from 1 to 100."
  }
}

variable "bot_checks_per_day" {
  description = "BOT_CHECKS_PER_DAY: the most reCAPTCHA assessments fhir-proxy creates in one UTC day, counted across instances in the demo counter bucket; past it the page says \"The demo is busy, try again tomorrow.\" 300 (default): about 9,000 a month, under the 10,000 assessments a month that are free per organisation. From 1 to 3000. Google offers no per-day quota on assessments to back this with a hard cap (README, \"The public demo\")."
  type        = number
  default     = 300
  nullable    = false

  validation {
    condition     = var.bot_checks_per_day >= 1 && var.bot_checks_per_day <= 3000 && floor(var.bot_checks_per_day) == var.bot_checks_per_day
    error_message = "bot_checks_per_day must be a whole number from 1 to 3000."
  }
}
