# Inputs for the one-time bootstrap root. Nothing author-specific has a default: the project ID,
# billing account and GitHub repository always come from the person running bootstrap
# (terraform.tfvars, git-ignored; see terraform.tfvars.example).

# ---------------------------------------------------------------------------------------------
# Project: create a new one, or adopt an existing one
# ---------------------------------------------------------------------------------------------

variable "create_project" {
  description = "true: create a new GCP project with var.project_id and link var.billing_account to it. false: adopt an existing project with var.project_id and create nothing at project level. No default, so the choice is always explicit."
  type        = bool
  nullable    = false
}

variable "project_id" {
  description = "ID of the GCP project to create (create_project = true) or adopt (create_project = false). Project IDs are globally unique, 6 to 30 characters, lowercase letters, digits and hyphens, starting with a letter."
  type        = string
  nullable    = false

  validation {
    # The GCP project ID rule: 6-30 chars, starts with a letter, no trailing hyphen.
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "project_name" {
  description = "Human-readable display name for a newly created project. Only used when create_project = true. Defaults to project_id."
  type        = string
  default     = null

  validation {
    # Display-name rule: 4-30 chars of letters, digits, spaces, hyphens, single and double quotes and exclamation marks.
    condition     = var.project_name == null || can(regex("^[A-Za-z0-9 '\"!-]{4,30}$", coalesce(var.project_name, "xxxx")))
    error_message = "project_name must be 4-30 characters of letters, digits, spaces, hyphens, quotes or exclamation marks."
  }

  validation {
    condition     = var.project_name == null || var.create_project
    error_message = "project_name is only used when create_project = true; leave it unset when adopting an existing project."
  }
}

variable "billing_account" {
  description = "Billing account ID (format XXXXXX-XXXXXX-XXXXXX) to link to a newly created project. Required when create_project = true; must be unset when adopting, because bootstrap does not change an existing project's billing."
  type        = string
  default     = null

  validation {
    condition     = var.billing_account == null || can(regex("^[A-F0-9]{6}-[A-F0-9]{6}-[A-F0-9]{6}$", coalesce(var.billing_account, "x")))
    error_message = "billing_account must look like 0123AB-4567CD-89EF01 (three groups of six uppercase hex characters)."
  }

  validation {
    # Cross-variable validation (Terraform >= 1.9): a new project without billing cannot enable
    # most APIs, so fail at plan time rather than half-way through an apply.
    condition     = var.create_project == (var.billing_account != null)
    error_message = "billing_account is required when create_project = true and must be unset when create_project = false."
  }
}

variable "org_id" {
  description = "Optional numeric organization ID to create the new project under. Leave unset (the default) for a personal Google account with no organization. Mutually exclusive with folder_id; only used when create_project = true."
  type        = string
  default     = null

  validation {
    condition     = var.org_id == null || can(regex("^[0-9]+$", coalesce(var.org_id, "x")))
    error_message = "org_id must be the numeric organization ID, for example 123456789012."
  }

  validation {
    condition     = var.org_id == null || var.folder_id == null
    error_message = "Set at most one of org_id and folder_id: a project has exactly one parent."
  }

  validation {
    condition     = var.org_id == null || var.create_project
    error_message = "org_id is only used when create_project = true; bootstrap never moves an adopted project."
  }
}

variable "folder_id" {
  description = "Optional numeric folder ID to create the new project under. Leave unset for a personal account. Mutually exclusive with org_id; only used when create_project = true."
  type        = string
  default     = null

  validation {
    condition     = var.folder_id == null || can(regex("^[0-9]+$", coalesce(var.folder_id, "x")))
    error_message = "folder_id must be the numeric folder ID, for example 123456789012."
  }

  validation {
    condition     = var.folder_id == null || var.create_project
    error_message = "folder_id is only used when create_project = true; bootstrap never moves an adopted project."
  }
}

variable "auto_create_network" {
  description = "Only for a newly created project: keep the 'default' VPC network GCP creates automatically. Default false, because that network ships with firewall rules open to the internet (SSH, RDP, ICMP) and the stack builds its own VPC (module project-base). With false, the provider enables the Compute Engine API to delete the default network."
  type        = bool
  default     = false
  nullable    = false
}

variable "labels" {
  description = "Labels added to the project (when created) and the state bucket, for example { app = \"pulsebridge\" }. Keys and values: lowercase letters, digits, hyphens and underscores; keys start with a letter."
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
# Remote state bucket
# ---------------------------------------------------------------------------------------------

variable "state_bucket_name" {
  description = "Name of the GCS bucket that holds Terraform state for every root (env roots use one prefix each). Bucket names are global. Defaults to \"<project_id>-tfstate\"."
  type        = string
  default     = null

  validation {
    condition     = var.state_bucket_name == null || can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", coalesce(var.state_bucket_name, "x")))
    error_message = "state_bucket_name must be 3-63 characters of lowercase letters, digits, hyphens, underscores and dots, starting and ending with a letter or digit."
  }

  validation {
    condition     = var.state_bucket_name == null || !startswith(coalesce(var.state_bucket_name, "x"), "goog")
    error_message = "Bucket names may not start with \"goog\"."
  }
}

variable "state_bucket_location" {
  description = "Location of the state bucket. Default us-central1: a single region inside the Cloud Storage always-free tier (us-central1, us-east1, us-west1), which is plenty for a few kilobytes of state."
  type        = string
  default     = "us-central1"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]+(-[a-z0-9]+)*$", lower(var.state_bucket_location))) && length(var.state_bucket_location) >= 2
    error_message = "state_bucket_location must be a GCS location such as us-central1, europe-west2 or EU."
  }
}

variable "state_noncurrent_versions_to_keep" {
  description = "How many older (noncurrent) versions of each state file the bucket keeps for recovery. Older ones are deleted by a lifecycle rule so the bucket does not grow forever."
  type        = number
  default     = 10
  nullable    = false

  validation {
    condition     = var.state_noncurrent_versions_to_keep >= 1 && floor(var.state_noncurrent_versions_to_keep) == var.state_noncurrent_versions_to_keep
    error_message = "state_noncurrent_versions_to_keep must be a whole number of at least 1; keeping no old versions defeats the point of versioning."
  }
}

# ---------------------------------------------------------------------------------------------
# Deployer service account
# ---------------------------------------------------------------------------------------------

variable "deployer_account_id" {
  description = "account_id (the part before @) of the deployer service account that env roots and CI run as. 6-30 characters."
  type        = string
  default     = "terraform-deployer"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.deployer_account_id))
    error_message = "deployer_account_id must be 6-30 characters of lowercase letters, digits and hyphens, start with a letter and not end with a hyphen."
  }
}

