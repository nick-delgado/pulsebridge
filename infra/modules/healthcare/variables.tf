# ---------------------------------------------------------------------------------------------
# Where and what to call things
# ---------------------------------------------------------------------------------------------

variable "project_id" {
  description = "ID of the GCP project that holds the Healthcare dataset and the Pub/Sub topics. Comes from the env root; never hardcoded."
  type        = string

  validation {
    # GCP project ID rules: 6 to 30 characters, lowercase letters, digits and hyphens,
    # starting with a letter and not ending with a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID: 6-30 characters, lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "location" {
  description = "Cloud Healthcare API location for the dataset and both stores (a region such as \"us-central1\", or a multi-region such as \"us\"). Stored health data stays in this location."
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+(-[a-z]+[0-9]+)?$", var.location))
    error_message = "location must look like a GCP region (\"us-central1\") or multi-region (\"us\")."
  }
}

variable "name_prefix" {
  description = "Prefix for every resource name in this module (dataset, stores, topics, subscriptions). Lets two stacks share one project without colliding."
  type        = string
  default     = "pulsebridge"

  validation {
    # The strictest of the naming rules involved: Pub/Sub names must start with a letter;
    # Healthcare IDs allow letters, digits, '_' and '-'. 3-30 characters leaves room for suffixes.
    condition     = can(regex("^[a-z][a-z0-9-]{1,28}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-30 characters of lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

variable "time_zone" {
  description = "IANA time zone of the Healthcare dataset. The API uses it to read HL7v2 times that carry no UTC offset (for example an OBX-14 without '+hhmm'). UTC is the safe default; the cuff simulator sends explicit offsets anyway."
  type        = string
  default     = "UTC"

  validation {
    condition     = can(regex("^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$", var.time_zone))
    error_message = "time_zone must be an IANA time zone name such as \"UTC\" or \"Europe/Amsterdam\"."
  }
}

variable "labels" {
  description = "Labels added to the stores, topics and subscriptions (the dataset resource has no labels)."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------------------------
# Destroy behaviour
# ---------------------------------------------------------------------------------------------

variable "deletion_policy" {
  description = <<-EOT
    What `terraform destroy` (or a replacing apply) may do to the dataset and the two stores.
    "DELETE" (default) deletes them together with every stored FHIR resource and HL7v2 message,
    so a destroyed dev stack leaves nothing billable. "PREVENT" makes destroy fail instead, which
    suits prod where the data is real. The setting is read from state, so switching from PREVENT
    to DELETE needs an apply before the destroy. "ABANDON" is not offered: it would leave the
    stores, and their storage bill, behind without Terraform knowing.
  EOT
  type        = string
  default     = "DELETE"

  validation {
    condition     = contains(["DELETE", "PREVENT"], var.deletion_policy)
    error_message = "deletion_policy must be \"DELETE\" or \"PREVENT\"."
  }
}

# ---------------------------------------------------------------------------------------------
# HL7v2 store
# ---------------------------------------------------------------------------------------------

variable "hl7v2_parser_version" {
  description = "Version of the HL7v2 store's built-in (unschematized) parser, which fills the stored message's parsedData. Cannot be changed after the store is created (spike S4). V1 is the version spikes S4 and S5 tested; see the README before changing it."
  type        = string
  default     = "V1"

  validation {
    condition     = contains(["V1", "V2", "V3"], var.hl7v2_parser_version)
    error_message = "hl7v2_parser_version must be one of \"V1\", \"V2\", \"V3\"."
  }
}

# ---------------------------------------------------------------------------------------------
# Notification delivery: push subscription to v2-to-fhir
# ---------------------------------------------------------------------------------------------

variable "push_enabled" {
  description = <<-EOT
    Turn the ingest subscription into an OIDC-authenticated push subscription to v2-to-fhir.
    false (default): the subscription exists as a pull subscription and simply holds
    notifications (up to the retention period) until push is switched on, so nothing is lost
    while the v2-to-fhir service does not exist yet. true: push_endpoint and
    push_service_account_email are required. This is a plain bool, not derived from
    push_endpoint, so Terraform knows at plan time which resources to create even when the
    endpoint is a Cloud Run URL that is only known after apply.
  EOT
  type        = bool
  default     = false
}

variable "push_endpoint" {
  description = "HTTPS URL Pub/Sub posts each notification to: the v2-to-fhir Cloud Run service URL plus its handler path (from module ingestion, T20). Required when push_enabled is true."
  type        = string
  default     = null

  validation {
    condition     = var.push_endpoint == null || can(regex("^https://[^/\\s]+(/\\S*)?$", var.push_endpoint))
    error_message = "push_endpoint must be an https:// URL."
  }

  validation {
    condition     = !var.push_enabled || var.push_endpoint != null
    error_message = "push_endpoint is required when push_enabled is true."
  }
}

variable "push_service_account_email" {
  description = "Service account Pub/Sub signs the push request's OIDC token as: module identity's \"pubsub-push\" account. It needs roles/run.invoker on v2-to-fhir (module ingestion), and the Pub/Sub service agent needs roles/iam.serviceAccountTokenCreator on it, which module identity grants. Required when push_enabled is true."
  type        = string
  default     = null

  validation {
    condition     = var.push_service_account_email == null || can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\\.iam\\.gserviceaccount\\.com$", var.push_service_account_email))
    error_message = "push_service_account_email must be a service account email (name@project.iam.gserviceaccount.com)."
  }

  validation {
    condition     = !var.push_enabled || var.push_service_account_email != null
    error_message = "push_service_account_email is required when push_enabled is true."
  }
}

variable "push_audience" {
  description = "Audience (aud claim) of the OIDC token. null (default) uses the endpoint's origin, scheme and host only, which is the Cloud Run service URL that Cloud Run checks tokens against."
  type        = string
  default     = null
}

variable "ack_deadline_seconds" {
  description = "How long Pub/Sub waits for v2-to-fhir to answer a push before treating it as failed and retrying. 60 s covers a Cloud Run cold start plus a FHIR write; the Pub/Sub default of 10 s does not."
  type        = number
  default     = 60

  validation {
    condition     = var.ack_deadline_seconds >= 10 && var.ack_deadline_seconds <= 600
    error_message = "ack_deadline_seconds must be between 10 and 600 (Pub/Sub's limits)."
  }
}

variable "retry_minimum_backoff" {
  description = "Shortest wait before Pub/Sub redelivers a notification v2-to-fhir did not acknowledge (a duration such as \"10s\"). Waits grow exponentially up to retry_maximum_backoff."
  type        = string
  default     = "10s"

  validation {
    condition     = can(regex("^[0-9]+s$", var.retry_minimum_backoff)) && try(tonumber(trimsuffix(var.retry_minimum_backoff, "s")) <= 600, false)
    error_message = "retry_minimum_backoff must be whole seconds with an 's' suffix, at most \"600s\"."
  }
}

variable "retry_maximum_backoff" {
  description = "Longest wait between redeliveries of one notification (a duration such as \"600s\")."
  type        = string
  default     = "600s"

  validation {
    condition     = can(regex("^[0-9]+s$", var.retry_maximum_backoff)) && try(tonumber(trimsuffix(var.retry_maximum_backoff, "s")) <= 600, false)
    error_message = "retry_maximum_backoff must be whole seconds with an 's' suffix, at most \"600s\"."
  }

  validation {
    condition     = try(tonumber(trimsuffix(var.retry_maximum_backoff, "s")) >= tonumber(trimsuffix(var.retry_minimum_backoff, "s")), false)
    error_message = "retry_maximum_backoff must not be shorter than retry_minimum_backoff."
  }
}

variable "max_delivery_attempts" {
  description = "Delivery attempts before a notification is moved to the dead-letter topic. v2-to-fhir acknowledges mapping errors itself (D7), so only infrastructure failures retry; 10 attempts with the default backoff span roughly 40 minutes, enough to ride out a cold start or a short API outage."
  type        = number
  default     = 10

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100 && floor(var.max_delivery_attempts) == var.max_delivery_attempts
    error_message = "max_delivery_attempts must be a whole number from 5 to 100 (Pub/Sub's limits)."
  }
}

variable "message_retention_duration" {
  description = "How long the ingest and dead-letter subscriptions keep unacknowledged notifications (a duration such as \"604800s\" = 7 days, Pub/Sub's default). Pub/Sub allows up to 31 days; this module caps it at 7 days as a cost choice, since unacknowledged messages older than a day can be billed as storage (negligible at this volume, but not zero)."
  type        = string
  default     = "604800s"

  validation {
    # Pub/Sub accepts 10 minutes to 31 days; the 7-day cap is this module's cost choice.
    condition     = can(regex("^[0-9]+s$", var.message_retention_duration)) && try(tonumber(trimsuffix(var.message_retention_duration, "s")) >= 600 && tonumber(trimsuffix(var.message_retention_duration, "s")) <= 604800, false)
    error_message = "message_retention_duration must be whole seconds with an 's' suffix, from \"600s\" to \"604800s\"."
  }
}

# ---------------------------------------------------------------------------------------------
# Store-level IAM for the service accounts created by module identity (T04)
# ---------------------------------------------------------------------------------------------

variable "store_iam_service_accounts" {
  description = <<-EOT
    Module identity's map of account name to service account email, passed as it is
    (store_iam_service_accounts = module.identity.service_account_emails). Keys this module
    knows get store-level roles (see the README table); every key is optional, and keys it does
    not know (for example scheduler-invoker, pubsub-push) are ignored:
      health-sync -> FHIR store: roles/healthcare.fhirResourceEditor
      seeder      -> FHIR store: roles/healthcare.fhirResourceEditor
      v2-to-fhir  -> FHIR store: roles/healthcare.fhirResourceEditor;
                     HL7v2 store: roles/healthcare.hl7V2Consumer (get message, patch its labels)
      fhir-proxy  -> FHIR store: roles/healthcare.fhirResourceReader;
                     HL7v2 store: roles/healthcare.hl7V2Consumer (list and get messages)
      edge-vm     -> HL7v2 store: roles/healthcare.hl7V2Ingest (MLLP adapter)
      cuff-sim    -> HL7v2 store: roles/healthcare.hl7V2Ingest (HTTPS ingest transport)
    Bindings are chosen by key name only, so Terraform knows at plan time which exist even
    while the emails are still unknown (first apply, accounts not yet created).
  EOT
  type        = map(string)
  default     = {}

  validation {
    # Only the keys this module uses are checked; other keys are ignored, not validated.
    condition = alltrue([
      for k, v in var.store_iam_service_accounts :
      can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\\.iam\\.gserviceaccount\\.com$", v))
      if contains(["health-sync", "seeder", "v2-to-fhir", "fhir-proxy", "edge-vm", "cuff-sim"], k)
    ])
    error_message = "store_iam_service_accounts values for health-sync, seeder, v2-to-fhir, fhir-proxy, edge-vm and cuff-sim must be service account emails (name@project.iam.gserviceaccount.com)."
  }
}
