variable "project_id" {
  description = "ID of the GCP project that holds the service accounts and secrets."
  type        = string

  validation {
    # GCP project IDs: 6 to 30 characters, lower-case letters, digits and hyphens, starting with
    # a letter and not ending with a hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be a valid GCP project ID (6-30 characters, a-z, 0-9 and -, starting with a letter)."
  }
}

variable "name_prefix" {
  description = "Prefix for every service account ID and secret ID this module creates, for example \"pb\" gives \"pb-health-sync\". Keeps PulseBridge's identities recognisable in a shared project."
  type        = string
  default     = "pb"

  validation {
    # A service account ID is at most 30 characters. The longest suffix is "scheduler-invoker"
    # (17 characters) plus the joining hyphen, which leaves 12 for the prefix.
    condition     = can(regex("^[a-z]([a-z0-9-]{0,10}[a-z0-9])?$", var.name_prefix))
    error_message = "name_prefix must be 1-12 characters of a-z, 0-9 and -, start with a letter and not end with a hyphen."
  }
}

variable "create_seeder_account" {
  description = "Create a service account for the synthetic-data seeder (task T09). Set it to the env root's use_synthetic_data flag, so a deployment that uses real watch data has no spare identity that can write to the FHIR store."
  type        = bool
  default     = false
}

variable "grant_pubsub_token_creator" {
  description = "Let the project's Pub/Sub service agent mint OIDC tokens for the pubsub-push account. Only projects created before 8 April 2021 need this; newer projects already allow it. The grant is on the pubsub-push account only, not project-wide."
  type        = bool
  default     = false
}

variable "secret_labels" {
  description = "Extra labels for every Secret Manager secret, merged over the module's own labels (component, managed-by)."
  type        = map(string)
  default     = {}

  validation {
    # Secret Manager label keys and values: lower-case letters, digits, '_' and '-', at most 63
    # characters; keys must start with a letter.
    condition = alltrue([
      for k, v in var.secret_labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", k)) && can(regex("^[a-z0-9_-]{0,63}$", v))
    ])
    error_message = "secret_labels keys must match ^[a-z][a-z0-9_-]{0,62}$ and values ^[a-z0-9_-]{0,63}$."
  }
}

variable "secret_deletion_protection" {
  description = "Refuse to destroy the secrets (Secret Manager deletion_protection). Leave false in dev, which is destroyed nightly; consider true in prod so a stray destroy cannot drop the refresh token or the OpenEMR key."
  type        = bool
  default     = false
}

# ----------------------------------------------------------------------------------------------
# Optional reviewer password for dev (decision D9, docs/spikes/s6.md).
#
# Two variables, because an ephemeral value cannot decide whether a resource exists: Terraform
# refuses an ephemeral value in `count`, since it is not saved between plan and apply. The
# non-secret version number switches the secret version on (> 0) and rotates it (increase it);
# the password itself is ephemeral and only ever reaches the write-only argument.
# ----------------------------------------------------------------------------------------------

variable "reviewer_password" {
  description = "Dev only: the shared reviewer password, written as the first version of the reviewer-password secret through the write-only argument secret_data_wo, so it never enters Terraform state or plan files. Used only when reviewer_password_wo_version > 0. Leave unset in prod; scripts/secrets/set-reviewer-password (T27) sets it there. At least 16 characters."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true

  validation {
    # The proxy derives its token-signing key from this password (s6.md), so it must be long
    # enough not to be guessed. The same minimum is enforced by the T27 script.
    condition     = var.reviewer_password == null || length(coalesce(var.reviewer_password, "")) >= 16
    error_message = "reviewer_password must be at least 16 characters."
  }
}

variable "reviewer_password_wo_version" {
  description = "Dev only: 0 (the default) creates no reviewer-password version. 1 or more creates one from reviewer_password; increase it to write a new password, because Terraform cannot see a write-only value change and only sends it when this number changes."
  type        = number
  default     = 0
  nullable    = false

  validation {
    condition     = var.reviewer_password_wo_version >= 0 && floor(var.reviewer_password_wo_version) == var.reviewer_password_wo_version
    error_message = "reviewer_password_wo_version must be a whole number, 0 or more."
  }

  validation {
    condition     = var.reviewer_password_wo_version == 0 || var.reviewer_password != null
    error_message = "reviewer_password must be set when reviewer_password_wo_version is greater than 0."
  }
}