variable "deployer_roles" {
  description = "Project-level IAM roles granted to the deployer service account. The default is the smallest set of predefined roles that can create the whole PulseBridge stack; see README.md (\"Deployer roles\") for the reasoning. Basic roles (owner, editor) are rejected."
  type        = list(string)
  nullable    = false
  default = [
    # Enable the stack's APIs (module project-base).
    "roles/serviceusage.serviceUsageAdmin",
    # VPC, subnet, Cloud NAT, static IP (networkAdmin); firewall rules (securityAdmin);
    # the e2-micro and its disk (instanceAdmin.v1).
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/compute.instanceAdmin.v1",
    # Container images (module project-base).
    "roles/artifactregistry.admin",
    # One service account per service, and attaching them to Cloud Run, the VM and Scheduler.
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    # Project-level IAM bindings for those service accounts (module identity).
    # This is the most powerful role in the list; README.md explains the trade-off.
    "roles/resourcemanager.projectIamAdmin",
    # Secrets without values, and their accessor bindings (module identity).
    "roles/secretmanager.admin",
    # Healthcare dataset, FHIR R4 store, HL7v2 store and their IAM (module healthcare).
    "roles/healthcare.datasetAdmin",
    "roles/healthcare.fhirStoreAdmin",
    "roles/healthcare.hl7V2StoreAdmin",
    # Topic, push subscription, dead-letter topic (module healthcare).
    "roles/pubsub.admin",
    # Cloud Run services and their invoker bindings (modules ingestion, phr).
    "roles/run.admin",
    # Scheduler job that triggers health-sync (module ingestion).
    "roles/cloudscheduler.admin",
    # Dashboard bucket (module phr).
    "roles/storage.admin",
  ]

  validation {
    condition     = length(var.deployer_roles) > 0
    error_message = "deployer_roles must not be empty: a deployer with no roles cannot deploy anything."
  }

  validation {
    # Role names look like roles/<service>.<role> (predefined), roles/<name> (basic: rejected by
    # the next validation, with its own message) or projects/<p>/roles/<r> (project custom role).
    condition = alltrue([
      for r in var.deployer_roles :
      can(regex("^(roles/[a-zA-Z0-9]+(\\.[a-zA-Z0-9.]+)?|projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/[a-zA-Z0-9_.]+)$", r))
    ])
    error_message = "Each deployer role must be a role name such as roles/<service>.<role> or a project custom role (projects/<project>/roles/<role>)."
  }

  validation {
    # Basic roles are far broader than any deployment needs. roles/owner in particular could
    # grant itself anything, delete the project, and remove every human's access.
    condition     = length(setintersection(toset(var.deployer_roles), toset(["roles/owner", "roles/editor", "roles/viewer"]))) == 0
    error_message = "Basic roles (roles/owner, roles/editor, roles/viewer) are not allowed for the deployer; use predefined roles."
  }
}

# ---------------------------------------------------------------------------------------------
# Workload Identity Federation for GitHub Actions
# ---------------------------------------------------------------------------------------------

variable "github_repository" {
  description = "The ONE GitHub repository, as \"owner/name\", whose Actions workflows may impersonate the deployer service account. Matched exactly (case-sensitive) against the `repository` claim of GitHub's OIDC token."
  type        = string
  nullable    = false

  validation {
    # GitHub owner: 1-39 alphanumerics or single hyphens; repository: letters, digits, '.', '_', '-'.
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$", var.github_repository))
    error_message = "github_repository must be \"owner/name\", for example \"octo-org/pulsebridge\"."
  }
}

variable "github_repository_id" {
  description = "Optional numeric ID of the GitHub repository (`gh api repos/OWNER/NAME --jq .id`). When set, the attribute condition also pins this ID, so a different repository later created under the same owner/name (after a rename or deletion) cannot match. Recommended."
  type        = string
  default     = null

  validation {
    condition     = var.github_repository_id == null || can(regex("^[0-9]+$", coalesce(var.github_repository_id, "x")))
    error_message = "github_repository_id must be the numeric repository ID, for example 123456789."
  }
}

variable "wif_pool_id" {
  description = "ID of the Workload Identity pool. 4-32 lowercase letters, digits and hyphens; the \"gcp-\" prefix is reserved. A deleted pool's ID cannot be reused for 30 days."
  type        = string
  default     = "github-actions"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9-]{4,32}$", var.wif_pool_id)) && !startswith(var.wif_pool_id, "gcp-")
    error_message = "wif_pool_id must be 4-32 lowercase letters, digits and hyphens and must not start with \"gcp-\"."
  }
}

variable "wif_provider_id" {
  description = "ID of the OIDC provider inside the pool. 4-32 lowercase letters, digits and hyphens; the \"gcp-\" prefix is reserved."
  type        = string
  default     = "github-oidc"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9-]{4,32}$", var.wif_provider_id)) && !startswith(var.wif_provider_id, "gcp-")
    error_message = "wif_provider_id must be 4-32 lowercase letters, digits and hyphens and must not start with \"gcp-\"."
  }
}
